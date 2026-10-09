"""FastAPI Router for Native Jewellery Request Broadcast and Wholesaler Queue.

Enforces strict DB-verified authentication for retailers and wholesalers.
Preserves existing systems, zero side effects on credits or catalogue searches.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
import hashlib
import io
from typing import Any, Optional
import uuid

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image, ImageOps
from pydantic import BaseModel, Field
from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions

from app.config import settings
from app.db.repository import get_supabase
from app.logging import logger
from app.services.manufacturing_scheduler import scheduler

router = APIRouter(prefix="/api", tags=["manufacturing"])

STORAGE_BUCKET = "manufacturing-requests"
MAX_ASSET_BYTES = 12 * 1024 * 1024  # 12 MB
ALLOWED_MIMES = {"image/jpeg", "image/png", "image/webp"}


def get_authenticated_supabase(token: str) -> Client:
    """Create an isolated, dedicated Supabase client for a specific user request.

    Never mutates headers on shared singletons, preventing auth mixups between concurrent users.
    """
    opts = ClientOptions(headers={"Authorization": f"Bearer {token}"})
    # The request's JWT controls PostgREST permissions. Keep the service client
    # separate; this deployment already provides the server-only API key.
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY, options=opts)


# ── STRICT AUTH DEPENDENCIES ─────────────────────────────────────────────────

async def require_verified_retailer(authorization: Optional[str] = Header(default=None)) -> dict:
    """Verify bearer token against Supabase and check verified retailer status in DB.

    Strict rule: Never permits unauthenticated calls.
    Rejects active employees, unverified, or banned stores.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Sign-in required.")

    parts = authorization.split(None, 1)
    if len(parts) != 2 or not parts[1].strip():
        raise HTTPException(401, "Sign-in required.")
    token = parts[1].strip()

    def _resolve():
        sb = get_supabase()
        try:
            user_resp = sb.auth.get_user(token)
            user = getattr(user_resp, "user", None)
            if not user or not getattr(user, "id", None):
                raise HTTPException(401, "Session expired. Please sign in again.")
            user_id = str(user.id)
        except Exception:
            raise HTTPException(401, "Session expired. Please sign in again.")

        # Check for active employee status — employees cannot create or manage broadcast
        emp_rows = (
            sb.table("employees")
            .select("id")
            .eq("auth_user_id", user_id)
            .eq("status", "active")
            .limit(1)
            .execute()
            .data
        )
        if emp_rows:
            raise HTTPException(403, "Employees are not permitted to manage manufacturing requests.")

        # Query retailer record
        ret_rows = (
            sb.table("retailers")
            .select("id, business_name, verification_status")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
            .data
        )
        if not ret_rows:
            raise HTTPException(403, "No retailer store found for this account.")

        ret = ret_rows[0]
        if ret.get("verification_status") != "verified":
            raise HTTPException(403, "Retailer account is not verified.")

        return {
            "user_id": user_id,
            "retailer_id": ret["id"],
            "business_name": ret.get("business_name") or "Retailer",
            "token": token,
            "client": get_authenticated_supabase(token),
        }

    return await asyncio.to_thread(_resolve)


