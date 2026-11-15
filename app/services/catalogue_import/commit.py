"""app/services/catalogue_import/commit.py

Chunked, idempotent, resumable commit engine for catalogue import jobs.
Implements specifications from 01_ANTIGRAVITY_PLAN.md:
- Chunked writes (100 rows per batch).
- Resumable: if interrupted, picks up only uncommitted rows where product_id is NULL.
- Idempotent: re-running commit produces 0 changes.
- Never writes or overwrites is_published (preserves draft or published state).
- Never calls /process or any AI generation endpoints.
- Re-import photo safety: replaces raw_image_url only if ai_processing_state == 'none',
  otherwise appends to images.
- Updates or creates wholesaler import profile.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from typing import Any, Optional
import uuid

from app.config import settings
from app.db.repository import get_supabase
from app.logging import logger
from app.services.storage import public_url_for, upload_file_to_storage

CHUNK_SIZE = 100


class CommitError(Exception):
    """Raised when committing a catalogue import job fails."""
    pass


async def commit_catalogue_import_job(
    job_id: str,
    wholesaler_id: str,
    stored_photos: Optional[dict[str, tuple[bytes, str]]] = None,
) -> dict[str, Any]:
    """Execute chunked, idempotent commit of an import job into products table.

    stored_photos: optional mapping of photo filename -> (bytes, mime_type)
    Returns:
        dict: Final job summary
    """
    sb = get_supabase()

    # 1. Fetch job record
    job_res = sb.table("catalogue_import_jobs").select("*").eq("id", job_id).execute()
    if not job_res.data:
        raise CommitError(f"Job {job_id} not found.")

    job = job_res.data[0]
    if str(job["wholesaler_id"]) != str(wholesaler_id):
        raise CommitError("Wholesaler does not own this job.")

    if job["status"] == "done":
        logger.info(f"Job {job_id} is already committed. No-op.")
        return job.get("summary") or {}

    if job["status"] not in ("ready", "committing"):
        raise CommitError(f"Job cannot be committed in status '{job['status']}'. Must be 'ready'.")

    # Set status to committing
    sb.table("catalogue_import_jobs").update({"status": "committing"}).eq("id", job_id).execute()

    # 2. Fetch existing products for this wholesaler
    existing_res = sb.table("products").select(
        "id, product_code, import_snapshot, raw_image_url, images, ai_processing_state, is_published, extra_attributes"
    ).eq("wholesaler_id", wholesaler_id).execute()

    existing_by_code: dict[str, dict[str, Any]] = {}
    for p in existing_res.data or []:
        if p.get("product_code"):
            existing_by_code[p["product_code"].strip().upper()] = p

    # 3. Handle images-only upload (source_kind='images_only')
    if job["source_kind"] == "images_only":
        return await _commit_images_only_job(sb, job, wholesaler_id, existing_by_code, stored_photos)

    # 4. Standard import: fetch all rows for this job
    # Fetch in chunks of CHUNK_SIZE
    total_created = 0
    total_updated = 0
    total_unchanged = 0

    while True:
        # Fetch uncommitted rows (product_id is null and action in actionable list)
        uncommitted_res = sb.table("catalogue_import_rows").select("*").eq(
            "job_id", job_id
        ).in_(
            "action", ["create", "update", "conflict", "unchanged"]
        ).is_(
            "product_id", "null"
        ).order("id").limit(CHUNK_SIZE).execute()

        rows = uncommitted_res.data or []
        if not rows:
            break

        for row in rows:
            action = row.get("action")
            code = (row.get("product_code") or "").strip().upper()
            norm = row.get("normalized") or {}

            if action == "unchanged":
                # Link existing product_id to row
                existing = existing_by_code.get(code)
                if existing:
                    sb.table("catalogue_import_rows").update(
                        {"product_id": existing["id"]}
                    ).eq("id", row["id"]).execute()
                total_unchanged += 1
                continue

            if action == "create":
                # Create new product
                prod_data = _build_product_payload(
                    wholesaler_id=wholesaler_id,
                    job_id=job_id,
                    code=row.get("product_code"),
                    norm=norm,
                )

                # Process photo if present
                photo_refs = row.get("photo_refs") or []
                raw_url, extra_urls = _resolve_photo_urls(
                    wholesaler_id, job_id, photo_refs, stored_photos
                )
                if raw_url:
                    prod_data["raw_image_url"] = raw_url
                if extra_urls:
                    prod_data["images"] = extra_urls

                # Insert into products
                insert_res = sb.table("products").insert(prod_data).execute()
                if insert_res.data:
                    new_id = insert_res.data[0]["id"]
                    # Update row with product_id
                    sb.table("catalogue_import_rows").update(
                        {"product_id": new_id}
                    ).eq("id", row["id"]).execute()
                    # Cache in local map for subsequent rows
                    existing_by_code[code] = insert_res.data[0]
                    total_created += 1

            elif action in ("update", "conflict"):
                existing = existing_by_code.get(code)
                if not existing:
                    logger.warning(f"Expected existing product for code {code} during {action}, none found.")
                    continue

                resolution = row.get("resolution")
                if action == "conflict" and resolution == "mine":
                    # Wholesaler chose to keep app values:
                    # Update snapshot to incoming sheet value so future imports don't conflict again
                    existing_extra = existing.get("extra_attributes") or {}
                    incoming_extra = norm.get("extra_attributes") or {}
                    merged_extra = {**existing_extra, **incoming_extra} if incoming_extra else existing_extra
                    snapshot = dict(norm)
                    snapshot["extra_attributes"] = merged_extra

                    sb.table("products").update({
                        "import_snapshot": snapshot,
                        "import_job_id": job_id,
                    }).eq("id", existing["id"]).execute()
                    sb.table("catalogue_import_rows").update(
                        {"product_id": existing["id"]}
                    ).eq("id", row["id"]).execute()
                    total_unchanged += 1
                    continue

                # Action is update, or conflict resolved with "sheet"
                update_payload = _build_update_payload(
                    job_id=job_id,
                    norm=norm,
                    existing=existing,
                )

                # Photo update rules
                photo_refs = row.get("photo_refs") or []
                raw_url, extra_urls = _resolve_photo_urls(
                    wholesaler_id, job_id, photo_refs, stored_photos
                )
                if raw_url:
                    ai_state = existing.get("ai_processing_state") or "none"
                    if ai_state == "none":
                        update_payload["raw_image_url"] = raw_url
                    else:
                        # Product has already been AI-processed; append new photo to images
                        current_images = list(existing.get("images") or [])
                        if raw_url not in current_images:
                            current_images.append(raw_url)
                        update_payload["images"] = current_images

                if extra_urls:
                    current_images = list(update_payload.get("images") or existing.get("images") or [])
                    for eu in extra_urls:
                        if eu not in current_images:
                            current_images.append(eu)
                    update_payload["images"] = current_images

                # Update product in database
                sb.table("products").update(update_payload).eq("id", existing["id"]).execute()
                sb.table("catalogue_import_rows").update(
                    {"product_id": existing["id"]}
                ).eq("id", row["id"]).execute()
                total_updated += 1

    # 5. Mark job as done
    now_iso = datetime.now(timezone.utc).isoformat()
    sb.table("catalogue_import_jobs").update({
        "status": "done",
        "committed_at": now_iso,
    }).eq("id", job_id).execute()

    # 6. Save or update wholesaler import profile
    await _update_or_create_profile(sb, job, wholesaler_id)

    final_job = sb.table("catalogue_import_jobs").select("*").eq("id", job_id).execute().data[0]
    return final_job.get("summary") or {}


async def _commit_images_only_job(
    sb: Any,
    job: dict[str, Any],
    wholesaler_id: str,
    existing_by_code: dict[str, dict[str, Any]],
    stored_photos: Optional[dict[str, tuple[bytes, str]]],
) -> dict[str, Any]:
    """Commit photos from an images_only upload into existing products."""
    now_iso = datetime.now(timezone.utc).isoformat()
    photo_summary = job.get("photo_summary") or {}

    # Update existing products with photos
    if stored_photos:
        for filename, (img_bytes, mime) in stored_photos.items():
            clean_name = filename.split(".")[0].strip()
            # Find matching code
            matched_code = None
            is_extra = False

            for c in existing_by_code:
                if clean_name.upper() == c:
                    matched_code = c
                    break
                if clean_name.upper().startswith(c):
                    matched_code = c
                    is_extra = True
                    break

            if matched_code and matched_code in existing_by_code:
                target_prod = existing_by_code[matched_code]
                file_key = f"import_{job['id']}_{uuid.uuid4().hex[:8]}.jpg"
                storage_path = f"raw/{wholesaler_id}/{file_key}"
                pub_url = upload_file_to_storage(
                    img_bytes, bucket="plant-images", path=storage_path, content_type=mime
                )

                if not target_prod.get("raw_image_url") and not is_extra:
                    sb.table("products").update({
                        "raw_image_url": pub_url,
                        "import_job_id": job["id"],
                    }).eq("id", target_prod["id"]).execute()
                    target_prod["raw_image_url"] = pub_url
                else:
                    curr_images = list(target_prod.get("images") or [])
                    curr_images.append(pub_url)
                    sb.table("products").update({
                        "images": curr_images,
                        "import_job_id": job["id"],
                    }).eq("id", target_prod["id"]).execute()
                    target_prod["images"] = curr_images

    sb.table("catalogue_import_jobs").update({
        "status": "done",
        "committed_at": now_iso,
    }).eq("id", job["id"]).execute()

    return job.get("summary") or {}


def _build_product_payload(
    wholesaler_id: str,
    job_id: str,
    code: Optional[str],
    norm: dict[str, Any],
) -> dict[str, Any]:
    """Assemble products insert dictionary."""
    return {
        "wholesaler_id": wholesaler_id,
        "product_code": code,
        "import_job_id": job_id,
        "import_snapshot": norm,
        "extra_attributes": norm.get("extra_attributes") or {},
        "is_published": False,
        "ai_processing_state": "none",
        "title": norm.get("title") or "",
        "jewellery_type": norm.get("jewellery_type"),
        "category": norm.get("category"),
        "style": norm.get("style"),
        "size": norm.get("size"),
        "metal_purity": norm.get("metal_purity"),
        "gross_weight": norm.get("gross_weight"),
        "stone_weight": norm.get("stone_weight"),
        "net_weight": norm.get("net_weight"),
        "stock_available": norm.get("stock_available", False),
        "make_to_order_days": norm.get("make_to_order_days"),
    }


def _build_update_payload(
    job_id: str,
    norm: dict[str, Any],
    existing: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Assemble products update dictionary."""
    existing_extra = (existing.get("extra_attributes") or {}) if existing else {}
    incoming_extra = norm.get("extra_attributes") or {}
    merged_extra = {**existing_extra, **incoming_extra} if incoming_extra else existing_extra

    snapshot = dict(norm)
    snapshot["extra_attributes"] = merged_extra

    return {
        "import_job_id": job_id,
        "import_snapshot": snapshot,
        "extra_attributes": merged_extra,
        "title": norm.get("title") or (existing.get("title") if existing else "") or "",
        "jewellery_type": norm.get("jewellery_type"),
        "category": norm.get("category"),
        "style": norm.get("style"),
        "size": norm.get("size"),
        "metal_purity": norm.get("metal_purity"),
        "gross_weight": norm.get("gross_weight"),
        "stone_weight": norm.get("stone_weight"),
        "net_weight": norm.get("net_weight"),
        "stock_available": norm.get("stock_available", False),
        "make_to_order_days": norm.get("make_to_order_days"),
    }


