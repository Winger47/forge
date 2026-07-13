import os
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Protocol
from agent.tools import get_schemas, get_tool, is_dangerous, get_tool_names
from dotenv import load_dotenv
from openai import OpenAI
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from dataclasses import dataclass, field
import difflib


load_dotenv(Path(__file__).parent.parent / ".env")

console = Console()

# ── EXTERNAL TOOLS (MCP) ──────────────────────────────
import sys
from agent.mcp_client import MCPClient, to_openai_schema

MCP_CLIENT = MCPClient(command=sys.executable, args=["-m", "mcp_server_fetch"])
MCP_TOOLS = {}          # name -> mcp tool dict, filled at startup


def load_mcp_tools():
    """Discover the MCP server's tools. Failure is non-fatal — FORGE still
    runs on its local tools if the external server is unavailable."""
    global MCP_TOOLS
    try:
        MCP_TOOLS = {t["name"]: t for t in MCP_CLIENT.list_tools()}
    except Exception as e:
        console.print(f"[dim red]MCP unavailable: {e}[/dim red]")
        MCP_TOOLS = {}


def all_schemas():
    """Local @tool schemas + discovered MCP schemas, as one uniform list."""
    return get_schemas() + [to_openai_schema(t) for t in MCP_TOOLS.values()]


# ─────────────────────────────────────────────
# EVENTS (the agent announces; it never prints)
# ─────────────────────────────────────────────
@dataclass
class Event:
    type: str          # "status" | "tool_call" | "tool_result" | "text" | "cost" | "confirm_request"
    data: dict[str, Any]


class LLMClient(Protocol):
    """The contract the agent depends on. Anything with this method is an LLMClient."""
    def create(self, messages: list, tools: list): ...


# ─────────────────────────────────────────────
# 1. THE AI CONNECTIONS
# ─────────────────────────────────────────────
class DirectClient:
    """Talks straight to Groq."""
    def __init__(self):
        self.client = OpenAI(
            api_key=os.getenv("GROQ_API_KEY"),
            base_url="https://api.groq.com/openai/v1",
        )

    def create(self, messages, tools):
        return self.client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            tools=tools,
            temperature=0,
        )
    def create_stream(self, messages, tools):
        """Streaming variant. Bypasses the cache (can't stream a cached hit)."""
        return self.client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            tools=tools,
            temperature=0,
            stream=True,                                # ← the streaming switch
            stream_options={"include_usage": True},     # ← ask for token usage in the stream
            extra_headers={"Cache-Control": "no-cache"},  # streaming bypasses cache
        )


class GatewayClient:
    """Same interface as DirectClient — but calls OUR gateway, not Groq directly.
    bypass_cache=True sends Cache-Control: no-cache so the gateway skips the cache."""
    def __init__(self, bypass_cache: bool = False):
        self.bypass_cache = bypass_cache
        self.client = OpenAI(
            api_key="not-needed-yet",
            base_url="http://127.0.0.1:8000/v1",
        )

    def create(self, messages, tools):
        extra_headers = {"Cache-Control": "no-cache"} if self.bypass_cache else {}
        return self.client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            tools=tools,
            temperature=0,
            extra_headers=extra_headers,
        )
    def create_stream(self, messages, tools):
        """Streaming variant. Bypasses the cache (can't stream a cached hit)."""
        return self.client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            tools=tools,
            temperature=0,
            stream=True,                                # ← the streaming switch
            stream_options={"include_usage": True},     # ← ask for token usage in the stream
            extra_headers={"Cache-Control": "no-cache"},  # streaming bypasses cache
        )


# ─────────────────────────────────────────────
# 2. TOOLS — defined once in tools.py via @tool() decorator
# ─────────────────────────────────────────────


