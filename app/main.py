import asyncio
import hashlib
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

# pyrefly: ignore [missing-import]
import sentry_sdk
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from sentry_sdk.integrations.fastapi import FastApiIntegration
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.auth import require_ownership, require_user, resolve_user_id
from app.config import settings
from app.db.repository import (
    count_prior_debits,
    refund_debit,
    create_product,
    fetch_chamak_generation,
    fetch_job_by_id,
    spend_credits,
    update_chamak_generation,
    update_job_status,
    update_product_image_url,
)
from app.logging import logger
from app.services.chamak import (
    _refund_failed_generation,
    run_set_creation_generation,
    run_stage1_vision_analysis,
    run_stage4_generation,
    run_stage4_generation_openai,
    SET_MIN_PIECES,
    set_source_urls,
)
from app.services.backfill import backfill_on_startup
from app.services.pipeline import process_product_image
from app.services.storage import upload_raw_image
from app.validation import (
    ChamakGenerationRequest,
    ValidationError,
    validate_jewellery_type_dynamic,
    validate_product_id,
    validate_product_input,
    validate_uuid,
)
from app.worker import worker_loop

# Rate limiter keyed on client IP. Each route sets its own cap;
# the default here is a fallback for any route we forget to decorate.
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

# Error tracking: unhandled exceptions (Reve/Nanobana API errors, storage
# failures, pipeline exceptions, etc.) are reported to Sentry so they're
# visible outside container stdout logs. Only initialized when a DSN is
# actually configured — this keeps local/dev runs working without Sentry.
if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        integrations=[FastApiIntegration()],
        traces_sample_rate=1.0,
        # Add data like request headers and IP for users,
        # see https://docs.sentry.io/platforms/python/data-management/data-collected/ for more info
        send_default_pii=True,
    )
    logger.info("Sentry error tracking initialized.")
else:
    logger.warning(
        "SENTRY_DSN is not set — unhandled exceptions will NOT be reported to "
        "Sentry and will only be visible in container stdout logs. Set the "
        "SENTRY_DSN environment variable to enable error tracking."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the background worker as soon as the server is ready.
    # On shutdown, cancel it cleanly instead of letting it hang.
    task = asyncio.create_task(worker_loop())
    # One-off catch-up for images stored before they had small copies. Costs a
    # single query once everything has been converted.
    catch_up = asyncio.create_task(backfill_on_startup())
    yield
    task.cancel()
    catch_up.cancel()
    try:
        await task
    except asyncio.CancelledError:
        logger.info("Worker stopped.")


app = FastAPI(lifespan=lifespan)

app.state.limiter = limiter
# slowapi needs this handler registered so it returns a proper 429 JSON body
# instead of a generic 500 when a rate limit is hit.
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "https://jwellery.arpitray.in",
    "https://jewel-india-frontend-yws1.vercel.app",
    "https://www.jewelindia.shop",
    "https://app.jewelindia.shop"
]

# Whitelist only the origins we actually own. Wildcard would be simpler but
# breaks cookies/credentials on the frontend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def ensure_cors_headers(request, call_next):
    """Ensure CORS headers are present on all responses including errors."""
    # FastAPI's CORSMiddleware only runs on successful handler paths.
    # Unhandled exceptions bypass it, so the browser sees a CORS error instead
    # of the real error. This fallback makes sure the header is always there.
    try:
        response = await call_next(request)
    except Exception:
        response = JSONResponse({"detail": "Internal Server Error"}, status_code=500)

    if "Access-Control-Allow-Origin" not in response.headers:
        origin = request.headers.get("origin")
        if origin in ALLOWED_ORIGINS:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"

    return response


# ─────────────────────────────────────────────────────────────────────────────
# Credit gate
# ─────────────────────────────────────────────────────────────────────────────

# A client key longer than this is hashed before it reaches the ledger, so an
# oversized header can never trip the UNIQUE index (which would surface as a
# 503 rather than a charge).
_MAX_CLIENT_KEY_LEN = 128

