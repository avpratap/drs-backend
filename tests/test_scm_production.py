from conftest import auth, idem, login


async def _product(client, h, sku) -> int:
    return (await client.post("/api/v1/products", json={"sku": sku, "name": sku}, headers=h)).json()["id"]


async def _stock(client, h, pid) -> float:
    # Filter server-side: the unfiltered list is paginated (50 rows), so a product created
    # late in the suite fell off the page and this silently returned 0.0.
    rows = (await client.get(f"/api/v1/admin/stock?product_id={pid}", headers=h)).json()
    return next((s["qty_on_hand"] for s in rows if s["product_id"] == pid), 0.0)


async def _seed_batch(client, h, pid, qty, batch_no):
    await client.post(
        "/api/v1/admin/stock/batches",
        json={"product_id": pid, "batch_no": batch_no, "qty": qty, "expiry_date": "2041-01-01"},
        headers=idem(h),
    )


async def _bom(client, h, finished, comps, output_qty=1, name="Ghee recipe") -> int:
    r = await client.post(
        "/api/v1/admin/boms",
        json={
            "product_id": finished,
            "name": name,
            "output_qty": output_qty,
            "components": [{"component_product_id": c, "qty": q} for c, q in comps],
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_bom_create_and_self_component_rejected(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    ghee = await _product(client, h, "PRD-GHEE-1")
    cream = await _product(client, h, "PRD-CREAM-1")
    bom_id = await _bom(client, h, ghee, [(cream, 12)], output_qty=1)
    got = (await client.get(f"/api/v1/admin/boms/{bom_id}", headers=h)).json()
    assert got["product_id"] == ghee and got["output_qty"] == 1.0
    assert got["components"][0] == {"component_product_id": cream, "qty": 12.0}

    # A product cannot be its own component.
    bad = await client.post(
        "/api/v1/admin/boms",
        json={"product_id": ghee, "name": "x", "components": [{"component_product_id": ghee, "qty": 1}]},
        headers=h,
    )
    assert bad.status_code == 422


async def test_production_full_run_consumes_and_produces(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    ghee = await _product(client, h, "PRD-GHEE-2")
    cream = await _product(client, h, "PRD-CREAM-2")
    milk = await _product(client, h, "PRD-MILK-2")
    # 1 unit ghee needs 10 cream + 4 milk.
    bom_id = await _bom(client, h, ghee, [(cream, 10), (milk, 4)], output_qty=1)
    await _seed_batch(client, h, cream, 100, "CR-1")
    await _seed_batch(client, h, milk, 50, "MK-1")

    pro = (await client.post(
        "/api/v1/admin/production-orders",
        json={"bom_id": bom_id, "planned_qty": 5},  # needs 50 cream, 20 milk
        headers=h,
    )).json()
    assert pro["status"] == "draft" and pro["pro_no"].startswith("PR")
    pid_order = pro["id"]

    # Cannot complete before start.
    assert (await client.post(
        f"/api/v1/admin/production-orders/{pid_order}/complete",
        json={"output_batch_no": "GHEE-OUT"}, headers=h,
    )).status_code == 409

    started = await client.post(f"/api/v1/admin/production-orders/{pid_order}/start", headers=h)
    assert started.status_code == 200 and started.json()["status"] == "in_progress"
    # Components consumed FEFO.
    assert await _stock(client, h, cream) == 50.0  # 100 - 50
    assert await _stock(client, h, milk) == 30.0   # 50 - 20
    assert await _stock(client, h, ghee) == 0.0    # not produced yet

    done = await client.post(
        f"/api/v1/admin/production-orders/{pid_order}/complete",
        json={"output_batch_no": "GHEE-OUT", "expiry_date": "2041-06-01"},
        headers=h,
    )
    assert done.status_code == 200 and done.json()["status"] == "completed"
    assert await _stock(client, h, ghee) == 5.0  # finished goods produced
    gb = (await client.get(f"/api/v1/admin/stock/batches?product_id={ghee}&active_only=false", headers=h)).json()
    assert any(b["batch_no"] == "GHEE-OUT" and b["qty"] == 5.0 for b in gb)


async def test_production_insufficient_components_422(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    ghee = await _product(client, h, "PRD-GHEE-3")
    cream = await _product(client, h, "PRD-CREAM-3")
    bom_id = await _bom(client, h, ghee, [(cream, 10)], output_qty=1)
    await _seed_batch(client, h, cream, 5, "CR-SHORT")  # only 5, need 10
    pid_order = (await client.post(
        "/api/v1/admin/production-orders",
        json={"bom_id": bom_id, "planned_qty": 1},
        headers=h,
    )).json()["id"]
    r = await client.post(f"/api/v1/admin/production-orders/{pid_order}/start", headers=h)
    assert r.status_code == 422
    # Nothing consumed — still draft, cream intact.
    assert (await client.get(f"/api/v1/admin/production-orders/{pid_order}", headers=h)).json()["status"] == "draft"
    assert await _stock(client, h, cream) == 5.0


async def test_production_scales_with_output_qty(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    paneer = await _product(client, h, "PRD-PANEER-4")
    milk = await _product(client, h, "PRD-MILK-4")
    # recipe yields 2 units paneer per 10 milk; plan 6 → factor 3 → 30 milk.
    bom_id = await _bom(client, h, paneer, [(milk, 10)], output_qty=2, name="Paneer")
    await _seed_batch(client, h, milk, 100, "MK-4")
    pid_order = (await client.post(
        "/api/v1/admin/production-orders",
        json={"bom_id": bom_id, "planned_qty": 6},
        headers=h,
    )).json()["id"]
    await client.post(f"/api/v1/admin/production-orders/{pid_order}/start", headers=h)
    assert await _stock(client, h, milk) == 70.0  # 100 - 30


async def test_production_cancel_and_guards(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    ghee = await _product(client, h, "PRD-GHEE-5")
    cream = await _product(client, h, "PRD-CREAM-5")
    bom_id = await _bom(client, h, ghee, [(cream, 1)], output_qty=1)
    pid_order = (await client.post(
        "/api/v1/admin/production-orders",
        json={"bom_id": bom_id, "planned_qty": 1},
        headers=h,
    )).json()["id"]
    c = await client.post(f"/api/v1/admin/production-orders/{pid_order}/cancel", headers=h)
    assert c.status_code == 200 and c.json()["status"] == "cancelled"
    assert (await client.post(f"/api/v1/admin/production-orders/{pid_order}/start", headers=h)).status_code == 409


async def test_production_unknown_bom_404(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    r = await client.post(
        "/api/v1/admin/production-orders",
        json={"bom_id": 999999, "planned_qty": 1},
        headers=h,
    )
    assert r.status_code == 404


async def test_production_requires_admin(client):
    cust = await login(client, "9876543210")
    hc = auth(cust)
    assert (await client.get("/api/v1/admin/boms", headers=hc)).status_code == 403
    assert (await client.get("/api/v1/admin/production-orders", headers=hc)).status_code == 403
    assert (await client.post("/api/v1/admin/boms", json={"product_id": 1, "name": "x", "components": [{"component_product_id": 1, "qty": 1}]}, headers=hc)).status_code == 403


async def test_complete_books_actual_produced_qty(client):
    """B8: a run that yields less (or more) than planned books what was actually made."""
    h = auth(await login(client, "9999900001"))
    ghee = await _product(client, h, "PRD-GHEE-YIELD")
    cream = await _product(client, h, "PRD-CREAM-YIELD")
    bom_id = await _bom(client, h, ghee, [(cream, 10)], output_qty=1)
    await _seed_batch(client, h, cream, 100, "CR-Y")
    pro = (await client.post("/api/v1/admin/production-orders",
                             json={"bom_id": bom_id, "planned_qty": 5}, headers=h)).json()
    await client.post(f"/api/v1/admin/production-orders/{pro['id']}/start", headers=h)
    bad = await client.post(f"/api/v1/admin/production-orders/{pro['id']}/complete",
                            json={"output_batch_no": "GY", "produced_qty": 0}, headers=h)
    assert bad.status_code == 422
    done = await client.post(f"/api/v1/admin/production-orders/{pro['id']}/complete",
                             json={"output_batch_no": "GY", "produced_qty": 4.25}, headers=h)
    assert done.status_code == 200, done.text
    assert done.json()["produced_qty"] == 4.25 and done.json()["planned_qty"] == 5.0
    assert await _stock(client, h, ghee) == 4.25  # actual, not planned


async def test_complete_without_produced_qty_books_planned(client):
    h = auth(await login(client, "9999900001"))
    ghee = await _product(client, h, "PRD-GHEE-PLAN")
    cream = await _product(client, h, "PRD-CREAM-PLAN")
    bom_id = await _bom(client, h, ghee, [(cream, 1)], output_qty=1)
    await _seed_batch(client, h, cream, 10, "CR-P")
    pro = (await client.post("/api/v1/admin/production-orders",
                             json={"bom_id": bom_id, "planned_qty": 3}, headers=h)).json()
    await client.post(f"/api/v1/admin/production-orders/{pro['id']}/start", headers=h)
    done = await client.post(f"/api/v1/admin/production-orders/{pro['id']}/complete",
                             json={"output_batch_no": "GP"}, headers=h)
    assert done.json()["produced_qty"] == 3.0 and await _stock(client, h, ghee) == 3.0
