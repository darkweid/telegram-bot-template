# Fixed window: the first call sets the counter with the window as its TTL,
# the following ones increment it, and a call past the limit learns the TTL.
# 0 means admitted; PTTL reads 0 in the key's last millisecond (and -1 on a key
# whose TTL was removed), so a refusal always answers at least 1.
RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])

local current = tonumber(redis.call('GET', key) or '0')
if current > 0 then
  if current + 1 > limit then
    local ttl = redis.call('PTTL', key)
    if ttl < 1 then
      ttl = 1
    end
    return ttl
  end
  redis.call('INCR', key)
  return 0
end
redis.call('SET', key, 1, 'PX', window_ms)
return 0
"""