async def require_verified_wholesaler(authorization: Optional[str] = Header(default=None)) -> dict:
    """Verify bearer token against Supabase and check verified wholesaler status in DB."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Sign-in required.")

    parts = authorization.split(None, 1)
    if len(parts) != 2 or not parts[1].strip():
        raise HTTPException(401, "Sign-in required.")
    token = parts[1].strip()

    def _resolve():
        sb = get_supabase()
        try:
            user_resp = sb.auth.get_user(token)
            user = getattr(user_resp, "user", None)
            if not user or not getattr(user, "id", None):
                raise HTTPException(401, "Session expired. Please sign in again.")
            user_id = str(user.id)
        except Exception:
            raise HTTPException(401, "Session expired. Please sign in again.")

        ws_rows = (
            sb.table("wholesalers")
            .select("id, business_name, verification_status")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
            .data
        )
        if not ws_rows:
            raise HTTPException(403, "No wholesaler account found for this account.")

        ws = ws_rows[0]
        if ws.get("verification_status") != "verified":
            raise HTTPException(403, "Wholesaler account is not verified.")

        return {
            "user_id": user_id,
            "wholesaler_id": ws["id"],
            "business_name": ws.get("business_name") or "Wholesaler",
            "token": token,
            "client": get_authenticated_supabase(token),
        }

    return await asyncio.to_thread(_resolve)


def _get_signed_asset_url(path: str, expires_in_seconds: int = 3600) -> Optional[str]:
    """Generate a short-lived signed URL for an asset in the private bucket."""
    if not path:
        return None
    try:
        res = get_supabase().storage.from_(STORAGE_BUCKET).create_signed_url(path, expires_in_seconds)
        if isinstance(res, dict):
            return res.get("signedURL") or res.get("signedUrl")
        return str(res)
    except Exception as e:
        logger.error(f"Failed to sign asset URL for {path}", exc_info=e)
        return None


# ── RETAILER ASSET UPLOAD ────────────────────────────────────────────────────

@router.post("/retailer/manufacturing-assets")
async def upload_manufacturing_asset(
    file: UploadFile = File(...),
    retailer: dict = Depends(require_verified_retailer),
):
    """Authenticated upload of reference jewellery photo.

    Validates decoded image bytes, normalizes orientation, strips EXIF,
    and uploads to private storage bucket. Returns asset_id and signed preview URL.
    """
    content_type = file.content_type or ""
    if content_type.lower() not in ALLOWED_MIMES:
        raise HTTPException(422, f"Invalid image type {content_type}. Supported: JPEG, PNG, WebP.")

    raw_bytes = await file.read()
    if len(raw_bytes) > MAX_ASSET_BYTES:
        raise HTTPException(413, "Image file too large. Maximum size is 12MB.")
    if len(raw_bytes) < 100:
        raise HTTPException(422, "Image file is empty or corrupted.")

    def _process_and_upload():
        try:
            img = Image.open(io.BytesIO(raw_bytes))
            img.verify()
            img = Image.open(io.BytesIO(raw_bytes))
        except Exception:
            raise HTTPException(422, "Could not decode image file.")

        # Reorient based on EXIF and remove EXIF
        img = ImageOps.exif_transpose(img)
        width, height = img.size

        # Cap max dimensions
        if max(width, height) > 5000:
            img.thumbnail((3000, 3000), Image.Resampling.LANCZOS)
            width, height = img.size

        # Save normalized image to buffer
        out_buf = io.BytesIO()
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")

        # Save as JPEG for standardized fast display derivative
        if img.mode == "RGBA":
            # flatten on white background if transparent
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg

        img.save(out_buf, format="JPEG", quality=90, optimize=True)
        processed_bytes = out_buf.getvalue()

        # Generate unique storage path
        asset_uuid = uuid.uuid4()
        storage_path = f"assets/{retailer['retailer_id']}/{asset_uuid}.jpg"

        sb = get_supabase()
        sb.storage.from_(STORAGE_BUCKET).upload(
            storage_path,
            processed_bytes,
            file_options={"content-type": "image/jpeg", "upsert": "false"},
        )

        # Insert asset database row
        row = sb.table("manufacturing_request_assets").insert({
            "id": str(asset_uuid),
            "owner_retailer_id": retailer["retailer_id"],
            "storage_bucket": STORAGE_BUCKET,
            "storage_path": storage_path,
            "mime_type": "image/jpeg",
            "byte_size": len(processed_bytes),
            "width": width,
            "height": height,
            "status": "uploaded",
        }).execute().data[0]

        signed_url = _get_signed_asset_url(storage_path, 3600)

        return {
            "ok": True,
            "asset_id": row["id"],
            "preview_url": signed_url,
            "width": width,
            "height": height,
            "byte_size": len(processed_bytes),
        }

    return await asyncio.to_thread(_process_and_upload)


# ── RETAILER REQUEST CREATION & MANAGEMENT ───────────────────────────────────

class CreateRequestPayload(BaseModel):
    asset_id: uuid.UUID
    category: str = Field(..., min_length=2, max_length=100)
    min_weight_grams: float = Field(..., gt=0, le=5000)
    max_weight_grams: float = Field(..., gt=0, le=5000)
    material: str = Field(..., min_length=2, max_length=50)
    purity: str = Field(..., min_length=1, max_length=50)
    gemstone_preference: str = Field(default="none", max_length=100)
    quantity: int = Field(default=1, ge=1, le=1000)
    making_budget_mode: str = Field(..., pattern="^(per_gram|fixed_total|percentage)$")
    making_budget_amount: float = Field(..., gt=0)
    currency: str = Field(default="INR", max_length=10)
    metal_rate_snapshot: Optional[float] = None
    metal_rate_basis: Optional[str] = None
    delivery_needed_date: date
    notes: Optional[str] = Field(default=None, max_length=2000)
    superseded_request_id: Optional[uuid.UUID] = None
    quotation_window_hours: int = Field(default=24, ge=1, le=48)


@router.post("/retailer/manufacturing-requests")
async def create_manufacturing_request(
    payload: CreateRequestPayload,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    retailer: dict = Depends(require_verified_retailer),
):
    """Validated creation of a manufacturing request.

    Stores an atomic retry key and invites all verified wholesalers with one deadline.
    """
    if payload.max_weight_grams < payload.min_weight_grams:
        raise HTTPException(422, "Maximum weight cannot be less than minimum weight.")

    if payload.delivery_needed_date <= date.today():
        raise HTTPException(422, "Delivery needed date must be strictly in the future.")

    def _execute():
        user_sb = retailer["client"]

        # Call database function with row locks, transaction safety, and atomic idempotency
        params = {
            "p_offer_duration_seconds": payload.quotation_window_hours * 3600,
            "p_asset_id": str(payload.asset_id),
            "p_category": payload.category.strip(),
            "p_min_weight": payload.min_weight_grams,
            "p_max_weight": payload.max_weight_grams,
            "p_material": payload.material.strip(),
            "p_purity": payload.purity.strip(),
            "p_gemstone_preference": payload.gemstone_preference.strip() or "none",
            "p_quantity": payload.quantity,
            "p_making_budget_mode": payload.making_budget_mode,
            "p_making_budget_amount": payload.making_budget_amount,
            "p_currency": payload.currency,
            "p_metal_rate_snapshot": payload.metal_rate_snapshot,
            "p_metal_rate_basis": payload.metal_rate_basis,
            "p_delivery_needed_date": payload.delivery_needed_date.isoformat(),
            "p_notes": payload.notes.strip() if payload.notes else None,
            "p_superseded_request_id": str(payload.superseded_request_id) if payload.superseded_request_id else None,
            "p_idempotency_key": idempotency_key.strip() if idempotency_key else None,
            "p_request_hash": hashlib.sha256(payload.model_dump_json().encode()).hexdigest() if idempotency_key else None,
        }

        rpc_res = user_sb.rpc("manufacturing_broadcast_create", params).execute().data

        if not rpc_res or not rpc_res.get("ok"):
            err = rpc_res.get("error") if rpc_res else "CREATION_FAILED"
            msg = rpc_res.get("message") if rpc_res else "Failed to create manufacturing request."
            status = 403 if err == "UNAUTHORIZED" else 422
            raise HTTPException(status, msg)

        return rpc_res

    return await asyncio.to_thread(_execute)


@router.get("/retailer/manufacturing-requests")
async def list_retailer_requests(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    retailer: dict = Depends(require_verified_retailer),
):
    """List caller's own manufacturing requests with active status and signed asset URLs."""
    def _fetch():
        sb = get_supabase()
        q = (
            sb.table("manufacturing_requests")
            .select(
                "id, category, min_weight_grams, max_weight_grams, material, purity, "
                "gemstone_preference, quantity, making_budget_mode, making_budget_amount, "
                "currency, delivery_needed_date, state, active_offer_id, assigned_wholesaler_id, "
                "created_at, updated_at, assigned_at, cancelled_at, broadcast_mode, quotation_deadline"
            )
            .eq("retailer_id", retailer["retailer_id"])
            .order("created_at", desc=True)
            .limit(limit)
        )
        if status:
            q = q.eq("state", status)

        rows = q.execute().data or []

        # Populate asset paths and signed URLs
        req_ids = [r["id"] for r in rows]
        assets_by_req = {}
        if req_ids:
            asset_rows = (
                sb.table("manufacturing_request_assets")
                .select("id, request_id, storage_path")
                .in_("request_id", req_ids)
                .execute()
                .data or []
            )
            for a in asset_rows:
                assets_by_req[a["request_id"]] = _get_signed_asset_url(a["storage_path"], 3600)

        # Populate assigned wholesaler names if assigned
        wholesaler_names = {}
        ws_ids = list({r["assigned_wholesaler_id"] for r in rows if r.get("assigned_wholesaler_id")})
        if ws_ids:
            ws_rows = (
                sb.table("wholesalers")
                .select("id, business_name")
                .in_("id", ws_ids)
                .execute()
                .data or []
            )
            for w in ws_rows:
                wholesaler_names[w["id"]] = w.get("business_name")

        for r in rows:
            r["image_url"] = assets_by_req.get(r["id"])
            if r.get("assigned_wholesaler_id"):
                r["assigned_wholesaler_name"] = wholesaler_names.get(r["assigned_wholesaler_id"])

        return {
            "ok": True,
            "requests": rows,
            "server_time": datetime.now(timezone.utc).isoformat(),
        }

    return await asyncio.to_thread(_fetch)


