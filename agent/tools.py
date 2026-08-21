# tools.py — extensible tool system with auto-registration + auto-generated schemas

import inspect
import subprocess
import importlib.util
from enum import Enum
from pathlib import Path

import jsonschema


# ─────────────────────────────────────────────
# ToolKind — what a tool TOUCHES, tagged at the door (Phase 2 trust boundary).
# The classification is declared here; Phase 5 maps it to an approval policy.
# Keeping it separate from the `dangerous` flag means "what it does" and "does it
# need a yes" stay independent knobs.
# ─────────────────────────────────────────────
class ToolKind(str, Enum):
    READ = "read"
    WRITE = "write"
    SHELL = "shell"
    NETWORK = "network"
    MEMORY = "memory"
    MCP = "mcp"
    META = "meta"          # self-directed tools (calculate, finish, …)


# ─────────────────────────────────────────────
# THE REGISTRY — one place that holds every tool
# (replaces the old TOOLS dict + TOOL_SCHEMAS list + DANGEROUS_TOOLS set)
# ─────────────────────────────────────────────
TOOL_REGISTRY = {}      # name → {"func", "schema", "dangerous", "kind"}


# maps Python type hints → JSON schema types (what the LLM API expects)
_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def tool(dangerous: bool = False, kind: ToolKind = ToolKind.META):
    """Decorator that registers a function as an agent tool and auto-builds its
    JSON schema from the signature and docstring.

        @tool(kind=ToolKind.READ)                       # safe read
        def read_file(path: str):
            '''Read a file and return its contents.'''
            ...

        @tool(dangerous=True, kind=ToolKind.WRITE)      # needs human approval
        def write_file(path: str, content: str):
            '''Write content to a file.'''
            ...
    """
    def decorator(func):
        name = func.__name__
        description = (func.__doc__ or "").strip()

        # --- introspect the parameters to build the schema automatically ---
        sig = inspect.signature(func)
        properties = {}
        required = []
        for param_name, param in sig.parameters.items():
            json_type = _TYPE_MAP.get(param.annotation, "string")   # default to string if unhinted
            properties[param_name] = {"type": json_type}
            if param.default is inspect.Parameter.empty:            # no default → required
                required.append(param_name)

        schema = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    # untrusted callers (the model, user code) don't get to invent
                    # extra params — the schema is the contract, validated below.
                    "additionalProperties": False,
                },
            },
        }

        TOOL_REGISTRY[name] = {
            "func": func, "schema": schema, "dangerous": dangerous, "kind": kind,
        }
        return func      # return the function unchanged — it still works when called normally

    return decorator


# ─────────────────────────────────────────────
# THE TOOLS — each is now ONE decorated block
# ─────────────────────────────────────────────
@tool(kind=ToolKind.READ)
def read_file(path: str):
    """Read a file and return its contents."""
    with open(path, "r") as f:
        return f.read()


@tool(dangerous=True, kind=ToolKind.WRITE)
def write_file(path: str, content: str):
    """Write content to a file."""
    with open(path, "w") as f:
        f.write(content)
    return f"File '{path}' written successfully."


@tool(dangerous=True, kind=ToolKind.SHELL)
def run_shell(command: str):
    """Run a shell command and return its output."""
    result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
    return (result.stdout + result.stderr).strip() or "(no output)"


# ─────────────────────────────────────────────
# HELPERS — what the agent loop calls
# ─────────────────────────────────────────────
def get_schemas(allowlist=None):
    """Tool schemas to send to the LLM. With `allowlist` (a set/list of names),
    only those tools are exposed — the filter that lets config or a subagent
    restrict which tools a run may see."""
    return [
        entry["schema"]
        for name, entry in TOOL_REGISTRY.items()
        if allowlist is None or name in allowlist
    ]


def get_tool(name):
    """Look up a tool's function by name."""
    return TOOL_REGISTRY[name]["func"]


def is_dangerous(name):
    """Does this tool require human approval?"""
    return TOOL_REGISTRY.get(name, {}).get("dangerous", False)


def get_tool_names():
    """All registered tool names, for the system prompt."""
    return list(TOOL_REGISTRY.keys())


def get_kind(name):
    """The ToolKind of a registered tool (META if unknown)."""
    entry = TOOL_REGISTRY.get(name)
    return entry["kind"] if entry else ToolKind.META


def validate_call(name, args: dict):
    """Check a tool call's arguments against the tool's JSON schema BEFORE running
    it. Returns (ok, error): ok=True and error=None when valid; ok=False and a
    human-readable message when not. This is the machine-checkable boundary
    between untrusted input (the model / user code) and the executor — a
    malformed call becomes an observation the model can correct, never a crash.
    """
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        return False, f"unknown tool '{name}'"
    params_schema = entry["schema"]["function"]["parameters"]
    try:
        jsonschema.validate(instance=args, schema=params_schema)
    except jsonschema.ValidationError as e:
        return False, f"invalid arguments for {name}: {e.message}"
    return True, None