# How many times one request may be retried after its charge was refunded
# (its work failed). Each retry is a new, fully billed attempt; the cap only
# stops a client that loops forever from walking an endless key chain.
_MAX_RETRIES_AFTER_REFUND = 5


def _ledger_key(
    *,
    user_id: str,
    feature_key: str,
    generation_id: str,
    idempotency_key: Optional[str],
) -> str:
    """The idempotency key the ledger stores for this charge.

    A client's Idempotency-Key means "this user's request on this generation",
    nothing more. Stored verbatim it was a master key: once one charge existed
    under it, replaying the same header on ANY other generation came back
    `replayed: true` and ran that generation for free — unlimited times.
    Namespacing it server-side makes the same header on another generation, or
    from another account, simply a different key — so it gets charged.
    """
    client_key = (idempotency_key or "").strip()
    if client_key:
        if len(client_key) > _MAX_CLIENT_KEY_LEN:
            client_key = "sha256:" + hashlib.sha256(client_key.encode()).hexdigest()
        return f"{user_id}:{generation_id}:{client_key}"

    # Fallback for clients that predate the header. Keyed on the row, so a
    # double-tap still charges once. A replay of this key never starts new work
    # (see `_charge_or_reject`), so re-rolls past the first one are refused
    # rather than run for free until the client sends the header.
    logger.warning(
        "No Idempotency-Key header — falling back to a row-derived key. "
        "Re-rolls on this generation past the first will not run.",
        extra={"generation_id": generation_id, "feature_key": feature_key},
    )
    return f"chamak:{feature_key}:{generation_id}"


async def _charge_or_reject(
    *,
    user_id: Optional[str],
    feature_key: str,
    generation_id: str,
    idempotency_key: Optional[str],
    metadata: Optional[dict] = None,
) -> Optional[dict]:
    """Debit before any work starts, or refuse the request.

    Returns the RPC result, or None when metering is switched off. A result
    with `replayed: true` means this request was already paid for and its work
    already started: the caller must NOT start it again (`_is_replay`).

    One exception to "a replay starts nothing": if that earlier charge was
    refunded — its work failed and the credits went back — the retry is a new
    attempt and is billed as one, under a key derived from the original so a
    double-tap of the retry still charges once. That is what lets a client
    that keeps its key until success (iOS does) retry a failed generation.

    Fails CLOSED. If the ledger is unreachable we return 503 rather than
    letting the job through — the old upload-quota check failed open, which is
    fine for a courtesy limit and unacceptable for money.
    """
    if not settings.CREDITS_ENABLED or user_id is None:
        return None

    base_key = _ledger_key(
        user_id=user_id,
        feature_key=feature_key,
        generation_id=generation_id,
        idempotency_key=idempotency_key,
    )

    result: dict = {}
    for attempt in range(_MAX_RETRIES_AFTER_REFUND + 1):
        key = base_key if attempt == 0 else f"{base_key}:after-refund:{attempt}"
        try:
            result = await spend_credits(
                user_id=user_id,
                feature_key=feature_key,
                idempotency_key=key,
                reference_type="chamak_generation",
                reference_id=generation_id,
                metadata=metadata or {},
            )
        except Exception as exc:
            logger.error(
                f"Credit ledger unreachable for {generation_id}: {exc}",
                extra={"generation_id": generation_id},
                exc_info=True,
            )
            raise HTTPException(
                status_code=503,
                detail="Could not reach your Treasure Chest just now. Please try again.",
            ) from exc

        if not (result.get("ok") and result.get("replayed") and result.get("refunded")):
            break
        logger.info(
            "Retry of a refunded charge — billing it as a new attempt",
            extra={"generation_id": generation_id, "attempt": attempt + 1},
        )
    else:
        raise HTTPException(
            status_code=409,
            detail="This request has already failed several times. Please start a new one.",
        )

    if result.get("ok"):
        return result

    if result.get("error") == "IDEMPOTENCY_CONFLICT":
        # The ledger holds this key for a different wallet or a different
        # generation. Never a replay, never free.
        logger.warning(
            f"Idempotency-Key conflict on {generation_id}",
            extra={"generation_id": generation_id, "feature_key": feature_key},
        )
        raise HTTPException(
            status_code=409,
            detail={
                "error": "IDEMPOTENCY_CONFLICT",
                "message": "This request key was already used for something else.",
            },
        )

    if result.get("error") == "INSUFFICIENT_CREDITS":
        # 402 so the app can tell "you are out of credits" apart from every
        # other failure and open the top-up sheet instead of an error alert.
        raise HTTPException(
            status_code=402,
            detail={
                "error": "INSUFFICIENT_CREDITS",
                "message": "You do not have enough credits for this.",
                "required": result.get("required"),
                "balance": result.get("balance"),
                "short_by": result.get("short_by"),
            },
        )

    logger.error(
        f"Credit debit refused for {generation_id}: {result}",
        extra={"generation_id": generation_id},
    )
    raise HTTPException(status_code=500, detail="Could not process credits for this action.")


