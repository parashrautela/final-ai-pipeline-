from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

# pyrefly: ignore [missing-import]
from postgrest.exceptions import APIError
from supabase import Client, create_client

from app.config import settings
from app.logging import logger
# Module-level singleton — creating a Supabase client is expensive, so we
# do it once on first use rather than on every request.
_client: Optional[Client] = None
_TABLE: str = settings.DB_TABLE_NAME

# These PostgREST error codes indicate a schema/config problem that won't
# fix itself on retry, so we let them propagate immediately.
_FATAL_PGRST_CODES = {"PGRST205", "PGRST200", "PGRST106"}


def get_supabase() -> Client:
    """Return singleton Supabase client, creating on first use."""
    global _client
    if _client is not None:
        return _client
    try:
        _client = create_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_SERVICE_ROLE_KEY,
        )
        return _client
    except TypeError as exc:
        logger.error("Supabase client init failed (TypeError)", exc_info=exc)
        raise RuntimeError(
            "Supabase client initialization failed. "
            "Ensure httpx>=0.28.0 is installed — gotrue>=2.9 uses the proxy= kwarg."
        ) from exc
    except Exception as exc:
        logger.error("Supabase client init failed", exc_info=exc)
        raise


async def create_product(title: str = "", jewellery_type: str = "") -> dict:
    """Insert a new product row and return it."""
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "title": title or "",
        "jewellery_type": jewellery_type or None,
    }
    try:
        resp = get_supabase().table(_TABLE).insert(payload).execute()
        product = resp.data[0]
        logger.info("Created product", extra={"product_id": product["id"]})
        return product
    except Exception as exc:
        logger.error("create_product failed", exc_info=exc)
        raise
async def fetch_pending_job() -> Optional[dict]:
    """Atomically claim one pending job using optimistic locking."""
    try:
        # Select the oldest pending job first ...
        select_resp = (
            get_supabase()
            .table(_TABLE)
            .select("id")
            .eq("status", "pending")
            .order("created_at", desc=False)
            .limit(1)
            .execute()
        )
        if not select_resp.data:
            return None

        job_id = select_resp.data[0]["id"]
        now = datetime.now(timezone.utc).isoformat()

        # ... then claim it by flipping status only if it's still 'pending'.
        # If another worker grabbed it between select and update, we get 0 rows
        # back and simply return None without erroring out.
        update_resp = (
            get_supabase()
            .table(_TABLE)
            .update({"status": "processing", "processing_started_at": now})
            .eq("id", job_id)
            .eq("status", "pending")
            .execute()
        )
        if not update_resp.data:
            return None

        logger.info("Claimed job", extra={"job_id": job_id})
        return update_resp.data[0]

    except APIError as exc:
        if exc.code in _FATAL_PGRST_CODES:
            raise
        logger.error("fetch_pending_job API error", exc_info=exc)
        return None
    except Exception as exc:
        logger.error("fetch_pending_job failed", exc_info=exc)
        return None


async def fetch_job_by_id(job_id: str) -> Optional[dict]:
    """Return a job row by primary key, or None if not found."""
    try:
        resp = (
            get_supabase()
            .table(_TABLE)
            .select("*")
            .eq("id", job_id)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None
    except Exception as exc:
        logger.error("fetch_job_by_id failed", extra={"job_id": job_id}, exc_info=exc)
        return None


async def reset_stale_jobs(timeout_seconds: int) -> int:
    """Reset jobs stuck in 'processing' beyond timeout back to 'pending'."""
    # A job gets stuck if the server crashed mid-pipeline. Without this,
    # the product would stay in 'processing' forever and never show images.
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat()
        resp = (
            get_supabase()
            .table(_TABLE)
            .update({"status": "pending", "processing_started_at": None})
            .eq("status", "processing")
            .lt("processing_started_at", cutoff)
            .execute()
        )
        count = len(resp.data) if resp.data else 0
        if count:
            logger.warning(f"Reset {count} stale job(s) back to pending")
        return count
    except APIError as exc:
        if exc.code in _FATAL_PGRST_CODES:
            raise
        logger.error("reset_stale_jobs failed", exc_info=exc)
        return 0
    except Exception as exc:
        logger.error("reset_stale_jobs failed", exc_info=exc)
        return 0


async def update_job_status(
    job_id: str,
    status: str,
    *,
    processed_url: Optional[str] = None,
    error_message: Optional[str] = None,
    processing_time_ms: Optional[int] = None,
) -> None:
    """Persist a status transition for a job."""
    payload: dict = {
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if processed_url is not None:
        payload["processed_url"] = processed_url
    if error_message is not None:
        payload["error_message"] = error_message
    if processing_time_ms is not None:
        payload["processing_time_ms"] = processing_time_ms

    try:
        get_supabase().table(_TABLE).update(payload).eq("id", job_id).execute()
        logger.info(f"Job status → {status}", extra={"job_id": job_id, "status": status})
    except Exception as exc:
        logger.error("update_job_status failed", extra={"job_id": job_id, "status": status}, exc_info=exc)
        raise


async def update_product_image_url(product_id: str, processed_url: str) -> None:
    """Write the processed image URL to the product row."""
    try:
        get_supabase().table(_TABLE).update({"image_url": processed_url}).eq("id", product_id).execute()
        logger.info("Product image_url updated", extra={"product_id": product_id, "processed_url": processed_url})
    except Exception as exc:
        logger.error("update_product_image_url failed", extra={"product_id": product_id}, exc_info=exc)
        raise


async def update_product_generated_images(
    product_id: str,
    generated_urls: list[str],
    *,
    update_image_url: bool = True,
) -> None:
    """Persist the generated image variant URLs for a product."""
    if not generated_urls:
        logger.warning("update_product_generated_images called with empty list — skipping", extra={"product_id": product_id})
        return

    payload: dict = {"generated_image_urls": generated_urls}
    if update_image_url:
        payload["image_url"] = generated_urls[0]

    try:
        resp = get_supabase().table(_TABLE).update(payload).eq("id", product_id).execute()
        logger.info(
            f"Stored {len(generated_urls)} generated image URL(s)",
            extra={"product_id": product_id, "variant_count": len(generated_urls)},
        )
    except Exception as exc:
        logger.error("update_product_generated_images failed", extra={"product_id": product_id}, exc_info=exc)
        raise


# ---------------------------------------------------------------------------
# Chamak Generations repository
# ---------------------------------------------------------------------------


async def fetch_chamak_generation(generation_id: str) -> Optional[dict]:
    """Return a chamak_generations row by primary key (id), or None if not found."""
    try:
        resp = (
            get_supabase()
            .table(settings.CHAMAK_TABLE_NAME)
            .select("*")
            .eq("id", generation_id)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None
    except Exception as exc:
        logger.error(
            "fetch_chamak_generation failed",
            extra={"generation_id": generation_id},
            exc_info=exc,
        )
        return None


async def update_chamak_generation(generation_id: str, updates: dict) -> Optional[dict]:
    """Apply updates to a chamak_generations row and return updated record."""
    try:
        resp = (
            get_supabase()
            .table(settings.CHAMAK_TABLE_NAME)
            .update(updates)
            .eq("id", generation_id)
            .execute()
        )
        if resp.data:
            logger.info(
                "Updated chamak_generation",
                extra={"generation_id": generation_id, "updated_fields": list(updates.keys())},
            )
            return resp.data[0]
        return None
    except Exception as exc:
        logger.error(
            "update_chamak_generation failed",
            extra={"generation_id": generation_id, "updates": updates},
            exc_info=exc,
        )
        raise

