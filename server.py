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
import re
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


# A ticket id such as "DEV-16397" — the "DEV-" prefix is optional on input so a
# bare number ("16397") is accepted too. Digit count is left flexible.
_TICKET_RE = re.compile(r"^(?:DEV-)?(\d{3,})$", re.IGNORECASE)


def _normalize_ticket(ticket: str) -> str | None:
    """Return a canonical 'DEV-#####' id, or None if the input isn't a ticket."""
    m = _TICKET_RE.match(ticket.strip())
    return f"DEV-{m.group(1)}" if m else None


def _highest_permission(perms: dict) -> str:
    """Map GitHub's boolean permission flags to a single human-readable role."""
    for flag, label in (
        ("admin", "admin"),
        ("maintain", "maintain"),
        ("push", "write"),
        ("triage", "triage"),
        ("pull", "read"),
    ):
        if perms.get(flag):
            return label
    return "read"


def _pr_label(pr: dict) -> str:
    """Classify a PR as 'merged', 'closed' (without merging), or 'open'."""
    if pr.get("merged_at"):
        return "merged"
    return "closed" if pr.get("state") == "closed" else "open"


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
def list_my_repositories(visibility: str = "all", sort: str = "updated", limit: int = 10) -> str:
    """List repositories the logged-in GitHub user can access, including PRIVATE ones.

    Use this for questions about "my repos" or private repositories. Requires the
    user to be logged in (a GITHUB_TOKEN with 'repo' scope). GitHub's public search
    does not reliably surface private repos, so this endpoint is the way to reach them.

    Args:
        visibility: Which repos to include: "all", "public", or "private".
        sort: Sort order: "created", "updated", "pushed", or "full_name".
        limit: Maximum number of repositories to return (1-30).
    """
    if not os.environ.get("GITHUB_TOKEN"):
        return (
            "Not logged in to GitHub, so private repositories aren't available. "
            "Restart the client and complete the GitHub login prompt, then try again."
        )
    if visibility not in ("all", "public", "private"):
        return f"Invalid visibility {visibility!r}. Use 'all', 'public', or 'private'."
    if sort not in ("created", "updated", "pushed", "full_name"):
        return f"Invalid sort {sort!r}. Use 'created', 'updated', 'pushed', or 'full_name'."
    limit = max(1, min(limit, 30))
    data = _get(
        "/user/repos",
        {"visibility": visibility, "sort": sort, "per_page": limit},
    )
    repos = data if isinstance(data, list) else []
    if not repos:
        return f"No repositories found for the logged-in user (visibility={visibility})."
    lines = [f"Your repositories (visibility={visibility}, showing {len(repos)}):", ""]
    for r in repos:
        vis = "private" if r.get("private") else "public"
        lines.append(
            f"- {r['full_name']}  ★{r['stargazers_count']}  "
            f"[{r.get('language') or 'n/a'}]  ({vis})\n"
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


@mcp.tool()
def list_collaborators(owner: str, repo: str, limit: int = 30) -> str:
    """List the users (collaborators) associated with a repository.

    Use this for "who has access to", "who works on", or "who are the members of"
    a repo. Shows each user's login and permission level (admin, maintain, write,
    triage, or read). Listing collaborators requires access to the repo; private
    repos need the user to be logged in with a GITHUB_TOKEN that has 'repo' scope.

    Args:
        owner: Repository owner (user or organization).
        repo: Repository name.
        limit: Maximum number of collaborators to return (1-100).
    """
    limit = max(1, min(limit, 100))
    try:
        data = _get(f"/repos/{owner}/{repo}/collaborators", {"per_page": limit})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            return (
                f"Not allowed to list collaborators for {owner}/{repo}. Listing "
                "collaborators requires access to the repository — log in with a "
                "GITHUB_TOKEN that can see it (private repos need 'repo' scope)."
            )
        raise
    users = data if isinstance(data, list) else []
    if not users:
        return f"No collaborators found for {owner}/{repo}."
    lines = [f"Collaborators on {owner}/{repo} (showing {len(users)}):", ""]
    for u in users:
        role = u.get("role_name") or _highest_permission(u.get("permissions") or {})
        lines.append(f"- {u['login']}  [{role}]\n    {u.get('html_url', '')}")
    return "\n".join(lines)


@mcp.tool()
def search_commits_by_ticket(owner: str, repo: str, ticket_id: str, limit: int = 10) -> str:
    """Find commits in a repository whose message references a ticket id.

    Ticket ids look like "DEV-16397". Use this to trace which commits implemented
    or mentioned a given ticket. Accepts the full id ("DEV-16397") or just the
    number ("16397"). Searches the commit messages via GitHub's commit search.

    Args:
        owner: Repository owner (user or organization).
        repo: Repository name.
        ticket_id: Ticket id such as "DEV-16397" (or just "16397").
        limit: Maximum number of commits to return (1-30).
    """
    ticket = _normalize_ticket(ticket_id)
    if not ticket:
        return f"Invalid ticket id {ticket_id!r}. Expected something like 'DEV-16397'."
    # GitHub's commit search API rejects unauthenticated requests (422), so require
    # a login up front rather than surfacing a confusing validation error.
    if not os.environ.get("GITHUB_TOKEN"):
        return (
            "Searching commits requires being logged in to GitHub. Restart the "
            "client and complete the login (or set a GITHUB_TOKEN), then try again."
        )
    limit = max(1, min(limit, 30))
    try:
        data = _get(
            "/search/commits",
            {"q": f'"{ticket}" repo:{owner}/{repo}', "per_page": limit},
        )
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            return (
                f"Not allowed to search commits in {owner}/{repo}. Private repos "
                "require logging in with a GITHUB_TOKEN that can see the repo."
            )
        if code == 422:
            return (
                f"Couldn't search commits in {owner}/{repo}. Check the repo exists "
                "and that your GitHub login can see it (private repos need 'repo' scope)."
            )
        raise
    items = data.get("items", []) if isinstance(data, dict) else []
    if not items:
        return f"No commits referencing {ticket} found in {owner}/{repo}."
    lines = [f"Commits referencing {ticket} in {owner}/{repo} (showing {len(items)}):", ""]
    for c in items:
        sha = c["sha"][:7]
        commit = c.get("commit", {})
        message = (commit.get("message") or "").splitlines()[0]
        author = (commit.get("author") or {}).get("name", "unknown")
        date = (commit.get("author") or {}).get("date", "")
        lines.append(
            f"- {sha}  {message}\n    by {author} on {date}\n    {c.get('html_url', '')}"
        )
    return "\n".join(lines)


@mcp.tool()
def list_pull_requests(owner: str, repo: str, state: str = "open", limit: int = 10) -> str:
    """List pull requests in a repository by state.

    Use this for questions about PRs. States:
      - "open":   PRs still awaiting review/merge.
      - "merged": PRs that were closed AND merged.
      - "closed": PRs that were closed WITHOUT merging (rejected/abandoned).
      - "all":    every PR regardless of state.
    (In GitHub a merged PR is a closed PR whose merge went through, so "closed"
    here excludes merged ones.)

    Args:
        owner: Repository owner (user or organization).
        repo: Repository name.
        state: One of "open", "merged", "closed", or "all".
        limit: Maximum number of PRs to return (1-30).
    """
    # Map each state to GitHub search qualifiers so the API does the filtering —
    # in particular "is:merged" vs "is:closed is:unmerged", which can't be told
    # apart reliably by paging the /pulls endpoint.
    qualifiers = {
        "open": ["is:open"],
        "merged": ["is:merged"],
        "closed": ["is:closed", "is:unmerged"],
        "all": [],
    }
    if state not in qualifiers:
        return f"Invalid state {state!r}. Use one of: {', '.join(qualifiers)}."
    limit = max(1, min(limit, 30))
    q = " ".join([f"repo:{owner}/{repo}", "is:pr", *qualifiers[state]])
    try:
        data = _get(
            "/search/issues",
            {"q": q, "per_page": limit, "sort": "updated", "order": "desc"},
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403, 422):
            return (
                f"Couldn't search pull requests in {owner}/{repo}. Check the repo "
                "exists and that your GitHub login can see it (private repos need "
                "a GITHUB_TOKEN with 'repo' scope)."
            )
        raise
    items = data.get("items", []) if isinstance(data, dict) else []
    if not items:
        return f"No {state} pull requests found for {owner}/{repo}."
    total = data.get("total_count", len(items))
    lines = [f"{state.capitalize()} PRs for {owner}/{repo} ({total} total, showing {len(items)}):", ""]
    for p in items:
        pr = p.get("pull_request") or {}
        label = _pr_label({"merged_at": pr.get("merged_at"), "state": p.get("state")})
        author = (p.get("user") or {}).get("login", "unknown")
        if label == "merged":
            when = f"merged {pr.get('merged_at', '')}"
        elif label == "closed":
            when = f"closed {p.get('closed_at', '')}"
        else:
            when = f"opened {p.get('created_at', '')}"
        lines.append(
            f"- #{p['number']} {p['title']}  [{label}]\n"
            f"    by {author} · {when}\n"
            f"    {p['html_url']}"
        )
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
