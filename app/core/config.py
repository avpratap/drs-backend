from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "postgresql+asyncpg://drs:drs@localhost:5432/drs"
    REDIS_URL: str = "redis://localhost:6379/0"

    # DB pooling. 'direct' = app-side pool (local/dev, direct Postgres).
    # 'pgbouncer' = NullPool + disable asyncpg statement cache (prod behind PgBouncer
    # transaction pooling, where server-side prepared statements must be off).
    DB_POOL_MODE: str = "direct"
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10

    # Observability
    SENTRY_DSN: str = ""
    LOG_JSON: bool = False

    # Cache
    CACHE_TTL_SECONDS: int = 300
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

    # Subscriptions / daily cutoff
    TIMEZONE: str = "Asia/Kolkata"
    CUTOFF_HOUR: int = 23  # local hour after which tomorrow's order is frozen
    WALLET_LOW_THRESHOLD: float = 100.0  # notify when prepaid wallet drops below this

    # Razorpay (payments). Empty keys -> dev stub (verify accepts signature "dev").
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""  # empty -> dev stub accepts unsigned webhooks

    # WhatsApp (Gupshup BSP). Empty key -> dev stub (messages logged, not sent).
    WHATSAPP_PROVIDER: str = "gupshup"
    GUPSHUP_API_KEY: str = ""
    GUPSHUP_APP_NAME: str = ""
    GUPSHUP_SOURCE: str = ""  # the WABA sender number (e.g. 919999999999)
    GUPSHUP_SRC_NAME: str = ""  # registered src.name / app display name

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()
