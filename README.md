# FORGE

**An agentic coding CLI built on a self-hosted LLM gateway.**

FORGE takes a natural-language goal and works toward it in a loop — *think → act (use a tool) → observe → repeat* — until the task is done. Every model call is routed through a self-built **LLM gateway**: a separate HTTP service handling caching, rate limiting, circuit breaking, failover, streaming, and cost metering.

The agent is the *consumer*; the gateway is the *substrate*. Two processes, one contract (OpenAI-compatible HTTP). The agent depends only on an `LLMClient` protocol and consumes a typed event stream, so the loop never changed as the gateway grew from a passthrough into the service below.

---

## Highlights

**Gateway**
- **Layered caching** — exact-match (Redis hash) in front, semantic (embeddings + cosine similarity) behind, scoped by `(model, tools)` so one model's answer is never served for another's question.
- **Circuit breaker per model** — CLOSED → OPEN → HALF_OPEN state machine; a model that keeps failing is skipped instantly instead of being retried on every request.
- **Failover with backoff** — per-model retries with exponential backoff + jitter, then the next model in the chain. Works on the streaming path up to the first token.
- **Atomic rate limiting** — per-client token bucket as a Redis Lua script, clock sourced from Redis to avoid cross-worker skew.
- **Non-blocking** — the async handler offloads every blocking step to a threadpool, so concurrent requests overlap.
- **Cost ledger + dashboard** — every provider call writes one immutable Postgres row (tokens, cost, latency, cache status); `/v1/metrics` and `/dashboard` are read models over it (cache-hit rate, $/model, p95 latency).

