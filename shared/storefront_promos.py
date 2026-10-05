"""Admin-curated storefront promotion slots; no schema migration needed.

Three fixed slots live in the existing ``app_settings`` key/value table as
``storefront_promo:1`` … ``storefront_promo:3`` (JSON). A slot points at a
real product or a real category; it never carries its own price or stock.
At render time :func:`resolve` checks the target against the live catalog,
so a promotion for a product that was hidden, deleted or sold out simply
stops showing instead of advertising something that cannot be bought.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import quote

from sqlalchemy import select

from shared import bot_appearance
from shared.models import AppSetting
from shared.services import transaction_scope

logger = logging.getLogger(__name__)

PREFIX = "storefront_promo:"
SLOT_COUNT = 3
TARGET_KINDS = ("product", "category")
# Visual treatments the storefront knows how to draw. Names describe the
# surface, not a colour, so the stylesheet can evolve without migrations.
TONES = ("brand", "deep", "plain")

MAX_TITLE = 48
MAX_SUBTITLE = 110
MAX_CTA = 22


@dataclass(frozen=True)
class PromoSlot:
    slot: int
    enabled: bool = False
    title: str = ""
    subtitle: str = ""
    target_kind: str = "product"
    target: str = ""
    cta_label: str = ""
    tone: str = "brand"
    emoji_slot: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ResolvedPromo:
    """What a template needs; built only from live catalog data."""

    slot: int
    title: str
    subtitle: str
    href: str
    cta_label: str
    tone: str
    emoji_slot: str
    price: Decimal | None  # set for product promotions only
    meta: str  # e.g. "12 products" or the product's category


def _key(slot: int) -> str:
    return f"{PREFIX}{slot}"


def _clean(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def validate(slot: PromoSlot) -> PromoSlot:
    """Normalise a slot or raise ValueError with an admin-readable message."""
    if not 1 <= slot.slot <= SLOT_COUNT:
        raise ValueError("Unknown promotion slot.")
    if slot.target_kind not in TARGET_KINDS:
        raise ValueError("Target must be a product or a category.")
    if slot.tone not in TONES:
        raise ValueError("Unknown visual style.")
    if slot.emoji_slot and slot.emoji_slot not in bot_appearance.DEFAULTS:
        raise ValueError("Unknown emoji slot.")

    title = _clean(slot.title, MAX_TITLE)
    target = _clean(slot.target, 100)
    if slot.enabled:
        if not title:
            raise ValueError("A visible promotion needs a title.")
        if not target:
            raise ValueError("Choose the product or category to promote.")
    if slot.target_kind == "product" and target and not target.isdigit():
        raise ValueError("Product target must be a product ID.")

    return PromoSlot(
        slot=slot.slot,
        enabled=bool(slot.enabled),
        title=title,
        subtitle=_clean(slot.subtitle, MAX_SUBTITLE),
        target_kind=slot.target_kind,
        target=target,
        cta_label=_clean(slot.cta_label, MAX_CTA),
        tone=slot.tone,
        emoji_slot=slot.emoji_slot,
        updated_at=slot.updated_at,
    )


def _parse(slot: int, raw: str) -> PromoSlot:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Invalid promotion record")
    return validate(
        PromoSlot(
            slot=slot,
            enabled=data.get("enabled") is True,
            title=data.get("title", ""),
            subtitle=data.get("subtitle", ""),
            target_kind=data.get("target_kind", "product"),
            target=str(data.get("target", "")),
            cta_label=data.get("cta_label", ""),
            tone=data.get("tone", "brand"),
            emoji_slot=data.get("emoji_slot", ""),
            updated_at=data.get("updated_at", ""),
        )
    )


async def load_slots(session) -> list[PromoSlot]:
    """All slots in order; a missing or corrupt record reads as empty."""
    async with transaction_scope(session):
        rows = (
            await session.scalars(
                select(AppSetting).where(AppSetting.key.startswith(PREFIX))
            )
        ).all()
        stored = {row.key: row.value for row in rows}
    slots = []
    for n in range(1, SLOT_COUNT + 1):
        raw = stored.get(_key(n))
        if raw is None:
            slots.append(PromoSlot(slot=n))
            continue
        try:
            slots.append(_parse(n, raw))
        except (ValueError, TypeError, json.JSONDecodeError):
            bot_appearance.warn_once("invalid_storefront_promo")
            slots.append(PromoSlot(slot=n))
    return slots


async def save_slot(session, slot: PromoSlot) -> PromoSlot:
    clean = validate(slot)
    clean = PromoSlot(**{**asdict(clean), "updated_at": datetime.now(timezone.utc).isoformat()})
    value = json.dumps(asdict(clean), ensure_ascii=False)
    async with transaction_scope(session):
        row = await session.get(AppSetting, _key(clean.slot))
        if row is None:
            session.add(AppSetting(key=_key(clean.slot), value=value))
        else:
            row.value = value
    return clean


async def clear_slot(session, slot: int) -> None:
    if not 1 <= slot <= SLOT_COUNT:
        raise ValueError("Unknown promotion slot.")
    async with transaction_scope(session):
        row = await session.get(AppSetting, _key(slot))
        if row is not None:
            await session.delete(row)


def resolve(slots: list[PromoSlot], overviews: list) -> list[ResolvedPromo]:
    """Turn enabled slots into renderable promotions using live catalog data.

    ``overviews`` are the storefront's active ``ProductOverview`` rows. A
    product promotion needs its product active and in stock; a category
    promotion needs at least one active product in that category.
    """
    by_id = {o.product.id: o for o in overviews}
    per_category: dict[str, int] = {}
    for o in overviews:
        cat = o.product.category or "Other"
        per_category[cat] = per_category.get(cat, 0) + 1

    out: list[ResolvedPromo] = []
    for s in slots:
        if not s.enabled or not s.target:
            continue
        if s.target_kind == "product":
            o = by_id.get(int(s.target)) if s.target.isdigit() else None
            if o is None or o.available <= 0:
                continue
            out.append(
                ResolvedPromo(
                    slot=s.slot,
                    title=s.title,
                    subtitle=s.subtitle,
                    href=f"/p/{o.product.id}",
                    cta_label=s.cta_label or "View product",
                    tone=s.tone,
                    emoji_slot=s.emoji_slot,
                    price=o.product.price,
                    meta=o.product.category or "",
                )
            )
        else:
            count = per_category.get(s.target, 0)
            if count == 0:
                continue
            out.append(
                ResolvedPromo(
                    slot=s.slot,
                    title=s.title,
                    subtitle=s.subtitle,
                    href=f"/shop?cat={quote(s.target)}",
                    cta_label=s.cta_label or "Browse",
                    tone=s.tone,
                    emoji_slot=s.emoji_slot,
                    price=None,
                    meta=f"{count} product{'' if count == 1 else 's'}",
                )
            )
    return out
