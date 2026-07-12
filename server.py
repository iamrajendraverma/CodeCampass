"""GitHub MCP server.

Wraps a handful of GitHub REST API endpoints as MCP tools, served over stdio.
Works unauthenticated (60 requests/hour) or with a GITHUB_TOKEN env var
(5000 requests/hour).

Run standalone:
    uv run server.py
Usually it is spawned by client.py over stdio rather than run directly.
"""

from __future__ import annotations

import base64
import os
import sys

import httpx
from mcp.server.fastmcp import FastMCP

import banner

GITHUB_API = "https://api.github.com"
USER_AGENT = "codecompass/1.0"

mcp = FastMCP("codecompass")


def _client() -> httpx.Client:
    """Build an httpx client with GitHub headers (token optional)."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=GITHUB_API, headers=headers, timeout=30.0)


def _get(path: str, params: dict | None = None) -> dict | list:
    """GET a GitHub endpoint, raising a readable error on failure."""
    with _client() as client:
        resp = client.get(path, params=params)
    if resp.status_code == 403 and "rate limit" in resp.text.lower():
        raise RuntimeError(
            "GitHub rate limit exceeded. Set a GITHUB_TOKEN env var for a higher limit."
        )
    if resp.status_code == 404:
        raise RuntimeError(f"Not found: {path}")
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def search_repositories(query: str, limit: int = 5) -> str:
    """Search public GitHub repositories.

    Args:
        query: Search terms, e.g. "language:python mcp server" or "web framework".
        limit: Maximum number of results to return (1-20).
    """
    limit = max(1, min(limit, 20))
    data = _get("/search/repositories", {"q": query, "per_page": limit, "sort": "stars"})
    items = data.get("items", []) if isinstance(data, dict) else []
    if not items:
        return f"No repositories found for query: {query!r}"
    lines = [f"Found {data.get('total_count', 0)} repositories (showing {len(items)}):", ""]
    for r in items:
        lines.append(
            f"- {r['full_name']}  ★{r['stargazers_count']}  "
            f"[{r.get('language') or 'n/a'}]\n"
            f"    {r.get('description') or '(no description)'}\n"
            f"    {r['html_url']}"
        )
    return "\n".join(lines)


@mcp.tool()
def get_repo_info(owner: str, repo: str) -> str:
    """Get summary information about a specific repository.

    Args:
        owner: Repository owner (user or organization), e.g. "anthropics".
        repo: Repository name, e.g. "anthropic-sdk-python".
    """
    r = _get(f"/repos/{owner}/{repo}")
    license_name = (r.get("license") or {}).get("name") or "none"
    return (
        f"{r['full_name']}\n"
        f"  Description: {r.get('description') or '(none)'}\n"
        f"  Stars: {r['stargazers_count']}   Forks: {r['forks_count']}   "
        f"Open issues: {r['open_issues_count']}\n"
        f"  Language: {r.get('language') or 'n/a'}   License: {license_name}\n"
        f"  Homepage: {r.get('homepage') or '(none)'}\n"
        f"  URL: {r['html_url']}"
    )


@mcp.tool()
def list_issues(owner: str, repo: str, state: str = "open", limit: int = 10) -> str:
    """List issues in a repository.

    Args:
        owner: Repository owner (user or organization).
        repo: Repository name.
        state: Issue state: "open", "closed", or "all".
        limit: Maximum number of issues to return (1-30).
    """
    if state not in ("open", "closed", "all"):
        return f"Invalid state {state!r}. Use 'open', 'closed', or 'all'."
    limit = max(1, min(limit, 30))
    # GitHub's issues endpoint interleaves pull requests with issues, so over-fetch
    # (up to the 100-item page cap) and filter PRs out before truncating to `limit`.
    data = _get(
        f"/repos/{owner}/{repo}/issues",
        {"state": state, "per_page": min(limit * 3, 100)},
    )
    issues = [i for i in data if "pull_request" not in i][:limit]
    if not issues:
        return f"No {state} issues found for {owner}/{repo}."
    lines = [f"{state.capitalize()} issues for {owner}/{repo} (showing {len(issues)}):", ""]
    for i in issues:
        labels = ", ".join(l["name"] for l in i.get("labels", [])) or "no labels"
        lines.append(
            f"- #{i['number']} {i['title']}\n"
            f"    by {i['user']['login']}  [{labels}]\n"
            f"    {i['html_url']}"
        )
    return "\n".join(lines)


@mcp.tool()
def get_readme(owner: str, repo: str, max_chars: int = 4000) -> str:
    """Get the decoded README text of a repository.

    Args:
        owner: Repository owner (user or organization).
        repo: Repository name.
        max_chars: Truncate the README to this many characters (keeps context small).
    """
    r = _get(f"/repos/{owner}/{repo}/readme")
    content = base64.b64decode(r.get("content", "")).decode("utf-8", errors="replace")
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n\n... [truncated, {len(content)} chars total]"
    return content or "(README is empty)"


@mcp.tool()
def list_commits(owner: str, repo: str, limit: int = 10) -> str:
    """List recent commits on a repository's default branch.

    Args:
        owner: Repository owner (user or organization).
        repo: Repository name.
        limit: Maximum number of commits to return (1-30).
    """
    limit = max(1, min(limit, 30))
    data = _get(f"/repos/{owner}/{repo}/commits", {"per_page": limit})
    if not data:
        return f"No commits found for {owner}/{repo}."
    lines = [f"Recent commits for {owner}/{repo} (showing {len(data)}):", ""]
    for c in data:
        sha = c["sha"][:7]
        message = c["commit"]["message"].splitlines()[0]
        author = c["commit"]["author"]["name"]
        date = c["commit"]["author"]["date"]
        lines.append(f"- {sha}  {message}\n    by {author} on {date}")
    return "\n".join(lines)


if __name__ == "__main__":
    # Show the welcome banner only when launched in a real terminal. When the
    # client spawns this server over stdio, stdout is a pipe (not a TTY), so we
    # stay quiet and let the client own the UI. The banner always goes to stderr
    # so it can never corrupt the JSON-RPC stream on stdout.
    if sys.stdout.isatty():
        print(banner.banner("Team · Rajendra · Vikash · Parth · Atish"), file=sys.stderr)
        print(file=sys.stderr)
        print(banner.tools_panel(list(banner.TOOL_INFO)), file=sys.stderr)
        print(
            f"\n  {banner.DIM}Waiting for an MCP client on stdin… "
            f"(this is normal — run client.py instead){banner.RESET}\n",
            file=sys.stderr,
        )
    try:
        mcp.run(transport="stdio")
    except KeyboardInterrupt:
        # Ctrl+C is a normal way to stop a stdio server — exit quietly instead
        # of dumping an asyncio traceback.
        print(f"\n{banner.DIM}CodeCompass server stopped.{banner.RESET}", file=sys.stderr)
