from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import sentry_sdk

from app.config import settings
from app.db.repository import fetch_chamak_generation, update_chamak_generation
from app.logging import logger
from app.services.ai import nanobana_client
from app.services.storage import (
    fetch_image_bytes_and_content_type,
    upload_chamak_output,
)


def _encode_data_uri(image_bytes: bytes, mime_type: str) -> str:
    """Format image bytes as base64 data URI for OpenAI Vision API."""
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}"


async def call_openai_vision_analysis(
    image1_bytes: bytes,
    image1_mime: str,
    image2_bytes: bytes,
    image2_mime: str,
) -> dict[str, Any]:
    """Call OpenAI Chat Completions endpoint with vision inputs in structured JSON mode."""
    if not settings.OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY is not configured in settings")

    img1_uri = _encode_data_uri(image1_bytes, image1_mime)
    img2_uri = _encode_data_uri(image2_bytes, image2_mime)

    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    system_prompt = (
        "You are an expert master jeweler, gemologist, and AI vision analyst for luxury jewelry manufacturing. "
        "Analyze the two provided jewelry design images (Image 1: Design 1 - High Engagement/Strengths, "
        "Image 2: Design 2 - Low Engagement/Weaknesses). "
        "Return ONLY a valid JSON object matching the exact keys and types specified."
    )

    user_text = (
        "Analyze both jewelry images for the Chamak AI Jewelry Fusion synthesis pipeline.\n"
        "Image 1 represents Design 1 (high customer engagement / hero piece).\n"
        "Image 2 represents Design 2 (low customer engagement / comparison piece).\n\n"
        "Provide a structured JSON response with the following fields:\n"
        "- jewelry_type: (string) Category of jewelry, e.g. 'necklace', 'ring', 'bangle', 'earrings', 'pendant', 'bracelet'.\n"
        "- image1_strengths: (list of strings) 3-5 specific craftsmanship, metalwork, gemstone, or aesthetic strengths of Design 1.\n"
        "- image1_weaknesses: (list of strings) Observed minor weaknesses or areas for improvement in Design 1.\n"
        "- image2_strengths: (list of strings) Any positive design accents or components in Design 2.\n"
        "- image2_weaknesses: (list of strings) 3-5 specific aesthetic flaws, structural bulkiness, or design weaknesses in Design 2.\n"
        "- near_identical: (boolean) True if Design 1 and Design 2 are nearly identical pieces.\n"
        "- type_mismatch: (boolean) True if the two designs belong to completely different jewelry categories (e.g. ring vs necklace).\n"
        "- content_flag: (string) Exactly one of: 'ok', 'not_jewelry', 'inappropriate', 'too_unclear_to_assess'."
    )

    payload = {
        "model": settings.OPENAI_MODEL or "gpt-4o",
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": img1_uri, "detail": "high"}},
                    {"type": "image_url", "image_url": {"url": img2_uri, "detail": "high"}},
                ],
            },
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }

    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        if not resp.is_success:
            logger.error(f"OpenAI API error {resp.status_code}: {resp.text}")
            resp.raise_for_status()

        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)

    # Validate and normalize keys
    return {
        "jewelry_type": str(parsed.get("jewelry_type", "jewelry")),
        "image1_strengths": list(parsed.get("image1_strengths", [])),
        "image1_weaknesses": list(parsed.get("image1_weaknesses", [])),
        "image2_strengths": list(parsed.get("image2_strengths", [])),
        "image2_weaknesses": list(parsed.get("image2_weaknesses", [])),
        "near_identical": bool(parsed.get("near_identical", False)),
        "type_mismatch": bool(parsed.get("type_mismatch", False)),
        "content_flag": str(parsed.get("content_flag", "ok")),
    }


