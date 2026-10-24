async def _verify(client, phone):
    r = await client.post("/api/v1/auth/otp/request", json={"phone": phone})
    otp = r.json()["dev_otp"]
    r = await client.post("/api/v1/auth/otp/verify", json={"phone": phone, "otp": otp})
    return r.json()


async def test_refresh_rotation_and_reuse_detection(client):
    toks = await _verify(client, "9876543210")
    r1 = toks["refresh_token"]

    rotated = await client.post("/api/v1/auth/refresh", json={"refresh_token": r1})
    assert rotated.status_code == 200
    r2 = rotated.json()["refresh_token"]
    assert r2 != r1

    # Reusing the already-rotated token is treated as theft -> 401.
    reuse = await client.post("/api/v1/auth/refresh", json={"refresh_token": r1})
    assert reuse.status_code == 401

    # ...and it kills the whole family, so the newer token is now dead too.
    after = await client.post("/api/v1/auth/refresh", json={"refresh_token": r2})
    assert after.status_code == 401


async def test_logout_revokes_refresh(client):
    toks = await _verify(client, "9876543210")
    out = await client.post("/api/v1/auth/logout", json={"refresh_token": toks["refresh_token"]})
    assert out.status_code == 200
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": toks["refresh_token"]})
    assert r.status_code == 401


async def test_access_token_required(client):
    # No token -> 401/403 on a protected route.
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 401
