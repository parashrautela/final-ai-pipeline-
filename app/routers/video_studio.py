"""Allowlisted, versioned storyboard review; approved storyboards render through the Kling pilot's jobs."""
import copy
import hashlib
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.auth import resolve_user_id
from app.config import settings
from app.db import video_studio as db, videos as videos_db
from app.routers import videos
from app.services import video_prompts, video_studio as studio, video_storage
from app.services.kling import units

router=APIRouter(prefix="/api/video-studio",tags=["Video Studio development"])
limiter=Limiter(key_func=get_remote_address)


class HumanInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    ethnicity: str=Field(min_length=1,max_length=100)
    skin_tone: str=Field(min_length=1,max_length=100)
    appearance: str=Field(default="",max_length=400)

    @field_validator("ethnicity","skin_tone","appearance")
    @classmethod
    def clean(cls,value):
        value=value.strip()
        return value

    @model_validator(mode="after")
    def nonempty(self):
        if not self.ethnicity or not self.skin_tone:
            raise ValueError("Select ethnicity and skin tone separately.")
        return self


class Configuration(BaseModel):
    model_config=ConfigDict(extra="forbid")
    product_id: UUID
    image_indexes: list[int]=Field(min_length=1,max_length=2)
    main_image_index: int=Field(ge=0,strict=True)
    mode: Literal["product_led","human_led"]
    duration: Literal[5,10]
    category: str | None=Field(default=None,max_length=40)
    human: HumanInput | None=None

    @field_validator("duration",mode="before")
    @classmethod
    def integer_duration(cls,value):
        if type(value) is not int:
            raise ValueError("Choose 5 or 10 seconds of development shot timing.")
        return value

    @field_validator("image_indexes",mode="before")
    @classmethod
    def integer_indexes(cls,value):
        if not isinstance(value,list) or any(type(item) is not int or item<0 for item in value):
            raise ValueError("Image indexes must be nonnegative whole numbers.")
        return value

    @model_validator(mode="after")
    def valid_selection(self):
        if len(set(self.image_indexes))!=len(self.image_indexes) or self.main_image_index not in self.image_indexes:
            raise ValueError("Select one or two unique images and mark a selected image as Main.")
        if (self.mode=="human_led") != (self.human is not None):
            raise ValueError("Human-led requires appearance selections; Product-led omits human setup.")
        return self


class ShotInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    panel: int=Field(ge=1,le=9,strict=True)
    direction: str=Field(min_length=1,max_length=400)

    @field_validator("direction")
    @classmethod
    def nonempty(cls,value):
        if not value.strip():
            raise ValueError("Enter a shot direction.")
        return value.strip()


class VersionInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    expected_version: int=Field(ge=1,strict=True)


class UpdateInput(Configuration):
    expected_version: int=Field(ge=1,strict=True)
    shots: list[ShotInput] | None=Field(default=None,min_length=9,max_length=9)


class VideoQuoteInput(VersionInput):
    model: Literal["kling-3.0","kling-2.5-turbo"] | None=None
    style: Literal["simple","match_cut"]="simple"


class VideoGenerateInput(VideoQuoteInput):
    quote_hash: str=Field(pattern=r"^[a-f0-9]{64}$")


def video_ready() -> bool:
    return settings.VIDEO_ENABLED and settings.VIDEO_ALLOW_PAID_GENERATION and bool(settings.KLING_API_KEY)


