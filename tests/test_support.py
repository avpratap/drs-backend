from conftest import auth, login


async def _open_ticket(client, h, subject="Late delivery", category="delivery"):
    r = await client.post(
        "/api/v1/support/tickets",
        json={"subject": subject, "category": category, "body": "My order hasn't arrived."},
        headers=h,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_customer_opens_ticket_with_first_message(client):
    cust = await login(client, "9876543210")
    t = await _open_ticket(client, auth(cust))
    assert t["status"] == "open"
    assert t["ticket_no"].startswith("TKT")
    assert len(t["messages"]) == 1 and t["messages"][0]["body"].startswith("My order")


async def test_customer_lists_and_gets_own(client):
    cust = await login(client, "9876543210")
    t = await _open_ticket(client, auth(cust))
    lst = await client.get("/api/v1/support/tickets", headers=auth(cust))
    assert lst.status_code == 200 and any(x["id"] == t["id"] for x in lst.json())
    got = await client.get(f"/api/v1/support/tickets/{t['id']}", headers=auth(cust))
    assert got.status_code == 200 and got.json()["id"] == t["id"]


async def test_reply_flow_status_transitions(client):
    cust = await login(client, "9876543210")
    admin = await login(client, "9999900001")
    ch, ah = auth(cust), auth(admin)
    t = await _open_ticket(client, ch)
    tid = t["id"]

    # Admin reply -> pending (awaiting customer).
    r = await client.post(
        f"/api/v1/admin/support/tickets/{tid}/messages",
        json={"body": "We're looking into it."},
        headers=ah,
    )
    assert r.status_code == 200 and r.json()["status"] == "pending"

    # Customer reply -> back to open.
    r = await client.post(
        f"/api/v1/support/tickets/{tid}/messages", json={"body": "Any update?"}, headers=ch
    )
    assert r.status_code == 200 and r.json()["status"] == "open"
    assert len(r.json()["messages"]) == 3

    # Admin resolves.
    r = await client.post(
        f"/api/v1/admin/support/tickets/{tid}/status", json={"status": "resolved"}, headers=ah
    )
    assert r.status_code == 200 and r.json()["status"] == "resolved"


async def test_admin_list_and_filter(client):
    cust = await login(client, "9876543210")
    admin = await login(client, "9999900001")
    await _open_ticket(client, auth(cust), subject="Payment stuck", category="payment")
    r = await client.get("/api/v1/admin/support/tickets?category=payment", headers=auth(admin))
    assert r.status_code == 200 and all(t["category"] == "payment" for t in r.json())


async def test_ticket_idor(client):
    cust = await login(client, "9876543210")
    t = await _open_ticket(client, auth(cust))
    other = await login(client, "9000000456")
    r = await client.get(f"/api/v1/support/tickets/{t['id']}", headers=auth(other))
    assert r.status_code == 404


async def test_support_admin_gate(client):
    cust = await login(client, "9876543210")
    r = await client.get("/api/v1/admin/support/tickets", headers=auth(cust))
    assert r.status_code == 403
