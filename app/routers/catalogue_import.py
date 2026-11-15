"""app/routers/catalogue_import.py

FastAPI router for Jewel India Bulk Catalogue Import.
Endpoints defined in 01_ANTIGRAVITY_PLAN.md Section 3:
- POST /catalogue-import/jobs
- POST /catalogue-import/jobs/{id}/read
- POST /catalogue-import/jobs/{id}/analyze
- POST /catalogue-import/jobs/{id}/answers
- GET  /catalogue-import/jobs/{id}/preview
- POST /catalogue-import/jobs/{id}/resolutions
- POST /catalogue-import/jobs/{id}/commit
- GET  /catalogue-import/jobs/{id}
- GET  /catalogue-import/jobs/{id}/errors.csv
- POST /catalogue-import/jobs/{id}/cancel
- POST /catalogue-import/profiles/{id}/sync
"""

from __future__ import annotations

import asyncio
import csv
from datetime import datetime, timezone
import io
from typing import Any, Dict, List, Optional
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.auth import resolve_user_id
from app.db.repository import get_supabase
from app.logging import logger
from app.services.catalogue_import.commit import commit_catalogue_import_job
from app.services.catalogue_import.gsheet import GoogleSheetError, fetch_google_sheet_xlsx
from app.services.catalogue_import.mapper import analyze_import, find_best_matching_profile
from app.services.catalogue_import.merge import detect_intra_job_duplicates, merge_rows_with_existing
from app.services.catalogue_import.normalize import NormalizedRow, normalize_row
from app.services.catalogue_import.photos import (
    PhotoSummary,
    extract_photos_from_zip,
    match_photos_images_only,
    match_photos_to_rows,
    process_loose_files,
)
from app.services.catalogue_import.reader import (
    MAX_DATA_ROWS,
    MAX_SHEET_BYTES,
    MAX_ZIP_BYTES,
    RawRow,
    ReaderLimitExceeded,
    read_source_file,
)
from app.services.catalogue_import.safe_fetch import (
    StorageSecurityError,
    validate_wholesaler_storage_path,
)
from app.services.storage import download_from_storage

router = APIRouter(prefix="/catalogue-import", tags=["catalogue-import"])