@router.get("/retailer/manufacturing-requests/{request_id}")
async def get_retailer_request_detail(
    request_id: uuid.UUID,
    retailer: dict = Depends(require_verified_retailer),
):
    """Detailed view of an owned request, including reference photo, quote details if assigned."""
    def _fetch():
        sb = get_supabase()
        req_rows = (
            sb.table("manufacturing_requests")
            .select("*")
            .eq("id", str(request_id))
            .eq("retailer_id", retailer["retailer_id"])
            .limit(1)
            .execute()
            .data
        )
        if not req_rows:
            raise HTTPException(404, "Manufacturing request not found.")

        req = req_rows[0]

        # Asset URL
        asset_rows = (
            sb.table("manufacturing_request_assets")
            .select("id, storage_path, width, height, byte_size")
            .eq("request_id", req["id"])
            .limit(1)
            .execute()
            .data
        )
        if asset_rows:
            req["image_url"] = _get_signed_asset_url(asset_rows[0]["storage_path"], 3600)
            req["asset_metadata"] = asset_rows[0]

        # Active offer remaining time if routing
        if req.get("state") == "routing" and req.get("active_offer_id"):
            off_rows = (
                sb.table("manufacturing_offers")
                .select("id, rank, offered_at, expires_at, status")
                .eq("id", req["active_offer_id"])
                .limit(1)
                .execute()
                .data
            )
            if off_rows:
                req["active_offer"] = off_rows[0]

        if req.get("broadcast_mode") == "parallel":
            quotes = sb.table("manufacturing_quotes").select("*").eq("request_id", req["id"]).order("created_at").execute().data or []
            ids = list({q["wholesaler_id"] for q in quotes})
            businesses = sb.table("wholesalers").select("id,business_name,city,state").in_("id", ids).execute().data if ids else []
            by_id = {w["id"]: w for w in businesses}
            for quote in quotes:
                quote["wholesaler"] = by_id.get(quote["wholesaler_id"])
            req["quotes"] = quotes

        # Accepted quote details if assigned
        if req.get("state") == "assigned" and req.get("accepted_quote_id"):
            quote_rows = (
                sb.table("manufacturing_quotes")
                .select("*")
                .eq("id", req["accepted_quote_id"])
                .limit(1)
                .execute()
                .data
            )
            if quote_rows:
                req["quote"] = quote_rows[0]

            if req.get("assigned_wholesaler_id"):
                ws_rows = (
                    sb.table("wholesalers")
                    .select("id, business_name, city, state")
                    .eq("id", req["assigned_wholesaler_id"])
                    .limit(1)
                    .execute()
                    .data
                )
                if ws_rows:
                    req["assigned_wholesaler"] = ws_rows[0]

        return {
            "ok": True,
            "request": req,
            "server_time": datetime.now(timezone.utc).isoformat(),
        }

    return await asyncio.to_thread(_fetch)


