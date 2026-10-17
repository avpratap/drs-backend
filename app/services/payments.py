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
    paise = round(amount * 100)
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
        if settings.ENV == "prod":
            return False  # never accept the dev stub in prod (fail closed)
        return signature == "dev"  # dev stub
    expected = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def create_gateway_refund(
    gateway_payment_id: str | None, amount: float, *, idempotency_key: str | None = None
) -> str:
    """Issue a refund against a captured payment. Dev stub returns a synthetic id.

    A stable `idempotency_key` (derived from our Refund row id) is sent as Razorpay's
    `Idempotency-Key` header so a retry after a lost response / crash cannot issue a
    SECOND real refund — the gateway returns the original refund instead."""
    if not gateway_enabled() or not gateway_payment_id:
        return "rfnd_dev_" + secrets.token_hex(8)
    paise = round(amount * 100)
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"https://api.razorpay.com/v1/payments/{gateway_payment_id}/refund",
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
            json={"amount": paise},
            headers=headers,
        )
    resp.raise_for_status()
    return resp.json()["id"]


def verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    """Razorpay signs webhook bodies with the webhook secret (HMAC-SHA256)."""
    if not settings.RAZORPAY_WEBHOOK_SECRET:
        return settings.ENV != "prod"  # dev stub accepts unsigned; prod fails closed
    if not signature:
        return False
    expected = hmac.new(
        settings.RAZORPAY_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
