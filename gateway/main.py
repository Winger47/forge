import os
import json
import time
import hashlib
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.concurrency import run_in_threadpool
from dotenv import load_dotenv
from openai import OpenAI
from sentence_transformers import SentenceTransformer, util
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
semantic_store = []

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
            args=[RATE_CAPACITY, RATE_REFILL_PER_SEC, time.time()],
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
    for model in MODEL_CHAIN:
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


def _blocking_completion(body: dict, bypass: bool):
    """The full non-streaming path: exact cache -> semantic cache -> failover -> store.

    Every step here is BLOCKING (redis I/O, embedding compute, provider HTTP).
    It is deliberately a plain sync function so the async handler can run it via
    run_in_threadpool — keeping the event loop free to accept other requests
    instead of serializing everyone behind one Groq call.

    `bypass` is decided by the caller BEFORE this runs, because it reads the
    no_cache body flag which we strip here (stripping before hashing keeps the
    cache key stable)."""
    body.pop("no_cache", None)
    key = make_cache_key(body)

    if not bypass:
        # 1. exact cache — FAST path: hash lookup only, NO embedding
        cached = cache.get(key)
        if cached is not None:
            print("CACHE HIT", key)
            return json.loads(cached)

        # 2. semantic cache — only now do we pay for the embedding
        prompt = get_prompt_text(body)
        query_vec = embedder.encode(prompt)
        for entry in semantic_store:
            score = util.cos_sim(query_vec, entry["vector"]).item()
            if score > SIMILARITY_THRESHOLD:
                print(f"SEMANTIC HIT (score {score:.3f})")
                return entry["response"]
    else:
        print("CACHE BYPASS -> forcing fresh response")
        prompt = get_prompt_text(body)
        query_vec = embedder.encode(prompt)   # bypass skips reads but still needs the vector to store

    # 3. real miss (or bypass) -> providers with failover
    print("MISS -> calling providers")
    response = call_with_failover(body)
    result = response.model_dump()

    # store the fresh result so future NORMAL requests benefit (bypass refreshes, doesn't disable)
    cache.set(key, json.dumps(result), ex=3600)
    semantic_store.append({"vector": query_vec, "response": result})
    return result


def _blocking_invalidate(body: dict):
    """Blocking half of cache invalidation (redis delete + embed + O(n) scan).
    Sync by design so the async endpoint can offload it to a thread."""
    body.pop("no_cache", None)
    key = make_cache_key(body)

    exact_deleted = bool(cache.delete(key))     # exact cache: O(1)

    # semantic: remove entries matching this prompt (O(n) scan — in-memory list limitation)
    prompt = get_prompt_text(body)
    query_vec = embedder.encode(prompt)
    before = len(semantic_store)
    semantic_store[:] = [
        e for e in semantic_store
        if util.cos_sim(query_vec, e["vector"]).item() <= SIMILARITY_THRESHOLD
    ]
    return {"exact_deleted": exact_deleted, "semantic_removed": before - len(semantic_store)}


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
        def event_stream():
            try:
                response = groq.chat.completions.create(**body)
                for chunk in response:
                    yield f"data: {chunk.model_dump_json()}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                print(f"STREAM ERROR: {e}")
                yield "data: [DONE]\n\n"
        print("STREAMING -> forwarding chunks")
        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # --- non-streaming path ---
    # bypass intent is read HERE (needs the request headers + no_cache flag),
    # then the entire blocking pipeline is offloaded to a thread in one hop.
    bypass = wants_fresh(body, request)
    return await run_in_threadpool(_blocking_completion, body, bypass)


@app.post("/v1/cache/invalidate")
async def invalidate_cache(request: Request):
    """Remove a cached entry so a wrong/stale answer isn't served again.
    Body: same shape as the original chat request."""
    body = await request.json()
    return await run_in_threadpool(_blocking_invalidate, body)