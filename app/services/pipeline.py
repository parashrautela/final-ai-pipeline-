from __future__ import annotations

import asyncio
import time
from typing import Optional

from app.config import settings
from app.db.repository import (
    log_ai_generation_complete,
    log_ai_generation_start,
    update_job_status,
    update_product_generated_images,
)
from app.logging import logger
from app.services.ai import nanobana_client
from app.services.prompt_composer import prompt_composer
from app.services.storage import (
    download_image,
    upload_processed_image,
    upload_processed_image_variant,
)

VARIANT_SCENE_SETTINGS = [
    (
        "SCENE 1: Place the jewellery on a dark navy-blue sculpted stone surface. "
        "Classic front-facing display angle, centered, with soft directional studio lighting from the upper-left. "
        "Deep, luxurious atmosphere."
    ),
    (
        "SCENE 2: Place the jewellery on a deep burgundy-red velvet cushion surface. "
        "Gentle 45-degree front-left perspective with warm golden studio lighting and soft depth-of-field bokeh. "
        "Luxury jewellery boutique aesthetic."
    ),
    (
        "SCENE 3: Place the jewellery on a pristine white Carrara marble surface with subtle grey veining. "
        "Slight elevated perspective with bright diffused daylight and clean, airy editorial composition."
    ),
    (
        "SCENE 4: Place the jewellery floating and centered against a deep charcoal-black gradient background. "
        "Subtle warm gold rim accents, dramatic side-profile angle, and a defined overhead spotlight creating a sharp contact shadow."
    ),
]


async def _generate_variant(
    input_image_url: str,
    product_id: str,
    variant_index: int,
    prompt: str,
    jewellery_type: str,
    base_version: int,
    category_version: int,
    wholesaler_id: Optional[str] = None,
) -> Optional[str]:
    """Run single Nanobana generation + upload for one variant, with full audit logging."""
    log_id = await log_ai_generation_start(
        product_id=product_id,
        jewellery_type=jewellery_type,
        composed_prompt=prompt,
        base_module_version=base_version,
        category_module_version=category_version,
        wholesaler_id=wholesaler_id,
    )

    try:
        logger.info(
            f"Variant {variant_index} — starting (base v{base_version}, category v{category_version})",
            extra={"product_id": product_id},
        )

        image_bytes = await nanobana_client.enhance_image(input_image_url, prompt=prompt)

        # Upload runs in a thread because the Supabase storage SDK is synchronous.
        public_url = await asyncio.to_thread(
            upload_processed_image_variant, image_bytes, product_id, variant_index
        )

        if log_id:
            await log_ai_generation_complete(log_id, status="success")

        logger.info(f"Variant {variant_index} — done: {public_url}", extra={"product_id": product_id})
        return public_url

    except Exception as exc:
        if log_id:
            await log_ai_generation_complete(log_id, status="failed")
        # Return None so the other variants can still finish successfully.
        logger.error(f"Variant {variant_index} FAILED: {exc}", extra={"product_id": product_id}, exc_info=True)
        return None


async def process_product_image(product: dict) -> list[str]:
    """
    Full AI pipeline for a product — directly sends raw image URL to Nanobana with composed category prompts.

    Steps:
    1. Get raw image URL from product
    2. Compose Base + Category prompts
    3. Generate 4 variants concurrently via Nanobana
    4. Upload each variant to Supabase Storage
    5. Persist URLs to database

    Returns list of successful variant URLs. Raises if all fail.
    """
    product_id = product["id"]
    raw_image_url = product.get("image_url")
    if not raw_image_url:
        raise ValueError(f"Product {product_id} has no image_url")

    start = time.time()

    variant_count = 1 if settings.TEST_MODE else 4
    logger.info(
        f"Pipeline started ({'TEST — 1 variant' if settings.TEST_MODE else '4-variant mode'})",
        extra={"product_id": product_id, "raw_image_url": raw_image_url},
    )

    # Compose category-based prompts
    title = product.get("title", "")
    jewellery_type = (product.get("jewellery_type") or "other").strip().lower()
    wholesaler_id = product.get("wholesaler_id")

    item_description = f"{title} ({jewellery_type})" if title else jewellery_type
    composed = await prompt_composer.get_composed_prompt(
        jewellery_type=jewellery_type,
        item_description=item_description,
    )

    logger.info(
        f"Composed prompt for '{jewellery_type}' (Base v{composed.base_module_version}, Category v{composed.category_module_version})",
        extra={"product_id": product_id},
    )

    # Build exact prompts per variant
    variant_prompts = [
        f"{composed.composed_prompt}\n\n{scene}"
        for scene in VARIANT_SCENE_SETTINGS
    ]

    active_prompts = variant_prompts[:variant_count]
    results = await asyncio.gather(
        *[
            _generate_variant(
                input_image_url=raw_image_url,
                product_id=product_id,
                variant_index=i + 1,
                prompt=p,
                jewellery_type=jewellery_type,
                base_version=composed.base_module_version,
                category_version=composed.category_module_version,
                wholesaler_id=wholesaler_id,
            )
            for i, p in enumerate(active_prompts)
        ]
    )

    # Filter out any None values from variants that failed.
    successful_urls = [url for url in results if url is not None]

    if not successful_urls:
        raise RuntimeError(f"All {variant_count} variant(s) failed for product {product_id}")

    logger.info(f"{len(successful_urls)}/{variant_count} variants generated", extra={"product_id": product_id})

    # Persist to database
    await update_product_generated_images(product_id, successful_urls, update_image_url=True)

    elapsed_ms = int((time.time() - start) * 1000)
    logger.info(f"Pipeline complete in {elapsed_ms}ms", extra={"product_id": product_id})

    return successful_urls


async def process_job(job: dict) -> None:
    """Worker-facing pipeline wrapper for job queue processing."""
    job_id = job["id"]
    raw_url = job.get("raw_url") or job.get("image_url")
    if not raw_url:
        raise ValueError(f"Job {job_id} has no raw_url or image_url")

    title = job.get("title", "")
    jewellery_type = (job.get("jewellery_type") or "other").strip().lower()
    wholesaler_id = job.get("wholesaler_id")
    start = time.time()

    logger.info("Worker job started", extra={"job_id": job_id})

    try:
        item_description = f"{title} ({jewellery_type})" if title else jewellery_type
        composed = await prompt_composer.get_composed_prompt(
            jewellery_type=jewellery_type,
            item_description=item_description,
        )

        final_prompt = f"{composed.composed_prompt}\n\n{VARIANT_SCENE_SETTINGS[0]}"

        log_id = await log_ai_generation_start(
            product_id=job_id,
            jewellery_type=jewellery_type,
            composed_prompt=final_prompt,
            base_module_version=composed.base_module_version,
            category_module_version=composed.category_module_version,
            wholesaler_id=wholesaler_id,
        )

        logger.info("Enhancing image directly with Nanobana", extra={"job_id": job_id})
        final_image = await nanobana_client.enhance_image(raw_url, prompt=final_prompt)

        logger.info("Uploading processed image", extra={"job_id": job_id})
        processed_url = upload_processed_image(final_image, job_id)

        if log_id:
            await log_ai_generation_complete(log_id, status="success")

        elapsed_ms = int((time.time() - start) * 1000)
        await update_job_status(
            job_id,
            status="done",
            processed_url=processed_url,
            processing_time_ms=elapsed_ms,
        )
        logger.info(f"Worker job complete in {elapsed_ms}ms", extra={"job_id": job_id})

    except Exception as exc:
        logger.error(f"Worker job failed: {exc}", extra={"job_id": job_id})
        await update_job_status(job_id, status="error", error_message=str(exc))
