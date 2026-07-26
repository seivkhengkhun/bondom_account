"""Admin credential storage and verification.

The admin password lives in the existing ``app_settings`` key/value table
as a salted PBKDF2-HMAC-SHA256 hash — no schema change, and no new
dependency (``hashlib`` is stdlib, which matters because the VPS is too
small to build native wheels).

Bootstrapping: until an admin sets a password from the panel, the
``ADMIN_PASSWORD`` value in ``.env`` is accepted. Once a password has
been set through the UI, the stored hash becomes the only accepted
credential and the env var is ignored — so changing it in the panel
genuinely revokes the old one.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import settings
from shared.models import AppSetting
from shared.services import transaction_scope

ADMIN_PASSWORD_KEY = "admin_password_hash"

# 210k iterations matches the OWASP 2023 floor for PBKDF2-HMAC-SHA256.
# Stored in the record itself so the cost can be raised later without
# invalidating existing passwords.
_ITERATIONS = 210_000
_ALGO = "pbkdf2_sha256"
_SALT_BYTES = 16

MIN_PASSWORD_LENGTH = 10


@dataclass(frozen=True)
class PasswordCheck:
    """Result of validating a candidate new password."""

    ok: bool
    message: str = ""


def _encode(algo: str, iterations: int, salt: bytes, digest: bytes) -> str:
    return f"{algo}${iterations}${salt.hex()}${digest.hex()}"


def _hash(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )


def hash_password(password: str) -> str:
    """Return a self-describing PBKDF2 record for ``password``."""
    salt = os.urandom(_SALT_BYTES)
    digest = _hash(password, salt, _ITERATIONS)
    return _encode(_ALGO, _ITERATIONS, salt, digest)


def verify_hash(password: str, record: str) -> bool:
    """Constant-time check of ``password`` against a stored record."""
    try:
        algo, raw_iterations, raw_salt, raw_digest = record.split("$")
        if algo != _ALGO:
            return False
        iterations = int(raw_iterations)
        salt = bytes.fromhex(raw_salt)
        expected = bytes.fromhex(raw_digest)
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(_hash(password, salt, iterations), expected)


def validate_new_password(password: str, confirm: str) -> PasswordCheck:
    """Policy for a password chosen in the admin panel."""
    if not password:
        return PasswordCheck(False, "Enter a new password.")
    if password != confirm:
        return PasswordCheck(False, "The two new passwords do not match.")
    if len(password) < MIN_PASSWORD_LENGTH:
        return PasswordCheck(
            False, f"Use at least {MIN_PASSWORD_LENGTH} characters."
        )
    if password.lower() in {"password", "admin", "administrator"}:
        return PasswordCheck(False, "That password is too easy to guess.")
    classes = (
        any(c.islower() for c in password)
        + any(c.isupper() for c in password)
        + any(c.isdigit() for c in password)
        + any(not c.isalnum() for c in password)
    )
    if classes < 3:
        return PasswordCheck(
            False,
            "Mix at least three of: lowercase, uppercase, digits, symbols.",
        )
    return PasswordCheck(True)


def password_strength(password: str) -> tuple[int, str]:
    """Coarse 0-4 strength score plus a label, for the UI meter."""
    if not password:
        return 0, ""
    score = 0
    if len(password) >= MIN_PASSWORD_LENGTH:
        score += 1
    if len(password) >= 16:
        score += 1
    classes = (
        any(c.islower() for c in password)
        + any(c.isupper() for c in password)
        + any(c.isdigit() for c in password)
        + any(not c.isalnum() for c in password)
    )
    score += min(2, max(0, classes - 1))
    score = min(4, score)
    return score, ["Very weak", "Weak", "Fair", "Good", "Strong"][score]


async def get_stored_hash(session: AsyncSession) -> str | None:
    async with transaction_scope(session):
        row = await session.get(AppSetting, ADMIN_PASSWORD_KEY)
        if row is None:
            return None
        return row.value or None


async def has_stored_password(session: AsyncSession) -> bool:
    """True once a password has been set from the panel."""
    return await get_stored_hash(session) is not None


async def set_admin_password(session: AsyncSession, password: str) -> None:
    """Store ``password`` as the only accepted admin credential."""
    record = hash_password(password)
    async with transaction_scope(session):
        row = await session.get(AppSetting, ADMIN_PASSWORD_KEY)
        if row is None:
            session.add(AppSetting(key=ADMIN_PASSWORD_KEY, value=record))
        else:
            row.value = record


async def verify_admin_password(session: AsyncSession, password: str) -> bool:
    """Check ``password`` against the stored hash, else the env bootstrap."""
    if not password:
        return False

    stored = await get_stored_hash(session)
    if stored is not None:
        return verify_hash(password, stored)

    env_password = settings.admin_password
    if not env_password:
        return False
    # compare_digest keeps the bootstrap path constant-time too.
    return hmac.compare_digest(password, env_password)
