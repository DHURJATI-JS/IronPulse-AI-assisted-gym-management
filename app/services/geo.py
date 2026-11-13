"""Geocoding (Geoapify, Nominatim fallback) + the shared HTTP client used by the map-tile proxy (WP-B).

* ``search(text)``  -> list[GeoResult]  (up to 5 normalised candidates)
* ``geocode(text)`` -> GeoResult | None (best candidate)
* ``reverse(lat, lon)`` -> GeoResult | None

Every function has timeouts + an in-memory TTL cache and NEVER raises: provider failures are logged and
return ``[]`` / ``None``. The HTTP client is injectable (``set_client``) so tests use ``httpx.MockTransport``.
Nominatim is only used when ``MAP_OSM_FALLBACK`` is on, with a proper User-Agent and <= 1 request/second.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings
from app.services.compliance import guess_timezone

log = logging.getLogger("ironpulse.geo")

USER_AGENT = "IronPulse/1.0 (student project)"
GEOAPIFY_SEARCH = "https://api.geoapify.com/v1/geocode/search"
GEOAPIFY_REVERSE = "https://api.geoapify.com/v1/geocode/reverse"
NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
TIMEOUT = httpx.Timeout(8.0, connect=4.0)
CACHE_TTL = 6 * 3600.0
NOMINATIM_MIN_INTERVAL = 1.05  # usage policy: max 1 request / second

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """Shared async HTTP client (connection pooling for geocoding and map tiles)."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
        )
    return _client


def set_client(client: httpx.AsyncClient | None) -> None:
    """Inject a client (tests: ``httpx.AsyncClient(transport=httpx.MockTransport(handler))``); None resets."""
    global _client
    _client = client


# ------------------------------------------------------------------ result model
@dataclass(slots=True)
class GeoResult:
    label: str
    lat: float
    lon: float
    city: str | None = None
    state: str | None = None
    postcode: str | None = None
    country_code: str | None = None
    timezone: str | None = None
    confidence: float | None = None
    place_id: str | None = None
    provider: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label, "lat": self.lat, "lon": self.lon, "city": self.city, "state": self.state,
            "postcode": self.postcode, "country_code": self.country_code, "timezone": self.timezone,
        }


# ------------------------------------------------------------------ tiny TTL/LRU cache
class TTLCache:
    def __init__(self, maxsize: int = 512, ttl: float = CACHE_TTL) -> None:
        self.maxsize, self.ttl = maxsize, ttl
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def get(self, key: Any) -> Any:
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: Any, value: Any, ttl: float | None = None) -> None:
        self._data[key] = (time.monotonic() + (ttl or self.ttl), value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


_search_cache = TTLCache(512)
_reverse_cache = TTLCache(512)
_nominatim_lock = asyncio.Lock()
_nominatim_last = 0.0


def clear_caches() -> None:
    _search_cache.clear()
    _reverse_cache.clear()


# ------------------------------------------------------------------ parsing
def _f(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # NaN guard


def _clean(value: Any, limit: int = 120) -> str | None:
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value)).strip()
    return s[:limit] or None


def _parse_geoapify(item: dict[str, Any]) -> GeoResult | None:
    lat, lon = _f(item.get("lat")), _f(item.get("lon"))
    if lat is None or lon is None:
        return None
    cc = (_clean(item.get("country_code"), 2) or "").upper() or None
    tz = item.get("timezone")
    tz_name = tz.get("name") if isinstance(tz, dict) else (tz if isinstance(tz, str) else None)
    rank = item.get("rank") if isinstance(item.get("rank"), dict) else {}
    return GeoResult(
        label=_clean(item.get("formatted") or item.get("address_line1") or item.get("name"), 300) or f"{lat:.5f}, {lon:.5f}",
        lat=lat, lon=lon,
        city=_clean(item.get("city") or item.get("town") or item.get("village") or item.get("suburb") or item.get("county")),
        state=_clean(item.get("state")),
        postcode=_clean(item.get("postcode"), 20),
        country_code=cc,
        timezone=_clean(tz_name, 64) or guess_timezone(cc, lon),
        confidence=_f(rank.get("confidence")),
        place_id=_clean(item.get("place_id"), 300),
        provider="geoapify",
    )


def _parse_nominatim(item: dict[str, Any]) -> GeoResult | None:
    lat, lon = _f(item.get("lat")), _f(item.get("lon"))
    if lat is None or lon is None:
        return None
    addr = item.get("address") if isinstance(item.get("address"), dict) else {}
    cc = (_clean(addr.get("country_code"), 2) or "").upper() or None
    importance = _f(item.get("importance"))
    return GeoResult(
        label=_clean(item.get("display_name"), 300) or f"{lat:.5f}, {lon:.5f}",
        lat=lat, lon=lon,
        city=_clean(addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality")
                    or addr.get("suburb") or addr.get("county")),
        state=_clean(addr.get("state") or addr.get("region")),
        postcode=_clean(addr.get("postcode"), 20),
        country_code=cc,
        timezone=guess_timezone(cc, lon),
        confidence=min(1.0, importance) if importance is not None else None,
        place_id=_clean(f"osm:{item.get('osm_type', '')}:{item.get('osm_id', item.get('place_id', ''))}", 300),
        provider="nominatim",
    )


