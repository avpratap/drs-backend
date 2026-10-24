"""Account-email ownership verification (signed, expiring, single-email tokens).

The token binds (user id, the exact email, expiry) with an HMAC, so it can't be reused
for a different address or after the user changes their email again. Stateless: the
only state is users.email_verified_at.
"""
import base64
import datetime as dt
import hashlib
import hmac

from app.core.config import settings

TTL = dt.timedelta(hours=24)


def _sig(msg: str) -> str:
    return hmac.new(settings.JWT_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()


def make_token(user_id: int, email: str, now: dt.datetime | None = None) -> str:
    exp = int(((now or dt.datetime.now(dt.UTC)) + TTL).timestamp())
    msg = f"{user_id}|{email.lower()}|{exp}"
    return base64.urlsafe_b64encode(f"{msg}|{_sig(msg)}".encode()).decode()


def check_token(token: str, user_id: int, email: str | None) -> bool:
    try:
        # The email may itself contain "|": split the id off the left and exp|sig off
        # the right, so whatever is in between is the email.
        uid, rest = base64.urlsafe_b64decode(token.encode()).decode().split("|", 1)
        em, exp, sig = rest.rsplit("|", 2)
    except Exception:
        return False
    msg = f"{uid}|{em}|{exp}"
    if not hmac.compare_digest(_sig(msg), sig):
        return False
    if int(uid) != user_id or not email or em != email.lower():
        return False
    return int(exp) >= int(dt.datetime.now(dt.UTC).timestamp())
