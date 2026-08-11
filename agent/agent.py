import os
import json
import subprocess
import datetime
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

import tomllib

MCP_SERVERS = {}      # server_name -> MCPClient
MCP_TOOLS = {}        # tool_name   -> {"server": name, "tool": {...}}
MCP_SAFE = set()      # tool names a human declared read-only, from config


def load_mcp_tools(config_path="forge.toml"):
    """Read forge.toml, spawn every declared server, and merge their tools
    into one registry — remembering WHICH server owns each tool.

    A server that fails to start is skipped, not fatal: an external dependency
    dying should cost a capability, not the whole agent.
    """
    global MCP_SERVERS, MCP_TOOLS, MCP_SAFE
    MCP_SERVERS, MCP_TOOLS, MCP_SAFE = {}, {}, set()

    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
    except FileNotFoundError:
        return                                  # no config, no MCP — fine

    for server_name, spec in config.get("mcp", {}).items():
        try:
            client = MCPClient(command=spec["command"], args=spec.get("args", []))
            tools = client.list_tools()
        except Exception as e:
            console.print(f"[dim red]mcp '{server_name}' unavailable: {e}[/dim red]")
            continue

        MCP_SERVERS[server_name] = client
        MCP_SAFE.update(spec.get("safe_tools", []))

        for t in tools:
            name = t["name"]
            if name in get_tool_names():
                console.print(f"[dim yellow]mcp '{server_name}' tool '{name}' shadows a "
                              f"local tool — skipped[/dim yellow]")
                continue                        # local tools win; never silently shadowed
            if name in MCP_TOOLS:
                console.print(f"[dim yellow]mcp '{server_name}' tool '{name}' collides with "
                              f"'{MCP_TOOLS[name]['server']}' — skipped[/dim yellow]")
                continue                        # first server to claim a name keeps it
            MCP_TOOLS[name] = {"server": server_name, "tool": t}


def needs_approval(name: str) -> bool:
    """Local tools: their declared danger flag.
    MCP tools: deny by default unless a human allowlisted them in config."""
    if name in MCP_TOOLS:
        return name not in MCP_SAFE
    return is_dangerous(name)


def call_mcp_tool(name: str, args: dict):
    """Route a tool call to the server that owns it."""
    entry = MCP_TOOLS[name]
    return MCP_SERVERS[entry["server"]].call_tool(name, args)


def all_schemas():
    """Local @tool schemas + discovered MCP schemas, as one uniform list."""
    return get_schemas() + [to_openai_schema(e["tool"]) for e in MCP_TOOLS.values()]


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
DEFAULT_MODEL = "llama-3.3-70b-versatile"

# Known Groq models — hint list for /model, NOT a validation gate.
# Any string is accepted; if it's wrong, the next request will fail with the
# provider's own error, which is better than a stale hardcoded allowlist.
KNOWN_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "mixtral-8x7b-32768",
    "gemma2-9b-it",
]


