"""Canonicalize Indian phone numbers so one person maps to one account.

+91 98765 43210, 0091-9876543210, 09876543210 and 9876543210 must all resolve to
the same stored identity. We strip all non-digits, then a recognised Indian prefix
(91 / 091 / 0091 / 0). Anything else is returned digits-only and fails
is_indian_mobile upstream (a foreign number is never truncated onto an Indian one).
"""
import re

_INDIAN_MOBILE = re.compile(r"^[6-9]\d{9}$")


def normalize_phone(raw: str) -> str:
    """Only a known Indian prefix (91 / 091 / 0091 / 0) is stripped. Any other longer
    number (e.g. a UK 44... mobile) is returned as-is and fails is_indian_mobile — it
    used to be truncated to its last 10 digits and mapped onto someone else's account."""
    digits = re.sub(r"\D", "", raw or "")
    for prefix in ("0091", "091", "91", "0"):
        if len(digits) == 10 + len(prefix) and digits.startswith(prefix):
            return digits[len(prefix):]
    return digits


def is_indian_mobile(phone: str) -> bool:
    """A normalized number is a valid Indian mobile: 10 digits starting 6-9."""
    return bool(_INDIAN_MOBILE.fullmatch(phone or ""))
