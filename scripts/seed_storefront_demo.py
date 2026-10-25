"""Storefront demo content: generated product art, banners, journal articles, static
pages, product reviews and a demo customer with account data.

Idempotent and additive: runs against an EXISTING database (never wipes) and can be
re-run safely. Rows are matched by natural keys (banner title, article/page slug,
reviewer phone, the demo customer's phone); existing banners, articles and pages and any
product image an admin set are left alone unless --force. Images are on-brand SVGs
generated here and stored through app.core.storage.put_object() (offline; the dev stub
returns a /files/... URL), at stable keys, so a re-run overwrites the same files.

Refuses ENV=prod: the reviews and the funded wallet are demo data and must never be
shown to (or spent by) real customers.

Run:  uv run python scripts/seed_storefront_demo.py [--phone 9999900010] [--force]
"""
import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # `app` when run by path

from sqlalchemy import select

DEMO_PREFIX = "storefront-demo"
DEFAULT_PHONE = "9999900010"
WALLET_TARGET = 500.0
POINTS_TARGET = 250
GIFT_CARD_AMOUNT = 500.0
WISHLIST_SIZE = 3

# Brand palette.
CREAM, GREEN, GOLD = "#F7F1E3", "#1F4D3A", "#C9A227"

# Placeholders: replace before showing these pages to anyone outside the team.
CONTACT = {
    "email": "support@example.com",
    "phone": "+91 00000 00000",
    "hours": "7:00 am to 8:00 pm, every day",
}


# ---------------------------------------------------------------- SVG art (800x800 art box)
def _art(kind: str) -> str:
    """Line-art silhouette for a product category, drawn in an 800x800 box."""
    s = f'fill="none" stroke="{GREEN}" stroke-width="10" stroke-linejoin="round" stroke-linecap="round"'
    g = f'fill="none" stroke="{GOLD}" stroke-width="8" stroke-linecap="round"'
    if kind == "milk":  # glass bottle with a milk line
        return (
            f'<rect x="335" y="140" width="130" height="34" rx="8" {s}/>'
            f'<path d="M345 174 h110 v56 q0 30 30 60 q45 45 45 115 v215 q0 30 -30 30 h-200'
            f' q-30 0 -30 -30 v-215 q0 -70 45 -115 q30 -30 30 -60 z" {s}/>'
            f'<path d="M272 440 q32 -20 64 0 t64 0 t64 0 t64 0" {g}/>'
            f'<circle cx="400" cy="540" r="34" {g}/>'
        )
    if kind == "buttermilk":  # tumbler with a mint leaf
        return (
            f'<path d="M285 230 h230 l-28 410 h-174 z" {s}/>'
            f'<path d="M298 330 q34 -16 68 0 t68 0 t68 0" {g}/>'
            f'<path d="M430 232 q40 -78 112 -70 q-6 70 -112 70 z" {g}/>'
            f'<path d="M455 210 q30 -20 62 -36" {g}/>'
        )
    if kind == "curd":  # clay pot (matka) with a spoon
        return (
            f'<ellipse cx="400" cy="380" rx="175" ry="30" {s}/>'
            f'<path d="M225 380 q0 230 175 230 q175 0 175 -230" {s}/>'
            f'<path d="M290 360 q40 -40 110 -40 q70 0 110 40" {g}/>'
            f'<path d="M520 300 l90 -120" {g}/><ellipse cx="625" cy="160" rx="22" ry="32" {g}/>'
        )
    if kind == "ghee":  # jar with a gold drop
        return (
            f'<rect x="300" y="190" width="200" height="56" rx="12" {s}/>'
            f'<path d="M290 256 h220 q30 0 30 30 v300 q0 30 -30 30 h-220 q-30 0 -30 -30 v-300'
            f' q0 -30 30 -30 z" {s}/>'
            f'<path d="M400 340 q55 75 55 118 a55 55 0 0 1 -110 0 q0 -43 55 -118 z" {g}/>'
        )
    if kind == "cheese":  # paneer block with cheese holes
        return (
            f'<path d="M230 360 L400 290 L570 360 L400 430 Z" {s}/>'
            f'<path d="M230 360 V540 L400 610 V430" {s}/><path d="M570 360 V540 L400 610" {s}/>'
            f'<circle cx="300" cy="470" r="22" {g}/><circle cx="350" cy="540" r="14" {g}/>'
            f'<circle cx="480" cy="500" r="20" {g}/><circle cx="520" cy="440" r="12" {g}/>'
        )
    # Generic dairy: a milk can.
    return (
        f'<rect x="345" y="150" width="110" height="40" rx="10" {s}/>'
        f'<path d="M360 190 v40 q-80 30 -80 110 v240 q0 30 30 30 h180 q30 0 30 -30 v-240'
        f' q0 -80 -80 -110 v-40" {s}/>'
        f'<path d="M290 400 h220" {g}/><path d="M290 450 h220" {g}/>'
    )


