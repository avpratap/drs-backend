"""Object storage abstraction.

Cloudflare R2 (S3-compatible) when configured — boto3 is imported lazily so it's an
optional dep and the API image stays lean. Otherwise a local dev-stub writes under
STORAGE_DIR and returns a /files/ URL, so uploads + PDF generation are fully testable
with no cloud credentials.
"""
import logging
import os

from app.core.config import settings

log = logging.getLogger("drs.storage")


def enabled() -> bool:
    return bool(settings.R2_ENDPOINT and settings.R2_ACCESS_KEY and settings.R2_BUCKET)


def put_object(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Store bytes and return a retrievable URL."""
    if enabled():
        import boto3  # lazy: only needed when R2 is configured

        client = boto3.client(
            "s3",
            endpoint_url=settings.R2_ENDPOINT,
            aws_access_key_id=settings.R2_ACCESS_KEY,
            aws_secret_access_key=settings.R2_SECRET_KEY,
        )
        client.put_object(
            Bucket=settings.R2_BUCKET, Key=key, Body=data, ContentType=content_type
        )
        base = settings.R2_PUBLIC_BASE or f"{settings.R2_ENDPOINT}/{settings.R2_BUCKET}"
        return f"{base.rstrip('/')}/{key}"

    # Dev stub: persist locally so the object is actually retrievable in dev.
    root = os.path.abspath(settings.STORAGE_DIR)
    path = os.path.abspath(os.path.join(root, key))
    # Defense-in-depth: a key containing '..' or an absolute path must not escape the
    # storage root (path traversal → arbitrary file write).
    if os.path.commonpath([root, path]) != root:
        raise ValueError("invalid storage key")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    log.info("[storage:STUB] wrote %s (%d bytes)", key, len(data))
    return f"/files/{key}"
