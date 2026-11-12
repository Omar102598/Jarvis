"""Forge in the cloud: issue → GitHub Actions → PR → "Ship" approval → deploy.

The flow, end to end:

  1. run_task()      opens a GitHub issue labelled `forge` holding the task.
  2. .github/workflows/forge.yml runs Claude Code on a GitHub runner, gates it
                     on golden tasks and opens a PR from forge/issue-<n>.
  3. run_task()      sees the PR and files a "Ship" approval whose action is
                     {"type": "github_merge", "pr": n}.
  4. On approve,     approvals._execute_action → ship(): squash-merge the PR,
                     then (in a thread) wait for "Build images" to finish for
                     the merge commit and write jarvis:deploy:request.
  5. scripts/vm_deploy.sh (cron on the VM host) pulls develop and restarts the
                     services named in the request, then reports back.

Why not the old path: Forge ran `claude -p` on the Mac through mac_bridge, so
it needed the Mac awake, hit macOS TCC on ~/Documents when the bridge came from
launchd, billed the API key whenever one leaked into the env, and left its
edits uncommitted in the working copy with no route to production.

Uses urllib only (no new dependency) and the fine-grained GITHUB_TOKEN.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import urllib.error
import urllib.request

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GITHUB_REPO", "") or "Omar102598/Jarvis"
BASE_BRANCH = os.environ.get("FORGE_BASE_BRANCH", "develop")
LABEL = "forge"
# The workflow's own ceiling is 90 min; allow for queueing on top.
CLOUD_TIMEOUT_S = int(os.environ.get("FORGE_CLOUD_TIMEOUT_S", "6600"))
CI_TIMEOUT_S = int(os.environ.get("FORGE_CI_TIMEOUT_S", "3600"))
POLL_S = 30

DEPLOY_REQUEST_KEY = "jarvis:deploy:request"

# Services the VM runs from GHCR images (docker-compose.core.yml).
CORE_SERVICES = {"llm_agent", "agent_runner", "mobile_gateway", "dashboard",
                 "synapse", "vision"}
# Services that run natively on the Mac — a merge can't deploy these.
NATIVE_SERVICES = {"stt", "wake_word", "tts_mac", "speaker_verify", "mac_bridge"}


def enabled() -> bool:
    return bool(GITHUB_TOKEN)


# -- GitHub API ---------------------------------------------------------------

def _api(method: str, path: str, body: dict | None = None):
    url = path if path.startswith("http") else f"https://api.github.com/repos/{REPO}{path}"
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "jarvis-forge"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"GitHub {method} {path} → {exc.code}: {detail}") from None


def _ensure_label() -> None:
    try:
        _api("GET", f"/labels/{LABEL}")
    except RuntimeError:
        try:
            _api("POST", "/labels", {"name": LABEL, "color": "f0a020",
                                     "description": "Task for Forge (cloud Claude Code)"})
        except RuntimeError:
            pass   # a race or a missing permission shows up on issue creation


def _title_for(task: str) -> str:
    first = task.strip().splitlines()[0].strip().rstrip(".")
    return first if len(first) <= 90 else first[:87].rstrip() + "…"


# -- 1–3: dispatch and wait for the PR ----------------------------------------

async def run_task(agent, task: str) -> str:
    """Open the issue, wait for Forge's PR, file the Ship approval. Returns the report."""
    import approvals

    _ensure_label()
    issue = await asyncio.to_thread(_api, "POST", "/issues", {
        "title": _title_for(task),
        "body": task.strip() + "\n\n---\n_Filed by Jarvis for Forge._",
        "labels": [LABEL],
    })
    num, issue_url = issue["number"], issue["html_url"]
    agent._progress(f"Opened issue #{num} for Forge in the cloud: {issue_url}")

    head = f"{REPO.split('/')[0]}:forge/issue-{num}"
    deadline = time.time() + CLOUD_TIMEOUT_S
    seen_comments = 0
    pr = None
    while time.time() < deadline:
        await asyncio.sleep(POLL_S)
        try:
            prs = await asyncio.to_thread(_api, "GET", f"/pulls?head={head}&state=open")
            if prs:
                pr = prs[0]
                break
            comments = await asyncio.to_thread(_api, "GET", f"/issues/{num}/comments")
        except RuntimeError as exc:
            agent._progress(f"GitHub poll failed (will retry): {exc}")
            continue
        for c in comments[seen_comments:]:
            body = (c.get("body") or "").strip()
            agent._progress(f"#{num}: {body[:200]}")
            if body.startswith(("Forge finished without changing", "⚠️ Forge can't run")):
                return (f"Forge (cloud) on #{num} — no PR.\n\n{body[:3000]}\n\n{issue_url}")
        seen_comments = len(comments)

    if not pr:
        return (f"Forge (cloud) didn't open a PR for #{num} within "
                f"{CLOUD_TIMEOUT_S // 60} min. Check the run: {issue_url}")

    pr_num, pr_url = pr["number"], pr["html_url"]
    files = await asyncio.to_thread(_api, "GET", f"/pulls/{pr_num}/files?per_page=100")
    names = [f["filename"] for f in files]
    body = pr.get("body") or ""
    verdict = next((ln.strip("* ") for ln in body.splitlines() if "Golden tasks" in ln), "")
    plan = deploy_plan(names)

    summary = (f"PR #{pr_num}: {pr['title']}\n{verdict}\n"
               f"Files: {', '.join(names[:12])}{' …' if len(names) > 12 else ''}\n"
               f"Ships: {plan['describe']}\n{pr_url}")
    approval_id = approvals.request_approval(
        agent.r, "Forge", f"Ship: {pr['title']}"[:80], summary,
        action={"type": "github_merge", "pr": pr_num})
    agent._progress(f"PR #{pr_num} ready — Ship approval {approval_id} filed.")
    report = _strip_pr_body(body)
    return (f"Forge (cloud) opened PR #{pr_num} for #{num}: {pr['title']}\n{pr_url}\n"
            f"{verdict}\n\nApprove “Ship” in the Approval Inbox to merge and deploy "
            f"({plan['describe']}).\n\n{report[:2500]}")


