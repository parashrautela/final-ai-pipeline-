"""Per-Wholesaler Feature Controls Enforcement for FastAPI.

Provides dependency factory `require_feature(key)` and background worker helper
`is_feature_blocked(user_id, key)` that query `public.feature_blocked(p_user, p_key)`.
Includes a 20-second in-process cache per (user_id, key).
"""

from __future__ import annotations

import time
from typing import Callable, Dict, Optional, Tuple

from fastapi import Depends, HTTPException

from app.auth import require_user
from app.db.repository import get_supabase
from app.logging import logger

# In-process cache: (user_id, key) -> (cached_at_epoch, is_blocked)
_CACHE: Dict[Tuple[str, str], Tuple[float, bool]] = {}
CACHE_TTL_SECONDS = 20.0


def clear_features_cache() -> None:
    """Clear in-process feature flags cache (primarily for tests)."""
    _CACHE.clear()


def is_feature_blocked(user_id: Optional[str], key: str) -> bool:
    """Check if a feature is blocked for a user, using 20s in-process cache.

    Calls `public.feature_blocked(p_user, p_key)`.
    If lookup fails due to transient error, fails open (returns False) and logs error.
    """
    if not user_id:
        return False

    cache_key = (user_id, key)
    now = time.monotonic()
    cached = _CACHE.get(cache_key)
    if cached and (now - cached[0] < CACHE_TTL_SECONDS):
        return cached[1]

    try:
        supabase = get_supabase()
        response = supabase.rpc("feature_blocked", {"p_user": user_id, "p_key": key}).execute()
        is_blocked = bool(response.data) if response and response.data is not None else False
        _CACHE[cache_key] = (now, is_blocked)
        return is_blocked
    except Exception as exc:
        logger.error(
            f"Feature control lookup failed for user={user_id}, key={key}: {exc}. Allowing action (fail-open).",
            exc_info=True,
        )
        return False


def require_feature(key: str) -> Callable:
    """FastAPI dependency factory enforcing that a feature is not blocked.

    Must run after caller identity is established (require_user).
    Raises HTTP 403 with FEATURE_DISABLED code if the feature is blocked.
    """
    async def _dependency(user_id: Optional[str] = Depends(require_user)) -> Optional[str]:
        if not user_id:
            # If user_id is None (metering off or client unauthenticated on legacy open routes), pass through
            return None

        if is_feature_blocked(user_id, key):
            logger.warning(f"Blocked request for user={user_id} on disabled feature={key}")
            raise HTTPException(
                status_code=403,
                detail={
                    "detail": "This feature is not enabled for your account.",
                    "code": "FEATURE_DISABLED",
                    "feature": key,
                },
            )
        return user_id

    return _dependency
