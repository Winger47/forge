import os
import json
import hashlib
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from openai import OpenAI
from sentence_transformers import SentenceTransformer, util
import redis
load_dotenv()

app = FastAPI()

groq = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1",
)
embedder = SentenceTransformer("all-MiniLM-L6-v2")   # load once at startup
SIMILARITY_THRESHOLD = 0.92                          # the tuning knob
semantic_store = []  
def get_prompt_text(body: dict) -> str:
    for msg in reversed(body["messages"]):
        if msg["role"] == "user":
            return msg["content"]
    return ""
cache = redis.Redis(host="localhost", port=6379, decode_responses=True)
def make_cache_key(body: dict) -> str:
    """Turn a request into a stable cache key (a hash of its contents).
    Identical requests → identical string → identical hash → same key."""
    stable = json.dumps(body, sort_keys=True)   # sort_keys makes it order-independent
    return "cache:" + hashlib.sha256(stable.encode()).hexdigest()

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()       
    key=make_cache_key(body)
    cached=cache.get(key)
    if cached is not None:  
        print("CACHE HIT", key)      # ← Add this line
        return json.loads(cached)
    prompt = get_prompt_text(body)
    print(f"   [embedding prompt: {prompt[:80]}]")     
    query_vec = embedder.encode(prompt)

    for entry in semantic_store:
        score = util.cos_sim(query_vec, entry["vector"]).item()
        if score > SIMILARITY_THRESHOLD:
            print(f"SEMANTIC HIT (score {score:.3f})")
            return entry["response"]

    print("MISS → calling Groq")
    response = groq.chat.completions.create(**body)
    result = response.model_dump()
    cache.set(key, json.dumps(result), ex=3600)        # exact cache
    semantic_store.append({"vector": query_vec, "response": result})  # semantic store
    return result
    # print("CACHE MISS → call", key) 
    
    # response = groq.chat.completions.create(**body)      # 2. forward to Groq
    # cache.set(key, json.dumps(response.model_dump()), ex=3600) 
    # return response.model_dump()                         # 3. return as a dict

