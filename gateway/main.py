import os
import json
import hashlib
from pathlib import Path
from fastapi import FastAPI, Request
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

# ── NEW (4c): the fallback chain — try these models in order ──
MODEL_CHAIN = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

cache = redis.Redis(host="localhost", port=6379, decode_responses=True)


def get_prompt_text(body: dict) -> str:
    for msg in reversed(body["messages"]):
        if msg["role"] == "user":
            return msg["content"]
    return ""


def make_cache_key(body: dict) -> str:
    stable = json.dumps(body, sort_keys=True)
    return "cache:" + hashlib.sha256(stable.encode()).hexdigest()


# ── NEW (4c): try each model in the chain until one succeeds ──
def call_with_failover(body: dict):
    last_error = None
    for model in MODEL_CHAIN:
        try:
            attempt = dict(body)          # copy so we don't mutate the original request
            attempt["model"] = model      # override with this chain's model
            print(f"   [trying {model}]")
            response = groq.chat.completions.create(**attempt)
            print(f"   [SUCCESS with {model}]")
            return response
        except Exception as e:
            print(f"   [FAILED {model}: {e}] → failing over")
            last_error = e
            continue                      # next model in the chain
    raise last_error                      # all providers failed


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
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

    # 3. real miss → call providers WITH FAILOVER
    print("MISS → calling providers")
    response = call_with_failover(body)              # ← was: groq.chat.completions.create(**body)
    result = response.model_dump()

    # store in both caches
    cache.set(key, json.dumps(result), ex=3600)
    semantic_store.append({"vector": query_vec, "response": result})
    return result