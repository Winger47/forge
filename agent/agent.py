# agent.py — FORGE

import os
import json
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Protocol

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
    """Same interface as DirectClient — but calls OUR gateway, not Groq directly."""
    def __init__(self):
        self.client = OpenAI(
            api_key="not-needed-yet",
            base_url="http://127.0.0.1:8000/v1",
        )

    def create(self, messages, tools):
        return self.client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            tools=tools,
            temperature=0,
        )


# ─────────────────────────────────────────────
# 2. THE TOOLS (the agent's hands)
# ─────────────────────────────────────────────
def read_file(path):
    with open(path, "r") as f:
        return f.read()

def write_file(path, content):
    with open(path, "w") as f:
        f.write(content)
    return f"File '{path}' written successfully."

def run_shell(command):
    result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
    return (result.stdout + result.stderr).strip() or "(no output)"


DANGEROUS_TOOLS = {"run_shell", "write_file"}   # tools that can cause harm → need approval

TOOLS = {
    "read_file": read_file,
    "write_file": write_file,
    "run_shell": run_shell,
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file and return its contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to read."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file."},
                    "content": {"type": "string", "description": "Content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command and return its output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."},
                },
                "required": ["command"],
            },
        },
    },
]


# ─────────────────────────────────────────────
# 3. THE LOOP (think → act → look → repeat)
# ─────────────────────────────────────────────
def run_agent(goal, client: LLMClient, max_iterations=10, max_tokens=50000):
    total_tokens = 0
    last_call = None
    rejected_streak = 0          # ← Part A: initialize HERE, with the other loop state
    messages = [
        {"role": "system", "content": (
            "You are an agent with access to tools: read_file, write_file, run_shell. "
            "When a task requires information about files or the system, you MUST call the "
            "appropriate tool to get real data. Do NOT guess or describe what a file might "
            "contain — call read_file and read it. Only give a final answer after you have "
            "used the tools you need. "
            "If a tool fails, is denied, or returns empty output, do NOT retry the same "
            "command. Either try a different approach or give your final answer, explaining "
            "any limitation."
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
        response = client.create(messages, TOOL_SCHEMAS)
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
                rejected_streak += 1                          # ← Part B: increment HERE
                if rejected_streak >= 2:                      # stuck → force-terminate the whole run
                    yield Event("status", {"phase": "aborted", "reason": "stuck: repeated rejected call"})
                    return
                result = ("You already requested this exact action and it was handled "
                          "(run or denied). Do NOT request it again. Give your final answer.")
                yield Event("tool_result", {"name": name, "content": result})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                last_call = call_signature
                continue

            rejected_streak = 0        # ← Part B: reset when the model does something NEW

            # --- THEN permission gate for dangerous tools ---
            if name in DANGEROUS_TOOLS:
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
                result = TOOLS[name](**args)
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
    client = GatewayClient()

    agent = run_agent(goal, client)     # the generator (not started yet)
    to_send = None                      # what we pass back on the next .send()

    while True:
        try:
            event = agent.send(to_send)   # advance; first send MUST be None (generator not started)
        except StopIteration:
            break                          # generator finished → done

        to_send = None                     # reset; only set it when answering a confirm

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


if __name__ == "__main__":
    main()