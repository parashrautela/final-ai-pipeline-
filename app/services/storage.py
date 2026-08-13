from __future__ import annotations

from urllib.parse import urlparse

import httpx

from app.config import settings
from app.db.repository import get_supabase
from app.logging import logger


def _ensure_bucket(bucket_name: str, *, public: bool = True) -> None:
    """Create bucket if it doesn't exist."""
    try:
        sb = get_supabase()
        existing = [b.name for b in sb.storage.list_buckets()]
        if bucket_name not in existing:
            sb.storage.create_bucket(bucket_name, options={"public": public})
            logger.info(f"Created storage bucket '{bucket_name}' (public={public})")
    except Exception as exc:
        logger.warning(f"Could not verify/create bucket '{bucket_name}': {exc}")


def upload_chamak_output(file_content: bytes, wholesaler_id: str, generation_id: str) -> str:
    """Upload generated Chamak output image to private bucket chamak-outputs.

    Returns the relative path inside the bucket: '{wholesaler_id}/{generation_id}.png'
    """
    bucket = settings.CHAMAK_OUTPUT_BUCKET
    path = f"{wholesaler_id}/{generation_id}.png"
    try:
        _ensure_bucket(bucket, public=False)
        sb = get_supabase()
        sb.storage.from_(bucket).upload(
            path=path,
            file=file_content,
            file_options={"content-type": "image/png", "upsert": "true"},
        )
        logger.info(
            f"Uploaded chamak output to private bucket: {bucket}/{path}",
            extra={"wholesaler_id": wholesaler_id, "generation_id": generation_id},
        )
        return path
    except Exception as exc:
        logger.error(
            f"Failed to upload chamak output {bucket}/{path}: {exc}",
            extra={"wholesaler_id": wholesaler_id, "generation_id": generation_id},
            exc_info=exc,
        )
        raise


async def fetch_image_bytes_and_content_type(image_url_or_path: str) -> tuple[bytes, str]:
    """Resolve an image URL or Supabase storage path to (bytes, mime_type)."""
    # 1. If it's a standard HTTP/HTTPS URL
    if image_url_or_path.startswith("http://") or image_url_or_path.startswith("https://"):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(image_url_or_path, timeout=30.0, follow_redirects=True)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
                if not content_type.startswith("image/"):
                    content_type = "image/jpeg"
                return resp.content, content_type
        except Exception as http_exc:
            logger.warning(f"Direct HTTP fetch failed for {image_url_or_path}, checking storage: {http_exc}")
            # If HTTP fetch fails, attempt to extract bucket and path if it's a Supabase URL
            bucket, path = _storage_path_from_image_url(image_url_or_path)
            data = download_from_storage(bucket, path)
            ext = path.split(".")[-1].lower() if "." in path else "jpg"
            mime = "image/png" if ext == "png" else ("image/webp" if ext == "webp" else "image/jpeg")
            return data, mime

    # 2. If it's a storage path like 'bucket/path' or 'path'
    if "/" in image_url_or_path:
        parts = image_url_or_path.split("/", 1)
        bucket = parts[0]
        path = parts[1]
        try:
            data = download_from_storage(bucket, path)
            ext = path.split(".")[-1].lower() if "." in path else "jpg"
            mime = "image/png" if ext == "png" else ("image/webp" if ext == "webp" else "image/jpeg")
            return data, mime
        except Exception:
            pass

    # Fallback to RAW_BUCKET_NAME
    data = download_from_storage(settings.RAW_BUCKET_NAME, image_url_or_path)
    return data, "image/jpeg"

        
def _storage_path_from_image_url(image_url: str) -> tuple[str, str]:
    """Parse bucket and path from a Supabase storage URL or relative path."""
    # Supabase public URLs look like:
    # https://<project>.supabase.co/storage/v1/object/public/<bucket>/<path>
    # We extract the bucket and path so we can download via the SDK.
    parsed = urlparse(image_url)
    if parsed.scheme in ("http", "https") and "/storage/v1/object/" in parsed.path:
        parts = parsed.path.split("/storage/v1/object/")[-1].split("/", 2)
        if len(parts) >= 3:
            return parts[1], parts[2]
    # Treat anything else as a raw path in the default raw bucket.
    return settings.RAW_BUCKET_NAME, image_url


