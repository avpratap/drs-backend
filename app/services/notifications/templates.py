"""Event -> template registry.

Each event maps to:
  - category: 'transactional' (always sent on an enabled channel) or 'marketing'
    (consent-gated + quiet-hours-deferred).
  - channels: ordered preference; the router creates one row per channel.
  - render(data): returns {"text": <sms/push/whatsapp body>, "wa_template": name,
    "wa_params": [..]}. wa_template/params are used for Gupshup template sends;
    text is the fallback / log body / sms body.

Keep provider template *names* (wa_template) in sync with what's approved in the
Gupshup/WABA console. Until approved, dev-stub just logs `text`.
"""
from typing import Callable

TRANSACTIONAL = "transactional"
MARKETING = "marketing"


def _money(v) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _r_order_generated(d: dict) -> dict:
    txt = (
        f"DRS: Your order for {d.get('date')} is confirmed — "
        f"{d.get('summary', 'items')}. Total ₹{_money(d.get('total', 0))}. "
        f"Changes before 11 PM."
    )
    return {"text": txt, "wa_template": "order_generated",
            "wa_params": [str(d.get("date")), str(d.get("summary", "")), _money(d.get("total", 0))]}


def _r_delivery_completed(d: dict) -> dict:
    txt = f"DRS: Delivered ✅ {d.get('summary', '')}. Amount ₹{_money(d.get('amount', 0))}. Thank you!"
    return {"text": txt, "wa_template": "delivery_completed",
            "wa_params": [str(d.get("summary", "")), _money(d.get("amount", 0))]}


def _r_payment_success(d: dict) -> dict:
    txt = (
        f"DRS: Payment received ₹{_money(d.get('amount', 0))}. "
        f"Wallet balance ₹{_money(d.get('balance', 0))}."
    )
    return {"text": txt, "wa_template": "payment_success",
            "wa_params": [_money(d.get("amount", 0)), _money(d.get("balance", 0))]}


def _r_payment_failed(d: dict) -> dict:
    txt = f"DRS: Payment of ₹{_money(d.get('amount', 0))} failed. Please retry to avoid service interruption."
    return {"text": txt, "wa_template": "payment_failed", "wa_params": [_money(d.get("amount", 0))]}


def _r_wallet_low(d: dict) -> dict:
    txt = (
        f"DRS: Low wallet balance ₹{_money(d.get('balance', 0))}. "
        f"Top up to keep daily deliveries running."
    )
    return {"text": txt, "wa_template": "wallet_low", "wa_params": [_money(d.get("balance", 0))]}


def _r_winback(d: dict) -> dict:
    txt = f"DRS: We miss you! {d.get('offer', 'Enjoy a special offer')} on your next order. Reply to reorder."
    return {"text": txt, "wa_template": "winback", "wa_params": [str(d.get("offer", ""))]}


def _r_promo(d: dict) -> dict:
    txt = f"DRS: {d.get('message', 'New offer available!')}"
    return {"text": txt, "wa_template": "promo", "wa_params": [str(d.get("message", ""))]}


# event_type -> (category, channels, renderer)
REGISTRY: dict[str, tuple[str, list[str], Callable[[dict], dict]]] = {
    "order.generated": (TRANSACTIONAL, ["whatsapp", "sms"], _r_order_generated),
    "delivery.completed": (TRANSACTIONAL, ["whatsapp", "sms"], _r_delivery_completed),
    "payment.success": (TRANSACTIONAL, ["whatsapp"], _r_payment_success),
    "payment.failed": (TRANSACTIONAL, ["whatsapp", "sms"], _r_payment_failed),
    "wallet.low": (TRANSACTIONAL, ["whatsapp", "sms"], _r_wallet_low),
    "marketing.winback": (MARKETING, ["whatsapp"], _r_winback),
    "marketing.promo": (MARKETING, ["whatsapp", "sms"], _r_promo),
}


def get_spec(event_type: str):
    return REGISTRY.get(event_type)
