# Explainer: How This GitHub MCP Project Works

This document explains the concepts behind this project and walks through exactly
what happens when you ask a question. It assumes no prior MCP knowledge.

---

## 1. What problem is MCP solving?

An LLM like Claude is great at reasoning over text, but on its own it can't *do*
anything — it can't read your files, query a database, or hit an API. To make an
LLM useful, you give it **tools**: functions it can ask to run.

Historically, every app wired up its own bespoke tool integrations. If you built a
GitHub integration for one app, you couldn't reuse it in another — the glue code
was specific to that app.

**The Model Context Protocol (MCP)** standardizes this. It defines a common
protocol so that:

- **Servers** expose capabilities (tools, resources, prompts) in a standard shape.
- **Clients** (any MCP-aware app — Claude Desktop, an IDE, your own script) can
  connect to *any* MCP server and use its tools without custom glue.

Think of it like USB for LLM tools: build the server once, plug it into any host.

In this project:

- **`server.py`** is an MCP **server** that exposes 9 GitHub tools.
- **`client.py`** is an MCP **client** (and also an LLM host) that connects to the
  server and lets Claude use those tools, with a rich terminal UI.
- **`github_auth.py`** is an optional GitHub OAuth device-flow login so those tools
  can reach your private repos.

---

## 2. The three roles

MCP has three roles. It's worth keeping them straight because "client" and "host"
are easy to conflate.

| Role | In this project | Responsibility |
|------|-----------------|----------------|
| **Server** | `server.py` | Exposes tools; executes them when asked. Knows nothing about Claude. |
| **Client** | the MCP session inside `client.py` | Speaks the MCP protocol to the server; lists tools, calls them. |
| **Host** | the rest of `client.py` | Owns the LLM conversation; decides *when* to invoke the client's tools. |

The server is deliberately dumb about AI — it just wraps GitHub. That's the point:
the same `server.py` could be driven by Claude Desktop, an IDE, or a plain test
script, with no changes.

---

## 3. Transport: how client and server talk

MCP messages can travel over different **transports**. The two common ones:

- **stdio** (what we use): the client launches the server as a subprocess and they
  exchange JSON-RPC messages over the process's stdin/stdout. Simple, local, no
  network. Ideal for local tools.
- **HTTP/SSE**: the server runs as a network service. Use this when the server is
  remote or shared.

We use **stdio**, so `client.py` literally spawns `server.py` as a child process
(after an optional GitHub login — see §6):

```python
server_params = StdioServerParameters(
    command=sys.executable,   # the same Python interpreter (from our uv venv)
    args=["server.py"],
    env=server_env,           # includes GITHUB_TOKEN (from the login or .env)
)
```

Under the hood, the messages are **JSON-RPC 2.0** — the same request/response
format you'd see in `Processing request of type ListToolsRequest` in the logs.

---

## 4. The server, piece by piece (`server.py`)

### FastMCP

We use `FastMCP`, a high-level helper from the MCP SDK. It turns an ordinary Python
function into an MCP tool via a decorator:

```python
mcp = FastMCP("codecompass")

@mcp.tool()
def get_repo_info(owner: str, repo: str) -> str:
    """Get summary information about a specific repository.

    Args:
        owner: Repository owner (user or organization), e.g. "anthropics".
        repo: Repository name, e.g. "anthropic-sdk-python".
    """
    ...
```

Two things happen automatically here, and both matter:

1. **The type hints become a JSON Schema.** `owner: str, repo: str` tells the
   client (and ultimately Claude) that this tool takes two required string
   arguments. Claude uses this schema to produce valid tool calls.
2. **The docstring becomes the tool's description.** This is not just
   documentation — it's the *prompt* Claude reads to decide whether and how to use
   the tool. Vague docstrings lead to misused tools; that's why each one here is
   specific and gives example values.

### Talking to GitHub

Each tool calls a small helper, `_get()`, which uses `httpx` to hit
`api.github.com`. Key design choices:

- **Optional auth.** If `GITHUB_TOKEN` is set it's sent as a Bearer token (5000
  requests/hour); without it, GitHub allows 60/hour. The demo works either way.
- **Errors are returned, not raised (mostly).** Rate limits and 404s become
  readable messages. This matters because those messages flow back to Claude,
  which can then adjust — e.g. tell you to set a token instead of crashing.

### Running

```python
if __name__ == "__main__":
    mcp.run(transport="stdio")
```

That's the whole server: start listening on stdio for MCP requests, and answer
`list_tools` / `call_tool` calls.

