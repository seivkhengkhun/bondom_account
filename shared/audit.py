"""Admin activity log.

Every privileged action that destroys data or moves money is recorded
here. Writes are best-effort and must never block the action itself — an
audit failure should not stop an admin from doing their job — but they
are written in their own transaction so a rolled-back action does not
silently drop its log entry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.database import AsyncSessionLocal
from shared.models import AdminAuditLog

logger = logging.getLogger(__name__)

# Action names used across the panel. Kept as constants so the Activity
# tab can filter on them without matching free text.
ACTION_USER_DELETE = "user.delete"
ACTION_USER_SOFT_DELETE = "user.deactivate"
ACTION_USER_RESTORE = "user.restore"
ACTION_USER_BLOCK = "user.block"
ACTION_USER_UNBLOCK = "user.unblock"
ACTION_USER_SUSPEND = "user.suspend"
ACTION_USER_ACTIVATE = "user.activate"
ACTION_WALLET_CREDIT = "wallet.credit"
ACTION_WALLET_DEBIT = "wallet.debit"
ACTION_TOPUP_OVERRIDE = "user.topup_override"
ACTION_STOCK_CLEAR = "stock.clear"
ACTION_PASSWORD_CHANGE = "admin.password_change"
ACTION_LOGIN_FAILED = "admin.login_failed"
ACTION_API_KEY_REVOKE = "api_key.revoke"


@dataclass(frozen=True)
class AuditEntry:
    """Flattened row for the admin UI (state vars must be serialisable)."""

    id: int
    actor: str
    action: str
    target: str
    summary: str
    reason: str
    created_at: str


async def log_action(
    action: str,
    *,
    actor: str = "admin",
    target_type: str = "",
    target_id: str | int = "",
    summary: str = "",
    reason: str = "",
) -> None:
    """Append an entry. Never raises — auditing must not break the action."""
    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                session.add(
                    AdminAuditLog(
                        actor=actor,
                        action=action,
                        target_type=target_type,
                        target_id=str(target_id),
                        summary=summary[:2000],
                        reason=reason[:2000],
                    )
                )
    except Exception:
        logger.exception("Failed to write audit entry for %s", action)


def _fmt(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def list_entries(
    session: AsyncSession, limit: int = 300, action: str = ""
) -> list[AuditEntry]:
    """Most recent entries first."""
    stmt = select(AdminAuditLog).order_by(AdminAuditLog.id.desc()).limit(limit)
    if action:
        stmt = stmt.where(AdminAuditLog.action == action)
    rows = (await session.scalars(stmt)).all()
    return [
        AuditEntry(
            id=r.id,
            actor=r.actor,
            action=r.action,
            target=f"{r.target_type} #{r.target_id}".strip() if r.target_type else "",
            summary=r.summary,
            reason=r.reason,
            created_at=_fmt(r.created_at),
        )
        for r in rows
    ]
