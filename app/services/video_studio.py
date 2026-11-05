"""Small storyboard renderer. OpenAI is explicit opt-in; mocks never depict a generated human."""
import asyncio
import hashlib
import io
import json
from textwrap import wrap
from uuid import uuid4

from PIL import Image, ImageDraw, ImageOps

from app.config import settings
from app.services import video_storage
from app.services.chamak_prompt_v3 import normalize_jewellery_type
from app.jewellery_types import normalize_chain_type

PLACEMENTS = {
    "necklace":"around the neck", "choker":"around the neck", "long_necklace":"around the neck",
    "pendant":"on its chain around the neck", "mangalsutra":"around the neck",
    "earrings":"on the earlobes", "jhumka":"on the earlobes", "ring":"on a finger",
    "bangle":"around the wrist", "kada":"around the wrist", "bracelet":"around the wrist",
    "chain":"around the neck", "maang_tikka":"at the central hair part and forehead",
    "nose_ring":"at the nostril", "anklet":"around the ankle",
}
PRESERVATION = (
    "These reference images show ONE existing jewellery product, never designs to blend. "
    "Image 1 is the MAIN immutable product identity; another catalogue reference shows the SAME product, possibly in another scene. "
    "Preserve visible product count, metal tone, stone count/colours/settings, silhouette, proportions, links and details. "
    "Never redesign, merge products, invent unseen geometry, duplicate jewellery or add other jewellery. "
    "Prefer views supported by the references. Product accuracy overrides style and motion."
)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()


def render_config() -> dict:
    return {"provider":settings.VIDEO_STUDIO_RENDER_MODE,"model":settings.OPENAI_IMAGE_MODEL,
            "size":settings.OPENAI_IMAGE_SIZE,"prompt_version":"video-studio-v2"}


def category(raw: str) -> str:
    return "chain" if normalize_chain_type(raw or "") == "chain" else normalize_jewellery_type(raw) or "other"


def images(product: dict) -> list[dict]:
    if product.get("ai_processing_state") not in (None,"ready","done","completed"):
        return []
    urls = product.get("generated_image_urls")
    if not isinstance(urls,list):
        return []
    result = []
    for index,url in enumerate(urls):
        if not isinstance(url,str) or not url:
            continue
        try:
            video_storage.source_location(url)
        except ValueError:
            continue
        result.append({"index":index,"url":url})
    return result


def shots(duration: int, supplied: list[dict] | None = None) -> list[dict]:
    directions = [
        "Establish the full product from the main visible view.",
        "Slow restrained front lateral camera movement; full product remains visible.",
        "Move slightly closer without revealing unsupported hidden surfaces.",
        "Inspect the most distinctive visible stone or motif.",
        "Macro detail of visible craftsmanship; no invented facets.",
        "Show existing connections and construction from a supported angle.",
        "Return smoothly to the original full-product framing.",
        "A restrained hero view with controlled natural highlights.",
        "Finish on a stable complete product view.",
    ]
    if supplied is not None:
        if [item["panel"] for item in supplied] != list(range(1,10)):
            raise ValueError("Provide exactly nine shots numbered 1 through 9 in order.")
        directions = [item["direction"] for item in supplied]
    return [{"panel":index+1,"direction":direction,"start_seconds":round(duration*index/9,4),
             "end_seconds":round(duration*(index+1)/9,4)} for index,direction in enumerate(directions)]


def wearing_identity(data: dict) -> str:
    config=data["config"]
    return digest({"references":[{k:item[k] for k in ("image_index","role","sha256")} for item in data["references"]],
                   "mode":config["mode"],"category":config["category"],"human":config["human"],
                   "placement":config["placement"],"render_config":data["render_config"]})


def storyboard_identity(data: dict) -> str:
    return digest({"wearing_identity":wearing_identity(data),"wearing":data.get("wearing"),
                   "duration":data["config"]["duration"],"shots":data["shots"]})


