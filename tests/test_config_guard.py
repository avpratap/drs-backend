"""#111 prod boot-guard: a prod deploy must fail fast on insecure/missing config."""
import pytest

from app.core.config import Settings

_GOOD = {
    "ENV": "prod",
    "JWT_SECRET": "x" * 40,
    "DB_SSL": True,
    "DB_POOL_MODE": "pgbouncer",
    "RAZORPAY_WEBHOOK_SECRET": "whsec",
    "CORS_ORIGINS": "https://drs.co.in",
}


def test_prod_config_ok_with_all_requirements():
    s = Settings(**_GOOD)
    assert s.ENV == "prod"


@pytest.mark.parametrize(
    "override",
    [
        {"JWT_SECRET": "dev-only-change-me-to-a-long-random-32plus-byte-string"},
        {"JWT_SECRET": "too-short"},
        {"DB_SSL": False},
        {"RAZORPAY_WEBHOOK_SECRET": ""},
        {"CORS_ORIGINS": "http://localhost:3000"},
    ],
)
def test_prod_config_rejects_insecure(override):
    bad = {**_GOOD, **override}
    with pytest.raises(ValueError):
        Settings(**bad)


def test_dev_config_not_guarded():
    # The dev defaults (dev JWT secret, DB_SSL off) are fine when ENV != prod.
    assert Settings(ENV="dev").ENV == "dev"
