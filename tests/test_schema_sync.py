"""The additive schema sync repairs drift on existing databases without touching data."""

from __future__ import annotations

from sqlalchemy import text

from app import db as dbmod


async def test_sync_restores_missing_column_index_check_and_widens_enum(db):
    async with dbmod.get_engine().begin() as conn:
        await conn.execute(text("INSERT INTO users (email, full_name, role) VALUES ('keep@example.com', 'Keep Me', 'customer')"))
        await conn.execute(text("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_username_length"))
        await conn.execute(text("DROP INDEX IF EXISTS ix_users_username"))
        await conn.execute(text("ALTER TABLE users DROP COLUMN username"))
        await conn.execute(text("ALTER TABLE transactions DROP CONSTRAINT ck_transactions_category"))
        await conn.execute(text("ALTER TABLE transactions ADD CONSTRAINT ck_transactions_category CHECK (category IN ('subscription'))"))
    async with dbmod.get_engine().begin() as conn:
        applied = await dbmod.sync_schema(conn)
    assert "column users.username" in applied
    assert "index ix_users_username" in applied
    assert "check ck_users_username_length" in applied
    assert "enum check ck_transactions_category" in applied
    async with dbmod.get_engine().begin() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM users WHERE email = 'keep@example.com'")) == 1  # data kept
        definition = await conn.scalar(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_transactions_category'"))
        assert "shop_sale" in definition
        again = await dbmod.sync_schema(conn)
    assert again == []  # idempotent
