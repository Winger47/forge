# gateway/main.py
import os
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

app = FastAPI()

groq = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1",
)

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()                          # 1. read what the agent sent
    response = groq.chat.completions.create(**body)      # 2. forward to Groq
    return response.model_dump()                         # 3. return as a dict