def prompt(data: dict, kind: str) -> str:
    config=data["config"]
    text=PRESERVATION+"\nCategory: "+config["category"]+".\n"
    if config["mode"] == "human_led":
        human=config["human"]
        text+=("Create an adult human wearing only this supplied product, correctly placed "+config["placement"]+". "
               "Appearance selections are independent: "+json.dumps(human,ensure_ascii=False)+". "
               "Do not infer ethnicity from skin tone. Keep anatomy and jewellery attachment physically plausible. ")
    elif len(data["references"]) == 2 and kind == "storyboard":
        # Both chosen catalogue images are used: the film starts in Image 1's scene and moves to Image 2's.
        text+=("Panels 1-5 use Image 1's product arrangement, scene, display, background and palette. "
               "Panels 6-9 use Image 2's product arrangement, scene, display, background and palette. "
               "The jewellery is the identical piece in all nine panels. No human or new body parts. ")
    else:
        text+="Retain the MAIN image's product arrangement, scene, display, background and palette. No human or new body parts. "
    if kind == "wearing":
        return text+"Create ONE photorealistic wearing-preview image, not a collage. Keep the complete visible jewellery readable."
    if config["mode"] == "human_led":
        text+="The LAST reference is the selected wearing preview: keep that same human, appearance, clothing, lighting and scene in every panel. "
    plan="\n".join(f"Panel {item['panel']} ({item['start_seconds']}-{item['end_seconds']}s): {item['direction']}" for item in data["shots"])
    return (text+"Create ONE storyboard contact-sheet image in a 3 by 3 grid with EXACTLY NINE equally sized panels, "
            "numbered 1 through 9 in reading order. This is a shot-planning preview, not a video. "
            "Only small panel numbers may be added; no logos or marketing text. Plan for "+str(config["duration"])+" seconds.\n"+plan)


def mock_image(source: bytes, data: dict, kind: str) -> bytes:
    """Visible test fixtures: reference tiles, not AI-generated people or scenes."""
    with Image.open(io.BytesIO(source)) as photo:
        tile=ImageOps.contain(photo.convert("RGB"),(470,380))
    canvas=Image.new("RGB",(1536,1536) if kind=="storyboard" else (1024,1024),"#eee9df")
    draw=ImageDraw.Draw(canvas)
    draw.text((25,18),"MOCK DEVELOPMENT PREVIEW - NO INFERENCE",fill="black")
    if kind=="wearing":
        canvas.paste(tile,((1024-tile.width)//2,200))
        draw.text((25,700),"Wearing placeholder: no human was generated.",fill="black")
    else:
        for item in data["shots"]:
            index=item["panel"]-1
            x,y=(index%3)*512,(index//3)*490+55
            canvas.paste(tile,(x+(512-tile.width)//2,y+20))
            draw.text((x+15,y),f"MOCK PANEL {index+1}",fill="black")
            for line_index,line in enumerate(wrap(item["direction"],55)[:3]):
                draw.text((x+15,y+410+line_index*17),line,fill="black")
    output=io.BytesIO()
    canvas.save(output,"PNG")
    return output.getvalue()


def ensure_renderer():
    if settings.VIDEO_STUDIO_RENDER_MODE=="openai" and (not settings.VIDEO_STUDIO_ALLOW_OPENAI or not settings.OPENAI_API_KEY):
        raise ValueError("OpenAI storyboard rendering is disabled or its server key is not configured.")


async def render(data: dict, kind: str, owner_id: str, project_id: str, version: int) -> dict:
    ensure_renderer()
    references=[await video_storage.read_snapshot(item["path"]) for item in data["references"]]
    if any(hashlib.sha256(image).hexdigest()!=item["sha256"] for image,item in zip(references,data["references"])):
        raise ValueError("A saved product reference changed; no inference was submitted.")
    if settings.VIDEO_STUDIO_RENDER_MODE=="mock":
        output=await asyncio.to_thread(mock_image,references[0],data,kind)
    else:
        if kind=="storyboard" and data["config"]["mode"]=="human_led":
            if data["wearing"]["is_mock"]:
                raise ValueError("A mock wearing preview cannot be used for OpenAI rendering. Regenerate the wearing preview first.")
            wearing=await video_storage.read_snapshot(data["wearing"]["path"])
            if hashlib.sha256(wearing).hexdigest()!=data["wearing"]["sha256"]:
                raise ValueError("The wearing preview changed; no inference was submitted.")
            references.append(wearing)
        # Import only the existing OpenAI renderer; never dispatch by legacy image provider.
        from app.services.ai import OpenAIImageClient
        output=await OpenAIImageClient().fuse_images([(item,"image/png") for item in references],
                prompt=prompt(data,kind),size=data["render_config"]["size"],model=data["render_config"]["model"],max_retries=0)
    output,sha=await asyncio.to_thread(video_storage.normalize,output)
    asset_id=str(uuid4())
    path=f"{owner_id}/studio/{project_id}/{asset_id}/{kind}.png"
    await video_storage.upload(path,output,"image/png")
    return {"id":asset_id,"path":path,"sha256":sha,"provider":settings.VIDEO_STUDIO_RENDER_MODE,
            "is_mock":settings.VIDEO_STUDIO_RENDER_MODE=="mock","version":version,
            "input_hash":wearing_identity(data) if kind=="wearing" else storyboard_identity(data)}