@router.post("/retailer/manufacturing-requests/{request_id}/cancel")
async def cancel_manufacturing_request(
    request_id: uuid.UUID,
    retailer: dict = Depends(require_verified_retailer),
):
    """Atomic cancellation of an active manufacturing request by its retailer."""
    def _cancel():
        user_sb = retailer["client"]
        res = user_sb.rpc("manufacturing_request_cancel", {"p_request_id": str(request_id)}).execute().data
        if not res or not res.get("ok"):
            err = res.get("error") if res else "CANCEL_FAILED"
            msg = res.get("message") if res else "Could not cancel request."
            status = 404 if err == "NOT_FOUND" else (403 if err == "FORBIDDEN" else 409)
            raise HTTPException(status, msg)
        return res

    return await asyncio.to_thread(_cancel)


# ── WHOLESALER OFFERS INBOX & ACTIONS ────────────────────────────────────────

@router.get("/wholesaler/manufacturing-offers")
async def list_wholesaler_offers(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    wholesaler: dict = Depends(require_verified_wholesaler),
):
    """List offers addressed to caller's verified wholesaler account."""
    def _fetch():
        sb = get_supabase()
        now = datetime.now(timezone.utc)

        q = (
            sb.table("manufacturing_offers")
            .select("id, request_id, rank, status, offered_at, expires_at, responded_at, decline_reason")
            .eq("wholesaler_id", wholesaler["wholesaler_id"])
            .order("offered_at", desc=True)
            .limit(limit)
        )
        if status:
            q = q.eq("status", status)

        offers = q.execute().data or []

        # Join request details
        req_ids = list({o["request_id"] for o in offers})
        requests_map = {}
        assets_map = {}

        if req_ids:
            req_rows = (
                sb.table("manufacturing_requests")
                .select(
                    "id, category, min_weight_grams, max_weight_grams, material, purity, "
                    "gemstone_preference, quantity, making_budget_mode, making_budget_amount, "
                    "currency, delivery_needed_date, notes, state, broadcast_mode, quotation_deadline"
                )
                .in_("id", req_ids)
                .execute()
                .data or []
            )
            for r in req_rows:
                requests_map[r["id"]] = r

            asset_rows = (
                sb.table("manufacturing_request_assets")
                .select("request_id, storage_path")
                .in_("request_id", req_ids)
                .execute()
                .data or []
            )
            for a in asset_rows:
                assets_map[a["request_id"]] = _get_signed_asset_url(a["storage_path"], 3600)

        for off in offers:
            off["request"] = requests_map.get(off["request_id"])
            off["image_url"] = assets_map.get(off["request_id"])
            # Compute authoritative remaining seconds
            if off.get("status") in ("active", "open") and off.get("expires_at"):
                exp_raw = off["expires_at"]
                if isinstance(exp_raw, str):
                    exp = datetime.fromisoformat(exp_raw.replace("Z", "+00:00"))
                else:
                    exp = exp_raw
                rem = max(0, int((exp - now).total_seconds()))
                off["remaining_seconds"] = rem
            else:
                off["remaining_seconds"] = 0

        return {
            "ok": True,
            "offers": offers,
            "server_time": now.isoformat(),
        }

    return await asyncio.to_thread(_fetch)


