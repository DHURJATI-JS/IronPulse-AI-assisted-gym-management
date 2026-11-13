"""Secure file storage.

* Every upload is size-limited and content-sniffed (never trust the filename/content-type).
* Images are decoded and re-encoded (strips EXIF/GPS metadata, defeats polyglot files) within a pixel budget checked
  on the header BEFORE any pixel is decoded (``decode_image``): at most ``MAX_IMAGE_PIXELS`` as decoded - a single-scan
  JPEG may say up to ``MAX_JPEG_PIXELS``, it is decoded at 1/2-1/8 scale - and at most ``IMAGE_DECODE_SLOTS`` decodes
  at once per process (``run_image_decode``), so parallel uploads queue instead of adding up their memory. (Pillow's
  own MAX_IMAGE_PIXELS only warns below twice its value: a 380 KB PNG of 100 megapixels used to take ~1 GB.)
* PDFs are rejected if they contain JavaScript / launch actions / embedded files / XFA forms - judged on the names as
  PDF readers decode them (``#xx`` escapes), in the file as uploaded AND as PDFium re-writes it (object streams
  unpacked, names canonical), plus PDFium's own count of document scripts and attachments (``_validate_pdf``).
* ``public/...`` keys (gym photos, staff/admin photos) are served by /media/public/...
* ``private/...`` keys (ID cards, certificates, payment proofs) are Fernet-encrypted at rest and are
  only ever returned through authorised routes via ``load_private``.
* Keys are random UUIDs, so user input never becomes a filesystem path.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
import threading
import uuid
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from fastapi import UploadFile

from app.config import get_settings
from app.security.crypto import decrypt_bytes, encrypt_bytes

KEY_RE = re.compile(r"^(public|private)/[a-z0-9_]{1,40}/[0-9a-f]{32}\.(jpg|png|webp|pdf|bin)\Z")
IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF", "MPO", "HEIF"}

# ---------------------------------------------------------------------- load-safety limits (README "Limits")
MAX_IMAGE_PIXELS = 25_000_000  # an image as decoded (~100 MB at Pillow's 4 bytes a pixel); checked on the header
MAX_JPEG_PIXELS = 64_000_000  # a single-scan JPEG's header (48-64 MP phone photos): decoded at 1/2-1/8 scale (draft)
IMAGE_DECODE_SLOTS = 2  # image decodes running at once per worker process; more uploads wait for a slot ...
IMAGE_DECODE_WAIT_SECONDS = 30  # ... at most this long, then "busy - please try again"
PDF_MAX_PAGES = 30

PDFIUM_LOCK = threading.Lock()  # PDFium is not thread-safe (pypdfium2 docs): every PDFium call of the process holds it

# PDF names (as readers decode them) that make a PDF active content: scripts, launching programs, embedded files,
# XFA forms, multimedia. Refused anywhere in the file - a name inside a string is a (rare) false refusal, never a miss.
PDF_DANGER_NAMES = frozenset({b"JavaScript", b"JS", b"Launch", b"XFA", b"FileAttachment", b"Rendition", b"GoToE",
                              b"ImportData", b"Sound", b"Movie"})
PDF_DANGER_PREFIXES = (b"EmbeddedFile", b"RichMedia")
_PDF_NAME = re.compile(rb"/([^\x00\t\n\x0c\r ()<>\[\]{}/%]*)")  # a name token: '/' + regular characters (PDF 7.3.5)
_PDF_HEX_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
PDF_REFUSED = "PDFs containing scripts, embedded files or launch actions are not accepted."
PDF_UNCHECKABLE = ("This PDF could not be checked for scripts - please upload an unprotected PDF, or a photo of the "
                   "document instead.")


class UploadError(ValueError):
    """Raised for invalid/unsafe uploads; the message is safe to show to the user."""


@dataclass(slots=True)
class StoredFile:
    key: str
    content_type: str
    size: int
    sha256: str
    original_filename: str
    width: int | None = None
    height: int | None = None


def _root() -> Path:
    return get_settings().upload_dir


def path_for(key: str) -> Path:
    if not KEY_RE.match(key or ""):
        raise UploadError("invalid storage key")
    return _root() / key


async def read_upload(upload: UploadFile | None, *, max_bytes: int | None = None, required: bool = True) -> bytes:
    if upload is None or not getattr(upload, "filename", None):
        if required:
            raise UploadError("Please choose a file to upload.")
        return b""
    limit = max_bytes or get_settings().max_upload_bytes
    buf = bytearray()
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > limit:
            raise UploadError(f"File is larger than {limit // (1024 * 1024)} MB.")
    if not buf:
        raise UploadError("The uploaded file is empty.")
    return bytes(buf)


# ---------------------------------------------------------------------- images
_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}  # start-of-frame markers (C4 DHT, C8 JPG, CC DAC are not)
_JPEG_SEQUENTIAL = frozenset({0xC0, 0xC1, 0xC9})  # baseline / extended sequential DCT: decodable at 1/2-1/8 scale
_RESIZE_FIRST = frozenset({"RGB", "L", "CMYK", "YCbCr"})  # no alpha, resampled well: shrink before any conversion
_EXIF_ORIENTATION = 0x0112


def _jpeg_single_scan(data: bytes) -> bool:
    """Is this a sequential DCT JPEG whose first image is ONE interleaved scan? Only such a file is decoded row by row
    at the reduced scale ``draft`` asks for. A progressive, lossless or multi-scan JPEG makes libjpeg keep the
    coefficients of the whole image at full size (up to 8 bytes a pixel), whatever the output scale."""
    if not data.startswith(b"\xff\xd8"):
        return False
    i, n, components, scans = 2, len(data), None, 0
    while i + 2 <= n:
        if data[i] != 0xFF:
            return False
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker == 0xD9:  # end of the first image (an MPO's other images follow)
            return scans == 1
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # markers without a length
            i += 2
            continue
        if i + 4 > n:
            return False
        length = (data[i + 2] << 8) | data[i + 3]
        if length < 2:
            return False
        if marker in _JPEG_SOF:
            if marker not in _JPEG_SEQUENTIAL or components is not None or i + 9 >= n:
                return False  # progressive / lossless / hierarchical frame
            components = data[i + 9]
        elif marker == 0xDA:  # start of scan
            scans += 1
            if scans > 1 or components is None or i + 4 >= n or data[i + 4] != components:
                return False  # a second scan, or a scan that does not interleave every component
            j = i + 2 + length  # skip the entropy-coded data: up to a marker that is no stuffed 0xFF00 or restart
            while True:
                j = data.find(b"\xff", j)
                if j < 0 or j + 1 >= n:
                    return True  # ends inside the one scan (Pillow refuses a truncated file anyway)
                if data[j + 1] == 0x00 or 0xD0 <= data[j + 1] <= 0xD7 or data[j + 1] == 0xFF:
                    j += 1
                    continue
                break
            i = j
            continue
        i += 2 + length
    return False


def _too_large(width: int, height: int, limit: int) -> UploadError:
    return UploadError(f"That image is too large ({width:,} × {height:,} pixels). Please upload one of at most "
                       f"{limit // 1_000_000} megapixels.")


def decode_image(data: bytes, max_side: int, *, flatten_on: str | None = None) -> Any:
    """Decode an uploaded image safely and return it as a small Pillow image: a supported format; at most
    ``MAX_IMAGE_PIXELS`` as decoded, checked on the header before any pixel is decoded (a single-scan JPEG may say up to
    ``MAX_JPEG_PIXELS``: it is decoded at 1/2-1/8 scale); EXIF orientation applied; at most ``max_side`` on its longer
    side; mode RGB or L. Transparency is dropped as before, or composited onto ``flatten_on`` (a colour name) when
    given. Blocking - run it through ``run_image_decode``. Raises ``UploadError`` with a user-safe message."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as probe:  # Image.open reads the header only
            if probe.format not in IMAGE_FORMATS:
                raise UploadError("Unsupported image format. Use JPG, PNG or WEBP.")
            probe.verify()
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
            jpeg = im.format in ("JPEG", "MPO")
            limit = MAX_JPEG_PIXELS if jpeg and _jpeg_single_scan(data) else MAX_IMAGE_PIXELS
            if width * height > limit:
                raise _too_large(width, height, limit)
            if jpeg:
                im.draft("RGB", (max_side, max_side))  # libjpeg scales while decoding (1/2, 1/4 or 1/8)
                if im.size[0] * im.size[1] > MAX_IMAGE_PIXELS:  # a strip too thin to shrink much
                    raise _too_large(width, height, MAX_IMAGE_PIXELS)
            im.load()  # the only full decode, of a bounded size
            orientation = im.getexif().get(_EXIF_ORIENTATION, 1)
            out = im
            if out.mode not in _RESIZE_FIRST:
                if flatten_on and (out.mode in ("RGBA", "LA", "PA") or "transparency" in out.info):
                    rgba = out.convert("RGBA")
                    out = Image.new("RGB", rgba.size, flatten_on)
                    out.paste(rgba, mask=rgba.getchannel("A"))
                    del rgba
                else:
                    out = out.convert("RGB")
            out.thumbnail((max_side, max_side))
            if out.mode not in ("RGB", "L"):
                out = out.convert("RGB")
            method = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
                      4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270,
                      7: Image.Transpose.TRANSVERSE, 8: Image.Transpose.ROTATE_90}.get(orientation)
            if method is not None:
                out = out.transpose(method)
            return out.copy() if out is im else out  # never the opened file's image: closing it frees its pixels
    except UploadError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:  # Pillow's own guard, at open time
        raise UploadError(f"That image is too large. Please upload one of at most {MAX_IMAGE_PIXELS // 1_000_000} "
                          "megapixels.") from exc
    except Exception as exc:
        raise UploadError("That file is not a valid image.") from exc


