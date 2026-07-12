"""LLM-powered MCP client.

Spawns server.py over stdio, discovers its tools, and runs an interactive chat
where Claude autonomously calls the GitHub tools to answer your questions.

Requires ANTHROPIC_API_KEY in the environment (or a .env file).
GITHUB_TOKEN is optional but recommended (passed through to the server).

Run:
    uv run client.py
"""

from __future__ import annotations

import asyncio
import atexit
import os
import sys
import threading

try:
    # Importing readline transparently upgrades input() with line editing and
    # ↑/↓ history recall. Absent on some bare Windows installs — degrade quietly.
    import readline
except ImportError:  # pragma: no cover
    readline = None

from anthropic import Anthropic
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import banner
import github_auth

MODEL = "claude-opus-4-8"
MAX_TOKENS = 4096
SYSTEM_PROMPT = (
    "You are a helpful assistant with access to GitHub tools. "
    "Use the tools to answer questions about repositories, issues, commits, and "
    "READMEs. Prefer calling a tool over guessing. Be concise."
)

AUTH_HELP = (
    "\n\033[31mNo Anthropic credentials found.\033[0m The client can't authenticate to the Claude API.\n"
    "Set your API key with either option, then re-run  \033[1muv run client.py\033[0m:\n\n"
    "  1) Create a .env file in this folder (recommended):\n"
    "       cp .env.example .env\n"
    "       # then edit .env and set:  ANTHROPIC_API_KEY=sk-ant-...\n\n"
    "  2) Or export it in your shell (use 'export' so child processes inherit it):\n"
    "       export ANTHROPIC_API_KEY=sk-ant-...\n"
)


HISTORY_FILE = os.path.expanduser("~/.codecompass_history")

# readline needs non-printing bytes wrapped in \001..\002 so it computes the
# prompt width correctly; otherwise recalling long history lines corrupts the
# display. Only emit the markers when readline is actually active.
if readline is not None:
    PROMPT = "\001\033[1m\002you ›\001\033[0m\002 "
else:
    PROMPT = "\033[1myou ›\033[0m "


def _setup_history() -> None:
    """Load past REPL queries and persist new ones so ↑/↓ recall them.

    input() already records each line into readline's in-memory history (so ↑/↓
    work within a session as soon as readline is imported); this just seeds that
    history from a file on startup and writes it back on exit.
    """
    if readline is None:
        return
    try:
        readline.read_history_file(HISTORY_FILE)
    except (FileNotFoundError, OSError):
        pass  # no prior history, or unreadable — start fresh
    readline.set_history_length(1000)
    atexit.register(_save_history)


def _save_history() -> None:
    if readline is None:
        return
    try:
        readline.write_history_file(HISTORY_FILE)
    except OSError:
        pass


def _looks_like_auth_error(exc: Exception) -> bool:
    """True if an exception is about missing/invalid Claude credentials."""
    from anthropic import AuthenticationError

    if isinstance(exc, AuthenticationError):
        return True
    text = str(exc).lower()
    return any(
        s in text
        for s in ("resolve authentication", "api_key", "auth_token", "credentials to be set")
    )


def mcp_tools_to_anthropic(mcp_tools) -> list[dict]:
    """Convert MCP tool definitions into the Anthropic tools format."""
    return [
        {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.inputSchema,
        }
        for t in mcp_tools
    ]


async def run_turn(
    anthropic: Anthropic,
    session: ClientSession,
    tools: list[dict],
    messages: list[dict],
) -> str:
    """Run one user turn to completion, executing any tool calls Claude requests."""
    while True:
        response = anthropic.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            tools=tools,
            messages=messages,
        )

        # Record the assistant turn (may contain text + tool_use blocks).
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            # Done: return the concatenated text of the final answer.
            return "".join(
                block.text for block in response.content if block.type == "text"
            ).strip()

        # Execute every tool_use block and collect results for the next request.
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"  \033[2m↳ calling {block.name}({_fmt_args(block.input)})\033[0m")
            try:
                result = await session.call_tool(block.name, block.input)
                content = "".join(
                    part.text for part in result.content if part.type == "text"
                )
                is_error = bool(result.isError)
            except Exception as exc:  # noqa: BLE001 - surface any failure to the model
                content = f"Tool execution failed: {exc}"
                is_error = True
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content or "(empty result)",
                    "is_error": is_error,
                }
            )

        messages.append({"role": "user", "content": tool_results})


def _fmt_args(args: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items())


