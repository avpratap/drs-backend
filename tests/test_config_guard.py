"""#111 prod boot-guard: a prod deploy must fail fast on insecure/missing config."""
import pytest

from app.core.config import Settings

_GOOD = {
    "ENV": "prod",
    "DATABASE_URL": "postgresql+asyncpg://drs:pw@db.internal:5432/drs",
    "REDIS_URL": "redis://cache.internal:6379/0",
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
    "MSG91_AUTHKEY": "msg91-auth",
    "MSG91_TEMPLATE_ID": "dlt-template",
    "TRUSTED_PROXY_HOPS": 1,
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
        # OTP login: without both, codes are never delivered (nor returned in prod).
        {"MSG91_AUTHKEY": ""},
        {"MSG91_TEMPLATE_ID": ""},
        {"MSG91_AUTHKEY": "   "},
        # Behind a proxy, 0 hops makes every client share the proxy's IP.
        {"TRUSTED_PROXY_HOPS": 0},
        # Left on the local dev defaults: would crash on first connect instead of at boot.
        {"DATABASE_URL": "postgresql+asyncpg://drs:drs@localhost:5432/drs"},
        {"REDIS_URL": "redis://localhost:6379/0"},
        # Half an FCM configuration.
        {"FCM_PROJECT_ID": "drs-prod"},
        {"FCM_CREDENTIALS_JSON": "{}"},
    ],
)
def test_prod_config_rejects_insecure(override):
    bad = {**_GOOD, **override}
    with pytest.raises(ValueError):
        Settings(**bad)


def test_dev_config_not_guarded():
    # The dev defaults (dev JWT secret, DB_SSL off) are fine when ENV != prod.
    assert Settings(ENV="dev").ENV == "dev"


def test_prod_error_names_msg91():
    with pytest.raises(ValueError, match="MSG91_AUTHKEY, MSG91_TEMPLATE_ID: not set"):
        Settings(**{**_GOOD, "MSG91_TEMPLATE_ID": ""})


def test_direct_exposure_must_be_explicit():
    with pytest.raises(ValueError, match="TRUSTED_PROXY_HOPS"):
        Settings(**{**_GOOD, "TRUSTED_PROXY_HOPS": 0})
    ok = Settings(**{**_GOOD, "TRUSTED_PROXY_HOPS": 0, "ALLOW_DIRECT_CLIENT_IP": True})
    assert ok.TRUSTED_PROXY_HOPS == 0


def test_negative_proxy_hops_rejected():
    with pytest.raises(ValueError):
        Settings(ENV="dev", TRUSTED_PROXY_HOPS=-1)


def test_every_problem_is_listed_in_one_error():
    """One error naming EVERY missing/unsafe setting, one per line."""
    from app.core.config import ProdConfigError

    bad = {**_GOOD, "MSG91_AUTHKEY": "", "SELLER_GSTIN": "", "REDIS_URL": "redis://localhost:6379/0"}
    with pytest.raises(ValueError) as ei:
        Settings(**bad)
    original = ei.value.errors()[0]["ctx"]["error"]
    assert isinstance(original, ProdConfigError)
    msg = str(original)
    assert msg.startswith("Refusing to start: ENV=prod")
    listed = [ln for ln in msg.splitlines() if ln.startswith("  - ")]
    assert [ln.split(":")[0].strip("- ").strip() for ln in listed] == [
        "REDIS_URL", "MSG91_AUTHKEY, MSG91_TEMPLATE_ID", "SELLER_GSTIN",
    ]


def test_fcm_unset_is_allowed_both_set_is_allowed():
    assert Settings(**_GOOD).FCM_PROJECT_ID == ""
    s = Settings(**{**_GOOD, "FCM_PROJECT_ID": "drs-prod", "FCM_CREDENTIALS_JSON": "{}"})
    assert s.FCM_PROJECT_ID == "drs-prod"


def test_boot_fails_fast_with_a_plain_error_listing_what_is_missing():
    """Importing the config with ENV=prod and missing settings stops the process with ONE
    readable RuntimeError (not pydantic's wrapper), naming each missing setting."""
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if not k.startswith(("MSG91_", "R2_"))}
    env.update(ENV="prod", MSG91_AUTHKEY="", MSG91_TEMPLATE_ID="")
    r = subprocess.run([sys.executable, "-c", "import app.core.config"], env=env,
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "RuntimeError: Refusing to start: ENV=prod" in r.stderr
    assert "MSG91_AUTHKEY, MSG91_TEMPLATE_ID: not set" in r.stderr
    assert "ValidationError" not in r.stderr


def test_guard_is_a_noop_in_dev_and_test():
    for env in ("dev", "test"):
        assert Settings(ENV=env, MSG91_AUTHKEY="", FCM_PROJECT_ID="half").ENV == env
