"""Authenticated, allowlisted local/internal video-conversion pilot."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.auth import resolve_user_id
from app.config import settings
from app.db import videos as db
from app.logging import logger
from app.services import video_prompts, video_storage
from app.services.kling import RATES, PROMPT_LIMITS, units

router = APIRouter(prefix="/api/videos", tags=["Video pilot"])
limiter = Limiter(key_func=get_remote_address)


class VideoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: UUID
    image_index: int = Field(default=0,ge=0,le=3,strict=True)
    duration: Literal[5,10] = 5
    shot_id: Literal["option_1","option_2"] = "option_1"
    model: Literal["kling-3.0","kling-2.5-turbo"] | None = None
    category_inputs: dict[str,str] = Field(default_factory=dict,max_length=2)
    note: str = Field(default="",max_length=400)
    parent_id: UUID | None = None

    @field_validator("duration",mode="before")
    @classmethod
    def strict_duration(cls,value):
        if type(value) is not int:
            raise ValueError("Duration must be 5 or 10 whole seconds.")
        return value


class GenerateInput(VideoInput):
    quote_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


def fingerprint(data: dict) -> str:
    return hashlib.sha256(json.dumps(data,sort_keys=True,separators=(",",":")).encode()).hexdigest()


async def pilot_user(authorization: str | None = Header(default=None)) -> str:
    # Unlike legacy image routes, video never permits anonymous unmetered access.
    if not authorization or not authorization.lower().startswith("bearer ") or not authorization[7:].strip():
        raise HTTPException(401,"Sign in to create a video.")
    user_id = await resolve_user_id(authorization)
    if not user_id:
        raise HTTPException(401,"Sign in to create a video.")
    allowed = {value.strip().lower() for value in settings.VIDEO_PILOT_USER_IDS.split(",") if value.strip()}
    if not settings.VIDEO_ENABLED or ("*" not in allowed and str(user_id).lower() not in allowed):
        raise HTTPException(403,"Video conversion is not enabled for this account yet.")
    return str(user_id)


async def own_product(product_id: str, owner_id: str) -> dict:
    product = await db.product(product_id,owner_id)
    if not product:
        raise HTTPException(404,"Product not found.")
    return product


def generated_images(product: dict) -> list[str]:
    images = product.get("generated_image_urls")
    if not isinstance(images,list):
        return []
    # Never fall back to image_url/raw_image_url; those may be unprocessed uploads.
    return [url for url in images[:4] if isinstance(url,str) and url]


async def own_job(job_id: str, owner_id: str) -> dict:
    job = await db.find(owner_id,job_id=job_id)
    if not job:
        raise HTTPException(404,"Video not found.")
    return job


async def public_job(job: dict) -> dict:
    keys = ("id","product_id","parent_id","model","duration","shot_id","multi_shot","category","category_inputs",
            "prompt_version","estimated_units","billed_units","status","error_code","message","created_at","updated_at")
    result = {key:job.get(key) for key in keys}
    result.update(source_url=None,video_url=None,media_unavailable=False)
    try:
        result["source_url"] = await video_storage.signed_url(job["source_path"])
        if job.get("output_path") and job["status"] == "ready":
            result["video_url"] = await video_storage.signed_url(job["output_path"])
    except Exception:
        result["media_unavailable"] = True
        logger.warning("Video preview URL unavailable",extra={"video_id":job["id"]})
    result["can_cancel"] = job["status"] == "queued"
    result["billing_confirmed"] = job.get("billed_units") is not None
    return result


async def prepare(payload: VideoInput, owner_id: str) -> tuple[dict,bytes]:
    product = await own_product(str(payload.product_id),owner_id)
    images = generated_images(product)
    state = product.get("ai_processing_state")
    if not images or state and state not in {"ready","done","completed"}:
        raise HTTPException(409,"Generate a studio image first, then convert one of its completed outputs into a video.")
    if payload.image_index >= len(images):
        raise HTTPException(422,"Choose one of the available generated images.")
    if payload.parent_id:
        parent = await own_job(str(payload.parent_id),owner_id)
        if parent["product_id"] != str(payload.product_id):
            raise HTTPException(422,"The previous attempt belongs to another product.")
    model = payload.model or settings.VIDEO_MODEL
    try:
        cost = units(model,payload.duration)
        composed = video_prompts.compose(product.get("jewellery_type") or "other",payload.shot_id,payload.category_inputs,payload.note,payload.duration,model)
        if composed["multi_shot"] and model != "kling-3.0":
            raise ValueError("Energetic editorial uses a storyboard. Choose Kling 3.0 for this option.")
        image, image_hash = await video_storage.source(images[payload.image_index])
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503,"The selected studio image could not be loaded. Please try again.") from exc
    quote = {"product_id":str(payload.product_id),"source_image_url":images[payload.image_index],"source_sha256":image_hash,
             "model":model,"duration":payload.duration,"shot_id":payload.shot_id,"category":composed["category"],
             "category_inputs":composed["inputs"],"prompt":composed["prompt"],"prompt_parts":composed["parts"],
             "prompt_version":composed["version"],"multi_shot":composed["multi_shot"],"estimated_units":str(cost),"resolution":"1080p","audio":"off",
             "budget_key":settings.VIDEO_BUDGET_KEY}
    quote["quote_hash"] = fingerprint(quote)
    return quote,image


@router.get("/products/{product_id}/options")
@limiter.limit("20/minute")
async def options(request: Request, product_id: UUID, user_id: str = Depends(pilot_user)):
    product = await own_product(str(product_id),user_id)
    images = generated_images(product)
    kind = video_prompts.category(product.get("jewellery_type") or "other")
    shots = []
    for item in video_prompts.shot_options():
        preview = item["preview_url"]
        if not preview and settings.ENVIRONMENT in {"development","test"} and (Path(settings.VIDEO_LOCAL_REFERENCE_DIR)/(item["id"]+".mp4")).is_file():
            preview = "/api/videos/references/"+item["id"]
        shots.append({"id":item["id"],"label":item["label"],"preview_url":preview,"reference_pending":not bool(preview),
                      "multi_shot":item["multi_shot"],"supported_models":["kling-3.0"] if item["multi_shot"] else list(RATES)})
    return {"product_id":str(product_id),"eligible":bool(images) and product.get("ai_processing_state") in (None,"ready","done","completed"),
            "images":[{"index":i,"url":url} for i,url in enumerate(images)],"category":kind,
            "fields":video_prompts.form_fields(kind),"shots":shots,"durations":[5,10],"default_duration":5,
            "default_model":settings.VIDEO_MODEL,"resolution":"1080p","audio":"off",
            "models":[{"id":model,"units_per_second":str(rate),"max_prompt_characters":PROMPT_LIMITS[model]} for model,rate in RATES.items()],
            "budget":await db.budget(),"pack_expires_at":settings.VIDEO_PACK_EXPIRES_AT or None,
            "help":"Generate a studio image first. Select one output, choose a shot and duration, then review the cost.",
            "pricing_scope":"internal_provider_units","customer_wallet_charged":False}


@router.get("/references/{shot_id}")
async def reference(shot_id: Literal["option_1","option_2"],user_id: str = Depends(pilot_user)):
    path = Path(settings.VIDEO_LOCAL_REFERENCE_DIR)/(shot_id+".mp4")
    if settings.ENVIRONMENT not in {"development","test"} or not path.is_file():
        raise HTTPException(404,"Reference preview not available.")
    return FileResponse(path,media_type="video/mp4",headers={"Cache-Control":"private, no-store"})


@router.post("/quote")
@limiter.limit("12/minute")
async def quote(request: Request, payload: VideoInput, user_id: str = Depends(pilot_user)):
    result,_ = await prepare(payload,user_id)
    # Prompt text is available to internal testers, never provider credentials.
    return {**result,"budget":await db.budget(),"customer_wallet_charged":False}


async def replay(user_id: str, key: str, request_hash: str) -> dict | None:
    existing = await db.find(user_id,key=key)
    if not existing:
        return None
    if existing["request_hash"] != request_hash:
        raise HTTPException(409,"This request key was already used with different settings.")
    return await public_job(existing)


def paid_expiry() -> datetime:
    if not settings.VIDEO_ALLOW_PAID_GENERATION:
        raise HTTPException(403,"Paid video generation is disabled. You can review settings and quotes without spending units.")
    if not settings.KLING_API_KEY:
        raise HTTPException(503,"Configure the server's Kling API key before starting a paid test.")
    try:
        expires = datetime.fromisoformat(settings.VIDEO_PACK_EXPIRES_AT.replace("Z","+00:00"))
        if expires.tzinfo is None:
            raise ValueError("Timezone required")
    except ValueError as exc:
        raise HTTPException(503,"Configure the purchased resource pack's expiry, including its timezone.") from exc
    if expires <= datetime.now(timezone.utc):
        raise HTTPException(409,"The configured video resource pack has expired.")
    return expires


async def reserve(prepared: dict, image: bytes, user_id: str, key: str, request_hash: str,
                  parent_id: str | None, expires: datetime, last_frame: bytes | None = None) -> dict:
    """Snapshot the source (and optional closing frame) and reserve units for one queued Kling job.

    Shared with the storyboard studio. The closing frame's private path rides in
    `prompt_parts` next to its quoted hash, so the worker can verify it.
    """
    available = await db.budget()
    if float(available["available_units"]) < float(prepared["estimated_units"]):
        raise HTTPException(402,"There are not enough unreserved trial units for this video.")
    job_id = str(uuid4())
    snapshots = []
    try:
        source_path = await video_storage.snapshot(job_id,user_id,image)
        snapshots.append(source_path)
        if last_frame is not None:
            last_path = f"{user_id}/{job_id}/last_frame.png"
            await video_storage.upload(last_path,last_frame,"image/png")
            snapshots.append(last_path)
            prepared = {**prepared,"prompt_parts":{**prepared["prompt_parts"],"last_frame_path":last_path}}
    except Exception as exc:
        raise HTTPException(503,"The source image could not be saved privately. No generation was submitted.") from exc
    job = {**prepared,"id":job_id,"owner_id":user_id,"parent_id":parent_id,
           "idempotency_key":key,"request_hash":request_hash,"source_path":source_path}
    try:
        result = await db.rpc("video_reserve",{"p_job":job,"p_limit":settings.VIDEO_BUDGET_UNITS,"p_expires":expires.isoformat()})
    except Exception as exc:
        # The transaction may have committed. Keep the snapshot; replaying the
        # same Idempotency-Key recovers it without another paid attempt.
        raise HTTPException(503,{"error":"REQUEST_STATUS_UNKNOWN","message":"Check this attempt by repeating the same request with the same Idempotency-Key."}) from exc
    errors = {"idempotency_conflict":(409,"This request key was already used with different settings."),
              "pack_expired":(409,"The video resource pack has expired."),
              "budget_exhausted":(402,"There are not enough unreserved trial units for this video.")}
    if result.get("error") or result["id"] != job_id:
        for path in snapshots:
            try:
                await video_storage.remove_snapshot(path)
            except Exception:
                logger.warning("Unused video source snapshot needs cleanup",extra={"video_id":job_id})
    if result.get("error") in errors:
        status,message = errors[result["error"]]
        raise HTTPException(status,message)
    return await public_job(result)


@router.post("/generate",status_code=202)
@limiter.limit("6/minute")
async def generate(request: Request, payload: GenerateInput, idempotency_key: UUID = Header(), user_id: str = Depends(pilot_user)):
    request_hash = fingerprint(payload.model_dump(mode="json"))
    previous = await replay(user_id,str(idempotency_key),request_hash)
    if previous:
        return previous
    expires = paid_expiry()
    prepared,image = await prepare(payload,user_id)
    if prepared.pop("quote_hash") != payload.quote_hash:
        raise HTTPException(409,"The image, prompt or price changed. Review a fresh quote before generating.")
    return await reserve(prepared,image,user_id,str(idempotency_key),request_hash,
                         str(payload.parent_id) if payload.parent_id else None,expires)


@router.get("/products/{product_id}/history")
async def history(product_id: UUID,user_id: str = Depends(pilot_user)):
    await own_product(str(product_id),user_id)
    return {"videos":[await public_job(job) for job in await db.history(user_id,str(product_id))]}


@router.get("/{job_id}")
async def status(job_id: UUID,user_id: str = Depends(pilot_user)):
    return await public_job(await own_job(str(job_id),user_id))


@router.post("/{job_id}/cancel")
async def cancel(job_id: UUID,user_id: str = Depends(pilot_user)):
    job = await own_job(str(job_id),user_id)
    if job["status"] == "cancelled":
        return await public_job(job)
    result = await db.rpc("video_cancel",{"p_id":str(job_id),"p_owner":user_id})
    if not result:
        raise HTTPException(409,"This video has already started and cannot be cancelled. You can leave and return later.")
    return await public_job(result)
