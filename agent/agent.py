

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
        # forward the bypass header only when regenerating
        extra_headers = {"Cache-Control": "no-cache"} if self.bypass_cache else {}
        return self.client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            tools=tools,
            temperature=0,
            extra_headers=extra_headers,   # OpenAI SDK forwards custom headers to the gateway
        )


# ─────────────────────────────────────────────
# 2. TOOLS — defined once in tools.py via @tool() decorator
#    agent.py uses: get_schemas(), get_tool(name), is_dangerous(name)
# ─────────────────────────────────────────────


# ─────────────────────────────────────────────
# 3. THE LOOP (think → act → look → repeat)
# ─────────────────────────────────────────────
def run_agent(goal, client: LLMClient, max_iterations=10, max_tokens=50000):
    total_tokens = 0
    last_call = None
    rejected_streak = 0          # ← Part A: initialize HERE, with the other loop state
    tool_names = ", ".join(get_tool_names())
    messages = [
        {"role": "system", "content": (
            f"You are a task-completing agent with access to these tools: {tool_names}.\n\n"
            "HOW TO WORK:\n"
            "- When a task needs real data about files or the system, call the appropriate "
            "tool to get it. Do NOT guess or make up file contents — use a tool.\n"
            "- After a tool returns its result, READ the result and decide: do you now have "
            "enough to answer the user's goal?\n"
            "- If YES: stop calling tools and write your final answer to the user directly, "
            "using the information the tool gave you. Do NOT call the same tool again.\n"
            "- If NO: call another tool to get what's still missing.\n\n"
            "IMPORTANT: Once you have the information you need, you MUST give a final text "
            "answer instead of calling more tools. Repeating a tool you already ran will not "
            "give new information. If a tool fails or is denied, do not retry it — either try "
            "a different approach or give your best answer explaining the limitation."
        )},
        {"role": "user", "content": goal},
    ]

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
            args = json.loads(tc.function.arguments)
            call_signature = f"{name}:{json.dumps(args, sort_keys=True)}"

            # --- GUARD FIRST: catch repeats BEFORE bothering the human ---
            if call_signature == last_call:
                messages.append({
                    "role": "user",
                    "content": (
                        "You already have the tool result above. Do not call any more tools. "
                        "Write your final answer to the original goal now, in plain text, "
                        "using the information you already have."
                    ),
                })
                forced = client.create(messages, [])
                total_tokens += forced.usage.total_tokens
                yield Event("cost", {"total_tokens": total_tokens})
                yield Event("text", {"content": forced.choices[0].message.content})
                return

            rejected_streak = 0        # ← Part B: reset when the model does something NEW

            # --- THEN permission gate for dangerous tools ---
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
                result = f"ERROR: {type(e).__name__}: {e}"
            yield Event("tool_result", {"name": name, "content": result})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    yield Event("status", {"phase": "aborted", "reason": "max iterations"})
# ─────────────────────────────────────────────
# 4. ENTRY POINT (drives the generator with .send() so it can answer confirm requests)
# ─────────────────────────────────────────────
def main():
    goal = input("goal> ")
    bypass = False                          # first run uses the cache normally

    while True:                             # OUTER loop = re-run on regenerate
        client = GatewayClient(bypass_cache=bypass)
        agent = run_agent(goal, client)
        to_send = None

        while True:                         # INNER loop = drive the agent generator
            try:
                event = agent.send(to_send)
            except StopIteration:
                break
            to_send = None

            if event.type == "status":
                reason = event.data.get("reason", "")
                print(f"[status: {event.data['phase']} {event.data.get('n', '')} {reason}]".rstrip())
            elif event.type == "tool_call":
                print(f"  → {event.data['name']}({event.data['args']})")
            elif event.type == "tool_result":
                print(f"  ← {event.data['content'][:200]}")
            elif event.type == "cost":
                print(f"  [tokens so far: {event.data['total_tokens']}]")
            elif event.type == "text":
                print(f"\n{event.data['content']}")
            elif event.type == "confirm_request":
                name = event.data["name"]
                args = event.data["args"]
                print(f"\n⚠️  Agent wants to run: {name}({args})")
                answer = input("    Approve? [yes/no]: ").strip().lower()
                to_send = "yes" if answer in ("yes", "y") else "no"

        # --- after the agent finishes: ask if the user is satisfied ---
        choice = input("\nSatisfied? [enter = yes / r = regenerate]: ").strip().lower()
        if choice != "r":
            break                           # user is happy → exit
        bypass = True                       # next run bypasses the cache → fresh answer
        print("\nregenerating (fresh answer)...\n")

if __name__ == "__main__":
    main()