def _svg(width: int, height: int, body: str) -> bytes:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img">'
        f'<rect width="{width}" height="{height}" fill="{CREAM}"/>'
        f'<rect x="18" y="18" width="{width - 36}" height="{height - 36}" fill="none" '
        f'stroke="{GOLD}" stroke-width="3"/>{body}</svg>'
    ).encode()


def _lines(text: str, width: int) -> list[str]:
    out, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    return out + ([cur] if cur else [])


def _text(x: int, y: int, lines: list[str], size: int, color: str = GREEN) -> str:
    spans = "".join(
        f'<tspan x="{x}" dy="{0 if i == 0 else round(size * 1.2)}">{escape(line)}</tspan>'
        for i, line in enumerate(lines)
    )
    return (
        f'<text x="{x}" y="{y}" font-family="Georgia, \'Times New Roman\', serif" '
        f'font-size="{size}" fill="{color}">{spans}</text>'
    )


def product_svg(kind: str) -> bytes:
    label = {"cheese": "CHEESE &amp; PANEER"}.get(kind, escape(kind.upper()))
    return _svg(800, 800, _art(kind) + (
        f'<text x="400" y="715" text-anchor="middle" font-family="Georgia, serif" '
        f'font-size="40" letter-spacing="8" fill="{GREEN}">{label}</text>'
        f'<path d="M330 740 h140" stroke="{GOLD}" stroke-width="4"/>'
    ))


def banner_svg(title: str, kind: str) -> bytes:  # 1600x600 hero
    art = f'<g transform="translate(1020 30) scale(0.68)">{_art(kind)}</g>'
    return _svg(1600, 600, _text(110, 230, _lines(title, 24), 72) + (
        f'<path d="M110 420 h180" stroke="{GOLD}" stroke-width="6"/>'
        f'{_text(110, 480, ["DRS DAIRY"], 30, GOLD)}{art}'
    ))


def cover_svg(title: str, kind: str) -> bytes:  # 1200x630 article cover / OG image
    art = f'<g transform="translate(760 60) scale(0.62)">{_art(kind)}</g>'
    return _svg(1200, 630, _text(80, 230, _lines(title, 20), 56) + (
        f'<path d="M80 470 h150" stroke="{GOLD}" stroke-width="6"/>'
        f'{_text(80, 525, ["DRS JOURNAL"], 26, GOLD)}{art}'
    ))


def product_kind(*fields: str | None) -> str:
    """Art category from a product's category / name / sku (buttermilk before milk)."""
    text = " ".join(f for f in fields if f).lower()
    for kind, words in (
        ("buttermilk", ("buttermilk", "chaas", "chhach", "chaach", "lassi")),
        ("curd", ("curd", "dahi", "yogurt", "yoghurt", "shrikhand")),
        ("ghee", ("ghee",)),
        ("cheese", ("cheese", "paneer")),
        ("milk", ("milk",)),
    ):
        if any(w in text for w in words):
            return kind
    return "dairy"


def _store(key: str, data: bytes) -> str:
    from app.core import storage

    return storage.put_object(f"{DEMO_PREFIX}/{key}", data, "image/svg+xml")


# ---------------------------------------------------------------- content
BANNERS = [  # (title, art, cta_link)
    ("Farm-fresh milk at your door every morning", "milk", "/shop"),
    ("Pure ghee, slow-cooked the traditional way", "ghee", "/shop?q=ghee"),
    ("Recipes and stories from our dairy", "curd", "/blog"),
]


def _p(*paras: str) -> str:
    return "".join(f"<p>{x}</p>" for x in paras)