async def _ainput(prompt: str) -> str:
    """Read a line from stdin without blocking the event loop.

    Uses a *daemon* thread rather than asyncio.to_thread's pooled (non-daemon)
    thread: on Ctrl+C the reader thread is still blocked in input(), and a
    non-daemon thread there hangs interpreter shutdown. A daemon thread is
    abandoned cleanly when the process exits.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str] = loop.create_future()

    def deliver(setter, value) -> None:
        def cb() -> None:
            if not fut.done():
                setter(value)
        try:
            loop.call_soon_threadsafe(cb)
        except RuntimeError:
            pass  # event loop already closed — we're shutting down

    def worker() -> None:
        try:
            line = input(prompt)
        except BaseException as exc:  # noqa: BLE001 - relay EOFError etc. to the awaiter
            deliver(fut.set_exception, exc)
        else:
            deliver(fut.set_result, line)

    threading.Thread(target=worker, daemon=True).start()
    return await fut


async def main() -> None:
    load_dotenv()
    _setup_history()  # enable ↑/↓ query history in the prompt
    # The SDK resolves credentials from ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN,
    # or an `ant auth login` profile. Fail fast with clear instructions when none
    # of those exist, so the user isn't shown the UI only to hit a cryptic SDK
    # error on their first message. (We still keep a mid-session safety net below.)
    have_env = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    have_profile = os.path.isdir(os.path.expanduser("~/.config/anthropic"))
    if not have_env and not have_profile:
        sys.exit(AUTH_HELP)

    anthropic = Anthropic()

    # Log in to GitHub so the server can reach the user's private repos. If a
    # GITHUB_TOKEN is already set we reuse it; otherwise run the OAuth device
    # flow. A failed/skipped login is non-fatal — we just fall back to public
    # data (unauthenticated → 60 requests/hour, public repos only).
    server_env = os.environ.copy()
    if server_env.get("GITHUB_TOKEN"):
        print(f"  {banner.DIM}Using GITHUB_TOKEN from the environment.{banner.RESET}")
    else:
        try:
            token = await asyncio.to_thread(github_auth.login)
            server_env["GITHUB_TOKEN"] = token
        except github_auth.DeviceFlowError as exc:
            print(f"  {banner.YELLOW}⚠ GitHub login unavailable:{banner.RESET} {exc}")
            print(f"  {banner.DIM}Continuing with public data only.{banner.RESET}\n")
        except (KeyboardInterrupt, asyncio.CancelledError):
            print(f"\n  {banner.DIM}Login skipped — continuing with public data only.{banner.RESET}\n")

    # Launch server.py in the same interpreter/venv over stdio.
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["server.py"],
        env=server_env,  # pass through GITHUB_TOKEN (from env or the login above)
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tool_list = (await session.list_tools()).tools
            tools = mcp_tools_to_anthropic(tool_list)

            print(banner.banner(f"interactive client · {MODEL}"))
            print()
            print(banner.tools_panel(
                [t["name"] for t in tools],
                {t["name"]: t["description"] for t in tools},
            ))
            print(
                f"\n  {banner.DIM}Ask about repos, issues, commits, or READMEs. "
                f"Type 'quit' or press Ctrl-C to exit.{banner.RESET}\n"
            )

            messages: list[dict] = []
            while True:
                try:
                    # Read input off the event loop so it keeps servicing the MCP
                    # stdio transport while we wait for the user. CancelledError is
                    # what a Ctrl+C delivers here (asyncio cancels the main task).
                    user_input = (await _ainput(PROMPT)).strip()
                except (EOFError, KeyboardInterrupt, asyncio.CancelledError):
                    print("\nBye.")
                    break
                if user_input.lower() in ("quit", "exit"):
                    print("Bye.")
                    break
                if not user_input:
                    continue

                checkpoint = len(messages)  # so we can roll back a failed turn
                messages.append({"role": "user", "content": user_input})
                try:
                    answer = await run_turn(anthropic, session, tools, messages)
                except Exception as exc:  # noqa: BLE001 - keep the REPL alive on any error
                    # Drop the partial turn so a half-finished tool exchange can't
                    # corrupt the conversation history on the next request.
                    del messages[checkpoint:]
                    if _looks_like_auth_error(exc):
                        sys.exit(AUTH_HELP)  # config problem — no point looping
                    print(f"\n\033[31m⚠ request failed:\033[0m {exc}\n  (Try again or ask something else.)\n")
                    continue
                print(f"\n\033[1mclaude ›\033[0m {answer}\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Ctrl+C pressed mid-request: asyncio re-raises it here after cancelling.
        print("\nBye.")
