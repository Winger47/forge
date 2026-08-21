# approval.py — the approval policy engine (Phase 5).
#
# Decouples "what is allowed" (policy) from "how it runs" (the loop). The loop
# asks decide(...) and gets back one of three verdicts — it never encodes the
# policy itself. That separation is the whole point: adding a mode changes this
# file, not the agent.
#
#   RUN    — execute without asking
#   PROMPT — ask the human (yield confirm_request)
#   DENY   — refuse without asking, return an observation
#
# Modes (over a tool's ToolKind / dangerous flag):
#   on-request  — the default: dangerous tools PROMPT, safe tools RUN
#   on-failure  — dangerous tools RUN once; if that action already FAILED this
#                 run, it PROMPTs before retrying (the reference impl's mode)
#   auto        — dangerous tools RUN (trusted automation)
#   never       — dangerous tools are DENIED outright (read-only sessions)
#   yolo        — everything RUNs, and even the always-on safety scan is skipped
#
# ALWAYS-ON (every mode except yolo): a dangerous-command / path-escape scan can
# escalate a RUN to a PROMPT (or DENY under `never`) even when the mode wouldn't
# otherwise stop it — defense in depth, so `auto` can't silently `rm -rf /`.

import re
from enum import Enum

from agent.tools import get_kind, is_dangerous, ToolKind

RUN, PROMPT, DENY = "run", "prompt", "deny"


class ApprovalMode(str, Enum):
    ON_REQUEST = "on-request"
    ON_FAILURE = "on-failure"
    AUTO = "auto"
    NEVER = "never"
    YOLO = "yolo"


# Patterns that are dangerous regardless of the tool's declared flag. Matched
# against any string argument (shell command, path, ...).
_DANGEROUS_PATTERNS = [
    r"\brm\s+-[a-z]*[rf]",          # rm -rf / rm -f
    r"\bgit\s+reset\s+--hard",
    r"\bgit\s+clean\s+-[a-z]*f",
    r"\bmkfs\b", r"\bdd\s+if=", r":\(\)\s*\{",   # fork bomb
    r"\bsudo\b",
    r">\s*/dev/sd", r"\bchmod\s+-R\s+777",
    r"\bcurl\b.*\|\s*(sh|bash)", r"\bwget\b.*\|\s*(sh|bash)",
]


def _touches_dangerous_command(args: dict) -> bool:
    for v in args.values():
        if isinstance(v, str) and any(re.search(p, v) for p in _DANGEROUS_PATTERNS):
            return True
    return False


def _touches_path_escape(args: dict) -> bool:
    """A path arg that climbs out of the project (../) or is absolute to a
    sensitive root. Conservative — false positives PROMPT, they don't DENY."""
    for key in ("path", "file", "directory", "filename"):
        v = args.get(key)
        if isinstance(v, str):
            if ".." in v.split("/"):
                return True
            if v.startswith(("/etc", "/usr", "/bin", "/sys", "/System", "~/..")):
                return True
    return False


def decide(mode: ApprovalMode, name: str, args: dict, failed_before: bool) -> str:
    """Return RUN / PROMPT / DENY for a single tool call."""
    if mode == ApprovalMode.YOLO:
        return RUN                                   # trust everything, skip scans

    # always-on safety scan — can escalate below, never de-escalates
    scanned_dangerous = _touches_dangerous_command(args) or _touches_path_escape(args)

    dangerous = is_dangerous(name) or get_kind(name) in (ToolKind.WRITE, ToolKind.SHELL)

    if not dangerous and not scanned_dangerous:
        return RUN                                   # safe read/meta tool, clean args

    if mode == ApprovalMode.NEVER:
        return DENY
    if mode == ApprovalMode.AUTO:
        return PROMPT if scanned_dangerous else RUN  # auto still stops at a real hazard
    if mode == ApprovalMode.ON_FAILURE:
        # run freely until it fails; after a failure, require a human to retry
        if scanned_dangerous:
            return PROMPT
        return PROMPT if failed_before else RUN
    # ON_REQUEST (default): ask for anything dangerous
    return PROMPT
