# FORGE

**An agentic coding CLI built on a self-hosted LLM gateway.**

FORGE takes a natural-language goal and works toward it in a loop — *think → act (use a tool) → observe → repeat* — until the task is done. Every model call is routed through a self-built **LLM gateway**: a separate HTTP service handling caching, failover, rate limiting, and streaming.

The agent is the *consumer*; the gateway is the *substrate*. Two processes, one contract (OpenAI-compatible HTTP). The agent's loop never changed as the gateway grew from a passthrough into a caching, rate-limited, streaming service beneath it.

---

## Highlights

- **Real token streaming** — responses stream character-by-character, including reassembly of tool calls that arrive as fragments across chunks.
- **Layered caching** — exact-match (Redis hash) in front, semantic (embeddings + cosine similarity) behind. Rephrased questions hit the cache; the expensive embedding only runs on an exact miss. Semantic hits are scoped to the request's model and tools, so one model's answer is never served for another's question. The vector store is a single normalized matrix (one matrix-vector product per lookup) with a bounded, FIFO-evicted size.
- **Non-blocking gateway** — the async request handler offloads every blocking step (Redis, embedding, provider call) to a threadpool, so concurrent requests overlap instead of serializing behind one another.
- **Model failover, including on the stream** — a failing provider fails over to the next model in the chain up to the first token; a mid-stream failure is surfaced as an explicit error event, never a silent truncation. The caller's chosen model is tried first.
- **MCP client** — connects to external tool servers declared in config. Tools are discovered at runtime, merged into the local registry, and routed to whichever server owns them.
- **Permission model** — dangerous tools require approval; foreign MCP tools are default-deny unless explicitly allowlisted; a denial holds for the whole run regardless of how the model varies its arguments.
- **Context compaction** — long conversations are summarized (triggered by an estimated-token budget) rather than growing until they blow the context window.
- **Session persistence** — every turn auto-saves; `/resume` picks up the last session, and named sessions can be saved and loaded.
- **Runtime model switching** — `/model <name>` swaps the model mid-session; the current model shows in the header and after each turn.
- **Extensible tools** — one decorated function adds a tool; its JSON schema is generated from the signature and docstring.

---

## Architecture

```
  You type a goal
        |
        v
  +----------------------------------------------+        +----------------------+
  |  AGENT  (agent/)                              |  MCP   |  fetch server        |
  |  loop: think -> act -> observe                | <----> |  time server         |
  |  8 local tools + discovered MCP tools         | stdio  |  (separate processes)|
  |  streaming . memory . compaction . approval   |        +----------------------+
  +---------------------+------------------------+
                        |  HTTP (OpenAI-compatible)
                        |  POST /v1/chat/completions
                        v
  +---------------------------------------------------+
  |  GATEWAY  (gateway/)                              |
  |  async handler; blocking steps -> threadpool      |
  |  0. rate limit     (token bucket, Redis Lua)      |
  |  1. streaming?     -> SSE + failover, no cache    |
  |  2. exact cache    (Redis, hashed body)           |
  |  3. semantic cache (scoped, vectorized matrix)    |
  |  4. failover       (requested model, then chain)  |
  +---------------------------------------------------+
                        v
                    Groq (OpenAI-compatible API)
```

---

## The gateway

**Concurrency.** The handler is `async`, but every expensive step it makes — Redis I/O, embedding, the provider HTTP call — is a blocking, synchronous call. A blocking call inside a coroutine stalls the whole event loop, so those steps are offloaded with `run_in_threadpool`; concurrent requests then overlap instead of serializing behind one slow provider call. (The streaming path already runs off-loop: Starlette iterates a sync streaming body in a threadpool.)

**Rate limiting.** A token bucket per client, implemented as a **Redis Lua script**. The refill-check-decrement is a read-modify-write; done as separate commands it races under concurrency and two requests consume the same last token. Lua executes atomically inside Redis, so the limit actually holds. The clock is sourced from Redis's own `TIME` inside the script, not passed in by the caller — each worker has its own wall clock, and any skew between them corrupts the refill math. The arithmetic is also factored into a pure Python function so it's unit tested without Redis. Fails *open* — if Redis is down, requests are allowed rather than blocked.

**Caching, in two tiers.** The exact cache hashes the request body (`sort_keys=True` for deterministic serialization) and does one Redis lookup — cheap, and it short-circuits before the embedder ever runs. Only on an exact miss does the semantic tier embed the prompt and compare it against stored vectors by cosine similarity above a threshold, catching rephrasings that exact matching misses. A semantic hit must also match the request's **scope** — a hash of `(model, tools)` — because the embedding captures only the prompt text; the same words asked of a different model, or with a different toolset, are a different question and must not share an answer. Vectors live in one L2-normalized matrix, so a lookup is a single matrix-vector product rather than a per-entry loop, and the store is bounded with FIFO eviction so a long-running process can't leak memory.