def _reencode_image(data: bytes, max_side: int, fmt: str = "JPEG", quality: int = 88) -> tuple[bytes, int, int]:
    im = decode_image(data, max_side)
    out = io.BytesIO()
    im.save(out, fmt, quality=quality, optimize=True)
    return out.getvalue(), im.width, im.height


_T = TypeVar("_T")
_decode_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()


async def run_image_decode(fn: Callable[..., _T], *args: Any) -> _T:
    """``await asyncio.to_thread(fn, *args)`` for blocking image work, at most ``IMAGE_DECODE_SLOTS`` at once in this
    process: parallel uploads queue instead of adding up their memory. A slot is held until the thread has finished -
    also when the request was cancelled meanwhile. No slot within ``IMAGE_DECODE_WAIT_SECONDS``: a friendly
    ``UploadError``."""
    loop = asyncio.get_running_loop()
    slots = _decode_slots.get(loop)
    if slots is None:
        slots = _decode_slots[loop] = asyncio.Semaphore(IMAGE_DECODE_SLOTS)
    try:
        async with asyncio.timeout(IMAGE_DECODE_WAIT_SECONDS):
            await slots.acquire()
    except TimeoutError:
        raise UploadError("The server is busy processing other photos - please try again in a minute.") from None

    def _done(task: asyncio.Task[_T]) -> None:
        slots.release()
        if not task.cancelled():
            task.exception()  # retrieved: a request that went away meanwhile leaves no 'never retrieved' warning

    work = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    work.add_done_callback(_done)
    return await asyncio.shield(work)