async def run_stage1_vision_analysis(generation_id: str) -> None:
    """Stage 1: Vision Analysis for Chamak AI Fusion."""
    try:
        logger.info(f"Starting Chamak Stage 1 vision analysis for {generation_id}")
        row = await fetch_chamak_generation(generation_id)
        if not row:
            logger.error(f"Chamak generation {generation_id} not found")
            return

        await update_chamak_generation(generation_id, {"status": "analyzing"})

        img1_url = row.get("source_image_1_url")
        img2_url = row.get("source_image_2_url")

        if not img1_url or not img2_url:
            raise ValueError(f"Missing source image URLs for generation {generation_id}")

        img1_bytes, img1_mime = await fetch_image_bytes_and_content_type(img1_url)
        img2_bytes, img2_mime = await fetch_image_bytes_and_content_type(img2_url)

        analysis = await call_openai_vision_analysis(
            img1_bytes, img1_mime, img2_bytes, img2_mime
        )

        content_flag = analysis.get("content_flag", "ok")
        if content_flag not in ("ok", "not_jewelry", "inappropriate", "too_unclear_to_assess"):
            content_flag = "ok"

        # If content is flagged as invalid or inappropriate, set status to failed
        status = "failed" if content_flag != "ok" else "awaiting_input"

        await update_chamak_generation(
            generation_id,
            {
                "stage1_analysis_json": analysis,
                "content_flag_hit": content_flag,
                "status": status,
            },
        )
        logger.info(
            f"Chamak Stage 1 completed for {generation_id} -> status={status}, flag={content_flag}"
        )

    except Exception as exc:
        logger.error(
            f"Chamak Stage 1 vision analysis failed for {generation_id}: {exc}",
            extra={"generation_id": generation_id},
            exc_info=True,
        )
        if settings.SENTRY_DSN:
            sentry_sdk.capture_exception(exc)

        try:
            await update_chamak_generation(generation_id, {"status": "failed"})
        except Exception:
            logger.error(f"Failed to persist failure status for {generation_id}", exc_info=True)


def compile_chamak_prompt(
    analysis: Optional[dict[str, Any]],
    form_input: Optional[dict[str, Any]],
    note: Optional[str],
) -> str:
    """Stage 3: High-cohesion prompt synthesis for Chamak AI Fusion."""
    analysis = analysis or {}
    form_input = form_input or {}
    jewelry_type = (analysis.get("jewelry_type") or "luxury jewelry").strip()

    img1_strengths = analysis.get("image1_strengths") or [
        "intricate filigree craftsmanship",
        "radiant high-carat gold finish",
        "precision prong stone setting",
    ]
    img2_weaknesses = analysis.get("image2_weaknesses") or [
        "clunky base geometry",
        "overly heavy accents",
    ]
    img2_strengths = analysis.get("image2_strengths") or []

    # Parse slider weights if present
    slider_weights: dict[str, float] = {}
    if isinstance(form_input, dict):
        raw_weights = form_input.get("slider_weights") or form_input.get("weights") or {}
        if isinstance(raw_weights, dict):
            for k, v in raw_weights.items():
                try:
                    slider_weights[k] = float(v)
                except (ValueError, TypeError):
                    pass

    # Prompt synthesis sections
    fusion_directives: list[str] = []

    # Primary hero directive
    hero_traits = ", ".join(img1_strengths[:4])
    fusion_directives.append(
        f"Masterpiece luxury {jewelry_type} design fusion. "
        f"Prominently retain and elevate the hero craftsmanship features of Design 1: {hero_traits}."
    )

    # Replacement directive
    if img2_weaknesses:
        weakness_text = ", ".join(img2_weaknesses[:3])
        fusion_directives.append(
            f"Refine and eliminate the heavy and unrefined elements ({weakness_text}), "
            f"seamlessly replacing them with graceful, high-elegance contours and balanced proportions."
        )

    # Secondary strengths directive
    if img2_strengths:
        sec_traits = ", ".join(img2_strengths[:2])
        fusion_directives.append(
            f"Subtly integrate the best complementary accents: {sec_traits}."
        )

    # Slider weighting integration
    if slider_weights:
        high_weight_attrs = [k for k, w in slider_weights.items() if w >= 0.6]
        low_weight_attrs = [k for k, w in slider_weights.items() if w < 0.4]
        if high_weight_attrs:
            clean_high = ", ".join(k.replace("attr_", "attribute ").replace("_", " ") for k in high_weight_attrs)
            fusion_directives.append(f"Emphasize strong presence of {clean_high}.")
        if low_weight_attrs:
            clean_low = ", ".join(k.replace("attr_", "attribute ").replace("_", " ") for k in low_weight_attrs)
            fusion_directives.append(f"Keep subtle and delicate: {clean_low}.")

    # Artisan custom note
    if note and note.strip():
        fusion_directives.append(f"Artisan custom modification: {note.strip()}.")

    # High-luxury studio rendering instructions
    studio_rendering_specs = (
        "Hyper-realistic luxury fine jewelry commercial photograph. "
        "Pristine polished 22K yellow gold and platinum luster, flawless gemstone brilliance, "
        "razor-sharp pavé and prong micro-setting details, exquisite Indian bridal & contemporary haute joaillerie craftsmanship. "
        "Dramatic studio lighting with soft diffused key light, golden rim light, crisp reflections, "
        "shot on 85mm macro lens, 8K ultra high definition, perfect symmetry, pure luxury aesthetic."
    )
    fusion_directives.append(studio_rendering_specs)

    compiled_prompt = " ".join(fusion_directives)
    return compiled_prompt


