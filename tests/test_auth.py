from conftest import login


async def test_otp_login_returns_tokens(client):
    r = await client.post("/api/v1/auth/otp/request", json={"phone": "9876543210"})
    assert r.status_code == 200
    otp = r.json()["dev_otp"]
    r = await client.post("/api/v1/auth/otp/verify", json={"phone": "9876543210", "otp": otp})
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"] and body["refresh_token"]


async def test_bad_otp_rejected(client):
    real = (await client.post("/api/v1/auth/otp/request", json={"phone": "9876543210"})).json()["dev_otp"]
    wrong = f"{(int(real) + 1) % 10**6:06d}"  # guaranteed != the real OTP (a fixed guess wasn't)
    r = await client.post("/api/v1/auth/otp/verify", json={"phone": "9876543210", "otp": wrong})
    assert r.status_code == 401
    # The real code still works afterwards (a wrong guess only costs one attempt).
    ok = await client.post("/api/v1/auth/otp/verify", json={"phone": "9876543210", "otp": real})
    assert ok.status_code == 200


async def test_role_from_db(client):
    # Seeded admin keeps its admin role through OTP login.
    token = await login(client, "9999900001")
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["role"] == "admin"