class DirectClient:
    """Talks straight to Groq."""
    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model
        self.client = OpenAI(
            api_key=os.getenv("GROQ_API_KEY"),
            base_url="https://api.groq.com/openai/v1",
        )

    def create(self, messages, tools):
        return self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            temperature=0,
        )
    def create_stream(self, messages, tools):
        """Streaming variant. Bypasses the cache (can't stream a cached hit)."""
        return self.client.chat.completions.create(
            model=self.model,
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
    def __init__(self, bypass_cache: bool = False, model: str = DEFAULT_MODEL):
        self.bypass_cache = bypass_cache
        self.model = model
        self.client = OpenAI(
            api_key="not-needed-yet",
            base_url="http://127.0.0.1:8000/v1",
        )

    def create(self, messages, tools):
        extra_headers = {"Cache-Control": "no-cache"} if self.bypass_cache else {}
        return self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            temperature=0,
            extra_headers=extra_headers,
        )
    def create_raw(self, messages, tools):
        """Non-streaming call that ALSO returns the HTTP headers, so the caller
        can read the gateway's X-Cache decision (HIT / SEMANTIC / MISS / BYPASS).
        Returns (completion, cache_status)."""
        extra_headers = {"Cache-Control": "no-cache"} if self.bypass_cache else {}
        raw = self.client.chat.completions.with_raw_response.create(
            model=self.model,
            messages=messages,
            tools=tools,
            temperature=0,
            extra_headers=extra_headers,
        )
        return raw.parse(), raw.headers.get("x-cache")
    def create_stream(self, messages, tools):
        """Streaming variant. Bypasses the cache (can't stream a cached hit)."""
        return self.client.chat.completions.create(
            model=self.model,
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


def _env_context() -> str:
    """Ambient facts the model shouldn't waste tool calls figuring out.
    Kept short — every character re-ships on every request."""
    cwd = os.getcwd()
    project = os.path.basename(cwd)
    date = datetime.date.today().isoformat()

    branch = ""
    try:
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=cwd, capture_output=True, text=True, timeout=1,
        ).stdout.strip()
    except Exception:
        pass

    about = ""
    for candidate in ("README.md", "readme.md", "Readme.md"):
        p = os.path.join(cwd, candidate)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    text = f.read(1200)
                for para in text.split("\n\n"):
                    para = para.strip().replace("\n", " ")
                    if para and not para.startswith("#"):
                        about = para[:220]
                        break
            except Exception:
                pass
            break

    parts = [f"cwd: {cwd}", f"project: {project}", f"date: {date}"]
    if branch:
        parts.append(f"branch: {branch}")
    if about:
        parts.append(f"about: {about}")
    return "\n".join(parts)