---

## 5. The client + host, piece by piece (`client.py`)

This file does two jobs: it's the MCP **client** (talks to the server) and the LLM
**host** (talks to Claude and orchestrates the loop).

### Step 1 — Connect and discover tools

```python
async with stdio_client(server_params) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tool_list = (await session.list_tools()).tools
```

`initialize()` performs the MCP handshake. `list_tools()` asks the server what it
can do — this returns the names, descriptions, and input schemas that FastMCP
generated from our functions.

### Step 2 — Translate MCP tools → Anthropic tools

MCP and the Claude API describe tools slightly differently, so we map between them:

```python
{
    "name": t.name,
    "description": t.description or "",
    "input_schema": t.inputSchema,   # MCP's schema is already the shape Claude wants
}
```

This is the crucial bridge: the tools the server advertises become the tools Claude
is told about. We never hardcode the tool list in the client — if you add a tool to
`server.py`, it shows up automatically.

### Step 3 — The agentic loop

This is the heart of the project (`run_turn`). One user question can require several
tool calls, so we loop:

```
send conversation to Claude
   │
   ▼
Claude responds
   │
   ├─ stop_reason == "end_turn"  → we have the final answer, return it
   │
   └─ stop_reason == "tool_use"  → Claude wants to call one or more tools
          │
          ▼
      for each requested tool:
          session.call_tool(name, input)   ← runs on the MCP server
          collect the result
          │
          ▼
      append results to the conversation, loop again
```

In code, condensed (the client uses the **async** SDK, `AsyncAnthropic`, so the
request is awaitable — which is also what lets **Esc** cancel it mid-flight):

```python
while True:
    response = await anthropic.messages.create(..., tools=tools, messages=messages)
    usage.add(response.usage)                # track token consumption
    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason != "tool_use":
        return final_text(response)          # done

    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            result = await session.call_tool(block.name, block.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,      # must match the request
                "content": result_text,
            })
    messages.append({"role": "user", "content": tool_results})
```

A few details that are easy to get wrong:

- **You must append the assistant's full `content`** (including the `tool_use`
  blocks), not just its text. The API needs to see what it asked for.
- **Every `tool_use` needs a matching `tool_result`** with the same
  `tool_use_id`, sent back in a single `user` message.
- **The loop can run several times.** In our test, Claude called `list_issues`,
  then `get_repo_info`, then `list_issues` again before answering — three tool
  round-trips inside one "turn".

### Step 4 — Credentials

The client constructs `AsyncAnthropic()` with no arguments. The SDK resolves
credentials in order: `ANTHROPIC_API_KEY` → `ANTHROPIC_AUTH_TOKEN` → an
`ant auth login` profile. On startup we fail fast **only** when *none* of those
exist (we check for either env var **or** a `~/.config/anthropic` profile), so
valid profile-based credentials aren't wrongly rejected. A mid-session safety net
still catches `AuthenticationError` at call time and prints guidance.

---

## 6. GitHub authentication (`github_auth.py`)

The tools work anonymously against public data (GitHub allows 60 requests/hour).
To reach **private** repos — or use tools that require auth like
`search_commits_by_ticket` — the client needs a GitHub token. There are two paths,
resolved in `main()` before the server is spawned:

1. **`GITHUB_TOKEN` already set** (env or `.env`) → reuse it directly.
2. **Otherwise, run the OAuth device flow** (`github_auth.login()`):
   - The client asks GitHub for a device + user code (`POST /login/device/code`).
   - It prints a short code and `github.com/login/device`; you approve in a browser.
   - It polls `POST /login/oauth/access_token` until you approve, then returns a
     user access token carrying *your* permissions.

The token is placed into the environment passed to `server.py`, so the server's
`_get()` sends it as a Bearer token. Device flow needs a registered OAuth App
client id (`GITHUB_CLIENT_ID`) with device flow enabled; if that's missing or the
user skips the prompt, the client continues in public-only mode — login is never
fatal.

```python
if server_env.get("GITHUB_TOKEN"):
    ...                                   # reuse it
else:
    try:
        token = await asyncio.to_thread(github_auth.login)   # blocking poll off the loop
        server_env["GITHUB_TOKEN"] = token
    except github_auth.DeviceFlowError:
        ...                               # fall back to public data
```

---

## 7. The terminal UI

`client.py` is more than a print loop — it's a small interactive app:

