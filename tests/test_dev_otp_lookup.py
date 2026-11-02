"""Dev/staging-only OTP lookup: secret-gated, returns the live code, and is INVISIBLE in
prod (every path 404s and the plaintext is never stored)."""
from conftest import auth, login

from app.core.config import settings
from app.services import otp as otp_service

TOKEN = "dev-lookup-secret-xyz"
PAGE = "/api/v1/auth/dev/otp"


async def test_lookup_returns_live_code(client, monkeypatch):
    monkeypatch.setattr(settings, "DEV_OTP_TOKEN", TOKEN)
    phone = "9000005001"
    sent = (await client.post("/api/v1/auth/otp/request", json={"phone": phone})).json()["dev_otp"]

    r = await client.get(f"{PAGE}/{phone}", headers={"X-Dev-Token": TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert body["otp"] == sent and body["phone"] == phone and body["ttl_seconds"] > 0

    # The looked-up code actually logs in (it is the real outstanding OTP).
    tok = (await client.post("/api/v1/auth/otp/verify",
                             json={"phone": phone, "otp": body["otp"]})).json()
    assert "access_token" in tok


async def test_wrong_or_missing_token_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "DEV_OTP_TOKEN", TOKEN)
    phone = "9000005002"
    await client.post("/api/v1/auth/otp/request", json={"phone": phone})
    assert (await client.get(f"{PAGE}/{phone}", headers={"X-Dev-Token": "nope"})).status_code == 401
    assert (await client.get(f"{PAGE}/{phone}")).status_code == 401


async def test_no_active_code_is_404(client, monkeypatch):
    monkeypatch.setattr(settings, "DEV_OTP_TOKEN", TOKEN)
    # Never requested an OTP for this number -> nothing to peek.
    r = await client.get(f"{PAGE}/9000005003", headers={"X-Dev-Token": TOKEN})
    assert r.status_code == 404


async def test_disabled_when_token_unset(client, monkeypatch):
    monkeypatch.setattr(settings, "DEV_OTP_TOKEN", "")  # feature off -> looks absent
    phone = "9000005004"
    await client.post("/api/v1/auth/otp/request", json={"phone": phone})
    assert (await client.get(f"{PAGE}/{phone}", headers={"X-Dev-Token": "x"})).status_code == 404
    assert (await client.get(PAGE)).status_code == 404


async def test_invisible_and_inert_in_prod(client, monkeypatch):
    monkeypatch.setattr(settings, "DEV_OTP_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "ENV", "prod")
    phone = "9000005005"
    # In prod the plaintext is never stashed...
    await client.post("/api/v1/auth/otp/request", json={"phone": phone})
    assert await otp_service.peek_otp(phone) == (None, 0)
    # ...and every path of the feature 404s.
    assert (await client.get(f"{PAGE}/{phone}", headers={"X-Dev-Token": TOKEN})).status_code == 404
    assert (await client.get(PAGE)).status_code == 404


async def test_page_served_when_enabled(client, monkeypatch):
    monkeypatch.setattr(settings, "DEV_OTP_TOKEN", TOKEN)
    r = await client.get(PAGE)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "OTP lookup" in r.text
