#!/usr/bin/env python3
"""Re-point Home Assistant's Apple TV entries when DHCP moves them.

Why this exists instead of a DHCP reservation: the Austin apartment runs a
complex-managed network (10.131.40.0/22). The gateway has no admin interface we
can reach, so there is nowhere to pin a lease -- and the lease is 24 hours, so
the Apple TVs can land on new addresses any day.

When that happens the failure is silent and awful to diagnose: HA keeps the old
address, `media_player.turn_on` returns **HTTP 200**, and nothing happens. The
entity just sits `off` with an empty source_list, which looks exactly like the
degraded-Companion-session problem that has a completely different fix.

Discovery cannot rescue it either -- the complex blocks mDNS, so both
`dns-sd -B _airplay._tcp` and `pyatv.scan()` return zero devices with the TVs
awake. Unicast works fine, so this scans for AirPlay's port 7000 and identifies
each hit over unicast.

The repair is safe because pyatv keys devices on hardware identifiers, not IP:
the stored pairing credentials survive an address change, so only `address` in
core.config_entries needs rewriting. No re-pairing, no PINs.

Run with --dry-run to see what it would change. HA must be stopped before a
write, or it will overwrite .storage from memory on shutdown.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import pathlib
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

REPO = pathlib.Path(__file__).resolve().parent.parent
ENTRIES = REPO / "data" / "homeassistant" / ".storage" / "core.config_entries"
AIRPLAY_PORT = 7000
HA_CONTAINER = "jarvis-homeassistant"


def local_subnet() -> str | None:
    """The /22 (or whatever) this machine is actually on, from the routing table."""
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("inet ") or "127.0.0.1" in line:
            continue
        parts = line.split()
        ip = parts[1]
        if "netmask" not in parts:
            continue
        mask_hex = parts[parts.index("netmask") + 1]
        try:
            bits = bin(int(mask_hex, 16)).count("1")
            return str(ipaddress.ip_network(f"{ip}/{bits}", strict=False))
        except Exception:
            continue
    return None


def port_open(ip: str, port: int, timeout: float = 0.6) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((ip, port)) == 0


def find_airplay_hosts(cidr: str) -> list[str]:
    net = ipaddress.ip_network(cidr, strict=False)
    hosts = [str(h) for h in net.hosts()]
    found: list[str] = []
    # Threads, not processes: this is pure socket wait. 256 keeps a /22 under a
    # minute without exhausting file descriptors.
    with ThreadPoolExecutor(max_workers=256) as pool:
        for ip, ok in zip(hosts, pool.map(lambda h: port_open(h, AIRPLAY_PORT), hosts)):
            if ok:
                found.append(ip)
    return found


def identify(hosts: list[str]) -> dict[str, str]:
    """Map hardware identifier -> ip, using pyatv inside the HA container.

    pyatv lives in that container, and asking it there also proves the address
    is reachable from the process that will actually use it -- which is the
    thing that matters, not whether this script can see it.
    """
    if not hosts:
        return {}
    script = (
        "import asyncio,json\n"
        "from pyatv import scan\n"
        "async def m():\n"
        f"    r=await scan(asyncio.get_event_loop(), timeout=20, hosts={hosts!r})\n"
        "    print(json.dumps([{'id':a.identifier,'ip':str(a.address),'name':str(a.name)} for a in r]))\n"
        "asyncio.run(m())\n"
    )
    try:
        out = subprocess.run(
            ["docker", "exec", HA_CONTAINER, "python", "-c", script],
            capture_output=True, text=True, timeout=120,
        ).stdout
    except Exception as exc:
        print(f"  pyatv identify failed: {type(exc).__name__}: {exc}")
        return {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("["):
            try:
                return {d["id"]: (d["ip"], d["name"]) for d in json.loads(line)}
            except Exception:
                pass
    return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without touching anything")
    args = ap.parse_args()

    if not ENTRIES.exists():
        print(f"  no config entries at {ENTRIES}")
        return 1

    data = json.loads(ENTRIES.read_text(encoding="utf-8"))
    atv = [e for e in data["data"]["entries"] if e.get("domain") == "apple_tv"]
    if not atv:
        print("  no apple_tv config entries")
        return 0

    stale = []
    for e in atv:
        addr = e.get("data", {}).get("address")
        if addr and not port_open(addr, AIRPLAY_PORT):
            stale.append(e)
        else:
            print(f"  OK   {e.get('title')} -> {addr}")
    if not stale:
        print("  all Apple TVs reachable; nothing to do")
        return 0

    print(f"  {len(stale)} unreachable; scanning for AirPlay hosts…")
    cidr = local_subnet()
    if not cidr:
        print("  could not determine the local subnet")
        return 1
    hosts = find_airplay_hosts(cidr)
    print(f"  AirPlay hosts on {cidr}: {hosts or 'none'}")
    by_id = identify(hosts)
    if not by_id:
        print("  could not identify any device (are the TVs awake?)")
        return 1

    changed = 0
    for e in stale:
        ids = [str(x) for x in (e.get("data", {}).get("identifiers") or [])]
        match = next((v for k, v in by_id.items() if k in ids), None)
        if not match:
            print(f"  NO MATCH for {e.get('title')} (identifiers {ids[:1]})")
            continue
        new_ip, name = match
        old = e["data"].get("address")
        if old == new_ip:
            continue
        print(f"  FIX  {e.get('title')}: {old} -> {new_ip}  ({name})")
        e["data"]["address"] = new_ip
        changed += 1

    if not changed:
        print("  nothing to rewrite")
        return 0
    if args.dry_run:
        print("  --dry-run: not writing")
        return 0

    # HA holds .storage in memory and flushes on shutdown, so a write while it
    # runs gets clobbered. Caller is expected to stop it; refuse otherwise.
    running = subprocess.run(
        ["docker", "ps", "--filter", f"name={HA_CONTAINER}", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=20).stdout.strip()
    if running:
        print(f"  REFUSING to write while {HA_CONTAINER} is running "
              f"(it would overwrite this on shutdown).")
        print(f"  docker stop {HA_CONTAINER} && {sys.argv[0]} && docker start {HA_CONTAINER}")
        return 2

    backup = ENTRIES.with_suffix(f".bak.{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(ENTRIES, backup)
    ENTRIES.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"  rewrote {changed} entry/entries (backup: {backup.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