def _is_replay(charge: Optional[dict]) -> bool:
    """The charge already existed: this request is a retry of one we took."""
    return bool(charge and charge.get("replayed"))


def _parse_timestamp(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _charge_already_delivered(row: dict, charge: dict) -> bool:
    """Did the job this (latest) charge paid for finish with an output?"""
    if charge.get("superseded") or not row.get("output_image_url"):
        return False
    charged_at = _parse_timestamp(charge.get("created_at"))
    completed_at = _parse_timestamp(row.get("completed_at"))
    return bool(charged_at and completed_at and completed_at >= charged_at)


async def _replayed_charge_response(
    generation_id: str,
    charge: dict,
    message: str,
    *,
    restore_done: bool = True,
) -> dict:
    """Answer a retry of an already-paid request WITHOUT starting new work.

    The job this charge paid for was queued by the original request. Starting
    another one here — on this generation or any other — would be a fresh
    output billed zero times, which is the free-generation hole this closes.
    Instead, report where that job has got to, in the same shape as the
    original 202, so a client that retried simply carries on polling.

    `restore_done`: iOS and web both write `status: generating` to the row
    themselves before calling us. If the paid job had already finished, that
    write hides the finished result and the client would poll a job that is
    never coming. When the ledger and the row agree the job completed after
    this charge, put `done` back.
    """
    row = await fetch_chamak_generation(generation_id) or {}
    status = row.get("status") or "generating"

    if restore_done and status != "done" and _charge_already_delivered(row, charge):
        try:
            await update_chamak_generation(generation_id, {"status": "done"})
            status = "done"
        except Exception:
            logger.warning(
                "Could not restore 'done' after a replayed charge",
                extra={"generation_id": generation_id},
                exc_info=True,
            )

    logger.info(
        "Replayed charge — no new work started",
        extra={"generation_id": generation_id, "status": status},
    )
    return {
        "message": message,
        "generation_id": generation_id,
        "status": status,
        "replayed": True,
    }


async def _start_job(
    background_tasks: BackgroundTasks,
    generation_id: str,
    charge: Optional[dict],
    job: Callable[..., Awaitable[None]],
    *,
    status: str,
) -> None:
    """Mark the row and queue the job that `charge` paid for.

    The charge travels with the job so a failure refunds exactly that debit.
    If the row cannot be marked, the credits go back before the error does:
    the client's retry then finds a refunded charge and bills a fresh attempt,
    instead of a live charge whose job never started.
    """
    try:
        await update_chamak_generation(generation_id, {"status": status})
    except Exception:
        await _refund_failed_generation(generation_id, "Could not start the job", charge)
        raise
    background_tasks.add_task(job, generation_id, charge)


@app.get("/health")
@limiter.limit("30/minute")
async def health_check(request: Request):
    return {"status": "ok", "environment": settings.ENVIRONMENT}


@app.post("/process", status_code=202)
@limiter.limit("5/minute")
async def process_upload(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(
        ..., description="Raw jewellery image (JPEG/PNG/WebP, max 10MB)"
    ),
    title: str = Form("Untitled"),
    jewellery_type: Optional[str] = Form(None),
    jewelry_type: Optional[str] = Form(None),
    wholesaler_id: Optional[str] = Form(None),
    image_count: Optional[int] = Form(None),
    authorization: Optional[str] = Header(default=None),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """Upload an image and start the AI pipeline.

    `image_count` is how many studio images the uploader is paying for (1–4);
    each is priced on the rate card as `product.images_<n>`. A caller that
    sends a session token is charged for it before any work starts, and
    refunded if nothing could be generated.

    A caller with no token (the web, until it sends one) is not charged and
    gets the server's default count. That keeps the web working while it
    catches up, and is the one free path left — close it by requiring a token
    here once the web sends one.
    """
    # A token, when sent, is always verified — never downgraded to anonymous.
    user_id: Optional[str] = None
    if authorization:
        user_id = await resolve_user_id(authorization)
    # Direct fallback in case fields are passed with alternative content headers
    form_data = None
    try:
        form_data = await request.form()
    except Exception:
        pass

    if form_data:
        if not title or title == "Untitled":
            title = form_data.get("title", title)
        if not jewellery_type:
            jewellery_type = form_data.get("jewellery_type") or form_data.get("jewelry_type")
        if not wholesaler_id:
            wholesaler_id = form_data.get("wholesaler_id")

    raw_type = (jewellery_type or jewelry_type or "").strip()

    # --- Input validation (prevents injection attacks & validates category) ---
    try:
        validated = validate_product_input(title=title, jewellery_type=raw_type)
        title = validated.title
        jewellery_type = await validate_jewellery_type_dynamic(validated.jewellery_type)
    except (ValidationError, PydanticValidationError) as exc:
        logger.warning(f"Input validation failed: {exc}")
        raise HTTPException(status_code=422, detail=str(exc))

    # Strip charset suffix from content-type before comparing (e.g. 'image/jpeg; charset=...')
    content_type = (file.content_type or "image/jpeg").split(";")[0].strip()
    if content_type not in settings.ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=415, detail=f"Unsupported file type '{content_type}'"
        )

    raw_bytes = await file.read()
    if len(raw_bytes) > settings.MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=413, detail=f"File too large ({len(raw_bytes):,} bytes)"
        )

    count: Optional[int] = None
    if image_count is not None:
        if not 1 <= image_count <= 4:
            raise HTTPException(status_code=422, detail="Choose between 1 and 4 images.")
        count = image_count

    # A signed-in uploader's product is theirs, whatever the form says.
    if user_id:
        wholesaler_id = user_id

    charge = await _charge_product_upload(
        user_id=user_id,
        image_count=count,
        idempotency_key=idempotency_key,
    )

    try:
        product = await create_product(
            title=title,
            jewellery_type=jewellery_type,
            wholesaler_id=wholesaler_id,
        )
        product_id = product["id"]

        raw_url = upload_raw_image(raw_bytes, product_id, content_type)
        await update_product_image_url(product_id, raw_url)
    except Exception:
        # Paid for, but nothing was started: give it back before failing.
        await _refund_product_upload(charge, reason="upload could not be saved")
        raise
    product = {**product, "image_url": raw_url}

    logger.info(
        "Product created",
        extra={"product_id": product_id, "raw_url": raw_url, "image_count": count,
               "charged": (charge or {}).get("charged")},
    )

    background_tasks.add_task(_run_product_pipeline, product, count, charge)

    return {
        "message": "Uploaded. Processing started in background.",
        "product_id": product_id,
        "raw_image_url": raw_url,
        "image_count": count,
        "charged": (charge or {}).get("charged", 0),
        "balance": (charge or {}).get("balance"),
    }


