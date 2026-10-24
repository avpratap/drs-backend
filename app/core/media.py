"""Private media references for user-uploaded images (POD photos/signatures, support
attachments). These carry personal data (faces, doorsteps, GPS) so they are stored in
the PRIVATE bucket and referenced as "media:<key>" — never a public URL. Responses turn
a reference into a short-lived signed URL; requests may only attach their own uploads."""
from app.core.storage import PRIVATE_PREFIX, private_url

REF_PREFIX = "media:"
VIEW_TTL_SECONDS = 3600


def make_ref(key: str) -> str:
    return REF_PREFIX + key


def is_ref(value: str | None) -> bool:
    return bool(value) and value.startswith(REF_PREFIX)


def ref_owned(value: str, area: str, user_id: int) -> bool:
    """The reference points at `user_id`'s own upload in `area` (e.g. uploads/, pod/)."""
    key = value[len(REF_PREFIX):]
    return key.startswith(f"{PRIVATE_PREFIX}{area}/{user_id}/") and ".." not in key


def resolve(value: str | None) -> str | None:
    """A displayable URL: signed short-lived for a private reference, else unchanged."""
    if not is_ref(value):
        return value
    return private_url(value[len(REF_PREFIX):], ttl_seconds=VIEW_TTL_SECONDS)
