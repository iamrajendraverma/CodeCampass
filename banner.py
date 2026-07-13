"""Shared terminal branding for CodeCompass — a compass banner + tool infographic.

Kept dependency-free and stream-agnostic so both the server (stderr) and the
client (stdout) can render it. Never print the banner to a stdio server's stdout:
that channel carries JSON-RPC and any stray bytes corrupt the protocol.
"""

from __future__ import annotations

# ANSI styles
CYAN = "\033[36m"
BLUE = "\033[34m"
BOLD = "\033[1m"
DIM = "\033[2m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RESET = "\033[0m"

_W = 54  # inner width of the framed banner

# Display metadata for each tool: (single-width glyph, one-line blurb).
TOOL_INFO: dict[str, tuple[str, str]] = {
    "search_repositories": ("◎", "Search public repositories by keyword"),
    "list_my_repositories":("★", "Your repos, incl. private (needs login)"),
    "get_repo_info":       ("◆", "Stars, forks, language, license, issues"),
    "list_issues":         ("◈", "Open/closed issues with authors & labels"),
    "get_readme":          ("▤", "Decoded README text (truncated)"),
    "list_commits":        ("◌", "Recent commits with messages & authors"),
    "list_collaborators":  ("◍", "Users with access to a repo & their roles"),
    "search_commits_by_ticket": ("⌗", "Find commits by ticket id (DEV-#####)"),
    "list_pull_requests":  ("⎇", "Open / merged / closed pull requests"),
    "get_jira_issue":      ("◇", "Jira status of a ticket / story / epic"),
    "list_issues_by_assignee": ("☰", "Jira issues assigned to a user"),
}


def _row(plain: str = "", color: str = "") -> str:
    """Frame one centered content line. `plain` must be free of ANSI codes so the
    visible width is correct; color is applied around the already-padded text."""
    padded = plain.center(_W)
    return f"{CYAN}│{RESET}{color}{padded}{RESET}{CYAN}│{RESET}"


def banner(subtitle: str = "") -> str:
    """Return the framed CodeCompass compass banner as a multi-line string."""
    top = f"{CYAN}╭{'─' * _W}╮{RESET}"
    bottom = f"{CYAN}╰{'─' * _W}╯{RESET}"
    lines = [
        top,
        _row(),
        _row("N", BLUE),
        _row("╲  │  ╱", BLUE),
        _row("W ──  ◆  ── E", BLUE),
        _row("╱  │  ╲", BLUE),
        _row("S", BLUE),
        _row(),
        _row("C O D E C O M P A S S", BOLD + YELLOW),
        _row("Git-Driven Onboarding & Knowledge Agent", DIM),
    ]
    if subtitle:
        lines.append(_row(subtitle, GREEN))
    lines += [_row(), bottom]
    return "\n".join(lines)


def tools_panel(names: list[str], descriptions: dict[str, str] | None = None) -> str:
    """Return an indented, colorized inventory of the given tool names.

    `descriptions` (name -> description) is used as a fallback blurb for any tool
    not in TOOL_INFO — handy when the client renders tools discovered at runtime.
    """
    descriptions = descriptions or {}
    out = [
        f"  {BOLD}◈ Tools{RESET}  {DIM}({len(names)} available){RESET}",
        f"  {DIM}{'─' * 48}{RESET}",
    ]
    for name in names:
        if name in TOOL_INFO:
            glyph, blurb = TOOL_INFO[name]
        else:
            glyph = "•"
            blurb = (descriptions.get(name, "") or "").splitlines()[0][:44]
        out.append(f"  {CYAN}{glyph}{RESET}  {BOLD}{name.ljust(22)}{RESET}{DIM}{blurb}{RESET}")
    return "\n".join(out)