async def _charge_product_upload(
    *,
    user_id: Optional[str],
    image_count: Optional[int],
    idempotency_key: Optional[str],
) -> Optional[dict]:
    """Debit a product upload before any work starts, or refuse it.

    Returns the spend result, or None when nothing is charged: metering off,
    no signed-in caller, or no count chosen (an older client that doesn't
    know about pricing). Fails closed like `_charge_or_reject`.
    """
    if not settings.CREDITS_ENABLED or user_id is None or image_count is None:
        return None

    # The client's key makes a double-tapped Submit charge once. Without one,
    # every request is its own upload.
    client_key = (idempotency_key or "").strip()[:_MAX_CLIENT_KEY_LEN] or str(uuid.uuid4())
    reference_id = f"{user_id}:{client_key}"
    feature_key = f"product.images_{image_count}"

    try:
        result = await spend_credits(
            user_id=user_id,
            feature_key=feature_key,
            idempotency_key=f"product_upload:{reference_id}",
            reference_type="product_upload",
            reference_id=reference_id,
            metadata={
                "image_count": image_count,
                "cost_paise": settings.COST_PAISE_BACKGROUND_REMOVAL
                + settings.COST_PAISE_IMAGE_GENERATION * image_count,
            },
        )
    except Exception as exc:
        logger.error(f"Credit ledger unreachable for product upload: {exc}", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Could not reach your Treasure Chest just now. Please try again.",
        ) from exc

    if result.get("ok"):
        if result.get("replayed"):
            # Same Submit, already paid for and already started.
            raise HTTPException(status_code=409, detail="This design was already submitted.")
        return result

    if result.get("error") == "INSUFFICIENT_CREDITS":
        raise HTTPException(
            status_code=402,
            detail={
                "error": "INSUFFICIENT_CREDITS",
                "message": "You do not have enough credits for this.",
                "required": result.get("required"),
                "balance": result.get("balance"),
                "short_by": result.get("short_by"),
            },
        )
    if result.get("error") == "UNKNOWN_FEATURE":
        logger.error(f"No price on the rate card for {feature_key}")
        raise HTTPException(status_code=503, detail="Uploading isn't priced yet. Please try again later.")

    logger.error(f"Product upload charge refused: {result}")
    raise HTTPException(status_code=500, detail="Could not charge for this upload. Please try again.")


