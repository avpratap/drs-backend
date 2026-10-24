from conftest import auth, login


async def test_profile_validation(client):
    token = await login(client, "9876543210")
    assert (
        await client.patch("/api/v1/profile", json={"gstin": "BADGST"}, headers=auth(token))
    ).status_code == 422
    assert (
        await client.patch(
            "/api/v1/profile", json={"date_of_birth": "2099-01-01"}, headers=auth(token)
        )
    ).status_code == 422
    ok = await client.patch(
        "/api/v1/profile",
        json={"display_name": "Ravi", "gstin": "27AAPFU0939F1ZV"},
        headers=auth(token),
    )
    assert ok.status_code == 200
    assert ok.json()["display_name"] == "Ravi"


async def test_address_default_flip(client):
    # Own customer: "first address is auto-default" depends on having NO prior address,
    # and other tests add addresses for the shared 9876543210.
    token = await login(client, "9100000971")
    a1 = await client.post(
        "/api/v1/addresses",
        json={"line1": "1 A Rd", "city": "Jaipur", "state": "RJ", "pincode": "302001"},
        headers=auth(token),
    )
    assert a1.status_code == 201
    assert a1.json()["is_default"] is True  # first is auto-default

    a2 = await client.post(
        "/api/v1/addresses",
        json={"line1": "2 B Rd", "city": "Jaipur", "state": "RJ", "pincode": "302015"},
        headers=auth(token),
    )
    a2id = a2.json()["id"]
    assert a2.json()["is_default"] is False

    await client.post(f"/api/v1/addresses/{a2id}/default", headers=auth(token))
    listing = (await client.get("/api/v1/addresses", headers=auth(token))).json()
    defaults = [a for a in listing if a["is_default"]]
    assert len(defaults) == 1 and defaults[0]["id"] == a2id


async def test_bad_pincode_422(client):
    token = await login(client, "9876543210")
    r = await client.post(
        "/api/v1/addresses",
        json={"line1": "x", "city": "c", "state": "s", "pincode": "02001"},
        headers=auth(token),
    )
    assert r.status_code == 422


async def test_address_idor_404(client):
    owner = await login(client, "9876543210")
    attacker = await login(client, "9191919191")
    a = await client.post(
        "/api/v1/addresses",
        json={"line1": "1 A Rd", "city": "Jaipur", "state": "RJ", "pincode": "302001"},
        headers=auth(owner),
    )
    aid = a.json()["id"]
    r = await client.patch(
        f"/api/v1/addresses/{aid}", json={"city": "hack"}, headers=auth(attacker)
    )
    assert r.status_code == 404
