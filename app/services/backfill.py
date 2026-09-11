"""Catch images that were stored before variants existed.

Every new upload writes its card/detail/full copies (see `derivatives.py`).
Everything uploaded before that has only the full-size original, so a
catalogue screen still downloads ~32 MB. This walks those rows once, writes
the missing copies, and records them.

It runs in the background shortly after the server starts and stops as soon
as there is nothing left to do, so a deploy with nothing outstanding costs
one query. It is safe to run repeatedly: a variant's file name is a hash of
its own bytes, so re-running writes the same paths rather than piling up
copies, and rows that already have every size are skipped.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from app.config import settings
from app.db.repository import get_supabase
from app.logging import logger
from app.services.derivatives import VARIANTS
from app.services.storage import upload_variants

PUBLIC_PREFIX = "/storage/v1/object/public/"
VARIANT_NAMES = {spec.name for spec in VARIANTS}

#: Wait for the app to finish starting before adding load.
STARTUP_DELAY_SECONDS = 30
#: Images converted at once. Deliberately small: this shares a container with
#: the AI pipeline, and no wholesaler is waiting for it.
CONCURRENCY = 3


def split_public_url(url: str) -> Optional[tuple[str, str]]:
    """`https://…/object/public/plant-images/products/x.png` → (bucket, path)."""
    marker = url.find(PUBLIC_PREFIX)
    if marker < 0:
        return None
    rest = url[marker + len(PUBLIC_PREFIX):].split("?", 1)[0]
    bucket, _, path = rest.partition("/")
    return (bucket, path) if bucket and path else None


def _download(bucket: str, path: str) -> bytes:
    return get_supabase().storage.from_(bucket).download(path)


# ─────────────────────────────────────────────────────────────────────────────
# Products — public bucket, variants keyed by the original URL
# ─────────────────────────────────────────────────────────────────────────────

def _product_rows_needing_variants(limit: Optional[int]) -> list[dict[str, Any]]:
    rows = (
        get_supabase()
        .table("products")
        .select("id, image_url, processed_image_url, generated_image_urls, image_variants")
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )

    todo: list[dict[str, Any]] = []
    for row in rows:
        have = row.get("image_variants") or {}
        urls: list[str] = [
            value
            for value in (row.get("image_url"), row.get("processed_image_url"))
            if isinstance(value, str)
        ]
        generated = row.get("generated_image_urls")
        if isinstance(generated, list):
            urls.extend(u for u in generated if isinstance(u, str))

        missing = [
            url
            for url in dict.fromkeys(urls)
            if url.startswith("http")
            and split_public_url(url)
            and VARIANT_NAMES - set((have.get(url) or {}).keys())
        ]
        if missing:
            todo.append({"id": row["id"], "urls": missing, "have": have})
        if limit and len(todo) >= limit:
            break
    return todo


def _backfill_product(job: dict[str, Any]) -> int:
    written_by_url: dict[str, dict[str, str]] = {}
    for url in job["urls"]:
        located = split_public_url(url)
        if not located:
            continue
        bucket, path = located
        try:
            written = upload_variants(_download(bucket, path), bucket, path)
            if written:
                written_by_url[url] = written
        except Exception as exc:
            logger.warning(f"Backfill could not convert {bucket}/{path}: {exc}")

    if not written_by_url:
        return 0
    try:
        get_supabase().table("products").update(
            {"image_variants": {**(job["have"] or {}), **written_by_url}}
        ).eq("id", job["id"]).execute()
    except Exception as exc:
        logger.warning(f"Backfill could not record variants for product {job['id']}: {exc}")
        return 0
    return len(written_by_url)


# ─────────────────────────────────────────────────────────────────────────────
# Chamak — private bucket, variants are paths inside it
# ─────────────────────────────────────────────────────────────────────────────

def _chamak_rows_needing_variants(limit: Optional[int]) -> list[dict[str, Any]]:
    query = (
        get_supabase()
        .table(settings.CHAMAK_TABLE_NAME)
        .select("id, output_image_url")
        .eq("status", "done")
        .is_("output_variants", "null")
        .not_.is_("output_image_url", "null")
        .order("created_at", desc=True)
    )
    if limit:
        query = query.limit(limit)
    return query.execute().data or []


def _backfill_chamak(row: dict[str, Any]) -> int:
    bucket = settings.CHAMAK_OUTPUT_BUCKET
    path = str(row["output_image_url"]).replace(f"{bucket}/", "", 1)
    if path.startswith("http"):
        return 0
    try:
        written = upload_variants(_download(bucket, path), bucket, path)
        if not written:
            return 0
        get_supabase().table(settings.CHAMAK_TABLE_NAME).update(
            {"output_variants": written}
        ).eq("id", row["id"]).execute()
        return 1
    except Exception as exc:
        logger.warning(f"Backfill could not convert chamak output {path}: {exc}")
        return 0


# ─────────────────────────────────────────────────────────────────────────────

async def run_backfill(limit: Optional[int] = None, concurrency: int = CONCURRENCY) -> dict[str, int]:
    """Convert everything still missing its variants. Returns what it did."""
    loop = asyncio.get_running_loop()
    gate = asyncio.Semaphore(concurrency)

    async def guarded(fn, arg) -> int:
        async with gate:
            # The Supabase SDK is synchronous; keep the event loop free.
            return await loop.run_in_executor(None, fn, arg)

    products = await loop.run_in_executor(None, _product_rows_needing_variants, limit)
    chamak = await loop.run_in_executor(None, _chamak_rows_needing_variants, limit)
    if not products and not chamak:
        return {"products": 0, "chamak": 0, "images": 0}

    logger.info(
        f"Image variant backfill starting: {len(products)} product row(s), "
        f"{len(chamak)} chamak generation(s)"
    )

    product_counts = await asyncio.gather(*(guarded(_backfill_product, job) for job in products))
    chamak_counts = await asyncio.gather(*(guarded(_backfill_chamak, row) for row in chamak))

    done = {
        "products": sum(1 for c in product_counts if c),
        "chamak": sum(chamak_counts),
        "images": sum(product_counts) + sum(chamak_counts),
    }
    logger.info(
        f"Image variant backfill finished: {done['images']} image(s) converted "
        f"across {done['products']} product row(s) and {done['chamak']} generation(s)"
    )
    return done


async def backfill_on_startup() -> None:
    """Background task: convert anything outstanding, once, after startup."""
    if not settings.IMAGE_VARIANT_BACKFILL:
        return
    try:
        await asyncio.sleep(STARTUP_DELAY_SECONDS)
        await run_backfill()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Thumbnails are an optimisation; never take the API down for them.
        logger.error(f"Image variant backfill failed: {exc}", exc_info=exc)
