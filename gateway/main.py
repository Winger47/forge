import os
import json
import hashlib
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.concurrency import run_in_threadpool
from dotenv import load_dotenv
from openai import OpenAI
from sentence_transformers import SentenceTransformer
import numpy as np
import redis

from gateway.rate_limit import BUCKET_LUA

load_dotenv(Path(__file__).parent.parent / ".env")

app = FastAPI()

groq = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1",
)

embedder = SentenceTransformer("all-MiniLM-L6-v2")
SIMILARITY_THRESHOLD = 0.92
MAX_SEMANTIC_ENTRIES = 500        # bound the in-memory store; FIFO eviction when full

# Semantic store, VECTORIZED. All vectors live in one L2-normalized matrix, so a
# lookup is a single matrix-vector product (cosine == dot when normalized) instead
# of an O(n) Python loop calling cos_sim per entry. A parallel meta list holds each
# row's response + scope. Bounded so a long-running process can't leak memory.
_sem_vectors = None               # np.ndarray (N, D), normalized rows — None when empty
_sem_meta = []                    # list of {"response", "scope"}, parallel to the rows

MODEL_CHAIN = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

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
        try:
            attempt = dict(body)
            attempt["model"] = model
            print(f"   [trying {model}]")
            response = groq.chat.completions.create(**attempt)
            print(f"   [SUCCESS with {model}]")
            return response
        except Exception as e:
            print(f"   [FAILED {model}: {e}] -> failing over")
            last_error = e
            continue
    raise last_error


def stream_with_failover(body: dict):
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
        attempt = dict(body)
        attempt["model"] = model
        try:
            response = groq.chat.completions.create(**attempt)
            it = iter(response)
            first = next(it)                      # force establishment + first token
        except StopIteration:
            print(f"   [stream {model}: empty] -> failing over")
            last_error = RuntimeError("empty stream from provider")
            continue
        except Exception as e:
            print(f"   [stream FAILED {model}: {e}] -> failing over")
            last_error = e
            continue

        # established — committed to this model now
        print(f"   [streaming with {model}]")
        try:
            yield f"data: {first.model_dump_json()}\n\n"
            for chunk in it:
                yield f"data: {chunk.model_dump_json()}\n\n"
        except Exception as e:
            # mid-stream failure: bytes already sent, can't fail over. Report it.
            print(f"   [stream ERROR mid-flight {model}: {e}]")
            err = {"error": {"message": str(e), "type": "stream_error"}}
            yield f"data: {json.dumps(err)}\n\n"
        yield "data: [DONE]\n\n"
        return

    # every model failed to even start
    print(f"   [stream: all models failed: {last_error}]")
    err = {"error": {"message": str(last_error), "type": "upstream_unavailable"}}
    yield f"data: {json.dumps(err)}\n\n"
    yield "data: [DONE]\n\n"


def _blocking_completion(body: dict, bypass: bool):
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

    if not bypass:
        # 1. exact cache — FAST path: hash lookup only, NO embedding
        cached = cache.get(key)
        if cached is not None:
            print("CACHE HIT", key)
            return json.loads(cached), "HIT"

        # 2. semantic cache — only now do we pay for the embedding.
        #    A hit must match BOTH the prompt (by vector) AND the scope, or we'd
        #    serve one model's answer for another model's question.
        prompt = get_prompt_text(body)
        query_vec = embedder.encode(prompt)
        hit = semantic_lookup(query_vec, scope)
        if hit is not None:
            response, score = hit
            print(f"SEMANTIC HIT (score {score:.3f})")
            return response, "SEMANTIC"
        status = "MISS"
    else:
        print("CACHE BYPASS -> forcing fresh response")
        prompt = get_prompt_text(body)
        query_vec = embedder.encode(prompt)   # bypass skips reads but still needs the vector to store
        status = "BYPASS"

    # 3. real miss (or bypass) -> providers with failover
    print("MISS -> calling providers")
    response = call_with_failover(body)
    result = response.model_dump()

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
        return StreamingResponse(stream_with_failover(body),
                                 media_type="text/event-stream",
                                 headers={"X-Cache": "STREAM-BYPASS"})

    # --- non-streaming path ---
    # bypass intent is read HERE (needs the request headers + no_cache flag),
    # then the entire blocking pipeline is offloaded to a thread in one hop.
    bypass = wants_fresh(body, request)
    result, cache_status = await run_in_threadpool(_blocking_completion, body, bypass)
    return JSONResponse(content=result, headers={"X-Cache": cache_status})


@app.post("/v1/cache/invalidate")
async def invalidate_cache(request: Request):
    """Remove a cached entry so a wrong/stale answer isn't served again.
    Body: same shape as the original chat request."""
    body = await request.json()
    return await run_in_threadpool(_blocking_invalidate, body)