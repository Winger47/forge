# FORGE

**An agentic CLI built on a self-hosted LLM gateway.**

FORGE is a terminal AI agent that takes a natural-language goal and works toward it in a
loop — *think -> act (use a tool) -> observe -> repeat* — until the task is done. Every model
call the agent makes is routed through a self-built **LLM gateway**: a separate HTTP service
that sits between the agent and the model providers, and handles **semantic caching,
provider failover, and cost control**.

The agent is the *consumer*; the gateway is the *substrate*. The agent only worries about
*doing the task*; the gateway owns everything about *talking to providers*.

---

## Highlights

- **Semantic caching** — repeated *or reworded* questions are served from cache without
  calling the model. "capital of France?" and "tell me France's capital" resolve to the same
  cached answer via embedding similarity.
- **Provider failover** — if the primary model fails, the gateway transparently falls over to
  a backup. The agent never notices.
- **Agentic loop with guards** — iteration cap, token budget, and repeated-call detection so
  the agent can't run away or get stuck retrying a failing tool.
- **OpenAI-compatible gateway** — any OpenAI client can point at it by changing one base URL.

---

## Architecture

```
   You type a goal
        |
        v
   +---------------------------------------------+
   |  AGENT  (agent/agent.py)                     |
   |  loop: think -> act -> observe -> repeat     |
   |  tools: read_file . write_file . run_shell   |
   |  guards: max-iterations . token budget .     |
   |          repeated-call detection             |
   +---------------+-----------------------------+
                   |  HTTP  (OpenAI-compatible)
                   |  POST /v1/chat/completions
                   v
   +---------------------------------------------+
   |  GATEWAY  (gateway/main.py)                  |
   |  1. exact cache      (Redis, hash key)       |
   |  2. semantic cache   (embeddings + cosine)   |
   |  3. failover chain   (primary -> backup)     |
   +---------------+-----------------------------+
                   |
                   v
              Groq  (OpenAI-compatible API)
```

Every request the gateway receives flows through three tiers: an exact-match cache (instant
hash lookup), a semantic cache (embed the prompt, return a cached answer if a stored one is
similar enough), and — only on a real miss — the provider chain with failover.

---

## How it works

**The agent loop.** On each turn the agent sends the conversation so far plus its tool
definitions to the model. If the model returns a final answer, the loop ends. If it requests a
tool, the agent runs it, appends the result as an observation, and loops so the model can
decide the next step. The loop emits a stream of typed **events** (`status`, `tool_call`,
`tool_result`, `cost`, `text`) rather than printing directly, keeping the agent decoupled from
any display.

**The gateway's three tiers.**
1. **Exact cache** — the request is hashed into a key; an identical prior request returns
   instantly from Redis.
2. **Semantic cache** — the prompt is embedded into a vector; if a stored vector is above a
   similarity threshold (cosine similarity), its cached answer is returned. This catches
   *rephrasings* that exact matching misses.
3. **Failover** — on a real miss, the gateway tries each model in a chain until one succeeds,
   so a single provider failing doesn't break the request.

**Guards.** The agent has a max-iteration cap, a token budget, and repeated-call detection —
if the model requests the same failing command twice, the loop refuses to run it again and
pushes the model to conclude. Tool failures are returned to the model as observations rather
than crashing the loop.

---

## How to run it

FORGE runs as two services. You'll need two terminals, plus Redis running locally.

**1. Prerequisites**

```bash
# Redis (macOS)
brew install redis
brew services start redis
redis-cli ping        # should return PONG
```

**2. Set up Python**

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**3. Add your API key**

Create a `.env` file in the project root:

```
GROQ_API_KEY=your_key_here
```

(Get a free key at console.groq.com. `.env` is gitignored — never commit it.)

**4. Terminal 1 — start the gateway**

```bash
uvicorn gateway.main:app --port 8000
```

Live at `http://127.0.0.1:8000` (interactive docs at `/docs`).

**5. Terminal 2 — run the agent**

```bash
source venv/bin/activate
python3 agent/agent.py
```

Then give it a goal:

```
goal> read agent/agent.py and tell me what it does
```

Watch both terminals: the agent loops through tool calls, and the gateway logs cache
hits/misses and failover decisions as calls flow through it.

---

## Project structure

```
forge/
|-- agent/
|   \-- agent.py        # agent loop, tools, clients, event stream, guards
|-- gateway/
|   \-- main.py         # LLM gateway: caching, semantic cache, failover
|-- requirements.txt
|-- .gitignore
\-- README.md
```

---

## Roadmap

Built in phases; each ships something runnable.

| Phase | Status | What it adds |
|-------|--------|--------------|
| 0 | done | Agent loop + tools (read/write/shell) |
| 1 | done | Event stream + token cost guard |
| 2 | done | `LLMClient` interface (dependency injection) |
| 3 | done | Agent routes through the HTTP gateway |
| 4a | done | Exact-match Redis cache |
| 4b | done | Semantic cache (embeddings + cosine similarity) |
| 4c | done | Provider failover chain |
| 4d | planned | Per-client rate limiting (token bucket) |
| 5 | planned | Agent depth: skills, tool permissioning |
| 6 | planned | MCP client + connectors |
| 7 | planned | Surfaces: TUI, chat UI, dashboard |
| 8 | planned | Gateway as a deployable service with sessions |

---

## Design notes

- **Substrate + consumer.** The gateway is a proxy that decides nothing; the agent is the
  decision-maker. Keeping them separate lets the gateway grow smarter without the agent
  noticing — the agent's code never changed as caching and failover were added beneath it.
- **Semantic cache tradeoff.** The similarity threshold balances catching rephrasings against
  false positives (serving a cached answer to a genuinely different question). It is scoped and
  tuned rather than set loosely.
- **Guards enforced in code, not prompts.** A determined model will ignore a "don't retry"
  instruction — so loop-breaking and budget limits are enforced in the loop itself, with the
  system prompt as a secondary nudge.
- **Trust boundary.** `run_shell` executes model-chosen commands with the user's permissions —
  powerful, but a real risk surface (mistakes, prompt injection). A confirmation gate is
  planned for Phase 5.