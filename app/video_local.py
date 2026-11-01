"""Video-only local entry point; never starts image/backfill/manufacturing workers."""
import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.routers.videos import router, limiter
from app.services.video_pipeline import worker_loop


@asynccontextmanager
async def lifespan(app):
    if settings.ENVIRONMENT not in {"development","test"} or urlparse(settings.SUPABASE_URL).hostname not in {"localhost","127.0.0.1","::1"}:
        raise RuntimeError("The local video server requires a local Supabase URL and a development/test environment.")
    task = None
    if settings.VIDEO_ENABLED and settings.VIDEO_ALLOW_PAID_GENERATION and settings.KLING_API_KEY and settings.VIDEO_PILOT_USER_IDS.strip():
        task = asyncio.create_task(worker_loop())
    try:
        yield
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)


app = FastAPI(title="Jol India — local video pilot",lifespan=lifespan)
app.include_router(router)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded,_rate_limit_exceeded_handler)
app.add_middleware(CORSMiddleware,allow_origins=["http://localhost:3000","http://127.0.0.1:3000"],allow_credentials=True,allow_methods=["GET","POST"],allow_headers=["Authorization","Content-Type","Idempotency-Key"])


@app.get("/health")
async def health():
    return {"status":"ok","mode":"local-video-only"}
