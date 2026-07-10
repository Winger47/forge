import os
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Protocol
from agent.tools import get_schemas, get_tool, is_dangerous, get_tool_names
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path(__file__).parent.parent / ".env")


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


# ─────────────────────────────────────────────
# 2. TOOLS — defined once in tools.py via @tool() decorator
# ─────────────────────────────────────────────


def build_system_prompt():
    """The system message. Built from the live tool registry so it never drifts."""
    tool_names = ", ".join(get_tool_names())
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
def run_agent(messages, client: LLMClient, max_iterations=10, max_tokens=50000):
    total_tokens = 0
    last_call = None

    for i in range(max_iterations):
        yield Event("status", {"phase": "iteration", "n": i})

        # --- GUARD: token budget ---
        if total_tokens > max_tokens:
            yield Event("status", {"phase": "aborted", "reason": "token budget exceeded"})
            return

        # --- THINK ---
        response = client.create(messages, get_schemas())
        msg = response.choices[0].message
        total_tokens += response.usage.total_tokens
        yield Event("cost", {"total_tokens": total_tokens})

        # --- DONE? no tool call means the AI is finished ---
        if not msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content})
            yield Event("text", {"content": msg.content})
            return

        # --- ACT: record the assistant tool-call message ---
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
        })

        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments)
            call_signature = f"{name}:{json.dumps(args, sort_keys=True)}"

            # --- GUARD: on a repeat, FORCE a final answer (remove ability to loop) ---
            if call_signature == last_call:
                messages.append({
                    "role": "user",
                    "content": (
                        "You already have the tool result above. Do not call any more tools. "
                        "Write your final answer to the original goal now, in plain text, "
                        "using the information you already have."
                    ),
                })
                fresh_client = GatewayClient(bypass_cache=True)   # forced answer -> not cached
                forced = fresh_client.create(messages, [])
                total_tokens += forced.usage.total_tokens
                yield Event("cost", {"total_tokens": total_tokens})
                answer = forced.choices[0].message.content
                messages.append({"role": "assistant", "content": answer})
                yield Event("text", {"content": answer})
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
                result = get_tool(name)(**args)
            except Exception as e:
                result = f"ERROR: {type(e).__name__}: {e}"   # tool failure = data, not a crash
            yield Event("tool_result", {"name": name, "content": result})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    yield Event("status", {"phase": "aborted", "reason": "max iterations"})


# ─────────────────────────────────────────────
#  COMPACTION — keep the conversation from growing unbounded.
#  The context window is a finite cache; summarize old turns, keep recent ones.
#  Threshold is high so it only fires on genuinely long sessions (not every turn).
# ─────────────────────────────────────────────
def compact(messages, client, keep_recent=6, threshold=6):
    """If the conversation is long, summarize the older turns into one message.
    Keeps the system prompt and the most recent turns verbatim.
    Returns a new (shorter) messages list, or the original if no compaction needed."""

    if len(messages) <= threshold:
        return messages

    system = messages[0]                      # always keep the system prompt
    recent = messages[-keep_recent:]          # keep the last few messages verbatim
    to_summarize = messages[1:-keep_recent]   # the middle — old turns to compress

    if not to_summarize:
        return messages

    summary_request = [
        {"role": "system", "content": "Summarize the following conversation history concisely, "
                                      "preserving key facts, decisions, file contents, and tool "
                                      "results the user might refer back to. Be brief."},
        {"role": "user", "content": json.dumps(to_summarize)},
    ]
    resp = client.create(summary_request, []) 
    print(f"  [COMPACTING: {len(messages)} messages -> summary + {keep_recent} recent]")  # no tools needed for summarizing
    summary_text = resp.choices[0].message.content

    summary_msg = {
        "role": "system",
        "content": f"[Summary of earlier conversation]\n{summary_text}",
    }
    return [system, summary_msg] + recent


# ─────────────────────────────────────────────
# 4. ENTRY POINT — owns the conversation; loops per user goal (multi-turn session)
# ─────────────────────────────────────────────
def _render(event):
    """Render one event to the terminal. The ONLY place that prints."""
    if event.type == "status":
        reason = event.data.get("reason", "")
        print(f"[status: {event.data['phase']} {event.data.get('n', '')} {reason}]".rstrip())
    elif event.type == "tool_call":
        print(f"  -> {event.data['name']}({event.data['args']})")
    elif event.type == "tool_result":
        print(f"  <- {event.data['content'][:200]}")
    elif event.type == "cost":
        print(f"  [tokens so far: {event.data['total_tokens']}]")
    elif event.type == "text":
        print(f"\n{event.data['content']}")


def main():
    messages = [build_system_prompt()]      # THE CONVERSATION — created once, survives across goals

    print("FORGE — enter a goal. Commands: /exit  /clear  /tools\n")

    while True:                     # SESSION loop — one iteration per user goal
        goal = input("goal> ").strip()

        if goal in ("/exit", "/quit"):
            break
        if goal == "/clear":
            messages = [build_system_prompt()]
            print("(conversation cleared)\n")
            continue
        if goal == "/tools":
            print("  tools: " + ", ".join(get_tool_names()) + "\n")
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
                print(f"\n[!] Agent wants to run: {name}({args})")
                answer = input("    Approve? [yes/no]: ").strip().lower()
                to_send = "yes" if answer in ("yes", "y") else "no"
            else:
                _render(event)

        print()   # blank line between turns


if __name__ == "__main__":
    main()