@router.get("/wholesaler/manufacturing-offers/{offer_id}")
async def get_wholesaler_offer_detail(
    offer_id: uuid.UUID,
    wholesaler: dict = Depends(require_verified_wholesaler),
):
    """Detailed view of an offer addressed to caller, with authoritative remaining countdown."""
    def _fetch():
        sb = get_supabase()
        now = datetime.now(timezone.utc)

        off_rows = (
            sb.table("manufacturing_offers")
            .select("*")
            .eq("id", str(offer_id))
            .eq("wholesaler_id", wholesaler["wholesaler_id"])
            .limit(1)
            .execute()
            .data
        )
        if not off_rows:
            raise HTTPException(404, "Offer not found.")

        offer = off_rows[0]

        # Request details
        req_rows = (
            sb.table("manufacturing_requests")
            .select("*")
            .eq("id", offer["request_id"])
            .limit(1)
            .execute()
            .data
        )
        if not req_rows:
            raise HTTPException(404, "Associated manufacturing request not found.")

        req = req_rows[0]
        offer["request"] = req

        # Signed reference image URL
        asset_rows = (
            sb.table("manufacturing_request_assets")
            .select("storage_path, width, height, byte_size")
            .eq("request_id", offer["request_id"])
            .limit(1)
            .execute()
            .data
        )
        if asset_rows:
            offer["image_url"] = _get_signed_asset_url(asset_rows[0]["storage_path"], 3600)
            offer["asset_metadata"] = asset_rows[0]

        # Calculate authoritative remaining countdown
        if offer.get("status") in ("active", "open") and offer.get("expires_at"):
            exp = datetime.fromisoformat(offer["expires_at"].replace("Z", "+00:00"))
            rem = max(0, int((exp - now).total_seconds()))
            offer["remaining_seconds"] = rem
            if rem == 0:
                offer["status"] = "expired"
        else:
            offer["remaining_seconds"] = 0

        # Quote if already accepted
        if offer.get("status") in ("accepted", "quoted", "not_selected", "cancelled"):
            q_rows = (
                sb.table("manufacturing_quotes")
                .select("*")
                .eq("offer_id", offer["id"])
                .limit(1)
                .execute()
                .data
            )
            if q_rows:
                offer["quote"] = q_rows[0]

        return {
            "ok": True,
            "offer": offer,
            "server_time": now.isoformat(),
        }

    return await asyncio.to_thread(_fetch)


