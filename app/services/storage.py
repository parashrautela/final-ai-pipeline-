from __future__ import annotations

from typing import NamedTuple
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.db.repository import get_supabase
from app.logging import logger
from app.services.derivatives import CACHE_SECONDS, WEBP_CONTENT_TYPE, build_variants

#: Originals are overwritten in place when a product is re-processed, so they
#: are cached for a day rather than forever. The variants next to them carry a
#: hash in the name and are cached for a year (CACHE_SECONDS).
CACHE_SECONDS_DEFAULT = "86400"


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


class StoredImage(NamedTuple):
    """Where an image ended up, and the small copies stored next to it.

    `url` is a public URL for a public bucket and a bucket-relative path for a
    private one. `variants` is `{"card"|"detail"|"full": url or path}`, and is
    empty when they could not be made — callers then fall back to the original.
    """

    url: str
    variants: dict[str, str]


def upload_chamak_output(file_content: bytes, wholesaler_id: str, generation_id: str) -> StoredImage:
    """Upload generated Chamak output image to private bucket chamak-outputs.

    Returns the relative path inside the bucket
    ('{wholesaler_id}/{generation_id}.png') and the paths of its variants.
    """
    bucket = settings.CHAMAK_OUTPUT_BUCKET
    path = f"{wholesaler_id}/{generation_id}.png"
    try:
        _ensure_bucket(bucket, public=False)
        sb = get_supabase()
        sb.storage.from_(bucket).upload(
            path=path,
            file=file_content,
            file_options={
                "content-type": "image/png",
                "upsert": "true",
                "cache-control": CACHE_SECONDS_DEFAULT,
            },
        )
        logger.info(
            f"Uploaded chamak output to private bucket: {bucket}/{path}",
            extra={"wholesaler_id": wholesaler_id, "generation_id": generation_id},
        )
        return StoredImage(path, upload_variants(file_content, bucket, path))
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


def public_url_for(bucket: str, path: str) -> str:
    """The public URL of a stored object. The SDK doesn't return one on upload."""
    return f"{settings.SUPABASE_URL}/storage/v1/object/public/{bucket}/{path}"


def upload_file_to_storage(
    content: bytes,
    bucket: str,
    path: str,
    content_type: str = "image/png",
    cache_seconds: str = CACHE_SECONDS_DEFAULT,
) -> str:
    """Upload bytes to bucket and return public URL.

    `cache_seconds` is seconds, not a header — storage3 turns it into
    `cache-control: max-age=N`. Without it Supabase answers `no-cache`, so
    Cloudflare re-fetches every image from origin and phones keep nothing:
    that alone made every catalogue scroll re-download megabytes.
    """
    try:
        _ensure_bucket(bucket)
        sb = get_supabase()
        # x-upsert: true — overwrite silently if the file already exists.
        # This lets us re-run the pipeline for the same product without errors.
        sb.storage.from_(bucket).upload(
            path=path,
            file=content,
            file_options={
                "content-type": content_type,
                "x-upsert": "true",
                "cache-control": cache_seconds,
            },
        )
        public_url = public_url_for(bucket, path)
        logger.info(f"Uploaded to storage: {public_url}")
        return public_url
    except Exception as exc:
        logger.error(f"Failed to upload to storage {bucket}/{path}: {exc}")
        raise


def upload_variants(original: bytes, bucket: str, original_path: str) -> dict[str, str]:
    """Write the card/detail/full copies of an image next to the original.

    Returns `{variant name: public URL}` for a public bucket, or
    `{variant name: storage path}` for a private one (the apps sign those
    themselves). Returns `{}` if the image can't be read or a copy can't be
    stored: a missing thumbnail must never fail the upload that produced it,
    because the apps fall back to the original.
    """
    is_public = bucket != settings.CHAMAK_OUTPUT_BUCKET
    try:
        variants = build_variants(original, original_path)
    except Exception as exc:
        logger.warning(f"Could not build image variants for {bucket}/{original_path}: {exc}")
        return {}

    out: dict[str, str] = {}
    for variant in variants:
        try:
            _ensure_bucket(bucket, public=is_public)
            get_supabase().storage.from_(bucket).upload(
                path=variant.path,
                file=variant.content,
                file_options={
                    "content-type": WEBP_CONTENT_TYPE,
                    "x-upsert": "true",
                    # Safe to cache forever: the name contains a hash of the
                    # bytes, so different pixels always mean a different path.
                    "cache-control": CACHE_SECONDS,
                },
            )
            out[variant.name] = public_url_for(bucket, variant.path) if is_public else variant.path
        except Exception as exc:
            logger.warning(f"Could not upload {variant.name} variant {bucket}/{variant.path}: {exc}")
    if out:
        logger.info(
            f"Wrote {len(out)} image variant(s) for {bucket}/{original_path}",
            extra={"variants": list(out)},
        )
    return out


def upload_processed_image(file_content: bytes, product_id: str) -> StoredImage:
    """Upload processed image to storage, with its card/detail/full copies."""
    path = f"{settings.PROCESSED_STORAGE_FOLDER}/{product_id}.png"
    url = upload_file_to_storage(
        file_content,
        settings.PROCESSED_BUCKET_NAME,
        path,
        content_type="image/png",
    )
    return StoredImage(url, upload_variants(file_content, settings.PROCESSED_BUCKET_NAME, path))


def upload_processed_image_variant(file_content: bytes, product_id: str, variant_index: int) -> StoredImage:
    """Upload one of the 4 generated image variants, with its smaller copies."""
    path = f"{settings.PROCESSED_STORAGE_FOLDER}/{product_id}_v{variant_index}.png"
    logger.info(f"Uploading variant {variant_index}/4: {path}", extra={"product_id": product_id})
    url = upload_file_to_storage(
        file_content,
        settings.PROCESSED_BUCKET_NAME,
        path,
        content_type="image/png",
    )
    return StoredImage(url, upload_variants(file_content, settings.PROCESSED_BUCKET_NAME, path))


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
