"""Password hashing (Argon2id, off the event loop) and password policy.

``hash_password`` / ``verify_password`` run Argon2 (~50 ms and 19 MiB each) in a worker thread. Never await them while
a database transaction is open: a pooled connection would sit idle in it all that time (longer while the threads are
busy). The sign-in reads the account, commits, verifies, then writes in a new transaction (app/routers/auth.py)."""

from __future__ import annotations

import asyncio
import re
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)  # OWASP Argon2id baseline

COMMON_PASSWORDS = {
    "password", "password1", "password123", "123456", "12345678", "123456789", "1234567890", "qwerty",
    "qwerty123", "111111", "abc123", "letmein", "welcome", "welcome1", "iloveyou", "admin", "admin123",
    "monkey", "dragon", "football", "baseball", "sunshine", "princess", "passw0rd", "p@ssw0rd", "gym12345",
    "fitness1", "ironpulse", "changeme", "trustno1", "superman", "batman", "000000", "987654321",
}


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return _hasher.hash("timing-equaliser-not-a-password")


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(password_hash: str | None, password: str) -> bool:
    """Constant-ish time: verifies against a dummy hash when the account has no password."""
    target = password_hash or _dummy_hash()

    def _verify() -> bool:
        try:
            return _hasher.verify(target, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    ok = await asyncio.to_thread(_verify)
    return ok and password_hash is not None


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def password_problems(password: str, *, email: str | None = None, name: str | None = None) -> list[str]:
    problems: list[str] = []
    if len(password) < 10:
        problems.append("Use at least 10 characters.")
    if len(password) > 128:
        problems.append("Use at most 128 characters.")
    classes = sum(bool(re.search(p, password)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    if classes < 3:
        problems.append("Mix at least three of: lowercase, uppercase, digits, symbols.")
    if password.lower() in COMMON_PASSWORDS:
        problems.append("That password is too common.")
    lowered = password.lower()
    if email:
        local = email.split("@", 1)[0].lower()
        if len(local) >= 4 and local in lowered:
            problems.append("Don't include your e-mail address in the password.")
    if name:
        for part in re.split(r"\s+", name.lower()):
            if len(part) >= 4 and part in lowered:
                problems.append("Don't include your name in the password.")
                break
    return problems
