"""How many gyms one person may be a member of at once (approved limits - the owner: "the person can join up to 3 gyms
online mode and only 1 gym at offline mode").

* online-capable memberships (online or combo plans): at most ``MAX_ONLINE_GYMS`` (3) DISTINCT gyms;
* in-person-capable memberships (gym or combo plans): at most ``MAX_OFFLINE_GYMS`` (1) gym. A combo uses both.

Both values are caps of app/core/caps.py (``online_gyms`` / ``offline_gyms``), read from the settings
``MAX_ONLINE_GYMS`` / ``MAX_OFFLINE_GYMS``.

Counted: active memberships (until their end date), suspended ones and unfinished purchases (waiting for the desk
check or the payment) - so two checkouts at the same moment can never both take the last slot. An unfinished purchase
is discarded after 7 days (``checkout.cancel_stale_pending``, or at once under "My memberships") and frees its slot.
Expired, cancelled and failed memberships never count. Renewing or switching plans at a gym the person already holds
never needs another slot; moving up from online to a gym or combo plan at that gym needs the in-person slot.

Every join path calls ``join_refusal`` - the member's checkout (``checkout.start_subscription``) and the owner's desk
registration (``checkout.register_cash_subscription``) - which first takes ``lock``: a transaction-scoped advisory lock
per person (``caps.lock``), held until the caller's COMMIT / ROLLBACK and taken BEFORE any transaction or subscription
row lock (lock order: person -> transaction -> subscription), so parallel joins of one person are serialised and can
never deadlock with a payment. Payment (``/checkout/{sid}/pay``) and activation (``checkout.settle_order``) check again:
a join that still slipped through (an unfinished purchase from before the limit, a late payment of a failed one) is
never activated - the payment is kept for the gym to review and refund, and the member and the owner are told.

Members already above a limit when it was introduced keep every membership (grandfathered): only joining another gym
is refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select

from app.core import caps
from app.models import Gym, Plan, Subscription
from app.models.enums import PlanMode, SubscriptionStatus

LOCK = "gym_memberships"  # caps.lock name: one per person, shared by every join, payment and activation
PENDING = (SubscriptionStatus.PENDING_OTP, SubscriptionStatus.PENDING_PAYMENT)
COUNTED = (SubscriptionStatus.ACTIVE, SubscriptionStatus.SUSPENDED, *PENDING)
ONLINE_MODES = (PlanMode.ONLINE, PlanMode.COMBO)
IN_PERSON_MODES = (PlanMode.OFFLINE, PlanMode.COMBO)
READ_MAX = 200  # (gym, mode) pairs read for one person - far above any limit, so a grandfathered list stays bounded
MY_MEMBERSHIPS = "/me/subscriptions"


def max_online() -> int:
    return caps.limit("online_gyms")


def max_in_person() -> int:
    return caps.limit("offline_gyms")


@dataclass(frozen=True, slots=True)
class HeldGym:
    id: int
    name: str
    slug: str
    pending: bool  # only an unfinished purchase holds this slot (it can be discarded under "My memberships")

    @property
    def label(self) -> str:
        return f"{self.name} (unfinished purchase)" if self.pending else self.name


def _names(gyms: list[HeldGym]) -> str:
    labels = [g.label for g in gyms]
    if len(labels) <= 1:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def _gyms(n: int) -> str:
    return f"{n} gym" if n == 1 else f"{n} gyms"


@dataclass(slots=True)
class GymSlots:
    """The gyms one person holds, per kind of membership (distinct gyms, A-Z)."""

    online: list[HeldGym] = field(default_factory=list)
    in_person: list[HeldGym] = field(default_factory=list)
    online_max: int = 3
    in_person_max: int = 1

    @property
    def online_used(self) -> int:
        return len(self.online)

    @property
    def in_person_used(self) -> int:
        return len(self.in_person)

    @property
    def summary(self) -> str:
        """``Online gyms 1/3 · In-person gym 0/1`` - the member dashboard's line."""
        noun = "gym" if self.in_person_max == 1 else "gyms"
        return (f"Online gyms {self.online_used}/{self.online_max} · "
                f"In-person {noun} {self.in_person_used}/{self.in_person_max}")

    def _blocked(self, gym_id: int, mode: PlanMode) -> tuple[str, list[HeldGym], int] | None:
        """(kind, gyms holding the slots, limit) when joining ``gym_id`` with a ``mode`` plan needs a slot that is full."""
        if mode in IN_PERSON_MODES and all(g.id != gym_id for g in self.in_person) \
                and len(self.in_person) >= self.in_person_max:
            return "in-person", self.in_person, self.in_person_max
        if mode in ONLINE_MODES and all(g.id != gym_id for g in self.online) and len(self.online) >= self.online_max:
            return "online", self.online, self.online_max
        return None

    def refusal(self, gym_id: int, mode: PlanMode, *, who: str | None = None) -> str | None:
        """The friendly refusal of joining ``gym_id`` on a ``mode`` plan (None = allowed). ``who`` = the member's name
        when someone else (the gym owner at the desk) asks; else it speaks to the member."""
        blocked = self._blocked(gym_id, mode)
        if blocked is None:
            return None
        kind, held, limit = blocked
        plans = "gym or combo" if kind == "in-person" else "online or combo"
        rule = f"one person can be an {kind} ({plans}) member of at most {_gyms(limit)} at a time"
        which = "that membership" if len(held) == 1 else "one of them"
        online_ok = kind == "in-person" and (any(g.id == gym_id for g in self.online)
                                             or len(self.online) < self.online_max)
        if who:
            alt = " - or join here on an online plan" if online_ok else ""
            return f"{who} is already an {kind} member of {_names(held)} - {rule}. They can end {which} first{alt}."
        hint = " Online plans here are still open to you." if online_ok else ""
        return f"You're already an {kind} member of {_names(held)} - {rule}. To join here, end {which} under My " \
               f"memberships first.{hint}"

    def short_reason(self, gym_id: int, mode: PlanMode) -> str | None:
        """A plan card's note (the button is replaced by it)."""
        blocked = self._blocked(gym_id, mode)
        if blocked is None:
            return None
        kind, held, limit = blocked
        return f"Limit reached: you're already an {kind} member of {_gyms(limit)} ({_names(held)})"


