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


async def create_product(
    title: str = "",
    jewellery_type: str = "",
    wholesaler_id: Optional[str] = None,
) -> dict:
    """Insert a new product row and return it."""
    payload: dict = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "title": title or "",
        "jewellery_type": jewellery_type or None,
    }
    if wholesaler_id:
        payload["wholesaler_id"] = wholesaler_id

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
    except APIError as exc:
        # If optional columns (like error_message or processing_time_ms) don't exist in the table,
        # retry updating just status
        if exc.code == "PGRST204":
            logger.warning(
                f"update_job_status: '{_TABLE}' rejected one or more optional columns "
                f"(payload keys: {list(payload.keys())}) — retrying with status only",
                extra={"job_id": job_id, "status": status},
            )
            try:
                minimal_payload = {"status": status}
                get_supabase().table(_TABLE).update(minimal_payload).eq("id", job_id).execute()
                logger.info(f"Job status (minimal) → {status}", extra={"job_id": job_id})
                return
            except Exception as retry_exc:
                # This is the real failure if it happens: even 'status' isn't a column
                # on `_TABLE`, so the job can never be marked. Surface it — the original
                # error_message-missing exception below is a red herring once we're here.
                logger.error(
                    f"update_job_status: minimal retry ALSO failed — '{_TABLE}' may be "
                    f"missing the 'status' column, or job_id={job_id} doesn't exist",
                    extra={"job_id": job_id, "status": status},
                    exc_info=retry_exc,
                )
                return
        logger.error("update_job_status failed", extra={"job_id": job_id, "status": status}, exc_info=exc)
    except Exception as exc:
        logger.error("update_job_status failed", extra={"job_id": job_id, "status": status}, exc_info=exc)


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
    image_variants: Optional[dict[str, dict[str, str]]] = None,
) -> None:
    """Persist the generated image variant URLs for a product.

    `image_variants` maps each of those URLs to its card/detail/full copies —
    what lets the apps fetch a 41 KB thumbnail instead of the 3 MB original.
    The column arrives in migration 010; if it isn't there yet the update is
    retried without it, so an un-migrated database still works.
    """
    if not generated_urls:
        logger.warning("update_product_generated_images called with empty list — skipping", extra={"product_id": product_id})
        return

    payload: dict = {"generated_image_urls": generated_urls}
    if update_image_url:
        payload["image_url"] = generated_urls[0]
    if image_variants:
        payload["image_variants"] = image_variants

    try:
        try:
            get_supabase().table(_TABLE).update(payload).eq("id", product_id).execute()
        except APIError as exc:
            if exc.code != "PGRST204" or "image_variants" not in payload:
                raise
            logger.warning(
                f"'{_TABLE}' has no image_variants column yet (migration 010) — "
                "storing the URLs without it",
                extra={"product_id": product_id},
            )
            payload.pop("image_variants")
            get_supabase().table(_TABLE).update(payload).eq("id", product_id).execute()

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
    """Apply updates to a chamak_generations row and return updated record.

    `output_variants` (migration 010) is dropped and the update retried if the
    column isn't there: by this point the image is generated and the credits
    are spent, so a missing thumbnail column must not lose the result.
    """
    def _apply(payload: dict):
        return (
            get_supabase()
            .table(settings.CHAMAK_TABLE_NAME)
            .update(payload)
            .eq("id", generation_id)
            .execute()
        )

    try:
        try:
            resp = _apply(updates)
        except APIError as exc:
            if exc.code != "PGRST204" or not ({"output_variants", "output_images"} & updates.keys()):
                raise
            logger.warning(
                f"'{settings.CHAMAK_TABLE_NAME}' has no output_variants column yet "
                "(migration 010) — saving the generation without it",
                extra={"generation_id": generation_id},
            )
            resp = _apply({k: v for k, v in updates.items() if k not in {"output_variants", "output_images"}})
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


