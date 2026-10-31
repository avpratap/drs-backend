"""OTP/abuse-counter hardening: a third party can't lock a phone's owner out of OTP login
(B4), fixed-window counters always carry a TTL (B5), and the account-erase step-up OTP
check counts per-IP failures like login."""
from conftest import auth, login

from app.core.redis import incr_window, redis_client
from app.services import otp as otp_service


async def _clear_cooldown(phone: str) -> None:
    await redis_client.delete(f"otp:cooldown:{phone}")


async def test_another_ip_exhausting_a_phone_does_not_block_its_owner():
    phone = "9000003001"
    for _ in range(otp_service.MAX_SENDS_PER_HOUR):  # attacker's IP uses up ITS budget
        assert await otp_service.request_otp(phone, ip="203.0.113.9")
        await _clear_cooldown(phone)
    # Attacker is capped (a code is outstanding, so: "use the latest code", not a send).
    try:
        await otp_service.request_otp(phone, ip="203.0.113.9")
        raise AssertionError("attacker should be capped")
    except otp_service.UseLatestCode:
        pass
    await _clear_cooldown(phone)
    # The owner, from their own network, still gets a fresh code.
    code = await otp_service.request_otp(phone, ip="198.51.100.7")
    assert code and await otp_service.verify_otp(phone, code, ip="198.51.100.7")


async def test_phone_ceiling_keeps_the_latest_code_valid(client, monkeypatch):
    monkeypatch.setattr(otp_service, "MAX_SENDS_PER_PHONE_HOUR", 2)
    phone = "9000003002"
    url = "/api/v1/auth/otp/request"
    assert (await client.post(url, json={"phone": phone})).status_code == 200
    await _clear_cooldown(phone)
    latest = (await client.post(url, json={"phone": phone})).json()["dev_otp"]
    await _clear_cooldown(phone)
    r = await client.post(url, json={"phone": phone})  # ceiling reached
    assert r.status_code == 200 and "dev_otp" not in r.json()
    assert "latest code" in r.json()["detail"]
    # The latest code still logs in (it was not replaced or invalidated).
    r = await client.post("/api/v1/auth/otp/verify", json={"phone": phone, "otp": latest})
    assert r.status_code == 200


async def test_capped_with_no_live_code_is_429(client, monkeypatch):
    monkeypatch.setattr(otp_service, "MAX_SENDS_PER_PHONE_HOUR", 1)
    phone = "9000003003"
    url = "/api/v1/auth/otp/request"
    assert (await client.post(url, json={"phone": phone})).status_code == 200
    await _clear_cooldown(phone)
    await redis_client.delete(f"otp:{phone}")  # the earlier code expired
    assert (await client.post(url, json={"phone": phone})).status_code == 429


async def test_incr_window_always_sets_a_ttl():
    key = "test:incr_window"
    await redis_client.delete(key)
    assert await incr_window(key, 100) == 1
    assert 0 < await redis_client.ttl(key) <= 100
    # A counter whose EXPIRE was lost (no TTL) is healed on the next increment instead of
    # living forever as a permanent lockout.
    await redis_client.set(key, 7)
    assert await redis_client.ttl(key) == -1
    assert await incr_window(key, 100) == 8
    assert 0 < await redis_client.ttl(key) <= 100


async def test_erase_step_up_counts_per_ip_failures(client, monkeypatch):
    monkeypatch.setattr(otp_service, "VERIFY_FAILS_PER_IP_CEILING", 1)
    h = auth(await login(client, "9000003004"))
    await redis_client.set("otp:vfail:127.0.0.1", 1)  # this IP already at the ceiling
    r = await client.post("/api/v1/account/erase", json={"otp": "123456"}, headers=h)
    assert r.status_code == 429
    assert (await client.get("/api/v1/auth/me", headers=h)).status_code == 200  # not erased
