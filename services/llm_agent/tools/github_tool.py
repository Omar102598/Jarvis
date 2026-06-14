"""GitHub tool for JARVIS — read repos/PRs/issues and create issues.

Uses the GitHub REST API with a personal access token:
    GITHUB_TOKEN, optionally GITHUB_DEFAULT_REPO ("owner/name")
"""

import os

import aiohttp
from langchain_core.tools import tool

TOKEN = os.environ.get("GITHUB_TOKEN", "")
DEFAULT_REPO = os.environ.get("GITHUB_DEFAULT_REPO", "")
_API = "https://api.github.com"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "JARVIS",
    }


@tool
async def github_tool(action: str, repo: str = "", arg1: str = "", arg2: str = "") -> str:
    """Interact with GitHub.

    Args:
        action: One of 'list_prs', 'list_issues', 'repo_info', 'create_issue'.
        repo: 'owner/name'. Defaults to GITHUB_DEFAULT_REPO if omitted.
        arg1: For 'create_issue', the issue title.
        arg2: For 'create_issue', the issue body (optional).
    """
    if not TOKEN:
        return "GitHub is not configured. Set GITHUB_TOKEN."
    repo = repo.strip() or DEFAULT_REPO
    if not repo or "/" not in repo:
        return "Please give a repo as 'owner/name' (or set GITHUB_DEFAULT_REPO)."

    action = action.lower().strip()
    async with aiohttp.ClientSession(headers=_headers()) as session:
        try:
            if action == "list_prs":
                async with session.get(f"{_API}/repos/{repo}/pulls?state=open&per_page=10") as r:
                    prs = await r.json()
                if not prs:
                    return f"No open PRs in {repo}."
                return "Open PRs:\n" + "\n".join(
                    f"#{p['number']} {p['title']} ({p['user']['login']})" for p in prs
                )
            if action == "list_issues":
                async with session.get(
                    f"{_API}/repos/{repo}/issues?state=open&per_page=10"
                ) as r:
                    issues = [i for i in await r.json() if "pull_request" not in i]
                if not issues:
                    return f"No open issues in {repo}."
                return "Open issues:\n" + "\n".join(
                    f"#{i['number']} {i['title']}" for i in issues
                )
            if action == "repo_info":
                async with session.get(f"{_API}/repos/{repo}") as r:
                    if r.status != 200:
                        return f"Repo {repo} not found."
                    d = await r.json()
                return (
                    f"{d['full_name']} — {d.get('description', 'no description')}\n"
                    f"⭐ {d['stargazers_count']} | forks {d['forks_count']} | "
                    f"open issues {d['open_issues_count']} | {d.get('language', '?')}"
                )
            if action == "create_issue":
                if not arg1:
                    return "Please provide an issue title in arg1."
                async with session.post(
                    f"{_API}/repos/{repo}/issues",
                    json={"title": arg1, "body": arg2 or ""},
                ) as r:
                    if r.status == 201:
                        d = await r.json()
                        return f"Created issue #{d['number']}: {d['html_url']}"
                    return f"Failed to create issue: HTTP {r.status}"
            return f"Unknown action '{action}'. Use list_prs, list_issues, repo_info, create_issue."
        except Exception as exc:
            return f"GitHub request failed: {exc}"
