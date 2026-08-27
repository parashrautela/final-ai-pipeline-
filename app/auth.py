"""Caller identity for credit-bearing endpoints.

Until now every route here was open HTTP: the body carried a `generation_id`
and the service acted on it, so anyone who watched the app's traffic once could
drive the pipeline for free and — worse, once credits exist — spend somebody
else's balance. Money cannot sit behind that.

Two verification paths, because Supabase projects sign JWTs differently
depending on when they were created and whether they have migrated to
asymmetric keys:

  * `SUPABASE_JWT_SECRET` set  → verify HS256 locally. No network hop, and the
    pipeline keeps working even if Supabase Auth is briefly unreachable.
  * otherwise                  → ask Supabase to resolve the token. Correct for
    any signing algorithm, and honours revocation, at the cost of one call.

Both fail closed. An unverifiable token is a 401, never an anonymous pass.
"""

from __future__ import annotations

from typing import Optional

# pyrefly: ignore [missing-import]
import jwt
from fastapi import Header, HTTPException

from app.config import settings
from app.db.repository import get_supabase
from app.logging import logger


class AuthError(Exception):
    """Raised when a bearer token cannot be resolved to a real user."""


def _strip_bearer(header_value: str) -> str:
    parts = header_value.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    # Tolerate a bare token: some HTTP clients drop the scheme.
    return header_value.strip()


def _verify_locally(token: str) -> str:
    """Verify a Supabase HS256 JWT against the project secret."""
    try:
        claims = jwt.decode(
            token,
            settings.SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            # Supabase stamps every end-user token with this audience. Checking
            # it stops a service-role key being replayed as a user token.
            audience="authenticated",
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Session expired. Please sign in again.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("Invalid session token.") from exc

    user_id = claims.get("sub")
    if not user_id:
        raise AuthError("Session token carries no user id.")
    return str(user_id)


async def _verify_remotely(token: str) -> str:
    """Ask Supabase to resolve the token. Works for any signing algorithm."""
    try:
        response = get_supabase().auth.get_user(token)
    except Exception as exc:
        raise AuthError("Could not verify your session.") from exc

    user = getattr(response, "user", None)
    if user is None or not getattr(user, "id", None):
        raise AuthError("Invalid session token.")
    return str(user.id)


async def resolve_user_id(authorization: Optional[str]) -> Optional[str]:
    """Return the caller's user id, or raise HTTPException(401).

    Returns None only in the one safe case: metering is switched off AND no
    token was sent. That is what lets the backend deploy BEFORE the iOS and web
    clients learn to send their token — without it, shipping this would 401
    every live app the moment it went out.

    A token that IS sent is always verified, whether metering is on or not: a
    bad token is never silently downgraded to anonymous.

    Once CREDITS_ENABLED is true a token is mandatory, because there is no
    honest way to debit a wallet you cannot identify.
    """
    if not authorization:
        if not settings.CREDITS_ENABLED:
            logger.warning(
                "Unauthenticated call allowed because CREDITS_ENABLED is false. "
                "This endpoint is still open — turn metering on once the clients ship."
            )
            return None
        raise HTTPException(
            status_code=401,
            detail="Sign-in required. This action spends credits.",
        )

    token = _strip_bearer(authorization)
    try:
        if settings.SUPABASE_JWT_SECRET:
            return _verify_locally(token)
        return await _verify_remotely(token)
    except AuthError as exc:
        # Deliberately terse: never tell an unauthenticated caller *why* the
        # token failed beyond what they need to recover.
        logger.warning("Rejected an unverifiable bearer token: %s", exc)
        raise HTTPException(status_code=401, detail=str(exc)) from exc


async def require_user(authorization: Optional[str] = Header(default=None)) -> Optional[str]:
    """FastAPI dependency. Yields the caller's Supabase user id, or None while
    metering is off and the clients have not yet been updated."""
    return await resolve_user_id(authorization)


def require_ownership(row: dict, user_id: Optional[str], *, field: str = "wholesaler_id") -> None:
    """Refuse to act on somebody else's row.

    Separate from authentication on purpose: a valid token proves *who* you
    are, not that the `generation_id` in the body belongs to you. Without this
    check any signed-in wholesaler could burn another wholesaler's credits by
    passing their generation id.
    """
    if user_id is None:
        # Pre-rollout only: nobody was identified, so there is nothing to
        # compare against. Unreachable once CREDITS_ENABLED is true, because
        # resolve_user_id refuses to return None in that case.
        return

    owner = row.get(field)
    if not owner or str(owner).lower() != str(user_id).lower():
        # 404 rather than 403: do not confirm that the id exists.
        raise HTTPException(status_code=404, detail="Not found.")