ARTICLES = [
    {
        "slug": "from-farm-to-your-doorstep", "kind": "milk", "category": "our-story",
        "title": "From the farm to your doorstep before breakfast",
        "author_name": "DRS Team", "published": dt.datetime(2026, 9, 2, 6, 30, tzinfo=dt.UTC),
        "tags": ["milk", "delivery"],
        "excerpt": "How a morning's milk travels from partner farms to your door, chilled all the way.",
        "body_html": _p(
            "Our day starts long before yours. Milk from our partner farms is collected at dawn, "
            "chilled within minutes and brought to the dairy, where every can is checked before it "
            "is accepted.",
            "Accepted milk is pasteurised, cooled again and packed the same morning. Nothing sits "
            "on a shelf overnight: packs go straight onto delivery routes planned around the "
            "orders and subscriptions placed by the previous evening's cutoff.",
            "By the time you wake up, your milk is at the door. If you are away, pause a "
            "delivery from your account before the cutoff and it simply skips that day.",
        ),
    },
    {
        "slug": "what-pure-milk-means-to-us", "kind": "milk", "category": "quality",
        "title": "What 'pure milk' means to us",
        "author_name": "DRS Quality Team", "published": dt.datetime(2026, 9, 16, 6, 30, tzinfo=dt.UTC),
        "tags": ["quality", "milk"],
        "excerpt": "The checks every batch goes through before it is packed.",
        "body_html": _p(
            "Pure milk should taste like milk and nothing else. That is why each collection is "
            "tested for fat and solids-not-fat, and screened for common adulterants, before it "
            "joins a batch.",
            "Temperature matters as much as testing. Milk is kept cold from collection to "
            "packing, which keeps it fresh without preservatives.",
            "Every pack carries a batch number, so if you ever have a question about what you "
            "received, we can trace it back to the day and the route it came from.",
        ),
    },
    {
        "slug": "ghee-the-traditional-way", "kind": "ghee", "category": "process",
        "title": "Ghee, the slow and traditional way",
        "author_name": "DRS Kitchen", "published": dt.datetime(2026, 9, 30, 6, 30, tzinfo=dt.UTC),
        "tags": ["ghee", "tradition"],
        "excerpt": "Why our ghee takes hours, and why you can smell the difference.",
        "body_html": _p(
            "Good ghee cannot be hurried. Cream is cultured, churned into butter and then "
            "simmered on a low flame until the milk solids turn golden and settle.",
            "That slow simmer is what gives ghee its nutty aroma and grainy texture. It is "
            "strained warm and packed once it has cooled, with nothing added.",
            "Store it with the lid closed, away from moisture, and always use a dry spoon; it "
            "keeps for months at room temperature.",
        ),
    },
    {
        "slug": "three-summer-coolers-with-chaas", "kind": "buttermilk", "category": "recipes",
        "title": "Three summer coolers with chaas",
        "author_name": "DRS Kitchen", "published": dt.datetime(2026, 10, 7, 6, 30, tzinfo=dt.UTC),
        "tags": ["recipes", "buttermilk"],
        "excerpt": "Masala chaas, mint chaas and a sweet saffron version for hot afternoons.",
        "body_html": _p(
            "<strong>Masala chaas:</strong> whisk a glass of chilled chaas with a pinch of roasted "
            "cumin, black salt and finely chopped coriander.",
            "<strong>Mint chaas:</strong> blend a handful of mint leaves and a small piece of green "
            "chilli into the chaas, then strain and serve over ice.",
            "<strong>Saffron chaas:</strong> soak a few saffron strands in a spoon of warm milk, "
            "stir into chaas with a little sugar and a pinch of cardamom.",
        ),
    },
    {
        "slug": "easy-homemade-paneer-tikka", "kind": "cheese", "category": "recipes",
        "title": "Easy paneer tikka at home",
        "author_name": "DRS Kitchen", "published": dt.datetime(2026, 10, 14, 6, 30, tzinfo=dt.UTC),
        "tags": ["recipes", "paneer"],
        "excerpt": "Smoky, tender paneer tikka on a regular pan in under 30 minutes.",
        "body_html": _p(
            "Cut fresh paneer into large cubes. Mix thick curd with ginger-garlic paste, red chilli, "
            "turmeric, garam masala, salt and a spoon of mustard oil.",
            "Coat the paneer with onion and capsicum chunks and rest for twenty minutes; fresh "
            "paneer soaks up the marinade quickly.",
            "Sear on a hot pan with a little ghee until charred at the edges, then finish with "
            "lemon juice and chaat masala.",
        ),
    },
]

