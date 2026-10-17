"""Authenticated customer file upload → returns a URL.

Produces the URL that RMA damaged-item photos and support-ticket attachments reference
(the only other upload, /delivery/uploads, is delivery-staff-only). Same storage
helper, allow-list and size cap as the POD upload."""
import secrets

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.core.storage import put_object
from app.deps.auth import get_current_user
from app.models.user import User

router = APIRouter(prefix="/uploads", tags=["uploads"])

_MAX_UPLOAD = 5 * 1024 * 1024  # 5 MB
_ALLOWED_UPLOAD = {"image/jpeg", "image/png", "image/webp"}
_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


class UploadOut(BaseModel):
    url: str


@router.post("", operation_id="upload_file", response_model=UploadOut)
async def upload_file(
    file: UploadFile = File(...), user: User = Depends(get_current_user)
):
    """Upload an image (JPEG/PNG/WEBP, ≤5 MB); returns a URL to attach to an RMA or ticket."""
    if file.content_type not in _ALLOWED_UPLOAD:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Only JPEG/PNG/WEBP allowed")
    data = await file.read()
    if len(data) > _MAX_UPLOAD:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File too large (max 5 MB)")
    key = f"uploads/{user.id}/{secrets.token_hex(8)}.{_EXT[file.content_type]}"
    url = put_object(key, data, file.content_type)
    return {"url": url}
