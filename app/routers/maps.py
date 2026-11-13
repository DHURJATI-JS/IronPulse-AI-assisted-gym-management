"""WP-B map tiles proxy (Geoapify, OpenStreetMap fallback) + geocoding API for signed-in users.

  GET /maps/tiles/{z}/{x}/{y}.png   z 0..19 and 0 <= x, y < 2^z (else 404). Same-origin so the strict CSP needs no
                                    third-party img-src; in-memory LRU (~2000 tiles), Cache-Control public 1 day;
                                    upstream failure -> transparent 1x1 PNG with a short cache.
  GET /api/geo/search?q=            3..200 chars -> {"results": [{label, lat, lon, city, state, postcode,
                                    country_code, timezone}]}
  GET /api/geo/reverse?lat=&lon=    -> same shape (0 or 1 result)
"""

from __future__ import annotations

import base64
import logging
from collections import OrderedDict

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from app.config import get_settings
from app.core.deps import AuthUser, NotFound
from app.core.geo import valid_coords
from app.services import geo

log = logging.getLogger("ironpulse.maps")
router = APIRouter()

MAX_ZOOM = 19
TILE_CACHE_SIZE = 2000
TILE_CACHE_MAX_BYTES = 48 * 1024 * 1024
TILE_CACHE_HEADERS = {"Cache-Control": "public, max-age=86400"}
FALLBACK_CACHE_HEADERS = {"Cache-Control": "public, max-age=60"}
GEOAPIFY_TILE = "https://maps.geoapify.com/v1/tile/osm-bright/{z}/{x}/{y}.png?apiKey={key}"
OSM_TILE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_USER_AGENT = "IronPulse/1.0 (student project)"
TRANSPARENT_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNgYGBgAAAABQABeqhXUAAAAABJRU5ErkJggg=="
)

_tiles: OrderedDict[tuple[str, int, int, int], bytes] = OrderedDict()
_tile_bytes = [0]


def clear_tile_cache() -> None:
    _tiles.clear()
    _tile_bytes[0] = 0


def _remember(key: tuple[str, int, int, int], data: bytes) -> None:
    """LRU by count (~2000 tiles) and by size (48 MB)."""
    old = _tiles.pop(key, None)
    if old is not None:
        _tile_bytes[0] -= len(old)
    _tiles[key] = data
    _tile_bytes[0] += len(data)
    while _tiles and (len(_tiles) > TILE_CACHE_SIZE or _tile_bytes[0] > TILE_CACHE_MAX_BYTES):
        _, evicted = _tiles.popitem(last=False)
        _tile_bytes[0] -= len(evicted)


def _parse_tile(z: str, x: str, y: str) -> tuple[int, int, int]:
    try:
        zi, xi, yi = int(z), int(x), int(y)
    except ValueError:
        raise NotFound("Unknown map tile.") from None
    if not 0 <= zi <= MAX_ZOOM:
        raise NotFound("Unknown map tile.")
    n = 1 << zi
    if not (0 <= xi < n and 0 <= yi < n):
        raise NotFound("Unknown map tile.")
    return zi, xi, yi


def _transparent() -> Response:
    return Response(TRANSPARENT_PNG, media_type="image/png", headers=FALLBACK_CACHE_HEADERS)


async def _fetch(url: str, headers: dict[str, str]) -> bytes | None:
    try:
        resp = await geo.get_client().get(url, headers=headers)
    except httpx.HTTPError as exc:
        log.info("tile fetch failed: %s", exc.__class__.__name__)
        return None
    ctype = resp.headers.get("content-type", "")
    if resp.status_code != 200 or not resp.content or not ctype.startswith("image/"):
        log.info("tile upstream answered %s (%s)", resp.status_code, ctype)
        return None
    return resp.content


@router.get("/maps/tiles/{z}/{x}/{y}.png", include_in_schema=False)
async def map_tile(z: str, x: str, y: str) -> Response:
    zi, xi, yi = _parse_tile(z, x, y)
    settings = get_settings()
    sources: list[tuple[str, str, dict[str, str]]] = []
    if settings.geoapify_api_key:
        sources.append(("geoapify", GEOAPIFY_TILE.format(z=zi, x=xi, y=yi, key=settings.geoapify_api_key),
                        {"User-Agent": OSM_USER_AGENT, "Accept": "image/png,image/*"}))
    if settings.map_osm_fallback:
        sources.append(("osm", OSM_TILE.format(z=zi, x=xi, y=yi), {"User-Agent": OSM_USER_AGENT, "Accept": "image/png,image/*"}))
    for provider, _url, _headers in sources:
        cached = _tiles.get((provider, zi, xi, yi))
        if cached is not None:
            _tiles.move_to_end((provider, zi, xi, yi))
            return Response(cached, media_type="image/png", headers=TILE_CACHE_HEADERS)
    for provider, url, headers in sources:
        data = await _fetch(url, headers)
        if data is not None:
            _remember((provider, zi, xi, yi), data)
            return Response(data, media_type="image/png", headers=TILE_CACHE_HEADERS)
    return _transparent()


def _bad_request(detail: str) -> JSONResponse:
    return JSONResponse({"error": "Invalid input", "detail": detail}, status_code=400)


@router.get("/api/geo/search")
async def geo_search(user: AuthUser, q: str = "") -> JSONResponse:
    query = " ".join((q or "").split())
    if not 3 <= len(query) <= 200:
        return _bad_request("Type between 3 and 200 characters to search for an address.")
    results = await geo.search(query, limit=5)
    return JSONResponse({"results": [r.as_dict() for r in results]})


@router.get("/api/geo/reverse")
async def geo_reverse(user: AuthUser, lat: str = "", lon: str = "") -> JSONResponse:
    try:
        flat, flon = float(lat), float(lon)
    except ValueError:
        return _bad_request("Send numeric lat and lon.")
    if not valid_coords(flat, flon):
        return _bad_request("Latitude must be -90..90 and longitude -180..180.")
    result = await geo.reverse(flat, flon)
    return JSONResponse({"results": [result.as_dict()] if result else []})
