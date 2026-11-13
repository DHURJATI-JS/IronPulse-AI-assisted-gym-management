"""Platform findings of the bug hunt (app/services/storage.py, app/core/errors.py, app/main.py) - one regression test
per finding, each failing on the code before the fix:

F21 / F40 / F48  an image "decompression bomb" was decoded in full: Pillow's MAX_IMAGE_PIXELS only WARNS below twice
    its value, so a 380 KB PNG of 100 megapixels was accepted - as any member's avatar too - and took ~1 GB of the
    worker's memory, and parallel uploads added up. Now the pixel count is checked on the header before any pixel is
    decoded, a single-scan JPEG is decoded at reduced scale, and a process decodes at most IMAGE_DECODE_SLOTS images
    at once (the next ones wait, then hear "busy").
F41  the "no JavaScript in PDFs" check was a byte regex: /J#61vaScript (= /JavaScript), /J#53 (= /JS) and names inside
    compressed object streams passed, and the PDF was served inline.
F24  after a failed flush (a UniqueViolation) the error page of a signed-in user crashed on the expired, detached user
    and the browser got raw JSON.
F43  responses answered outside Talisman - the Host check's 400, the HTTP->HTTPS redirect, the 500 page - carried no
    security headers.
"""

from __future__ import annotations

import asyncio
import html
import io
import struct
import threading
import time
import zlib
from typing import Any

import httpx
import pytest
from fastapi import Request

from app.config import get_settings
from app.core import errors
from app.core.deps import DB
from app.main import app as fastapi_app
from app.services import storage
from tests.conftest import login_as, post_form
from tests.factories import make_customer, png_bytes


# ============================================================================ helpers
def png_bomb(width: int, height: int, color_type: int = 6) -> bytes:
    """A one-colour PNG of width x height pixels, compressed row by row (never held in memory): a few hundred KB."""
    channels = {0: 1, 2: 3, 3: 1, 6: 4}[color_type]

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    deflate = zlib.compressobj(9)
    row = b"\x00" * (1 + width * channels)
    idat = b"".join(deflate.compress(row) for _ in range(height)) + deflate.flush()
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0))
    if color_type == 3:
        out += chunk(b"PLTE", b"\x00\x00\x00\xff\xff\xff")
    return out + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def classic_pdf(objects: list[bytes]) -> bytes:
    """A PDF with a classic cross-reference table; object 1 is the catalog."""
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def object_stream_pdf(objects: list[bytes]) -> bytes:
    """A PDF 1.5 whose objects all live in ONE Flate-compressed object stream (an xref stream points into it): none of
    their names appears in the file's bytes."""
    n = len(objects)
    body, offsets = b"", []
    for obj in objects:
        offsets.append(len(body))
        body += obj + b"\n"
    header = b" ".join(f"{i} {off}".encode() for i, off in enumerate(offsets, 1)) + b"\n"
    packed = zlib.compress(header + body)
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    stream_at = len(out)
    out += (f"{n + 1} 0 obj\n<< /Type /ObjStm /N {n} /First {len(header)} /Filter /FlateDecode /Length {len(packed)} >>"
            "\nstream\n").encode() + packed + b"\nendstream\nendobj\n"
    xref_at = len(out)
    rows = [struct.pack(">BIH", 0, 0, 65535)] + [struct.pack(">BIH", 2, n + 1, i) for i in range(n)]
    rows += [struct.pack(">BIH", 1, stream_at, 0), struct.pack(">BIH", 1, xref_at, 0)]
    xdata = zlib.compress(b"".join(rows))
    out += (f"{n + 2} 0 obj\n<< /Type /XRef /Size {n + 3} /W [1 4 2] /Root 1 0 R /Filter /FlateDecode "
            f"/Length {len(xdata)} >>\nstream\n").encode() + xdata + b"\nendstream\nendobj\n"
    return bytes(out + f"startxref\n{xref_at}\n%%EOF\n".encode())