async def studio_user(authorization: str | None=Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer ") or not authorization[7:].strip():
        raise HTTPException(401,"Sign in to use Video Studio.")
    user=await resolve_user_id(authorization)
    if not user:
        raise HTTPException(401,"Sign in to use Video Studio.")
    allowed={value.strip().lower() for value in settings.VIDEO_STUDIO_USER_IDS.split(",") if value.strip()}
    if not settings.VIDEO_STUDIO_ENABLED or str(user).lower() not in allowed:
        raise HTTPException(403,"Video Studio isn't enabled for this account.")
    return str(user)


async def owned_product(product_id: str, owner: str):
    product=await db.product(product_id,owner)
    if not product:
        raise HTTPException(404,"Product not found.")
    return product


async def owned_project(project_id: str, owner: str):
    project=await db.project(owner,project_id)
    if not project:
        raise HTTPException(404,"Storyboard project not found.")
    await owned_product(project["product_id"],owner)
    return project


def approved(project: dict) -> bool:
    data=project["data"]
    asset=data.get("storyboard")
    approval=data.get("approval")
    return bool(not project.get("active_operation") and asset and approval
                and approval["version"]==project["version"] and approval["asset_id"]==asset["id"]
                and approval["storyboard_sha256"]==asset["sha256"]
                and asset["input_hash"]==studio.storyboard_identity(data))


async def public(project: dict, operation_status: str | None=None) -> dict:
    data=copy.deepcopy(project["data"])
    unavailable=False
    for item in data["references"]+[asset for asset in (data.get("wearing"),data.get("storyboard")) if asset]:
        path=item.pop("path")
        item["preview_url"]=None
        try:
            item["preview_url"]=await video_storage.signed_url(path)
        except Exception:
            unavailable=True
    current_status="pending" if project.get("active_operation") else "needs_review" if data.get("last_error") else "complete"
    return {"id":project["id"],"product_id":project["product_id"],"version":project["version"],
            "created_at":project["created_at"],"updated_at":project["updated_at"],**data,
            "operation_in_progress":bool(project.get("active_operation")),"operation_status":current_status,
            "request_status":operation_status,
            "active_operation_id":project.get("active_operation"),
            "pending_recovery_notice":"Pending or unconfirmed work is never automatically resubmitted. If it does not complete, an operator must reconcile the persisted operation before further edits." if project.get("active_operation") else None,
            "approval_valid":approved(project),"media_unavailable":unavailable,
            "video_generation_available":video_ready(),"video_url":None,
            "live_video_approval_valid":approved(project) and not project["data"]["storyboard"]["is_mock"],
            "development_notice":"Mock assets are labelled and cannot become videos. An approved real storyboard can be rendered with Kling."}


def request_hash(action: str, project_id: str | None, payload: BaseModel) -> str:
    return studio.digest({"action":action,"project_id":project_id,"body":payload.model_dump(mode="json")})


async def replay(owner: str, key: str, fingerprint: str):
    previous=await db.request(owner,key)
    if not previous:
        return None
    if previous["request_hash"]!=fingerprint:
        raise HTTPException(409,{"error":"IDEMPOTENCY_CONFLICT","message":"This key was used with different input."})
    project=await owned_project(previous["project_id"],owner)
    return await public(project,previous["status"])


def checked(result: dict) -> dict:
    error=result.get("error")
    if error:
        raise HTTPException(404 if error=="not_found" else 409,
                            {"error":error.upper(),"message":"Refresh this project and review its current state before trying again."})
    return result


async def begin(project_id: str, owner: str, key: str, fingerprint: str, expected: int,
                action: str, product_id: str, data: dict, render: bool=False):
    try:
        result=await db.rpc("video_studio_begin",{"p_id":project_id,"p_owner":owner,"p_key":key,
            "p_hash":fingerprint,"p_expected":expected,"p_action":action,"p_product":product_id,"p_data":data,"p_render":render})
    except Exception as exc:
        raise HTTPException(503,{"error":"REQUEST_STATUS_UNKNOWN","message":"Read the project or repeat the identical request and Idempotency-Key. Do not submit a new key automatically."}) from exc
    return checked(result)


async def prepare(payload: Configuration, owner: str, project_id: str, previous: dict | None=None,
                  supplied_shots: list[dict] | None=None) -> dict:
    product=await owned_product(str(payload.product_id),owner)
    available={item["index"]:item["url"] for item in studio.images(product)}
    if any(index not in available for index in payload.image_indexes):
        raise HTTPException(422,"Choose completed eligible images belonging to this one catalogue product.")
    kind=studio.category(product.get("jewellery_type") or "")
    if payload.category:
        if payload.category not in studio.PLACEMENTS:
            raise HTTPException(422,"Choose a supported jewellery category.")
        if kind!="other" and payload.category!=kind:
            raise HTTPException(422,"Category must match the imported product category.")
        kind=payload.category
    if payload.mode=="human_led" and kind not in studio.PLACEMENTS:
        raise HTTPException(422,"Correct this product's unknown category before creating a wearing preview.")
    references=[]
    ordered=[payload.main_image_index]+[i for i in payload.image_indexes if i!=payload.main_image_index]
    for index in ordered:
        try:
            image,sha=await video_storage.source(available[index])
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503,"A selected catalogue source could not be read. No image inference was submitted.") from exc
        old=next((item for item in (previous or {}).get("references",[]) if item["image_index"]==index and item["sha256"]==sha),None)
        path=old["path"] if old else f"{owner}/studio/{project_id}/{uuid4()}/source.png"
        if not old:
            try:
                await video_storage.upload(path,image,"image/png")
            except Exception as exc:
                raise HTTPException(503,"A selected source could not be saved privately. No image inference was submitted.") from exc
        references.append({"image_index":index,"role":"main" if index==payload.main_image_index else "supporting",
                           "sha256":sha,"path":path})
    config={"product_id":str(payload.product_id),"image_indexes":payload.image_indexes,"main_image_index":payload.main_image_index,
            "mode":payload.mode,"duration":payload.duration,"category":kind,"placement":studio.PLACEMENTS.get(kind),
            "human":payload.human.model_dump() if payload.human else None,
            "metadata":{"title":product.get("title") or "","imported_category":product.get("jewellery_type") or ""}}
    try:
        plan=studio.shots(payload.duration,supplied_shots)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    data={"config":config,"references":references,"shots":plan,"render_config":studio.render_config(),
          "wearing":None,"storyboard":None,"approval":None,"last_error":None}
    if previous and studio.wearing_identity(previous)==studio.wearing_identity(data):
        data["wearing"]=previous.get("wearing")
    return data