def build_system_prompt():
    """The system message. Built from the live tool registry so it never drifts."""
    tool_names = ", ".join(get_tool_names() + list(MCP_TOOLS.keys()))
    return {
        "role": "system",
        "content": (
            f"You are a task-completing agent with access to these tools: {tool_names}.\n\n"
            "HOW TO WORK:\n"
            "- When a task needs real data about files or the system, call the appropriate "
            "tool to get it. Do NOT guess or make up file contents — use a tool.\n"
            "- After a tool returns its result, READ the result and decide: do you now have "
            "enough to answer the user's goal?\n"
            "- If YES: stop calling tools and write your final answer directly. Do NOT call "
            "the same tool again.\n"
            "- If NO: call another tool to get what's still missing.\n\n"
            "IMPORTANT: Once you have the information you need, you MUST give a final text "
            "answer instead of calling more tools. Repeating a tool you already ran will not "
            "give new information. If a tool fails or is denied, do not retry it.\n\n"
            "This is a MULTI-TURN conversation — the user may ask follow-up questions that "
            "refer to earlier context (e.g. 'which of those is largest?'). Use the "
            "conversation history to resolve such references."
        ),
    }


# ─────────────────────────────────────────────
# 3. THE LOOP (think → act → look → repeat)
#    messages is OWNED BY THE SESSION and passed in — mutated in place so the
#    conversation survives across turns.
# ─────────────────────────────────────────────
# from dataclasses import dataclass, field


# ─────────────────────────────────────────────
#  STREAMING — reassemble fragmented chunks into a complete message.
#  Text chunks display live; tool-call chunks arrive as fragments that
#  must be accumulated (by index) and stitched back together.
# ─────────────────────────────────────────────
@dataclass
class StreamedToolCall:
    """A reassembled tool call — mimics the SDK's tool_call shape so the loop
    can use it unchanged (.id, .function.name, .function.arguments)."""
    id: str
    name: str
    arguments: str

    @property
    def function(self):
        # mimic tc.function.name / tc.function.arguments
        return type("F", (), {"name": self.name, "arguments": self.arguments})()

    def model_dump(self):
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass
class StreamedMessage:
    """A reassembled message — mimics response.choices[0].message
    so run_agent can treat streamed and non-streamed results identically."""
    content: str = None
    tool_calls: list = None


def stream_completion(client, messages, tools, on_text=None):
    """Make a STREAMING call and reassemble the chunks into a complete message.

    - text fragments are accumulated (and passed live to on_text if given)
    - tool-call fragments are accumulated by index and stitched together
    Returns a StreamedMessage with .content and .tool_calls, plus total_tokens.
    """
    stream = client.create_stream(messages, tools)

    text_parts = []
    tool_acc = {}          # index -> {"id", "name", "arguments"} being built up
    total_tokens = 0

    for chunk in stream:
        # usage may arrive on the final chunk (depends on provider settings)
        if getattr(chunk, "usage", None):
            total_tokens = chunk.usage.total_tokens

        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta

        # --- TEXT: accumulate, and stream live if a callback was given ---
        if getattr(delta, "content", None):
            text_parts.append(delta.content)
            if on_text:
                on_text(delta.content)          # print this piece live

        # --- TOOL CALLS: reassemble fragments, grouped by index ---
        if getattr(delta, "tool_calls", None):
            for frag in delta.tool_calls:
                idx = frag.index
                if idx not in tool_acc:
                    tool_acc[idx] = {"id": "", "name": "", "arguments": ""}
                if frag.id:
                    tool_acc[idx]["id"] = frag.id
                if frag.function and frag.function.name:
                    tool_acc[idx]["name"] += frag.function.name
                if frag.function and frag.function.arguments:
                    tool_acc[idx]["arguments"] += frag.function.arguments

    # assemble final results
    content = "".join(text_parts) or None
    tool_calls = None
    if tool_acc:
        tool_calls = [
            StreamedToolCall(id=v["id"], name=v["name"], arguments=v["arguments"])
            for _, v in sorted(tool_acc.items())    # sorted by index → correct order
        ]

    return StreamedMessage(content=content, tool_calls=tool_calls), total_tokens
