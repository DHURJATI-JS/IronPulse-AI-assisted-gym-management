"""Read-only subscription helpers shared by every feature (purchase flows live in app/services/checkout.py).

``plan_access`` answers "what may this customer use right now?":
* pre-recorded videos  -> any active subscription
* AI coach / AI advice -> an active plan with ``includes_ai``
* trainer              -> an active plan with ``includes_trainer``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import Plan, Subscription
from app.models.enums import PlanMode, SubscriptionStatus


@dataclass(slots=True)
class PlanAccess:
    subscriptions: list[Subscription] = field(default_factory=list)

    @property
    def has_any(self) -> bool:
        return bool(self.subscriptions)

    @property
    def videos(self) -> bool:  # included in every plan
        return self.has_any

    @property
    def ai(self) -> bool:
        return any(s.plan.includes_ai for s in self.subscriptions)

    @property
    def trainer(self) -> bool:
        return any(s.plan.includes_trainer for s in self.subscriptions)

    @property
    def online(self) -> bool:
        return any(s.plan.mode in (PlanMode.ONLINE, PlanMode.COMBO) for s in self.subscriptions)

    @property
    def offline(self) -> bool:
        return any(s.plan.mode in (PlanMode.OFFLINE, PlanMode.COMBO) for s in self.subscriptions)

    @property
    def gym_ids(self) -> set[int]:
        return {s.gym_id for s in self.subscriptions}


async def active_subscriptions(db: AsyncSession, customer_id: int, *, today: date | None = None) -> list[Subscription]:
    today = today or date.today()
    stmt = (
        select(Subscription)
        .options(joinedload(Subscription.plan), joinedload(Subscription.gym), joinedload(Subscription.branch))
        .where(
            Subscription.customer_id == customer_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
            or_(Subscription.ends_on.is_(None), Subscription.ends_on >= today),
        )
        .order_by(Subscription.ends_on.desc().nulls_last())
    )
    return list((await db.execute(stmt)).unique().scalars().all())


async def plan_access(db: AsyncSession, customer_id: int) -> PlanAccess:
    return PlanAccess(await active_subscriptions(db, customer_id))


async def has_active_subscription(db: AsyncSession, customer_id: int, gym_id: int | None = None) -> bool:
    stmt = select(Subscription.id).join(Plan, Plan.id == Subscription.plan_id).where(
        Subscription.customer_id == customer_id,
        Subscription.status == SubscriptionStatus.ACTIVE,
        or_(Subscription.ends_on.is_(None), Subscription.ends_on >= date.today()),
    )
    if gym_id is not None:
        stmt = stmt.where(Subscription.gym_id == gym_id)
    return (await db.execute(stmt.limit(1))).first() is not None
