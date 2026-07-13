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

# Jira Cloud REST API v3. Configured via env vars (see .env.example):
#   JIRA_BASE_URL   e.g. https://bechprep.atlassian.net
#   JIRA_EMAIL      the Atlassian account that owns the API token
#   JIRA_API_TOKEN  https://id.atlassian.com/manage-profile/security/api-tokens
# Cloud uses HTTP Basic auth with email:token (not a password).
JIRA_API = "/rest/api/3"

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


def _jira_config() -> tuple[str, str, str]:
    """Return (base_url, email, token) or raise a readable error if unset."""
    base = (os.environ.get("JIRA_BASE_URL") or "").rstrip("/")
    email = os.environ.get("JIRA_EMAIL")
    token = os.environ.get("JIRA_API_TOKEN")
    if not (base and email and token):
        raise RuntimeError(
            "Jira is not configured. Set JIRA_BASE_URL, JIRA_EMAIL, and "
            "JIRA_API_TOKEN in your .env (see .env.example)."
        )
    return base, email, token


def _jira_client() -> httpx.Client:
    """Build an httpx client for Jira Cloud (Basic auth with email:token)."""
    base, email, token = _jira_config()
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    # httpx encodes the (email, token) tuple as an HTTP Basic auth header.
    return httpx.Client(base_url=base, headers=headers, auth=(email, token), timeout=30.0)


def _jira_get(path: str, params: dict | None = None) -> dict | list:
    """GET a Jira endpoint, mapping common failures to readable errors."""
    with _jira_client() as client:
        resp = client.get(path, params=params)
    if resp.status_code == 401:
        raise RuntimeError(
            "Jira authentication failed (401). Check JIRA_EMAIL and JIRA_API_TOKEN "
            "— Cloud needs the account email plus an API token (not your password)."
        )
    if resp.status_code == 403:
        raise RuntimeError(
            "Jira access forbidden (403). Your account may lack permission to view "
            "this project or issue."
        )
    if resp.status_code == 404:
        raise RuntimeError(f"Not found in Jira: {path}")
    resp.raise_for_status()
    return resp.json()


def _jira_browse_url(key: str) -> str:
    """Human-facing Jira issue URL, e.g. https://acme.atlassian.net/browse/DEV-1."""
    base = (os.environ.get("JIRA_BASE_URL") or "").rstrip("/")
    return f"{base}/browse/{key}" if base else key


def _resolve_assignee(assignee: str) -> tuple[str | None, str]:
    """Resolve an email or display name to a Jira accountId.

    Jira Cloud identifies users by opaque accountId (emails/usernames were
    removed from most APIs for privacy). Returns (accountId, display_label);
    accountId is None when no user matches the query.
    """
    data = _jira_get("/rest/api/3/user/search", {"query": assignee})
    users = data if isinstance(data, list) else []
    if users:
        u = users[0]
        return u.get("accountId"), u.get("displayName") or assignee
    return None, assignee


def _jql_in_clause(field: str, value: str) -> str | None:
    """Build a quoted `field in (...)` JQL clause from a comma-separated value.

    Returns None for an empty value or "all" (meaning: no filter on this field).
    """
    if not value or value.strip().lower() == "all":
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    if not items:
        return None
    quoted = ", ".join(f'"{v}"' for v in items)
    return f"{field} in ({quoted})"


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


