import os
import json
import time
import hashlib
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from openai import OpenAI
from sentence_transformers import SentenceTransformer, util
import redis

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

# ── NEW (4d): rate limiting config ──
RATE_CAPACITY = 10           # max burst (bucket size)
RATE_REFILL_PER_SEC = 1.0    # steady-state refill rate

# Token-bucket as an atomic Lua script — read, refill, consume, write happen as ONE
# indivisible operation, so concurrent requests can't both consume the last token.
_BUCKET_LUA = """
local key      = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill   = tonumber(ARGV[2])
local now      = tonumber(ARGV[3])

local data   = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts     = tonumber(data[2])

if tokens == nil then
    tokens = capacity
    ts     = now
end

local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * refill)

local allowed = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, 3600)
return allowed
"""
_rate_script = cache.register_script(_BUCKET_LUA)


def check_rate_limit(client_id: str) -> bool:
    """Token bucket. True = allowed, False = rate-limited.
    Fail-open: if Redis is down, allow the request (availability over strict enforcement)."""
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
            print(f"   [FAILED {model}: {e}] → failing over")
            last_error = e
            continue
    raise last_error


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    # ── 0. RATE LIMIT — reject before doing ANY work ──
    client_id = request.headers.get("x-client-id", "default")
    if not check_rate_limit(client_id):
        print(f"RATE LIMITED: {client_id}")
        return JSONResponse(status_code=429, content={"error": "rate limit exceeded"})

    body = await request.json()
    key = make_cache_key(body)

    # 1. exact cache
    cached = cache.get(key)
    if cached is not None:
        print("CACHE HIT", key)
        return json.loads(cached)

    # 2. semantic cache
    prompt = get_prompt_text(body)
    query_vec = embedder.encode(prompt)
    for entry in semantic_store:
        score = util.cos_sim(query_vec, entry["vector"]).item()
        if score > SIMILARITY_THRESHOLD:
            print(f"SEMANTIC HIT (score {score:.3f})")
            return entry["response"]

    # 3. real miss → providers with failover
    print("MISS → calling providers")
    response = call_with_failover(body)
    result = response.model_dump()

    cache.set(key, json.dumps(result), ex=3600)
    semantic_store.append({"vector": query_vec, "response": result})
    return result