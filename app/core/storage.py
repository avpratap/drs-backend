"""Object storage abstraction.

Cloudflare R2 (S3-compatible) when configured — boto3 is imported lazily so it's an
optional dep and the API image stays lean. Otherwise a local dev-stub writes under
STORAGE_DIR and returns a /files/ URL, so uploads + PDF generation are fully testable
with no cloud credentials.
"""
import hashlib
import hmac
import logging
import os
import time
from urllib.parse import quote

from app.core.config import settings

PRIVATE_PREFIX = "private/"

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


def _r2_client():
    import boto3  # lazy: only needed when R2 is configured

    return boto3.client(
        "s3",
        endpoint_url=settings.R2_ENDPOINT,
        aws_access_key_id=settings.R2_ACCESS_KEY,
        aws_secret_access_key=settings.R2_SECRET_KEY,
    )


def _private_bucket() -> str:
    # A SEPARATE bucket with no public/CDN binding: keys in the public bucket are
    # readable by anyone who can guess them.
    return settings.R2_PRIVATE_BUCKET or settings.R2_BUCKET


def put_private_object(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Store a document that must NOT be publicly reachable (invoices: GSTIN, address,
    purchases). Returns the storage KEY, never a URL — hand out `private_url(key)`
    (short-lived, signed) only after an authorization check."""
    key = PRIVATE_PREFIX + key.lstrip("/")
    if enabled():
        _r2_client().put_object(
            Bucket=_private_bucket(), Key=key, Body=data, ContentType=content_type
        )
        return key
    root = os.path.abspath(settings.STORAGE_DIR)
    path = os.path.abspath(os.path.join(root, key))
    if os.path.commonpath([root, path]) != root:
        raise ValueError("invalid storage key")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    log.info("[storage:STUB] wrote private object (%d bytes)", len(data))
    return key


def _dev_sig(key: str, exp: int) -> str:
    return hmac.new(
        settings.JWT_SECRET.encode(), f"{key}|{exp}".encode(), hashlib.sha256
    ).hexdigest()


def private_url(key: str, ttl_seconds: int = 300) -> str:
    """Short-lived signed URL for a private object (presigned GET on R2; an HMAC-signed
    /files link in the dev stub)."""
    if enabled():
        return _r2_client().generate_presigned_url(
            "get_object",
            Params={"Bucket": _private_bucket(), "Key": key},
            ExpiresIn=ttl_seconds,
        )
    exp = int(time.time()) + ttl_seconds
    return f"/files/{quote(key)}?exp={exp}&sig={_dev_sig(key, exp)}"


def verify_dev_signature(key: str, exp: str | None, sig: str | None) -> bool:
    try:
        exp_i = int(exp or "")
    except ValueError:
        return False
    if exp_i < int(time.time()) or not sig:
        return False
    return hmac.compare_digest(_dev_sig(key, exp_i), sig)


def delete_private_keys(keys: list[str], prefixes: list[str] = ()) -> None:
    """Erase private objects (DPDP erasure): explicit keys plus everything under each
    prefix. Best-effort; blocking I/O — run it off the event loop."""
    keys = [k for k in keys if k.startswith(PRIVATE_PREFIX)]
    prefixes = [PRIVATE_PREFIX + p.lstrip("/") for p in prefixes]
    if enabled():
        client = _r2_client()
        bucket = _private_bucket()
        for prefix in prefixes:
            token = None
            while True:
                kw = {"Bucket": bucket, "Prefix": prefix}
                if token:
                    kw["ContinuationToken"] = token
                page = client.list_objects_v2(**kw)
                keys += [o["Key"] for o in page.get("Contents", [])]
                if not page.get("IsTruncated"):
                    break
                token = page.get("NextContinuationToken")
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            if batch:
                client.delete_objects(
                    Bucket=bucket, Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True}
                )
        return
    import shutil

    root = os.path.abspath(settings.STORAGE_DIR)
    for prefix in prefixes:
        path = os.path.abspath(os.path.join(root, prefix))
        if os.path.commonpath([root, path]) == root and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    for key in keys:
        path = os.path.abspath(os.path.join(root, key))
        if os.path.commonpath([root, path]) == root and os.path.isfile(path):
            os.remove(path)
