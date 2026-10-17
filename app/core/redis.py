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