@router.get("/products/{product_id}/options")
@limiter.limit("30/minute")
async def options(request: Request, product_id: UUID, owner: str=Depends(studio_user)):
    product=await owned_product(str(product_id),owner)
    kind=studio.category(product.get("jewellery_type") or "")
    return {"product_id":str(product_id),"title":product.get("title") or "","category":kind,
            "placement":studio.PLACEMENTS.get(kind),"category_corrections":list(studio.PLACEMENTS) if kind=="other" else [],
            "images":studio.images(product),"max_references":2,"panel_count":9,"modes":["product_led","human_led"],
            "planning_durations":[5,10],"timing_scope":"video_duration","supported_video_durations":[5,10],
            "render_mode":settings.VIDEO_STUDIO_RENDER_MODE,"openai_rendering_enabled":settings.VIDEO_STUDIO_ALLOW_OPENAI,
            "video_generation_available":video_ready(),"quote_available":video_ready()}


@router.post("/projects",status_code=201)
@limiter.limit("10/minute")
async def create(request: Request, payload: Configuration, idempotency_key: UUID=Header(), owner: str=Depends(studio_user)):
    key=str(idempotency_key)
    fingerprint=request_hash("create",None,payload)
    previous=await replay(owner,key,fingerprint)
    if previous:
        return previous
    project_id=str(uuid4())
    data=await prepare(payload,owner,project_id)
    result=await begin(project_id,owner,key,fingerprint,0,"create",str(payload.product_id),data)
    # Concurrent duplicate creates may leave unreferenced source snapshots; never delete on an uncertain transaction.
    return await public(result["project"],result["operation_status"])


@router.get("/projects/{project_id}")
async def get(project_id: UUID, owner: str=Depends(studio_user)):
    return await public(await owned_project(str(project_id),owner))


@router.get("/products/{product_id}/projects")
async def history(product_id: UUID, owner: str=Depends(studio_user)):
    await owned_product(str(product_id),owner)
    return {"projects":[await public(project) for project in await db.history(owner,str(product_id))]}


@router.patch("/projects/{project_id}")
@limiter.limit("12/minute")
async def update(request: Request, project_id: UUID, payload: UpdateInput, idempotency_key: UUID=Header(), owner: str=Depends(studio_user)):
    project_id,key=str(project_id),str(idempotency_key)
    fingerprint=request_hash("edit",project_id,payload)
    previous=await replay(owner,key,fingerprint)
    if previous:
        return previous
    project=await owned_project(project_id,owner)
    if str(payload.product_id)!=project["product_id"]:
        raise HTTPException(422,"A storyboard project cannot switch catalogue products.")
    if project["version"]!=payload.expected_version or project.get("active_operation"):
        raise HTTPException(409,"This project changed or has an operation in progress. Refresh it first.")
    plan=[item.model_dump() for item in payload.shots] if payload.shots is not None else project["data"]["shots"]
    data=await prepare(payload,owner,project_id,project["data"],plan)
    result=await begin(project_id,owner,key,fingerprint,payload.expected_version,"edit",project["product_id"],data)
    return await public(result["project"],result["operation_status"])


