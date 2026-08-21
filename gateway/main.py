import os
import json
import time
import random
import hashlib
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.concurrency import run_in_threadpool
from dotenv import load_dotenv
from openai import OpenAI
from sentence_transformers import SentenceTransformer
import numpy as np
import redis

from gateway.rate_limit import BUCKET_LUA
from gateway.circuit_breaker import CircuitBreaker
from gateway import ledger

# One breaker for the whole gateway, keyed per model. Trips a model out of
# rotation after repeated failures so we fail over FAST instead of waiting on a
# dependency we already know is down.
breaker = CircuitBreaker(failure_threshold=3, cooldown=15.0)

RETRY_ATTEMPTS = 2      # per-model retries before failing over to the next model


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter. The jitter is the point: without it, N
    clients that failed together retry together and stampede the recovering
    provider (the thundering-herd problem)."""
    return min(0.2 * (2 ** attempt), 2.0) + random.uniform(0, 0.1)

load_dotenv(Path(__file__).parent.parent / ".env")

app = FastAPI()


@app.on_event("startup")
def _startup():
    ledger.init_ledger()      # create the metering table if it isn't there yet


def _usage_from(result: dict) -> tuple[str, int, int, int]:
    """Pull (model, prompt_tokens, completion_tokens, total_tokens) out of a
    provider response dict. The model that actually answered lives in the
    response (failover may have changed it), so read it from there, not the request."""
    usage = result.get("usage") or {}
    return (
        result.get("model", "unknown"),
        int(usage.get("prompt_tokens", 0)),
        int(usage.get("completion_tokens", 0)),
        int(usage.get("total_tokens", 0)),
    )

groq = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1",
)

embedder = SentenceTransformer("all-MiniLM-L6-v2")
# The precision/recall dial. Measured on all-MiniLM-L6-v2: genuine paraphrases of
# the same question sit around 0.88–0.91, while different-meaning questions fall
# to ~0.5. 0.85 sits in that gap — high enough to reject a different question
# (poisoning guard), low enough that a real paraphrase still hits. 0.92 was too
# aggressive and killed recall (real paraphrases missed).
SIMILARITY_THRESHOLD = 0.85
MAX_SEMANTIC_ENTRIES = 500        # bound the in-memory store; FIFO eviction when full

# Semantic store, VECTORIZED. All vectors live in one L2-normalized matrix, so a
# lookup is a single matrix-vector product (cosine == dot when normalized) instead
# of an O(n) Python loop calling cos_sim per entry. A parallel meta list holds each
# row's response + scope. Bounded so a long-running process can't leak memory.
_sem_vectors = None               # np.ndarray (N, D), normalized rows — None when empty
_sem_meta = []                    # list of {"response", "scope"}, parallel to the rows

MODEL_CHAIN = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]

cache = redis.Redis(host="localhost", port=6379, decode_responses=True)

RATE_CAPACITY = 10
RATE_REFILL_PER_SEC = 1.0
_rate_script = cache.register_script(BUCKET_LUA)


def check_rate_limit(client_id: str) -> bool:
    """Token bucket. True = allowed, False = rate-limited. Fail-open if Redis is down."""
    try:
        allowed = _rate_script(
            keys=[f"ratelimit:{client_id}"],
            args=[RATE_CAPACITY, RATE_REFILL_PER_SEC],   # clock comes from Redis TIME, not us
        )
        return bool(allowed)
    except redis.RedisError:
        return True


def get_prompt_text(body: dict) -> str:
    for msg in reversed(body["messages"]):
        if msg["role"] == "user":
            return msg["content"]
    return ""


def make_cache_key(body: dict) -> str:
    stable = json.dumps(body, sort_keys=True)
    return "cache:" + hashlib.sha256(stable.encode()).hexdigest()


def semantic_scope(body: dict) -> str:
    """What must MATCH for a semantic hit to be valid.

    The embedding only captures the prompt TEXT. But two requests with identical
    text are NOT the same question if they target a different model or expose
    different tools — e.g. the same prompt on llama-8b vs llama-70b should not
    share a cached answer. So a stored answer is only eligible for reuse when the
    (model, tools) it was produced under match the incoming request's."""
    payload = json.dumps(
        {"model": body.get("model"), "tools": body.get("tools")},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _normalize(vec) -> np.ndarray:
    """L2-normalize so a dot product equals cosine similarity."""
    v = np.asarray(vec, dtype=np.float32).ravel()
    n = np.linalg.norm(v)
    return v / n if n else v


def semantic_lookup(query_vec, scope):
    """Best matching cached response for this vector WITHIN the given scope,
    or None. One matrix-vector product scores every row at once; the scope mask
    then restricts the argmax to entries produced under the same model+tools."""
    if _sem_vectors is None or not _sem_meta:
        return None
    q = _normalize(query_vec)
    sims = _sem_vectors @ q                                   # (N,) cosine scores
    mask = np.fromiter((m["scope"] == scope for m in _sem_meta), dtype=bool, count=len(_sem_meta))
    if not mask.any():
        return None
    scores = np.where(mask, sims, -1.0)                       # exclude other scopes
    best = int(scores.argmax())
    if scores[best] > SIMILARITY_THRESHOLD:
        return _sem_meta[best]["response"], float(scores[best])
    return None


def semantic_add(query_vec, response, scope):
    """Insert one entry, evicting the oldest (FIFO) if the store is at capacity."""
    global _sem_vectors, _sem_meta
    v = _normalize(query_vec).reshape(1, -1)
    _sem_vectors = v if _sem_vectors is None else np.vstack([_sem_vectors, v])
    _sem_meta.append({"response": response, "scope": scope})
    if len(_sem_meta) > MAX_SEMANTIC_ENTRIES:
        _sem_vectors = _sem_vectors[1:]                       # drop oldest row
        _sem_meta.pop(0)


def semantic_purge(query_vec, scope) -> int:
    """Remove every entry matching this vector AND scope. Returns how many went."""
    global _sem_vectors, _sem_meta
    if _sem_vectors is None or not _sem_meta:
        return 0
    q = _normalize(query_vec)
    sims = _sem_vectors @ q
    keep = [i for i, m in enumerate(_sem_meta)
            if not (m["scope"] == scope and sims[i] > SIMILARITY_THRESHOLD)]
    removed = len(_sem_meta) - len(keep)
    _sem_meta = [_sem_meta[i] for i in keep]
    _sem_vectors = _sem_vectors[keep] if keep else None
    return removed


def models_to_try(body: dict) -> list:
    """Model order for a request: the client's requested model FIRST (so an
    agent-side /model choice is honored), then the configured fallback chain,
    deduped. Shared by streaming and non-streaming so both fail over the same way
    without silently overriding the caller's model on the happy path."""
    ordered = []
    requested = body.get("model")
    if requested:
        ordered.append(requested)
    for m in MODEL_CHAIN:
        if m not in ordered:
            ordered.append(m)
    return ordered


def wants_fresh(body: dict, request: Request) -> bool:
    """Should we bypass the cache? Mirrors HTTP's Cache-Control: no-cache.
    Two signals: the standard header, or a body flag for clients that can't set headers."""
    if "no-cache" in request.headers.get("cache-control", "").lower():
        return True
    if body.get("no_cache") is True:
        return True
    return False


def call_with_failover(body: dict):
    last_error = None
    for model in models_to_try(body):
        if not breaker.allow(model):
            print(f"   [breaker OPEN for {model} — skipping]")
            last_error = last_error or RuntimeError(f"circuit open for {model}")
            continue
        for attempt in range(RETRY_ATTEMPTS):
            try:
                payload = dict(body)
                payload["model"] = model
                print(f"   [trying {model} (attempt {attempt + 1})]")
                response = groq.chat.completions.create(**payload)
                breaker.record_success(model)          # healthy again → close breaker
                print(f"   [SUCCESS with {model}]")
                return response
            except Exception as e:
                last_error = e
                breaker.record_failure(model)
                if attempt < RETRY_ATTEMPTS - 1:
                    delay = _backoff(attempt)
                    print(f"   [FAILED {model}: {e}] -> retry in {delay:.2f}s")
                    time.sleep(delay)
                else:
                    print(f"   [FAILED {model}: {e}] -> failing over")
    raise last_error


def stream_with_failover(body: dict, client_id: str = "default"):
    """Yield SSE chunks with failover ACROSS models — and surface errors instead
    of swallowing them.

    Failover is only possible up to the FIRST token: once a chunk has been sent
    to the client, the bytes are gone and we can't restart on another model. So
    we pull the first chunk eagerly here; a failure to establish the stream (bad
    model, auth, rate limit) or to produce a first token fails over to the next
    model. A failure AFTER streaming has begun can only be reported, as an error
    event — never silently truncated to a bare [DONE], which is what made real
    outages look to the agent like an 'empty response'."""
    last_error = None
    for model in models_to_try(body):
        if not breaker.allow(model):
            print(f"   [breaker OPEN for {model} — skipping stream]")
            last_error = last_error or RuntimeError(f"circuit open for {model}")
            continue
        attempt = dict(body)
        attempt["model"] = model
        try:
            response = groq.chat.completions.create(**attempt)
            it = iter(response)
            first = next(it)                      # force establishment + first token
        except StopIteration:
            print(f"   [stream {model}: empty] -> failing over")
            last_error = RuntimeError("empty stream from provider")
            breaker.record_failure(model)
            continue
        except Exception as e:
            print(f"   [stream FAILED {model}: {e}] -> failing over")
            last_error = e
            breaker.record_failure(model)
            continue

        # established — committed to this model now. First token arrived, so the
        # provider is healthy: close the breaker before we start yielding.
        breaker.record_success(model)
        print(f"   [streaming with {model}]")
        # A stream carries its token usage in a trailing chunk (the client asks
        # for it via stream_options.include_usage). Capture it as it flies past so
        # we can write ONE ledger row when the stream ends — streaming is still a
        # real provider call and must show up in metering like any other.
        usage = None
        answered_model = model
        stream_ms = ledger.timer().__enter__()
        try:
            for chunk in [first, *it]:
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                if getattr(chunk, "model", None):
                    answered_model = chunk.model
                yield f"data: {chunk.model_dump_json()}\n\n"
        except Exception as e:
            # mid-stream failure: bytes already sent, can't fail over. Report it.
            print(f"   [stream ERROR mid-flight {model}: {e}]")
            err = {"error": {"message": str(e), "type": "stream_error"}}
            yield f"data: {json.dumps(err)}\n\n"
        stream_ms.__exit__()
        pt = getattr(usage, "prompt_tokens", 0) if usage else 0
        ct = getattr(usage, "completion_tokens", 0) if usage else 0
        tt = getattr(usage, "total_tokens", 0) if usage else 0
        ledger.record(model=answered_model, prompt_tokens=pt, completion_tokens=ct,
                      total_tokens=tt, latency_ms=stream_ms.ms,
                      cache_status="STREAM", client_id=client_id)
        yield "data: [DONE]\n\n"
        return

    # every model failed to even start
    print(f"   [stream: all models failed: {last_error}]")
    err = {"error": {"message": str(last_error), "type": "upstream_unavailable"}}
    yield f"data: {json.dumps(err)}\n\n"
    yield "data: [DONE]\n\n"


def _blocking_completion(body: dict, bypass: bool, client_id: str = "default"):
    """The full non-streaming path: exact cache -> semantic cache -> failover -> store.

    Every step here is BLOCKING (redis I/O, embedding compute, provider HTTP).
    It is deliberately a plain sync function so the async handler can run it via
    run_in_threadpool — keeping the event loop free to accept other requests
    instead of serializing everyone behind one Groq call.

    `bypass` is decided by the caller BEFORE this runs, because it reads the
    no_cache body flag which we strip here (stripping before hashing keeps the
    cache key stable).

    Returns (result_dict, cache_status) where status is one of
    HIT | SEMANTIC | MISS | BYPASS — the caller puts it in the X-Cache header so
    a client can show the cache decision without reading the server logs."""
    body.pop("no_cache", None)
    key = make_cache_key(body)

    scope = semantic_scope(body)    # model+tools this answer is valid under

    def _log(result: dict, status: str, latency_ms: int):
        """Append the ledger row for this served request. Every path that
        RETURNS a response — cache hit or provider call — logs exactly once, so
        the ledger reflects every request the gateway served, not only misses."""
        model, pt, ct, tt = _usage_from(result)
        ledger.record(model=model, prompt_tokens=pt, completion_tokens=ct,
                      total_tokens=tt, latency_ms=latency_ms,
                      cache_status=status, client_id=client_id)

    if not bypass:
        # 1. exact cache — FAST path: hash lookup only, NO embedding
        with ledger.timer() as t:
            cached = cache.get(key)
        if cached is not None:
            print("CACHE HIT", key)
            result = json.loads(cached)
            _log(result, "HIT", t.ms)
            return result, "HIT"

        # 2. semantic cache — only now do we pay for the embedding.
        #    A hit must match BOTH the prompt (by vector) AND the scope, or we'd
        #    serve one model's answer for another model's question.
        prompt = get_prompt_text(body)
        with ledger.timer() as t:
            query_vec = embedder.encode(prompt)
            hit = semantic_lookup(query_vec, scope)
        if hit is not None:
            response, score = hit
            print(f"SEMANTIC HIT (score {score:.3f})")
            _log(response, "SEMANTIC", t.ms)
            return response, "SEMANTIC"
        status = "MISS"
    else:
        print("CACHE BYPASS -> forcing fresh response")
        prompt = get_prompt_text(body)
        query_vec = embedder.encode(prompt)   # bypass skips reads but still needs the vector to store
        status = "BYPASS"

    # 3. real miss (or bypass) -> providers with failover. Time ONLY the provider
    #    round-trip — that's the latency the ledger's p95 is supposed to reflect.
    print("MISS -> calling providers")
    with ledger.timer() as t:
        response = call_with_failover(body)
    result = response.model_dump()
    _log(result, status, t.ms)

    # store the fresh result so future NORMAL requests benefit (bypass refreshes, doesn't disable)
    cache.set(key, json.dumps(result), ex=3600)
    semantic_add(query_vec, result, scope)
    return result, status


def _blocking_invalidate(body: dict):
    """Blocking half of cache invalidation (redis delete + embed + O(n) scan).
    Sync by design so the async endpoint can offload it to a thread."""
    body.pop("no_cache", None)
    key = make_cache_key(body)

    exact_deleted = bool(cache.delete(key))     # exact cache: O(1)

    # semantic: remove entries matching this prompt AND scope. Scoping so
    # invalidating a poisoned answer for one model doesn't wipe a legitimately
    # different model's cached answer.
    scope = semantic_scope(body)
    prompt = get_prompt_text(body)
    query_vec = embedder.encode(prompt)
    removed = semantic_purge(query_vec, scope)
    return {"exact_deleted": exact_deleted, "semantic_removed": removed}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    # 0. rate limit — reject before doing any work. Redis I/O, so off-loop.
    client_id = request.headers.get("x-client-id", "default")
    if not await run_in_threadpool(check_rate_limit, client_id):
        print(f"RATE LIMITED: {client_id}")
        return JSONResponse(status_code=429, content={"error": "rate limit exceeded"})

    body = await request.json()

    # --- STREAMING: forward chunks straight through, no cache ---
    # A stream is a flow of chunks, not a complete response — it can't be cached
    # or model_dump()'d like a normal reply. So streaming takes its own SSE path
    # and bypasses the entire cache/failover machinery.
    #
    # The generator is SYNC on purpose: Starlette iterates a sync streaming body
    # in a threadpool (iterate_in_threadpool), so the blocking provider read here
    # already runs off the event loop — no async client needed for correctness.
    if body.get("stream"):
        print("STREAMING -> forwarding chunks")
        # X-Cache is honest even here: streaming never touches the cache.
        return StreamingResponse(stream_with_failover(body, client_id),
                                 media_type="text/event-stream",
                                 headers={"X-Cache": "STREAM-BYPASS"})

    # --- non-streaming path ---
    # bypass intent is read HERE (needs the request headers + no_cache flag),
    # then the entire blocking pipeline is offloaded to a thread in one hop.
    bypass = wants_fresh(body, request)
    result, cache_status = await run_in_threadpool(
        _blocking_completion, body, bypass, client_id
    )
    return JSONResponse(content=result, headers={"X-Cache": cache_status})


@app.post("/v1/cache/invalidate")
async def invalidate_cache(request: Request):
    """Remove a cached entry so a wrong/stale answer isn't served again.
    Body: same shape as the original chat request."""
    body = await request.json()
    return await run_in_threadpool(_blocking_invalidate, body)


_DASHBOARD_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """The metrics dashboard: a read model over the ledger. It only READS the
    projection (/v1/metrics, /v1/ledger) — fully decoupled from the write path,
    which never serves HTML or reads these back (CQRS-lite)."""
    return HTMLResponse(_DASHBOARD_HTML)


@app.get("/v1/metrics")
async def metrics():
    """Read model over the ledger: totals, $/model, cache-hit rate, p95 latency.
    Read-only projection — the write path (chat) never reads this back."""
    return await run_in_threadpool(ledger.summary)


@app.get("/v1/ledger")
async def ledger_rows(limit: int = 50):
    """Most-recent ledger rows — the raw request log for the dashboard."""
    return await run_in_threadpool(ledger.recent, limit)