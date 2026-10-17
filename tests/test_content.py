"""Tier-4 #34 CMS: articles, static pages, banners, sitemap/robots."""
from conftest import auth, login


async def _article(client, h, slug, **over):
    body = {"slug": slug, "title": "Farm to Table", "body_html": "<p>Fresh milk</p>", "category": "stories"}
    body.update(over)
    return await client.post("/api/v1/admin/content/articles", json=body, headers=h)


async def test_article_draft_then_publish(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    r = await _article(client, h, "fresh-milk-story")
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    assert r.json()["status"] == "draft"

    # Draft is not in the public list.
    pub = (await client.get("/api/v1/content/articles")).json()
    assert all(a["slug"] != "fresh-milk-story" for a in pub)

    p = await client.post(f"/api/v1/admin/content/articles/{aid}/publish", headers=h)
    assert p.status_code == 200 and p.json()["status"] == "published"
    assert p.json()["published_at"] is not None

    pub2 = (await client.get("/api/v1/content/articles")).json()
    assert any(a["slug"] == "fresh-milk-story" for a in pub2)
    one = await client.get("/api/v1/content/articles/fresh-milk-story")
    assert one.status_code == 200 and one.json()["title"] == "Farm to Table"


async def test_duplicate_slug_conflict(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    await _article(client, h, "dup-slug")
    again = await _article(client, h, "dup-slug")
    assert again.status_code == 409


async def test_static_page_lifecycle(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    r = await client.post(
        "/api/v1/admin/content/pages",
        json={"slug": "about-us", "title": "About DRS", "body_html": "<h1>About</h1>"},
        headers=h,
    )
    assert r.status_code == 200
    pid = r.json()["id"]
    # Not published yet.
    assert (await client.get("/api/v1/content/pages/about-us")).status_code == 404
    await client.post(f"/api/v1/admin/content/pages/{pid}/publish", headers=h)
    got = await client.get("/api/v1/content/pages/about-us")
    assert got.status_code == 200 and got.json()["title"] == "About DRS"


async def test_banner_active_window(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    # Active, no window -> shown.
    await client.post("/api/v1/admin/content/banners", json={"title": "Diwali", "image_url": "https://x/i.jpg"}, headers=h)
    # Expired window -> hidden.
    await client.post(
        "/api/v1/admin/content/banners",
        json={"title": "Old Sale", "image_url": "https://x/o.jpg", "valid_to": "2020-01-01"},
        headers=h,
    )
    shown = (await client.get("/api/v1/content/banners")).json()
    titles = {b["title"] for b in shown}
    assert "Diwali" in titles and "Old Sale" not in titles


async def test_sitemap_and_robots(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    r = await _article(client, h, "sitemap-article")
    await client.post(f"/api/v1/admin/content/articles/{r.json()['id']}/publish", headers=h)

    sm = await client.get("/sitemap.xml")
    assert sm.status_code == 200
    assert "application/xml" in sm.headers["content-type"]
    assert "sitemap-article" in sm.text

    rb = await client.get("/robots.txt")
    assert rb.status_code == 200 and "text/plain" in rb.headers["content-type"]
    assert "User-agent:" in rb.text and "Sitemap:" in rb.text


async def test_content_requires_admin(client):
    cust = await login(client, "9876543210")
    r = await _article(client, auth(cust), "nope-slug")
    assert r.status_code == 403
