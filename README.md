# 🧭 CodeCompass — GitHub MCP Server + LLM-Powered Client

**CodeCompass** is a minimal [Model Context Protocol](https://modelcontextprotocol.io) demo in Python — navigate GitHub with Claude:

- **`server.py`** — an MCP server (stdio transport) that wraps the GitHub REST API as tools.
- **`client.py`** — A client that Automate the Agent System, discovers  tools, and call **Claude**   autonomously to answer your questions.

> New to MCP or want to understand how it works end-to-end? Read **[EXPLAINER.md](EXPLAINER.md)** — it walks through the concepts, the code, and the full request lifecycle.

```
┌────────────┐   Claude API    ┌──────────────┐   MCP (stdio)   ┌────────────┐   HTTPS   ┌────────────┐
│  client.py │ ───tool calls──▶│  Anthropic   │                 │  server.py │ ────────▶ │ GitHub API │
│  (chat UI) │◀──tool schemas──│  (Opus 4.8)  │                 │ (5 tools)  │◀───────── │            │
└────────────┘                 └──────────────┘                 └────────────┘           └────────────┘
       │                                                             ▲
       └──────────────────── Agent + calls tools ────────────────────┘
```

## Tools exposed by the server

| Tool | Description |
|------|-------------|
| `search_repositories(query, limit)` | Search public repos, sorted by stars |
| `get_repo_info(owner, repo)` | Stars, forks, language, license, open issues |
| `list_issues(owner, repo, state, limit)` | Issues with authors and labels |
| `get_readme(owner, repo, max_chars)` | Decoded README text (truncated) |
| `list_commits(owner, repo, limit)` | Recent commits with messages and authors |

## Requirements

- Python **3.10+** (managed here via [`uv`](https://docs.astral.sh/uv/); this project pins 3.11)
- An Anthropic API key
- (Optional) a GitHub token — works without one, but the anonymous rate limit is 60 req/hr

## Setup

```bash
# dependencies are already declared in pyproject.toml; sync the venv:
uv sync

# configure secrets
cp .env.example .env
# then edit .env and set ANTHROPIC_API_KEY (and optionally GITHUB_TOKEN)
```

## Run

Interactive Claude-driven chat (this spawns the server for you):

```bash
uv run client.py
```

Example prompts:

- `What are the most-starred Python MCP repositories?`
- `Summarize the README for anthropics/anthropic-sdk-python.`
- `What are the open issues on modelcontextprotocol/python-sdk?`
- `Show me the last 5 commits on huggingface/transformers.`

Type `quit` or press Ctrl-C to exit.

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

1. Launch `server.py` over stdio and `list_tools()`.
2. Convert MCP tool schemas → Anthropic `tools` format.
3. Send the conversation to Claude. If `stop_reason == "tool_use"`, run each
   requested tool via `session.call_tool(...)`, append the results, and loop.
4. When Claude stops requesting tools, print the final answer.
