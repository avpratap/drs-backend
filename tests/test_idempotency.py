from conftest import auth, login


async def test_replay_returns_same_payment(client):
    token = await login(client, "9876543210")
    headers = {**auth(token), "Idempotency-Key": "test-key-1"}
    body = {"purpose": "wallet_topup", "amount": 150}

    import sqlalchemy as sa

    from app.core.db import engine

    async def n_payments():
        async with engine.connect() as conn:
            return (await conn.execute(sa.text("SELECT count(*) FROM payments"))).scalar()

    before = await n_payments()
    r1 = await client.post("/api/v1/payments/create", json=body, headers=headers)
    assert r1.status_code == 200
    r2 = await client.post("/api/v1/payments/create", json=body, headers=headers)
    assert r2.status_code == 200
    assert r2.headers.get("Idempotent-Replay") == "true"
    assert r1.json()["payment_id"] == r2.json()["payment_id"]
    assert await n_payments() == before + 1  # executed exactly once


async def test_key_reuse_different_body_422(client):
    token = await login(client, "9876543210")
    headers = {**auth(token), "Idempotency-Key": "test-key-2"}
    r1 = await client.post(
        "/api/v1/payments/create", json={"purpose": "wallet_topup", "amount": 10}, headers=headers
    )
    assert r1.status_code == 200
    r2 = await client.post(
        "/api/v1/payments/create", json={"purpose": "wallet_topup", "amount": 99}, headers=headers
    )
    assert r2.status_code == 422


async def test_idempotency_key_scoped_per_user(client):
    # Same raw key + same body from two different users must NOT replay across users.
    a = await login(client, "9876543210")
    b = await login(client, "9191919191")
    body = {"purpose": "wallet_topup", "amount": 20}
    hk = "shared-key-xyz"
    ra = await client.post(
        "/api/v1/payments/create", json=body, headers={**auth(a), "Idempotency-Key": hk}
    )
    rb = await client.post(
        "/api/v1/payments/create", json=body, headers={**auth(b), "Idempotency-Key": hk}
    )
    assert ra.status_code == 200 and rb.status_code == 200
    assert rb.headers.get("Idempotent-Replay") != "true"  # B executed its own, not A's
    assert ra.json()["payment_id"] != rb.json()["payment_id"]


async def test_no_key_creates_distinct(client):
    token = await login(client, "9876543210")
    body = {"purpose": "wallet_topup", "amount": 5}
    p1 = (await client.post("/api/v1/payments/create", json=body, headers=auth(token))).json()
    p2 = (await client.post("/api/v1/payments/create", json=body, headers=auth(token))).json()
    assert p1["payment_id"] != p2["payment_id"]
