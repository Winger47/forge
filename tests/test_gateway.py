from gateway.rate_limit import compute_bucket


def test_bucket_allows_when_tokens_available():
    allowed, tokens, _ = compute_bucket(tokens=3, ts=1000, capacity=3, refill=1.0, now=1000)
    assert allowed is True
    assert tokens == 2                       # consumed one


def test_bucket_blocks_when_empty():
    allowed, tokens, _ = compute_bucket(tokens=0, ts=1000, capacity=3, refill=1.0, now=1000)
    assert allowed is False
    assert tokens == 0                       # nothing to consume


def test_bucket_refills_over_time():
    # empty at t=1000; 5s later at 1 token/s → refilled to capacity, then consume one
    allowed, tokens, _ = compute_bucket(tokens=0, ts=1000, capacity=3, refill=1.0, now=1005)
    assert allowed is True
    assert tokens == 2                       # min(3, 0+5)=3 → consume 1 → 2


def test_bucket_caps_at_capacity():
    # idle 100s but capacity is 3 → tokens can't exceed 3 (the min() cap)
    allowed, tokens, _ = compute_bucket(tokens=0, ts=1000, capacity=3, refill=1.0, now=1100)
    assert allowed is True
    assert tokens == 2                       # min(3, 0+100)=3 → consume 1 → 2, NOT 99


def test_bucket_first_time_client_starts_full():
    # tokens=None means unseen client → starts at capacity
    allowed, tokens, _ = compute_bucket(tokens=None, ts=None, capacity=3, refill=1.0, now=1000)
    assert allowed is True
    assert tokens == 2                       # started at 3, consumed 1