PAGES = [b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>"]


def html_client() -> httpx.AsyncClient:
    """A browser-like client that receives the 500 page Starlette sends (instead of re-raising the error)."""
    transport = httpx.ASGITransport(app=fastapi_app, client=("127.0.0.1", 51234), raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver", follow_redirects=False,
                             headers={"User-Agent": "pytest-ironpulse/1.0", "Accept": "text/html"})


@pytest.fixture(autouse=True)
def _routes(app: Any) -> Any:
    """Two test-only routes, removed again afterwards: the crawls of other test modules (test_smoke_crawl,
    test_spoofing) walk every route of the shared app."""
    @app.post("/__test/hardening/flush-conflict")
    async def _flush_conflict(request: Request, db: DB) -> dict[str, bool]:
        form = await request.form()
        request.state.user.email = str(form["email"])
        await db.flush()  # UniqueViolation: SQLAlchemy rolls back and EXPIRES every object of the session at once
        return {"ok": True}

    @app.get("/__test/hardening/boom")
    async def _boom() -> None:
        raise RuntimeError("boom")

    yield
    app.router.routes[:] = [r for r in app.router.routes
                            if not str(getattr(r, "path", "")).startswith("/__test/hardening/")]


def assert_security_headers(r: httpx.Response, csp: str) -> None:
    assert csp in r.headers.get("content-security-policy", ""), r.headers
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert r.headers.get("cross-origin-opener-policy") == "same-origin"
    assert r.headers.get("cache-control") == "no-store"


# ============================================================================ F21 / F40 / F48: image bombs
async def test_a_members_avatar_bomb_is_refused_on_its_header_before_any_pixel_is_decoded(client, db, monkeypatch):
    from PIL import ImageFile

    decoded: list[tuple[int, int]] = []
    real_load = ImageFile.ImageFile.load

    def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        decoded.append(self.size)
        return real_load(self, *args, **kwargs)

    monkeypatch.setattr(ImageFile.ImageFile, "load", spy)
    user = await make_customer(db)
    await login_as(client, user)
    bombs = {
        "bomb100.png": png_bomb(10_000, 10_000, color_type=3),  # 100 MP in ~100 KB: the hunters' +570..1000 MB
        "bomb36.png": png_bomb(6_000, 6_000),  # 36 MP RGBA, under the old 50 MP "guard": +415 MB per upload
    }
    for name, bomb in bombs.items():
        assert len(bomb) < 400 * 1024
        r = await post_form(client, "/account/photo", {}, files={"photo": (name, bomb, "image/png")})
        assert r.status_code == 400, name
        assert "That image is too large" in html.unescape(r.text) and "25 megapixels" in html.unescape(r.text)
    assert decoded == []  # refused on the header: not one pixel decoded
    await db.refresh(user)
    assert user.photo_key is None


async def test_a_large_phone_jpeg_still_uploads_at_reduced_scale_while_full_decodes_are_capped():
    from PIL import Image

    big = Image.new("RGB", (6000, 4500), (120, 130, 140))  # 27 MP: above the 25 MP cap, below the JPEG one
    baseline, progressive = io.BytesIO(), io.BytesIO()
    big.save(baseline, "JPEG", quality=80)
    big.save(progressive, "JPEG", quality=80, progressive=True)
    del big
    assert storage._jpeg_single_scan(baseline.getvalue()) and not storage._jpeg_single_scan(progressive.getvalue())
    stored = await storage.save_public_image(baseline.getvalue(), kind="test")
    assert (stored.width, stored.height) == (1600, 1200)
    await storage.delete_key(stored.key)
    # a progressive JPEG keeps every coefficient at full size; a PNG decodes in full: both capped at 25 MP
    for data in (progressive.getvalue(), png_bomb(6000, 4500, color_type=2)):
        with pytest.raises(storage.UploadError, match="That image is too large"):
            await storage.save_private_file(data, kind="test")


async def test_parallel_uploads_decode_at_most_the_slot_count_at_once(monkeypatch):
    running = peak = 0
    lock = threading.Lock()

    def slow_decode(data: bytes, max_side: int, *args: Any) -> tuple[bytes, int, int]:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.15)
        with lock:
            running -= 1
        return b"jpeg", 1, 1

    monkeypatch.setattr(storage, "_reencode_image", slow_decode)
    stored = await asyncio.gather(*(storage.save_public_image(png_bytes(), kind="test") for _ in range(6)))
    assert peak == storage.IMAGE_DECODE_SLOTS == 2
    for item in stored:
        await storage.delete_key(item.key)


