"""Shiprocket adapter.

No SHIPROCKET_EMAIL/PASSWORD -> dev stub: returns deterministic fake AWBs and a
serviceable=True quote so the whole shipping flow is testable now; the moment creds
are set the same calls hit Shiprocket's API. The auth token (10-day validity) is
cached in Redis. httpx is imported lazily and every call fails soft.
"""
import logging

from app.core.config import settings
from app.core.redis import redis_client

log = logging.getLogger("drs.shipping")

_TOKEN_KEY = "shiprocket:token"
_TOKEN_TTL = 60 * 60 * 24 * 9  # refresh before the ~10-day expiry


def enabled() -> bool:
    return bool(settings.SHIPROCKET_EMAIL and settings.SHIPROCKET_PASSWORD)


def _as_float(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_int(v) -> int | None:
    """Best-effort int from a number or a string like '3' or '3-5 Days' (leading int)."""
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        import re

        m = re.match(r"\s*(\d+)", str(v))
        return int(m.group(1)) if m else None


class ShippingError(Exception):
    pass


async def _token() -> str:
    cached = await redis_client.get(_TOKEN_KEY)
    if cached:
        return cached
    import httpx

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"{settings.SHIPROCKET_BASE_URL}/auth/login",
            json={"email": settings.SHIPROCKET_EMAIL, "password": settings.SHIPROCKET_PASSWORD},
        )
    r.raise_for_status()
    token = r.json()["token"]
    await redis_client.set(_TOKEN_KEY, token, ex=_TOKEN_TTL)
    return token


async def _headers() -> dict:
    return {"Authorization": f"Bearer {await _token()}", "Content-Type": "application/json"}


async def check_serviceability(
    delivery_pincode: str, weight_kg: float, cod: bool
) -> dict:
    """Return {serviceable, etd_days, courier, rate}. Dev-stub: always serviceable."""
    if not enabled():
        return {"serviceable": True, "etd_days": 3, "courier": "DEV-Courier", "rate": 0.0}
    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{settings.SHIPROCKET_BASE_URL}/courier/serviceability/",
                headers=await _headers(),
                params={
                    "pickup_postcode": settings.PICKUP_PINCODE,
                    "delivery_postcode": delivery_pincode,
                    "weight": weight_kg,
                    "cod": 1 if cod else 0,
                },
            )
        data = r.json().get("data", {})
        couriers = data.get("available_courier_companies", [])
        if not couriers:
            return {"serviceable": False, "etd_days": None, "courier": None, "rate": None}
        # Shiprocket returns rate/etd as numbers OR strings ("45.5", "3-5 Days"); coerce
        # defensively so a string field can't crash min() or the response serializer.
        best = min(couriers, key=lambda x: _as_float(x.get("rate"), 1e9))
        return {
            "serviceable": True,
            "etd_days": _as_int(best.get("estimated_delivery_days")),
            "courier": best.get("courier_name"),
            "rate": _as_float(best.get("rate"), 0.0),
        }
    except Exception as exc:
        log.warning("serviceability check failed: %s", exc)
        return {"serviceable": False, "etd_days": None, "courier": None, "rate": None}


async def create_shipment(payload: dict) -> dict:
    """Create a Shiprocket order + assign an AWB.

    Returns {gateway_order_id, gateway_shipment_id, awb, courier, label_url, tracking_url}.
    Dev-stub synthesizes a deterministic AWB from the local shipment id.
    """
    local_id = payload["shipment_id"]
    if not enabled():
        awb = f"DEVAWB{int(local_id):08d}"
        return {
            "gateway_order_id": f"DEVORD{int(local_id):08d}",
            "gateway_shipment_id": f"DEVSHP{int(local_id):08d}",
            "awb": awb,
            "courier": "DEV-Courier",
            "label_url": None,
            "tracking_url": f"https://shiprocket.co/tracking/{awb}",
        }
    import httpx

    async with httpx.AsyncClient(timeout=20) as c:
        headers = await _headers()
        order = await c.post(
            f"{settings.SHIPROCKET_BASE_URL}/orders/create/adhoc",
            headers=headers,
            json=payload["order"],
        )
        order.raise_for_status()
        oj = order.json()
        gsid = oj.get("shipment_id")
        awb_resp = await c.post(
            f"{settings.SHIPROCKET_BASE_URL}/courier/assign/awb",
            headers=headers,
            json={"shipment_id": gsid},
        )
        awb_resp.raise_for_status()
        ad = awb_resp.json().get("response", {}).get("data", {})
    return {
        "gateway_order_id": str(oj.get("order_id")),
        "gateway_shipment_id": str(gsid),
        "awb": ad.get("awb_code"),
        "courier": ad.get("courier_name"),
        "label_url": ad.get("label_url"),
        "tracking_url": ad.get("tracking_url"),
    }


async def cancel_shipment(gateway_shipment_id: str) -> bool:
    if not enabled():
        return True
    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{settings.SHIPROCKET_BASE_URL}/orders/cancel",
                headers=await _headers(),
                json={"ids": [gateway_shipment_id]},
            )
        return r.status_code < 300
    except Exception as exc:
        log.warning("cancel failed: %s", exc)
        return False
