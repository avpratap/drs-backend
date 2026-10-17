"""Authenticated customer file upload → returns a URL.

Produces the URL that RMA damaged-item photos and support-ticket attachments reference
(the only other upload, /delivery/uploads, is delivery-staff-only). Same storage
helper, allow-list and size cap as the POD upload."""
import asyncio
import secrets

from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel

from app.core.storage import put_object
from app.core.uploads import precheck_upload_size, read_validated_image
from app.deps.auth import get_current_user
from app.models.user import User

router = APIRouter(prefix="/uploads", tags=["uploads"])


class UploadOut(BaseModel):
    url: str


@router.post("", operation_id="upload_file", response_model=UploadOut)
async def upload_file(
    request: Request, file: UploadFile = File(...), user: User = Depends(get_current_user)
):
    """Upload an image (JPEG/PNG/WEBP, ≤5 MB); returns a URL to attach to an RMA or ticket.
    The file is validated by magic bytes and stored with a server-chosen content-type."""
    precheck_upload_size(request)
    data, ext, ctype = await read_validated_image(file)
    key = f"uploads/{user.id}/{secrets.token_hex(8)}.{ext}"
    url = await asyncio.to_thread(put_object, key, data, ctype)
    return {"url": url}
