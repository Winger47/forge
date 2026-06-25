# agent.py — FORGE Phase 0

import os
import json
import subprocess
from openai import OpenAI


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
        response = self.client.chat.completions.create(   # FIX: chat.completions, not models.completions
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

# Describe the tools to the AI. Note the {"type": "function", "function": {...}} wrapper — it's required.
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
def run_agent(goal, max_iterations=10):
    client = DirectClient()
    messages = [
        {"role": "system", "content": "You are an agent with access to tools: read_file, write_file, run_shell. "
        "When a task requires information about files or the system, you MUST call the "
        "appropriate tool to get real data. Do NOT guess or describe what a file might "
        "contain — call read_file and read it. Only give a final answer after you have "
        "used the tools you need."},
        {"role": "user", "content": goal},
    ]

    for i in range(max_iterations):
        print(f"\n[iteration {i}]")

        # --- THINK ---
        response = client.create(messages, TOOL_SCHEMAS)
        msg = response.choices[0].message

        # --- DONE? no tool call means the AI is finished ---
        if not msg.tool_calls:
            print(f"\n{msg.content}")
            return

        # --- ACT: add the assistant message WITH its tool_calls, then run each tool ---
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
        })

        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments)   # args arrive as a JSON STRING → parse to dict
            print(f"  → {name}({args})")

            try:
                result = TOOLS[name](**args)           # look up + run the tool
            except Exception as e:
                result = f"ERROR: {type(e).__name__}: {e}"   # tool failure = data, not a crash
            print(f"  ← {result[:200]}")

            # --- LOOK: feed the result back so the AI sees it next turn ---
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,                 # must match the call's id
                "content": result,
            })

    print("[stopped: max iterations reached]")


# ─────────────────────────────────────────────
# 4. ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    goal = input("goal> ")
    run_agent(goal)