PAGES = {
    "faq": ("Frequently asked questions", _p(
        "<strong>When do deliveries arrive?</strong> Morning deliveries reach most areas before "
        "8 am.",
        "<strong>Can I change tomorrow's order?</strong> Yes. Edit, pause or skip it from your "
        "account any time before the daily cutoff.",
        "<strong>How do I pay?</strong> Use your DRS wallet, a gift card or online payment at "
        "checkout. Wallet top-ups are available from your account.",
        "<strong>Are prices inclusive of GST?</strong> Yes, all prices shown include GST.",
    )),
    "delivery-areas": ("Delivery areas", _p(
        "We deliver to a growing list of neighbourhoods. Enter your pincode on the shop page to "
        "check whether we deliver to you and which delivery slots are available.",
        "If we do not reach you yet, leave your details and we will let you know as soon as "
        "your area is added.",
    )),
    "terms": ("Terms of service", _p(
        "These terms govern your use of the DRS website and app and the purchase of products "
        "through them. By placing an order you agree to them.",
        "Orders and subscription changes must be made before the daily cutoff to apply to the "
        "next delivery. Prices include GST and are confirmed at checkout.",
        "If a product arrives damaged or is not delivered, contact us within 24 hours and we "
        "will replace it or refund you to your original payment method or wallet.",
    )),
    "privacy": ("Privacy policy", _p(
        "We collect only what we need to deliver your orders: your phone number, delivery "
        "address and order history, plus payment references from our payment partner.",
        "Your data is used to fulfil orders, send delivery updates and, with your consent, "
        "offers. We do not sell your personal data.",
        "You can download or erase your data from your account settings at any time, in line "
        "with India's Digital Personal Data Protection Act, 2023.",
    )),
    "contact": ("Contact us", _p(
        f"Email: {escape(CONTACT['email'])}",
        f"Phone: {escape(CONTACT['phone'])}",
        f"Hours: {escape(CONTACT['hours'])}",
        "For order issues, include your order number so we can help faster.",
    )),
}

# Demo reviewers. Reviews are attributed to these accounts and are NOT marked as verified
# purchases.
REVIEWERS = [
    ("9999900101", "Priya S."), ("9999900102", "Rahul M."), ("9999900103", "Anita K."),
    ("9999900104", "Vikram R."), ("9999900105", "Meera J."),
]
REVIEW_TEXT = {
    5: ["Fresh every single morning. My family loves it.",
        "Tastes just like it should. Will keep ordering.",
        "Excellent quality and always on time."],
    4: ["Very good quality, delivery was slightly late once.",
        "Good taste and packaging. Happy with it.",
        "Consistent and fresh. Would like more pack sizes."],
    3: ["Decent, but I expected a richer taste.",
        "Okay overall. Delivery timing could be better."],
}


def _reviews_for(product_id: int) -> list[tuple[int, int, str]]:
    """Deterministic 3-5 (reviewer index, stars 3..5, comment) per product."""
    n = 3 + product_id % 3
    out = []
    for i in range(n):
        stars = (5, 4, 5, 3, 4)[(product_id + i) % 5]
        texts = REVIEW_TEXT[stars]
        out.append(((product_id + i) % len(REVIEWERS), stars, texts[(product_id + 2 * i) % len(texts)]))
    return out


# ---------------------------------------------------------------- seeding
async def _get_or_create_user(db, phone: str, full_name: str | None = None):
    from app.models.user import User

    user = await db.scalar(select(User).where(User.phone == phone))
    created = user is None
    if created:
        user = User(phone=phone, phone_verified=True, role="customer", full_name=full_name)
        db.add(user)
        await db.flush()
    return user, created


