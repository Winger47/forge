# ledger.py — the append-only metering ledger (Phase 3).
#
# Every model call that reaches a provider writes ONE immutable row here:
# who asked, which model answered, how many tokens, what it cost, how long it
# took, and whether a cache served it. Append-only on purpose — this is an audit
# trail, not mutable state. We INSERT; we never UPDATE or DELETE. The dashboard
# (Phase 7) and the metrics endpoint (Phase 4) are READ MODELS over this table.
#
# CAP decision (the tradeoff FORGE.md §Phase-3 asks you to make out loud):
# the write is on the request path but NON-FATAL. A row is written before the
# response returns so metering is reliable on the happy path, but if Postgres is
# down the request still succeeds — we prefer availability of the agent over
# perfect accounting. A dropped row is a gap in the ledger, not a failed request.

import os
import time
import datetime
import psycopg

# One process-wide connection string. Points at the local `forge` database by
# default; override with DATABASE_URL for a different host/deploy.
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost:5432/forge")

# Per-model pricing, USD per 1,000,000 tokens: (input, output).
# These are Groq's published rates and are meant to be edited — an unknown model
# costs 0 and is recorded as such rather than guessed. Cost is derived here, at
# write time, from the token counts the provider returned, so the ledger stores a
# real number instead of forcing every reader to re-derive it.
PRICES = {
    "openai/gpt-oss-120b":     (0.15, 0.75),
    "openai/gpt-oss-20b":      (0.10, 0.50),
    "qwen/qwen3.6-27b":        (0.29, 0.59),
    # legacy names kept so historical ledger rows still price correctly
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant":    (0.05, 0.08),
}

_conn: psycopg.Connection | None = None


def cost_for(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """USD cost of one call, from token counts and the price table.
    Unknown model → 0.0 (recorded honestly, not estimated)."""
    rate_in, rate_out = PRICES.get(model, (0.0, 0.0))
    return (prompt_tokens * rate_in + completion_tokens * rate_out) / 1_000_000


def _connect() -> psycopg.Connection | None:
    """Lazily open (and cache) one autocommit connection. Returns None if the DB
    is unreachable, so callers degrade instead of crashing."""
    global _conn
    if _conn is not None and not _conn.closed:
        return _conn
    try:
        _conn = psycopg.connect(DATABASE_URL, autocommit=True)
        return _conn
    except Exception as e:  # noqa: BLE001 — any connect failure means "no ledger"
        print(f"   [ledger: DB unreachable: {e}]")
        _conn = None
        return None


def init_ledger() -> None:
    """Create the ledger table if it doesn't exist. Called once at startup.
    Safe to call repeatedly (IF NOT EXISTS)."""
    conn = _connect()
    if conn is None:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ledger (
            id                SERIAL PRIMARY KEY,
            ts                TIMESTAMPTZ  NOT NULL DEFAULT now(),
            client_id         TEXT         NOT NULL DEFAULT 'default',
            model             TEXT         NOT NULL,
            prompt_tokens     INTEGER      NOT NULL DEFAULT 0,
            completion_tokens INTEGER      NOT NULL DEFAULT 0,
            total_tokens      INTEGER      NOT NULL DEFAULT 0,
            cost_usd          DOUBLE PRECISION NOT NULL DEFAULT 0,
            latency_ms        INTEGER      NOT NULL DEFAULT 0,
            cache_status      TEXT         NOT NULL DEFAULT 'MISS'
        )
        """
    )
    # index the columns the read models filter/sort on most (time, then model)
    conn.execute("CREATE INDEX IF NOT EXISTS ledger_ts_idx ON ledger (ts DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS ledger_model_idx ON ledger (model)")


def record(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    latency_ms: int,
    cache_status: str,
    client_id: str = "default",
) -> None:
    """Append one row. Never raises: a metering failure must not fail a request."""
    conn = _connect()
    if conn is None:
        return
    # A cache hit paid the provider NOTHING — record it (so cache-hit-rate and
    # tokens-served stay honest) but at zero cost. Only a real provider call
    # (MISS / BYPASS) accrues spend.
    cost = 0.0 if cache_status in ("HIT", "SEMANTIC") else \
        cost_for(model, prompt_tokens, completion_tokens)
    try:
        conn.execute(
            """
            INSERT INTO ledger
              (client_id, model, prompt_tokens, completion_tokens,
               total_tokens, cost_usd, latency_ms, cache_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                client_id,
                model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                cost,
                latency_ms,
                cache_status,
            ),
        )
    except Exception as e:  # noqa: BLE001 — availability over accounting
        print(f"   [ledger: write dropped: {e}]")


class _Timer:
    """Context manager that measures wall-clock ms for the block it wraps.
    `.ms` is readable after exit — used to time the provider round-trip."""

    def __enter__(self):
        self._t0 = time.perf_counter()
        self.ms = 0
        return self

    def __exit__(self, *exc):
        self.ms = int((time.perf_counter() - self._t0) * 1000)
        return False


def timer() -> _Timer:
    return _Timer()


# ── READ MODELS (Phase 4 metrics / Phase 7 dashboard consume these) ──────────
def summary() -> dict:
    """Aggregate rollup the dashboard needs in one query: totals, $/model,
    cache-hit rate, and p95 latency. Returns empty-ish structure if DB is down."""
    conn = _connect()
    empty = {"total_requests": 0, "total_cost": 0.0, "total_tokens": 0,
             "cache_hit_rate": 0.0, "p95_latency_ms": 0, "by_model": []}
    if conn is None:
        return empty
    try:
        row = conn.execute(
            """
            SELECT count(*),
                   coalesce(sum(cost_usd), 0),
                   coalesce(sum(total_tokens), 0),
                   coalesce(avg((cache_status IN ('HIT','SEMANTIC'))::int), 0),
                   coalesce(percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms), 0)
            FROM ledger
            """
        ).fetchone()
        by_model = conn.execute(
            """
            SELECT model, count(*), coalesce(sum(cost_usd), 0),
                   coalesce(sum(total_tokens), 0)
            FROM ledger GROUP BY model ORDER BY sum(cost_usd) DESC
            """
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        print(f"   [ledger: summary failed: {e}]")
        return empty
    return {
        "total_requests": row[0],
        "total_cost": float(row[1]),
        "total_tokens": int(row[2]),
        "cache_hit_rate": float(row[3]),
        "p95_latency_ms": int(row[4]),
        "by_model": [
            {"model": m, "requests": c, "cost": float(cost), "tokens": int(tok)}
            for (m, c, cost, tok) in by_model
        ],
    }


def recent(limit: int = 50) -> list[dict]:
    """Most-recent rows for the dashboard's request log."""
    conn = _connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT ts, client_id, model, prompt_tokens, completion_tokens,
                   total_tokens, cost_usd, latency_ms, cache_status
            FROM ledger ORDER BY ts DESC LIMIT %s
            """,
            (limit,),
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        print(f"   [ledger: recent failed: {e}]")
        return []
    return [
        {
            "ts": ts.isoformat() if isinstance(ts, datetime.datetime) else str(ts),
            "client_id": cid, "model": model,
            "prompt_tokens": pt, "completion_tokens": ct, "total_tokens": tt,
            "cost_usd": float(cost), "latency_ms": lat, "cache_status": cache,
        }
        for (ts, cid, model, pt, ct, tt, cost, lat, cache) in rows
    ]
