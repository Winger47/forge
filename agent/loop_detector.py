# loop_detector.py — cycle detection over the agent's actions (Phase 1).
#
# The iteration cap (max_iterations) is a blunt backstop: it stops a runaway loop
# eventually, but only after burning the whole budget. This is the sharp one — it
# notices the agent is STUCK (repeating itself with no new information) and aborts
# early, before the meter runs out.
#
# The key idea (FORGE.md §Phase-1 SDE-3 lens): the fingerprint includes the tool
# RESULT, not just the call. A tool called three times with three DIFFERENT
# results is making progress and must NOT be flagged. The same call returning the
# same result three times is a loop. Putting the result in the fingerprint is what
# separates "iterating" from "spinning".

import json
import hashlib
from collections import deque


class LoopDetector:
    """Fingerprints recent (tool, args, result) triples and reports when one
    recurs `threshold` times inside a sliding `window`.

    Catches two shapes the immediate-repeat guard in the loop misses:
      - a call that keeps returning the same thing across non-adjacent turns
      - a short cycle (A, B, A, B, A, B ...) where no two adjacent calls match
    """

    def __init__(self, threshold: int = 3, window: int = 12):
        self.threshold = threshold
        self._recent: deque[str] = deque(maxlen=window)

    @staticmethod
    def _fingerprint(tool: str, args: dict, result) -> str:
        # result is truncated: two huge outputs that agree on their first 500
        # chars are almost certainly the same read — and hashing megabytes per
        # turn is wasteful. args are sorted so key order can't disguise a repeat.
        payload = json.dumps(
            {"tool": tool, "args": args, "result": str(result)[:500]},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def record(self, tool: str, args: dict, result) -> bool:
        """Log one completed action. Returns True the moment this exact
        (tool, args, result) has occurred `threshold` times in the window —
        i.e. the agent is spinning and the run should abort."""
        fp = self._fingerprint(tool, args, result)
        self._recent.append(fp)
        return self._recent.count(fp) >= self.threshold
