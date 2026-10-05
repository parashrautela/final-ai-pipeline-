"""Retailer-only image search, independently of the credit rollout flag."""
import asyncio
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings
from app.db.repository import get_supabase
from app.services.jev_catalogue import decide_matches
from app.services.catalogue_search import COLUMNS, MAX_BYTES, CatalogueIndex, ImageEncoder, InvalidPhoto, category

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)


def fetch_rows():
    # Fetch all published rows through paginated server queries, including
    # designs not in this retailer's existing shortlist. Never supplier names.
    rows, offset = [], 0
    while True:
        page = get_supabase().table("products").select(COLUMNS).eq("is_published", True).order("id").range(offset, offset + 499).execute().data
        rows.extend(page)
        if len(page) < 500:
            return rows
        offset += 500


index = CatalogueIndex(ImageEncoder(), fetch_rows, {urlparse(settings.SUPABASE_URL).hostname, "res.cloudinary.com"} - {None})
_maintenance = None


async def verified_retailer(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Sign-in required.")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or not parts[1].strip():
        raise HTTPException(401, "Sign-in required.")
    token = parts[1].strip()
    def verify():
        try:
            user = get_supabase().auth.get_user(token).user
        except Exception:
            raise HTTPException(401, "Session expired. Please sign in again.")
        if not user:
            raise HTTPException(401, "Sign-in required.")
        if (user.user_metadata or {}).get("role") != "retailer":
            raise HTTPException(403, "Retailer access required.")
        try:
            rows = get_supabase().table("retailers").select("id,verification_status").eq("user_id", str(user.id)).limit(1).execute().data
        except Exception:
            raise HTTPException(503, "Couldn’t verify retailer access.")
        if not rows or rows[0].get("verification_status") != "verified":
            raise HTTPException(403, "Retailer verification is required.")
        return str(user.id)
    return await asyncio.to_thread(verify)


@router.on_event("startup")
async def startup():
    global _maintenance
    _maintenance = asyncio.create_task(index.maintain())


@router.on_event("shutdown")
async def shutdown():
    tasks = [task for task in (_maintenance, index.refresh_task) if task is not None]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@router.post("/api/retailer/image-search")
@limiter.limit("12/minute")
async def search(request: Request, photo: UploadFile = File(...), jewellery_type: str = Form(...),
                 user_id: str = Depends(verified_retailer)):
    # Use the app's existing shared IP limiter. Query photos are neither stored
    # nor sent to any third-party inference provider, and never logged.
    if not jewellery_type.strip() or len(jewellery_type) > 80:
        raise HTTPException(400, "Select a jewellery category.")
    try:
        data = await photo.read(MAX_BYTES + 1)
    finally:
        await photo.close()
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(400, "Choose a photo smaller than 10 MB.")
    try:
        # Validation runs before readiness, so corrupt uploads are not hidden
        # behind a retry message. Only the selected query is encoded per search.
        await asyncio.to_thread(ImageEncoder.pixels, data)
        rows = await asyncio.to_thread(fetch_rows)
        if category(jewellery_type) not in {category(row.get("jewellery_type")) for row in rows}:
            raise HTTPException(400, "Select a category available in the catalogue.")
        result = await index.search(data, rows, jewellery_type, candidate_limit=12)
        result = await decide_matches(index, data, rows, result)
    except InvalidPhoto as exc:
        raise HTTPException(400, str(exc))
    except LookupError as exc:
        raise HTTPException(503, str(exc), headers={"Retry-After": "10"})
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Image search is temporarily unavailable. Please try again.")
    return JSONResponse(result, headers={"Cache-Control": "private, no-store"})


@router.get("/api/retailer/image-search/status")
async def search_status():
    from app.services.jev_catalogue import configured, MODEL
    return JSONResponse({"engine": "jewellery-subject-clip-jev", "subject_model": "u2netp", "comparison_target": "isolated jewellery", "model": MODEL, "jev_configured": configured(),
                         "indexed_images": len(index.vectors), "indexed_evidence": len(index.fingerprints), "unavailable_images": len(index.failed),
                         "index_refreshing": index.refresh_lock.locked()},
                        headers={"Cache-Control": "no-store"})