def _strip_pr_body(body: str) -> str:
    """The PR body minus the boilerplate, for the chat report."""
    keep = []
    for ln in body.splitlines():
        if ln.startswith(("Closes #", "<details>", "🤖 Generated")):
            if ln.startswith("<details>"):
                break
            continue
        keep.append(ln)
    return "\n".join(keep).strip()


# -- what a merge needs to deploy ---------------------------------------------

def deploy_plan(files: list[str]) -> dict:
    services, native = set(), set()
    restart_runner = False
    for f in files:
        parts = f.split("/")
        if parts[0] == "services" and len(parts) > 2:
            if parts[1] in CORE_SERVICES:
                services.add(parts[1])
            elif parts[1] in NATIVE_SERVICES:
                native.add(parts[1])
        elif f.startswith("config/prompts/agents/") or f == "config/agents.yml":
            restart_runner = True
    bits = []
    if services:
        bits.append("rebuild + restart " + ", ".join(sorted(services)))
    if restart_runner and "agent_runner" not in services:
        bits.append("restart agent_runner (agent prompts)")
    if any(f.startswith("config/") for f in files) and not bits:
        bits.append("config only — live after git pull")
    if native:
        bits.append("Mac services need a manual restart: " + ", ".join(sorted(native)))
    return {"services": sorted(services), "restart_agent_runner": restart_runner,
            "native": sorted(native), "describe": "; ".join(bits) or "nothing to restart"}


# -- 4: approve → merge → wait for CI → request deploy ------------------------

def ship(pr_num: int) -> str:
    """Approval action: squash-merge the PR, then deploy in the background."""
    pr = _api("GET", f"/pulls/{pr_num}")
    if pr.get("merged"):
        sha = pr["merge_commit_sha"]
    else:
        res = _api("PUT", f"/pulls/{pr_num}/merge",
                   {"merge_method": "squash", "commit_title": f"{pr['title']} (#{pr_num})"})
        sha = res["sha"]
    files = [f["filename"] for f in _api("GET", f"/pulls/{pr_num}/files?per_page=100")]
    plan = deploy_plan(files)
    threading.Thread(target=_deploy_after_ci, args=(pr_num, pr["title"], sha, plan),
                     daemon=True).start()
    return f"merged PR #{pr_num} ({sha[:7]}); deploying: {plan['describe']}"


def _notify(text: str, title: str, urgency: str = "normal") -> None:
    try:
        from notify import route_notification
        route_notification("Forge", text, title=title, urgency=urgency)
    except Exception:
        pass


def _deploy_after_ci(pr_num: int, title: str, sha: str, plan: dict) -> None:
    import redis
    r = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), decode_responses=True)

    if plan["services"]:
        # "Build images" only builds what changed; its success is the gate.
        deadline = time.time() + CI_TIMEOUT_S
        conclusion = None
        while time.time() < deadline:
            time.sleep(POLL_S)
            try:
                runs = _api("GET", f"/actions/workflows/build-images.yml/runs?head_sha={sha}")
            except RuntimeError:
                continue
            done = [x for x in runs.get("workflow_runs", []) if x.get("status") == "completed"]
            if done:
                conclusion = done[0].get("conclusion")
                break
        if conclusion != "success":
            _notify(f"PR #{pr_num} merged, but the image build "
                    f"{'timed out' if conclusion is None else conclusion}. Nothing deployed.",
                    title=f"🔴 Ship failed — {title}"[:80], urgency="urgent")
            return

    r.set(DEPLOY_REQUEST_KEY, json.dumps({
        "sha": sha, "pr": pr_num, "title": title,
        "services": plan["services"],
        "restart_agent_runner": plan["restart_agent_runner"],
        "native": plan["native"],
        "requested": time.time(),
    }), ex=24 * 3600)