async def _refund_product_upload(charge: Optional[dict], *, reason: str) -> None:
    """Give back exactly the upload's charge. Never raises: a refund that
    fails is logged loudly for a person to settle, not surfaced to the user."""
    ledger_id = (charge or {}).get("ledger_id")
    if not ledger_id:
        return
    try:
        await refund_debit(ledger_id, reason=reason)
    except Exception:
        logger.error(
            "REFUND FAILED for a product upload — settle by hand",
            extra={"ledger_id": ledger_id, "reason": reason},
            exc_info=True,
        )


@app.post("/process/{image_id}")
@limiter.limit("10/minute")
async def process_image(
    request: Request,
    image_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(default=None),
):
    """Trigger AI processing for an existing product."""
    # --- Validate image_id is a proper UUID (prevents path traversal) ---
    try:
        image_id = validate_product_id(image_id)
    except ValidationError as exc:
        logger.warning(f"Invalid image_id: {exc}")
        raise HTTPException(status_code=422, detail=str(exc))

    product = await fetch_job_by_id(image_id)
    if not product:
        raise HTTPException(status_code=404, detail=f"Product '{image_id}' not found")

    if file is not None:
        content_type = (file.content_type or "image/jpeg").split(";")[0].strip()
        if content_type not in settings.ALLOWED_MIME_TYPES:
            raise HTTPException(
                status_code=415, detail=f"Unsupported file type '{content_type}'"
            )

        raw_bytes = await file.read()
        if len(raw_bytes) > settings.MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=413, detail=f"File too large ({len(raw_bytes):,} bytes)"
            )

        raw_url = upload_raw_image(raw_bytes, image_id, content_type)
        await update_product_image_url(image_id, raw_url)
        product = {**product, "image_url": raw_url}

    elif not product.get("image_url"):
        raise HTTPException(
            status_code=422, detail=f"Product '{image_id}' has no image"
        )

    background_tasks.add_task(_run_product_pipeline, product)
    logger.info("Processing queued", extra={"product_id": image_id})

    return {
        "message": "Processing queued.",
        "product_id": image_id,
        "title": product.get("title"),
        "raw_image_url": product.get("image_url"),
    }


