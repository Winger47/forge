# circuit_breaker.py — stop hammering a dead provider (Phase 4).
#
# A three-state machine, per provider/model key:
#
#     CLOSED  ── failures reach threshold ──►  OPEN
#       ▲                                        │
#       │ trial succeeds              cooldown elapsed
#       │                                        ▼
#     (reset) ◄──────────────────────────  HALF_OPEN ── trial fails ──► OPEN
#
# CLOSED   = healthy; calls flow.
# OPEN     = tripped; calls are refused instantly (no wasted round-trip) until a
#            cooldown passes — this is the point of the pattern: fail FAST instead
#            of piling requests onto something that's already down.
# HALF_OPEN = one probe allowed; success closes it, failure re-opens it.
#
# Kept dependency-free and clock-injectable so the state transitions are unit
# testable without sleeping (mirrors rate_limit.py's compute_bucket).

import time
from dataclasses import dataclass, field

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


@dataclass
class _State:
    status: str = CLOSED
    failures: int = 0
    opened_at: float = 0.0


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3       # consecutive failures that trip the breaker
    cooldown: float = 15.0           # seconds OPEN before a HALF_OPEN probe
    _states: dict = field(default_factory=dict)

    def _get(self, key: str) -> _State:
        return self._states.setdefault(key, _State())

    def allow(self, key: str, now: float | None = None) -> bool:
        """May a call to `key` proceed right now?"""
        now = time.monotonic() if now is None else now
        st = self._get(key)
        if st.status == OPEN:
            if now - st.opened_at >= self.cooldown:
                st.status = HALF_OPEN          # let exactly one probe through
                return True
            return False                       # still cooling down → fail fast
        return True                            # CLOSED or HALF_OPEN

    def record_success(self, key: str) -> None:
        st = self._get(key)
        st.status = CLOSED
        st.failures = 0
        st.opened_at = 0.0

    def record_failure(self, key: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        st = self._get(key)
        if st.status == HALF_OPEN:
            st.status = OPEN                   # the probe failed → straight back to OPEN
            st.opened_at = now
            return
        st.failures += 1
        if st.failures >= self.failure_threshold:
            st.status = OPEN
            st.opened_at = now

    def status(self, key: str) -> str:
        return self._get(key).status