async def download_image(url: str) -> bytes:
    """Download image from public URL with validation."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=30.0, follow_redirects=True)
            response.raise_for_status()

            content_type = response.headers.get("content-type", "").split(";")[0].strip()
            if content_type not in settings.ALLOWED_MIME_TYPES:
                raise ValueError(f"Unsupported MIME type: {content_type!r}")

            content = response.content
            if len(content) > settings.MAX_FILE_SIZE_BYTES:
                raise ValueError(f"File too large: {len(content)} bytes")

            return content
    except Exception as exc:
        logger.error(f"Failed to download image from {url}: {exc}")
        raise


def download_from_storage(bucket: str, path: str) -> bytes:
    """Download object from Supabase Storage."""
    try:
        sb = get_supabase()
        data = sb.storage.from_(bucket).download(path)
        logger.info(f"Downloaded {path!r} from bucket {bucket!r} ({len(data)} bytes)")
        return data
    except Exception as exc:
        logger.error(f"Storage download failed — bucket={bucket!r} path={path!r}: {exc}")
        raise


def resolve_product_image(product: dict) -> bytes:
    """Download raw image for a product record."""
    product_id = product["id"]
    image_url = product.get("image_url")

    if image_url:
        bucket, path = _storage_path_from_image_url(image_url)
        logger.info(f"Resolving image: bucket={bucket!r} path={path!r}", extra={"product_id": product_id})
        return download_from_storage(bucket, path)

    fallback_path = f"{settings.RAW_STORAGE_FOLDER}/{product_id}"
    logger.warning(f"image_url is NULL, trying fallback: {fallback_path}", extra={"product_id": product_id})
    return download_from_storage(settings.RAW_BUCKET_NAME, fallback_path)


def upload_file_to_storage(content: bytes, bucket: str, path: str, content_type: str = "image/png") -> str:
    """Upload bytes to bucket and return public URL."""
    try:
        _ensure_bucket(bucket)
        sb = get_supabase()
        # x-upsert: true — overwrite silently if the file already exists.
        # This lets us re-run the pipeline for the same product without errors.
        sb.storage.from_(bucket).upload(
            path=path,
            file=content,
            file_options={"content-type": content_type, "x-upsert": "true"},
        )
        # Construct the public URL manually — the SDK doesn't return it on upload.
        public_url = f"{settings.SUPABASE_URL}/storage/v1/object/public/{bucket}/{path}"
        logger.info(f"Uploaded to storage: {public_url}")
        return public_url
    except Exception as exc:
        logger.error(f"Failed to upload to storage {bucket}/{path}: {exc}")
        raise


def upload_processed_image(file_content: bytes, product_id: str) -> str:
    """Upload processed image to storage."""
    return upload_file_to_storage(
        file_content,
        settings.PROCESSED_BUCKET_NAME,
        f"{settings.PROCESSED_STORAGE_FOLDER}/{product_id}.png",
        content_type="image/png",
    )


def upload_processed_image_variant(file_content: bytes, product_id: str, variant_index: int) -> str:
    """Upload one of the 4 generated image variants."""
    path = f"{settings.PROCESSED_STORAGE_FOLDER}/{product_id}_v{variant_index}.png"
    logger.info(f"Uploading variant {variant_index}/4: {path}", extra={"product_id": product_id})
    return upload_file_to_storage(
        file_content,
        settings.PROCESSED_BUCKET_NAME,
        path,
        content_type="image/png",
    )


def upload_raw_image(file_content: bytes, product_id: str, content_type: str = "image/jpeg") -> str:
    """Upload raw/original image before processing."""
    ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    ext = ext_map.get(content_type, ".jpg")
    path = f"{settings.RAW_STORAGE_FOLDER}/{product_id}{ext}"

    try:
        _ensure_bucket(settings.RAW_BUCKET_NAME)
        sb = get_supabase()
        sb.storage.from_(settings.RAW_BUCKET_NAME).upload(
            path=path,
            file=file_content,
            file_options={"content-type": content_type, "upsert": "true"},
        )
        public_url = f"{settings.SUPABASE_URL}/storage/v1/object/public/{settings.RAW_BUCKET_NAME}/{path}"
        logger.info(f"Uploaded raw image → {public_url}", extra={"product_id": product_id})
        return public_url
    except Exception as exc:
        logger.error(f"Failed to upload raw image for product {product_id}: {exc}", extra={"product_id": product_id})
        raise