async def gym_slots(db: Any, customer_id: int, *, exclude_sub_id: int | None = None) -> GymSlots:
    """The distinct gyms ``customer_id`` holds online / in person (one statement, at most READ_MAX rows). Without the
    lock: exact for a page, a guess for a decision - decisions use ``join_refusal``."""
    current = or_(Subscription.status.in_(PENDING), Subscription.ends_on.is_(None),
                  Subscription.ends_on >= func.current_date())
    stmt = (
        select(Subscription.gym_id, Gym.name, Gym.slug, Plan.mode,
               func.bool_and(Subscription.status.in_(PENDING)).label("pending"))
        .join(Plan, Plan.id == Subscription.plan_id)
        .join(Gym, Gym.id == Subscription.gym_id)
        .where(Subscription.customer_id == customer_id, Subscription.status.in_(COUNTED), current)
        .group_by(Subscription.gym_id, Gym.name, Gym.slug, Plan.mode)
        .order_by(Gym.name, Subscription.gym_id)
        .limit(READ_MAX)
    )
    if exclude_sub_id is not None:
        stmt = stmt.where(Subscription.id != exclude_sub_id)
    online: dict[int, HeldGym] = {}
    in_person: dict[int, HeldGym] = {}
    for gym_id, name, slug, mode, pending in (await db.execute(stmt)).all():
        for bucket, modes in ((online, ONLINE_MODES), (in_person, IN_PERSON_MODES)):
            if mode in modes:
                held = bucket.get(gym_id)
                # a gym held by an unfinished purchase AND a real membership counts as a real membership
                bucket[gym_id] = HeldGym(gym_id, name, slug, bool(pending) and (held is None or held.pending))
    return GymSlots(list(online.values()), list(in_person.values()), max_online(), max_in_person())


async def lock(db: Any, customer_id: int) -> None:
    """Serialise every join / payment / activation of this person until the caller's transaction ends."""
    await caps.lock(db, LOCK, int(customer_id))


async def join_refusal(db: Any, customer_id: int, gym_id: int, mode: PlanMode, *, exclude_sub_id: int | None = None,
                       who: str | None = None) -> str | None:
    """Under the person's lock: why ``customer_id`` may not join ``gym_id`` on a ``mode`` plan (None = allowed).
    ``exclude_sub_id`` = the purchase being paid / activated (it must not count for itself)."""
    await lock(db, customer_id)
    slots = await gym_slots(db, customer_id, exclude_sub_id=exclude_sub_id)
    return slots.refusal(gym_id, mode, who=who)


__all__ = ["COUNTED", "GymSlots", "HeldGym", "IN_PERSON_MODES", "LOCK", "MY_MEMBERSHIPS", "ONLINE_MODES", "gym_slots",
           "join_refusal", "lock", "max_in_person", "max_online"]
