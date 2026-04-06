import asyncio
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.config import settings
from app.db.repository import create_product, fetch_job_by_id, update_product_image_url
from app.logging import logger
from app.services.pipeline import process_product_image
from app.services.storage import upload_raw_image
from app.validation import (
    ValidationError,
    validate_product_id,
    validate_product_input,
)
from app.worker import worker_loop

# Rate limiter keyed on client IP. Each route sets its own cap;
# the default here is a fallback for any route we forget to decorate.
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])


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

# Whitelist only the origins we actually own. Wildcard would be simpler but
# breaks cookies/credentials on the frontend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://ai-pipeline-frontend.vercel.app",
        "https://jwellery.arpitray.in",
        "https://jewel-india-frontend-yws1.vercel.app",
    ],
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
        response.headers["Access-Control-Allow-Origin"] = (
            "https://ai-pipeline-frontend.vercel.app"
        )
        response.headers["Access-Control-Allow-Credentials"] = "true"

    return response


@app.get("/health")
@limiter.limit("30/minute")
async def health_check(request: Request):
    return {"status": "ok", "environment": settings.ENVIRONMENT}


# 5 requests/minute per IP — the pipeline is expensive (Reve + 4x Nanobana),
# so we keep this tight to avoid runaway costs from a single client.
@app.post("/process", status_code=202)
@limiter.limit("5/minute")
async def process_upload(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(
        ..., description="Raw jewellery image (JPEG/PNG/WebP, max 10MB)"
    ),
    title: str = "Untitled",
    jewellery_type: str = "",
):
    """Upload an image and start the AI pipeline."""
    # --- Input validation (prevents injection attacks) ---
    try:
        validated = validate_product_input(title=title, jewellery_type=jewellery_type)
        title = validated.title
        jewellery_type = validated.jewellery_type
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

    product = await create_product(title=title, jewellery_type=jewellery_type)
    product_id = product["id"]

    # Store the raw image first so the product row always has something
    # to show while the AI pipeline is still running.
    raw_url = upload_raw_image(raw_bytes, product_id, content_type)
    await update_product_image_url(product_id, raw_url)
    product = {**product, "image_url": raw_url}

    logger.info("Product created", extra={"product_id": product_id, "raw_url": raw_url})

    # Fire and forget — respond immediately, pipeline runs in background.
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
