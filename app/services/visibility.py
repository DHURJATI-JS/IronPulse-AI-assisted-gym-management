"""Public visibility of branches and the 30 km offline-membership rule.

A physical branch is PUBLISHED (listed on the website, sells offline plans) only when:
  verified by the AI pipeline  AND  open  AND  its gym has physical locations enabled and is active
  AND  it has at least MIN_BRANCH_PHOTOS photos (machines + overall gym).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from app.config import get_settings
from app.core.geo import haversine_km, normalize_city, valid_coords
from app.models import Branch, BranchPhoto, Gym
from app.models.enums import VerificationStatus


def branch_photo_count() -> Any:
    """Correlated scalar subquery: number of photos of ``Branch``."""
    return (
        select(func.count(BranchPhoto.id)).where(BranchPhoto.branch_id == Branch.id).correlate(Branch).scalar_subquery()
    )


def published_branch_clauses() -> list[Any]:
    """WHERE clauses for published branches (the query must JOIN Gym on Branch.gym_id)."""
    return [
        Branch.verification_status == VerificationStatus.VERIFIED,
        Branch.is_open.is_(True),
        Gym.offline_enabled.is_(True),
        Gym.is_active.is_(True),
        branch_photo_count() >= get_settings().min_branch_photos,
    ]


def is_branch_published(branch: Branch, gym: Gym, photo_count: int) -> bool:
    return (
        branch.verification_status == VerificationStatus.VERIFIED
        and branch.is_open
        and gym.offline_enabled
        and gym.is_active
        and photo_count >= get_settings().min_branch_photos
    )


@dataclass(slots=True)
class OfflineEligibility:
    eligible: bool
    method: str  # "gps" | "city" | "unknown"
    distance_km: float | None
    nearest_branch_id: int | None
    message: str


def not_near_message(distance_km: float | None) -> str:
    limit = get_settings().offline_max_distance_km
    where = f" (the nearest branch is about {distance_km:.0f} km away)" if distance_km is not None else ""
    return (
        f"This gym is not near your area{where}. Offline memberships are only sold within {limit:.0f} km of a branch. "
        "If you still want an offline membership, please visit the gym owner directly for offline registration."
    )


def offline_eligibility(
    branches: Sequence[Branch], *, lat: float | None, lon: float | None, city: str | None
) -> OfflineEligibility:
    """Check the customer's location against every published branch (GPS first, city as fallback)."""
    limit = get_settings().offline_max_distance_km
    if not branches:
        return OfflineEligibility(False, "unknown", None, None, "This gym has no verified physical branch yet.")
    if valid_coords(lat, lon):
        best: tuple[float, int] | None = None
        for b in branches:
            if valid_coords(b.latitude, b.longitude):
                d = haversine_km(float(lat), float(lon), float(b.latitude), float(b.longitude))  # type: ignore[arg-type]
                if best is None or d < best[0]:
                    best = (d, b.id)
        if best is not None:
            ok = best[0] <= limit
            return OfflineEligibility(ok, "gps", round(best[0], 1), best[1],
                                      f"Nearest branch is {best[0]:.1f} km away." if ok else not_near_message(best[0]))
    if city:
        c = normalize_city(city)
        for b in branches:
            if normalize_city(b.city) == c:
                return OfflineEligibility(True, "city", None, b.id, f"A branch is in {b.city}.")
        return OfflineEligibility(False, "city", None, None, not_near_message(None))
    return OfflineEligibility(
        False, "unknown", None, None,
        "Share your location (or set your city in your account) so we can check that a branch is within "
        f"{limit:.0f} km of you.",
    )
