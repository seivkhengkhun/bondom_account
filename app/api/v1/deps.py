"""Authentication, rate limiting and error handling for the public API.

Every response — success or failure — uses one shape, so a client can
parse errors without special-casing. Errors are:

    {"error": {"code": "invalid_api_key", "message": "...",
               "status": 401, "request_id": "..."}}
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from shared import api_keys
from shared.database import get_db
from shared.models import ApiKey, User

SessionDep = Annotated[AsyncSession, Depends(get_db)]

API_VERSION = "v1"


class ApiError(Exception):
    """Raised anywhere in the API to produce the standard error envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        **extra,
    ) -> None:
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra
        super().__init__(message)


def error_response(exc: ApiError, request_id: str = "") -> JSONResponse:
    body = {
        "error": {
            "code": exc.code,
            "message": exc.message,
            "status": exc.status,
        }
    }
    if request_id:
        body["error"]["request_id"] = request_id
    body["error"].update(exc.extra)
    return JSONResponse(status_code=exc.status, content=body)


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #
@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_in: int


class SlidingWindowLimiter:
    """In-process sliding-window limiter, keyed per API key.

    Deliberately in-memory: the app runs as a single process, so this
    needs no extra infrastructure. It resets on restart and would not
    hold across multiple workers — if this is ever scaled out, move the
    window to Redis and keep this interface.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window: float = 60.0) -> RateLimitResult:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] >= window:
            hits.popleft()

        if len(hits) >= limit:
            reset_in = int(window - (now - hits[0])) + 1
            return RateLimitResult(False, limit, 0, reset_in)

        hits.append(now)
        return RateLimitResult(True, limit, limit - len(hits), int(window))


limiter = SlidingWindowLimiter()


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
@dataclass
class ApiCaller:
    """The authenticated owner of an API key."""

    key: ApiKey
    user: User

    @property
    def scopes(self) -> set[str]:
        return {s for s in (self.key.scopes or "").split(",") if s}

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise ApiError(
                "insufficient_scope",
                f"This API key does not have the '{scope}' scope.",
                status=403,
                required_scope=scope,
                granted_scopes=sorted(self.scopes),
            )


def _extract_key(authorization: str | None, x_api_key: str | None) -> str:
    """Accept either ``Authorization: Bearer <key>`` or ``X-API-Key``."""
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return authorization.strip()
    return (x_api_key or "").strip()


async def get_caller(
    request: Request,
    db: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> ApiCaller:
    """Authenticate the request and apply the key's rate limit."""
    plaintext = _extract_key(authorization, x_api_key)
    if not plaintext:
        raise ApiError(
            "missing_api_key",
            "Provide your key as 'Authorization: Bearer <key>' or 'X-API-Key'.",
            status=401,
        )

    key = await api_keys.resolve_key(db, plaintext)
    if key is None:
        raise ApiError(
            "invalid_api_key",
            "This API key is invalid or has been revoked.",
            status=401,
        )

    result = limiter.check(f"key:{key.id}", key.rate_limit_per_min or 60)
    request.state.rate_limit = result
    if not result.allowed:
        raise ApiError(
            "rate_limit_exceeded",
            (
                f"Rate limit of {result.limit} requests/minute exceeded. "
                f"Retry in {result.reset_in}s."
            ),
            status=429,
            retry_after=result.reset_in,
        )

    user = await db.get(User, key.user_id)
    if user is None:
        raise ApiError(
            "account_unavailable", "The account for this key no longer exists.", 401
        )
    if not user.is_active:
        raise ApiError(
            "account_suspended", "This account is suspended.", status=403
        )

    request.state.api_key_id = key.id
    request.state.api_user_id = user.id
    return ApiCaller(key=key, user=user)


CallerDep = Annotated[ApiCaller, Depends(get_caller)]


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]