def build_system_prompt():
    """Lean system message. Behaviors the code already enforces (repeat guard,
    per-run denials) are NOT restated here — the README's rule is 'guards live
    in code, not prompts'. Environment context lets the model skip trivial
    lookups. Tool list is here so the model sees availability without parsing
    every schema."""
    tool_names = ", ".join(get_tool_names() + list(MCP_TOOLS.keys()))
    return {
        "role": "system",
        "content": (
            "You are an agent that completes tasks by calling tools and writing answers. "
            "Use tools for real data; never invent file contents. When you have enough, "
            "stop calling tools and answer. Multi-turn — earlier messages carry context.\n\n"
            f"ENVIRONMENT\n{_env_context()}\n\n"
            f"TOOLS  {tool_names}"
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


TOOL_RESULT_CAP = 8000     # chars per tool result stored in messages


def _wrap_tool_result(name: str, args: dict, result) -> str:
    """Header line + oversize cap. Header tells the model at a glance what
    it got (name, size, key identifier). Cap prevents a single huge read
    from blowing the context window — the model can request a range for more."""
    body = "" if result is None else str(result)
    n_lines = body.count("\n") + 1 if body else 0

    # highlight the most identifying arg, in preference order
    ident = ""
    for key in ("path", "url", "pattern", "command", "expression"):
        if isinstance(args, dict) and key in args:
            v = str(args[key]).replace("\n", " ")
            if len(v) > 60:
                v = v[:59] + "…"
            ident = f" · {key}={v}"
            break

    truncated = ""
    if len(body) > TOOL_RESULT_CAP:
        omitted = len(body) - TOOL_RESULT_CAP
        body = body[:TOOL_RESULT_CAP]
        truncated = f" · TRUNCATED (+{omitted} chars omitted — request a range for more)"

    header = f"[{name} · {n_lines} lines · {len(body)} chars{ident}{truncated}]"
    return f"{header}\n{body}"


def run_agent(messages, client: LLMClient, max_iterations=10, max_tokens=50000,
              stream_enabled=True):
    total_tokens = 0
    last_call = None
    denied_tools = set()      # tools the human refused THIS RUN — denial is per-ACTION,
                              # not per-call, or the model reruns it with tweaked args

    for i in range(max_iterations):
        yield Event("status", {"phase": "iteration", "n": i})

        if total_tokens > max_tokens:
            yield Event("status", {"phase": "aborted", "reason": "token budget exceeded"})
            return

        # --- THINK ---
        # Streaming (default): tokens display live, but the gateway bypasses the
        # cache, so there is no hit/miss to report.
        # Non-streaming (/stream off): the request goes THROUGH the cache, so the
        # gateway's X-Cache decision comes back and we surface it as a cache event.
        text_pieces = []
        status = console.status("[dim]thinking…[/dim]", spinner="dots")
        status.start()
        spinner_stopped = [False]

        def stop_spinner():
            if not spinner_stopped[0]:
                status.stop()
                spinner_stopped[0] = True

        def on_text(p):
            stop_spinner()
            text_pieces.append(p)
            console.print(p, end="")

        stream_error = None
        cache_status = None
        try:
            if stream_enabled:
                msg, call_tokens = stream_completion(
                    client, messages, all_schemas(), on_text=on_text,
                )
            else:
                # cacheable path: one non-streamed call, read the X-Cache header
                completion, cache_status = client.create_raw(messages, all_schemas())
                stop_spinner()
                msg = completion.choices[0].message
                call_tokens = completion.usage.total_tokens if completion.usage else 0
                if msg.content:
                    console.print(msg.content)
                    text_pieces.append(msg.content)   # so the DONE branch adds no extra newline
        except Exception as e:
            # the gateway surfaces provider failures as an error rather than a
            # silent [DONE]; turn it into a clean abort, not a traceback.
            stream_error = e
        finally:
            stop_spinner()

        if stream_error is not None:
            yield Event("status", {"phase": "aborted",
                                   "reason": f"request failed: {stream_error}"})
            return

        if cache_status:
            yield Event("cache", {"status": cache_status})

        total_tokens += call_tokens
        yield Event("cost", {"total_tokens": total_tokens})

        # --- DONE? no tool call means the AI is finished ---
        if not msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content})
            if text_pieces:
                console.print()                      # newline after streamed text
            elif msg.content:
                yield Event("text", {"content": msg.content})
            else:
                yield Event("status", {"phase": "aborted",
                                       "reason": "empty response from model"})
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
                fresh_client = GatewayClient(bypass_cache=True, model=getattr(client, "model", DEFAULT_MODEL))
                fmsg, ftok = stream_completion(
                    fresh_client, messages, [],
                    on_text=lambda p: console.print(p, end=""),
                )
                total_tokens += ftok
                console.print()
                answer = fmsg.content
                messages.append({"role": "assistant", "content": answer})
                return

            # --- a denial is FINAL for this run, whatever args the model invents ---
            if name in denied_tools:
                result = (f"{name} was already denied by the user in this session and will "
                          f"NOT be executed with any arguments. Stop requesting it. Give your "
                          f"final answer using what you already have, and state that the "
                          f"action was declined.")
                yield Event("tool_result", {"name": name, "content": result})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                last_call = call_signature
                continue

            # --- permission gate for dangerous tools ---
            if needs_approval(name):
                decision = yield Event("confirm_request", {"name": name, "args": args})
                if decision != "yes":
                    denied_tools.add(name)      # the ACTION is denied, not just this call
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
                    result = call_mcp_tool(name, args)            # routed to its server
                else:
                    result = get_tool(name)(**args)                # local @tool
            except Exception as e:
                result = f"ERROR: {type(e).__name__}: {e}"
            yield Event("tool_result", {"name": name, "content": result})
            # For the model: wrap with a header line and cap oversize output so a
            # single big read_file can't blow the context window. Display gets raw.
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": _wrap_tool_result(name, args, result),
            })

    yield Event("status", {"phase": "aborted", "reason": "max iterations"})


# ─────────────────────────────────────────────
#  COMPACTION — keep the conversation from growing unbounded.
#  The context window is a finite cache; summarize old turns, keep recent ones.
#  Trigger by ESTIMATED TOKENS, not message count — a single 8KB tool result
#  is more urgent than 40 short messages.
# ─────────────────────────────────────────────
def _estimate_tokens(messages: list) -> int:
    """Rough estimate: ~4 chars per token. Fast, good enough as a trigger."""
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c) // 4
        for tc in m.get("tool_calls") or []:
            if isinstance(tc, dict):
                total += len(tc.get("function", {}).get("arguments", "")) // 4
    return total


