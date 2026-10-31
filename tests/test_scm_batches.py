from conftest import auth, idem, login


async def _product(client, h, sku) -> int:
    return (await client.post("/api/v1/products", json={"sku": sku, "name": sku}, headers=h)).json()["id"]


async def _stock(client, h, pid) -> float:
    # Filter server-side: the unfiltered list is paginated (50 rows), so a product created
    # late in the suite fell off the page and this silently returned 0.0.
    rows = (await client.get(f"/api/v1/admin/stock?product_id={pid}", headers=h)).json()
    return next((s["qty_on_hand"] for s in rows if s["product_id"] == pid), 0.0)


async def test_receive_batch_increments_stock(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    pid = await _product(client, h, "BATCH-RX-1")
    r = await client.post(
        "/api/v1/admin/stock/batches",
        json={"product_id": pid, "batch_no": "B1", "qty": 40, "expiry_date": "2040-01-01"},
        headers=idem(h),
    )
    assert r.status_code == 200 and r.json()["qty"] == 40.0
    assert await _stock(client, h, pid) == 40.0
    # expiry before mfg rejected
    bad = await client.post(
        "/api/v1/admin/stock/batches",
        json={"product_id": pid, "batch_no": "B2", "qty": 1, "mfg_date": "2040-01-02", "expiry_date": "2040-01-01"},
        headers=idem(h),
    )
    assert bad.status_code == 422


async def test_fefo_consumption_on_delivery(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    pid = await _product(client, h, "BATCH-FEFO-1")
    tid = (await client.post("/api/v1/admin/price-tiers", json={"code": "FEFO", "name": "t"}, headers=h)).json()["id"]
    await client.post("/api/v1/admin/product-prices", json={"product_id": pid, "price_tier_id": tid, "unit_price": 10, "valid_from": "2025-01-01"}, headers=h)
    shop_id = (await client.post("/api/v1/shops", json={"name": "FEFO Shop"}, headers=h)).json()["id"]
    await client.post(f"/api/v1/admin/shops/{shop_id}/tier", json={"price_tier_id": tid}, headers=h)
    sid = (await client.post("/api/v1/subscriptions", json={"shop_id": shop_id}, headers=h)).json()["id"]
    await client.put(f"/api/v1/subscriptions/{sid}/items", json=[{"product_id": pid, "default_qty": 5, "frequency": "daily"}], headers=h)

    # Two batches: earlier expiry (3) should be consumed first, then the later (10).
    await client.post("/api/v1/admin/stock/batches", json={"product_id": pid, "batch_no": "EARLY", "qty": 3, "expiry_date": "2040-02-01"}, headers=idem(h))
    await client.post("/api/v1/admin/stock/batches", json={"product_id": pid, "batch_no": "LATE", "qty": 10, "expiry_date": "2040-09-01"}, headers=idem(h))

    date = "2039-05-05"
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=h)
    orders = (await client.get(f"/api/v1/delivery/orders?date={date}", headers=h)).json()
    order = next(o for o in orders if o["shop_id"] == shop_id)
    await client.post(
        f"/api/v1/delivery/orders/{order['id']}/deliver",
        json={"items": [{"product_id": pid, "qty_delivered": 5}], "returns": []},
        headers=h,
    )
    batches = (await client.get(f"/api/v1/admin/stock/batches?product_id={pid}&active_only=false", headers=h)).json()
    by_no = {b["batch_no"]: b for b in batches}
    assert by_no["EARLY"]["qty"] == 0.0 and by_no["EARLY"]["status"] == "depleted"  # consumed first
    assert by_no["LATE"]["qty"] == 8.0  # 5 - 3 taken from LATE
    assert await _stock(client, h, pid) == 8.0  # 13 - 5


async def test_expire_batches_writeoff(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    pid = await _product(client, h, "BATCH-EXP-1")
    await client.post("/api/v1/admin/stock/batches", json={"product_id": pid, "batch_no": "OLD", "qty": 6, "expiry_date": "2020-01-01"}, headers=idem(h))
    await client.post("/api/v1/admin/stock/batches", json={"product_id": pid, "batch_no": "FRESH", "qty": 4, "expiry_date": "2040-01-01"}, headers=idem(h))
    assert await _stock(client, h, pid) == 10.0

    res = await client.post("/api/v1/admin/stock/expire", headers=h)
    assert res.status_code == 200 and res.json()["qty_written_off"] >= 6.0
    assert await _stock(client, h, pid) == 4.0  # only the expired OLD batch written off
    expiring = (await client.get(f"/api/v1/admin/stock/batches?product_id={pid}&active_only=false", headers=h)).json()
    assert next(b for b in expiring if b["batch_no"] == "OLD")["status"] == "expired"


async def test_batches_require_admin(client):
    cust = await login(client, "9876543210")
    assert (await client.get("/api/v1/admin/stock/batches", headers=idem(auth(cust)))).status_code == 403


async def test_receive_same_batch_merges_not_duplicates(client):
    """#26: re-receiving the same (product, location, batch_no, expiry) merges into one
    lot (qty added) rather than creating a duplicate that fragments FEFO."""
    h = auth(await login(client, "9999900001"))
    pid = await _product(client, h, "BATCH-MERGE-1")
    body = {"product_id": pid, "batch_no": "LOT-X", "qty": 10, "expiry_date": "2041-05-05"}
    r1 = await client.post("/api/v1/admin/stock/batches", json=body, headers=idem(h))
    assert r1.status_code == 200
    first_id = r1.json()["id"]
    r2 = await client.post("/api/v1/admin/stock/batches", json={**body, "qty": 15}, headers=idem(h))
    assert r2.status_code == 200
    # Same lot row reused, qty merged to 25; stock reflects the full 25.
    assert r2.json()["id"] == first_id
    assert r2.json()["qty"] == 25.0
    assert await _stock(client, h, pid) == 25.0


async def test_lot_is_good_through_its_expiry_date():
    """Convention: expiry_date is the LAST sellable day. A lot expiring today is used
    today and written off from tomorrow."""
    import datetime as dt

    from app.core.db import SessionLocal
    from app.models.batch import StockBatch
    from app.models.product import Product
    from app.services import batches

    async with SessionLocal() as db:
        p = Product(sku=f"EXP-{dt.datetime.now().timestamp()}", name="exp", uom="litre")
        db.add(p)
        await db.flush()
        today = batches._today()
        lot = await batches.receive_batch(db, p.id, "TODAY", 5, expiry_date=today)
        await batches.consume_fefo(db, p.id, 2, "delivery", strict=True)  # sellable today
        assert float(lot.qty) == 3.0 and lot.status == "active"
        swept = await batches.expire_batches(db, today)  # today's sweep: not yet expired
        assert swept["batches_expired"] == 0
        swept = await batches.expire_batches(db, today + dt.timedelta(days=1))
        assert swept["batches_expired"] == 1 and swept["qty_written_off"] == 3.0
        assert (await db.get(StockBatch, lot.id)).status == "expired"
        await db.rollback()
