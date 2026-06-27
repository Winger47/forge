# FORGE

**An agentic CLI built on a self-hosted LLM gateway.**

FORGE is a terminal AI agent that takes a natural-language goal and works toward it in a
loop — *think → act (use a tool) → observe → repeat* — until the task is done. Every model
call the agent makes is routed through a self-built **LLM gateway**: a separate HTTP service
that sits between the agent and the model providers. The agent is the *consumer*; the gateway
is the *substrate*.

This separation is the point. The agent only ever worries about *doing the task*. The gateway
owns everything about *talking to providers* — and is the single place where routing, caching,
failover, and cost control live (or will live, as later phases land).

---

## Architecture

```
   You type a goal
        │
        ▼
   ┌─────────────────────────────────────────────┐
   │  AGENT  (agent/agent.py)                      │
   │  loop: think → act → observe → repeat         │
   │  tools: read_file · write_file · run_shell    │
   │  guards: max-iterations · token budget        │
   └───────────────┬───────────────────────────────┘
                   │  HTTP  (OpenAI-compatible)
                   │  POST /v1/chat/completions
                   ▼
   ┌─────────────────────────────────────────────┐
   │  GATEWAY  (gateway/main.py)                    │
   │  FastAPI service · OpenAI-compatible           │
   │  forwards requests to the provider             │
   └───────────────┬───────────────────────────────┘
                   │
                   ▼
              Groq  (OpenAI-compatible API)
```

The agent depends on an `LLMClient` interface, not on any concrete provider. Swapping the
backend — direct provider, gateway, or a fake client for testing — is a one-line change in
`main()`; the agent loop never changes.

---

## How to run it

FORGE runs as two services. You'll need two terminals.

**1. Set up the environment**

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**2. Add your API key**

Create a `.env` file in the project root:

```
GROQ_API_KEY=your_key_here
```

(Get a free key at console.groq.com. `.env` is gitignored — never commit it.)

**3. Terminal 1 — start the gateway**

```bash
uvicorn gateway.main:app --port 8000
```

The gateway is now live at `http://127.0.0.1:8000` (interactive docs at `/docs`).

**4. Terminal 2 — run the agent**

```bash
source venv/bin/activate
python3 agent/agent.py
```

Then give it a goal:

```
goal> read agent/agent.py and tell me what it does
```

Watch both terminals: the agent loops through tool calls, and the gateway logs each
`POST /v1/chat/completions` as the agent's calls flow through it.

---

## How it works

The agent loop is a `while`/`for` loop around a model call. On each turn:

1. **Think** — the agent sends the conversation so far plus its tool definitions to the model.
2. **Decide** — if the model returns a final answer (no tool call), the loop ends. If it
   requests a tool, the agent runs it.
3. **Act** — the requested tool runs locally and returns an observation.
4. **Observe** — the observation is appended to the conversation, and the loop repeats so the
   model can decide the next step with the new information.

The loop emits a stream of typed **events** (`status`, `tool_call`, `tool_result`, `cost`,
`text`) rather than printing directly. The terminal is just one subscriber to that stream,
which keeps the agent decoupled from any particular display.

---

## Project structure

```
forge/
├── agent/
│   └── agent.py        # the agent loop, tools, clients, event stream
├── gateway/
│   └── main.py         # the LLM gateway (FastAPI, OpenAI-compatible)
├── requirements.txt
├── .gitignore
└── README.md
```

---

## Roadmap

FORGE is built in phases; each ships something runnable.

| Phase | Status | What it adds |
|-------|--------|--------------|
| 0 | ✅ | Agent loop + tools (read/write/shell) |
| 1 | ✅ | Event stream + token cost guard |
| 2 | ✅ | `LLMClient` interface (dependency injection) |
| 3 | ✅ | Agent routes through the HTTP gateway |
| 4 | ◻️ | Gateway depth: semantic caching, failover, rate limiting |
| 5 | ◻️ | Agent depth: skills, tool permissioning |
| 6 | ◻️ | MCP client + connectors |
| 7 | ◻️ | Surfaces: TUI, chat UI, dashboard |
| 8 | ◻️ | Gateway as a deployable service with sessions |

---

## Design notes

- **Substrate + consumer.** The gateway is a proxy that decides nothing; the agent is the
  decision-maker. Keeping them separate means the agent stays simple and the gateway can grow
  smarter without the agent noticing.
- **OpenAI-compatible contract.** The gateway speaks the OpenAI `/v1/chat/completions` format,
  so any OpenAI client (not just this agent) can point at it by changing one base URL.
- **Guards by default.** The loop has a max-iteration cap and a token budget so it can't run
  away — tool failures are returned to the model as observations rather than crashing the loop.