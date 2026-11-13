"""Geographic helpers (pure functions)."""

from __future__ import annotations

import math
import re
import unicodedata

EARTH_RADIUS_KM = 6371.0088


def valid_coords(lat: float | None, lon: float | None) -> bool:
    return (
        lat is not None
        and lon is not None
        and -90.0 <= float(lat) <= 90.0
        and -180.0 <= float(lon) <= 180.0
        and not (float(lat) == 0.0 and float(lon) == 0.0)
    )


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def normalize_city(city: str | None) -> str:
    """'Bengaluru ' / 'BENGALURU' / 'Bengalūru' -> 'bengaluru' (plus a few common aliases)."""
    if not city:
        return ""
    c = unicodedata.normalize("NFKD", city).encode("ascii", "ignore").decode().lower().strip()
    c = re.sub(r"[^a-z ]+", " ", c)
    c = re.sub(r"\s+", " ", c).strip()
    aliases = {
        "bangalore": "bengaluru", "bombay": "mumbai", "madras": "chennai", "calcutta": "kolkata",
        "gurgaon": "gurugram", "new delhi": "delhi", "nyc": "new york", "new york city": "new york",
        "trivandrum": "thiruvananthapuram", "mysore": "mysuru", "poona": "pune",
    }
    return aliases.get(c, c)