async def seed_demo(db, *, phone: str = DEFAULT_PHONE, force: bool = False) -> dict:
    """Seed everything into an open session and commit. Returns a summary dict."""
    from app.core.phone import is_indian_mobile, normalize_phone
    from app.models.content import Article, Banner, StaticPage
    from app.models.loyalty import GiftCard, LoyaltyAccount
    from app.models.product import Product
    from app.models.rating import Rating
    from app.models.wishlist import WishlistItem
    from app.services import billing, loyalty, profile, reviews

    summary: dict = {}

    # 1. Product images (ACTIVE products only).
    products = (
        await db.execute(select(Product).where(Product.status == "active").order_by(Product.id))
    ).scalars().all()
    urls = {kind: _store(f"products/{kind}.svg", product_svg(kind))
            for kind in ("milk", "buttermilk", "curd", "ghee", "cheese", "dairy")}
    set_, kept = [], []
    for p in products:
        current = p.image_url or ""
        if current and f"/{DEMO_PREFIX}/" not in current and not force:
            kept.append(p.sku)  # an image an admin set: keep it
            continue
        p.image_url = urls[product_kind(p.category, p.name, p.sku)]
        set_.append(p.sku)
    summary["product_images"] = {"set": set_, "kept_existing": kept}

    # 2. Banners (matched by title).
    created = updated = 0
    for pos, (title, kind, cta) in enumerate(BANNERS):
        image = _store(f"banners/banner-{pos + 1}.svg", banner_svg(title, kind))
        b = await db.scalar(select(Banner).where(Banner.title == title))
        if b is None:
            db.add(Banner(title=title, image_url=image, cta_link=cta, position=pos, active=True))
            created += 1
        elif force:
            b.image_url, b.cta_link, b.position, b.active = image, cta, pos, True
            updated += 1
    summary["banners"] = {"created": created, "updated": updated}

    # 3. Journal articles (matched by slug), published.
    created = updated = 0
    for a in ARTICLES:
        cover = _store(f"articles/{a['slug']}.svg", cover_svg(a["title"], a["kind"]))
        fields = {
            "title": a["title"], "body_html": a["body_html"], "excerpt": a["excerpt"],
            "author_name": a["author_name"], "category": a["category"], "tags": a["tags"],
            "meta_title": a["title"], "meta_description": a["excerpt"],
            "cover_image_url": cover, "status": "published", "published_at": a["published"],
        }
        row = await db.scalar(select(Article).where(Article.slug == a["slug"]))
        if row is None:
            db.add(Article(slug=a["slug"], **fields))
            created += 1
        elif force:
            for k, v in fields.items():
                setattr(row, k, v)
            updated += 1
    summary["articles"] = {"created": created, "updated": updated}

    # 4. Static pages (matched by slug), published.
    created = updated = 0
    for slug, (title, body) in PAGES.items():
        fields = {"title": title, "body_html": body, "status": "published",
                  "meta_title": title, "published_at": dt.datetime(2026, 9, 1, tzinfo=dt.UTC)}
        row = await db.scalar(select(StaticPage).where(StaticPage.slug == slug))
        if row is None:
            db.add(StaticPage(slug=slug, **fields))
            created += 1
        elif force:
            for k, v in fields.items():
                setattr(row, k, v)
            updated += 1
    summary["pages"] = {"created": created, "updated": updated}

    # 5. Ratings: 3-5 approved reviews per active product from the demo reviewers.
    reviewers = [(await _get_or_create_user(db, ph, name))[0] for ph, name in REVIEWERS]
    created = 0
    for p in products:
        for idx, stars, comment in _reviews_for(p.id):
            user = reviewers[idx]
            r = await db.scalar(
                select(Rating).where(Rating.user_id == user.id, Rating.product_id == p.id)
            )
            if r is None:
                db.add(Rating(user_id=user.id, product_id=p.id, stars=stars, comment=comment,
                              status="approved", verified_purchase=False))
                created += 1
            else:
                r.stars, r.comment, r.status = stars, comment, "approved"
        await db.flush()
        await reviews.recompute_product_rating(db, p.id)
    summary["ratings"] = {"created": created, "products": len(products)}

    # 6. Demo customer with wallet, points, a gift card and a wishlist.
    phone = normalize_phone(phone)
    if not is_indian_mobile(phone):
        raise SystemExit(f"--phone {phone!r} is not a valid Indian mobile (OTP login needs one)")
    user, user_created = await _get_or_create_user(db, phone, "Demo Customer")
    if user.role != "customer":
        raise SystemExit(f"{phone} belongs to a {user.role} user; pick another --phone")
    account = await profile.get_or_create_account(db, user)

    wallet = await billing.get_or_create_wallet(db, account.id)
    top_up = round(WALLET_TARGET - float(wallet.balance), 2)
    if top_up > 0:  # non-cash goodwill credit: spendable, never withdrawable as cash
        await billing.wallet_adjust(db, account.id, top_up, "adjustment", "demo_seed", None)

    la = await db.scalar(select(LoyaltyAccount).where(LoyaltyAccount.account_id == account.id))
    points_delta = POINTS_TARGET - (la.points_balance if la else 0)
    if points_delta:
        await loyalty.adjust_points(db, account.id, points_delta)

    card = await db.scalar(
        select(GiftCard).where(
            GiftCard.issued_to_account_id == account.id, GiftCard.status == "active",
            GiftCard.initial_amount == GIFT_CARD_AMOUNT,
        ).order_by(GiftCard.id)
    )
    if card is None:
        card = await loyalty.issue_gift_card(
            db, GIFT_CARD_AMOUNT, issued_to=account.id,
            expires_at=dt.date.today() + dt.timedelta(days=365),
        )

    have = set(
        (await db.execute(
            select(WishlistItem.product_id).where(WishlistItem.account_id == account.id)
        )).scalars()
    )
    for p in products[:WISHLIST_SIZE]:
        if p.id not in have and len(have) < WISHLIST_SIZE:
            db.add(WishlistItem(account_id=account.id, product_id=p.id))
            have.add(p.id)

    await db.commit()
    wallet = await billing.get_or_create_wallet(db, account.id)
    la = await db.scalar(select(LoyaltyAccount).where(LoyaltyAccount.account_id == account.id))
    summary["demo_customer"] = {
        "phone": phone, "created": user_created, "account_id": account.id,
        "wallet_balance": float(wallet.balance), "points": la.points_balance,
        "gift_card": {"code": card.code, "balance": float(card.balance)},
        "wishlist_items": len(have),
    }
    return summary