async def run_stage4_generation(generation_id: str) -> None:
    """Stage 4: Image Generation & Output Storage for Chamak AI Fusion."""
    try:
        logger.info(f"Starting Chamak Stage 4 generation for {generation_id}")
        row = await fetch_chamak_generation(generation_id)
        if not row:
            logger.error(f"Chamak generation {generation_id} not found")
            return

        wholesaler_id = str(row.get("wholesaler_id") or "default_wholesaler")
        await update_chamak_generation(generation_id, {"status": "generating"})

        analysis = row.get("stage1_analysis_json") or {}
        form_json = row.get("wholesaler_form_json") or {}
        note_text = row.get("note_text")

        # Stage 3 Prompt Compilation
        compiled_prompt = compile_chamak_prompt(analysis, form_json, note_text)
        logger.info(
            f"Compiled Chamak prompt for {generation_id}: {compiled_prompt[:120]}...",
            extra={"generation_id": generation_id},
        )

        # Source images
        img1_url = row.get("source_image_1_url")
        if not img1_url:
            raise ValueError("source_image_1_url is required for image generation")

        # Generate image using Nanobana
        generated_bytes = await nanobana_client.enhance_image(
            image_url=img1_url,
            prompt=compiled_prompt,
        )

        # Upload to Supabase Storage private bucket 'chamak-outputs' at '{wholesaler_id}/{generation_id}.png'
        output_storage_path = upload_chamak_output(
            file_content=generated_bytes,
            wholesaler_id=wholesaler_id,
            generation_id=generation_id,
        )

        completed_at = datetime.now(timezone.utc).isoformat()

        # Update chamak_generations record
        await update_chamak_generation(
            generation_id,
            {
                "compiled_prompt_text": compiled_prompt,
                "prompt_version": settings.CHAMAK_PROMPT_VERSION,
                "output_image_url": output_storage_path,
                "status": "done",
                "completed_at": completed_at,
            },
        )
        logger.info(
            f"Chamak Stage 4 generation finished successfully for {generation_id}",
            extra={
                "generation_id": generation_id,
                "output_image_url": output_storage_path,
                "completed_at": completed_at,
            },
        )

    except Exception as exc:
        logger.error(
            f"Chamak Stage 4 generation failed for {generation_id}: {exc}",
            extra={"generation_id": generation_id},
            exc_info=True,
        )
        if settings.SENTRY_DSN:
            sentry_sdk.capture_exception(exc)

        try:
            await update_chamak_generation(generation_id, {"status": "failed"})
        except Exception:
            logger.error(f"Failed to persist failure status for {generation_id}", exc_info=True)
