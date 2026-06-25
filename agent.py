# agent.py — FORGE Phase 1

import os
import json
import subprocess
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()   # read .env so GROQ_API_KEY is available


# ─────────────────────────────────────────────
# EVENTS (the agent announces; it never prints)
# ─────────────────────────────────────────────
@dataclass
class Event:
    type: str          # "status" | "tool_call" | "tool_result" | "text" | "cost"
    data: dict[str, Any]


# ─────────────────────────────────────────────
# 1. THE AI CONNECTION (DirectClient)
# ─────────────────────────────────────────────
class DirectClient:
    def __init__(self):
        self.client = OpenAI(
            api_key=os.getenv("GROQ_API_KEY"),
            base_url="https://api.groq.com/openai/v1",
        )

    def create(self, messages, tools):
        response = self.client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            tools=tools,
            temperature=0,
        )
        return response


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


# Registry: look up a tool function by name.
TOOLS = {
    "read_file": read_file,
    "write_file": write_file,
    "run_shell": run_shell,
}

# Describe the tools to the AI. The {"type": "function", "function": {...}} wrapper is required.
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
def run_agent(goal, max_iterations=10, max_tokens=100):
    client = DirectClient()
    total_tokens = 0          # state for THIS run — local, not global
    messages = [
        {"role": "system", "content": (
            "You are an agent with access to tools: read_file, write_file, run_shell. "
            "When a task requires information about files or the system, you MUST call the "
            "appropriate tool to get real data. Do NOT guess or describe what a file might "
            "contain — call read_file and read it. Only give a final answer after you have "
            "used the tools you need."
        )},
        {"role": "user", "content": goal},
    ]

    for i in range(max_iterations):
        yield Event("status", {"phase": "iteration", "n": i})

        # --- GUARD: stop if we've already spent too much before starting another turn ---
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

        # --- ACT: record the assistant message WITH its tool_calls, then run each tool ---
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
        })

        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments)   # args arrive as a JSON STRING → parse to dict
            yield Event("tool_call", {"name": name, "args": args})

            try:
                result = TOOLS[name](**args)
            except Exception as e:
                result = f"ERROR: {type(e).__name__}: {e}"   # tool failure = data, not a crash
            yield Event("tool_result", {"name": name, "content": result})

            # --- LOOK: feed the result back so the AI sees it next turn ---
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    yield Event("status", {"phase": "aborted", "reason": "max iterations"})


# ─────────────────────────────────────────────
# 4. ENTRY POINT (the only thing that prints)
# ─────────────────────────────────────────────
def main():
    goal = input("goal> ")
    for event in run_agent(goal):
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


if __name__ == "__main__":
    main()