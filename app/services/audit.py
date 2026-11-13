"""Append-only audit trail of sensitive actions (shown to owners and the super-admin)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, User


async def audit(
    db: AsyncSession,
    actor: User | None,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    *,
    gym_id: int | None = None,
    detail: dict[str, Any] | None = None,
    request: Any = None,
) -> AuditLog:
    ip = None
    if request is not None:
        from app.security.net import client_ip

        ip = client_ip(request)
    row = AuditLog(
        actor_id=actor.id if actor is not None else None,
        gym_id=gym_id,
        action=action[:80],
        entity_type=entity_type[:40],
        entity_id=entity_id,
        detail=detail or {},
        ip=ip,
    )
    db.add(row)
    return row