class AcceptOfferPayload(BaseModel):
    making_charge_mode: str = Field(..., pattern="^(per_gram|fixed_total|percentage)$")
    making_charge_amount: float = Field(..., gt=0, allow_inf_nan=False)
    metal_estimate_amount: Optional[float] = Field(default=0.0, ge=0, allow_inf_nan=False)
    gemstone_estimate_amount: Optional[float] = Field(default=0.0, ge=0, allow_inf_nan=False)
    other_estimate_amount: Optional[float] = Field(default=0.0, ge=0, allow_inf_nan=False)
    proposed_delivery_date: date
    comments: Optional[str] = Field(default=None, max_length=2000)
    expected_version: Optional[int] = None


@router.post("/wholesaler/manufacturing-offers/{offer_id}/accept")
async def accept_manufacturing_offer(
    offer_id: uuid.UUID,
    payload: AcceptOfferPayload,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    wholesaler: dict = Depends(require_verified_wholesaler),
):
    """Wholesaler accepts offer with binding quote and delivery commitment.

    Halts queue and assigns request atomically.
    """
    def _accept():
        sb = get_supabase()

        # Check idempotency
        if idempotency_key:
            idem_rows = (
                sb.table("manufacturing_idempotency")
                .select("response_code, response_body")
                .eq("actor_user_id", wholesaler["user_id"])
                .eq("action", "OFFER_ACCEPT")
                .eq("idempotency_key", idempotency_key)
                .execute()
                .data
            )
            if idem_rows:
                stored = idem_rows[0]
                return JSONResponse(status_code=stored["response_code"], content=stored["response_body"])

        params = {
            "p_offer_id": str(offer_id),
            "p_making_charge_mode": payload.making_charge_mode,
            "p_making_charge_amount": payload.making_charge_amount,
            "p_metal_estimate_amount": payload.metal_estimate_amount or 0.0,
            "p_gemstone_estimate_amount": payload.gemstone_estimate_amount or 0.0,
            "p_other_estimate_amount": payload.other_estimate_amount or 0.0,
            "p_proposed_delivery_date": payload.proposed_delivery_date.isoformat(),
            "p_comments": payload.comments.strip() if payload.comments else None,
            "p_expected_version": payload.expected_version,
        }

        user_sb = wholesaler["client"]
        rpc_res = user_sb.rpc("manufacturing_offer_accept", params).execute().data

        if not rpc_res or not rpc_res.get("ok"):
            err = rpc_res.get("error") if rpc_res else "ACCEPT_FAILED"
            msg = rpc_res.get("message") if rpc_res else "Could not accept offer."
            if err == "OFFER_EXPIRED":
                raise HTTPException(410, "Offer has expired.")
            elif err in ("BUDGET_EXCEEDED", "DEADLINE_EXCEEDED", "MODE_MISMATCH"):
                raise HTTPException(422, msg)
            elif err == "VERSION_CONFLICT":
                raise HTTPException(409, msg)
            else:
                raise HTTPException(403, msg)

        if idempotency_key:
            sb.table("manufacturing_idempotency").upsert({
                "actor_user_id": wholesaler["user_id"],
                "action": "OFFER_ACCEPT",
                "idempotency_key": idempotency_key,
                "request_hash": hashlib.sha256(payload.model_dump_json().encode()).hexdigest(),
                "response_code": 200,
                "response_body": rpc_res,
            }, on_conflict="actor_user_id,action,idempotency_key").execute()

        return rpc_res

    return await asyncio.to_thread(_accept)



