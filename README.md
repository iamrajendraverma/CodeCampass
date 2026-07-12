# 🧭 CodeCompass — GitHub MCP Server + LLM-Powered Client

**CodeCompass** is a [Model Context Protocol](https://modelcontextprotocol.io) demo in Python — navigate GitHub with Claude from your terminal:

- **`server.py`** — an MCP server (stdio transport) that wraps the GitHub REST API as tools.
- **`client.py`** — a rich terminal client that spawns the server, discovers its tools, and lets **Claude** call them autonomously to answer your questions.
- **`github_auth.py`** — an optional GitHub **OAuth device-flow login** so the tools can reach your **private** repos.
- **`banner.py`** — shared terminal branding.

> New to MCP or want to understand how it works end-to-end? Read **[EXPLAINER.md](EXPLAINER.md)** — it walks through the concepts, the code, and the full request lifecycle.

```
┌────────────┐   Claude API    ┌──────────────┐   MCP (stdio)   ┌────────────┐   HTTPS   ┌────────────┐
│  client.py │ ───tool calls──▶│  Anthropic   │                 │  server.py │ ────────▶ │ GitHub API │
│ (rich TUI) │◀──tool schemas──│  (Opus 4.8)  │                 │ (9 tools)  │◀───────── │            │
└────────────┘                 └──────────────┘                 └────────────┘           └────────────┘
       │                                                             ▲
       └──────────────────── agentic loop: calls tools ─────────────┘
```

## Tools exposed by the server

| Tool | Description |
|------|-------------|
| `search_repositories(query, limit)` | Search public repos, sorted by stars |
| `list_my_repositories(visibility, sort, limit)` | Your repos **including private** — needs login |
| `get_repo_info(owner, repo)` | Stars, forks, language, license, open issues |
| `list_issues(owner, repo, state, limit)` | Issues with authors and labels |
| `get_readme(owner, repo, max_chars)` | Decoded README text (truncated) |
| `list_commits(owner, repo, limit)` | Recent commits with messages and authors |
| `list_collaborators(owner, repo, limit)` | Users with access to a repo and their roles |
| `search_commits_by_ticket(owner, repo, ticket_id, limit)` | Find commits referencing a ticket like `DEV-16397` — needs login |
| `list_pull_requests(owner, repo, state, limit)` | Pull requests by state: `open` / `merged` / `closed` / `all` |

## Client experience

`client.py` is a polished terminal UI built on [`rich`](https://github.com/Textualize/rich):

- **Formatted answers** — Claude's markdown replies render as real headings, lists, tables, links, and **syntax-highlighted code** inside a clean panel (no more raw `##`/`**`).
- **Command history** — press **↑ / ↓** to recall previous queries; history persists across runs in `~/.codecompass_history`.
- **Live progress** — a spinner shows elapsed time and a running token count while a request is in flight.
- **Esc to cancel** — press **Esc** to abort an in-flight request (Unix terminals).
- **Token usage** — each answer shows `turn` and cumulative `session` input/output token counts.

## Requirements

- Python **3.11+** (managed here via [`uv`](https://docs.astral.sh/uv/))
- An Anthropic API key
- (Optional) GitHub auth — works anonymously (60 req/hr, public data only), or authenticate for private repos and a 5000 req/hr limit

## Setup

```bash
# sync the venv (installs anthropic, mcp, httpx, rich, python-dotenv):
uv sync

# configure secrets
cp .env.example .env
# then edit .env — set ANTHROPIC_API_KEY, and optionally GitHub auth (below)
```

## GitHub authentication (optional but recommended)

Anonymous access works for public data. To reach **private** repos (and use
`search_commits_by_ticket`, or list private repos/PRs/collaborators), authenticate
one of two ways in `.env`:

1. **Interactive browser login (device flow)** — set `GITHUB_CLIENT_ID` to an
   OAuth App's client id (create one at
   [github.com/settings/developers](https://github.com/settings/developers) → *New
   OAuth App*, and check **Enable Device Flow**). On startup the client prints a
   code + URL; you approve in your browser and the resulting token can see your
   private repos.
2. **Direct token** — set `GITHUB_TOKEN` to a Personal Access Token with `repo`
   scope. This skips the login prompt entirely.

If neither is configured, login is skipped and the client runs in public-only mode.

## Run

Interactive Claude-driven chat (this spawns the server for you):

```bash
uv run client.py
```

Example prompts:

- `What are the most-starred Python MCP repositories?`
- `Summarize the README for anthropics/anthropic-sdk-python.`
- `What are the open issues on modelcontextprotocol/python-sdk?`
- `List my private repositories.`
- `Who are the collaborators on <owner>/<repo>?`
- `Show merged pull requests in pallets/flask.`
- `Find commits for DEV-16397 in <owner>/<repo>.`

Type `quit` or press **Ctrl-C** to exit; press **Esc** to cancel a running request.

## Running the server on its own

`server.py` speaks MCP over stdio, so it's normally launched by a client. You can
also register it with any MCP-compatible host (e.g. Claude Desktop) using:

```json
{
  "mcpServers": {
    "github": {
      "command": "uv",
      "args": ["--directory", "/Users/rajendra/projects/hackthon/mcp", "run", "server.py"],
      "env": { "GITHUB_TOKEN": "ghp_..." }
    }
  }
}
```

## How the client's agentic loop works

1. (Optional) Log in to GitHub, then launch `server.py` over stdio and `list_tools()`.
2. Convert MCP tool schemas → Anthropic `tools` format.
3. Send the conversation to Claude. If `stop_reason == "tool_use"`, run each
   requested tool via `session.call_tool(...)`, append the results, and loop.
4. When Claude stops requesting tools, render the final answer as markdown.

Adding a tool to `server.py` makes it available to the client automatically — the
client never hardcodes the tool list.
