"""Authenticated customer file upload → returns a private media reference.

Produces the URL that RMA damaged-item photos and support-ticket attachments reference
(the only other upload, /delivery/uploads, is delivery-staff-only). Same storage
helper, allow-list and size cap as the POD upload."""
import asyncio
import secrets

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel

from app.core import media
from app.core.ratelimit_quota import upload_quota_ok
from app.core.storage import put_private_object
from app.core.uploads import precheck_upload_size, read_validated_image, strip_metadata
from app.deps.auth import get_current_user
from app.models.user import User

router = APIRouter(prefix="/uploads", tags=["uploads"])


class UploadOut(BaseModel):
    url: str  # the reference to attach (media:<key>) — store this, not preview_url
    preview_url: str  # short-lived signed URL to show the image right away


@router.post("", operation_id="upload_file", response_model=UploadOut)
async def upload_file(
    request: Request, file: UploadFile = File(...), user: User = Depends(get_current_user)
):
    """Upload an image (JPEG/PNG/WEBP, ≤5 MB); returns a URL to attach to an RMA or ticket.
    The file is validated by magic bytes and stored with a server-chosen content-type."""
    precheck_upload_size(request)
    if not await upload_quota_ok(user.id):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Upload limit reached; try tomorrow")
    data, ext, ctype = await read_validated_image(file)
    data = strip_metadata(data, ext)  # no GPS/device EXIF in stored photos
    key = await asyncio.to_thread(
        put_private_object, f"uploads/{user.id}/{secrets.token_hex(8)}.{ext}", data, ctype
    )
    ref = media.make_ref(key)
    return {"url": ref, "preview_url": media.resolve(ref)}
