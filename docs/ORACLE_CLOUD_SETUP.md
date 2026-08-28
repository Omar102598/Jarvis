# Oracle Cloud Always-Free Setup (the $0 test bed)

The cheap way to shake out the cloud-hosting kinks before paying for Hetzner.
Oracle's Always-Free ARM tier gives **2 OCPU / 12 GB RAM** at **$0/mo forever**
— 1.5× the Hetzner CPX32's RAM for free. If Jarvis runs here, it will run
anywhere.

> ⚠️ **Oracle halved this tier (confirmed 2026-07-27).** It was 4 OCPU / 24 GB;
> the official Always-Free docs now state **2 OCPU / 12 GB** (1,500 OCPU-hours
> / 9,000 GB-hours per month). Verify the current number in the docs before
> sizing — Oracle changed it quietly, without announcement.
>
> ⚠️ **Only `VM.Standard.A1.Flex` is Always-Free eligible.** The console may
> default you to a NEWER shape (e.g. `VM.Standard.A2.Flex`) that is **NOT** on
> the free list and **bills by the hour**. Always confirm the shape carries the
> **"Always Free-eligible"** badge in the console before creating.

Same repo, same compose files as the Hetzner plan
(`docker-compose.core.yml` + `docker-compose.edge.yml`); only the provisioning
differs. Once this works, `docs/HETZNER_SETUP.md` §2–7 applies verbatim.

## Why this is the right experiment first

- **$0** — no card burn while we iterate on the split.
- **ARM64** — the stack already runs on Apple Silicon, so images build clean.
- **12 GB RAM** — Chroma + Whisper + Redis + 17 MCP servers fit with headroom
  (the 8 GB Hetzner box is the tighter target, so watch `docker stats` before
  assuming Hetzner fits — but 12 GB is a much closer predictor than 24 GB was).

## Known caveats (read before starting)

1. **Capacity errors are normal — and can be permanent.** Free ARM capacity is
   often exhausted: "Out of host capacity" on create. Use
   `scripts/oci_launch_retry.sh` to hunt it automatically across all ADs.
   ⚠️ **Real result (2026-07-27 → 07-30): ~1,870 attempts across 3 days in
   `us-chicago-1` — all three ADs empty the entire time, never landed.**
   Because Always-Free resources must be in your **home region**, and home
   region **cannot be changed**, a saturated home region means the free tier
   is simply unavailable to you — no amount of retrying fixes it.
   **Recommendation: budget one day for the attempt, then move on.** Paid
   hosting (Hetzner CAX21, ARM, 4 vCPU / 8 GB, ~€10.49/mo) is the fallback and
   was the eventual destination anyway. This doc is kept for the day Oracle
   frees up capacity, or for readers in a less saturated home region.
2. **Idle reclamation.** Oracle may reclaim free instances idle for 7 days
   (<10% CPU, low network). Jarvis's agents + MQTT keep it busy enough that
   this shouldn't trigger, but it's a real risk for a box you leave dormant.
3. **Two firewalls.** Oracle has a cloud-level Security List *and* Ubuntu's
   iptables preloaded with rules. Forgetting the second is the classic "port
   is open but nothing connects" trap. We sidestep both by using Tailscale.
4. Free tier = no SLA. Fine for a test bed, not where the Hetzner box lands.

---

## 1. Account + instance

1. Sign up at cloud.oracle.com (needs a card for identity; free tier isn't
   charged). Pick your home region carefully — **it cannot be changed** and
   free ARM capacity varies by region. US Ashburn / Phoenix are reasonable.
2. Console → **Compute → Instances → Create Instance**.
   - **Image:** Canonical Ubuntu 24.04 (aarch64 build).
   - **Shape:** Change shape → **Ampere → `VM.Standard.A1.Flex`** →
     **2 OCPUs, 12 GB RAM** (the whole free allotment in one box).
     ⚠️ Confirm the shape shows **"Always Free-eligible"**. Do NOT accept a
     newer Ampere shape (A2.Flex etc.) — those are billed hourly. The console
     also defaults memory LOW (often 6 GB); drag it to the full 12 GB.
   - **Networking:** default VCN, assign a public IPv4.
   - **SSH keys:** paste your public key (`~/.ssh/jarvis_cloud.pub` works —
     reuse it, or generate a new one the same way).
