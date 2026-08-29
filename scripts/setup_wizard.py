#!/usr/bin/env python3
"""Interactive first-run setup for Jarvis.

`setup.sh` copies .env.example and stops, which leaves a new user hand-editing
136 variables and discovering their mistakes as runtime failures hours later —
a wrong Anthropic key looks exactly like a broken install.

This asks only for what is actually needed, checks each key against the real
service before writing it, and leaves everything else alone. A key that fails
its check is never silently accepted.

Design notes:
  - Edits .env IN PLACE, preserving comments, ordering and any values already
    set. Re-running is safe and is the intended way to add an integration later.
  - Only the Anthropic key is required. Everything else is offered one at a time
    and skipping is a first-class answer — tools without keys report themselves
    as unconfigured rather than breaking.
  - Validation uses the cheapest endpoint that proves the credential works.
    Nothing here spends money.

Usage:  python3 scripts/setup_wizard.py
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"

TIMEOUT = 15


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------

_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def bold(t: str) -> str:  return _c("1", t)
def green(t: str) -> str: return _c("32", t)
def red(t: str) -> str:   return _c("31", t)
def dim(t: str) -> str:   return _c("2", t)
def gold(t: str) -> str:  return _c("33", t)


def heading(text: str) -> None:
    print(f"\n{bold(text)}\n{dim('─' * len(text))}")


def ask(prompt: str, default: str = "", secret_hint: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    if secret_hint:
        suffix += dim("  (paste it; input is visible)")
    try:
        raw = input(f"  {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.")
        sys.exit(1)
    return raw or default


def ask_yes(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = ask(f"{prompt} ({hint})").lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False


# ---------------------------------------------------------------------------
# .env read/write — in place, comments preserved
# ---------------------------------------------------------------------------

# .env.example ships instructive placeholders rather than empty values, e.g.
# ANTHROPIC_API_KEY=sk-ant-your-key-here. Treating those as "already set" is
# how a new user ends up with a .env full of placeholders and an install that
# fails everywhere at once — the exact outcome this wizard exists to prevent.
_PLACEHOLDER_PAT = re.compile(
    r"\byour[-_ ]|change[-_ ]?me|replace[-_ ]?me|"
    r"placeholder|^<.*>$|example\.com|xxx+|todo|paste[-_ ]?here|here$",
    re.IGNORECASE,
)


def is_configured(value: str) -> bool:
    """True only for a value a human actually supplied."""
    v = (value or "").strip().strip('"').strip("'")
    if not v:
        return False
    return not _PLACEHOLDER_PAT.search(v)


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV.exists():
        return values
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip()
    return values


def write_env(updates: dict[str, str]) -> None:
    """Apply updates to .env, keeping its comments and ordering intact.

    Rewriting the file from a dict would throw away the documentation in
    .env.example, which is most of what makes the file navigable later.
    """
    if not updates:
        return
    lines = ENV.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)

    for i, line in enumerate(lines):
        if line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"

    if remaining:  # keys not present in the template
        lines += ["", "# Added by setup_wizard"]
        lines += [f"{k}={v}" for k, v in remaining.items()]

    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Validators — each returns (ok, message)
# ---------------------------------------------------------------------------

def _get(url: str, headers: dict[str, str], data: Optional[bytes] = None) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers, data=data,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read(400).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(400).decode("utf-8", "replace")
    except Exception as e:                                  # DNS, TLS, refused
        return 0, str(e)


def check_anthropic(key: str) -> tuple[bool, str]:
    # /v1/models is a plain listing — proves the key without spending tokens.
    status, body = _get("https://api.anthropic.com/v1/models",
                        {"x-api-key": key, "anthropic-version": "2023-06-01"})
    if status == 200:
        return True, "key accepted"
    if status == 401:
        return False, "rejected (401) — check for a typo or a revoked key"
    if status == 0:
        return False, f"could not reach Anthropic ({body[:60]})"
    return False, f"unexpected response {status}"


def check_tavily(key: str) -> tuple[bool, str]:
    payload = json.dumps({"api_key": key, "query": "test", "max_results": 1}).encode()
    status, body = _get("https://api.tavily.com/search",
                        {"Content-Type": "application/json"}, payload)
    if status == 200:
        return True, "key accepted"
    if status in (401, 403):
        return False, f"rejected ({status})"
    return False, f"unexpected response {status}"


def check_firecrawl(key: str) -> tuple[bool, str]:
    payload = json.dumps({"url": "https://example.com"}).encode()
    status, body = _get("https://api.firecrawl.dev/v1/scrape",
                        {"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"}, payload)
    if status in (200, 402):     # 402 = valid key, no credit left
        return True, "key accepted" if status == 200 else "valid, but out of credit"
    if status in (401, 403):
        return False, f"rejected ({status})"
    return False, f"unexpected response {status}"


def check_google_maps(key: str) -> tuple[bool, str]:
    status, body = _get(
        f"https://maps.googleapis.com/maps/api/geocode/json?address=Austin&key={key}", {})
    if status == 200 and '"REQUEST_DENIED"' not in body:
        return True, "key accepted"
    if '"REQUEST_DENIED"' in body:
        m = re.search(r'"error_message"\s*:\s*"([^"]+)"', body)
        return False, m.group(1) if m else "request denied"
    return False, f"unexpected response {status}"


def check_github(token: str) -> tuple[bool, str]:
    status, body = _get("https://api.github.com/user",
                        {"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json"})
    if status == 200:
        m = re.search(r'"login"\s*:\s*"([^"]+)"', body)
        return True, f"authenticated as {m.group(1)}" if m else "token accepted"
    return False, f"rejected ({status})"


def check_home_assistant(url: str, token: str) -> tuple[bool, str]:
    status, body = _get(url.rstrip("/") + "/api/",
                        {"Authorization": f"Bearer {token}"})
    if status == 200:
        return True, "connected"
    if status == 401:
        return False, "token rejected (401)"
    if status == 0:
        return False, f"could not reach {url} ({body[:50]})"
    return False, f"unexpected response {status}"


def check_elevenlabs(key: str) -> tuple[bool, str]:
    status, body = _get("https://api.elevenlabs.io/v1/user", {"xi-api-key": key})
    if status == 200:
        return True, "key accepted"
    return False, f"rejected ({status})"


# ---------------------------------------------------------------------------
# Prompt flow
# ---------------------------------------------------------------------------

def prompt_key(
    label: str,
    var: str,
    current: str,
    validator: Optional[Callable[[str], tuple[bool, str]]],
    where: str,
    required: bool = False,
) -> Optional[str]:
    """Ask for one credential, validating before accepting it.

    Returns the value to store, or None to leave the variable untouched.
    """
    if is_configured(current):
        print(f"  {green('✓')} {label} already set", end="")
        if validator and ask_yes(" — re-check it?", default=False):
            ok, msg = validator(current)
            print(f"    {green('✓') if ok else red('✗')} {msg}")
            if ok:
                return None
            print(dim("    Enter a new value, or press Enter to keep it anyway."))
        else:
            print()
            return None

    print(f"\n  {bold(label)}")
    print(dim(f"    {where}"))
    if not required:
        print(dim("    Press Enter to skip — Jarvis runs fine without it."))

    while True:
        value = ask(var, secret_hint=True)
        if not value:
            if required:
                print(red("    This one is required."))
                continue
            return None

        if not validator:
            return value

        print(dim("    checking…"))
        ok, msg = validator(value)
        if ok:
            print(f"    {green('✓')} {msg}")
            return value

        print(f"    {red('✗')} {msg}")
        # A key that fails its check is the whole reason this wizard exists —
        # storing it anyway would just move the failure to runtime.
        if ask_yes("    Try a different value?", default=True):
            continue
        return value if ask_yes("    Save it unchecked anyway?", default=False) else None


def check_prerequisites() -> bool:
    heading("Prerequisites")
    ok = True
    for name, cmd in (("Docker", ["docker", "--version"]),
                      ("Docker Compose", ["docker", "compose", "version"])):
        exe = shutil.which(cmd[0])
        if not exe:
            print(f"  {red('✗')} {name} not found")
            ok = False
            continue
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            if out.returncode == 0:
                print(f"  {green('✓')} {out.stdout.strip().splitlines()[0]}")
            else:
                print(f"  {red('✗')} {name} present but not working")
                ok = False
        except Exception as e:
            print(f"  {red('✗')} {name}: {e}")
            ok = False

    free_gb = shutil.disk_usage(ROOT).free / 1e9
    if free_gb < 15:
        print(f"  {gold('!')} {free_gb:.0f} GB free — the images need roughly 15 GB")
    else:
        print(f"  {green('✓')} {free_gb:.0f} GB free")

    if not ok:
        print(f"\n  {red('Install Docker first:')} https://docs.docker.com/get-docker/")
    return ok


def main() -> int:
    print(bold("\nJarvis setup\n"))
    print("This asks for the few things Jarvis needs, checks each one against the")
    print("real service, and writes them to .env. Safe to re-run any time — it")
    print("keeps what is already there and only fills gaps.\n")

    if not ENV_EXAMPLE.exists():
        print(red("  .env.example is missing — are you in the repo root?"))
        return 1

    if not ENV.exists():
        shutil.copy(ENV_EXAMPLE, ENV)
        print(f"  {green('✓')} created .env from the template")
    else:
        print(f"  {green('✓')} .env exists — existing values will be kept")

    docker_ok = check_prerequisites()

    env = read_env()
    updates: dict[str, str] = {}
    skipped: list[str] = []

    # --- required ---------------------------------------------------------
    heading("Required")
    val = prompt_key(
        "Anthropic API key", "ANTHROPIC_API_KEY", env.get("ANTHROPIC_API_KEY", ""),
        check_anthropic, "console.anthropic.com → API keys. Billed to your account.",
        required=True)
    if val:
        updates["ANTHROPIC_API_KEY"] = val

    # A shared secret between the phone app and the gateway. Users should not
    # have to invent this, and a weak one is worse than a generated one.
    if not is_configured(env.get("MOBILE_API_KEY", "")):
        generated = secrets.token_urlsafe(24)
        updates["MOBILE_API_KEY"] = generated
        print(f"\n  {green('✓')} generated MOBILE_API_KEY (app ↔ gateway shared secret)")
        print(dim(f"    {generated}"))
        print(dim("    You will paste this into the iOS app's settings later."))
    else:
        print(f"  {green('✓')} MOBILE_API_KEY already set")

    # --- personal context -------------------------------------------------
    heading("About you")
    print(dim("  Used for weather, greetings and scheduling. Not sent anywhere\n"
              "  except the services you enable."))
    current_loc = env.get("WEATHER_LOCATION", "")
    if not is_configured(current_loc):
        current_loc = ""
    loc = ask("Weather location, e.g. 'Austin, TX'", current_loc)
    if loc and loc != current_loc:
        updates["WEATHER_LOCATION"] = loc

    # --- optional integrations -------------------------------------------
    heading("Optional integrations")
    print(dim("  Skip anything you do not want yet. Re-run this wizard to add more\n"
              "  later — each unconfigured tool simply reports itself unavailable."))

    optional: list[tuple[str, str, Optional[Callable[[str], tuple[bool, str]]], str]] = [
        ("Tavily (web search + research agent)", "TAVILY_API_KEY",
         check_tavily, "tavily.com — free tier is generous"),
        ("Firecrawl (reliable page scraping)", "FIRECRAWL_API_KEY",
         check_firecrawl, "firecrawl.dev"),
        ("Google Maps (directions, commute)", "GOOGLE_MAPS_API_KEY",
         check_google_maps, "console.cloud.google.com — enable Geocoding + Directions"),
        ("GitHub (repo tools, Forge)", "GITHUB_TOKEN",
         check_github, "github.com/settings/tokens — needs repo scope"),
        ("ElevenLabs (natural voice)", "ELEVENLABS_API_KEY",
         check_elevenlabs, "elevenlabs.io — optional, macOS voice works without it"),
    ]
    for label, var, validator, where in optional:
        got = prompt_key(label, var, env.get(var, ""), validator, where)
        if got:
            updates[var] = got
        elif not is_configured(env.get(var, "")):
            skipped.append(label)

    # Home Assistant needs two values that only make sense together.
    heading("Home Assistant (lights, scenes, media)")
    if is_configured(env.get("HA_TOKEN", "")):
        print(f"  {green('✓')} already configured")
    elif ask_yes("  Set up Home Assistant now?", default=False):
        ha_default = env.get("HA_URL", "")
        if not is_configured(ha_default):
            ha_default = "http://homeassistant:8123"
        ha_url = ask("HA_URL", ha_default)
        ha_token = ask("HA_TOKEN (long-lived access token)", secret_hint=True)
        if ha_url and ha_token:
            print(dim("    checking…"))
            ok, msg = check_home_assistant(ha_url, ha_token)
            print(f"    {green('✓') if ok else red('✗')} {msg}")
            if ok or ask_yes("    Save anyway?", default=False):
                updates["HA_URL"] = ha_url
                updates["HA_TOKEN"] = ha_token
    else:
        skipped.append("Home Assistant")

    write_env(updates)

    # --- summary ----------------------------------------------------------
    heading("Summary")
    if updates:
        print(f"  {green('✓')} wrote {len(updates)} value(s) to .env")
        for k in updates:
            print(dim(f"    {k}"))
    else:
        print("  nothing changed — .env was already complete")
    if skipped:
        print(f"\n  {dim('skipped (re-run this wizard to add):')}")
        for s in skipped:
            print(dim(f"    · {s}"))

    heading("Next")
    if not docker_ok:
        print(f"  1. Install Docker, then re-run this wizard")
        print(f"  2. {bold('docker compose up -d')}")
    else:
        print(f"  1. {bold('docker compose up -d')}          start the stack (~2 min on prebuilt images)")
        print(f"  2. {bold('curl localhost:8080/health')}    confirm the gateway answers")
        print(f"  3. Open {bold('http://localhost:8888')}     the dashboard")
        print(f"  4. Say {bold(chr(34) + 'personalize jarvis' + chr(34))}       conversational onboarding for profile details")
    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  Cancelled — nothing was written.")
        sys.exit(1)