def _fmt_jira_issue(issue: dict, *, detailed: bool = False) -> str:
    """Format one Jira issue's fields into a readable block."""
    key = issue.get("key", "?")
    f = issue.get("fields") or {}
    summary = f.get("summary") or "(no summary)"
    itype = (f.get("issuetype") or {}).get("name") or "Issue"
    status = (f.get("status") or {}).get("name") or "Unknown"
    category = ((f.get("status") or {}).get("statusCategory") or {}).get("name")
    status_str = f"{status} ({category})" if category else status
    assignee = (f.get("assignee") or {}).get("displayName") or "Unassigned"
    if not detailed:
        return (
            f"- {key}  [{itype}]  {summary}\n"
            f"    status: {status_str}  ·  assignee: {assignee}\n"
            f"    {_jira_browse_url(key)}"
        )
    priority = (f.get("priority") or {}).get("name") or "n/a"
    reporter = (f.get("reporter") or {}).get("displayName") or "n/a"
    resolution = (f.get("resolution") or {}).get("name") or "Unresolved"
    parent = f.get("parent") or {}
    parent_str = (
        f"{parent.get('key')} ({(parent.get('fields') or {}).get('summary', '')})"
        if parent else "none"
    )
    return (
        f"{key}  [{itype}]  {summary}\n"
        f"  Status:     {status_str}\n"
        f"  Assignee:   {assignee}\n"
        f"  Reporter:   {reporter}\n"
        f"  Priority:   {priority}\n"
        f"  Resolution: {resolution}\n"
        f"  Parent:     {parent_str}\n"
        f"  Created:    {f.get('created', 'n/a')}\n"
        f"  Updated:    {f.get('updated', 'n/a')}\n"
        f"  URL:        {_jira_browse_url(key)}"
    )


@mcp.tool()
def get_jira_issue(issue_key: str) -> str:
    """Get the current status and details of a single Jira ticket, story, or epic.

    Use this for questions like "what's the status of DEV-16397" or "who is
    PROJ-42 assigned to". Works for any issue type (Task, Story, Bug, Epic, …).

    Args:
        issue_key: The Jira issue key, e.g. "DEV-16397" or "PROJ-42".
    """
    key = issue_key.strip().upper()
    if not key:
        return "Please provide a Jira issue key, e.g. 'DEV-16397'."
    try:
        data = _jira_get(
            f"/rest/api/3/issue/{key}",
            {
                "fields": (
                    "summary,status,issuetype,assignee,reporter,priority,"
                    "resolution,parent,created,updated"
                )
            },
        )
    except RuntimeError as exc:
        return str(exc)
    return _fmt_jira_issue(data, detailed=True)


@mcp.tool()
def list_issues_by_assignee(
    assignee: str,
    status: str = "all",
    issue_type: str = "all",
    limit: int = 15,
) -> str:
    """List the Jira tickets, stories, or epics assigned to a particular user.

    Use this for "what is <person> working on", "show <person>'s open stories",
    or "which tasks are assigned to <email>". Results are newest-updated first.

    Args:
        assignee: The user's email or display name, e.g. "jane@bechprep.com"
            or "Jane Doe". Resolved to their Jira account automatically.
        status: Filter by status, comma-separated (e.g. "In Progress" or
            "To Do,In Progress"). Use "all" for every status.
        issue_type: Filter by type, comma-separated (e.g. "Story" or
            "Story,Epic,Task"). Use "all" for every type.
        limit: Maximum number of issues to return (1-50).
    """
    limit = max(1, min(limit, 50))
    try:
        account_id, label = _resolve_assignee(assignee)
    except RuntimeError as exc:
        return str(exc)
    if not account_id:
        return (
            f"No Jira user found matching {assignee!r}. Try their exact email "
            "address or full display name."
        )

    clauses = [f'assignee = "{account_id}"']
    status_clause = _jql_in_clause("status", status)
    if status_clause:
        clauses.append(status_clause)
    type_clause = _jql_in_clause("issuetype", issue_type)
    if type_clause:
        clauses.append(type_clause)
    jql = " AND ".join(clauses) + " ORDER BY updated DESC"

    try:
        data = _jira_get(
            "/rest/api/3/search/jql",
            {
                "jql": jql,
                "maxResults": limit,
                "fields": "summary,status,issuetype,assignee,priority,updated",
            },
        )
    except RuntimeError as exc:
        return str(exc)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400:
            return (
                f"Jira rejected the query (400). Check the status/type filters — "
                f"JQL was: {jql}"
            )
        raise

    issues = data.get("issues", []) if isinstance(data, dict) else []
    if not issues:
        filt = []
        if status.lower() != "all":
            filt.append(f"status={status}")
        if issue_type.lower() != "all":
            filt.append(f"type={issue_type}")
        suffix = f" ({', '.join(filt)})" if filt else ""
        return f"No issues assigned to {label}{suffix}."

    header = f"Issues assigned to {label} (showing {len(issues)}):"
    lines = [header, ""]
    lines.extend(_fmt_jira_issue(i) for i in issues)
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
