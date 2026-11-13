"""Storage quotas (approved load-safety limits): how many bytes of uploaded files one gym and one member may keep.

* a gym - cap ``storage_mb_per_gym``: its branches' photos and documents, its shop's product photos and its staff's
  documents;
* a member - cap ``storage_mb_per_member``: their ID card uploads.
Logos and avatars are one file each (replaced, never added); a job enquiry's résumé is one PDF per enquiry, bounded by
the enquiry caps.

``reserve_gym`` / ``reserve_member`` run inside the create's transaction AFTER its narrower count cap (lock order of
app/core/caps.py: a branch's photos before the gym's storage) and refuse with ``caps.CapReached`` when the new bytes
would pass the quota - so parallel uploads can never pass it together. The sums read only the gym's / member's own
rows (foreign-key indexes) - a few thousand rows at most, bounded by the count caps.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.core import caps
from app.models import Branch, BranchDocument, BranchPhoto, Employee, EmployeeDocument, IdentityDocument, Product, ProductImage


def _sum(column: Any) -> Any:
    return func.coalesce(func.sum(column), 0)


def gym_bytes(gym_id: int) -> Any:
    """A scalar SELECT of the bytes a gym keeps in uploaded files."""
    photos = (select(_sum(BranchPhoto.size_bytes)).join(Branch, Branch.id == BranchPhoto.branch_id)
              .where(Branch.gym_id == gym_id).scalar_subquery())
    docs = (select(_sum(BranchDocument.size_bytes)).join(Branch, Branch.id == BranchDocument.branch_id)
            .where(Branch.gym_id == gym_id).scalar_subquery())
    images = (select(_sum(ProductImage.size_bytes)).join(Product, Product.id == ProductImage.product_id)
              .where(Product.gym_id == gym_id).scalar_subquery())
    staff = (select(_sum(EmployeeDocument.size_bytes)).join(Employee, Employee.id == EmployeeDocument.employee_id)
             .where(Employee.gym_id == gym_id).scalar_subquery())
    return select(photos + docs + images + staff)


def member_bytes(user_id: int) -> Any:
    """A scalar SELECT of the bytes a member keeps in uploaded files (their ID card uploads)."""
    return select(_sum(IdentityDocument.size_bytes)).where(IdentityDocument.user_id == user_id)


async def reserve_gym(db: Any, gym_id: int, adding: int) -> int:
    """Refuse (``caps.CapReached``) when ``adding`` more bytes would pass the gym's quota; else the bytes left after."""
    return await caps.enforce_bytes(db, "storage_mb_per_gym", gym_id, gym_bytes(gym_id), adding=adding)


async def reserve_member(db: Any, user_id: int, adding: int) -> int:
    """Refuse (``caps.CapReached``) when ``adding`` more bytes would pass the member's quota."""
    return await caps.enforce_bytes(db, "storage_mb_per_member", user_id, member_bytes(user_id), adding=adding)


async def gym_usage(db: Any, gym_id: int) -> tuple[int, int]:
    """(bytes used, quota in bytes) - for a page."""
    return int(await db.scalar(gym_bytes(gym_id)) or 0), caps.limit("storage_mb_per_gym") * caps.MB


__all__ = ["gym_bytes", "gym_usage", "member_bytes", "reserve_gym", "reserve_member"]