3. If you get **"Out of host capacity"**: switch availability domain (AD-1 →
   AD-2 → AD-3) and retry, or wait and retry — capacity frees up constantly.

## 2. First login + base setup

```bash
ssh -i ~/.ssh/jarvis_cloud ubuntu@<public-ip>     # user is 'ubuntu', not root
```

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker ubuntu && newgrp docker
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up          # same tailnet as your Mac/iPhone
```

**Oracle-specific firewall note:** Ubuntu images here ship with iptables rules
that drop most inbound traffic, *plus* the cloud Security List. Because every
Jarvis service is reached over Tailscale, leave both closed and change nothing.
Only if you deliberately want public access would you open ports in **both**
places (Security List → Ingress Rules, and `iptables`/`ufw` on the host).

Checkpoint: `tailscale status` on your Mac lists the Oracle box, and
`ssh ubuntu@<oracle-tailnet-name>` works.

## 3. Swap (recommended)

Free ARM instances ship with no swap. Docker builds of the heavier images
(vision/InsightFace, Whisper) are happier with some:

```bash
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 4. Repo, env, data

Follow **`docs/HETZNER_SETUP.md` §2 and §3** exactly — clone, copy `.env`,
rsync the stateful data (Ring token, MCP OAuth cache, Whisper model, face
embeddings), and add the three VPS-specific `.env` lines. Nothing differs.

For a brand-new user (not a migration), skip the rsync and start from
`.env.example` — see `docs/NEW_USER_SETUP.md`.

## 5. Start the core stack

```bash
cd ~/Jarvis
docker compose -f docker-compose.core.yml up -d --build
docker compose -f docker-compose.core.yml logs -f llm_agent
```

**Expect the first build to be slow** (~15–30 min): ARM wheels compile from
source more often, and the 17 MCP servers pull cold npm/uvx caches on first
boot. Watch `docker stats` — climbing PIDs and network I/O mean progress, not
a hang. Wait for `Graph rebuilt — N tools`.

Checkpoint: from your Mac, `curl http://<oracle-tailnet>:8080/health` returns
OK and the dashboard loads at `http://<oracle-tailnet>:8888`.

## 6. Point the Mac + phone at it

Same as `docs/HETZNER_SETUP.md` §5: set `JARVIS_CORE_HOST` on the Mac, bring up
`docker-compose.edge.yml`, repoint the native services' `MQTT_HOST`, and set the
iPhone app's Server URL to `http://<oracle-tailnet>:8080`.

Then run the §5 checkpoint sweep (voice loop, app chat, camera, lights, Forge,
dashboard widgets).

## 7. What to watch for during the experiment

These are the things we actually want to learn before paying for Hetzner:

- **Voice round-trip latency** — wake word → answer, versus today's all-local
  setup. STT stays on the Mac, so the added hop is brain-only; if it feels
  sluggish here it will feel the same on Hetzner (Ashburn is comparable).
- **Memory ceiling** — run `docker stats` under load (a grocery run + a camera
  event + a chat). If total usage approaches 8 GB, the Hetzner CPX32 is too
  small and we size up before buying. Biggest single lever if it's tight:
  `STT_MODEL=base` on the VPS saves ~1.3 GB vs turbo (STT stays on the Mac in
  the edge split, so the VPS copy only serves the app's /ask/audio endpoint).
- **ARM image gaps** — anything that fails to build on aarch64 here would also
  fail on Hetzner's ARM (CAX) line but *not* on its x86 (CPX) line. Note them.
- **mac_bridge over tailnet** — Forge, signed-in browsing, and HA all cross the
  tunnel now. Confirm each works and how it behaves when the Mac sleeps.

## 8. Rollback

Identical to Hetzner's §7 — the Mac's original all-in-one `docker-compose.yml`
is untouched:

```bash
# Mac
docker compose -f docker-compose.edge.yml down && docker compose up -d
# Oracle
docker compose -f docker-compose.core.yml down
```

## 9. Migrating the lessons to Hetzner

When the experiment succeeds, Hetzner is the same procedure with three
differences: pick **CPX32** (x86) or **CAX21+** (ARM) per what you learned in
§7, expect a *much* faster first build, and there is no capacity lottery.
