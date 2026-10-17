# DRS — What I Need From You (one-week collection checklist)

**Status:** the backend is fully built, tested (**260 tests passing**), and running **dev-stubbed** — it works today with *zero* external keys. Every item below just flips one integration from **stub → live**, or is needed to start building the **website / apps / admin**.

> **Where things go:** all keys go in **`backend/.env` only** — never committed. After adding keys, restart the API; no code changes. Hand me any subset and I verify that integration live end-to-end.

**Legend:** 🔴 needed before real launch · 🟡 needed for that feature · ⚪ optional/later.

---

## A. External service keys (go in `backend/.env`)

| # | `.env` key(s) | Unlocks | How to get it | When | Pri |
|---|---|---|---|---|---|
| 1 | `MSG91_AUTHKEY`, `MSG91_TEMPLATE_ID`, `MSG91_SENDER` | Real **OTP login SMS** (otherwise dev returns the OTP in the response) | msg91.com → get **Auth Key**. Separately do **TRAI DLT registration** (register entity on a DLT portal like Jio/Vilpower/Airtel, then register an **OTP template** → that gives the `TEMPLATE_ID`). `SENDER` = your 6-char header (e.g. `DRSMLK`). | **Start day 1 — slowest** | 🔴 |
| 2 | `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | Real **payments** (checkout, order pay, refunds, UPI Autopay) | dashboard.razorpay.com → **Settings → API Keys → Generate**. Use **Test** keys first; **Live** keys need business KYC + a linked bank account. | Week 1 | 🔴 |
| 3 | `RAZORPAY_WEBHOOK_SECRET` | Secure payment webhook (**prod won't boot without it**) | Razorpay → **Settings → Webhooks → Add** → URL `https://<api>/api/v1/payments/webhook`, set a secret, paste here. (Do after the API is deployed.) | At deploy | 🔴 |
| 4 | `SELLER_GSTIN` (confirm `SELLER_STATE`) | Correct **GSTIN on tax invoices** | Your GST certificate for *Dhanna Ram Sherawat Enterprises* — 15-char GSTIN. Confirm state is **Rajasthan**. | Week 1 | 🔴 |
| 5 | `SHIPROCKET_EMAIL`, `SHIPROCKET_PASSWORD`, `SHIPROCKET_CHANNEL_ID`, `SHIPROCKET_PICKUP_LOCATION`, `SHIPROCKET_WEBHOOK_TOKEN` | Real **courier shipping** for D2C orders (AWB, labels, tracking, serviceability) | shiprocket.in → sign up → add a **pickup address** (the nickname = `PICKUP_LOCATION`), create an **API user** (Settings → API → *that* email/password, not your login), note the **Channel ID**. Set any `WEBHOOK_TOKEN` string and configure their tracking webhook to `…/api/v1/shipping/webhook`. Confirm `PICKUP_PINCODE` (currently `302001`, Jaipur). | Week 1–2 | 🟡 |
| 6 | `GUPSHUP_API_KEY`, `GUPSHUP_SOURCE`, `GUPSHUP_APP_NAME`, `GUPSHUP_SRC_NAME` | Real **WhatsApp** alerts (order/delivery/payment) + promos | gupshup.io → create a **WhatsApp API** app → API key; onboard a **WABA number** (needs a Facebook Business Manager + WhatsApp Business verification). `SOURCE` = that number (e.g. `9199…`). | Week 2+ | 🟡 |
| 7 | `R2_ENDPOINT`, `R2_ACCESS_KEY`, `R2_SECRET_KEY`, `R2_BUCKET`, `R2_PUBLIC_BASE` | Invoice PDFs + POD/upload photos on **Cloudflare R2 CDN** (else local disk) | dash.cloudflare.com → **R2** → create bucket → **Manage API Tokens** → create. `ENDPOINT = https://<account>.r2.cloudflarestorage.com`; `PUBLIC_BASE` = the bucket's public/custom domain. | Week 2 | 🟡 |
| 8 | `FCM_PROJECT_ID`, `FCM_CREDENTIALS_JSON` | Real **push notifications** to the apps | console.firebase.google.com → create project → **Project settings → Service accounts → Generate new private key** (that JSON, raw or a file path). Only **FCM** is used — not Firebase Auth/DB. | When apps build | 🟡 |
| 9 | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | Real **transactional email** (receipts/newsletter confirm) | Any provider — e.g. Amazon SES, Zoho Mail, or Gmail app-password. `FROM` should be on your domain. | Week 2+ | 🟡 |
| 10 | `ANTHROPIC_API_KEY` | **AI** support bot + admin copilot go live | platform.claude.com → **API Keys → Create Key** (billing set up). | Anytime | 🟡 |
| 11 | `SENTRY_DSN` | Error tracking in production | sentry.io → new **Python/FastAPI** project → copy DSN. | At deploy | ⚪ |

