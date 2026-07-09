# tools.py — extensible tool system with auto-registration + auto-generated schemas

import inspect
import subprocess


# ─────────────────────────────────────────────
# THE REGISTRY — one place that holds every tool
# (replaces the old TOOLS dict + TOOL_SCHEMAS list + DANGEROUS_TOOLS set)
# ─────────────────────────────────────────────
TOOL_REGISTRY = {}      # name → {"func": callable, "schema": dict, "dangerous": bool}


# maps Python type hints → JSON schema types (what the LLM API expects)
_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def tool(dangerous: bool = False):
    """Decorator that registers a function as an agent tool and auto-builds its
    JSON schema from the signature and docstring.

        @tool()                  # safe tool
        def read_file(path: str):
            '''Read a file and return its contents.'''
            ...

        @tool(dangerous=True)    # needs human approval
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
                },
            },
        }

        TOOL_REGISTRY[name] = {"func": func, "schema": schema, "dangerous": dangerous}
        return func      # return the function unchanged — it still works when called normally

    return decorator


# ─────────────────────────────────────────────
# THE TOOLS — each is now ONE decorated block
# ─────────────────────────────────────────────
@tool()
def read_file(path: str):
    """Read a file and return its contents."""
    with open(path, "r") as f:
        return f.read()


@tool(dangerous=True)
def write_file(path: str, content: str):
    """Write content to a file."""
    with open(path, "w") as f:
        f.write(content)
    return f"File '{path}' written successfully."


@tool(dangerous=True)
def run_shell(command: str):
    """Run a shell command and return its output."""
    result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
    return (result.stdout + result.stderr).strip() or "(no output)"


# ─────────────────────────────────────────────
# HELPERS — what the agent loop calls
# ─────────────────────────────────────────────
def get_schemas():
    """All tool schemas, to send to the LLM."""
    return [entry["schema"] for entry in TOOL_REGISTRY.values()]


def get_tool(name):
    """Look up a tool's function by name."""
    return TOOL_REGISTRY[name]["func"]


def is_dangerous(name):
    """Does this tool require human approval?"""
    return TOOL_REGISTRY.get(name, {}).get("dangerous", False)


def get_tool_names():
    """All registered tool names, for the system prompt."""
    return list(TOOL_REGISTRY.keys())
@tool()
def list_files(path: str):
    """List the files and folders at a given path (e.g. '.' for current directory)."""
    import os
    return "\n".join(os.listdir(path))

@tool()
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
@tool()
def current_time():
    """Return the current date and time."""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
@tool()
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

@tool(dangerous=True)
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