**Agent**
- **5-mode approval engine** — `on-request`, `on-failure`, `auto`, `never`, `yolo`, with an always-on dangerous-command / path-escape scan that can escalate but never de-escalate.
- **Sub-agents** — delegate read-heavy exploration to a read-only child with its own isolated context; only the final answer returns.
- **Loop detection** — fingerprints `(tool, args, result)` in a sliding window, catching repeats and short cycles (A, B, A, B) before the iteration budget burns.
- **MCP client** — stdio, SSE, and streamable-HTTP transports; tools discovered at runtime, foreign tools default-deny unless allowlisted.
- **Plugin host** — user tools in `~/.forge/tools/` load with fail-isolation (a tool that crashes on import is rolled back and skipped); lifecycle hooks (`before_tool` can veto).
- **On-demand skills** — markdown instructions loaded into context only when the model asks for them.
- **Streaming, compaction, sessions, runtime model switching** — see [The agent](#the-agent).

---

## Architecture

```
  You type a goal
        |
        v
  +------------------------------------------------+      +------------------------+
  |  AGENT  (agent/)                                | MCP  |  fetch / time (stdio)  |
  |  loop: think -> act -> observe                  |<---->|  github (HTTP)         |
  |  approval engine . loop detector . sub-agents   |      +------------------------+
  |  hooks . plugin host . skills . compaction      |
  +---------------------+--------------------------+
                        |  HTTP (OpenAI-compatible)
                        |  POST /v1/chat/completions
                        v
  +---------------------------------------------------+      +------------------+
  |  GATEWAY  (gateway/)                              |      |  PostgreSQL      |
  |  async handler; blocking steps -> threadpool      |----->|  append-only     |
  |  0. rate limit      (token bucket, Redis Lua)     | write|  cost ledger     |
  |  1. streaming?      -> SSE + failover, no cache   |      +--------+---------+
  |  2. exact cache     (Redis, hashed body)          |               | read
  |  3. semantic cache  (scoped, vectorized matrix)   |      /v1/metrics, /dashboard
  |  4. breaker + retry/backoff + failover chain      |
  +---------------------------------------------------+
                        v
                  Groq (OpenAI-compatible API)
```

---

## The gateway

**Concurrency.** The handler is `async`, but every expensive step — Redis I/O, embedding, the provider HTTP call — is a blocking call. A blocking call inside a coroutine stalls the whole event loop, so those steps are offloaded with `run_in_threadpool`; concurrent requests then overlap instead of serializing behind one slow provider call.

**Rate limiting.** A token bucket per client, implemented as a **Redis Lua script**. Refill-check-decrement is a read-modify-write; as separate commands it races and two requests consume the same last token. Lua runs atomically inside Redis. The clock comes from Redis's own `TIME`, not the caller — worker clocks skew, and skew corrupts the refill math. The arithmetic is also a pure Python function, unit tested without Redis. Fails *open*: if Redis is down, requests are allowed.

**Caching, in two tiers.** The exact cache hashes the request body (`sort_keys=True`) and does one Redis lookup, short-circuiting before the embedder runs. Only on an exact miss does the semantic tier embed the prompt and compare by cosine similarity. A semantic hit must also match the request's **scope** — a hash of `(model, tools)` — because the embedding captures only the prompt text. Vectors live in one L2-normalized matrix (one matrix-vector product per lookup), bounded with FIFO eviction.

**Cache control.** `Cache-Control: no-cache` bypasses the read path while refreshing the entry. `POST /v1/cache/invalidate` removes a poisoned entry from both tiers.

**Circuit breaker.** One breaker per model (`gateway/circuit_breaker.py`). Three consecutive failures trip it OPEN; while OPEN, the gateway skips that model instantly and fails over, instead of spending a round-trip on a dependency it already knows is down. After a 15s cooldown it goes HALF_OPEN and lets exactly one probe through — success closes it, failure re-opens it. The clock is injectable, so transitions are tested without sleeping.

**Failover.** Each model gets bounded retries with exponential backoff plus jitter (without jitter, clients that failed together retry together and stampede the recovering provider). Then the gateway walks the chain: the caller's requested model first, then the configured fallbacks — same ordering on streaming and non-streaming paths.

**Streaming.** `stream=True` takes a separate SSE path that skips caching. It fails over up to the first token; after that the bytes are sent, so a mid-stream failure is emitted as an explicit `error` event instead of a bare `[DONE]` — which previously made a real outage look like an empty response.

**Cost ledger.** Every call that reaches a provider writes one row to an append-only Postgres table: client, model that actually answered, prompt/completion tokens, cost (priced at write time), latency, cache status. We INSERT; we never UPDATE or DELETE. The write is on the request path but **non-fatal** — if Postgres is down the request still succeeds. That's a deliberate availability-over-accounting choice: a dropped row is a gap in the ledger, not a failed request.

**Read models.** `/v1/metrics` returns totals, $/model, cache-hit rate, and p95 latency in one query; `/v1/ledger` returns recent rows; `/dashboard` renders both. The write path never reads these back (CQRS-lite).

---

## The agent

**Provider-agnostic core.** The loop depends on an `LLMClient` protocol and yields typed events; it never prints. Provider message shapes stop at the client, so swapping the gateway underneath never touched the loop.

**Tool registry.** A `@tool()` decorator registers a function, generates its JSON schema from the signature and docstring, and tags it with a `ToolKind` (read, write, shell, network, memory, MCP, meta). Eight local tools: `read_file`, `write_file`, `run_shell`, `list_files`, `search_files`, `edit_file`, `calculate`, `current_time`. `calculate` uses AST parsing with an operator allowlist rather than `eval`.

**Approval engine.** Policy is separate from the loop: the loop asks `decide(...)` and gets RUN, PROMPT, or DENY.

| Mode | Dangerous tools |
|---|---|
| `on-request` (default) | prompt |
| `on-failure` | run; prompt before retrying an action that already failed |
| `auto` | run |
| `never` | deny (read-only sessions) |
| `yolo` | run, and skip the safety scan |

In every mode except `yolo`, a scan for dangerous commands (`rm -rf`, `git reset --hard`, `curl | sh`, `sudo`, ...) and path escapes (`../`, `/etc`, ...) can escalate a RUN to a PROMPT, so `auto` can't silently wipe a disk. `edit_file` shows a colored unified diff before approval. Denials hold per tool for the whole run.

**Loop detection.** Two guards. An immediate-repeat guard re-invokes the model with an *empty* tool list, making a tool call impossible. A `LoopDetector` fingerprints `(tool, args, result)` over a sliding window. Including the *result* is the point: the same call returning different results is progress; the same call returning the same result is spinning.

**Sub-agents.** `spawn_subagent(task)` runs a child agent with its own message history in `never` mode (read-only). Its exploration never enters the parent transcript or the screen; only its final answer comes back — delegation without blowing the parent's context window.

**Plugin host and hooks.** User tools in `~/.forge/tools/` are imported at startup with fail-load isolation: a tool that raises on import has anything it half-registered rolled back, and is skipped. Five lifecycle hooks (`before_run`, `after_run`, `before_tool`, `after_tool`, `on_error`); only `before_tool` can veto.

**Skills.** Markdown files with a name and one-line description (`skills/`, plus `~/.forge/skills/`). The model sees the list and pulls a body with `load_skill` only when it needs it, so the token cost is paid on the few turns that use it.

**Layered config.** defaults < `~/.forge/config.toml` < `./.forge/config.toml` < CLI, merged key by key and validated with Pydantic.

**Streaming with tool-call reassembly.** Tool calls arrive as fragments across chunks; they're accumulated by index and stitched together before execution.

**Compaction.** Triggered by an estimated-token budget, not message count. Older turns are summarized; the system prompt and recent turns stay verbatim.

**Sessions and model switching.** Every turn auto-saves; `/resume`, `/save <name>`, `/load <name>`. `/model <name>` switches mid-session.

---

## MCP (external tools)

Servers are declared in `forge.toml`:

```toml
[mcp.fetch]
command = "python"
args = ["-m", "mcp_server_fetch"]
safe_tools = ["fetch"]        # reviewed as read-only; everything else is gated

[mcp.github]
url = "https://api.githubcopilot.com/mcp/"
transport = "http"            # stdio | sse | http
safe_tools = ["get_issue", "list_issues", "get_pull_request", "search_repositories"]

[mcp.github.headers]
Authorization = "Bearer $GITHUB_PERSONAL_ACCESS_TOKEN"
```

FORGE discovers each server's tools (`tools/list`) and merges them into one registry that remembers which server owns each tool. **Adding an external toolset is config, not code.** A foreign tool that shadows a local one is skipped with a warning. A server that fails to start, or is missing its token, is skipped — a dead dependency costs a capability, not the agent.

---

## Quickstart

**Prerequisites:** Python 3.11+, Redis, PostgreSQL (optional — without it the ledger and dashboard are empty but requests still work).

```bash
brew install redis postgresql
brew services start redis
brew services start postgresql
createdb forge
```

**Install**

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e .
pip install pydantic mcp mcp-server-fetch mcp-server-time
```

**Configure**

```bash
echo "GROQ_API_KEY=your_key_here" > .env
# optional: DATABASE_URL=postgresql://localhost:5432/forge
# optional: GITHUB_PERSONAL_ACCESS_TOKEN=... (enables the GitHub MCP server)
```

**Run** — two terminals:

```bash
uvicorn gateway.main:app --port 8000     # terminal 1 — dashboard at http://127.0.0.1:8000/dashboard
forge                                    # terminal 2
```

```
▸ read agent/agent.py and tell me what it does
▸ fetch https://example.com and summarize it
▸ /model llama-3.1-8b-instant
▸ /tools
▸ /resume
▸ /help
```

**Tests and evals**

```bash
pytest tests/ -v           # 55 tests, no network required
python -m eval.runner      # golden-task evals (needs the gateway running)
```

The eval harness runs each golden task in an isolated temp directory and checks a **side effect** (a file exists with the right content), not the model's wording — the way to test a non-deterministic system.

---

## Project structure

```
forge/
|-- agent/
|   |-- agent.py          # loop, LLMClient, streaming, compaction, sessions, sub-agents, TUI
|   |-- tools.py          # @tool registry, ToolKind, 8 local tools, plugin host
|   |-- approval.py       # 5-mode approval policy + safety scan
|   |-- loop_detector.py  # (tool, args, result) fingerprinting
|   |-- hooks.py          # lifecycle hooks
|   |-- skills.py         # on-demand skills
|   |-- config.py         # layered TOML config
|   \-- mcp_client.py     # MCP client: stdio / SSE / HTTP
|-- gateway/
|   |-- main.py           # rate limit -> stream -> cache -> semantic -> breaker/failover
|   |-- rate_limit.py     # token bucket (pure logic + Redis Lua)
|   |-- circuit_breaker.py
|   |-- ledger.py         # append-only Postgres ledger + read models
|   \-- dashboard.html
|-- eval/                 # golden tasks + runner
|-- skills/               # builtin skills
|-- tests/                # 55 tests
\-- forge.toml            # MCP server declarations
```

---

## Design decisions

**Streaming and caching are incompatible.** A cache stores a complete response; a stream has no single response to store. Streaming gets its own path that bypasses the cache.

**Not everything should be cached.** Forced final answers and conversation summaries have near-identical prompts across unrelated conversations, so the semantic cache once served a file listing as the answer to a math question. Those generations now bypass the cache.

**A semantic hit needs more than matching words.** The embedding encodes only the prompt, but the answer also depends on the model and the tools. Semantic entries are scoped by `(model, tools)`: the vector finds candidates, the scope decides eligibility.

**Async is a contract, not a decoration.** An `async` handler making blocking calls is worse than an honest sync one — it looks concurrent while the loop is frozen. Blocking steps go to a threadpool, verified by two slow requests finishing in the time of one.

**Fail fast on a known-dead dependency.** Failover alone still pays a timeout on every request to a dead provider. The circuit breaker remembers the failure and skips it until a probe says it's back.

**Availability over perfect accounting.** The ledger write is non-fatal. Losing a metering row is better than failing a user's request because the database blipped.

**Guards live in code, not prompts.** A determined model ignores "don't retry" — verified across three prompt rewrites. Loop termination removes the *ability* to call a tool.

**Denials are per-action, not per-call.** Keyed on name + arguments, the model slipped past a denial by adding one argument. Denials now key on the tool name and hold for the run.

**Default-deny, but not gate-everything.** Gating every MCP tool causes approval fatigue, and a gate you always approve isn't a gate. Hence a per-server allowlist.

**Narrow tools beat a shell tool.** `run_shell` can do anything, so it must be gated on every use. A narrow tool has a bounded blast radius and returns structured output.

---

## Known limitations

- **The semantic store is in-memory** — bounded and vectorized, but lost on restart and not shared across replicas. The fix is Redis vector search or pgvector.
- **Breaker state is per-process** — multiple gateway replicas would each learn a failure separately. Shared state would move it to Redis.
- **Gateway concurrency is threadpool-based, not fully async.** A full rewrite would use `AsyncOpenAI` + `redis.asyncio`.
- **Streaming failover only covers pre-first-token failures** — a property of streaming, not an implementation gap.
- **The MCP client opens a session per call** (`asyncio.run()` per call) because the agent core is synchronous. The clean fix is a persistent session on an async core.
- **`run_shell` executes model-generated strings** with the user's permissions. The approval gate is a control, not a sandbox.
- **No CI yet.** Tests pass locally but aren't enforced on push.

---

## Status

A learning build that works. Every feature was built, broken, debugged, and verified running — not scaffolded.