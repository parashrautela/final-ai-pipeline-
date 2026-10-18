"""Per-Wholesaler Feature Controls Enforcement for FastAPI.

Provides dependency factory `require_feature(key)` and background worker helper
`get_feature_status(user_id, key)` that query `public.feature_check(p_user, p_key)`.
Includes a 20-second in-process cache per (user_id, key) storing (cached_at, is_blocked, locked_message).
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Dict, Optional, Tuple

from fastapi import Depends, HTTPException

from app.auth import require_user
from app.db.repository import get_supabase
from app.logging import logger

# In-process cache: (user_id, key) -> (cached_at_epoch, is_blocked, locked_message)
_CACHE: Dict[Tuple[str, str], Tuple[float, bool, str]] = {}
CACHE_TTL_SECONDS = 20.0
DEFAULT_LOCKED_MESSAGE = "This feature has been locked for your account by Jewel India admin. Contact support to unlock it."


def clear_features_cache() -> None:
    """Clear in-process feature flags cache (primarily for tests)."""
    _CACHE.clear()


def get_feature_status(user_id: Optional[str], key: str) -> Tuple[bool, str]:
    """Check if a feature is blocked and get its locked_message, using 20s in-process cache.

    Calls `public.feature_check(p_user, p_key)` which returns `{"blocked": bool, "message": str}`.
    If lookup fails due to transient error, fails open (returns False, "") and logs error.
    """
    if not user_id:
        return False, ""

    cache_key = (user_id, key)
    now = time.monotonic()
    cached = _CACHE.get(cache_key)
    if cached and (now - cached[0] < CACHE_TTL_SECONDS):
        return cached[1], cached[2]

    try:
        supabase = get_supabase()
        response = supabase.rpc("feature_check", {"p_user": user_id, "p_key": key}).execute()
        data = response.data if response and response.data is not None else None
        if isinstance(data, dict):
            is_blocked = bool(data.get("blocked", False))
            msg = str(data.get("message") or DEFAULT_LOCKED_MESSAGE)
        elif isinstance(data, bool):
            is_blocked = data
            msg = DEFAULT_LOCKED_MESSAGE
        else:
            is_blocked = False
            msg = DEFAULT_LOCKED_MESSAGE

        _CACHE[cache_key] = (now, is_blocked, msg)
        return is_blocked, msg
    except Exception as exc:
        logger.error(
            f"Feature control lookup failed for user={user_id}, key={key}: {exc}. Allowing action (fail-open).",
            exc_info=True,
        )
        return False, DEFAULT_LOCKED_MESSAGE


def is_feature_blocked(user_id: Optional[str], key: str) -> bool:
    """Check if a feature is blocked for a user (convenience wrapper)."""
    is_blocked, _ = get_feature_status(user_id, key)
    return is_blocked


def require_feature(key: str) -> Callable:
    """FastAPI dependency factory enforcing that a feature is not blocked.

    Must run after caller identity is established (require_user).
    Raises HTTP 403 with plain string detail=<locked_message> and header X-Feature-Disabled: <key>.
    Runs lookup via asyncio.to_thread to avoid blocking the event loop on cache misses.
    """
    async def _dependency(user_id: Optional[str] = Depends(require_user)) -> Optional[str]:
        if not user_id:
            # If user_id is None (metering off or client unauthenticated on legacy open routes), pass through
            return None

        is_blocked, locked_msg = await asyncio.to_thread(get_feature_status, user_id, key)
        if is_blocked:
            logger.warning(f"Blocked request for user={user_id} on disabled feature={key}")
            raise HTTPException(
                status_code=403,
                detail=locked_msg,
                headers={"X-Feature-Disabled": key},
            )
        return user_id

    return _dependency
