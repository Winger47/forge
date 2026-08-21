# tests/test_circuit_breaker.py — the provider circuit breaker (Phase 4).
# Clock is injected (now=...) so state transitions are tested without sleeping.

from gateway.circuit_breaker import CircuitBreaker, CLOSED, OPEN, HALF_OPEN


def test_starts_closed_and_allows():
    cb = CircuitBreaker(failure_threshold=3)
    assert cb.allow("groq") is True
    assert cb.status("groq") == CLOSED


def test_trips_open_after_threshold_failures():
    cb = CircuitBreaker(failure_threshold=3, cooldown=10)
    for _ in range(3):
        cb.record_failure("m", now=0)
    assert cb.status("m") == OPEN
    # OPEN → calls are refused fast while cooling down
    assert cb.allow("m", now=1) is False


def test_half_open_probe_after_cooldown():
    cb = CircuitBreaker(failure_threshold=2, cooldown=10)
    cb.record_failure("m", now=0)
    cb.record_failure("m", now=0)               # OPEN at t=0
    assert cb.allow("m", now=5) is False         # still cooling
    assert cb.allow("m", now=10) is True         # cooldown elapsed → HALF_OPEN probe
    assert cb.status("m") == HALF_OPEN


def test_probe_success_closes_breaker():
    cb = CircuitBreaker(failure_threshold=1, cooldown=5)
    cb.record_failure("m", now=0)                # OPEN
    assert cb.allow("m", now=5) is True          # HALF_OPEN
    cb.record_success("m")                       # probe worked
    assert cb.status("m") == CLOSED
    assert cb.allow("m", now=6) is True


def test_probe_failure_reopens_breaker():
    cb = CircuitBreaker(failure_threshold=1, cooldown=5)
    cb.record_failure("m", now=0)                # OPEN
    cb.allow("m", now=5)                          # HALF_OPEN
    cb.record_failure("m", now=5)                # probe failed → OPEN again
    assert cb.status("m") == OPEN
    assert cb.allow("m", now=6) is False          # cooling down again from t=5


def test_success_resets_failure_count():
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure("m")
    cb.record_failure("m")
    cb.record_success("m")                        # intermittent recovery
    cb.record_failure("m")
    assert cb.status("m") == CLOSED               # 1 failure, not 3 → still closed


def test_breaker_is_per_key():
    cb = CircuitBreaker(failure_threshold=1, cooldown=100)
    cb.record_failure("model-a", now=0)           # a trips
    assert cb.allow("model-a", now=1) is False
    assert cb.allow("model-b", now=1) is True     # b unaffected
