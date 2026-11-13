"""WP-B maps: tile proxy validation/caching/fallback and the geocoding API (httpx.MockTransport - no network)."""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.routers import maps as maps_router
from app.services import geo
from tests.conftest import login_as
from tests.factories import make_customer, make_owner, png_bytes

TILE_PNG = png_bytes((256, 256), "lightgray")

NOMINATIM_SEARCH = [{
    "place_id": 1, "osm_type": "way", "osm_id": 42, "lat": "12.9716", "lon": "77.6412", "importance": 0.61,
    "display_name": "100 Feet Road, Indiranagar, Bengaluru, Karnataka, 560038, India",
    "address": {"road": "100 Feet Road", "town": "Bengaluru", "state": "Karnataka", "postcode": "560038", "country_code": "in"},
}]
GEOAPIFY_SEARCH = {"results": [{
    "formatted": "80 Feet Road, Koramangala, Bengaluru 560034, India", "lat": 12.9352, "lon": 77.6245, "city": "Bengaluru",
    "state": "Karnataka", "postcode": "560034", "country_code": "in", "timezone": {"name": "Asia/Kolkata"},
    "rank": {"confidence": 0.93}, "place_id": "51abc",
}]}


@pytest.fixture
def http(monkeypatch):
    """Install a MockTransport-backed client for geo + tiles. Returns (set_handler, requests)."""
    requests: list[httpx.Request] = []
    state = {"handler": lambda req: httpx.Response(404)}

    def transport(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        return state["handler"](req)

    geo.set_client(httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    geo.clear_caches()
    maps_router.clear_tile_cache()
    monkeypatch.setattr(geo, "NOMINATIM_MIN_INTERVAL", 0.0)
    settings = get_settings()
    monkeypatch.setattr(settings, "geoapify_api_key", None)
    monkeypatch.setattr(settings, "map_osm_fallback", True)

    def set_handler(fn):
        state["handler"] = fn

    yield set_handler, requests
    geo.set_client(None)
    geo.clear_caches()
    maps_router.clear_tile_cache()


# ------------------------------------------------------------------ tiles
@pytest.mark.parametrize("path", [
    "/maps/tiles/20/0/0.png", "/maps/tiles/-1/0/0.png", "/maps/tiles/2/4/0.png", "/maps/tiles/2/0/4.png",
    "/maps/tiles/3/-1/2.png", "/maps/tiles/a/b/c.png", "/maps/tiles/1/1/1.jpg",
])
async def test_tile_coordinates_are_validated(client, http, path):
    _, requests = http
    r = await client.get(path)
    assert r.status_code == 404
    assert requests == []  # never proxied upstream


async def test_tile_proxy_uses_osm_with_user_agent_and_caches(client, http):
    set_handler, requests = http
    set_handler(lambda req: httpx.Response(200, content=TILE_PNG, headers={"content-type": "image/png"}))
    r = await client.get("/maps/tiles/3/2/5.png")
    assert r.status_code == 200 and r.content == TILE_PNG
    assert r.headers["content-type"] == "image/png" and r.headers["cache-control"] == "public, max-age=86400"
    assert len(requests) == 1
    req = requests[0]
    assert req.url.host == "tile.openstreetmap.org" and req.url.path == "/3/2/5.png"
    assert req.headers["user-agent"] == "IronPulse/1.0 (student project)"
    again = await client.get("/maps/tiles/3/2/5.png")
    assert again.status_code == 200 and again.content == TILE_PNG
    assert len(requests) == 1  # served from the in-memory LRU


async def test_tile_proxy_prefers_geoapify_when_configured(client, http, monkeypatch):
    set_handler, requests = http
    monkeypatch.setattr(get_settings(), "geoapify_api_key", "test-key")
    set_handler(lambda req: httpx.Response(200, content=TILE_PNG, headers={"content-type": "image/png"}))
    r = await client.get("/maps/tiles/0/0/0.png")
    assert r.status_code == 200
    assert requests[0].url.host == "maps.geoapify.com" and requests[0].url.params["apiKey"] == "test-key"
    assert requests[0].url.path == "/v1/tile/osm-bright/0/0/0.png"


async def test_tile_falls_back_to_osm_then_transparent_png(client, http, monkeypatch):
    set_handler, requests = http
    monkeypatch.setattr(get_settings(), "geoapify_api_key", "test-key")

    def handler(req):
        if req.url.host == "maps.geoapify.com":
            return httpx.Response(503)
        return httpx.Response(200, content=TILE_PNG, headers={"content-type": "image/png"})

    set_handler(handler)
    r = await client.get("/maps/tiles/1/1/1.png")
    assert r.status_code == 200 and r.content == TILE_PNG and len(requests) == 2

    def broken(req):
        raise httpx.ConnectError("offline", request=req)

    set_handler(broken)
    r = await client.get("/maps/tiles/1/0/1.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content == maps_router.TRANSPARENT_PNG and r.headers["cache-control"] == "public, max-age=60"


async def test_tile_without_any_provider_is_transparent(client, http, monkeypatch):
    _, requests = http
    monkeypatch.setattr(get_settings(), "map_osm_fallback", False)
    r = await client.get("/maps/tiles/4/3/2.png")
    assert r.status_code == 200 and r.content == maps_router.TRANSPARENT_PNG and requests == []


async def test_tile_upstream_html_error_is_not_cached(client, http):
    set_handler, requests = http
    set_handler(lambda req: httpx.Response(200, content=b"<html>blocked</html>", headers={"content-type": "text/html"}))
    r = await client.get("/maps/tiles/2/1/1.png")
    assert r.content == maps_router.TRANSPARENT_PNG
    set_handler(lambda req: httpx.Response(200, content=TILE_PNG, headers={"content-type": "image/png"}))
    r = await client.get("/maps/tiles/2/1/1.png")
    assert r.content == TILE_PNG and len(requests) == 2


# ------------------------------------------------------------------ geocoding API
async def test_geo_search_requires_sign_in_and_validates(client, anon_client_factory, db, http):
    anon = anon_client_factory()
    r = await anon.get("/api/geo/search?q=Indiranagar")
    assert r.status_code == 401 and r.headers["content-type"].startswith("application/json")
    user = await make_customer(db)
    await login_as(client, user)
    assert (await client.get("/api/geo/search?q=ab")).status_code == 400
    assert (await client.get("/api/geo/search?q=" + "x" * 201)).status_code == 400
    assert (await client.get("/api/geo/reverse?lat=abc&lon=1")).status_code == 400
    assert (await client.get("/api/geo/reverse?lat=95&lon=1")).status_code == 400


async def test_geo_search_nominatim_fallback_is_normalised(client, db, http):
    set_handler, requests = http
    set_handler(lambda req: httpx.Response(200, json=NOMINATIM_SEARCH))
    owner = await make_owner(db)
    await login_as(client, owner)
    r = await client.get("/api/geo/search", params={"q": "100 Feet Road, Indiranagar, Bengaluru"})
    assert r.status_code == 200
    (res,) = r.json()["results"]
    assert res == {"label": "100 Feet Road, Indiranagar, Bengaluru, Karnataka, 560038, India", "lat": 12.9716, "lon": 77.6412,
                   "city": "Bengaluru", "state": "Karnataka", "postcode": "560038", "country_code": "IN",
                   "timezone": "Asia/Kolkata"}
    req = requests[0]
    assert req.url.host == "nominatim.openstreetmap.org" and req.url.params["format"] == "jsonv2"
    assert req.url.params["addressdetails"] == "1" and req.headers["user-agent"].startswith("IronPulse/1.0")
    await client.get("/api/geo/search", params={"q": "100 Feet Road, Indiranagar, Bengaluru"})
    assert len(requests) == 1  # cached


async def test_geo_search_geoapify_and_failure_paths(client, db, http, monkeypatch):
    set_handler, requests = http
    monkeypatch.setattr(get_settings(), "geoapify_api_key", "geo-key")
    set_handler(lambda req: httpx.Response(200, json=GEOAPIFY_SEARCH))
    owner = await make_owner(db)
    await login_as(client, owner)
    r = await client.get("/api/geo/search", params={"q": "80 Feet Road Koramangala"})
    res = r.json()["results"][0]
    assert res["timezone"] == "Asia/Kolkata" and res["country_code"] == "IN" and res["lat"] == 12.9352
    assert requests[0].url.host == "api.geoapify.com" and requests[0].url.params["format"] == "json"

    def broken(req):
        raise httpx.ReadTimeout("slow", request=req)

    set_handler(broken)
    r = await client.get("/api/geo/search", params={"q": "Somewhere unknown 123"})
    assert r.status_code == 200 and r.json() == {"results": []}


async def test_geo_reverse(client, db, http):
    set_handler, _ = http
    set_handler(lambda req: httpx.Response(200, json=NOMINATIM_SEARCH[0]))
    user = await make_customer(db)
    await login_as(client, user)
    r = await client.get("/api/geo/reverse?lat=12.9716&lon=77.6412")
    assert r.status_code == 200 and r.json()["results"][0]["postcode"] == "560038"
    set_handler(lambda req: httpx.Response(200, json={"error": "Unable to geocode"}))
    r = await client.get("/api/geo/reverse?lat=-45.1&lon=170.2")
    assert r.status_code == 200 and r.json() == {"results": []}


async def test_geo_service_never_raises(http):
    set_handler, _ = http
    set_handler(lambda req: httpx.Response(500))
    assert await geo.search("Indiranagar Bengaluru") == []
    assert await geo.geocode("Indiranagar Bengaluru") is None
    assert await geo.reverse(12.97, 77.64) is None
    assert await geo.reverse("x", None) is None  # type: ignore[arg-type]
    set_handler(lambda req: httpx.Response(200, content=b"not json"))
    assert await geo.search("Another place 42") == []
    assert await geo.search("ab") == []  # too short: no request


def test_tile_cache_is_bounded_by_count_and_bytes(monkeypatch):
    maps_router.clear_tile_cache()
    monkeypatch.setattr(maps_router, "TILE_CACHE_SIZE", 3)
    monkeypatch.setattr(maps_router, "TILE_CACHE_MAX_BYTES", 10)
    for i in range(5):
        maps_router._remember(("osm", 3, 0, i), b"1234")
    assert list(maps_router._tiles) == [("osm", 3, 0, 3), ("osm", 3, 0, 4)]  # 10-byte budget keeps 2 tiles
    assert maps_router._tile_bytes[0] == 8
    monkeypatch.setattr(maps_router, "TILE_CACHE_MAX_BYTES", 10_000)
    for i in range(5, 9):
        maps_router._remember(("osm", 3, 0, i), b"1234")
    assert len(maps_router._tiles) == 3  # count limit
    maps_router.clear_tile_cache()
    assert maps_router._tile_bytes[0] == 0
