from redis.asyncio import Redis

from app.core.config import settings

# socket timeouts so a black-holed/unreachable Redis FAILS FAST (the cache/ratelimit
# fail-open guards catch the error) instead of hanging every request on a blocked socket.
redis_client: Redis = Redis.from_url(
    settings.REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=2,
    health_check_interval=30,
)


# INCR and, only if the key has no TTL yet, EXPIRE — atomically (one server-side script).
# The two-call idiom `if incr()==1: expire()` left a counter with NO TTL whenever the
# EXPIRE was lost (timeout/crash between the calls): a permanent lockout. Checking for a
# missing TTL (not n==1) also heals a counter that was already left without one.
_INCR_WINDOW_LUA = """
local n = redis.call('INCR', KEYS[1])
if redis.call('TTL', KEYS[1]) < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return n
"""


async def incr_window(key: str, ttl_seconds: int) -> int:
    """Fixed-window counter increment; the window's TTL is always set."""
    return int(await redis_client.eval(_INCR_WINDOW_LUA, 1, key, ttl_seconds))