- **Rendered markdown.** Claude's answers are markdown, so they're rendered with
  [`rich`](https://github.com/Textualize/rich): headings, lists, tables, links, and
  syntax-highlighted code blocks inside a panel — instead of raw `##`/`**` text.
  The startup tool inventory is a `rich` table.
- **Command history.** Importing `readline` upgrades `input()` with line editing
  and **↑/↓** recall; history is saved to `~/.codecompass_history` between runs.
- **Progress + token usage.** While a turn runs, a spinner shows elapsed time and a
  live token count; after each answer the panel footer shows `turn` and `session`
  input/output token totals (summed from each response's `usage`).
- **Esc to cancel.** Each turn runs as an `asyncio` task. A watcher puts the
  terminal in cbreak mode and, on **Esc**, cancels the task — which works because
  the request uses `AsyncAnthropic` and is genuinely awaitable. A cancelled turn is
  rolled back so a half-finished tool exchange can't corrupt the history.
- **Non-blocking input.** Input is read on a daemon thread so the event loop keeps
  servicing the MCP stdio transport while waiting for you to type.

---

## 8. Full request lifecycle (worked example)

You type: *"What are the top 2 open issues on modelcontextprotocol/python-sdk?"*

```
1. client.py: append your message, call Claude with the 9 tool schemas.
2. Claude: "I need to call list_issues(owner=..., repo=..., limit=2)."
   → stop_reason = "tool_use"
3. client.py: session.call_tool("list_issues", {...})
4. server.py: GET https://api.github.com/repos/.../issues?state=open&per_page=6
   → filters out PRs, truncates to 2, formats text, returns it over stdio.
5. client.py: append the tool_result, call Claude again.
6. Claude: (maybe calls get_repo_info too for context) ... then
   → stop_reason = "end_turn" with the written answer.
7. client.py: print the answer.
```

Everything after step 1 happens automatically — you don't tell Claude which tools
to use; it decides from the descriptions.

---

## 9. Why one detail was subtle: the `list_issues` PR bug

GitHub's "issues" REST endpoint returns **pull requests too** (GitHub models PRs as
a kind of issue). The first version fetched `per_page=limit` and then filtered PRs
out — so on an active repo where the most recent items are all PRs, the filtered
list came back empty even though the repo had 533 open issues.

The fix: **over-fetch, then filter, then truncate.**

```python
data = _get(..., {"state": state, "per_page": min(limit * 3, 100)})
issues = [i for i in data if "pull_request" not in i][:limit]
```

This is a good example of why you verify tools against real data — the schema and
the happy path looked fine; only a live call revealed the interleaving.

**A second instance of the same lesson: merged vs. closed PRs.** GitHub has no
"merged" PR *state* — a merged PR is a *closed* PR whose `merged_at` is set. An
early version of `list_pull_requests` paged the closed list and split it by
`merged_at`, but on a repo whose most recent closed PRs were all *unmerged*, the
merged ones fell outside the fetched page and "merged" came back empty. The fix was
to let GitHub filter server-side via the search API (`is:merged` /
`is:closed is:unmerged`) instead of paging-and-filtering. Same moral: test against
a real, busy repo.

---

## 10. How to extend it

**Add a tool:** write a new decorated function in `server.py`. It appears in the
client automatically — no client changes needed.

```python
@mcp.tool()
def get_user(username: str) -> str:
    """Get a GitHub user's profile summary."""
    u = _get(f"/users/{username}")
    return f"{u['login']} — {u.get('name')} — {u['public_repos']} public repos"
```

**Swap the LLM host:** point Claude Desktop or an IDE at `server.py` (see the
`mcpServers` config in `README.md`). The server doesn't change.

**Swap the transport:** run the server over HTTP/SSE instead of stdio if you want
it to live on a different machine.

---

## 11. Glossary

- **MCP** — Model Context Protocol; the standard for exposing tools/data to LLM apps.
- **Server** — exposes tools (`server.py`).
- **Client** — speaks MCP to a server (`ClientSession` in `client.py`).
- **Host** — owns the LLM conversation and decides when to call tools (`client.py`).
- **Transport** — how messages travel (stdio here; also HTTP/SSE).
- **Tool** — a function the LLM can invoke, described by a name, description, and
  input schema.
- **Tool call / `tool_use`** — the model's request to run a tool.
- **`tool_result`** — the output you send back for a given `tool_use`.
- **Agentic loop** — repeatedly calling the model, running the tools it asks for,
  and feeding results back until it produces a final answer.
- **stdio transport** — client launches server as a subprocess; they talk over
  stdin/stdout using JSON-RPC.