**Cache control.** `Cache-Control: no-cache` bypasses the read path while still refreshing the stored entry. `POST /v1/cache/invalidate` removes a poisoned entry from both tiers (semantic removal is scoped, so invalidating one model's answer doesn't wipe another's).

**Streaming.** Requests with `stream=True` take a separate SSE path that forwards provider chunks straight through and skips caching entirely. It fails over across models up to the first token — after that the bytes are already sent and can't be retried, so a mid-stream failure is emitted as an explicit `error` event instead of a bare `[DONE]`, which is what previously made a real outage look to the agent like an empty response.

**Failover.** On a real miss, the gateway walks a model chain until one succeeds. The caller's requested model is tried first (honoring an agent-side `/model` choice), then the configured fallback chain — the same ordering on both the streaming and non-streaming paths.

---

## The agent

**Tool registry.** A `@tool()` decorator registers a function and generates its JSON schema from the signature and type hints, with the docstring as the description. Adding a tool is one decorated block — no schema written by hand, no registry edited.

Eight local tools: `read_file`, `write_file`, `run_shell`, `list_files`, `search_files`, `edit_file`, `calculate`, `current_time`. `calculate` uses AST parsing with an operator allowlist rather than `eval`, which on model-generated input would be a remote-code-execution hole.

**Lean system prompt with environment context.** The prompt states the job in a few sentences and injects ambient facts — working directory, project name, date, git branch, a one-line project summary — so the model doesn't burn tool calls rediscovering where it is. Behaviors that are *enforced in code* (don't-repeat, denials) are deliberately **not** restated in the prompt; guards live in code, not prose.

**Bounded tool results.** The full result of a tool goes into the model's context, not just what's shown on screen — so a single large `read_file` could blow the window. Results are capped before they enter the conversation and prefixed with a header line (`[read_file · 640 lines · 8000 chars · path=… · TRUNCATED …]`) that tells the model at a glance what it got and how to ask for more.

**Streaming with tool-call reassembly.** Text chunks display immediately, but tool calls arrive as *fragments* — the name split across chunks, the arguments as partial JSON. These are accumulated by call index and stitched back together before execution, since a partial tool call is unparseable and unrunnable. If the gateway surfaces a provider failure as a stream error, the loop turns it into a clean aborted turn rather than a traceback.

**Multi-turn memory and compaction.** The conversation is owned by the session and passed into the loop, so it survives across goals. Compaction triggers on an **estimated-token budget** (not a raw message count — one 8KB tool result is more urgent than forty short messages): older turns are summarized into a single message while the system prompt and recent turns stay verbatim. The summarizer is told to preserve facts the model may need again (goals, paths, commands, tool results, errors) and drop reasoning chatter — the context window treated as a finite cache with an eviction policy.

**Session persistence.** Every completed turn auto-saves to `~/.forge/sessions/last.json`; `/resume` restores it (model included), and `/save <name>` / `/load <name>` manage named checkpoints. A startup hint shows when a previous session is waiting.

**Runtime model switching.** `/model` lists the current and known models; `/model <name>` switches for the rest of the session. Any name is accepted — the known list is a hint, not a validation gate, so the CLI doesn't rot as the provider adds models. The active model shows in the header and after each turn.

**Approval gate.** Tools with side effects require confirmation. `edit_file` shows a colored unified diff of the exact change before you approve it. A denial is recorded **per tool for the whole run**, not per call.

**Loop termination.** If the model requests the same call twice, the loop re-invokes it with an *empty* tool list, making a tool call physically impossible and forcing a text answer.

**Terminal UI.** A quiet, minimal TUI: a small header with tool counts, model, and a live gateway health indicator; one-line tool calls with collapsed args; short results inlined and long ones panelled; a spinner over the think latency; `Ctrl-C` aborts the current turn without killing the session. The agent still only *yields events* — a single presentation layer decides how they look, so the loop is unchanged.

---

## MCP (external tools)

FORGE speaks the Model Context Protocol as a client. Servers are declared in `forge.toml`:

```toml
[mcp.fetch]
command = "python"
args = ["-m", "mcp_server_fetch"]
safe_tools = ["fetch"]     # reviewed as read-only; anything unlisted is gated

[mcp.time]
command = "python"
args = ["-m", "mcp_server_time"]
safe_tools = ["get_current_time", "convert_time"]
```

At startup FORGE spawns each server as a subprocess, speaks JSON-RPC over stdio, asks each what tools it has (`tools/list`), and merges the results into one registry that remembers which server owns each tool. The model sees a single flat list and can't tell local tools from foreign ones.

**Adding an entire external toolset is four lines of config. No Python changes.**

Collisions are handled explicitly and loudly: a foreign tool that shadows a local one is skipped with a warning (local tools are code you wrote; untrusted code never silently replaces trusted code), and between two servers the first to claim a name keeps it.

A server that fails to start is skipped — an external dependency dying costs a capability, not the agent.

---

## Quickstart

**Prerequisites**

```bash
brew install redis
brew services start redis
redis-cli ping        # PONG
```

**Install**

```bash
python3 -m venv venv          # Python 3.10+ required (MCP SDK)
source venv/bin/activate
pip install -e .
pip install mcp-server-fetch mcp-server-time    # optional MCP servers
```

**Configure**

