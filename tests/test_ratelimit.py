from app.core.ratelimit import allow


async def test_fixed_window_trips_at_limit():
    ip = "203.0.113.77"  # unique to this test (redis is flushed per test anyway)
    limit = 5
    results = [await allow(ip, limit, window=60) for _ in range(limit + 2)]
    assert results[:limit] == [True] * limit  # first `limit` allowed
    assert results[limit] is False and results[limit + 1] is False  # then throttled


async def test_separate_ips_independent():
    assert await allow("198.51.100.1", 1, window=60) is True
    assert await allow("198.51.100.2", 1, window=60) is True  # different IP, own bucket
    assert await allow("198.51.100.1", 1, window=60) is False  # first IP now over
