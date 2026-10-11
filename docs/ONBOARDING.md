# DRS Backend — What I Need From You (external keys)

Everything in the backend is **built, tested (109 tests), and running dev-stubbed** —
it works today without any of these. Each key below just flips one integration from
stub → live. Put every value in `backend/.env` only (never commit it). After adding
keys, restart the API. Nothing else changes in the code.

Priority: 🔴 needed before real launch · 🟡 needed for that feature · ⚪ optional.

| # | Key(s) in `.env` | Unlocks | How to get it (brief) | Priority |
|---|---|---|---|---|
| 1 | `ANTHROPIC_API_KEY` | AI support bot + admin copilot (`/ai/support`, `/admin/ai/ask`) go live | platform.claude.com → sign in → **API Keys** → Create Key. Billing must be set up. | 🟡 |
| 2 | `GUPSHUP_API_KEY`, `GUPSHUP_SOURCE`, `GUPSHUP_APP_NAME`, `GUPSHUP_SRC_NAME` | Real WhatsApp sends (order/delivery/payment alerts, promos) | gupshup.io → create account → **WhatsApp API** app → get API key + onboard a WABA number (`SOURCE` = that number, e.g. `919999…`). Needs a Facebook Business Manager + WhatsApp Business verification. | 🟡 |
| 3 | `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | Real payments (create/verify, order pay, refunds) instead of the `dev` stub | dashboard.razorpay.com → **Settings → API Keys** → Generate. Use **Test** keys first, then Live after KYC. | 🔴 |
| 4 | `RAZORPAY_WEBHOOK_SECRET` | Secure Razorpay webhook (`/payments/webhook`) — **prod refuses to boot without it** | Razorpay dashboard → **Settings → Webhooks** → Add webhook pointing at `https://<api>/api/v1/payments/webhook`, set a secret, paste it here. | 🔴 |
| 5 | `MSG91_AUTHKEY`, `MSG91_TEMPLATE_ID`, `MSG91_SENDER` | Real OTP SMS (login) instead of the dev `dev_otp` in the response | msg91.com → **API key**; create a **DLT-approved** OTP template → use its template_id. **DLT registration has a long lead time — start this first.** | 🔴 |
| 6 | `SELLER_GSTIN` (+ confirm `SELLER_STATE`) | Correct GSTIN printed on tax invoices | Your GST registration certificate (entity: *Dhanna Ram Sherawat Enterprises*). 15-char GSTIN. | 🔴 |
| 7 | `R2_ENDPOINT`, `R2_ACCESS_KEY`, `R2_SECRET_KEY`, `R2_BUCKET`, `R2_PUBLIC_BASE` | Invoice PDFs + POD photos stored on Cloudflare R2 (CDN) instead of local disk | dash.cloudflare.com → **R2** → create bucket → **Manage API Tokens** → create (endpoint = `https://<account>.r2.cloudflarestorage.com`; public base = your R2 public/custom domain). | 🟡 |
| 8 | `GOOGLE_MAPS_API_KEY` *(not wired yet)* | Real travel times for delivery route optimization (future) | console.cloud.google.com → enable **Routes/Distance Matrix API** → **Credentials** → API key. | ⚪ |
| 9 | `SENTRY_DSN` | Error tracking in prod | sentry.io → create project (Python/FastAPI) → copy the DSN. | ⚪ |

## Prod-only required (set when you deploy, not for local dev)
- `ENV=prod`, a strong 32+ char `JWT_SECRET`, explicit `CORS_ORIGINS` (no `*`),
  and `DB_POOL_MODE=pgbouncer` with the pooled `DATABASE_URL`.
- In prod the app **won't start** unless Razorpay keys + webhook secret (#3, #4) are set.

## Quickest path to a live launch
1. **MSG91 DLT** (#5) — start now, it's the slowest.
2. **Razorpay** (#3, #4) + **GSTIN** (#6) — core money + compliance.
3. **Gupshup** (#2) for WhatsApp, **Anthropic** (#1) for AI — switch on anytime.
4. **R2** (#7) when you want downloadable invoice PDFs / uploaded POD photos.

Hand me any subset and I'll verify the live integration end-to-end.
