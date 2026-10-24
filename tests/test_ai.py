from conftest import auth, login


async def test_support_bot_devstub(client):
    cust = await login(client, "9876543210")
    # Give the bot something to ground on.
    await client.post(
        "/api/v1/orders", json={"items": [{"product_id": 1, "qty": 1}]}, headers=auth(cust)
    )
    r = await client.post(
        "/api/v1/ai/support", json={"message": "where is my order?"}, headers=auth(cust)
    )
    assert r.status_code == 200
    body = r.json()
    assert "answer" in body and body["ai_live"] is False  # no key in tests -> dev stub


async def test_support_requires_auth(client):
    r = await client.post("/api/v1/ai/support", json={"message": "hi"})
    assert r.status_code == 401


async def test_admin_copilot_devstub(client):
    admin = await login(client, "9999900001")
    r = await client.post(
        "/api/v1/admin/ai/ask", json={"message": "how is revenue?"}, headers=auth(admin)
    )
    assert r.status_code == 200 and "answer" in r.json()


async def test_copilot_requires_admin(client):
    cust = await login(client, "9876543210")
    r = await client.post("/api/v1/admin/ai/ask", json={"message": "x"}, headers=auth(cust))
    assert r.status_code == 403


async def test_ai_input_validation(client):
    cust = await login(client, "9876543210")
    r = await client.post("/api/v1/ai/support", json={"message": ""}, headers=auth(cust))
    assert r.status_code == 422
