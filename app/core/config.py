from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    DATABASE_URL: str = "postgresql+asyncpg://drs:drs@localhost:5432/drs"
    REDIS_URL: str = "redis://localhost:6379/0"

    # DB pooling. 'direct' = app-side pool (local/dev, direct Postgres).
    # 'pgbouncer' = NullPool + disable asyncpg statement cache (prod behind PgBouncer
    # transaction pooling, where server-side prepared statements must be off).
    DB_POOL_MODE: str = "direct"
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_SSL: bool = False  # require TLS to Postgres (set true for managed prod DB)

    # Observability
    SENTRY_DSN: str = ""
    LOG_JSON: bool = False

    # Cache
    CACHE_TTL_SECONDS: int = 300

    # Global per-IP rate limit (fixed window). Fail-open if Redis is down.
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_PER_MIN: int = 300
    JWT_SECRET: str = "dev-only-change-me-to-a-long-random-32plus-byte-string"
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

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()