def compact(messages, client, keep_recent=6, token_threshold=8000):
    """If the estimated context exceeds token_threshold, summarize older turns.
    System prompt and the most recent `keep_recent` messages stay verbatim.
    The summary prompt is specific: preserve facts the model will need again,
    drop the assistant's reasoning chatter and redundant tool calls."""

    if _estimate_tokens(messages) < token_threshold:
        return messages
    if len(messages) <= keep_recent + 2:
        return messages                    # nothing meaningful to compact yet

    system = messages[0]
    recent = messages[-keep_recent:]
    to_summarize = messages[1:-keep_recent]

    if not to_summarize:
        return messages

    summary_request = [
        {"role": "system", "content": (
            "Summarize a working session between a user and an autonomous agent. "
            "PRESERVE VERBATIM: user goals, decisions, file paths read/written, "
            "commands run, tool results the model may need again, error messages. "
            "DROP: verbose reasoning, repeated tool calls, filler acknowledgements. "
            "Output as a compact numbered list of facts, most recent last. No prose intro."
        )},
        {"role": "user", "content": json.dumps(to_summarize)},
    ]
    resp = client.create(summary_request, [])
    summary_text = resp.choices[0].message.content

    summary_msg = {
        "role": "system",
        "content": f"[SUMMARY of {len(to_summarize)} earlier messages]\n{summary_text}",
    }
    console.print(f"  [dim]· compacted {len(to_summarize)} earlier messages[/dim]")
    return [system, summary_msg] + recent


# ─────────────────────────────────────────────
# 4. PRESENTATION — rich rendering. The ONLY place that prints.
#    The agent yields plain Event objects; this decides how they LOOK.
#    Swapping plain print() for rich touched only this layer — the loop is unchanged.
# ─────────────────────────────────────────────
def _fmt_args(args: dict, max_len: int = 64) -> str:
    """Collapse a tool's args dict into one short line for the call header.
    Long values are truncated with an ellipsis so a call fits on one row."""
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        s = str(v).replace("\n", " ⏎ ")
        if len(s) > 32:
            s = s[:31] + "…"
        parts.append(f"{k}={s}")
    joined = ", ".join(parts)
    if len(joined) > max_len:
        joined = joined[:max_len - 1] + "…"
    return joined


def _render(event):
    """Render one Event. Kept quiet on purpose — iteration ticks and running
    token counts are suppressed to avoid drowning the actual work."""
    t = event.type

    if t == "status":
        if event.data["phase"] == "aborted":
            console.print(f"  [red]✗[/red] [dim red]{event.data.get('reason', 'aborted')}[/dim red]")

    elif t == "tool_call":
        name = event.data["name"]
        args = _fmt_args(event.data["args"])
        args_part = f"[dim]({args})[/dim]" if args else ""
        console.print(f"  [cyan]→[/cyan] [bold]{name}[/bold]{args_part}")

    elif t == "tool_result":
        content = str(event.data["content"])
        # short single-line results collapse to one indented line
        if len(content) <= 80 and "\n" not in content:
            console.print(f"    [dim]↳ {content}[/dim]")
            return
        preview = content[:400] + ("…" if len(content) > 400 else "")
        console.print(Panel(
            Text(preview, style="grey70"),
            border_style="grey30",
            expand=False,
            padding=(0, 1),
        ))

    elif t == "cost":
        pass                              # too noisy per iteration; surfaced elsewhere

    elif t == "text":
        # only reached when a final answer arrived non-streamed
        console.print(Panel(
            Text(event.data["content"]),
            border_style="green",
            expand=False,
            padding=(0, 1),
        ))


def _render_diff(old_text: str, new_text: str):
    """Colored unified diff with line-number gutter — the shape of a
    minimal `git diff`, without the noise of headers we don't need."""
    lines = list(difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        lineterm="",
        n=2,
    ))
    for line in lines:
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            console.print(f"    [cyan]{line}[/cyan]")
        elif line.startswith("+"):
            console.print(f"    [green]+ {line[1:]}[/green]")
        elif line.startswith("-"):
            console.print(f"    [red]- {line[1:]}[/red]")
        else:
            console.print(f"    [dim]  {line[1:] if line.startswith(' ') else line}[/dim]")


