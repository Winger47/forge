# golden_tasks.py — the seed eval set (Phase 1).
#
# Three end-to-end tasks with DETERMINISTIC checks. An LLM's prose is not
# byte-stable, so a golden task never asserts on the exact transcript — it asserts
# on a SIDE EFFECT the agent had to produce to succeed (a file exists, with the
# right content). That is the whole trick to testing a non-deterministic system:
# check the outcome, not the wording.
#
# This is the smoke test. Phase 9 grows it into a regression suite + LLM-as-judge;
# the shape (setup -> run goal -> check side effect) stays the same.

from pathlib import Path


def _setup_none(workdir: Path) -> None:
    """No fixture files needed."""


def _setup_notes(workdir: Path) -> None:
    (workdir / "notes.txt").write_text(
        "buy milk\n"
        "TODO: fix the login bug\n"
        "call the dentist\n"
        "TODO: write the release notes\n"
        "read a book\n",
        encoding="utf-8",
    )


def _check_greeting(workdir: Path) -> tuple[bool, str]:
    p = workdir / "greeting.txt"
    if not p.exists():
        return False, "greeting.txt was not created"
    body = p.read_text(encoding="utf-8")
    if "Hello, FORGE!" not in body:
        return False, f"greeting.txt missing expected text (got: {body!r:.80})"
    return True, "greeting.txt contains 'Hello, FORGE!'"


def _check_todos(workdir: Path) -> tuple[bool, str]:
    p = workdir / "todos.md"
    if not p.exists():
        return False, "todos.md was not created"
    body = p.read_text(encoding="utf-8")
    hits = sum(kw in body for kw in ("login bug", "release notes"))
    if hits < 2:
        return False, f"todos.md captured {hits}/2 TODO items"
    return True, "todos.md lists both TODO items"


def _check_answer(workdir: Path) -> tuple[bool, str]:
    p = workdir / "answer.txt"
    if not p.exists():
        return False, "answer.txt was not created"
    body = p.read_text(encoding="utf-8")
    if "391" not in body:
        return False, f"answer.txt does not contain 391 (got: {body!r:.40})"
    return True, "answer.txt contains 391"


# Each task: a stable name, the goal string handed to the agent, a setup that
# seeds any fixture files, and a check over the resulting working directory.
GOLDEN_TASKS = [
    {
        "name": "write-file",
        "goal": "Create a file named greeting.txt whose contents are exactly: Hello, FORGE!",
        "setup": _setup_none,
        "check": _check_greeting,
    },
    {
        "name": "read-transform",
        "goal": "Read notes.txt and write a file called todos.md that lists every "
                "line containing the word TODO.",
        "setup": _setup_notes,
        "check": _check_todos,
    },
    {
        "name": "compute-write",
        "goal": "Calculate 17 * 23 and write only the numeric result to a file named answer.txt.",
        "setup": _setup_none,
        "check": _check_answer,
    },
]
