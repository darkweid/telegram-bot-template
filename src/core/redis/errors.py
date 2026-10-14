import redis.exceptions as redis_exc

# A lost or unresponsive connection is an outage worth failing open on. Every
# other RedisError - a ResponseError from a malformed argument or a broken Lua
# script above all - is a programmer error: swallowing it would hide the bug
# and burn the degradation reporter's cooldown on a fake outage, muting the
# real one.
TRANSPORT_ERRORS = (redis_exc.ConnectionError, redis_exc.TimeoutError)
# The one ResponseError that is an outage: under noeviction a full Redis
# refuses writes with OOM, and failing every update over it would make a
# struggling instance the whole bot's problem.
CAPACITY_ERRORS = (redis_exc.OutOfMemoryError,)
FAIL_OPEN_ERRORS = TRANSPORT_ERRORS + CAPACITY_ERRORS
