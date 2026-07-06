# tests/test_agent.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))   # let tests import the agent package

from agent.agent import run_agent, Event


# ─────────────────────────────────────────────
# TEST DOUBLES (fakes that mimic the OpenAI SDK's response shape)
# ─────────────────────────────────────────────
class FakeMessage:
    """Mimics response.choices[0].message"""
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls          # None = final answer; list = tool request


class FakeToolCall:
    """Mimics a single tool_call object from the SDK."""
    def __init__(self, name, arguments, call_id="call_1"):
        self.id = call_id
        self.function = type("F", (), {"name": name, "arguments": arguments})()

    def model_dump(self):
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.function.name, "arguments": self.function.arguments},
        }


class FakeResponse:
    """Mimics the full chat.completions response object."""
    def __init__(self, message):
        self.choices = [type("C", (), {"message": message})()]
        self.usage = type("U", (), {"total_tokens": 10})()


class FakeClient:
    """Returns canned responses in order. No network, no cost, deterministic.
    This is why the LLMClient interface (Phase 2) mattered — it lets us inject this."""
    def __init__(self, responses):
        self._responses = responses
        self._i = 0

    def create(self, messages, tools):
        response = self._responses[self._i]
        self._i += 1
        return response


# ─────────────────────────────────────────────
# TESTS
# ─────────────────────────────────────────────
def test_agent_returns_final_answer_with_no_tool_call():
    # ARRANGE: model replies with text, no tool call → agent should finish immediately
    fake = FakeClient([FakeResponse(FakeMessage(content="The answer is 42."))])

    # ACT
    events = list(run_agent("what is the answer", fake))

    # ASSERT: exactly one text (final answer) event, containing the answer
    text_events = [e for e in events if e.type == "text"]
    assert len(text_events) == 1
    assert "42" in text_events[0].data["content"]


def test_agent_runs_a_tool_when_requested():
    # ARRANGE: two responses —
    #   1) a tool call (read this test file), then
    #   2) a final answer (no tool call → loop ends)
    fake = FakeClient([
        FakeResponse(FakeMessage(
            content=None,
            tool_calls=[FakeToolCall("read_file", '{"path": "tests/test_agent.py"}')],
        )),
        FakeResponse(FakeMessage(content="I read the file.")),
    ])

    # ACT
    events = list(run_agent("read the test file", fake))

    # ASSERT: a tool_call event fired for read_file, and the tool actually ran (tool_result)
    tool_calls = [e for e in events if e.type == "tool_call"]
    tool_results = [e for e in events if e.type == "tool_result"]

    assert len(tool_calls) == 1
    assert tool_calls[0].data["name"] == "read_file"
    assert len(tool_results) == 1
    # the file exists, so the result should NOT be an error
    assert not tool_results[0].data["content"].startswith("ERROR")


def test_agent_stops_at_max_iterations():
    # ARRANGE: model ALWAYS requests a tool → never finishes → must hit the guard.
    # A client that returns a tool-call response every single time.
    class AlwaysToolClient:
        def create(self, messages, tools):
            return FakeResponse(FakeMessage(
                content=None,
                tool_calls=[FakeToolCall("read_file", '{"path": "tests/test_agent.py"}')],
            ))

    # ACT: cap iterations low so the test is fast
    events = list(run_agent("loop forever", AlwaysToolClient(), max_iterations=3))

    # ASSERT: the loop aborted on max iterations
    aborts = [e for e in events
              if e.type == "status" and e.data.get("reason") == "max iterations"]
    assert len(aborts) == 1


def test_tool_failure_becomes_an_observation_not_a_crash():
    # ARRANGE: model asks to read a file that does NOT exist → tool raises →
    #          the loop must catch it and return an ERROR observation, then finish.
    fake = FakeClient([
        FakeResponse(FakeMessage(
            content=None,
            tool_calls=[FakeToolCall("read_file", '{"path": "does_not_exist_xyz.txt"}')],
        )),
        FakeResponse(FakeMessage(content="I could not read it.")),
    ])

    # ACT
    events = list(run_agent("read a missing file", fake))

    # ASSERT: a tool_result came back, and it's an ERROR string (not a crash)
    tool_results = [e for e in events if e.type == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0].data["content"].startswith("ERROR")