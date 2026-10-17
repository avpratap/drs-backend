"""Shared image-upload validation: reject by Content-Length before reading, verify the
bytes are actually a JPEG/PNG/WEBP by magic number (not the client-declared type), and
return a server-chosen canonical content-type so a spoofed header can't be stored."""
from fastapi import HTTPException, Request, UploadFile, status

MAX_UPLOAD = 5 * 1024 * 1024  # 5 MB

# (magic-byte prefix, extension, canonical content-type)
_SIGS = (
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
)


def precheck_upload_size(request: Request) -> None:
    """Reject an over-large upload by its Content-Length header BEFORE buffering it."""
    cl = request.headers.get("content-length")
    if cl is None:
        return
    try:
        declared = int(cl)
    except ValueError:
        return
    # Allow a little multipart framing overhead over the raw file cap.
    if declared > MAX_UPLOAD + 8192:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, "File too large (max 5 MB)"
        )


async def read_validated_image(file: UploadFile) -> tuple[bytes, str, str]:
    """Read the upload (capped), verify it is really an image by magic bytes, and return
    (data, extension, canonical_content_type). Raises 413/422 on violation."""
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, "File too large (max 5 MB)"
        )
    for sig, ext, ctype in _SIGS:
        if data.startswith(sig):
            return data, ext, ctype
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return data, "webp", "image/webp"
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY, "Only real JPEG/PNG/WEBP images are allowed"
    )