async def require_user_id(authorization: Optional[str] = Header(default=None)) -> str:
    """Validate bearer token and return user ID, raising 401 on failure."""
    user_id = await resolve_user_id(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user_id


async def get_owned_job(job_id: str, user_id: str) -> dict[str, Any]:
    """Retrieve job and verify ownership."""
    sb = get_supabase()
    res = sb.table("catalogue_import_jobs").select("*").eq("id", job_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Catalogue import job not found.")
    job = res.data[0]
    if str(job["wholesaler_id"]) != str(user_id):
        raise HTTPException(status_code=403, detail="Forbidden. You do not own this job.")
    return job


# ---------------------------------------------------------------------------
# Request & Response Schemas
# ---------------------------------------------------------------------------

class CreateJobRequest(BaseModel):
    source_kind: str = Field(..., pattern="^(file|gsheet|images_only)$")
    source_files: list[dict[str, Any]] = Field(default_factory=list)
    gsheet_url: Optional[str] = None
    profile_id: Optional[str] = None


class AnswersRequest(BaseModel):
    answers: dict[str, Any] = Field(default_factory=dict)


class ResolutionsRequest(BaseModel):
    resolutions: Optional[dict[str, str]] = None
    all: Optional[str] = Field(default=None, pattern="^(sheet|mine)$")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/limits")
async def get_import_limits():
    """Return maximum upload and row limits for catalogue import (P4)."""
    return {
        "max_rows": MAX_DATA_ROWS,
        "max_sheet_bytes": MAX_SHEET_BYTES,
        "max_sheet_mb": 25,
        "max_zip_bytes": MAX_ZIP_BYTES,
        "max_zip_mb": 200,
    }


@router.post("/jobs")
async def create_import_job(
    req: CreateJobRequest,
    user_id: str = Depends(require_user_id),
):
    """Create a new catalogue import job in 'uploaded' status."""
    sb = get_supabase()

    # Validate storage paths to enforce tenant isolation and prevent directory traversal
    validated_files = []
    if req.source_files:
        for f in req.source_files:
            raw_path = f.get("path")
            try:
                norm_path = validate_wholesaler_storage_path(raw_path, user_id)
                validated_files.append({**f, "path": norm_path})
            except StorageSecurityError as exc:
                raise HTTPException(status_code=403, detail=str(exc))

    # Validate mapping profile ownership
    if req.profile_id:
        prof_res = (
            sb.table("catalogue_import_profiles")
            .select("id")
            .eq("id", req.profile_id)
            .eq("wholesaler_id", user_id)
            .execute()
        )
        if not prof_res.data:
            raise HTTPException(status_code=403, detail="Profile not found or access denied.")

    payload = {
        "wholesaler_id": user_id,
        "source_kind": req.source_kind,
        "source_files": validated_files if req.source_files is not None else None,
        "gsheet_url": req.gsheet_url,
        "profile_id": req.profile_id,
        "status": "uploaded",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    res = sb.table("catalogue_import_jobs").insert(payload).execute()
    if not res.data:
        raise HTTPException(status_code=500, detail="Failed to create import job.")

    job = res.data[0]
    return {"job_id": job["id"], "status": job["status"]}


@router.post("/jobs/{job_id}/read")
async def read_job(
    job_id: str,
    user_id: str = Depends(require_user_id),
):
    """Read source files and store raw rows."""
    job = await get_owned_job(job_id, user_id)
    sb = get_supabase()

    if job["status"] in ("committing", "done"):
        raise HTTPException(status_code=400, detail="Cannot re-read a job that is already committing or completed.")

    # Atomic claim: only one caller transitions the job from 'uploaded' or 'failed' to 'reading'
    claim_res = (
        sb.table("catalogue_import_jobs")
        .update({"status": "reading"})
        .eq("id", job_id)
        .in_("status", ["uploaded", "failed"])
        .execute()
    )

    if not claim_res.data:
        # Another caller is already reading this job or has already read it.
        # Do not touch the rows! Return current job status.
        fresh_res = sb.table("catalogue_import_jobs").select("*").eq("id", job_id).execute()
        cur_job = fresh_res.data[0] if fresh_res.data else job
        logger.info(
            f"Job {job_id} already claimed or processed (status: {cur_job.get('status')}). Skipping raw row ingestion."
        )
        return {
            "job_id": job_id,
            "status": cur_job.get("status", "reading"),
            "sheets": [],
            "row_count": 0,
            "already_claimed": True,
        }

    # Item 5: Delete any raw rows from an earlier read to ensure idempotency on re-read
    sb.table("catalogue_import_rows").delete().eq("job_id", job_id).execute()

    sheet_bytes = None
    sheet_name = "data.xlsx"

    try:
        if job["source_kind"] == "gsheet":
            sheet_bytes, sheet_name = await fetch_google_sheet_xlsx(job["gsheet_url"])
        elif job["source_kind"] in ("file", "images_only"):
            files = job.get("source_files") or []
            sheet_file = next(
                (f for f in files if f.get("name", "").lower().endswith((".xlsx", ".xls", ".ods", ".csv", ".tsv"))),
                None,
            )
            if sheet_file and job["source_kind"] == "file":
                try:
                    path = validate_wholesaler_storage_path(sheet_file["path"], user_id)
                except StorageSecurityError as exc:
                    raise HTTPException(status_code=403, detail=str(exc))
                sheet_bytes = await asyncio.to_thread(download_from_storage, "catalogue-imports", path)
                sheet_name = sheet_file["name"]

        # Parse rows if spreadsheet found
        read_res = None
        if sheet_bytes:
            read_res = await asyncio.to_thread(read_source_file, sheet_bytes, sheet_name)

            # Insert raw rows into catalogue_import_rows in batches of 100
            raw_rows_payload = []
            for r in read_res.rows:
                raw_rows_payload.append({
                    "job_id": job_id,
                    "sheet": r.sheet,
                    "row_number": r.row_number,
                    "raw": r.raw,
                    "messages": [],
                })
                if len(raw_rows_payload) >= 100:
                    sb.table("catalogue_import_rows").insert(raw_rows_payload).execute()
                    raw_rows_payload = []

            if raw_rows_payload:
                sb.table("catalogue_import_rows").insert(raw_rows_payload).execute()

        sb.table("catalogue_import_jobs").update({"status": "read"}).eq("id", job_id).execute()

        sheets = [s.name for s in read_res.sheets] if read_res else []
        row_count = len(read_res.rows) if read_res else 0
        _job_read_totals[job_id] = row_count

        return {
            "job_id": job_id,
            "status": "read",
            "sheets": sheets,
            "row_count": row_count,
        }

    except (GoogleSheetError, ReaderLimitExceeded) as exc:
        sb.table("catalogue_import_jobs").update({
            "status": "failed",
            "error": str(exc),
            "summary": {"create": 0},
        }).eq("id", job_id).execute()
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(f"Failed reading job {job_id}: {exc}", exc_info=exc)
        sb.table("catalogue_import_jobs").update({"status": "failed", "error": str(exc)}).eq("id", job_id).execute()
        raise HTTPException(status_code=500, detail=f"Failed to read source file: {exc}")


_analyze_locks: dict[str, asyncio.Lock] = {}
_commit_tasks: dict[str, asyncio.Task] = {}
_job_read_totals: dict[str, int] = {}


@router.post("/jobs/{job_id}/analyze")
async def analyze_job_endpoint(
    job_id: str,
    user_id: str = Depends(require_user_id),
):
    """Analyze headers and values, check profile, generate questions or build preview."""
    job = await get_owned_job(job_id, user_id)

    if job.get("status") in ("committing", "done"):
        return {
            "job_id": job_id,
            "status": job["status"],
            "summary": job.get("summary") or {},
            "photo_summary": job.get("photo_summary") or {},
            "questions": [],
        }

    if job.get("status") == "ready" and job.get("summary"):
        return {
            "job_id": job_id,
            "status": "ready",
            "summary": job.get("summary") or {},
            "photo_summary": job.get("photo_summary") or {},
            "questions": [],
        }

    lock = _analyze_locks.setdefault(job_id, asyncio.Lock())
    async with lock:
        fresh_job = await get_owned_job(job_id, user_id)
        if fresh_job.get("status") in ("committing", "done"):
            return {
                "job_id": job_id,
                "status": fresh_job["status"],
                "summary": fresh_job.get("summary") or {},
                "photo_summary": fresh_job.get("photo_summary") or {},
                "questions": [],
            }
        if fresh_job.get("status") == "ready" and fresh_job.get("summary"):
            return {
                "job_id": job_id,
                "status": "ready",
                "summary": fresh_job.get("summary") or {},
                "photo_summary": fresh_job.get("photo_summary") or {},
                "questions": [],
            }
        return await _run_analyze_pipeline(fresh_job, user_id)


@router.post("/jobs/{job_id}/answers")
async def submit_answers(
    job_id: str,
    req: AnswersRequest,
    user_id: str = Depends(require_user_id),
):
    """Submit answers to questions and re-analyze."""
    job = await get_owned_job(job_id, user_id)
    sb = get_supabase()

    current_answers = job.get("answers") or {}
    current_answers.update(req.answers)

    sb.table("catalogue_import_jobs").update({"answers": current_answers}).eq("id", job_id).execute()
    job["answers"] = current_answers

    return await _run_analyze_pipeline(job, user_id)


async def _run_analyze_pipeline(job: dict[str, Any], user_id: str) -> dict[str, Any]:
    """Execute mapping, normalization, 3-way merge, and photo matching."""
    job_id = job["id"]
    sb = get_supabase()

    # 1. Look for existing profile for this wholesaler
    profile = None
    if job.get("profile_id"):
        prof_res = (
            sb.table("catalogue_import_profiles")
            .select("*")
            .eq("id", job["profile_id"])
            .eq("wholesaler_id", user_id)
            .execute()
        )
        if prof_res.data:
            profile = prof_res.data[0]
        else:
            raise HTTPException(status_code=403, detail="Profile not found or access denied.")

    # 2. Re-read raw source to get RawSourceResult
    sheet_bytes = None
    sheet_name = "data.xlsx"
    if job["source_kind"] == "gsheet":
        sheet_bytes, sheet_name = await fetch_google_sheet_xlsx(job["gsheet_url"])
    elif job["source_kind"] == "file":
        files = job.get("source_files") or []
        sheet_file = next(
            (f for f in files if f.get("name", "").lower().endswith((".xlsx", ".xls", ".ods", ".csv", ".tsv"))),
            None,
        )
        if sheet_file:
            try:
                path = validate_wholesaler_storage_path(sheet_file["path"], user_id)
            except StorageSecurityError as exc:
                raise HTTPException(status_code=403, detail=str(exc))
            sheet_bytes = await asyncio.to_thread(download_from_storage, "catalogue-imports", path)
            sheet_name = sheet_file["name"]

    if not sheet_bytes and job["source_kind"] != "images_only":
        raise HTTPException(status_code=400, detail="No source spreadsheet file found.")

    read_res = None
    if sheet_bytes:
        read_res = await asyncio.to_thread(read_source_file, sheet_bytes, sheet_name)

    # 2.5 Auto-match existing profile by header fingerprint if no profile_id was provided
    headers: list[str] = []
    if read_res:
        for s in read_res.sheets:
            if s.is_product_sheet and not s.is_hidden:
                for h in s.headers:
                    if h and h not in headers:
                        headers.append(h)

    if profile is None and read_res and headers:
        prof_list_res = (
            sb.table("catalogue_import_profiles")
            .select("*")
            .eq("wholesaler_id", user_id)
            .execute()
        )
        matched_profile = find_best_matching_profile(headers, prof_list_res.data or [])
        if matched_profile:
            profile = matched_profile
            job["profile_id"] = matched_profile["id"]
            sb.table("catalogue_import_jobs").update({"profile_id": matched_profile["id"]}).eq("id", job_id).execute()

    # 3. Analyze mapping
    answers = job.get("answers") or {}
    mapped = await asyncio.to_thread(analyze_import, read_res, profile=profile, answers=answers)

    if not mapped.is_ready:
        questions_payload = [q.to_dict() if hasattr(q, "to_dict") else q for q in mapped.questions]
        # Needs answers
        sb.table("catalogue_import_jobs").update({
            "status": "needs_answers",
            "questions": questions_payload,
            "llm_usage": mapped.llm_usage,
        }).eq("id", job_id).execute()

        return {
            "job_id": job_id,
            "status": "needs_answers",
            "questions": questions_payload,
        }

    # If profile exists, keep it updated with confirmed mapping; otherwise create a new profile row
    now_iso = datetime.now(timezone.utc).isoformat()
    if job.get("profile_id"):
        sb.table("catalogue_import_profiles").update({
            "column_map": mapped.column_map,
            "value_maps": mapped.value_maps,
            "sheet_rules": mapped.sheet_rules,
            "updated_at": now_iso,
        }).eq("id", job["profile_id"]).execute()
    elif headers:
        prof_name = sheet_name.rsplit(".", 1)[0].replace("_", " ").title() if sheet_name else "Import Profile"
        new_prof_payload = {
            "wholesaler_id": user_id,
            "name": prof_name,
            "header_fingerprint": headers,
            "column_map": mapped.column_map,
            "value_maps": mapped.value_maps,
            "sheet_rules": mapped.sheet_rules,
            "gsheet_url": job.get("gsheet_url"),
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        ins_prof = sb.table("catalogue_import_profiles").insert(new_prof_payload).execute()
        if ins_prof.data:
            new_id = ins_prof.data[0]["id"]
            job["profile_id"] = new_id
            sb.table("catalogue_import_jobs").update({"profile_id": new_id}).eq("id", job_id).execute()

    # 4. Ready: Normalize rows
    norm_rows: list[NormalizedRow] = []
    if read_res:
        for raw_r in read_res.rows:
            norm = normalize_row(raw_r, mapped.column_map, mapped.value_maps)
            norm_rows.append(norm)

    detect_intra_job_duplicates(norm_rows)

    # 5. Extract & Match Photos
    photo_summary = PhotoSummary()
    # Check if a zip file or loose images were uploaded
    zip_bytes = None
    files = job.get("source_files") or []
    zip_file = next((f for f in files if f.get("name", "").lower().endswith(".zip")), None)
    if zip_file:
        try:
            zip_path = validate_wholesaler_storage_path(zip_file["path"], user_id)
        except StorageSecurityError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        zip_bytes = await asyncio.to_thread(download_from_storage, "catalogue-imports", zip_path)

    valid_photos = []
    if zip_bytes:
        valid_photos = await asyncio.to_thread(extract_photos_from_zip, zip_bytes, photo_summary)

    # 6. Fetch existing products for wholesaler
    existing_res = sb.table("products").select(
        "id, product_code, import_snapshot, raw_image_url, images, is_published, extra_attributes, title, jewellery_type, category, style, size, metal_purity, gross_weight, stone_weight, net_weight, stock_available, make_to_order_days, import_job_id"
    ).eq("wholesaler_id", user_id).execute()
    existing_products = existing_res.data or []

    if job["source_kind"] == "images_only":
        match_photos_images_only(existing_products, valid_photos, photo_summary)
        summary = {"create": 0, "update": 0, "unchanged": len(existing_products), "conflict": 0, "not_in_file": 0, "error": 0, "skipped": 0, "duplicate_skipped": 0}
    else:
        # Match photos to sheet rows
        match_photos_to_rows(norm_rows, valid_photos, photo_summary)

        # Item 6: Determine which existing products belong to the same import series/profile/sheet for not_in_file calculation
        profile_id = job.get("profile_id")
        gsheet_url = job.get("gsheet_url")
        relevant_codes: Optional[set[str]] = None

        if profile_id:
            p_jobs = sb.table("catalogue_import_jobs").select("id").eq("wholesaler_id", user_id).eq("profile_id", profile_id).execute()
            same_series_ids = {j["id"] for j in (p_jobs.data or []) if j.get("id")}
            relevant_codes = {
                p["product_code"].upper() for p in existing_products
                if p.get("product_code") and p.get("import_job_id") in same_series_ids
            }
        elif gsheet_url:
            g_jobs = sb.table("catalogue_import_jobs").select("id").eq("wholesaler_id", user_id).eq("gsheet_url", gsheet_url).execute()
            same_series_ids = {j["id"] for j in (g_jobs.data or []) if j.get("id")}
            relevant_codes = {
                p["product_code"].upper() for p in existing_products
                if p.get("product_code") and p.get("import_job_id") in same_series_ids
            }
        else:
            # Standalone file upload with no linked profile: products from other files are unrelated
            relevant_codes = set()

        # 3-Way merge
        summary = merge_rows_with_existing(norm_rows, existing_products, relevant_previous_codes=relevant_codes)

    # 7. Update catalogue_import_rows in DB
    for r in norm_rows:
        sb.table("catalogue_import_rows").update({
            "normalized": r.to_normalized_dict(),
            "product_code": r.product_code,
            "action": r.action,
            "changes": r.changes,
            "messages": r.messages,
            "photo_refs": [p.filename for p in ([r.photo_file] if r.photo_file else []) + r.extra_photos],
        }).eq("job_id", job_id).eq("sheet", r.sheet).eq("row_number", r.row_number).execute()

    sb.table("catalogue_import_jobs").update({
        "status": "ready",
        "questions": [],
        "summary": summary,
        "photo_summary": photo_summary.to_dict(),
        "llm_usage": mapped.llm_usage,
    }).eq("id", job_id).execute()

    return {
        "job_id": job_id,
        "status": "ready",
        "summary": summary,
        "photo_summary": photo_summary.to_dict(),
    }


@router.get("/jobs/{job_id}/preview")
async def get_preview(
    job_id: str,
    filter: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    user_id: str = Depends(require_user_id),
):
    """Return paginated preview rows and count chips."""
    job = await get_owned_job(job_id, user_id)
    sb = get_supabase()

    q = sb.table("catalogue_import_rows").select("*", count="exact").eq("job_id", job_id)

    if filter:
        f = filter.lower()
        if f in ("create", "update", "conflict", "unchanged", "error", "skipped", "duplicate_skipped"):
            q = q.eq("action", f)
        elif f == "no_photo":
            # Rows with no photo refs
            q = q.filter("photo_refs", "eq", "{}")

    offset = (page - 1) * page_size
    res = q.order("row_number").range(offset, offset + page_size - 1).execute()

    return {
        "job_id": job_id,
        "status": job["status"],
        "summary": job.get("summary") or {},
        "photo_summary": job.get("photo_summary") or {},
        "total_rows": res.count or 0,
        "page": page,
        "page_size": page_size,
        "rows": res.data or [],
    }


@router.post("/jobs/{job_id}/resolutions")
async def resolve_conflicts(
    job_id: str,
    req: ResolutionsRequest,
    user_id: str = Depends(require_user_id),
):
    """Set resolution ('sheet' or 'mine') for conflicting products."""
    await get_owned_job(job_id, user_id)
    sb = get_supabase()

    if req.all:
        sb.table("catalogue_import_rows").update({"resolution": req.all}).eq(
            "job_id", job_id
        ).eq("action", "conflict").execute()
    elif req.resolutions:
        for code, choice in req.resolutions.items():
            if choice in ("sheet", "mine"):
                sb.table("catalogue_import_rows").update({"resolution": choice}).eq(
                    "job_id", job_id
                ).eq("product_code", code).execute()

    return {"status": "ok", "message": "Resolutions updated."}


@router.post("/jobs/{job_id}/commit")
async def commit_job_endpoint(
    job_id: str,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(require_user_id),
):
    """Run chunked idempotent commit surviving client disconnections (P2)."""
    job = await get_owned_job(job_id, user_id)

    if job["status"] == "done":
        return {"job_id": job_id, "status": "done", "summary": job.get("summary") or {}}

    existing_task = _commit_tasks.get(job_id)
    if existing_task and not existing_task.done():
        try:
            summary = await asyncio.shield(existing_task)
            return {"job_id": job_id, "status": "done", "summary": summary}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Commit error for job {job_id}: {exc}")
            raise HTTPException(status_code=500, detail=f"Commit failed: {exc}")

    # Atomic claim on /commit: only claim if status is 'ready'
    sb = get_supabase()
    claim_res = sb.table("catalogue_import_jobs").update(
        {"status": "committing"}
    ).eq("id", job_id).eq("wholesaler_id", user_id).eq("status", "ready").execute()

    if not claim_res.data:
        fresh = await get_owned_job(job_id, user_id)
        if fresh.get("status") == "committing":
            existing_task = _commit_tasks.get(job_id)
            if existing_task:
                try:
                    summary = await asyncio.shield(existing_task)
                    return {"job_id": job_id, "status": "done", "summary": summary}
                except Exception:
                    pass
            return {"job_id": job_id, "status": "committing", "message": "Commit already in progress."}
        if fresh.get("status") == "done":
            return {"job_id": job_id, "status": fresh["status"], "summary": fresh.get("summary") or {}}
        raise HTTPException(
            status_code=400,
            detail=f"Job cannot be committed in status '{fresh.get('status')}'. Must be 'ready'."
        )

    async def _do_commit():
        try:
            return await commit_catalogue_import_job(job_id, user_id)
        finally:
            _commit_tasks.pop(job_id, None)

    task = asyncio.create_task(_do_commit())
    _commit_tasks[job_id] = task

    try:
        summary = await asyncio.shield(task)
        return {"job_id": job_id, "status": "done", "summary": summary}
    except asyncio.CancelledError:
        # Client disconnected mid-commit! Shielded task keeps running to completion in background.
        raise
    except Exception as exc:
        logger.error(f"Commit error for job {job_id}: {exc}", exc_info=exc)
        fresh = await get_owned_job(job_id, user_id)
        if fresh.get("status") in ("committing", "done"):
            return {"job_id": job_id, "status": fresh["status"], "summary": fresh.get("summary") or {}}
        raise HTTPException(status_code=500, detail=f"Commit failed: {exc}")


@router.get("/jobs/{job_id}")
async def get_job_status(
    job_id: str,
    user_id: str = Depends(require_user_id),
):
    """Return full job status, summary, and progress (P3)."""
    job = await get_owned_job(job_id, user_id)
    status = job.get("status")
    progress = None

    if status == "committing":
        summary = job.get("summary") or {}
        total = (summary.get("create", 0) + summary.get("update", 0))
        sb = get_supabase()
        done_res = sb.table("catalogue_import_rows").select(
            "id", count="exact"
        ).eq("job_id", job_id).not_.is_("product_id", "null").execute()
        done = done_res.count or 0
        progress = {"done": done, "total": total}
    elif status == "reading":
        sb = get_supabase()
        row_res = sb.table("catalogue_import_rows").select(
            "id", count="exact"
        ).eq("job_id", job_id).execute()
        done = row_res.count or 0
        total = _job_read_totals.get(job_id) or done
        progress = {"done": done, "total": total}
    elif status == "done":
        summary = job.get("summary") or {}
        total = (summary.get("create", 0) + summary.get("update", 0))
        progress = {"done": total, "total": total}

    job["progress"] = progress
    return job


@router.get("/jobs/{job_id}/errors.csv")
async def export_errors_csv(
    job_id: str,
    user_id: str = Depends(require_user_id),
):
    """Export CSV of rows with errors or warnings."""
    await get_owned_job(job_id, user_id)
    sb = get_supabase()

    res = sb.table("catalogue_import_rows").select(
        "sheet, row_number, product_code, messages"
    ).eq("job_id", job_id).order("row_number").execute()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Sheet", "Row Number", "Product Code", "Level", "Field", "Message"])

    for r in res.data or []:
        msgs = r.get("messages") or []
        for m in msgs:
            writer.writerow([
                r.get("sheet") or "",
                r.get("row_number"),
                r.get("product_code") or "",
                m.get("level", ""),
                m.get("field", ""),
                m.get("text", ""),
            ])

    csv_data = output.getvalue()
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=import_errors_{job_id}.csv"},
    )


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    user_id: str = Depends(require_user_id),
):
    """Cancel a job before commit."""
    job = await get_owned_job(job_id, user_id)
    if job["status"] in ("committing", "done"):
        raise HTTPException(status_code=400, detail="Cannot cancel a job that is already committing or completed.")

    sb = get_supabase()
    sb.table("catalogue_import_jobs").update({"status": "cancelled"}).eq("id", job_id).execute()
    return {"status": "cancelled"}


@router.post("/profiles/{profile_id}/sync")
async def sync_profile(
    profile_id: str,
    user_id: str = Depends(require_user_id),
):
    """Trigger a new import job for a saved Google Sheets profile."""
    sb = get_supabase()
    prof_res = sb.table("catalogue_import_profiles").select("*").eq("id", profile_id).execute()
    if not prof_res.data:
        raise HTTPException(status_code=404, detail="Profile not found.")

    profile = prof_res.data[0]
    if str(profile["wholesaler_id"]) != str(user_id):
        raise HTTPException(status_code=403, detail="Forbidden.")

    if not profile.get("gsheet_url"):
        raise HTTPException(status_code=400, detail="Profile does not have a Google Sheet URL.")

    # Create new job
    new_job_res = sb.table("catalogue_import_jobs").insert({
        "wholesaler_id": user_id,
        "source_kind": "gsheet",
        "gsheet_url": profile["gsheet_url"],
        "profile_id": profile["id"],
        "status": "uploaded",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    new_job = new_job_res.data[0]
    return {"job_id": new_job["id"], "status": "uploaded"}
