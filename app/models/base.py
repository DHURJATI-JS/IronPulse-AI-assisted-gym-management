"""Shared column helpers and mixins for all models."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Enum as SAEnum, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

# Dimension of every pgvector embedding column (see app/ai/embeddings.py).
EMBEDDING_DIM = 384


class IdMixin:
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)


class CreatedMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TimestampMixin(CreatedMixin):
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


def enum_col(enum_cls: type[enum.Enum], ck_name: str, **kwargs: Any) -> Any:
    """VARCHAR column + CHECK constraint holding the enum's *values* (portable, migration friendly).

    A Python ``default`` is mirrored as a ``server_default`` so raw SQL inserts behave identically."""
    default = kwargs.get("default")
    if isinstance(default, enum.Enum) and "server_default" not in kwargs:
        kwargs["server_default"] = str(default.value)
    return mapped_column(
        SAEnum(
            enum_cls,
            native_enum=False,
            length=40,
            values_callable=lambda e: [m.value for m in e],
            validate_strings=True,
            create_constraint=True,
            name=ck_name,
        ),
        **kwargs,
    )


def fk(target: str, ondelete: str, *, use_alter: bool = False, **kwargs: Any) -> Any:
    """BIGINT foreign key with an explicit ON DELETE rule (CASCADE / SET NULL / NO ACTION)."""
    nullable = kwargs.pop("nullable", ondelete == "SET NULL")
    return mapped_column(
        BigInteger,
        ForeignKey(target, ondelete=ondelete, use_alter=use_alter),
        nullable=nullable,
        index=True,
        **kwargs,
    )


def utcnow_sql() -> Any:
    return func.now()
