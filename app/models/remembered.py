"""Remembered accounts of the sign-in page's account chooser ("like Google").

A browser that signed in with "Remember this account on this device" keeps, in its localStorage, ONLY an opaque random
reference per remembered account - never the database id (sequential ids would let anyone enumerate members' names and
photos), a name, an e-mail, a photo or a password. The server keeps only the SHA-256 of each reference, with the account
it points at, the SHA-256 of the browser's own random device key (no fingerprinting: the key is a random value the
page generated and keeps in that browser's localStorage), the session it was issued with, a coarse "Chrome on macOS"
label and two timestamps. app/services/remembered_accounts.py has the rules: at most MAX_PER_DEVICE references per
device and MAX_PER_USER per account, unused ones expire, and every reference of an account is revoked on "sign out
everywhere", a password change or reset, account deletion and a ban (``revoke_all_sessions``); purging the account
deletes them (ON DELETE CASCADE).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import fk


class RememberedAccount(Base):
    __tablename__ = "remembered_accounts"
    __table_args__ = (
        # per-device cap and "one entry per account per device": WHERE device_hash = ? [AND user_id = ?]
        Index("ix_remembered_accounts_device", "device_hash", "last_used_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 hex of the reference the browser holds
    user_id: Mapped[int] = fk("users.id", "CASCADE")
    device_hash: Mapped[str] = mapped_column(String(64))  # sha256 hex of the browser's random device key
    session_id: Mapped[str | None] = mapped_column(String(64))  # the session it was issued with / last used by
    label: Mapped[str | None] = mapped_column(String(80))  # "Chrome on macOS" - never the raw user agent
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
