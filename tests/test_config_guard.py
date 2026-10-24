"""#111 prod boot-guard: a prod deploy must fail fast on insecure/missing config."""
import pytest

from app.core.config import Settings

_GOOD = {
    "ENV": "prod",
    "JWT_SECRET": "x" * 40,
    "DB_SSL": True,
    "DB_POOL_MODE": "pgbouncer",
    "RAZORPAY_WEBHOOK_SECRET": "whsec",
    "RAZORPAY_KEY_ID": "rzp_live_x",
    "RAZORPAY_KEY_SECRET": "secret",
    "SELLER_GSTIN": "08ABCDE1234F1Z5",
    "SHIPROCKET_WEBHOOK_TOKEN": "srtoken",
    "R2_ENDPOINT": "https://r2.example",
    "R2_ACCESS_KEY": "ak",
    "R2_SECRET_KEY": "sk",
    "R2_BUCKET": "public-assets",
    "R2_PRIVATE_BUCKET": "private-docs",
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
        # I11: guards formerly only in main.py (worker never imported them) + storage.
        {"CORS_ORIGINS": "*"},
        {"RAZORPAY_KEY_ID": ""},
        {"RAZORPAY_KEY_SECRET": ""},
        {"SELLER_GSTIN": ""},
        {"SHIPROCKET_WEBHOOK_TOKEN": ""},
        {"R2_BUCKET": ""},
        {"R2_PRIVATE_BUCKET": ""},
        {"R2_PRIVATE_BUCKET": "public-assets"},  # same as the public bucket
    ],
)
def test_prod_config_rejects_insecure(override):
    bad = {**_GOOD, **override}
    with pytest.raises(ValueError):
        Settings(**bad)


def test_dev_config_not_guarded():
    # The dev defaults (dev JWT secret, DB_SSL off) are fine when ENV != prod.
    assert Settings(ENV="dev").ENV == "dev"