def run_agent(messages, client: LLMClient, max_iterations=10, max_tokens=50000):
    total_tokens = 0
    last_call = None

    for i in range(max_iterations):
        yield Event("status", {"phase": "iteration", "n": i})

        if total_tokens > max_tokens:
            yield Event("status", {"phase": "aborted", "reason": "token budget exceeded"})
            return

        # --- THINK (streaming) ---
        text_pieces = []
        msg, call_tokens = stream_completion(
            client, messages, all_schemas(),
            on_text=lambda p: (text_pieces.append(p), console.print(p, end="")),
        )
        total_tokens += call_tokens
        yield Event("cost", {"total_tokens": total_tokens})

        # --- DONE? no tool call means the AI is finished ---
        if not msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content})
            if text_pieces:
                console.print()                      # newline after streamed text
            else:
                yield Event("text", {"content": msg.content})
            return

        # --- ACT ---
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
        })

        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            call_signature = f"{name}:{json.dumps(args, sort_keys=True)}"

            # --- GUARD: on a repeat, FORCE a final answer (streamed) ---
            if call_signature == last_call:
                messages.append({
                    "role": "user",
                    "content": (
                        "You already have the tool result above. Do not call any more tools. "
                        "Write your final answer to the original goal now, in plain text, "
                        "using the information you already have."
                    ),
                })
                fresh_client = GatewayClient(bypass_cache=True)
                fmsg, ftok = stream_completion(
                    fresh_client, messages, [],
                    on_text=lambda p: console.print(p, end=""),
                )
                total_tokens += ftok
                console.print()
                answer = fmsg.content
                messages.append({"role": "assistant", "content": answer})
                return

            # --- permission gate for dangerous tools ---
            if is_dangerous(name):
                decision = yield Event("confirm_request", {"name": name, "args": args})
                if decision != "yes":
                    result = (f"DENIED by user: {name} was not run. "
                              "Do not request this same action again.")
                    yield Event("tool_result", {"name": name, "content": result})
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                    last_call = call_signature
                    continue

            yield Event("tool_call", {"name": name, "args": args})
            last_call = call_signature

            try:
                if name in MCP_TOOLS:
                    result = MCP_CLIENT.call_tool(name, args)     # external, over MCP
                else:
                    result = get_tool(name)(**args)                # local @tool
            except Exception as e:
                result = f"ERROR: {type(e).__name__}: {e}"
            yield Event("tool_result", {"name": name, "content": result})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    yield Event("status", {"phase": "aborted", "reason": "max iterations"})


# ─────────────────────────────────────────────
#  COMPACTION — keep the conversation from growing unbounded.
#  The context window is a finite cache; summarize old turns, keep recent ones.
# ─────────────────────────────────────────────
def compact(messages, client, keep_recent=6, threshold=40):
    """If the conversation is long, summarize the older turns into one message.
    Keeps the system prompt and the most recent turns verbatim."""

    if len(messages) <= threshold:
        return messages

    system = messages[0]
    recent = messages[-keep_recent:]
    to_summarize = messages[1:-keep_recent]

    if not to_summarize:
        return messages

    summary_request = [
        {"role": "system", "content": "Summarize the following conversation history concisely, "
                                      "preserving key facts, decisions, file contents, and tool "
                                      "results the user might refer back to. Be brief."},
        {"role": "user", "content": json.dumps(to_summarize)},
    ]
    resp = client.create(summary_request, [])
    summary_text = resp.choices[0].message.content

    summary_msg = {
        "role": "system",
        "content": f"[Summary of earlier conversation]\n{summary_text}",
    }
    return [system, summary_msg] + recent


