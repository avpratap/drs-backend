import logging

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_JWT_SECRET = "dev-only-change-me-to-a-long-random-32plus-byte-string"
_DEV_DATABASE_URL = "postgresql+asyncpg://drs:drs@localhost:5432/drs"
_DEV_REDIS_URL = "redis://localhost:6379/0"


class ProdConfigError(ValueError):
    """ENV=prod with required settings missing/unsafe. The message lists every one."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("ENV")
    @classmethod
    def _validate_env(cls, v: str) -> str:
        # Only "prod" enables the production guards/behaviour; a typo like
        # "production"/"staging" would silently run in dev mode (docs exposed, OTP
        # leaked, payment stubs). Fail fast on anything unexpected.
        if v not in {"dev", "test", "prod"}:
            raise ValueError("ENV must be one of: dev, test, prod")
        return v

    @field_validator("INVOICE_PREFIX")
    @classmethod
    def _validate_invoice_prefix(cls, v: str) -> str:
        # invoice_no is String(32): "{prefix}/CN/{fy:7}/{seq:06d}" leaves 11 chars for it.
        if not v or len(v) > 10 or "/" in v:
            raise ValueError("INVOICE_PREFIX must be 1-10 characters with no '/'")
        return v

    DATABASE_URL: str = _DEV_DATABASE_URL
    REDIS_URL: str = _DEV_REDIS_URL

    # DB pooling. 'direct' = app-side pool (local/dev, direct Postgres).
    # 'pgbouncer' = NullPool + disable asyncpg statement cache (prod behind PgBouncer
    # transaction pooling, where server-side prepared statements must be off).
    DB_POOL_MODE: str = "direct"
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_SSL: bool = False  # require TLS to Postgres (set true for managed prod DB)
    # Optional CA bundle (PEM) to verify the DB server cert against — managed providers
    # (e.g. AWS RDS) use their own CA not in the system trust store. Empty -> default
    # verifying context (system CAs). Only consulted when DB_SSL is true.
    DB_SSL_ROOT_CERT: str = ""

    # Observability
    SENTRY_DSN: str = ""
    LOG_JSON: bool = False

    # Cache
    CACHE_TTL_SECONDS: int = 300

    # Global per-IP rate limit (fixed window). Fail-open if Redis is down.
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_PER_MIN: int = 300
    # Stock policy. True (default): a delivery/consume that exceeds on-hand still goes
    # through and on-hand goes negative (an implicit backorder, reconciled by the next
    # receipt). PRODUCTION SETS THIS FALSE: any delivery or stock consume that would take
    # on-hand below zero is refused with 409 instead of silently backordering.
    ALLOW_NEGATIVE_STOCK: bool = True
    # Reverse proxies in front of the app that APPEND to X-Forwarded-For (Railway: 1).
    # 0 = use the socket peer and ignore the header (dev / direct exposure).
    TRUSTED_PROXY_HOPS: int = Field(default=0, ge=0)
    # Prod refuses TRUSTED_PROXY_HOPS=0 (behind a proxy every client would share the
    # proxy's IP: one rate-limit / OTP-per-IP bucket for everyone) unless the app really
    # is exposed directly, stated explicitly here.
    ALLOW_DIRECT_CLIENT_IP: bool = False
    # Bearer token the Prometheus scraper must send to /metrics ("" = open, dev only).
    METRICS_TOKEN: str = ""
    JWT_SECRET: str = _DEV_JWT_SECRET
    JWT_ALG: str = "HS256"
    ACCESS_TOKEN_MINUTES: int = 15
    REFRESH_TOKEN_DAYS: int = 14
    ENV: str = "dev"
    CORS_ORIGINS: str = "http://localhost:3000"

    # MSG91 (OTP SMS). SMS is only sent when BOTH authkey + template_id are set
    # (template_id requires a DLT-approved OTP template). Until then, dev returns the OTP.
    MSG91_AUTHKEY: str = ""
    MSG91_TEMPLATE_ID: str = ""
    MSG91_SENDER: str = ""
    MSG91_OTP_EXPIRY_MIN: int = 5

    # GST invoicing (seller = Dhanna Ram Sherawat Enterprises)
    SELLER_LEGAL_NAME: str = "Dhanna Ram Sherawat Enterprises"
    SELLER_GSTIN: str = ""  # 15-char GSTIN once registered
    SELLER_STATE: str = "Rajasthan"  # seller's place of business (for intra/inter-state)
    INVOICE_PREFIX: str = "DRS"

    # Subscriptions / daily cutoff
    TIMEZONE: str = "Asia/Kolkata"
    CUTOFF_HOUR: int = 23  # local hour after which tomorrow's order is frozen
    WALLET_LOW_THRESHOLD: float = 100.0  # notify when prepaid wallet drops below this

    # Razorpay (payments). Empty keys -> dev stub (verify accepts signature "dev").
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""  # empty -> dev stub accepts unsigned webhooks

    # Object storage (Cloudflare R2 / S3-compatible). Empty -> local dev-stub.
    R2_ENDPOINT: str = ""
    R2_ACCESS_KEY: str = ""
    R2_SECRET_KEY: str = ""
    R2_BUCKET: str = ""
    R2_PUBLIC_BASE: str = ""  # public CDN base URL for stored objects
    # Bucket for private documents (invoice PDFs) — must have NO public/CDN binding;
    # objects are only reachable via short-lived presigned URLs.
    R2_PRIVATE_BUCKET: str = ""
    STORAGE_DIR: str = ".storage"  # dev-stub local directory

    # WhatsApp (Gupshup BSP). Empty key -> dev stub (messages logged, not sent).
    WHATSAPP_PROVIDER: str = "gupshup"
    GUPSHUP_API_KEY: str = ""
    GUPSHUP_APP_NAME: str = ""
    GUPSHUP_SOURCE: str = ""  # the WABA sender number (e.g. 919999999999)
    GUPSHUP_SRC_NAME: str = ""  # registered src.name / app display name

    # Shipping (Shiprocket). Empty creds -> dev stub (fake AWBs/serviceability, no calls).
    SHIPROCKET_EMAIL: str = ""
    SHIPROCKET_PASSWORD: str = ""
    SHIPROCKET_PICKUP_LOCATION: str = "Primary"  # pickup nickname registered in Shiprocket
    SHIPROCKET_CHANNEL_ID: str = ""
    SHIPROCKET_WEBHOOK_TOKEN: str = ""  # shared secret echoed in the tracking webhook
    SHIPROCKET_BASE_URL: str = "https://apiv2.shiprocket.in/v1/external"
    PICKUP_PINCODE: str = "302001"  # seller origin pincode (Jaipur) for serviceability

    # FCM push (HTTP v1). Empty -> dev stub (logs, no send).
    FCM_PROJECT_ID: str = ""
    FCM_CREDENTIALS_JSON: str = ""  # service-account JSON (raw or path); empty -> stub

    # Newsletter one-click-unsubscribe token signing. A dedicated secret (not JWT_SECRET)
    # so rotating JWT auth doesn't invalidate unsubscribe links already in inboxes; falls
    # back to JWT_SECRET when unset (dev).
    NEWSLETTER_UNSUB_SECRET: str = ""

    # Email (SMTP). Empty host -> dev stub (logs, no send).
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "DRS <no-reply@drs.co.in>"

    # AI/LLM (Claude). Empty key -> dev stub (templated answers, no external call).
    ANTHROPIC_API_KEY: str = ""
    CLAUDE_MODEL: str = "claude-opus-4-8"
    AI_TIMEOUT_SECONDS: float = 30.0  # hard cap per Claude call
    AI_MAX_RETRIES: int = 2  # transient-failure retries (exponential backoff)
    AI_MAX_TOKENS: int = 1024  # output token ceiling per call
    AI_DAILY_CALLS_PER_USER: int = 50  # cost/abuse guard on customer-facing AI

    @model_validator(mode="after")
    def _prod_boot_guard(self) -> "Settings":
        """Fail fast at boot if a prod deploy is missing a hard security/infra requirement,
        rather than running insecurely or crashing cryptically later. Collects EVERY
        problem and raises ONE error naming each setting. No-op unless ENV == 'prod'."""
        if self.ENV != "prod":
            return self
        problems: list[tuple[str, str]] = []  # (setting name(s), what is wrong)

        def need(ok: bool, names: str, why: str) -> None:
            if not ok:
                problems.append((names, why))

        need(self.DATABASE_URL != _DEV_DATABASE_URL, "DATABASE_URL",
             "still the local dev default (localhost)")
        need(self.REDIS_URL != _DEV_REDIS_URL, "REDIS_URL",
             "still the local dev default (localhost)")
        need(self.JWT_SECRET != _DEV_JWT_SECRET and len(self.JWT_SECRET) >= 32, "JWT_SECRET",
             "must be a unique value of at least 32 bytes")
        need(self.DB_SSL, "DB_SSL", "must be true (TLS to the managed database)")
        need(self.DB_POOL_MODE in {"direct", "pgbouncer"}, "DB_POOL_MODE",
             "must be 'direct' or 'pgbouncer'")
        # Without both, OTPs are generated but never delivered (and never returned in
        # prod), so every OTP login would silently fail. MSG91 only sends with a
        # DLT-approved template id, hence both are required.
        need(bool(self.MSG91_AUTHKEY.strip() and self.MSG91_TEMPLATE_ID.strip()),
             "MSG91_AUTHKEY, MSG91_TEMPLATE_ID",
             "not set: OTP SMS would never be sent and nobody could log in")
        need(bool(self.RAZORPAY_KEY_ID and self.RAZORPAY_KEY_SECRET),
             "RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET", "not set: the payment dev stub would run")
        need(bool(self.RAZORPAY_WEBHOOK_SECRET), "RAZORPAY_WEBHOOK_SECRET",
             "not set: unsigned payment webhooks would be accepted")
        need(bool(self.SELLER_GSTIN), "SELLER_GSTIN", "not set: GST tax invoices are invalid")
        need(bool(self.SHIPROCKET_WEBHOOK_TOKEN), "SHIPROCKET_WEBHOOK_TOKEN", "not set")
        need("localhost" not in self.CORS_ORIGINS, "CORS_ORIGINS", "must not include localhost")
        # (Moved here from main.py so the ARQ worker — which never imports main.py — is
        # guarded too.)
        need("*" not in [o.strip() for o in self.CORS_ORIGINS.split(",")], "CORS_ORIGINS",
             "must be an explicit allow-list (no '*')")
        need(self.TRUSTED_PROXY_HOPS > 0 or self.ALLOW_DIRECT_CLIENT_IP, "TRUSTED_PROXY_HOPS",
             "must be the number of proxies in front of the app (e.g. 1); 0 behind a proxy "
             "collapses every client onto the proxy's IP for rate limiting and OTP caps "
             "(set ALLOW_DIRECT_CLIENT_IP=true only if the app is exposed directly)")
        # Without R2 the local-disk stub would store uploads/invoices on ephemeral disk
        # and serve them from /files; invoices need a bucket with NO public binding.
        need(bool(self.R2_ENDPOINT and self.R2_ACCESS_KEY and self.R2_SECRET_KEY and self.R2_BUCKET),
             "R2_ENDPOINT, R2_ACCESS_KEY, R2_SECRET_KEY, R2_BUCKET", "not all set")
        need(bool(self.R2_PRIVATE_BUCKET) and self.R2_PRIVATE_BUCKET != self.R2_BUCKET,
             "R2_PRIVATE_BUCKET", "must be set and differ from the public R2_BUCKET")
        # Push: half a configuration is a mistake; none at all means push is not live yet
        # (the dev stub logs instead of sending) — allowed, but said out loud.
        fcm = (bool(self.FCM_PROJECT_ID.strip()), bool(self.FCM_CREDENTIALS_JSON.strip()))
        need(fcm[0] == fcm[1], "FCM_PROJECT_ID, FCM_CREDENTIALS_JSON",
             "set both or neither (only one is set)")
        if problems:
            lines = "\n".join(f"  - {names}: {why}" for names, why in problems)
            raise ProdConfigError(
                "Refusing to start: ENV=prod but these settings are missing or unsafe:\n"
                f"{lines}\nSet them in the deployment environment (see docs/DEPLOY.md)."
            )
        if not any(fcm):
            logging.getLogger("drs.config").warning(
                "FCM_PROJECT_ID/FCM_CREDENTIALS_JSON not set: push notifications are not sent "
                "(dev stub logs them)"
            )
        return self

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


def _load_settings() -> Settings:
    """Settings for this process. A prod misconfiguration surfaces as ONE plain error
    listing every missing setting, not pydantic's validation-error wrapper."""
    try:
        return Settings()
    except ValidationError as exc:
        for err in exc.errors():
            original = (err.get("ctx") or {}).get("error")
            if isinstance(original, ProdConfigError):
                raise RuntimeError(str(original)) from None
        raise


settings = _load_settings()
