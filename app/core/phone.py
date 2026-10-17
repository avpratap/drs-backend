"""Canonicalize Indian phone numbers so one person maps to one account.

+91 98765 43210, 0091-9876543210, 09876543210 and 9876543210 must all resolve to
the same stored identity. Indian mobile numbers are 10 digits, so we strip all
non-digits and keep the last 10. Anything shorter is returned digits-only and will
fail schema validation upstream.
"""
import re

_INDIAN_MOBILE = re.compile(r"^[6-9]\d{9}$")


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else digits


def is_indian_mobile(phone: str) -> bool:
    """A normalized number is a valid Indian mobile: 10 digits starting 6-9."""
    return bool(_INDIAN_MOBILE.fullmatch(phone or ""))
