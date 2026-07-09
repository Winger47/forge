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