# ---------------------------------------------------------------------- PDFs
def _hex_digit(c: int) -> int:
    """PDFium's FXSYS_HexCharToInt: a non-hex character counts as 0."""
    if 0x30 <= c <= 0x39:
        return c - 0x30
    if 0x41 <= c <= 0x46 or 0x61 <= c <= 0x66:
        return (c | 0x20) - 0x61 + 10
    return 0


def _pdf_name_forms(raw: bytes) -> tuple[bytes, ...]:
    """A name token as PDF readers decode it: ``#xx`` escapes as the specification says (``/J#61vaScript`` is
    /JavaScript), and as PDFium does - '#' and ANY two characters, a non-hex digit counting as 0 (``/JavaScri#7Gt``)."""
    if b"#" not in raw:
        return (raw,)
    strict = _PDF_HEX_ESCAPE.sub(lambda m: bytes((int(m.group(1), 16),)), raw)
    lenient = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == 0x23 and i + 2 < len(raw):
            lenient.append((_hex_digit(raw[i + 1]) * 16 + _hex_digit(raw[i + 2])) & 0xFF)
            i += 3
        else:
            lenient.append(raw[i])
            i += 1
    return (strict, bytes(lenient))


def _pdf_has_danger(data: bytes) -> bool:
    for match in _PDF_NAME.finditer(data):
        for name in _pdf_name_forms(match.group(1)):
            if name in PDF_DANGER_NAMES or name.startswith(PDF_DANGER_PREFIXES):
                return True
    return False