async def test_an_upload_that_waits_too_long_for_a_decode_slot_is_told_to_try_again(monkeypatch):
    release = threading.Event()

    def stuck_decode(data: bytes, max_side: int, *args: Any) -> tuple[bytes, int, int]:
        release.wait(10)
        return b"jpeg", 1, 1

    monkeypatch.setattr(storage, "_reencode_image", stuck_decode)
    monkeypatch.setattr(storage, "IMAGE_DECODE_WAIT_SECONDS", 0.2)
    holders = [asyncio.ensure_future(storage.save_public_image(png_bytes(), kind="test"))
               for _ in range(storage.IMAGE_DECODE_SLOTS)]
    await asyncio.sleep(0.05)
    try:
        with pytest.raises(storage.UploadError, match="busy"):
            await storage.save_public_image(png_bytes(), kind="test")
    finally:
        release.set()
        for item in await asyncio.gather(*holders):
            await storage.delete_key(item.key)


def test_reencoding_keeps_the_orientation_and_the_transparency_rule():
    from PIL import Image

    photo = Image.new("RGB", (40, 30), "red")
    exif = Image.Exif()
    exif[0x0112] = 6  # "rotate 90° clockwise to view"
    buf = io.BytesIO()
    photo.save(buf, "JPEG", exif=exif.tobytes())
    encoded, w, h = storage._reencode_image(buf.getvalue(), 1600)
    assert (w, h) == (30, 40) and b"Exif" not in encoded
    logo = Image.new("RGBA", (20, 20), (255, 255, 255, 0))  # transparent white: drops to white, as before
    buf = io.BytesIO()
    logo.save(buf, "PNG")
    encoded, _, _ = storage._reencode_image(buf.getvalue(), 1600)
    assert min(Image.open(io.BytesIO(encoded)).convert("RGB").getpixel((10, 10))) >= 250


# ============================================================================ F41: PDFs with active content
JS_ACTION = b"<< /Type /Action /S /JavaScript /JS (app.alert('pdf-js-ran')) >>"


@pytest.mark.parametrize("data", [
    # the hunter's file: document-level JavaScript and an OpenAction, names written with #xx escapes
    classic_pdf([b"<< /Type /Catalog /Pages 2 0 R /Names << /J#61vaScript 5 0 R >> /OpenAction 4 0 R >>", *PAGES,
                 b"<< /Type /Action /S /J#61vaScript /J#53 (app.alert('pdf-js-ran')) >>",
                 b"<< /Names [(init) 4 0 R] >>"]),
    # the same script, every name inside a compressed object stream
    object_stream_pdf([b"<< /Type /Catalog /Pages 2 0 R /OpenAction 4 0 R >>", *PAGES, JS_ACTION]),
    object_stream_pdf([b"<< /Type /Catalog /Pages 2 0 R /OpenAction 4 0 R >>", *PAGES,
                       b"<< /S /Launch /F (calc.exe) >>"]),
    # an attachment whose names-tree key is escaped and whose file stream has no /Type
    classic_pdf([b"<< /Type /Catalog /Pages 2 0 R /Names << /Emb#65ddedFiles 4 0 R >> >>", *PAGES,
                 b"<< /Names [(a.exe) 5 0 R] >>", b"<< /Type /Filespec /F (a.exe) /EF << /F 6 0 R >> >>",
                 b"<< /Length 2 >>\nstream\nMZ\nendstream"]),
], ids=["escaped-names", "objstm-javascript", "objstm-launch", "escaped-attachment"])
async def test_pdfs_with_active_content_are_refused_however_their_names_are_written(data):
    with pytest.raises(storage.UploadError, match="not accepted"):
        await storage.save_private_file(data, kind="branch_docs")


