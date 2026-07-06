# Pure token-bucket logic. No Redis, no I/O, no side effects.
# Kept import-light so tests can load it without dragging in heavy deps
# (sentence-transformers, openai, redis, etc. — see gateway/main.py).

# Token-bucket as an atomic Lua script — read, refill, consume, write happen as ONE
# indivisible operation, so concurrent requests can't both consume the last token.
BUCKET_LUA = """
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


def compute_bucket(tokens, ts, capacity, refill, now):
    """Pure token-bucket math. No Redis, no I/O, no side effects.
    Returns (allowed, new_tokens, new_ts). Deterministic and instantly testable."""
    if tokens is None:                      # first time this client is seen
        tokens, ts = capacity, now
    tokens = min(capacity, tokens + max(0, now - ts) * refill)
    allowed = tokens >= 1
    if allowed:
        tokens -= 1
    return allowed, tokens, now
