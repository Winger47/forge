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
    def create_stream(self, messages, tools):
        resp = self._responses[self._i]
        self._i += 1
        m = resp.choices[0].message
        tool_frags = None
        if m.tool_calls:
            tool_frags = [
                type("TF", (), {
                    "index": j,
                    "id": tc.id,
                    "function": type("F", (), {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    })(),
                })()
                for j, tc in enumerate(m.tool_calls)
            ]
        delta = type("D", (), {"content": m.content, "tool_calls": tool_frags})()
        chunk = type("CH", (), {
            "choices": [type("C", (), {"delta": delta})()],
            "usage": type("U", (), {"total_tokens": 10})(),
        })()
        return [chunk]


# helper: build a messages list from a goal string (run_agent now takes messages, not a goal)
def make_messages(goal):
    return [{"role": "user", "content": goal}]


# ─────────────────────────────────────────────
# TESTS
# ─────────────────────────────────────────────
def test_agent_returns_final_answer_with_no_tool_call():
    fake = FakeClient([FakeResponse(FakeMessage(content="The answer is 42."))])
    messages = make_messages("what is the answer")
    list(run_agent(messages, fake))
    # the streamed answer is appended to the conversation
    assert messages[-1]["role"] == "assistant"
    assert "42" in messages[-1]["content"]


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
    events = list(run_agent(make_messages("read the test file"), fake))

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
        def __init__(self):
            self._n = 0
        def create_stream(self, messages, tools):
            self._n += 1
            frag = type("TF", (), {
                "index": 0, "id": "call_1",
                "function": type("F", (), {
                    "name": "read_file",
                    "arguments": f'{{"path": "file_{self._n}.txt"}}',
                })(),
            })()
            delta = type("D", (), {"content": None, "tool_calls": [frag]})()
            chunk = type("CH", (), {
                "choices": [type("C", (), {"delta": delta})()],
                "usage": type("U", (), {"total_tokens": 10})(),
            })()
            return [chunk]

    # ACT: cap iterations low so the test is fast
    events = list(run_agent(make_messages("loop forever"), AlwaysToolClient(), max_iterations=3))

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
    events = list(run_agent(make_messages("read a missing file"), fake))

    # ASSERT: a tool_result came back, and it's an ERROR string (not a crash)
    tool_results = [e for e in events if e.type == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0].data["content"].startswith("ERROR")