### Prod-only (set at deploy, not for local dev)
`ENV=prod` · strong 32+char `JWT_SECRET` · explicit `CORS_ORIGINS` (your real domains, no `*`) · `DB_POOL_MODE=pgbouncer` + pooled `DATABASE_URL` · `DB_SSL=true`. In prod the app refuses to start without Razorpay keys + webhook secret (#2, #3).

---

## B. Business & legal details (text values — email them to me)

| Detail | Why | Pri |
|---|---|---|
| **GSTIN** + registered business name/address | Tax invoices, Razorpay KYC | 🔴 |
| **FSSAI licence number** + logo | Must appear on packaging & site footer (dairy) | 🔴 |
| **Bank account** (for Razorpay settlement) | Receive customer payments | 🔴 |
| **Pickup/warehouse address + pincode** | Shiprocket origin, serviceability | 🔴 |
| **Support phone + email + WhatsApp number** | Shown on site/app, used as sender identities | 🔴 |
| **Delivery areas / pincodes served** | Serviceability checks, cutoff zones | 🟡 |
| **Product catalogue**: names, SKUs, pack sizes, prices (retail + any B2B tiers), HSN codes, GST %, photos, descriptions | Seed the real catalog | 🔴 |
| **Return/refund, privacy, T&C policy text** | Legal pages (DPDP consent copy too) | 🟡 |
| **Logo files** (SVG/PNG) + brand assets | Already have Creamio direction; need final files | 🟡 |

---

## C. Accounts/platforms to create for the frontends

| Platform | For | Notes | Pri |
|---|---|---|---|
| **Domain name** (e.g. drs.co.in) + DNS access | Website, email, API subdomain | Buy + give me DNS or delegate a subdomain | 🔴 |
| **Vercel account** | Hosting the Next.js website + admin | Free tier fine to start; connect the GitHub repo | 🔴 |
| **Railway account** (then Fly.io later) | Hosting the FastAPI backend + Postgres + Redis | Railway for MVP; I'll configure | 🔴 |
| **GitHub access** | You already own `avpratap/drs-backend` | I'll add web/mobile repos | 🔴 |
| **Google Play Console** ($25 one-time) | Publish the Android customer + delivery apps | Needed only when apps are ready | 🟡 |
| **Apple Developer** ($99/yr) | Publish iOS apps | Only if you want iOS now (can defer) | ⚪ |
| **Google Maps Platform key** | Maps/geocoding/route display in delivery app | console.cloud.google.com → enable Maps SDK + Geocoding → API key | 🟡 |
| **Firebase project** | FCM push (see #8) | One project covers both apps | 🟡 |

---

## D. Suggested one-week collection order (fastest payoff)

- **Day 1:** Kick off **DLT + MSG91** (#1) — longest lead time. Buy the **domain**. Create **Razorpay** test account (#2).
- **Day 2–3:** Send me **GSTIN, FSSAI, bank, pickup address, support contacts** (Section B). Create **Vercel + Railway** accounts (Section C).
- **Day 4–5:** **Shiprocket** signup + pickup + API user (#5). **Cloudflare R2** bucket (#7).
- **Day 6–7:** Compile the **real product catalogue** (Section B) — this is what makes the store feel real. **Gupshup WhatsApp** + **Firebase/FCM** can trail.

You don't need *all* of it to start — Section B catalogue + Vercel/Railway/domain are enough for me to begin building and show you a working site.

---

## E. What each frontend needs to go live (so you know what's coming)

- **Admin dashboard** (Next.js + Refine): **no external keys** — runs against the backend immediately. Just hosting (Vercel) + your login. *Build-first candidate.*
- **Customer website** (Next.js): domain + Razorpay (test) + catalogue. GST/FSSAI for footer. R2 for images (optional early).
- **Customer app** (Flutter): Razorpay + FCM (#8) + Maps (optional) + Play/App Store accounts to publish.
- **Delivery app** (Flutter): FCM (#8) + Maps key (route/navigation) + Play Store account.

---

*Questions on any row? Tell me and I'll expand it. Send keys/details as you get them — partial is fine; I'll wire and verify each one.*