# ─────────────────────────────────────────────
# 4. PRESENTATION — rich rendering. The ONLY place that prints.
#    The agent yields plain Event objects; this decides how they LOOK.
#    Swapping plain print() for rich touched only this layer — the loop is unchanged.
# ─────────────────────────────────────────────
def _render(event):
    """Render one event with rich styling."""
    if event.type == "status":
        phase = event.data["phase"]
        n = event.data.get("n", "")
        reason = event.data.get("reason", "")
        if phase == "aborted":
            console.print(f"[dim red]! aborted: {reason}[/dim red]")
        else:
            console.print(f"[dim]. iteration {n}[/dim]")

    elif event.type == "tool_call":
        name = event.data["name"]
        args = event.data["args"]
        console.print(f"[bold cyan]-> {name}[/bold cyan][dim]({args})[/dim]")

    elif event.type == "tool_result":
        content = event.data["content"]
        preview = content[:300] + ("..." if len(content) > 300 else "")
        console.print(Panel(Text(preview), title="result", border_style="dim", expand=False))

    elif event.type == "cost":
        console.print(f"[dim]  tokens: {event.data['total_tokens']}[/dim]")

    elif event.type == "text":
        console.print(Panel(
            Text(event.data["content"]),
            title="[bold green]answer[/bold green]",
            border_style="green",
            expand=False,
        ))

def _render_diff(old_text, new_text):
    """Show a colored diff: red removals, green additions — like git diff."""
    diff = difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        lineterm="",
    )
    for line in diff:
        if line.startswith("+") and not line.startswith("+++"):
            console.print(f"[green]{line}[/green]")
        elif line.startswith("-") and not line.startswith("---"):
            console.print(f"[red]{line}[/red]")
        elif line.startswith("@@"):
            console.print(f"[cyan]{line}[/cyan]")
def main():
    load_mcp_tools()
    if MCP_TOOLS:
        console.print(f"[dim]mcp: {', '.join(MCP_TOOLS)}[/dim]")
    messages = [build_system_prompt()]      # THE CONVERSATION — created once, survives across goals

    console.print(Panel(
        "[bold]FORGE[/bold] — agentic CLI\n[dim]commands: /exit  /clear  /tools[/dim]",
        border_style="cyan", expand=False,
    ))

    while True:                     # SESSION loop — one iteration per user goal
        try:
            goal = console.input("[bold cyan]goal>[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            break

        if goal in ("/exit", "/quit"):
            console.print("[dim]bye[/dim]")
            break
        if goal == "/clear":
            messages = [build_system_prompt()]
            console.print("[dim](conversation cleared)[/dim]")
            continue
        if goal == "/tools":
            console.print("[bold]tools:[/bold] " + ", ".join(get_tool_names()))
            continue
        if not goal:
            continue

        # add the new goal to the SAME history, so the agent remembers prior turns
        messages.append({"role": "user", "content": goal})

        # compact if the conversation has grown long (summary call must bypass cache)
        messages = compact(messages, GatewayClient(bypass_cache=True))

        client = GatewayClient()
        agent = run_agent(messages, client)     # pass the shared conversation IN
        to_send = None
        while True:                             # drive the agent generator
            try:
                event = agent.send(to_send)
            except StopIteration:
                break
            to_send = None

            if event.type == "confirm_request":
                name = event.data["name"]
                args = event.data["args"]
                console.print(f"[bold yellow]! approve {name}?[/bold yellow]")
                if name == "edit_file" and "old_text" in args and "new_text" in args:
                    console.print(f"[dim]file: {args.get('path', '?')}[/dim]")
                    _render_diff(args["old_text"], args["new_text"])
                else:
                    console.print(f"[dim]{args}[/dim]")
                answer = console.input("  [yellow]approve[/yellow] [dim][yes/no][/dim]: ").strip().lower()
                to_send = "yes" if answer in ("yes", "y") else "no"
            else:
                _render(event)

        console.print()   # blank line between turns


if __name__ == "__main__":
    main()