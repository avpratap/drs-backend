"""Razorpay payment flow. With no keys configured, runs in dev-stub mode
(create returns a fake order id; verify accepts the signature literal "dev")."""
import hashlib
import hmac
import secrets

import httpx

from app.core.config import settings

RAZORPAY_ORDERS_URL = "https://api.razorpay.com/v1/orders"


def gateway_enabled() -> bool:
    return bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)


async def create_gateway_order(amount: float) -> str:
    """Create a Razorpay order and return its id (amount in paise)."""
    if not gateway_enabled():
        return "order_dev_" + secrets.token_hex(8)
    paise = int(round(amount * 100))
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            RAZORPAY_ORDERS_URL,
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
            json={"amount": paise, "currency": "INR", "payment_capture": 1},
        )
    resp.raise_for_status()
    return resp.json()["id"]


def verify_signature(order_id: str, payment_id: str, signature: str) -> bool:
    if not gateway_enabled():
        return signature == "dev"  # dev stub
    expected = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