def _validate_pdf(data: bytes) -> None:
    """Refuse a PDF with active content. Three looks, any one refuses: (1) every name token of the file as uploaded,
    decoded as readers decode it; (2) PDFium's own count of document-level scripts and attachments, and its form type
    (XFA); (3) the same name scan over the file as PDFium re-writes it - objects packed into compressed object streams
    come out as plain objects there, with canonical names. Blocking (call via asyncio.to_thread)."""
    if not data.startswith(b"%PDF-"):
        raise UploadError("That file is not a valid PDF.")
    if _pdf_has_danger(data):
        raise UploadError(PDF_REFUSED)
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    with PDFIUM_LOCK:
        try:
            doc = pdfium.PdfDocument(data)
        except Exception as exc:
            raise UploadError("The PDF could not be opened (corrupted or password protected).") from exc
        try:
            pages = len(doc)
            if pages == 0 or pages > PDF_MAX_PAGES:
                raise UploadError(f"PDFs must have between 1 and {PDF_MAX_PAGES} pages.")
            if (pdfium_c.FPDFDoc_GetJavaScriptActionCount(doc.raw) > 0
                    or pdfium_c.FPDFDoc_GetAttachmentCount(doc.raw) > 0
                    or pdfium_c.FPDF_GetFormType(doc.raw) in (pdfium_c.FORMTYPE_XFA_FULL,
                                                              pdfium_c.FORMTYPE_XFA_FOREGROUND)):
                raise UploadError(PDF_REFUSED)
            rewritten = io.BytesIO()
            try:
                doc.save(rewritten, flags=pdfium_c.FPDF_REMOVE_SECURITY)  # a full save; strings/streams decrypted
            except Exception as exc:
                raise UploadError(PDF_UNCHECKABLE) from exc
        finally:
            doc.close()
    if _pdf_has_danger(rewritten.getvalue()):
        raise UploadError(PDF_REFUSED)


def pdf_render_scale(width_pt: float, height_pt: float, wanted: float) -> float:
    """The scale to render a ``width_pt`` x ``height_pt`` PDF page at (OCR, previews): ``wanted``, lowered so the bitmap
    stays within ``MAX_IMAGE_PIXELS`` - a page may measure 200 x 200 inches, a 3 GB bitmap at scale 2."""
    area = max(float(width_pt), 1.0) * max(float(height_pt), 1.0)
    return max(0.01, min(float(wanted), (MAX_IMAGE_PIXELS / area) ** 0.5))


def sniff_kind(data: bytes) -> str:
    if data.startswith(b"%PDF-"):
        return "pdf"
    return "image"


async def save_public_image(upload: UploadFile | bytes, *, kind: str, max_side: int = 1600) -> StoredFile:
    data = upload if isinstance(upload, bytes) else await read_upload(upload)
    name = "upload" if isinstance(upload, bytes) else (upload.filename or "upload")
    encoded, w, h = await run_image_decode(_reencode_image, data, max_side)
    key = f"public/{_kind(kind)}/{uuid.uuid4().hex}.jpg"
    await _write(key, encoded)
    return StoredFile(key, "image/jpeg", len(encoded), hashlib.sha256(encoded).hexdigest(), name[:255], w, h)


async def save_private_file(
    upload: UploadFile | bytes, *, kind: str, allow_pdf: bool = True, max_side: int = 2400, filename: str | None = None
) -> tuple[StoredFile, bytes]:
    """Validate, normalise and encrypt a private document. Returns (stored file, plaintext bytes) -
    the plaintext is handy for OCR right after upload and is never written to disk unencrypted."""
    data = upload if isinstance(upload, bytes) else await read_upload(upload)
    name = filename or ("upload" if isinstance(upload, bytes) else (upload.filename or "upload"))
    if sniff_kind(data) == "pdf":
        if not allow_pdf:
            raise UploadError("Please upload an image (JPG/PNG), not a PDF.")
        await asyncio.to_thread(_validate_pdf, data)
        plain, ctype, w, h = data, "application/pdf", None, None
    else:
        plain, w, h = await run_image_decode(_reencode_image, data, max_side, "JPEG", 92)
        ctype = "image/jpeg"
    key = f"private/{_kind(kind)}/{uuid.uuid4().hex}.bin"
    await _write(key, await asyncio.to_thread(encrypt_bytes, plain))
    return StoredFile(key, ctype, len(plain), hashlib.sha256(plain).hexdigest(), name[:255], w, h), plain


async def load_private(key: str) -> bytes:
    if not key.startswith("private/"):
        raise UploadError("not a private key")
    raw = await asyncio.to_thread(path_for(key).read_bytes)
    return await asyncio.to_thread(decrypt_bytes, raw)


async def delete_key(key: str | None) -> None:
    if not key:
        return
    try:
        p = path_for(key)
    except UploadError:
        return
    await asyncio.to_thread(lambda: p.unlink(missing_ok=True))


def media_url(key: str | None) -> str | None:
    if not key or not key.startswith("public/"):
        return None
    return f"/media/{key}"


def _kind(kind: str) -> str:
    k = re.sub(r"[^a-z0-9_]", "_", kind.lower())[:40]
    return k or "misc"


async def _write(key: str, data: bytes) -> None:
    p = path_for(key)

    def _w() -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)

    await asyncio.to_thread(_w)
