"""Isolated local storyboard API: no existing image or video workers start here."""
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.routers.video_studio import router, limiter


@asynccontextmanager
async def lifespan(app):
    if settings.ENVIRONMENT not in {"development","test"} or urlparse(settings.SUPABASE_URL).hostname not in {"localhost","127.0.0.1","::1"}:
        raise RuntimeError("The storyboard-only server requires local Supabase and a development/test environment.")
    yield


app=FastAPI(title="Jewel India — local storyboard development",lifespan=lifespan)
app.include_router(router)
app.state.limiter=limiter
app.add_exception_handler(RateLimitExceeded,_rate_limit_exceeded_handler)


@app.get("/health")
async def health():
    return {"status":"ok","mode":"local-storyboard-only","enabled":settings.VIDEO_STUDIO_ENABLED,
            "render_mode":settings.VIDEO_STUDIO_RENDER_MODE,"video_generation_available":False}
