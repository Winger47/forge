import os
import json
import hashlib
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from openai import OpenAI
import redis
load_dotenv()

app = FastAPI()

groq = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1",
)
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
    print("CACHE MISS → call", key) 
    
    response = groq.chat.completions.create(**body)      # 2. forward to Groq
    cache.set(key, json.dumps(response.model_dump()), ex=3600) 
    return response.model_dump()                         # 3. return as a dict

