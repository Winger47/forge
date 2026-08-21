# tests/test_ledger.py — the metering ledger (Phase 3).
#
# cost_for is pure math and is tested without a database. The DB-touching tests
# skip cleanly when Postgres isn't reachable, so the suite stays green on a
# machine without the `forge` database while still exercising real INSERTs where
# one exists.

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from gateway import ledger


# ── pure cost math (no DB) ───────────────────────────────────────────────────
def test_cost_known_model():
    # llama-3.3-70b-versatile: (0.59 in, 0.79 out) per 1M
    cost = ledger.cost_for("llama-3.3-70b-versatile", 1_000_000, 1_000_000)
    assert round(cost, 4) == round(0.59 + 0.79, 4)


def test_cost_unknown_model_is_zero():
    assert ledger.cost_for("some-model-nobody-priced", 5000, 5000) == 0.0


def test_cost_scales_with_tokens():
    small = ledger.cost_for("llama-3.1-8b-instant", 100, 100)
    big = ledger.cost_for("llama-3.1-8b-instant", 100_000, 100_000)
    assert big > small > 0


# ── real DB round-trip (skips if Postgres/forge is unavailable) ──────────────
@pytest.fixture
def db():
    if ledger._connect() is None:
        pytest.skip("Postgres 'forge' database not reachable")
    ledger.init_ledger()
    return ledger._connect()


def test_record_appends_a_row(db):
    before = db.execute("SELECT count(*) FROM ledger").fetchone()[0]
    ledger.record(
        model="llama-3.1-8b-instant",
        prompt_tokens=100, completion_tokens=50, total_tokens=150,
        latency_ms=42, cache_status="MISS", client_id="pytest",
    )
    after = db.execute("SELECT count(*) FROM ledger").fetchone()[0]
    assert after == before + 1

    row = db.execute(
        "SELECT model, total_tokens, cost_usd, cache_status FROM ledger "
        "WHERE client_id = 'pytest' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == "llama-3.1-8b-instant"
    assert row[1] == 150
    assert row[2] > 0            # a MISS accrues real cost
    assert row[3] == "MISS"


def test_cache_hit_costs_zero(db):
    ledger.record(
        model="llama-3.3-70b-versatile",
        prompt_tokens=1000, completion_tokens=1000, total_tokens=2000,
        latency_ms=1, cache_status="HIT", client_id="pytest",
    )
    cost = db.execute(
        "SELECT cost_usd FROM ledger WHERE client_id = 'pytest' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert cost == 0.0          # a cache hit paid the provider nothing


def test_summary_shape(db):
    s = ledger.summary()
    assert set(s) >= {"total_requests", "total_cost", "cache_hit_rate",
                      "p95_latency_ms", "by_model"}
    assert isinstance(s["by_model"], list)