def _resolve_photo_urls(
    wholesaler_id: str,
    job_id: str,
    photo_refs: list[str],
    stored_photos: Optional[dict[str, tuple[bytes, str]]],
) -> tuple[Optional[str], list[str]]:
    """Upload photos to plant-images storage bucket and return (raw_image_url, [images])."""
    if not photo_refs or not stored_photos:
        return None, []

    main_url: Optional[str] = None
    extra_urls: list[str] = []

    for idx, ref in enumerate(photo_refs):
        # ref can be filename or URL
        if ref in stored_photos:
            img_bytes, mime = stored_photos[ref]
            file_key = f"import_{job_id}_{uuid.uuid4().hex[:8]}.jpg"
            storage_path = f"raw/{wholesaler_id}/{file_key}"
            try:
                pub_url = upload_file_to_storage(
                    img_bytes, bucket="plant-images", path=storage_path, content_type=mime
                )
                if idx == 0:
                    main_url = pub_url
                else:
                    extra_urls.append(pub_url)
            except Exception as exc:
                logger.error(f"Failed to upload photo {ref} to plant-images: {exc}")

    return main_url, extra_urls


async def _update_or_create_profile(
    sb: Any,
    job: dict[str, Any],
    wholesaler_id: str,
) -> None:
    """Save or update wholesaler import profile upon successful commit, writing profile_id back to job."""
    now_iso = datetime.now(timezone.utc).isoformat()
    job_id = job["id"]

    profile_id = job.get("profile_id")
    if profile_id:
        sb.table("catalogue_import_profiles").update({
            "last_synced_at": now_iso,
            "updated_at": now_iso,
        }).eq("id", profile_id).execute()
        sb.table("catalogue_import_jobs").update({"profile_id": profile_id}).eq("id", job_id).execute()
        return

    # Check if a profile already exists for this wholesaler with the same gsheet_url or source
    gsheet_url = job.get("gsheet_url")
    if gsheet_url:
        existing = sb.table("catalogue_import_profiles").select("id").eq(
            "wholesaler_id", wholesaler_id
        ).eq("gsheet_url", gsheet_url).execute()
        if existing.data:
            p_id = existing.data[0]["id"]
            sb.table("catalogue_import_profiles").update({
                "last_synced_at": now_iso,
                "updated_at": now_iso,
            }).eq("id", p_id).execute()
            sb.table("catalogue_import_jobs").update({"profile_id": p_id}).eq("id", job_id).execute()
            return
