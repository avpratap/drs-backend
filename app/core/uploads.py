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
# Strip metadata containers without re-encoding: drop JPEG APPn (except JFIF/Adobe)/COM
# segments, PNG text/eXIf/time chunks, and WEBP EXIF/XMP chunks.
_JPEG_KEEP_APP = {0xE0, 0xEE}  # JFIF, Adobe (colour transform)
_PNG_DROP = {b"eXIf", b"tEXt", b"iTXt", b"zTXt", b"tIME"}


def _strip_jpeg(data: bytes) -> bytes:
    out = bytearray(data[:2])  # SOI
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return data  # not a marker where one must be: leave the file untouched
        marker = data[i + 1]
        if marker == 0xDA:  # SOS: entropy-coded data follows to EOI — copy the rest
            out += data[i:]
            return bytes(out)
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            out += data[i:i + 2]
            i += 2
            continue
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        end = i + 2 + seg_len
        if seg_len < 2 or end > len(data):
            return data
        drop = (0xE1 <= marker <= 0xEF and marker not in _JPEG_KEEP_APP) or marker == 0xFE
        if not drop:
            out += data[i:end]
        i = end
    return data


def _strip_png(data: bytes) -> bytes:
    out = bytearray(data[:8])
    i = 8
    while i + 12 <= len(data):
        length = int.from_bytes(data[i:i + 4], "big")
        ctype = data[i + 4:i + 8]
        end = i + 12 + length
        if end > len(data):
            return data
        if ctype not in _PNG_DROP:
            out += data[i:end]
        i = end
        if ctype == b"IEND":
            break
    return bytes(out)


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


def strip_metadata(data: bytes, ext: str) -> bytes:
    try:
        if ext == "jpg":
            return _strip_jpeg(data)
        if ext == "png":
            return _strip_png(data)
        if ext == "webp":
            return _strip_webp(data)
    except Exception:  # malformed container: keep the validated original bytes
        return data
    return data