async def seed_demo_session(session_factory, **kw) -> dict:
    """seed_demo() in a fresh session from `session_factory` (used by tests)."""
    async with session_factory() as db:
        return await seed_demo(db, **kw)


def _guard() -> None:
    from app.core.config import settings

    if settings.ENV == "prod":
        raise SystemExit("refusing to seed demo content into a prod database (ENV=prod)")


async def _clear_caches() -> None:
    """Drop the product-list and sitemap caches so new images/articles show at once."""
    try:
        from app.core.cache import cache_delete
        from app.services.content import SITEMAP_CACHE_KEY

        await cache_delete("cache:products:active")
        await cache_delete(SITEMAP_CACHE_KEY)
    except Exception as exc:  # Redis down: the caches expire on their own
        print(f"  (cache not cleared: {exc})")


def _print(summary: dict) -> None:
    imgs = summary["product_images"]
    print("Storefront demo seed complete.")
    print(f"  product images -> {len(imgs['set'])} set, {len(imgs['kept_existing'])} kept "
          f"(existing images; --force to replace)")
    for key in ("banners", "articles", "pages"):
        s = summary[key]
        print(f"  {key:<14} -> {s['created']} created, {s['updated']} updated")
    r = summary["ratings"]
    print(f"  ratings        -> {r['created']} created across {r['products']} active products")
    c = summary["demo_customer"]
    print(f"  demo customer  -> phone {c['phone']} ({'created' if c['created'] else 'existing'}), "
          f"account {c['account_id']}")
    print(f"                    wallet Rs {c['wallet_balance']:.2f}, {c['points']} points, "
          f"gift card {c['gift_card']['code']} (Rs {c['gift_card']['balance']:.2f}), "
          f"{c['wishlist_items']} wishlist items")


async def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--phone", default=DEFAULT_PHONE, help="demo customer's OTP phone")
    ap.add_argument("--force", action="store_true",
                    help="also overwrite existing banners/articles/pages and admin-set images")
    args = ap.parse_args(argv)
    _guard()
    from app.core.db import SessionLocal

    summary = await seed_demo_session(SessionLocal, phone=args.phone, force=args.force)
    await _clear_caches()
    _print(summary)
    return summary


if __name__ == "__main__":
    asyncio.run(main())