def _broadcast_result(result):
    if result and result.get("ok"):
        return result
    error = (result or {}).get("error", "FAILED")
    code = 404 if error == "NOT_FOUND" else 403 if error in ("UNAUTHORIZED", "FORBIDDEN") else 422 if error in ("INVALID_QUOTE", "INVALID_REASON") else 409
    raise HTTPException(code, (result or {}).get("message", "Could not complete this action."))


@router.post("/wholesaler/manufacturing-offers/{offer_id}/quotes")
async def submit_manufacturing_quote(offer_id: uuid.UUID, payload: AcceptOfferPayload,
                                    wholesaler: dict = Depends(require_verified_wholesaler)):
    """Submit one quote per invitation; never assigns the project. RPC retries are atomic."""
    params = {"p_offer_id": str(offer_id), "p_making_charge_mode": payload.making_charge_mode,
              "p_making_charge_amount": payload.making_charge_amount,
              "p_metal_estimate_amount": payload.metal_estimate_amount or 0,
              "p_gemstone_estimate_amount": payload.gemstone_estimate_amount or 0,
              "p_other_estimate_amount": payload.other_estimate_amount or 0,
              "p_proposed_delivery_date": payload.proposed_delivery_date.isoformat(),
              "p_comments": payload.comments.strip() if payload.comments else None,
              "p_expected_version": payload.expected_version}
    return await asyncio.to_thread(lambda: _broadcast_result(wholesaler["client"].rpc("manufacturing_quote_submit", params).execute().data))


class AwardQuotePayload(BaseModel):
    quote_id: uuid.UUID


