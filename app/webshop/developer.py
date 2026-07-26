"""Developer portal — API key management, usage and an API playground.

Reuses the storefront's Telegram session, so a developer is simply a
signed-in customer: their API purchases spend the same wallet balance
they top up on the website or in the bot.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from shared import api_keys
from shared.database import AsyncSessionLocal

from .auth import SESSION_COOKIE, check_csrf, read_session

router = APIRouter(include_in_schema=False)


def _base_url(request: Request) -> str:
    host = request.headers.get("host", "localhost")
    scheme = (
        "http"
        if host.startswith(("127.", "localhost", "0.0.0.0"))
        else "https"
    )
    return f"{scheme}://{host}"


@router.get("/developer", response_class=HTMLResponse)
async def developer_home(request: Request):
    from app.webshop.routes import _render, _session_user

    user = await _session_user(request)
    if user is None:
        return RedirectResponse("/?next=/developer")

    async with AsyncSessionLocal() as session:
        keys = await api_keys.list_keys(session, user.id)
        usage = await api_keys.usage_summary(session, user.id)
        logs = await api_keys.recent_requests(session, user.id, limit=50)

    return await _render(
        request,
        "developer.html",
        keys=keys,
        usage=usage,
        logs=logs,
        scopes=api_keys.SCOPES,
        max_keys=api_keys.MAX_KEYS_PER_USER,
        base_url=_base_url(request),
        # Shown exactly once, straight after creation.
        new_key=request.query_params.get("new_key", ""),
        error=request.query_params.get("error", ""),
        success=request.query_params.get("success", ""),
    )


@router.post("/developer/keys")
async def create_key(
    request: Request,
    name: str = Form(""),
    scope_read: str = Form(""),
    scope_orders: str = Form(""),
    scope_sms: str = Form(""),
    csrf_token: str = Form(""),
):
    from app.webshop.routes import _session_user

    user = await _session_user(request)
    if user is None:
        return RedirectResponse("/", status_code=303)
    if not check_csrf(read_session(request.cookies.get(SESSION_COOKIE)), csrf_token):
        return RedirectResponse(
            "/developer?error="
            + quote_plus("Your session expired. Please try again."),
            status_code=303,
        )

    async with AsyncSessionLocal() as session:
        if await api_keys.count_active_keys(session, user.id) >= api_keys.MAX_KEYS_PER_USER:
            return RedirectResponse(
                "/developer?error="
                + quote_plus(
                    f"You already have {api_keys.MAX_KEYS_PER_USER} active keys. "
                    "Revoke one before creating another."
                ),
                status_code=303,
            )

        chosen = [
            s
            for s, on in (
                ("read", scope_read),
                ("orders", scope_orders),
                ("sms", scope_sms),
            )
            if on
        ]
        issued = await api_keys.create_key(session, user.id, name, chosen)

    # The plaintext is passed back once via the URL and never stored.
    return RedirectResponse(
        f"/developer?new_key={quote_plus(issued.plaintext)}", status_code=303
    )


@router.post("/developer/keys/{key_id}/revoke")
async def revoke_key(
    request: Request, key_id: int, csrf_token: str = Form("")
):
    from app.webshop.routes import _session_user

    user = await _session_user(request)
    if user is None:
        return RedirectResponse("/", status_code=303)
    if not check_csrf(read_session(request.cookies.get(SESSION_COOKIE)), csrf_token):
        return RedirectResponse(
            "/developer?error="
            + quote_plus("Your session expired. Please try again."),
            status_code=303,
        )

    async with AsyncSessionLocal() as session:
        ok = await api_keys.revoke_key(session, user.id, key_id)

    msg = "Key revoked." if ok else "That key could not be revoked."
    param = "success" if ok else "error"
    return RedirectResponse(
        f"/developer?{param}={quote_plus(msg)}", status_code=303
    )


@router.get("/developer/docs", response_class=HTMLResponse)
async def developer_docs(request: Request):
    from app.webshop.routes import _render

    return await _render(
        request,
        "developer_docs.html",
        base_url=_base_url(request),
        scopes=api_keys.SCOPES,
        rate_limit=api_keys.DEFAULT_RATE_LIMIT_PER_MIN,
    )