# ------------------------------------------------------------------ providers
async def _get_json(url: str, params: dict[str, Any]) -> Any:
    resp = await get_client().get(url, params=params, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.json()


async def _nominatim(url: str, params: dict[str, Any]) -> Any:
    """Serialise Nominatim calls to <= 1 per second (OSM usage policy)."""
    global _nominatim_last
    async with _nominatim_lock:
        wait = NOMINATIM_MIN_INTERVAL - (time.monotonic() - _nominatim_last)
        if wait > 0 and _nominatim_last:
            await asyncio.sleep(wait)
        try:
            return await _get_json(url, params)
        finally:
            _nominatim_last = time.monotonic()


async def _search_geoapify(text: str, limit: int, key: str) -> list[GeoResult]:
    data = await _get_json(GEOAPIFY_SEARCH, {"text": text, "format": "json", "limit": limit, "apiKey": key})
    items = data.get("results", []) if isinstance(data, dict) else []
    return [r for r in (_parse_geoapify(i) for i in items if isinstance(i, dict)) if r][:limit]


async def _search_nominatim(text: str, limit: int) -> list[GeoResult]:
    data = await _nominatim(NOMINATIM_SEARCH, {"q": text, "format": "jsonv2", "addressdetails": 1, "limit": limit})
    items = data if isinstance(data, list) else []
    return [r for r in (_parse_nominatim(i) for i in items if isinstance(i, dict)) if r][:limit]


async def _reverse_geoapify(lat: float, lon: float, key: str) -> GeoResult | None:
    data = await _get_json(GEOAPIFY_REVERSE, {"lat": lat, "lon": lon, "format": "json", "apiKey": key})
    items = data.get("results", []) if isinstance(data, dict) else []
    return _parse_geoapify(items[0]) if items and isinstance(items[0], dict) else None


async def _reverse_nominatim(lat: float, lon: float) -> GeoResult | None:
    data = await _nominatim(NOMINATIM_REVERSE, {"lat": lat, "lon": lon, "format": "jsonv2", "addressdetails": 1})
    if not isinstance(data, dict) or data.get("error"):
        return None
    return _parse_nominatim(data)


def _normalise_query(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()[:200]


# ------------------------------------------------------------------ public API
async def search(text: str, *, limit: int = 5) -> list[GeoResult]:
    """Forward-geocode free text. Geoapify first (if configured), Nominatim as fallback. Never raises."""
    query = _normalise_query(text)
    if len(query) < 3:
        return []
    limit = max(1, min(int(limit), 10))
    cache_key = (query.lower(), limit)
    cached = _search_cache.get(cache_key)
    if cached is not None:
        return list(cached)
    settings = get_settings()
    results: list[GeoResult] = []
    answered = False
    if settings.geoapify_api_key:
        try:
            results = await _search_geoapify(query, limit, settings.geoapify_api_key)
            answered = True
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            log.warning("Geoapify geocoding failed: %s", exc.__class__.__name__)
    if not results and settings.map_osm_fallback:
        try:
            results = await _search_nominatim(query, limit)
            answered = True
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            log.warning("Nominatim geocoding failed: %s", exc.__class__.__name__)
    if answered:
        _search_cache.set(cache_key, list(results), ttl=None if results else 300.0)
    return results


async def geocode(text: str) -> GeoResult | None:
    results = await search(text, limit=5)
    return results[0] if results else None


async def reverse(lat: float, lon: float) -> GeoResult | None:
    try:
        lat, lon = round(float(lat), 5), round(float(lon), 5)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    cache_key = (lat, lon)
    cached = _reverse_cache.get(cache_key)
    if cached is not None:
        return cached or None
    settings = get_settings()
    result: GeoResult | None = None
    answered = False
    if settings.geoapify_api_key:
        try:
            result = await _reverse_geoapify(lat, lon, settings.geoapify_api_key)
            answered = True
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            log.warning("Geoapify reverse geocoding failed: %s", exc.__class__.__name__)
    if result is None and settings.map_osm_fallback:
        try:
            result = await _reverse_nominatim(lat, lon)
            answered = True
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            log.warning("Nominatim reverse geocoding failed: %s", exc.__class__.__name__)
    if answered:
        _reverse_cache.set(cache_key, result or False, ttl=None if result else 300.0)
    return result