async def render_project(project_id: str, payload: VersionInput, owner: str, key: str, kind: str):
    fingerprint=request_hash(kind,project_id,payload)
    previous=await replay(owner,key,fingerprint)
    if previous:
        return previous
    project=await owned_project(project_id,owner)
    if project["version"]!=payload.expected_version or project.get("active_operation"):
        raise HTTPException(409,"This project changed or has an operation in progress. Refresh it first.")
    data=copy.deepcopy(project["data"])
    data["render_config"]=studio.render_config()
    if kind=="wearing" and data["config"]["mode"]!="human_led":
        raise HTTPException(422,"Product-led does not use a wearing preview.")
    if kind=="storyboard" and data["config"]["mode"]=="human_led":
        wearing=data.get("wearing")
        if not wearing or wearing["input_hash"]!=studio.wearing_identity(data):
            raise HTTPException(409,"Create a wearing preview for the current references and appearance first.")
        if wearing["is_mock"] and settings.VIDEO_STUDIO_RENDER_MODE!="mock":
            raise HTTPException(409,"Regenerate the mock wearing preview with OpenAI before using OpenAI for the storyboard.")
    try:
        studio.ensure_renderer()
    except ValueError as exc:
        raise HTTPException(403,str(exc)) from exc
    data[kind]=None
    data["storyboard"]=None
    data["approval"]=None
    data["last_error"]=None
    result=await begin(project_id,owner,key,fingerprint,payload.expected_version,kind,project["product_id"],data,True)
    if result["duplicate"]:
        return await public(result["project"],result["operation_status"])
    reserved=result["project"]
    try:
        data[kind]=await studio.render(data,kind,owner,project_id,reserved["version"])
    except Exception:
        data["last_error"]={"error":"RENDER_NEEDS_REVIEW","kind":kind,
                            "message":"Rendering or saving could not be confirmed. This request will not be resubmitted automatically. Inspect it before deliberately trying a new key."}
        try:
            finished=checked(await db.rpc("video_studio_finish",{"p_id":project_id,"p_owner":owner,"p_key":key,"p_data":data,"p_failed":True}))
            return await public(finished["project"],"needs_review")
        except Exception as exc:
            raise HTTPException(503,{"error":"REQUEST_STATUS_UNKNOWN","message":"Read the project or replay this exact request key; do not automatically generate again."}) from exc
    try:
        finished=checked(await db.rpc("video_studio_finish",{"p_id":project_id,"p_owner":owner,"p_key":key,"p_data":data,"p_failed":False}))
    except Exception as exc:
        raise HTTPException(503,{"error":"REQUEST_STATUS_UNKNOWN","message":"The asset may already be saved. Read the project or replay the same request key; do not generate again automatically."}) from exc
    return await public(finished["project"],"complete")


@router.post("/projects/{project_id}/wearing")
@limiter.limit("6/minute")
async def wearing(request: Request, project_id: UUID, payload: VersionInput, idempotency_key: UUID=Header(), owner: str=Depends(studio_user)):
    return await render_project(str(project_id),payload,owner,str(idempotency_key),"wearing")


@router.post("/projects/{project_id}/storyboard")
@limiter.limit("6/minute")
async def storyboard(request: Request, project_id: UUID, payload: VersionInput, idempotency_key: UUID=Header(), owner: str=Depends(studio_user)):
    return await render_project(str(project_id),payload,owner,str(idempotency_key),"storyboard")


@router.post("/projects/{project_id}/approve")
async def approve(project_id: UUID, payload: VersionInput, idempotency_key: UUID=Header(), owner: str=Depends(studio_user)):
    project_id,key=str(project_id),str(idempotency_key)
    fingerprint=request_hash("approve",project_id,payload)
    previous=await replay(owner,key,fingerprint)
    if previous:
        return previous
    project=await owned_project(project_id,owner)
    data=copy.deepcopy(project["data"])
    asset=data.get("storyboard")
    if not asset or asset["version"]!=project["version"] or asset["input_hash"]!=studio.storyboard_identity(data):
        raise HTTPException(409,"Generate and review a storyboard matching the current configuration first.")
    data["approval"]={"version":project["version"],"asset_id":asset["id"],"storyboard_sha256":asset["sha256"],
                      "is_mock":asset["is_mock"],"approved_at":datetime.now(timezone.utc).isoformat()}
    result=await begin(project_id,owner,key,fingerprint,payload.expected_version,"approve",project["product_id"],data)
    return await public(result["project"],result["operation_status"])