# ---------------------------------------------------------------------------
# Prompt Modules & AI Generation Logs repository
# ---------------------------------------------------------------------------


async def fetch_active_prompt_modules() -> list[dict]:
    """Fetch all currently active prompt modules (base and categories)."""
    try:
        resp = (
            get_supabase()
            .table("prompt_modules")
            .select("id, module_type, jewellery_type, prompt_text, version, is_active")
            .eq("is_active", True)
            .execute()
        )
        return resp.data or []
    except Exception as exc:
        logger.error("fetch_active_prompt_modules failed", exc_info=exc)
        return []


async def log_ai_generation_start(
    product_id: str,
    jewellery_type: str,
    composed_prompt: str,
    base_module_version: Optional[int] = None,
    category_module_version: Optional[int] = None,
    wholesaler_id: Optional[str] = None,
) -> Optional[str]:
    """Insert a new record in ai_generation_logs and return the generated log ID."""
    payload: dict = {
        "product_id": product_id,
        "jewellery_type": jewellery_type,
        "composed_prompt": composed_prompt,
        "base_module_version": base_module_version,
        "category_module_version": category_module_version,
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "trigger_source": "api",
    }
    if wholesaler_id:
        payload["wholesaler_id"] = wholesaler_id

    # Try inserting without hardcoded status first (let DB default handle it), or with standard statuses
    for status_candidate in [None, "generating", "queued", "pending", "started"]:
        try:
            p = dict(payload)
            if status_candidate:
                p["status"] = status_candidate
            resp = get_supabase().table("ai_generation_logs").insert(p).execute()
            if resp.data:
                log_id = resp.data[0]["id"]
                logger.info("AI generation logged", extra={"log_id": log_id, "product_id": product_id})
                return log_id
        except APIError as exc:
            if exc.code == "23514":  # check constraint violation, try next candidate
                continue
            logger.warning(f"log_ai_generation_start insert attempt failed: {exc}")
            break
        except Exception as exc:
            logger.warning(f"log_ai_generation_start insert attempt failed: {exc}")
            break

    return None


async def log_ai_generation_complete(
    log_id: str,
    status: str = "completed",
) -> None:
    """Update ai_generation_logs with completion timestamp and final status."""
    if not log_id:
        return
    payload = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    # Try updating with status candidates matching DB constraint
    for status_candidate in [status, "completed", "done", "success", "finished"]:
        try:
            p = dict(payload)
            p["status"] = status_candidate
            get_supabase().table("ai_generation_logs").update(p).eq("id", log_id).execute()
            logger.info(f"AI generation log updated → {status_candidate}", extra={"log_id": log_id})
            return
        except APIError as exc:
            if exc.code == "23514":  # check constraint violation
                continue
            break
        except Exception:
            break




# ─────────────────────────────────────────────────────────────────────────────
# Credits (Treasure Chest)
#
# Every one of these is a `SECURITY DEFINER` RPC that only `service_role` may
# execute — see migrations/004_credits_treasure_chest.sql. This module holds
# the only service-role key in the system, which is precisely why the debit
# belongs here and not in either client.
#
# Note what is NOT passed: a price. The RPC reads it from `credit_prices`
# inside the same transaction, so a tampered client cannot charge itself one
# credit for a ten-credit action.
# ─────────────────────────────────────────────────────────────────────────────


