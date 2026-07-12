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
import contextlib
import os
import sys
import threading
import time

try:
    # Importing readline transparently upgrades input() with line editing and
    # ↑/↓ history recall. Absent on some bare Windows installs — degrade quietly.
    import readline
except ImportError:  # pragma: no cover
    readline = None

try:
    # Needed to read a raw Esc keypress mid-request. Unix only; on Windows we
    # simply run without the Esc-to-cancel feature.
    import termios
    import tty
except ImportError:  # pragma: no cover
    termios = tty = None

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from rich.box import ROUNDED, SIMPLE_HEAVY
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import banner
import github_auth

# Single shared console renders all rich output (markdown answers, tables, panels).
console = Console()

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


def _tools_table(tools: list[dict]) -> Table:
    """Build a clean two-column table of the available tools."""
    table = Table(
        box=SIMPLE_HEAVY,
        title="Available tools",
        title_style="bold",
        header_style="bold cyan",
        pad_edge=False,
        expand=False,
    )
    table.add_column("Tool", style="bold cyan", no_wrap=True)
    table.add_column("What it does", style="dim")
    for t in tools:
        blurb = (t["description"] or "").strip().splitlines()[0] if t["description"] else ""
        table.add_row(t["name"], blurb)
    return table


class Usage:
    """Running token tally for the whole session and the current turn."""

    def __init__(self) -> None:
        self.turn_in = self.turn_out = 0
        self.total_in = self.total_out = 0

    def start_turn(self) -> None:
        self.turn_in = self.turn_out = 0

    def add(self, u) -> None:
        """Fold one API response's usage into the running totals."""
        if not u:
            return
        self.turn_in += u.input_tokens
        self.turn_out += u.output_tokens
        self.total_in += u.input_tokens
        self.total_out += u.output_tokens


def _log_tool(name: str, args: str) -> None:
    """Print a styled 'calling tool' line, wiping any spinner text first.

    Uses Text.append (not markup) so tool arguments containing brackets/quotes
    can't be misread as rich markup.
    """
    sys.stdout.write("\r\033[K")
    line = Text("  ↳ calling ", style="dim")
    line.append(name, style="bold cyan")
    line.append(f"({args})", style="dim")
    console.print(line)


_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


async def _spinner(task: asyncio.Task, usage: Usage) -> None:
    """Animate a progress line (elapsed + live token count) until `task` ends."""
    start = time.monotonic()
    i = 0
    while not task.done():
        elapsed = time.monotonic() - start
        sys.stdout.write(
            f"\r\033[2m  {_SPIN[i % len(_SPIN)]} working… {elapsed:4.1f}s"
            f"   turn ↑{usage.turn_in:,} ↓{usage.turn_out:,}"
            f"   (esc to cancel)\033[0m"
        )
        sys.stdout.flush()
        i += 1
        try:
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            break
    sys.stdout.write("\r\033[K")  # clear the spinner line
    sys.stdout.flush()


@contextlib.contextmanager
def _esc_watcher(on_esc):
    """While active, call on_esc() when the user presses Esc. Unix TTY only.

    Puts the terminal in cbreak mode and watches stdin via the event loop so a
    single keypress is delivered immediately (no Enter needed), then restores the
    terminal on exit. A no-op on Windows / non-TTY stdin.
    """
    if termios is None or not sys.stdin.isatty():
        yield
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    loop = asyncio.get_running_loop()

    def _on_readable() -> None:
        try:
            data = os.read(fd, 1024)
        except OSError:
            return
        if b"\x1b" in data:  # Esc (also the prefix of arrow keys — fine here)
            on_esc()

    try:
        tty.setcbreak(fd)
        loop.add_reader(fd, _on_readable)
        yield
    finally:
        loop.remove_reader(fd)
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


async def run_turn_interactive(
    anthropic: AsyncAnthropic,
    session: ClientSession,
    tools: list[dict],
    messages: list[dict],
    usage: Usage,
) -> tuple[str, str]:
    """Run one turn with a live spinner and Esc-to-cancel.

    Returns (status, answer): status is "ok" (answer holds the text) or
    "cancelled" (the user pressed Esc; answer is empty).
    """
    turn = asyncio.create_task(run_turn(anthropic, session, tools, messages, usage))
    cancelled = False

    def request_cancel() -> None:
        nonlocal cancelled
        cancelled = True
        turn.cancel()

    with _esc_watcher(request_cancel):
        await _spinner(turn, usage)

    try:
        return "ok", await turn
    except asyncio.CancelledError:
        if cancelled:
            return "cancelled", ""
        raise  # a genuine outer cancellation (real Ctrl-C) — don't swallow it


async def run_turn(
    anthropic: AsyncAnthropic,
    session: ClientSession,
    tools: list[dict],
    messages: list[dict],
    usage: Usage,
) -> str:
    """Run one user turn to completion, executing any tool calls Claude requests."""
    while True:
        response = await anthropic.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            tools=tools,
            messages=messages,
        )
        usage.add(response.usage)

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
            _log_tool(block.name, _fmt_args(block.input))
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

    anthropic = AsyncAnthropic()

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
            console.print(_tools_table(tools))
            console.print(
                "\n[dim]Ask about repos, issues, commits, or READMEs. "
                "Type [/dim][bold]quit[/bold][dim] or press [/dim][bold]Ctrl-C[/bold]"
                "[dim] to exit.[/dim]\n"
            )

            usage = Usage()
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

                checkpoint = len(messages)  # so we can roll back a failed/cancelled turn
                messages.append({"role": "user", "content": user_input})
                usage.start_turn()
                try:
                    status, answer = await run_turn_interactive(
                        anthropic, session, tools, messages, usage
                    )
                except Exception as exc:  # noqa: BLE001 - keep the REPL alive on any error
                    # Drop the partial turn so a half-finished tool exchange can't
                    # corrupt the conversation history on the next request.
                    del messages[checkpoint:]
                    if _looks_like_auth_error(exc):
                        sys.exit(AUTH_HELP)  # config problem — no point looping
                    console.print(Panel(
                        f"[red]{escape(str(exc))}[/red]\n"
                        "[dim]Try again or ask something else.[/dim]",
                        title="⚠ request failed",
                        title_align="left",
                        border_style="red",
                        box=ROUNDED,
                    ))
                    continue

                if status == "cancelled":
                    # Roll back so the aborted exchange can't corrupt history.
                    del messages[checkpoint:]
                    console.print("\n[yellow]✗ request cancelled.[/yellow]\n")
                    continue

                console.print(Panel(
                    Markdown(answer or "_(no answer)_"),
                    title="[bold]techment[/bold]",
                    title_align="left",
                    subtitle=(
                        f"[dim]turn ↑{usage.turn_in:,} ↓{usage.turn_out:,}"
                        f"  ·  session ↑{usage.total_in:,} ↓{usage.total_out:,}[/dim]"
                    ),
                    subtitle_align="right",
                    border_style="cyan",
                    box=ROUNDED,
                    padding=(1, 2),
                ))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Ctrl+C pressed mid-request: asyncio re-raises it here after cancelling.
        print("\nBye.")
