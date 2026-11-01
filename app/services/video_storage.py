"""Private immutable source/output storage; clients cannot supply arbitrary URLs."""
import asyncio
import hashlib
import io
from urllib.parse import unquote, urlparse

from PIL import Image, UnidentifiedImageError

from app.config import settings
from app.db.repository import get_supabase


def source_location(url: str) -> tuple[str, str]:
    parsed, configured = urlparse(url), urlparse(settings.SUPABASE_URL)
    prefix = "/storage/v1/object/public/"
    local_http = settings.ENVIRONMENT in {"development","test"} and parsed.scheme == "http" and parsed.hostname in {"localhost","127.0.0.1","::1"}
    if not (parsed.scheme == "https" or local_http) or parsed.netloc != configured.netloc or parsed.username or parsed.password or not parsed.path.startswith(prefix):
        raise ValueError("Select an image generated and stored by Jol India.")
    parts = unquote(parsed.path[len(prefix):]).split("/", 1)
    if len(parts) != 2 or parts[0] != settings.PROCESSED_BUCKET_NAME or not parts[1].startswith(settings.PROCESSED_STORAGE_FOLDER + "/") or any(segment in {".","..",""} for segment in parts[1].split("/")):
        raise ValueError("Select a completed studio image, rather than an original upload.")
    return parts[0], parts[1]


def normalize(data: bytes) -> tuple[bytes, str]:
    if not data or len(data) > settings.MAX_FILE_SIZE_BYTES:
        raise ValueError("Choose a generated image smaller than 10 MB.")
    try:
        with Image.open(io.BytesIO(data)) as source:
            width, height = source.size
            if min(width,height) < 300 or width * height > 20_000_000 or max(width,height) / min(width,height) > 2.5:
                raise ValueError("The image needs edges of at least 300 pixels and an aspect ratio between 2:5 and 5:2.")
            if getattr(source,"n_frames",1) != 1 or source.format not in {"PNG","JPEG","WEBP"}:
                raise ValueError("Choose a single PNG, JPEG or WebP studio image.")
            if source.convert("RGBA").getextrema()[3][0] != 255:
                raise ValueError("Choose a finished image with a background, rather than a transparent cut-out.")
            output = io.BytesIO()
            source.convert("RGB").save(output,format="PNG")
            result = output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("This image could not be read. Choose another generated image.") from exc
    if len(result) > settings.MAX_FILE_SIZE_BYTES:
        raise ValueError("The prepared image exceeds the 10 MB pilot limit.")
    return result, hashlib.sha256(result).hexdigest()


async def source(url: str) -> tuple[bytes, str]:
    bucket, path = source_location(url)
    data = await asyncio.to_thread(lambda: get_supabase().storage.from_(bucket).download(path))
    return await asyncio.to_thread(normalize,data)


async def upload(path: str, data: bytes, content_type: str):
    # The SQL migration creates this bucket privately; never create a public fallback.
    def store():
        storage = get_supabase().storage
        bucket = storage.get_bucket(settings.VIDEO_OUTPUT_BUCKET)
        if getattr(bucket,"public",None) is not False:
            raise ValueError("Video source and output storage must be private.")
        storage.from_(settings.VIDEO_OUTPUT_BUCKET).upload(path,data,{"content-type":content_type,"upsert":"true"})
    await asyncio.to_thread(store)


async def snapshot(job_id: str, owner_id: str, data: bytes) -> str:
    path = f"{owner_id}/{job_id}/source.png"
    await upload(path,data,"image/png")
    return path


async def read_snapshot(path: str) -> bytes:
    return await asyncio.to_thread(lambda: get_supabase().storage.from_(settings.VIDEO_OUTPUT_BUCKET).download(path))


async def signed_url(path: str) -> str:
    result = await asyncio.to_thread(lambda: get_supabase().storage.from_(settings.VIDEO_OUTPUT_BUCKET).create_signed_url(path,3600))
    return result["signedURL"]


async def remove_snapshot(path: str):
    await asyncio.to_thread(lambda: get_supabase().storage.from_(settings.VIDEO_OUTPUT_BUCKET).remove([path]))
