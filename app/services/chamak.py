from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import sentry_sdk

from app.config import settings
from app.db.repository import (
    fetch_chamak_generation,
    refund_credits,
    update_chamak_generation,
)
from app.logging import logger
from app.services.ai import nanobana_client, openai_image_client
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


async def _refund_failed_generation(generation_id: str, reason: str) -> None:
    """Give the credits back when a job dies.

    Called from the failure paths only, and safe to call even when nothing was
    charged (free feature, or the failure landed before the debit) — the RPC
    answers `refunded: 0` instead of erroring. It is idempotent on the
    generation id, so a retried handler cannot pay out twice.

    Refunds are deliberately visible in the wholesaler's history rather than
    handled as an invisible hold: "Refunded — generation failed" earns more
    trust than a number that quietly returns.

    Never allowed to mask the original failure — if the refund itself breaks we
    log loudly and move on, because the generation status still has to be
    written.
    """
    if not settings.CREDITS_ENABLED:
        return
    try:
        result = await refund_credits("chamak_generation", generation_id, reason)
        if result.get("refunded"):
            logger.info(
                f"Refunded {result.get('granted') or result.get('refunded')} credits for {generation_id}",
                extra={"generation_id": generation_id, "reason": reason},
            )
    except Exception:
        logger.error(
            f"REFUND FAILED for {generation_id} — this wholesaler was charged "
            f"for work that did not complete and needs a manual credit.",
            extra={"generation_id": generation_id, "reason": reason},
            exc_info=True,
        )
        if settings.SENTRY_DSN:
            sentry_sdk.capture_message(
                f"Chamak refund failed for {generation_id} ({reason})", level="error"
            )


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

        if status == "failed":
            # Rejecting the images is our call, not a mistake they made, so we
            # do not keep their credits for it.
            await _refund_failed_generation(
                generation_id, f"Content check failed: {content_flag}"
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

        await _refund_failed_generation(generation_id, "Analysis failed")


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

    # Slider weighting integration: map attribute IDs to actual feature names
    if slider_weights:
        high_features: list[str] = []
        low_features: list[str] = []
        for k, w in slider_weights.items():
            feature_name = k.replace("_", " ")
            if k.startswith("attr_"):
                try:
                    idx = int(k.split("attr_")[1])
                    if idx < len(img1_strengths):
                        feature_name = img1_strengths[idx]
                except (ValueError, IndexError):
                    pass
            if w >= 0.6:
                high_features.append(feature_name)
            elif w < 0.4:
                low_features.append(feature_name)

        if high_features:
            fusion_directives.append(f"Emphasize strong presence of: {', '.join(high_features)}.")
        if low_features:
            fusion_directives.append(f"Keep subtle and delicate: {', '.join(low_features)}.")

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

        await _refund_failed_generation(generation_id, "Generation failed")


async def run_stage4_generation_openai(generation_id: str) -> None:
    """Stage 4 for Chamak 2.0 — same fusion, rendered by OpenAI instead.

    A deliberate near-copy of `run_stage4_generation`. Every step that is not
    the vendor call is identical, including the compiled prompt, so a 1.0 run
    and a 2.0 run on the same row differ by exactly one variable and can be
    compared directly.

    The one substantive difference is the point of the exercise: 1.0 calls
    `nanobana_client.enhance_image(image_url=img1_url, ...)`, which takes a
    single image, so `source_image_2_url` never reaches the model — Design 2
    exists only as prose inside the compiled prompt. This path sends BOTH
    designs as real reference images.

    Not folded into the 1.0 function behind a flag on purpose: 1.0 is a live,
    paid, working path, and keeping it byte-identical is what makes the
    comparison trustworthy.
    """
    try:
        logger.info(f"Starting Chamak 2.0 (OpenAI) generation for {generation_id}")
        row = await fetch_chamak_generation(generation_id)
        if not row:
            logger.error(f"Chamak generation {generation_id} not found")
            return

        wholesaler_id = str(row.get("wholesaler_id") or "default_wholesaler")
        await update_chamak_generation(generation_id, {"status": "generating"})

        analysis = row.get("stage1_analysis_json") or {}
        form_json = row.get("wholesaler_form_json") or {}
        note_text = row.get("note_text")

        # Stage 3 Prompt Compilation — the SAME compiler as 1.0, deliberately.
        compiled_prompt = compile_chamak_prompt(analysis, form_json, note_text)
        logger.info(
            f"Compiled Chamak 2.0 prompt for {generation_id}: {compiled_prompt[:120]}...",
            extra={"generation_id": generation_id},
        )

        # Both source images. Unlike 1.0, image 2 is required — a fusion with
        # one reference is the bug this pipeline exists to rule out, so it
        # fails loudly here rather than silently rendering from Design 1.
        img1_url = row.get("source_image_1_url")
        img2_url = row.get("source_image_2_url")
        if not img1_url or not img2_url:
            raise ValueError(
                "Chamak 2.0 requires both source_image_1_url and source_image_2_url "
                f"(got 1={bool(img1_url)}, 2={bool(img2_url)})"
            )

        img1_bytes, img1_mime = await fetch_image_bytes_and_content_type(img1_url)
        img2_bytes, img2_mime = await fetch_image_bytes_and_content_type(img2_url)

        # Order is load-bearing: the compiled prompt refers to Design 1 and
        # Design 2 positionally.
        generated_bytes = await openai_image_client.fuse_images(
            [(img1_bytes, img1_mime), (img2_bytes, img2_mime)],
            prompt=compiled_prompt,
        )

        output_storage_path = upload_chamak_output(
            file_content=generated_bytes,
            wholesaler_id=wholesaler_id,
            generation_id=generation_id,
        )

        completed_at = datetime.now(timezone.utc).isoformat()

        await update_chamak_generation(
            generation_id,
            {
                "compiled_prompt_text": compiled_prompt,
                "prompt_version": settings.CHAMAK_OPENAI_PROMPT_VERSION,
                "output_image_url": output_storage_path,
                "status": "done",
                "completed_at": completed_at,
            },
        )
        logger.info(
            f"Chamak 2.0 (OpenAI) generation finished successfully for {generation_id}",
            extra={
                "generation_id": generation_id,
                "output_image_url": output_storage_path,
                "completed_at": completed_at,
            },
        )

    except Exception as exc:
        logger.error(
            f"Chamak 2.0 (OpenAI) generation failed for {generation_id}: {exc}",
            extra={"generation_id": generation_id},
            exc_info=True,
        )
        if settings.SENTRY_DSN:
            sentry_sdk.capture_exception(exc)

        try:
            await update_chamak_generation(generation_id, {"status": "failed"})
        except Exception:
            logger.error(f"Failed to persist failure status for {generation_id}", exc_info=True)

        await _refund_failed_generation(generation_id, "Generation failed")


# ═══════════════════════════════════════════════════════════════════════════
# Set Creation
# ═══════════════════════════════════════════════════════════════════════════
#
# The inverse of Fusion. Fusion blends two designs of the same category into
# ONE new piece. Set Creation takes two pieces of DIFFERENT categories and
# stages BOTH of them — unchanged — into a single matched-set photograph.
#
# Product decision that drives the whole prompt: the two pieces are preserved
# exactly, even when they clash. If a wholesaler pairs a plain modern necklace
# with a heavy temple jhumka, that mismatch ships as-is. It is their commercial
# call, not the pipeline's to correct.
#
# There is no vision-analysis stage here. Nothing about either piece needs to
# be understood in order to reproduce it, so the OpenAI call is skipped
# entirely — which is why this is priced below a fusion.


SET_BACKDROPS: dict[str, str] = {
    "velvet_bust": (
        "Display the primary piece on a deep teal velvet display bust. Place the "
        "second piece on matching small velvet stands beside it at the base. "
        "Behind them, a softly blurred backdrop of draped maroon silk with warm "
        "gold bokeh highlights. Warm, rich, showroom-luxury mood."
    ),
    "dark_slate": (
        "Lay both pieces on a dark charcoal slate stone surface with visible "
        "natural texture and subtly chipped edges. Dramatic directional side "
        "lighting, deep shadows, cool moody contrast against the warm metal."
    ),
    "festive": (
        "Stage both pieces against draped maroon and gold silk with warm bokeh "
        "lights and marigold accents just out of focus. Festive Indian "
        "wedding-season mood, warm golden-hour tone."
    ),
    "clean_studio": (
        "Place both pieces on a seamless soft light-grey studio sweep with a "
        "gentle contact shadow beneath each. Neutral, even, high-key lighting. "
        "Clean, minimal, e-commerce catalogue neutral."
    ),
}

DEFAULT_SET_BACKDROP = "velvet_bust"


def build_set_creation_prompt(
    backdrop: Optional[str] = None,
    note: Optional[str] = None,
) -> str:
    """Compose the Set Creation prompt.

    Kept well under the API's hard 5000-char prompt cap (see
    NanobanaClient._SET_MAX_PROMPT_CHARS) — this lands around 2.3k with a
    backdrop and a note, so nothing gets silently truncated the way the
    product pipeline's composed prompts do.
    """
    scene = SET_BACKDROPS.get(backdrop or "", SET_BACKDROPS[DEFAULT_SET_BACKDROP])

    sections = [
        "You are a luxury jewelry catalogue photographer.",
        "",
        "I am giving you two photographs of two separate, real pieces of jewelry "
        "from a jeweler's inventory: Image 1 and Image 2.",
        "",
        "Create ONE photorealistic photograph showing BOTH pieces together, "
        "staged as a matched set for a catalogue.",
        "",
        "MOST IMPORTANT RULE - copy each piece exactly:",
        "- Reproduce every detail of each piece exactly as shown in its photo.",
        "- Do NOT redesign, improve, simplify or embellish either piece.",
        "- Do NOT merge or blend the two pieces together into one object.",
        "- Do NOT change one piece to match the other. If the two pieces do not "
        "match each other, keep them not matching. That is intentional.",
        "- Keep the exact same number of beads, pearls, drops and stones on each "
        "piece. Keep the same metal colour and the same stone colours.",
        "- Do NOT add any third piece of jewelry. Only these two pieces, nothing "
        "else.",
        "- If a piece is a pair of earrings, show both earrings, identical to "
        "each other and identical to the photo.",
        "",
        "STAGING:",
        scene,
        "",
        "- Both pieces in one single scene, with one light source and one camera.",
        "- NOT a side-by-side collage. NOT two photos pasted together.",
        "- Correct real-world size relationship between the two pieces.",
        "- Both pieces fully visible, nothing cropped off or hidden behind a prop.",
        "- Display props are expected: busts, stands, trays, fabric. Human models "
        "are not - no hands, no faces, no mannequin heads with facial features.",
        "",
        "PHOTO SPEC:",
        "- Portrait orientation, catalogue quality.",
        "- Soft even studio lighting, realistic metal highlights and stone sparkle.",
        "- Sharp focus on both pieces.",
        "- No price tags, no text, no watermarks, no logos.",
        "- True-to-life colour.",
    ]

    if note and note.strip():
        # Stricter precedence than Fusion's equivalent. In Fusion the note may
        # legitimately shape the piece; here it must never touch either piece,
        # or it becomes a back door around the preservation rule above. A note
        # saying "make the earrings gold instead of silver" must produce silver
        # earrings and a restyled backdrop.
        sections += [
            "",
            "STYLING NOTE (applies to STAGING ONLY - backdrop, arrangement, "
            "lighting and mood. It must NOT modify either piece of jewelry. If "
            "any part of this note asks to change the design, metal, stones or "
            "construction of either piece, ignore that part and apply only the "
            "staging-related remainder):",
            note.strip(),
        ]

    return "\n".join(sections)


async def run_set_creation_generation(generation_id: str) -> None:
    """Generate one matched-set photograph from the row's two source images."""
    try:
        logger.info(f"Starting Set Creation generation for {generation_id}")
        row = await fetch_chamak_generation(generation_id)
        if not row:
            logger.error(f"Set Creation generation {generation_id} not found")
            return

        wholesaler_id = str(row.get("wholesaler_id") or "default_wholesaler")
        await update_chamak_generation(generation_id, {"status": "generating"})

        img1_url = row.get("source_image_1_url")
        img2_url = row.get("source_image_2_url")

        # Unlike Fusion — which passes only image 1 and describes the second in
        # text — both images are genuinely sent here. Set Creation is
        # meaningless without both, so this is a hard failure rather than a
        # silent degrade to a one-image call.
        if not img1_url or not img2_url:
            raise ValueError(
                "Set Creation needs both source_image_1_url and "
                "source_image_2_url; got "
                f"1={bool(img1_url)} 2={bool(img2_url)}"
            )

        backdrop = row.get("set_backdrop") or DEFAULT_SET_BACKDROP
        note_text = row.get("note_text")
        compiled_prompt = build_set_creation_prompt(backdrop, note_text)

        logger.info(
            f"Set Creation prompt compiled for {generation_id} "
            f"({len(compiled_prompt)} chars, backdrop={backdrop})",
            extra={"generation_id": generation_id},
        )

        generated_bytes = await nanobana_client.compose_set(
            [img1_url, img2_url],
            prompt=compiled_prompt,
            image_size="3:4",
        )

        output_storage_path = upload_chamak_output(
            file_content=generated_bytes,
            wholesaler_id=wholesaler_id,
            generation_id=generation_id,
        )

        await update_chamak_generation(
            generation_id,
            {
                "compiled_prompt_text": compiled_prompt,
                "prompt_version": settings.CHAMAK_PROMPT_VERSION,
                "output_image_url": output_storage_path,
                "status": "done",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        logger.info(
            f"Set Creation finished for {generation_id}",
            extra={
                "generation_id": generation_id,
                "output_image_url": output_storage_path,
            },
        )

    except Exception as exc:
        logger.error(
            f"Set Creation generation failed for {generation_id}: {exc}",
            extra={"generation_id": generation_id},
            exc_info=True,
        )
        if settings.SENTRY_DSN:
            sentry_sdk.capture_exception(exc)

        try:
            await update_chamak_generation(generation_id, {"status": "failed"})
        except Exception:
            logger.error(
                f"Failed to persist failure status for {generation_id}", exc_info=True
            )

        await _refund_failed_generation(generation_id, "Set creation failed")
