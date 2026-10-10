from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "postgresql+asyncpg://drs:drs@localhost:5432/drs"
    REDIS_URL: str = "redis://localhost:6379/0"
    JWT_SECRET: str = "dev-only-change-me"
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

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()