USER_TOOLS_DIR = Path.home() / ".forge" / "tools"


def load_user_tools(tools_dir: Path = None) -> list[str]:
    """Import every *.py under ~/.forge/tools/ so their @tool functions register.

    Two trust-boundary rules (Phase 2):
      - FAIL-LOAD ISOLATION: a user tool that raises on import is quarantined —
        anything it half-registered is rolled back and the file is skipped. One
        bad extension never takes down startup.
      - NO SILENT SHADOWING: a user tool whose name collides with an already
        registered tool is REJECTED (the original is kept), loudly. A user
        `read_file` must not quietly replace the builtin.

    Returns human-readable messages (loaded / skipped / rejected) for the caller
    to print — this module itself never prints (print discipline lives in the UI).
    """
    tools_dir = USER_TOOLS_DIR if tools_dir is None else tools_dir
    messages: list[str] = []
    if not tools_dir.is_dir():
        return messages

    for py in sorted(tools_dir.glob("*.py")):
        if py.name.startswith("_"):
            continue
        before = dict(TOOL_REGISTRY)          # snapshot to detect adds + shadows
        try:
            spec = importlib.util.spec_from_file_location(f"forge_user_{py.stem}", py)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:  # noqa: BLE001 — quarantine, don't crash startup
            TOOL_REGISTRY.clear()
            TOOL_REGISTRY.update(before)       # roll back partial registration
            messages.append(f"user tool '{py.name}' failed to load, skipped: "
                            f"{type(e).__name__}: {e}")
            continue

        added = []
        for name, entry in list(TOOL_REGISTRY.items()):
            if name in before and entry["func"] is not before[name]["func"]:
                TOOL_REGISTRY[name] = before[name]     # reject the shadow, keep original
                messages.append(f"user tool '{py.name}': '{name}' shadows an existing "
                                f"tool — rejected")
            elif name not in before:
                added.append(name)
        if added:
            messages.append(f"user tools loaded from '{py.name}': {', '.join(added)}")

    return messages


@tool(kind=ToolKind.READ)
def list_files(path: str):
    """List the files and folders at a given path (e.g. '.' for current directory)."""
    import os
    return "\n".join(os.listdir(path))

@tool(kind=ToolKind.READ)
def search_files(directory: str, keyword: str):
    """Search for a keyword in all text files under a directory. Returns matching lines with file path and line number."""
    import os
    matches = []
    for root, _, files in os.walk(directory):
        # skip noise directories
        if any(skip in root for skip in (".git", "venv", "__pycache__", ".pytest_cache", "node_modules")):
            continue
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    for lineno, line in enumerate(f, 1):
                        if keyword in line:
                            matches.append(f"{fpath}:{lineno}: {line.strip()}")
            except (UnicodeDecodeError, PermissionError, IsADirectoryError):
                continue   # skip binary/unreadable files, don't crash
    if not matches:
        return f"No matches for '{keyword}' in {directory}."
    return "\n".join(matches[:50])   # cap output so we don't flood the model
@tool(kind=ToolKind.READ)
def current_time():
    """Return the current date and time."""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
@tool(kind=ToolKind.META)
def calculate(expression: str):
    """Evaluate a basic arithmetic expression, e.g. '2 + 3 * 4' or '(10 - 2) / 4'."""
    import ast, operator
    ops = {
        ast.Add: operator.add, ast.Sub: operator.sub,
        ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg,
    }
    def _eval(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BinOp):
            return ops[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            return ops[type(node.op)](_eval(node.operand))
        raise ValueError("unsupported expression")
    try:
        return str(_eval(ast.parse(expression, mode="eval").body))
    except Exception as e:
        return f"ERROR: could not evaluate '{expression}': {e}"

@tool(dangerous=True, kind=ToolKind.WRITE)
def edit_file(path: str, old_text: str, new_text: str):
    """Replace an exact snippet of text in a file with new text. Use this for targeted edits instead of rewriting the whole file. old_text must appear exactly once."""
    with open(path, "r") as f:
        content = f.read()
    count = content.count(old_text)
    if count == 0:
        return f"ERROR: '{old_text[:50]}...' not found in {path}. No changes made."
    if count > 1:
        return f"ERROR: '{old_text[:50]}...' appears {count} times in {path}. Must be unique. No changes made."
    with open(path, "w") as f:
        f.write(content.replace(old_text, new_text))
    return f"Replaced text in {path} successfully."