# ─────────────────────────────────────────────
#  SESSION PERSISTENCE — save/restore whole conversations to disk.
#  Every completed turn auto-saves to sessions/last.json so `/resume` works.
#  Named sessions via `/save <name>` and `/load <name>`.
# ─────────────────────────────────────────────
SESSIONS_DIR = Path.home() / ".forge" / "sessions"


def _sanitize_session_name(name: str) -> str:
    """Filesystem-safe name: alphanumeric, hyphen, underscore only. Capped at 64."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name).strip("_")
    return (safe[:64] or "session")


def save_session(name: str, messages: list, model: str) -> Path:
    """Persist a session to disk. Preserves `created` timestamp on re-save."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = SESSIONS_DIR / f"{_sanitize_session_name(name)}.json"
    now = datetime.datetime.now().isoformat(timespec="seconds")
    created = now
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                created = json.load(f).get("created", now)
        except Exception:
            pass
    payload = {"model": model, "created": created, "updated": now, "messages": messages}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return path


def load_session(name: str):
    """Return (messages, model) or None if not found / unreadable."""
    path = SESSIONS_DIR / f"{_sanitize_session_name(name)}.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return None
    return payload.get("messages", []), payload.get("model", DEFAULT_MODEL)


def list_sessions() -> list[dict]:
    """Newest first. Silently skips unreadable files."""
    if not SESSIONS_DIR.exists():
        return []
    out = []
    for p in sorted(SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with open(p, encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            continue
        out.append({
            "name": p.stem,
            "updated": payload.get("updated", ""),
            "messages": len(payload.get("messages", [])),
            "model": payload.get("model", ""),
        })
    return out


def _print_sessions():
    rows = list_sessions()
    if not rows:
        console.print("  [dim]no saved sessions in " + str(SESSIONS_DIR) + "[/dim]\n")
        return
    console.print("  [bold]sessions[/bold]  [dim](newest first)[/dim]")
    for r in rows:
        console.print(
            f"    [cyan]{r['name']:<20}[/cyan] "
            f"[dim]{r['messages']:>3} msg  ·  {r['updated']}  ·  {r['model']}[/dim]"
        )
    console.print(r"  [dim]usage: /load <name>  ·  /resume  ·  /save \[name][/dim]" + "\n")


def _print_last_session_hint():
    """One-line hint at startup so users know they can pick up where they left off."""
    path = SESSIONS_DIR / "last.json"
    if not path.exists():
        return
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        n_msgs = len(payload.get("messages", []))
        # user messages only, to give a truer sense of session length
        n_user = sum(1 for m in payload.get("messages", []) if m.get("role") == "user")
        updated = payload.get("updated", "")
        if n_user == 0:
            return
        console.print(
            f"  [dim]last session:[/dim] [bold]{n_user}[/bold][dim] goals · {updated}  ·  "
            f"[/dim][cyan]/resume[/cyan][dim] to continue[/dim]"
        )
    except Exception:
        pass


def _gateway_up(host: str = "127.0.0.1", port: int = 8000, timeout: float = 0.5) -> bool:
    """TCP-probe the gateway so we can warn the user at startup instead of
    surfacing a confusing traceback on the first request."""
    import socket
    with socket.socket() as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def _print_header(model: str = DEFAULT_MODEL):
    """The banner. Small, quiet, informative — no ascii art."""
    n_local = len(get_tool_names())
    n_mcp = len(MCP_TOOLS)
    tools_bit = f"{n_local} local"
    if n_mcp:
        tools_bit += f" · {n_mcp} mcp"
    console.print()
    console.print(f"  [bold cyan]forge[/bold cyan]  [dim]agentic cli · {tools_bit}[/dim]")
    console.print(f"  [dim]model:[/dim] [bold]{model}[/bold]")
    if _gateway_up():
        console.print(f"  [dim]gateway [green]●[/green] :8000  ·  /help  /model  /sessions  /clear  /exit[/dim]")
    else:
        console.print(f"  [dim]gateway [red]●[/red] :8000  [red](unreachable — start with:[/red] "
                      f"[cyan]uvicorn gateway.main:app --port 8000[/cyan][red])[/red][/dim]")
        console.print(f"  [dim]/help  /model  /sessions  /clear  /exit[/dim]")
    _print_last_session_hint()
    console.print()


def _print_models(current: str):
    """List the current model plus known Groq options. Any other name still works."""
    console.print(f"  [bold]current[/bold]  [cyan]{current}[/cyan]")
    console.print(f"  [bold]known[/bold]    [dim](any name is accepted — this is just a hint)[/dim]")
    for m in KNOWN_MODELS:
        marker = "[green]●[/green]" if m == current else "[dim]○[/dim]"
        console.print(f"    {marker} [dim]{m}[/dim]")
    console.print(f"  [dim]usage: /model <name>[/dim]")
    console.print()


def _print_tools():
    local = get_tool_names()
    console.print(f"  [bold]local[/bold]  [dim]{', '.join(local)}[/dim]")
    if MCP_TOOLS:
        by_server: dict[str, list[str]] = {}
        for name, entry in MCP_TOOLS.items():
            by_server.setdefault(entry["server"], []).append(name)
        for server, names in by_server.items():
            console.print(f"  [bold]mcp[/bold]    [cyan]{server}[/cyan] [dim]{', '.join(names)}[/dim]")
    console.print()


def _print_help():
    console.print("  [bold]commands[/bold]")
    console.print("    [cyan]/help[/cyan]           show this list")
    console.print("    [cyan]/tools[/cyan]          list available tools")
    console.print("    [cyan]/model[/cyan]          show current model and known options")
    console.print("    [cyan]/model <name>[/cyan]   switch to a different model")
    console.print(r"    [cyan]/stream \[on|off][/cyan] toggle streaming; off shows cache HIT/MISS")
    console.print("    [cyan]/sessions[/cyan]       list saved sessions")
    console.print("    [cyan]/resume[/cyan]         continue the auto-saved last session")
    console.print(r"    [cyan]/save \[name][/cyan]    save current session (default: timestamp)")
    console.print("    [cyan]/load <name>[/cyan]    replace current session with a saved one")
    console.print("    [cyan]/clear[/cyan]          reset the conversation")
    console.print("    [cyan]/exit[/cyan]           quit")
    console.print()


def main():
    load_mcp_tools()
    messages = [build_system_prompt()]      # THE CONVERSATION — survives across goals
    current_model = DEFAULT_MODEL           # session-scoped; /model swaps it
    stream_enabled = True                   # /stream toggles; off = cacheable + cache tag

    _print_header(current_model)

    while True:                             # SESSION loop — one iteration per user goal
        try:
            goal = console.input("[bold cyan]▸[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n  [dim]bye[/dim]")
            break

        if goal in ("/exit", "/quit"):
            console.print("  [dim]bye[/dim]")
            break
        if goal == "/clear":
            messages = [build_system_prompt()]
            console.print("  [dim]conversation cleared[/dim]\n")
            continue
        if goal == "/tools":
            _print_tools()
            continue
        if goal == "/help":
            _print_help()
            continue
        if goal == "/model":
            _print_models(current_model)
            continue
        if goal.startswith("/model "):
            new_model = goal[len("/model "):].strip()
            if not new_model:
                _print_models(current_model)
            else:
                current_model = new_model
                console.print(f"  [dim]model →[/dim] [bold cyan]{current_model}[/bold cyan]\n")
            continue
        if goal == "/stream" or goal.startswith("/stream "):
            arg = goal[len("/stream"):].strip().lower()
            if arg in ("on", "off"):
                stream_enabled = (arg == "on")
            elif not arg:
                stream_enabled = not stream_enabled          # bare /stream flips it
            else:
                console.print("  [dim red]usage: /stream [on|off][/dim red]\n")
                continue
            if stream_enabled:
                console.print("  [dim]streaming [green]on[/green] — live tokens, cache bypassed[/dim]\n")
            else:
                console.print("  [dim]streaming [yellow]off[/yellow] — cacheable, shows cache HIT/MISS[/dim]\n")
            continue
        if goal == "/sessions":
            _print_sessions()
            continue
        if goal == "/resume":
            loaded = load_session("last")
            if loaded is None:
                console.print("  [dim red]no previous session found[/dim red]\n")
            else:
                messages, current_model = loaded
                n_user = sum(1 for m in messages if m.get("role") == "user")
                console.print(f"  [dim]resumed · {len(messages)} messages · {n_user} goals · "
                              f"model[/dim] [bold cyan]{current_model}[/bold cyan]\n")
            continue
        if goal == "/save" or goal.startswith("/save "):
            name = goal[len("/save"):].strip() or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            try:
                path = save_session(name, messages, current_model)
                console.print(f"  [dim]saved →[/dim] [cyan]{path.name}[/cyan]\n")
            except Exception as e:
                console.print(f"  [dim red]save failed: {e}[/dim red]\n")
            continue
        if goal.startswith("/load "):
            name = goal[len("/load "):].strip()
            loaded = load_session(name)
            if loaded is None:
                console.print(f"  [dim red]session '{name}' not found (see /sessions)[/dim red]\n")
            else:
                messages, current_model = loaded
                console.print(f"  [dim]loaded '{name}' · {len(messages)} messages · "
                              f"model[/dim] [bold cyan]{current_model}[/bold cyan]\n")
            continue
        if not goal:
            continue

        # append to the SAME history so the agent remembers prior turns
        messages.append({"role": "user", "content": goal})
        # compact if the conversation has grown long (summary call bypasses cache)
        messages = compact(messages, GatewayClient(bypass_cache=True, model=current_model))

        console.print()                     # breathing room before the response
        client = GatewayClient(model=current_model)
        agent = run_agent(messages, client, stream_enabled=stream_enabled)
        to_send = None
        final_tokens = 0
        cache_status = None
        try:
            while True:                     # drive the agent generator
                try:
                    event = agent.send(to_send)
                except StopIteration:
                    break
                to_send = None

                if event.type == "cost":
                    final_tokens = event.data["total_tokens"]
                    continue

                if event.type == "cache":
                    cache_status = event.data["status"]
                    continue

                if event.type == "confirm_request":
                    name = event.data["name"]
                    args = event.data["args"]
                    console.print(f"\n  [yellow]![/yellow] [bold yellow]approve[/bold yellow] [bold]{name}[/bold]")
                    if name == "edit_file" and "old_text" in args and "new_text" in args:
                        console.print(f"    [dim]{args.get('path', '?')}[/dim]")
                        _render_diff(args["old_text"], args["new_text"])
                    else:
                        console.print(f"    [dim]{_fmt_args(args, max_len=200)}[/dim]")
                    answer = console.input("  [yellow]›[/yellow] [dim]y/n[/dim] ").strip().lower()
                    to_send = "yes" if answer in ("yes", "y") else "no"
                else:
                    _render(event)
        except KeyboardInterrupt:
            # Ctrl-C during a turn: kill this run, don't kill the session
            agent.close()
            console.print("\n  [dim red]✗ aborted[/dim red]")

        # per-turn footer: tokens, model, and — when non-streaming — the cache decision
        cache_tag = ""
        if cache_status:
            colors = {"HIT": "green", "SEMANTIC": "cyan", "MISS": "yellow", "BYPASS": "dim"}
            c = colors.get(cache_status, "dim")
            cache_tag = f" · cache [{c}]{cache_status}[/{c}]"
        if final_tokens or cache_tag:
            console.print(f"\n  [dim]· {final_tokens} tokens · {current_model}[/dim]{cache_tag}")

        # auto-save so /resume works next launch; silent on failure
        try:
            save_session("last", messages, current_model)
        except Exception:
            pass

        console.print()


if __name__ == "__main__":
    main()