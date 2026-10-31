"""API contract the web and mobile clients generate against.

- openapi/openapi.json (the committed spec clients generate from) must match the app:
  re-export with `uv run python scripts/export_openapi.py` after any API change.
- Every operation the mobile app's generated client calls (drs-mobile
  openapi/drs-core.openapi.yaml, incl. the M5 customer-account branch) stays at the
  same operationId, method and path; renaming/moving one breaks the app.
- Every API operation has an explicit operationId, a tag and a declared response.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (operationId, method, path) used by drs-mobile, plus ops the apps are adopting.
MOBILE_OPERATIONS = [
    ("add_cart_item", "POST", "/api/v1/cart/items"),
    ("cancel_order", "POST", "/api/v1/orders/{order_id}/cancel"),
    ("cancel_subscription", "POST", "/api/v1/subscriptions/{sub_id}/cancel"),
    ("cart_quote", "POST", "/api/v1/cart/quote"),
    ("checkout_cart", "POST", "/api/v1/cart/checkout"),
    ("create_address", "POST", "/api/v1/addresses"),
    ("create_order", "POST", "/api/v1/orders"),
    ("create_payment", "POST", "/api/v1/payments/create"),
    ("create_subscription", "POST", "/api/v1/subscriptions"),
    ("delete_address", "DELETE", "/api/v1/addresses/{address_id}"),
    ("delete_device_token", "DELETE", "/api/v1/account/device-tokens"),
    ("delete_my_rating", "DELETE", "/api/v1/products/{product_id}/ratings"),
    ("deliver_order", "POST", "/api/v1/delivery/orders/{order_id}/deliver"),
    ("delivery_route", "GET", "/api/v1/delivery/orders/route"),
    ("delivery_summary", "GET", "/api/v1/delivery/summary"),
    ("fail_delivery_order", "POST", "/api/v1/delivery/orders/{order_id}/fail"),
    ("get_cart", "GET", "/api/v1/cart"),
    ("get_consents", "GET", "/api/v1/consents"),
    ("get_me", "GET", "/api/v1/auth/me"),
    ("get_order", "GET", "/api/v1/orders/{order_id}"),
    ("get_product", "GET", "/api/v1/products/{product_id}"),
    ("get_product_full", "GET", "/api/v1/products/{product_id}/full"),
    ("get_profile", "GET", "/api/v1/profile"),
    ("get_subscription", "GET", "/api/v1/subscriptions/{sub_id}"),
    ("get_subscription_plan", "GET", "/api/v1/subscriptions/{sub_id}/plan"),
    ("list_addresses", "GET", "/api/v1/addresses"),
    ("list_delivery_orders", "GET", "/api/v1/delivery/orders"),
    ("list_orders", "GET", "/api/v1/orders"),
    ("list_product_categories", "GET", "/api/v1/products/categories"),
    ("list_product_ratings", "GET", "/api/v1/products/{product_id}/ratings"),
    ("list_products", "GET", "/api/v1/products"),
    ("list_subscriptions", "GET", "/api/v1/subscriptions"),
    ("logout", "POST", "/api/v1/auth/logout"),
    ("my_billing", "GET", "/api/v1/billing/me"),
    ("my_wallet_transactions", "GET", "/api/v1/billing/transactions"),
    ("pause_subscription", "POST", "/api/v1/subscriptions/{sub_id}/pause"),
    ("rate_product", "POST", "/api/v1/products/{product_id}/ratings"),
    ("record_consent", "POST", "/api/v1/consents"),
    ("refresh_token", "POST", "/api/v1/auth/refresh"),
    ("register_device_token", "POST", "/api/v1/account/device-tokens"),
    ("remove_cart_item", "DELETE", "/api/v1/cart/items/{item_id}"),
    ("request_otp", "POST", "/api/v1/auth/otp/request"),
    ("resume_subscription", "POST", "/api/v1/subscriptions/{sub_id}/resume"),
    ("set_cart_item_qty", "PATCH", "/api/v1/cart/items/{item_id}"),
    ("set_default_address", "POST", "/api/v1/addresses/{address_id}/default"),
    ("set_override", "POST", "/api/v1/subscriptions/{sub_id}/overrides"),
    ("set_subscription_items", "PUT", "/api/v1/subscriptions/{sub_id}/items"),
    ("start_delivery_route", "POST", "/api/v1/delivery/route/start"),
    ("toggle_review_helpful", "POST", "/api/v1/products/{product_id}/ratings/{rating_id}/helpful"),
    ("track_order", "GET", "/api/v1/orders/{order_id}/tracking"),
    ("unregister_device_token_body", "POST", "/api/v1/account/device-tokens/unregister"),
    ("update_address", "PATCH", "/api/v1/addresses/{address_id}"),
    ("update_profile", "PATCH", "/api/v1/profile"),
    ("upload_pod_asset", "POST", "/api/v1/delivery/uploads"),
    ("verify_otp", "POST", "/api/v1/auth/otp/verify"),
    ("verify_payment", "POST", "/api/v1/payments/verify"),
]


def _spec() -> dict:
    # Imported lazily (as conftest does): importing app.main at collection time sets up
    # logging before pytest's capture and breaks caplog-based tests elsewhere.
    from app.main import app

    return app.openapi()


def _ops() -> dict[str, tuple[str, str]]:
    return {
        o["operationId"]: (m.upper(), path)
        for path, d in _spec()["paths"].items()
        for m, o in d.items()
        if isinstance(o, dict) and "operationId" in o
    }


def test_committed_openapi_is_current():
    committed = (ROOT / "openapi" / "openapi.json").read_text()
    current = json.dumps(_spec(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    assert committed == current, (
        "openapi/openapi.json is stale: run `uv run python scripts/export_openapi.py`"
    )


def test_mobile_operations_keep_their_ids_methods_and_paths():
    ops = _ops()
    broken = [
        f"{op}: expected {m} {p}, got {ops.get(op)}"
        for op, m, p in MOBILE_OPERATIONS
        if ops.get(op) != (m, p)
    ]
    assert not broken, "mobile client contract broken:\n" + "\n".join(broken)


def test_every_operation_is_explicit_tagged_and_typed():
    spec = _spec()
    problems = []
    for path, d in spec["paths"].items():
        if not path.startswith("/api/"):
            continue
        for m, o in d.items():
            if not isinstance(o, dict):
                continue
            op = o.get("operationId", "")
            # FastAPI's auto-generated ids look like `name_api_v1_path_method`.
            if not op or re.search(r"_api_v1_", op):
                problems.append(f"{m.upper()} {path}: no explicit operationId ({op})")
            if not o.get("tags"):
                problems.append(f"{op}: no tag")
            ok = [c for c in o.get("responses", {}) if c.startswith("2")]
            if not ok:
                problems.append(f"{op}: no 2xx response declared")
    assert not problems, "\n".join(problems)
