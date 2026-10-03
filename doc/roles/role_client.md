
## 1. At startup, it:

-  Loads Anthropic credentials.
-  Logs into GitHub if needed.
-  Starts server.py as an MCP server.
-  Connects to that server through stdio.
-  Discovers the MCP tools exposed by the server.
- Converts those MCP tools into Anthropic tool definitions.
- Sends the user's question to Claude.
- If Claude asks to use a tool, the client executes that MCP tool.
- Sends the tool result back to Claude.
-  Claude produces the final answer.


```
User
  ↓
client.py
  ↓
Claude API
  ↓
"Use GitHub tool"
  ↓
client.py
  ↓
MCP server.py
  ↓
GitHub
  ↓
tool result
  ↓
Claude
  ↓
Final answer
```
## 2. Imports - What libraries are being used? 

```python 
from anthropic import AsyncAnthropic
``` 
it is used to communicate with claude asynchronously 

MCP 

```python 
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
```
These are the MCP pieces.

- **ClientSession** → communicates with the MCP server.
- **StdioServerParameters** → describes how to start the MCP server.
- **stdio_client**  → creates communication through standard input/output.


```python 
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
...
```
These are basically UI/terminal formatting.

## 3. Calude Configuration 

```python 

MODEL = "claude-opus-4-8"
MAX_TOKENS = 4096
```
## 4. System prompt 

```python
SYSTEM_PROMPT = (
    "You are a helpful assistant with access to GitHub tools. "
    "Use the tools to answer questions about repositories, issues, commits, and "
    "READMEs. Prefer calling a tool over guessing. Be concise."
)
```
this tells claude: 
you have github tools. Use them instead of guessing. 

That's important because the model should retrieve repository information through MCP rather than inventing it.


## 4. Authentication

The program expects:

```
ANTHROPIC_API_KEY
```

and optionally:

```
GITHUB_TOKEN
```

The code checks whether Anthropic credentials exist:

```python 
have_env = os.environ.get("ANTHROPIC_API_KEY") or \
           os.environ.get("ANTHROPIC_AUTH_TOKEN")
```

if neither exists 

```python 
sys.exit(AUTH_HELP)
```

## 5. The most important MCP part

This is where things become interesting.

The application starts:

```python 
server_params = StdioServerParameters(
    command=sys.executable,
    args=["server.py"],
    env=server_env,
)
```
Meaning server.py using same Python interpreter 
then 

```python 
async with stdio_client(server_params) as (read, write):
```
Intializes the MCP connection 

And 

```python 
tool_list = (await session.list_tools()).tools
```
The client ask : 
MCP server , what tools do you provide ? 

The server might respond with something like:

```python 
search_repositories
get_issue
get_commit
get_readme
```
The client then converts them: 

```python 
tools = mcp_tools_to_anthropic(tool_list)
```
## 6 Compelte architecture 

``` 
                 ┌───────────────────┐
                 │       USER        │
                 └─────────┬─────────┘
                           │
                           ▼
                 ┌───────────────────┐
                 │     client.py     │
                 │                   │
                 │  Anthropic SDK    │
                 │  MCP Client       │
                 └─────────┬─────────┘
                           │
                           ▼
                 ┌───────────────────┐
                 │      CLAUDE       │
                 │                   │
                 │ decides whether   │
                 │ a tool is needed  │
                 └─────────┬─────────┘
                           │
                    tool_use request
                           │
                           ▼
                 ┌───────────────────┐
                 │    MCP Client     │
                 │                   │
                 │ session.call_tool │
                 └─────────┬─────────┘
                           │
                         stdio
                           │
                           ▼
                 ┌───────────────────┐
                 │     server.py     │
                 │    MCP Server     │
                 └─────────┬─────────┘
                           │
                           ▼
                 ┌───────────────────┐
                 │    GitHub API     │
                 └─────────┬─────────┘
                           │
                       tool result
                           │
                           ▼
                 ┌───────────────────┐
                 │      CLAUDE       │
                 │                   │
                 │ final reasoning   │
                 └─────────┬─────────┘
                           │
                           ▼
                 ┌───────────────────┐
                 │       USER        │
                 └───────────────────┘
```

## Exam Trap 

Exam trap: Claude does not directly execute the GitHub tool in this architecture. Claude requests a tool call; the MCP client executes it through the MCP server. This file demonstrates exactly that separation.