async def verified_snapshot(item: dict) -> bytes:
    try:
        image=await video_storage.read_snapshot(item["path"])
    except Exception as exc:
        raise HTTPException(503,"The approved storyboard's images could not be loaded. Please try again.") from exc
    if hashlib.sha256(image).hexdigest()!=item["sha256"]:
        raise HTTPException(409,"An approved image changed. Create and approve a new storyboard.")
    return image


async def kling_job(project_id: str, payload: VideoQuoteInput, owner: str) -> tuple[dict,bytes,bytes | None]:
    """Quote one Kling job for the current approved storyboard, in the pilot's job shape."""
    project=await owned_project(project_id,owner)
    if project["version"]!=payload.expected_version or not approved(project):
        raise HTTPException(409,{"error":"STORYBOARD_APPROVAL_REQUIRED","message":"Approve the current storyboard version first."})
    data=project["data"]
    if data["storyboard"]["is_mock"]:
        raise HTTPException(409,{"error":"MOCK_STORYBOARD_NOT_RENDERABLE","message":"Mock storyboard approval is for development review only and cannot authorize a live video."})
    config=data["config"]
    # Human-led films open on the approved wearing preview; product-led on the Main reference.
    anchor=data["wearing"] if config["mode"]=="human_led" else data["references"][0]
    # Product-led storyboards with two images use image 1 for panels 1-5 and image 2 for 6-9: the film closes on image 2.
    closing=data["references"][1] if config["mode"]!="human_led" and len(data["references"])==2 else None
    model=payload.model or settings.VIDEO_MODEL
    try:
        cost=units(model,config["duration"])
        composed=video_prompts.storyboard(config["category"],data["shots"],model,payload.style,closing is not None)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    image=await verified_snapshot(anchor)
    last_frame=await verified_snapshot(closing) if closing else None
    parts={**composed["parts"],"studio_project_id":project_id,"studio_version":project["version"],"style":payload.style}
    if closing:
        parts["last_frame_sha256"]=closing["sha256"]
    quote={"product_id":project["product_id"],"source_image_url":f"video-studio://{project_id}/v{project['version']}",
           "source_sha256":anchor["sha256"],"model":model,"duration":config["duration"],"shot_id":"storyboard",
           "category":composed["category"],"category_inputs":{},"prompt":composed["prompt"],
           "prompt_parts":parts,"prompt_version":composed["version"],"multi_shot":composed["multi_shot"],
           "estimated_units":str(cost),"resolution":"1080p","audio":"off","budget_key":settings.VIDEO_BUDGET_KEY}
    quote["quote_hash"]=videos.fingerprint(quote)
    return quote,image,last_frame


@router.post("/projects/{project_id}/video/quote")
@limiter.limit("12/minute")
async def video_quote(request: Request, project_id: UUID, payload: VideoQuoteInput, owner: str=Depends(studio_user),
                      pilot: str=Depends(videos.pilot_user)):
    result,_image,_closing=await kling_job(str(project_id),payload,owner)
    return {**result,"budget":await videos_db.budget()}


@router.post("/projects/{project_id}/video",status_code=202)
@limiter.limit("6/minute")
async def video(request: Request, project_id: UUID, payload: VideoGenerateInput, idempotency_key: UUID=Header(),
                owner: str=Depends(studio_user), pilot: str=Depends(videos.pilot_user)):
    key=str(idempotency_key)
    request_hash=videos.fingerprint({"studio_project_id":str(project_id),**payload.model_dump(mode="json")})
    previous=await videos.replay(owner,key,request_hash)
    if previous:
        return previous
    expires=videos.paid_expiry()
    prepared,image,last_frame=await kling_job(str(project_id),payload,owner)
    if prepared.pop("quote_hash")!=payload.quote_hash:
        raise HTTPException(409,"The storyboard, prompt or price changed. Review a fresh quote before generating.")
    return await videos.reserve(prepared,image,owner,key,request_hash,None,expires,last_frame)


@router.get("/projects/{project_id}/videos")
async def project_videos(project_id: UUID, owner: str=Depends(studio_user)):
    await owned_project(str(project_id),owner)
    return {"videos":[await videos.public_job(job) for job in await db.videos(owner,str(project_id))]}
