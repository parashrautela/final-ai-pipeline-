from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Optional

# pyrefly: ignore [missing-import]
import sentry_sdk
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from sentry_sdk.integrations.fastapi import FastApiIntegration
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.config import settings
from app.db.repository import (
    create_product,
    fetch_chamak_generation,
    fetch_job_by_id,
    update_chamak_generation,
    update_job_status,
    update_product_image_url,
)
from app.logging import logger
from app.services.chamak import (
    run_stage1_vision_analysis,
    run_stage4_generation,
)
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
    yield
    task.cancel()
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
):
    """Upload an image and start the AI pipeline."""
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

    product = await create_product(
        title=title,
        jewellery_type=jewellery_type,
        wholesaler_id=wholesaler_id,
    )
    product_id = product["id"]

   
    raw_url = upload_raw_image(raw_bytes, product_id, content_type)
    await update_product_image_url(product_id, raw_url)
    product = {**product, "image_url": raw_url}

    logger.info("Product created", extra={"product_id": product_id, "raw_url": raw_url})

    
    background_tasks.add_task(_run_product_pipeline, product)

    return {
        "message": "Uploaded. Processing started in background.",
        "product_id": product_id,
        "raw_image_url": raw_url,
    }


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


async def _run_product_pipeline(product: dict) -> None:
    """Background task wrapper for the pipeline."""
    product_id = product["id"]
    try:
        generated_urls = await process_product_image(product)
        logger.info(
            f"Pipeline finished — {len(generated_urls)} variant(s)",
            extra={"product_id": product_id},
        )
    except Exception as exc:
        logger.error(
            "Product pipeline failed", extra={"product_id": product_id}, exc_info=exc
        )
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
):
    """Trigger Stage 1 vision analysis for a Chamak jewelry fusion job."""
    generation_id = body.generation_id
    row = await fetch_chamak_generation(generation_id)
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"Chamak generation '{generation_id}' not found",
        )

    await update_chamak_generation(generation_id, {"status": "analyzing"})
    background_tasks.add_task(run_stage1_vision_analysis, generation_id)
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
):
    """Trigger Stage 3 prompt compilation & Stage 4 image generation."""
    generation_id = body.generation_id
    row = await fetch_chamak_generation(generation_id)
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"Chamak generation '{generation_id}' not found",
        )

    await update_chamak_generation(generation_id, {"status": "generating"})
    background_tasks.add_task(run_stage4_generation, generation_id)
    logger.info("Chamak image generation enqueued", extra={"generation_id": generation_id})

    return {
        "message": "Chamak image generation queued.",
        "generation_id": generation_id,
        "status": "generating",
    }


@app.get("/api/chamak/{generation_id}")
@limiter.limit("60/minute")
async def get_chamak_status(request: Request, generation_id: str):
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
    return row

