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


# ---------- metadata stripping ----------
# Phone photos carry EXIF (GPS coordinates of the customer's doorstep, device serials).
# Images are re-encoded (see _reencode); an animated WEBP is cleaned at container level
# (EXIF/XMP chunks dropped) so its frames survive.


def _strip_webp(data: bytes) -> bytes:
    out = bytearray()
    i = 12
    while i + 8 <= len(data):
        fourcc = data[i:i + 4]
        size = int.from_bytes(data[i + 4:i + 8], "little")
        end = i + 8 + size + (size & 1)
        if end > len(data) + 1:
            return data
        chunk = bytearray(data[i:min(end, len(data))])
        if fourcc == b"VP8X" and len(chunk) >= 9:
            chunk[8] &= ~0x0C & 0xFF  # clear the EXIF (0x08) and XMP (0x04) flags
        if fourcc not in (b"EXIF", b"XMP "):
            out += chunk
        i = end
    return b"RIFF" + (len(out) + 4).to_bytes(4, "little") + b"WEBP" + bytes(out)


def _reencode(data: bytes, ext: str) -> bytes:
    """Decode and re-encode with Pillow: the pixels are rotated to the EXIF Orientation
    (so stripping it doesn't leave portrait photos sideways), the ICC colour profile is
    kept, and EVERY other metadata block (EXIF/GPS, XMP, comments) is dropped."""
    import io as _io

    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 40_000_000  # decompression-bomb guard (~40 MP)
    with Image.open(_io.BytesIO(data)) as img:
        if getattr(img, "is_animated", False):
            raise ValueError("animated")
        icc = img.info.get("icc_profile")
        img = ImageOps.exif_transpose(img)
        out = _io.BytesIO()
        kwargs = {"icc_profile": icc} if icc else {}
        if ext == "jpg":
            if img.mode not in ("RGB", "L", "CMYK"):
                img = img.convert("RGB")
            img.save(out, "JPEG", quality=90, optimize=True, **kwargs)
        elif ext == "png":
            img.save(out, "PNG", optimize=True, **kwargs)
        else:
            img.save(out, "WEBP", quality=90, **kwargs)
        return out.getvalue()


def strip_metadata(data: bytes, ext: str) -> bytes:
    """Remove location/device metadata before storage. Fails CLOSED: an image that
    can't be safely cleaned is rejected (422) rather than stored with GPS intact."""
    try:
        return _reencode(data, ext)
    except Exception as exc:
        if ext == "webp" and str(exc) == "animated":
            return _strip_webp(data)  # container-level strip keeps every frame
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Image could not be processed"
        ) from None
