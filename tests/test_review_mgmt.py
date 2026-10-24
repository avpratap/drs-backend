"""Tier-4 #32 review management: moderation, verified-purchase, helpful, aggregates."""
from conftest import auth, login


async def _product(client, h, sku) -> int:
    return (await client.post("/api/v1/products", json={"sku": sku, "name": sku}, headers=h)).json()["id"]


async def _rate(client, h, pid, stars, comment="ok", approve=True):
    """Post a review. Unverified reviews are held for moderation (O8), so by default an
    admin approves it — the tests below are about aggregates/listing, not moderation."""
    r = await client.post(f"/api/v1/products/{pid}/ratings", json={"stars": stars, "comment": comment}, headers=h)
    if approve and r.status_code == 200 and r.json()["status"] == "pending":
        ah = auth(await login(client, "9999900001"))
        m = await client.post(f"/api/v1/admin/ratings/{r.json()['id']}/moderate",
                              json={"status": "approved"}, headers=ah)
        assert m.status_code == 200
    return r


async def test_review_creates_and_updates_product_aggregate(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    cust = await login(client, "9876543210")
    pid = await _product(client, ah, "REV-AGG")

    # O8: an unverified review is held for moderation and doesn't move the aggregate.
    held = await _rate(client, auth(cust), pid, 4, approve=False)
    assert held.status_code == 200
    body = held.json()
    assert body["status"] == "pending"
    assert body["verified_purchase"] is False  # never bought this product
    assert (await client.get(f"/api/v1/products/{pid}", headers=ah)).json()["rating_count"] == 0
    assert (await client.get(f"/api/v1/products/{pid}/ratings")).json()["count"] == 0
    r = await _rate(client, auth(cust), pid, 4)  # re-post (upsert) + admin approval
    assert r.status_code == 200

    prod = (await client.get(f"/api/v1/products/{pid}", headers=ah)).json()
    assert float(prod["avg_rating"]) == 4.0 and prod["rating_count"] == 1

    listing = (await client.get(f"/api/v1/products/{pid}/ratings")).json()
    assert listing["count"] == 1 and listing["average"] == 4.0
    assert listing["distribution"]["4"] == 1


async def test_delete_own_review_recomputes(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    cust = await login(client, "9876543210")
    ch = auth(cust)
    pid = await _product(client, ah, "REV-DEL")
    await _rate(client, ch, pid, 5)
    assert (await client.get(f"/api/v1/products/{pid}", headers=ah)).json()["rating_count"] == 1
    d = await client.delete(f"/api/v1/products/{pid}/ratings", headers=ch)
    assert d.status_code == 204
    prod = (await client.get(f"/api/v1/products/{pid}", headers=ah)).json()
    assert prod["rating_count"] == 0 and float(prod["avg_rating"]) == 0.0


async def test_helpful_toggle_one_per_user(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    cust = await login(client, "9876543210")
    pid = await _product(client, ah, "REV-HELP")
    rid = (await _rate(client, auth(cust), pid, 4)).json()["id"]

    voter = await login(client, "9000000456")
    vh = auth(voter)
    v1 = await client.post(f"/api/v1/products/{pid}/ratings/{rid}/helpful", headers=vh)
    assert v1.json()["helpful_count"] == 1 and v1.json()["voted"] is True
    # Same user toggles off.
    v2 = await client.post(f"/api/v1/products/{pid}/ratings/{rid}/helpful", headers=vh)
    assert v2.json()["helpful_count"] == 0 and v2.json()["voted"] is False


async def test_moderation_hide_excludes_from_public(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    cust = await login(client, "9876543210")
    pid = await _product(client, ah, "REV-MOD")
    rid = (await _rate(client, auth(cust), pid, 5)).json()["id"]
    assert (await client.get(f"/api/v1/products/{pid}/ratings")).json()["count"] == 1

    m = await client.post(f"/api/v1/admin/ratings/{rid}/moderate", json={"status": "hidden"}, headers=ah)
    assert m.status_code == 200 and m.json()["status"] == "hidden"
    # Public (unauthenticated) listing excludes the hidden review + aggregate drops.
    pub = (await client.get(f"/api/v1/products/{pid}/ratings")).json()
    assert pub["count"] == 0
    prod = (await client.get(f"/api/v1/products/{pid}", headers=ah)).json()
    assert prod["rating_count"] == 0


async def test_admin_reply_appears(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    cust = await login(client, "9876543210")
    pid = await _product(client, ah, "REV-REPLY")
    rid = (await _rate(client, auth(cust), pid, 3)).json()["id"]
    rep = await client.post(f"/api/v1/admin/ratings/{rid}/reply", json={"reply": "Thanks for the feedback!"}, headers=ah)
    assert rep.status_code == 200 and rep.json()["admin_reply"] == "Thanks for the feedback!"


async def test_sort_by_helpful(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    pid = await _product(client, ah, "REV-SORT")
    r1 = (await _rate(client, auth(await login(client, "9876543210")), pid, 4)).json()["id"]
    r2 = (await _rate(client, auth(await login(client, "9000000456")), pid, 5)).json()["id"]
    # Give r2 a helpful vote so it should sort first.
    voter = auth(await login(client, "9000000789"))
    await client.post(f"/api/v1/products/{pid}/ratings/{r2}/helpful", headers=voter)
    listing = (await client.get(f"/api/v1/products/{pid}/ratings?sort=helpful")).json()
    assert listing["ratings"][0]["id"] == r2
    assert {r["id"] for r in listing["ratings"]} == {r1, r2}


async def test_public_listing_hides_other_reviewers_user_id(client):
    """#41: a reviewer's user_id is PII — public/other viewers get null + mine=False,
    the author sees their own id with mine=True, admins see it."""
    ah = auth(await login(client, "9999900001"))
    author = await login(client, "9876543210")
    pid = await _product(client, ah, "REV-PRIV")
    await _rate(client, auth(author), pid, 4)

    # Anonymous public viewer: user_id masked.
    pub = (await client.get(f"/api/v1/products/{pid}/ratings")).json()["ratings"]
    assert pub and pub[0]["user_id"] is None and pub[0]["mine"] is False

    # A DIFFERENT logged-in customer still can't see the author's id.
    other = auth(await login(client, "9000000456"))
    seen = (await client.get(f"/api/v1/products/{pid}/ratings", headers=other)).json()["ratings"]
    assert seen[0]["user_id"] is None and seen[0]["mine"] is False

    # The author sees their own id and mine=True.
    own = (await client.get(f"/api/v1/products/{pid}/ratings", headers=auth(author))).json()["ratings"]
    assert own[0]["user_id"] is not None and own[0]["mine"] is True


async def test_moderation_requires_admin(client):
    cust = await login(client, "9876543210")
    ch = auth(cust)
    assert (await client.get("/api/v1/admin/ratings", headers=ch)).status_code == 403
    assert (await client.post("/api/v1/admin/ratings/1/moderate", json={"status": "hidden"}, headers=ch)).status_code == 403