@router.post("/retailer/manufacturing-requests/{request_id}/award")
async def award_manufacturing_quote(request_id: uuid.UUID, payload: AwardQuotePayload,
                                   retailer: dict = Depends(require_verified_retailer)):
    """Retailer chooses the supplier; request locking guarantees exactly one winner."""
    return await asyncio.to_thread(lambda: _broadcast_result(retailer["client"].rpc(
        "manufacturing_quote_award", {"p_request_id": str(request_id), "p_quote_id": str(payload.quote_id)}).execute().data))


class DeclineOfferPayload(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=1000)


@router.post("/wholesaler/manufacturing-offers/{offer_id}/decline")
async def decline_manufacturing_offer(
    offer_id: uuid.UUID,
    payload: DeclineOfferPayload = DeclineOfferPayload(),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    wholesaler: dict = Depends(require_verified_wholesaler),
):
    """Wholesaler declines offer with optional reason. Immediately advances queue."""
    def _decline():
        user_sb = wholesaler["client"]
        sb = get_supabase()

        if idempotency_key:
            idem_rows = (
                sb.table("manufacturing_idempotency")
                .select("response_code, response_body")
                .eq("actor_user_id", wholesaler["user_id"])
                .eq("action", "OFFER_DECLINE")
                .eq("idempotency_key", idempotency_key)
                .execute()
                .data
            )
            if idem_rows:
                stored = idem_rows[0]
                return JSONResponse(status_code=stored["response_code"], content=stored["response_body"])

        params = {
            "p_offer_id": str(offer_id),
            "p_reason": payload.reason.strip() if payload.reason else None,
        }

        rpc_res = user_sb.rpc("manufacturing_offer_decline", params).execute().data

        if not rpc_res or not rpc_res.get("ok"):
            err = rpc_res.get("error") if rpc_res else "DECLINE_FAILED"
            msg = rpc_res.get("message") if rpc_res else "Could not decline offer."
            raise HTTPException(403 if err == "FORBIDDEN" else 409, msg)

        if idempotency_key:
            sb.table("manufacturing_idempotency").upsert({
                "actor_user_id": wholesaler["user_id"],
                "action": "OFFER_DECLINE",
                "idempotency_key": idempotency_key,
                "request_hash": hashlib.sha256((payload.reason or "").encode()).hexdigest(),
                "response_code": 200,
                "response_body": rpc_res,
            }, on_conflict="actor_user_id,action,idempotency_key").execute()

        return rpc_res

    return await asyncio.to_thread(_decline)


# ── OPERATIONAL HEALTH & DEVICE REGISTRATION ─────────────────────────────────

class RegisterDevicePayload(BaseModel):
    device_token: str = Field(..., min_length=16, max_length=200)
    environment: str = Field(default="production", pattern="^(development|sandbox|production)$")


@router.post("/devices/register")
async def register_device_token(
    payload: RegisterDevicePayload,
    authorization: Optional[str] = Header(default=None),
):
    """Register APNs device token for notifications."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Sign-in required.")

    token = authorization.split(None, 1)[1].strip()

    def _reg():
        sb = get_supabase()
        user_resp = sb.auth.get_user(token)
        user = getattr(user_resp, "user", None)
        if not user or not getattr(user, "id", None):
            raise HTTPException(401, "Invalid session token.")

        sb.table("manufacturing_device_tokens").upsert({
            "user_id": str(user.id),
            "device_token": payload.device_token.strip(),
            "environment": payload.environment,
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="user_id,device_token").execute()

        return {"ok": True, "registered": True}

    return await asyncio.to_thread(_reg)


@router.get("/manufacturing/health")
async def manufacturing_health():
    """Operational health probe for the manufacturing queue and scheduler."""
    return {
        "status": "healthy",
        "broadcast_modes": ["sequential", "parallel"],
        "last_sweep_at": scheduler.last_sweep_at.isoformat() if scheduler.last_sweep_at else None,
        "swept_count": scheduler.swept_count,
        "advanced_count": scheduler.advanced_count,
        "last_sweep_error": scheduler.last_sweep_error,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }
