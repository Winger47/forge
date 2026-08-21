# tests/test_loop_detector.py — cycle detection (Phase 1).
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.loop_detector import LoopDetector


def test_repeated_identical_action_trips_at_threshold():
    d = LoopDetector(threshold=3)
    assert d.record("read_file", {"path": "a"}, "SAME") is False
    assert d.record("read_file", {"path": "a"}, "SAME") is False
    assert d.record("read_file", {"path": "a"}, "SAME") is True   # 3rd = stuck


def test_same_call_different_result_is_progress_never_trips():
    # The result is IN the fingerprint, so a call that returns new info each time
    # is iterating, not spinning — it must never be flagged.
    d = LoopDetector(threshold=3)
    tripped = any(
        d.record("list_files", {"path": "."}, f"result-{i}") for i in range(10)
    )
    assert tripped is False


def test_alternating_two_step_cycle_trips():
    # A, B, A, B, A, B — no two ADJACENT calls match, so the loop's simple
    # last-call guard can't catch it; the detector still does.
    d = LoopDetector(threshold=3)
    seq = [("A", {}, "ra"), ("B", {}, "rb")] * 3
    assert any(d.record(*step) for step in seq)


def test_arg_order_does_not_disguise_a_repeat():
    d = LoopDetector(threshold=2)
    assert d.record("t", {"x": 1, "y": 2}, "r") is False
    assert d.record("t", {"y": 2, "x": 1}, "r") is True   # same call, keys reordered