async def test_plain_pdfs_still_upload():
    import pypdfium2 as pdfium

    made = pdfium.PdfDocument.new()
    made.new_page(200, 200)
    buf = io.BytesIO()
    made.save(buf)
    made.close()
    plain = [
        classic_pdf([b"<< /Type /Catalog /Pages 2 0 R >>", *PAGES]),
        object_stream_pdf([b"<< /Type /Catalog /Pages 2 0 R >>", *PAGES]),
        buf.getvalue(),
        # random bytes that happen to read '/JS' are no name /JS (a name ends at a delimiter): no false refusal
        classic_pdf([b"<< /Type /Catalog /Pages 2 0 R >>", PAGES[0],
                     b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>",
                     b"<< /Length 8 >>\nstream\n\x80/JS\x81\x82zz\nendstream"]),
    ]
    for data in plain:
        stored, body = await storage.save_private_file(data, kind="branch_docs")
        assert stored.content_type == "application/pdf" and body == data
        await storage.delete_key(stored.key)


# ============================================================================ F24: the error page after a failed flush
async def test_a_failed_flush_still_gets_the_styled_error_page_signed_in_as_the_same_user(db):
    user = await make_customer(db, full_name="Asha Rao")
    other = await make_customer(db)
    async with html_client() as c:
        await login_as(c, user)
        r = await post_form(c, "/__test/hardening/flush-conflict", {"email": other.email})
    assert r.status_code == 500 and r.headers["content-type"].startswith("text/html"), r.text[:300]
    body = html.unescape(r.text)
    assert "Something went wrong" in body
    assert f'data-user-id="{user.id}"' in body and "Asha" in body and user.email in body  # reloaded: the same account
    assert other.email not in body  # the e-mail the failed change tried to take is nowhere


async def test_when_the_user_cannot_be_reloaded_the_error_page_renders_signed_out(db, monkeypatch):
    monkeypatch.setattr(errors, "PAGE_USER_RELOAD_SECONDS", 0)  # the reload never gets its turn
    user = await make_customer(db)
    other = await make_customer(db)
    async with html_client() as c:
        await login_as(c, user)
        r = await post_form(c, "/__test/hardening/flush-conflict", {"email": other.email})
    assert r.status_code == 500 and r.headers["content-type"].startswith("text/html"), r.text[:300]
    body = html.unescape(r.text)
    assert 'data-role="guest"' in body and user.email not in body and other.email not in body


# ============================================================================ F43: headers outside Talisman
async def test_the_host_check_and_https_redirect_answers_carry_the_security_headers(monkeypatch):
    async with html_client() as c:
        r = await c.get("/healthz", headers={"Host": "evil.example"})
        assert r.status_code == 400 and r.text == "Invalid host header"
        assert_security_headers(r, "default-src 'none'")
        monkeypatch.setattr(get_settings(), "force_https", True)
        r = await c.get("/healthz")
        assert r.status_code == 308 and r.headers["location"] == "https://testserver/healthz"
        assert_security_headers(r, "default-src 'none'")
        assert r.headers.get("strict-transport-security", "").startswith("max-age=")


async def test_the_500_page_carries_the_security_headers_with_its_own_nonce():
    async with html_client() as c:
        r = await c.get("/__test/hardening/boom")
    assert r.status_code == 500 and r.headers["content-type"].startswith("text/html")
    assert_security_headers(r, "script-src 'self' 'nonce-")
    nonce = r.headers["content-security-policy"].split("'nonce-", 1)[1].split("'", 1)[0]
    assert f'nonce="{nonce}"' in r.text  # the page's own scripts and styles still load
