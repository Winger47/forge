# tests/test_agent_depth.py — skills, approval modes, subagents (Phase 5).

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.approval import decide, ApprovalMode, RUN, PROMPT, DENY
from agent import skills as skills_mod
from agent.skills import discover_skills, load_skill, skills_catalogue
from agent import agent as A


# ── approval policy ──────────────────────────────────────────────────────────
def test_on_request_prompts_dangerous_runs_safe():
    assert decide(ApprovalMode.ON_REQUEST, "write_file", {"path": "a", "content": "x"}, False) == PROMPT
    assert decide(ApprovalMode.ON_REQUEST, "read_file", {"path": "a"}, False) == RUN


def test_auto_runs_dangerous_but_stops_at_real_hazard():
    assert decide(ApprovalMode.AUTO, "write_file", {"path": "a", "content": "x"}, False) == RUN
    # a dangerous shell command escalates even under auto (always-on scan)
    assert decide(ApprovalMode.AUTO, "run_shell", {"command": "rm -rf /"}, False) == PROMPT


def test_never_denies_dangerous():
    assert decide(ApprovalMode.NEVER, "write_file", {"path": "a", "content": "x"}, False) == DENY
    assert decide(ApprovalMode.NEVER, "read_file", {"path": "a"}, False) == RUN


def test_yolo_runs_everything_even_hazards():
    assert decide(ApprovalMode.YOLO, "run_shell", {"command": "rm -rf /"}, True) == RUN


def test_on_failure_runs_first_then_prompts_after_a_failure():
    # first attempt: no prior failure → runs unprompted
    assert decide(ApprovalMode.ON_FAILURE, "write_file", {"path": "a", "content": "x"}, False) == RUN
    # after this tool has failed once this run → require approval to retry
    assert decide(ApprovalMode.ON_FAILURE, "write_file", {"path": "a", "content": "x"}, True) == PROMPT


def test_always_on_path_escape_prompts():
    assert decide(ApprovalMode.ON_REQUEST, "write_file",
                  {"path": "../../etc/passwd", "content": "x"}, False) == PROMPT


# ── skills: on-demand load ───────────────────────────────────────────────────
def test_discovers_seed_skills():
    reg = discover_skills()
    assert {"code-review", "git-commit", "using-shell-safely"} <= set(reg)


def test_catalogue_lists_triggers_not_bodies():
    cat = skills_catalogue()
    assert "code-review" in cat
    assert "Correctness first" not in cat        # body is NOT in the catalogue


def test_load_skill_returns_body():
    out = load_skill("code-review")
    assert "SKILL: code-review" in out and "Correctness first" in out


def test_load_unknown_skill_is_error():
    assert load_skill("no-such-skill").startswith("ERROR")


def test_skill_load_enters_context_via_the_loop():
    # A run where the model calls load_skill, then answers. The skill body must
    # show up as a tool_result — i.e. it entered the model's context (behavior
    # change is grounded in the content now being available).
    class F:
        def __init__(self): self._n = 0
        def create_stream(self, messages, tools):
            self._n += 1
            if self._n == 1:
                frag = type("TF", (), {"index": 0, "id": "c1",
                    "function": type("Fn", (), {"name": "load_skill",
                        "arguments": '{"name": "code-review"}'})()})()
                delta = type("D", (), {"content": None, "tool_calls": [frag]})()
            else:
                delta = type("D", (), {"content": "reviewing now", "tool_calls": None})()
            return [type("CH", (), {"choices": [type("C", (), {"delta": delta})()],
                                    "usage": type("U", (), {"total_tokens": 5})()})()]
    events, to_send = [], None
    agent = A.run_agent([{"role": "user", "content": "review this"}], F())
    while True:
        try: events.append(agent.send(to_send))
        except StopIteration: break
        to_send = None
    results = [e for e in events if e.type == "tool_result"]
    assert results and "Correctness first" in results[0].data["content"]


# ── subagent: only the answer returns, isolated context ──────────────────────
class _FakeMsg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeTC:
    def __init__(self, name, arguments):
        self.id = "call_1"
        self.function = type("F", (), {"name": name, "arguments": arguments})()
    def model_dump(self):
        return {"id": self.id, "type": "function",
                "function": {"name": self.function.name, "arguments": self.function.arguments}}


class _FakeResp:
    def __init__(self, msg):
        self.choices = [type("C", (), {"message": msg})()]
        self.usage = type("U", (), {"total_tokens": 7})()


class _RawFake:
    """Non-streaming child client: returns (completion, cache) for create_raw."""
    def __init__(self, responses):
        self._responses = responses
        self._i = 0
    def create_raw(self, messages, tools):
        r = self._responses[self._i]; self._i += 1
        return r, "MISS"


def test_subagent_returns_only_its_answer(monkeypatch):
    # child: reads a file, then answers. read_file is READ → allowed under NEVER.
    responses = [
        _FakeResp(_FakeMsg(tool_calls=[_FakeTC("read_file",
                                               '{"path": "tests/test_agent.py"}')])),
        _FakeResp(_FakeMsg(content="ISOLATED_ANSWER: the file defines FakeClient.")),
    ]
    monkeypatch.setattr(A, "_make_subagent_client", lambda: _RawFake(responses))

    out = A.spawn_subagent("what does the test file define?")
    assert out.startswith("ISOLATED_ANSWER")
    # what returns is a single answer string — not the child's tool transcript
    assert "read_file" not in out
