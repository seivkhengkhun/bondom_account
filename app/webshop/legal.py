"""Legal pages.

Content lives in Jinja templates under ``templates/legal/`` so it can be
edited without touching Python. Each document declares its own effective
date at the top of its template; add a new page by dropping a template in
and adding one line to ``DOCUMENTS``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from shared.config import settings

router = APIRouter(include_in_schema=False)

# slug -> (template, page title, short description for the index)
DOCUMENTS: dict[str, tuple[str, str, str]] = {
    "terms": (
        "legal/terms.html",
        "Terms of Service",
        "The rules for using the store, wallet and bot.",
    ),
    "privacy": (
        "legal/privacy.html",
        "Privacy Policy",
        "What we collect, why, how long we keep it, and your rights.",
    ),
    "cookies": (
        "legal/cookies.html",
        "Cookie Policy",
        "The cookies we set and what each one does.",
    ),
    "developer-terms": (
        "legal/developer_terms.html",
        "Developer & API Terms",
        "Conditions for using API keys and the public API.",
    ),
    "refunds": (
        "legal/refunds.html",
        "Refund Policy",
        "When purchases are refunded, replaced, or final.",
    ),
    "acceptable-use": (
        "legal/acceptable_use.html",
        "Acceptable Use Policy",
        "What you may not do with the platform.",
    ),
}

# Single place to change the contact address used across every document.
LEGAL_CONTACT = "khun_seivkheng@bkrt"


@router.get("/legal", response_class=HTMLResponse)
async def legal_index(request: Request):
    from app.webshop.routes import _render

    return await _render(
        request,
        "legal/index.html",
        documents=DOCUMENTS,
    )


@router.get("/legal/{slug}", response_class=HTMLResponse)
async def legal_document(request: Request, slug: str):
    from app.webshop.routes import _render, _get_bot_username

    entry = DOCUMENTS.get(slug)
    if entry is None:
        return RedirectResponse("/legal")

    template, title, _ = entry
    return await _render(
        request,
        template,
        doc_title=title,
        documents=DOCUMENTS,
        current_slug=slug,
        contact=LEGAL_CONTACT,
        support_bot=await _get_bot_username(),
        sms_enabled=settings.sms_enabled,
    )