# The frontend polls this to check if generated_image_urls is populated.
# 60/min allows polling every second without hitting the limiter.
@app.get("/product/{product_id}")
@limiter.limit("60/minute")
async def get_product(request: Request, product_id: str):
    """Fetch current state of a product."""
    # --- Validate product_id is a proper UUID ---
    try:
        product_id = validate_product_id(product_id)
    except ValidationError as exc:
        logger.warning(f"Invalid product_id: {exc}")
        raise HTTPException(status_code=422, detail=str(exc))

    product = await fetch_job_by_id(product_id)
    if not product:
        raise HTTPException(status_code=404, detail=f"Product '{product_id}' not found")
    return product


async def _run_product_pipeline(
    product: dict,
    image_count: Optional[int] = None,
    charge: Optional[dict] = None,
) -> None:
    """Background task wrapper for the pipeline.

    If nothing at all could be generated the upload's charge goes back. A
    partial result (some images failed) keeps the charge — see the note in
    `process_product_image`; a per-image refund is not built yet.
    """
    product_id = product["id"]
    try:
        generated_urls = await process_product_image(product, image_count)
        logger.info(
            f"Pipeline finished — {len(generated_urls)} variant(s)",
            extra={"product_id": product_id},
        )
    except Exception as exc:
        logger.error(
            "Product pipeline failed", extra={"product_id": product_id}, exc_info=exc
        )
        await _refund_product_upload(charge, reason="no studio images could be generated")
        # Without this, a failed generation looks identical to a still-processing
        # job forever — GET /product/{id} would keep reporting a pending/blank
        # state to the polling frontend instead of the real failure.
        try:
            await update_job_status(product_id, status="error", error_message=str(exc))
        except Exception:
            logger.error(
                "Failed to persist pipeline failure status",
                extra={"product_id": product_id},
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Chamak AI Jewelry Fusion Endpoints
# ---------------------------------------------------------------------------


@app.post("/api/chamak/analyze", status_code=202)
@limiter.limit("10/minute")
async def chamak_analyze(
    request: Request,
    body: ChamakGenerationRequest,
    background_tasks: BackgroundTasks,
    user_id: Optional[str] = Depends(require_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """Trigger Stage 1 vision analysis for a Chamak jewelry fusion job.

    Analysis is free (`chamak.analyze` is seeded at 0 credits) — it is the hook
    that shows a wholesaler what their designs are worth before asking for
    anything. It still goes through the meter so the path is exercised and
    pricing it later is a one-row UPDATE rather than a code change.
    """
    generation_id = body.generation_id
    row = await fetch_chamak_generation(generation_id)
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"Chamak generation '{generation_id}' not found",
        )
    require_ownership(row, user_id)

    charge = await _charge_or_reject(
        user_id=user_id,
        feature_key="chamak.analyze",
        generation_id=generation_id,
        idempotency_key=idempotency_key,
        metadata={"ai_cost_paise": settings.COST_PAISE_VISION_ANALYSIS},
    )
    # Analysis is free today, so it is never a replay — but the day it is
    # priced, a retried paid analysis must not run twice either.
    if _is_replay(charge):
        return await _replayed_charge_response(
            generation_id, charge, "Chamak vision analysis queued.", restore_done=False
        )

    await _start_job(
        background_tasks, generation_id, charge, run_stage1_vision_analysis,
        status="analyzing",
    )
    logger.info("Chamak vision analysis enqueued", extra={"generation_id": generation_id})

    return {
        "message": "Chamak vision analysis queued.",
        "generation_id": generation_id,
        "status": "analyzing",
    }


@app.post("/api/chamak/generate", status_code=202)
@limiter.limit("10/minute")
async def chamak_generate(
    request: Request,
    body: ChamakGenerationRequest,
    background_tasks: BackgroundTasks,
    user_id: Optional[str] = Depends(require_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """Trigger Stage 3 prompt compilation & Stage 4 image generation.

    This is the paid action. The debit happens here, synchronously, before the
    background task is queued — so a wholesaler who cannot afford it gets a 402
    and we never burn an API call we cannot bill for.
    """
    generation_id = body.generation_id
    row = await fetch_chamak_generation(generation_id)
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"Chamak generation '{generation_id}' not found",
        )
    require_ownership(row, user_id)

    # A re-roll is cheaper because stage 1 was already paid for. The iOS
    # `regenerate` path reuses the SAME row, so the id cannot distinguish the
    # two — but the ledger can. Deriving it server-side means a client cannot
    # simply claim the cheaper price.
    #
    # A retry of the SAME request (same Idempotency-Key) also sees prior > 0,
    # so it asks for the re-roll price — but it never reaches a new debit: the
    # ledger answers `replayed` and no new job starts.
    prior = await count_prior_debits("chamak_generation", generation_id)
    feature_key = "chamak.reroll" if prior > 0 else "chamak.generate"

    charge = await _charge_or_reject(
        user_id=user_id,
        feature_key=feature_key,
        generation_id=generation_id,
        idempotency_key=idempotency_key,
        metadata={
            "ai_cost_paise": settings.COST_PAISE_IMAGE_GENERATION,
            "attempt": prior + 1,
        },
    )
    if _is_replay(charge):
        return await _replayed_charge_response(
            generation_id, charge, "Chamak image generation queued."
        )

    await _start_job(
        background_tasks, generation_id, charge, run_stage4_generation,
        status="generating",
    )
    logger.info("Chamak image generation enqueued", extra={"generation_id": generation_id})

    return {
        "message": "Chamak image generation queued.",
        "generation_id": generation_id,
        "status": "generating",
    }


@app.post("/api/chamak/generate-v2", status_code=202)
@limiter.limit("10/minute")
async def chamak_generate_v2(
    request: Request,
    body: ChamakGenerationRequest,
    background_tasks: BackgroundTasks,
    user_id: Optional[str] = Depends(require_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """Chamak 2.0 — the same fusion, rendered by OpenAI instead of Nanobana.

    Deliberately a near-copy of `chamak_generate`: same ownership check, same
    re-roll pricing rule, same debit-before-work order, same row shape. Only
    the background task differs, so a 1.0 and a 2.0 run on identical inputs
    isolate the renderer as the single changed variable.

    Kept as its own route rather than a flag on `/api/chamak/generate` because
    that route is live and paid, and the whole point of the comparison is that
    1.0's behaviour is not perturbed while it runs.
    """
    generation_id = body.generation_id
    row = await fetch_chamak_generation(generation_id)
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"Chamak generation '{generation_id}' not found",
        )
    require_ownership(row, user_id)

    # Same ledger-derived pricing as 1.0. Debits for both pipelines land on the
    # same generation id, so switching pipelines on a row that has already been
    # generated is correctly priced as a re-roll.
    prior = await count_prior_debits("chamak_generation", generation_id)
    feature_key = "chamak.reroll" if prior > 0 else "chamak.generate"

    charge = await _charge_or_reject(
        user_id=user_id,
        feature_key=feature_key,
        generation_id=generation_id,
        idempotency_key=idempotency_key,
        metadata={
            "ai_cost_paise": settings.COST_PAISE_IMAGE_GENERATION,
            "attempt": prior + 1,
            "pipeline": "chamak_openai",
        },
    )
    if _is_replay(charge):
        return await _replayed_charge_response(
            generation_id, charge, "Chamak 2.0 image generation queued."
        )

    await _start_job(
        background_tasks, generation_id, charge, run_stage4_generation_openai,
        status="generating",
    )
    logger.info(
        "Chamak 2.0 (OpenAI) image generation enqueued",
        extra={"generation_id": generation_id},
    )

    return {
        "message": "Chamak 2.0 image generation queued.",
        "generation_id": generation_id,
        "status": "generating",
    }


@app.post("/api/set-creation/generate", status_code=202)
@limiter.limit("10/minute")
async def set_creation_generate(
    request: Request,
    body: ChamakGenerationRequest,
    background_tasks: BackgroundTasks,
    user_id: Optional[str] = Depends(require_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """Stage two pieces of jewelry into one matched-set photograph.

    Set Creation has no analysis stage — nothing about either piece needs to be
    understood in order to reproduce it — so unlike Fusion there is only this
    one paid call, and the row goes straight from `queued` to `generating`.

    Same debit-before-work rule as Chamak: a wholesaler who cannot afford it
    gets a 402 and we never burn an API call we cannot bill for.
    """
    generation_id = body.generation_id
    row = await fetch_chamak_generation(generation_id)
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"Generation '{generation_id}' not found",
        )
    require_ownership(row, user_id)

    if row.get("mode") != "set_creation":
        raise HTTPException(
            status_code=422,
            detail=(
                "This generation is not a Set Creation row. Use "
                "/api/chamak/generate for fusions."
            ),
        )

    # Two to four pieces. Fusion degrades to a single image when the second is
    # missing; a "set" of one piece is meaningless, so refuse before charging
    # rather than produce something the wholesaler cannot use.
    pieces = len(set_source_urls(row))
    if pieces < SET_MIN_PIECES:
        raise HTTPException(
            status_code=422,
            detail=f"Set Creation needs at least {SET_MIN_PIECES} source images.",
        )

    # Same server-side re-roll pricing as Chamak: derived from the ledger, not
    # claimed by the client. A bigger set costs more: `chamak.set_creation` is
    # the two-piece price, `chamak.set_creation_<n>` the three- and four-piece.
    prior = await count_prior_debits("chamak_generation", generation_id)
    first_key = "chamak.set_creation" if pieces == 2 else f"chamak.set_creation_{pieces}"
    feature_key = "chamak.reroll" if prior > 0 else first_key

    charge = await _charge_or_reject(
        user_id=user_id,
        feature_key=feature_key,
        generation_id=generation_id,
        idempotency_key=idempotency_key,
        metadata={
            "ai_cost_paise": settings.COST_PAISE_IMAGE_GENERATION,
            "attempt": prior + 1,
            "mode": "set_creation",
            "backdrop": row.get("set_backdrop"),
            "pieces": pieces,
        },
    )
    if _is_replay(charge):
        return await _replayed_charge_response(generation_id, charge, "Set creation queued.")

    await _start_job(
        background_tasks, generation_id, charge, run_set_creation_generation,
        status="generating",
    )
    logger.info(
        "Set Creation generation enqueued", extra={"generation_id": generation_id}
    )

    return {
        "message": "Set creation queued.",
        "generation_id": generation_id,
        "status": "generating",
    }


@app.get("/api/chamak/{generation_id}")
@limiter.limit("60/minute")
async def get_chamak_status(
    request: Request,
    generation_id: str,
    user_id: Optional[str] = Depends(require_user),
):
    """Fetch the current status and output of a Chamak generation job."""
    try:
        validated_id = validate_uuid(generation_id, "generation_id")
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    row = await fetch_chamak_generation(validated_id)
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"Chamak generation '{generation_id}' not found",
        )
    require_ownership(row, user_id)
    return row