```bash
echo "GROQ_API_KEY=your_key_here" > .env        # free key at console.groq.com
```

**Run** — two terminals:

```bash
uvicorn gateway.main:app --port 8000     # terminal 1
forge                                    # terminal 2
```

```
▸ read agent/agent.py and tell me what it does
▸ fetch https://example.com and summarize it
▸ /model llama-3.1-8b-instant     # switch model mid-session
▸ /tools                          # list local + MCP tools
▸ /resume                         # continue the last session
▸ /help                           # all commands
▸ /exit
```

Watch both terminals — the gateway logs cache hits, misses, streaming, and failover as calls flow through.

**Tests**

```bash
pytest tests/ -v      # 9 tests, no network required
```

---

## Project structure

```
forge/
|-- agent/
|   |-- agent.py        # loop, clients, streaming, compaction, sessions, model switch, TUI
|   |-- tools.py        # @tool registry + 8 local tools
|   \-- mcp_client.py   # MCP client: stdio transport, discovery, invocation
|-- gateway/
|   |-- main.py         # rate limit -> streaming -> cache -> semantic -> failover
|   \-- rate_limit.py   # token bucket (pure logic + Redis Lua script)
|-- tests/
|-- forge.toml          # MCP server declarations
\-- pyproject.toml      # packaging + `forge` entry point
```

---

## Design decisions

**Streaming and caching are incompatible.** A cache stores a complete response; a stream is a flow of chunks with no single response to store. Rather than force them together, streaming gets its own SSE path that bypasses the cache entirely. Two request types, two code paths.

**Not everything should be cached.** Context-dependent generations — forced final answers, conversation summaries — must bypass the cache. Their prompts are near-identical across completely different conversations, so the semantic cache matched them and served a file listing as the answer to a math question. The cache key didn't capture what actually determined the answer.

**A semantic hit needs more than matching words.** The embedding only encodes the prompt text, but the *answer* also depends on which model produced it and which tools were available. Once a `/model` switch existed, the same question on a small model could be served the large model's cached answer. Semantic entries are now scoped by `(model, tools)`; the vector finds candidates, the scope decides eligibility.

**Async is a contract, not a decoration.** An `async` handler that makes blocking calls is worse than an honest sync one — it looks concurrent but the event loop is frozen for the whole request, so everyone serializes anyway. Rather than a full async rewrite, the blocking steps are pushed to a threadpool: the loop stays free and requests actually overlap, verified by two slow requests finishing in the time of one.

**Guards live in code, not prompts.** A determined model ignores "don't retry" instructions — verified across three prompt rewrites. Loop termination works by removing the *ability* to call a tool, not by asking nicely.

**Denials are per-action, not per-call.** The repeat guard originally keyed on name + arguments. When a denial came back, the model retried with one extra argument — a different signature, so it slipped through and prompted again. Not malice: to a model, "denied" looks like any other failed call, and varying parameters is a reasonable retry. Denials now key on the tool name and hold for the run.

**Foreign tools are default-deny, but not everything is gated.** MCP tools carry no danger classification, so unknown provenance means unknown risk. But gating *every* MCP tool would be worse than useless — approval fatigue trains you to rubber-stamp, and a gate you always approve isn't a gate. Hence an explicit per-server allowlist.

**Local tools beat a shell tool.** `run_shell` could technically do everything — `curl`, `ls`, `cat`. But its capability set is *every program on the machine*, so it must be gated on every use. A narrow tool has a bounded blast radius, can safely skip the gate, and returns structured output instead of raw text the model has to parse. The shell stays as an escape hatch for the long tail.

---

## Known limitations

- **The semantic store is in-memory.** Vectors live in a normalized matrix inside the gateway process — now bounded (FIFO eviction) and searched in one matrix-vector product, but still lost on restart and not shared across replicas. The remaining fix for persistence/sharing is Redis vector search or pgvector.
- **Gateway concurrency is threadpool-based, not fully async.** Blocking steps are offloaded with `run_in_threadpool` so the event loop stays free; this is bounded by the threadpool size rather than truly async I/O. Good enough at this scale; a full rewrite would use `AsyncOpenAI` + `redis.asyncio`.
- **Streaming failover only covers pre-first-token failures.** Once a chunk is on the wire the bytes can't be un-sent, so a mid-stream provider failure is reported as an error event, not retried. That's a property of streaming, not an implementation gap.
- **The MCP client re-spawns a server per call.** Bridging a long-lived async session into a synchronous agent loop is awkward, so each call spawns, uses, and tears down. Costs process startup per tool call; the clean fix is a persistent session, which really wants an async core.
- **The agent core is synchronous.** MCP's SDK is async, bridged with `asyncio.run()` per call. A deliberate deferral, not an oversight.
- **`run_shell` executes model-generated strings** with the user's permissions. The approval gate is a *control*, not a *sandbox* — real isolation would mean containerizing tool execution.
- **No CI.** Tests are green but not enforced on push.

---

## Status

A learning build that works. Every feature was built, broken, debugged, and verified running — not scaffolded. The gateway and agent are separate processes; the agent's loop is unchanged from when the gateway was a bare passthrough.