async def spend_credits(
    user_id: str,
    feature_key: str,
    idempotency_key: str,
    reference_type: Optional[str] = None,
    reference_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """Debit a wholesaler's wallet. Returns the RPC's result dict.

    Success:      {"ok": True,  "charged": 10, "balance": 90,
                   "ledger_id": "<debit row>", "created_at": "..."}
    Out of funds: {"ok": False, "error": "INSUFFICIENT_CREDITS",
                   "required": 10, "balance": 4, "short_by": 6}
    Replay:       {"ok": True,  "replayed": True, "ledger_id": ...,
                   "created_at": ..., "refunded": bool, "superseded": bool}
                  — only for the same wallet AND the same reference
                  (migration 006). Anything else is
                  {"ok": False, "error": "IDEMPOTENCY_CONFLICT"}.

    A ledger that predates migration 006 omits ledger_id/created_at on success
    and refunded/superseded on replay; callers treat the absence as "unknown"
    and take the safe branch.

    A raised exception here means the ledger is unreachable, which must NOT be
    treated as "free" — callers fail closed.
    """
    resp = get_supabase().rpc(
        "spend_credits",
        {
            "p_user": user_id,
            "p_feature_key": feature_key,
            "p_idempotency_key": idempotency_key,
            "p_reference_type": reference_type,
            "p_reference_id": reference_id,
            "p_metadata": metadata or {},
        },
    ).execute()
    return resp.data or {"ok": False, "error": "NO_RESPONSE"}


async def refund_credits(
    reference_type: str,
    reference_id: str,
    reason: Optional[str] = None,
) -> dict:
    """Give back the LATEST debit on a reference.

    Safe to call unconditionally: if nothing was ever debited for this
    reference (free feature, or it failed before the debit landed) the RPC
    returns `refunded: 0` rather than erroring. Idempotent per debit, so a
    retried failure handler cannot pay out twice.

    Prefer `refund_debit` whenever the charge's ledger id is known: "latest
    debit on this generation" is only the right debit if nothing else has
    been charged on it since.
    """
    resp = get_supabase().rpc(
        "refund_credits",
        {
            "p_reference_type": reference_type,
            "p_reference_id": reference_id,
            "p_reason": reason,
        },
    ).execute()
    return resp.data or {"ok": False, "error": "NO_RESPONSE"}


async def refund_debit(ledger_id: str, reason: Optional[str] = None) -> dict:
    """Give back exactly one debit — the one whose work failed.

    Keyed on the debit row (migration 006), so it is idempotent per debit and
    can never hand back a different, successful charge on the same generation.
    """
    resp = get_supabase().rpc(
        "refund_debit",
        {"p_debit_id": ledger_id, "p_reason": reason},
    ).execute()
    return resp.data or {"ok": False, "error": "NO_RESPONSE"}


async def count_prior_debits(reference_type: str, reference_id: str) -> int:
    """How many charges on this reference the wholesaler actually paid for.

    This is what distinguishes a first generation from a re-roll. The iOS
    `regenerate` path reuses the SAME `chamak_generations` row, so the row id
    alone cannot tell them apart — but the ledger can, and unlike anything the
    client sends, it cannot be spoofed into claiming the cheaper price.

    A refunded debit does not count: its work failed and the credits went
    back, so the next attempt is still the first one that was paid for and is
    priced as a first generation. Counting it made a failed first run's retry
    a re-roll, undercharging it.
    """
    try:
        resp = (
            get_supabase()
            .table("credit_ledger")
            .select("id, kind, idempotency_key, metadata")
            .eq("reference_type", reference_type)
            .eq("reference_id", reference_id)
            .execute()
        )
    except Exception as exc:
        # Fail toward the CHEAPER price. Miscounting must never let us
        # overcharge somebody for a first-time generation.
        logger.warning(f"count_prior_debits failed for {reference_id}: {exc}")
        return 0

    rows = resp.data or []
    refunded: set[str] = set()
    for row in rows:
        if row.get("kind") not in ("refund", "grant"):
            continue
        # Every refund, before and after migration 006, names the debit it
        # reversed in metadata.refund_of; 006 refunds are also keyed
        # 'refund:<debit id>'.
        refund_of = (row.get("metadata") or {}).get("refund_of")
        if refund_of:
            refunded.add(str(refund_of))
        key = row.get("idempotency_key") or ""
        if key.startswith("refund:"):
            refunded.add(key.removeprefix("refund:"))

    return sum(
        1 for row in rows
        if row.get("kind") == "debit" and str(row.get("id")) not in refunded
    )
