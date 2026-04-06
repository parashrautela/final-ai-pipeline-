from __future__ import annotations

import asyncio
import base64
import json

import httpx
from config import settings
from logging_config import logger


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict,
    max_retries: int,
    retry_count: int = 0,
    **kwargs,
) -> httpx.Response:
    """Generic request helper with exponential back-off on 429 / 5xx."""
    response = await client.request(method, url, headers=headers, **kwargs)

    if (
        response.status_code == 429 or response.status_code >= 500
    ) and retry_count < max_retries:
        wait = 2**retry_count
        logger.warning(
            f"HTTP {response.status_code} from {url} — retrying in {wait}s "
            f"({retry_count + 1}/{max_retries})"
        )
        await asyncio.sleep(wait)
        return await _request_with_retry(
            client,
            method,
            url,
            headers=headers,
            max_retries=max_retries,
            retry_count=retry_count + 1,
            **kwargs,
        )

    if not response.is_success:
        try:
            body = response.json()
        except Exception:
            body = response.text
        logger.error(f"API error {response.status_code} from {url} ({method}): {body}")

    response.raise_for_status()
    return response


async def _post_with_retry(
    client: httpx.AsyncClient, url: str, **kwargs
) -> httpx.Response:
    """Backwards compat wrapper for POST."""
    return await _request_with_retry(client, "POST", url, **kwargs)


# ---------------------------------------------------------------------------
# Reve  —  background removal
# ---------------------------------------------------------------------------
# curl -X POST https://api.reve.ai/v1/image/edit \
#   -H "Authorization: Bearer <key>" \
#   -F "image=@input.png" \
#   -F "prompt=..."
# Returns: raw image bytes (PNG)
# ---------------------------------------------------------------------------


class ReveClient:
    _BASE_URL = "https://api.reve.com/v1/image/edit"

    def __init__(self) -> None:
        self._headers = {"Authorization": f"Bearer {settings.REVE_API_KEY}"}

    async def remove_background(self, image_bytes: bytes) -> bytes:
        """
        Send an image to Reve for background removal.
        Returns the processed image as raw bytes.
        """
        # Encode image to base64
        base64_image = base64.b64encode(image_bytes).decode("utf-8")

        json_data = {
            "edit_instruction": settings.REVE_PROMPT,
            "reference_image": base64_image,
            "version": "latest",
        }

        try:
            async with httpx.AsyncClient(timeout=150.0) as client:
                response = await _post_with_retry(
                    client,
                    self._BASE_URL,
                    headers=self._headers,
                    json=json_data,
                    max_retries=settings.MAX_RETRIES,
                )

            # The subagent didn't specify the response format,
            # let's check if it returns raw bytes or JSON with base64.
            # Most JSON-in APIs return JSON-out.
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                data = response.json()
                # If it's a simple {image: base64} response
                if "image" in data:
                    return base64.b64decode(data["image"])
                # Fallback to the generic extractor (for Gemini/complex formats)
                return _extract_image_bytes(data)

            logger.info(
                f"Reve response: {response.status_code}, {len(response.content)} bytes"
            )
            return response.content

        except Exception as exc:
            logger.error(f"Reve remove_background failed: {exc}")
            raise


# ---------------------------------------------------------------------------
# Nanobana  —  scene enhancement
# ---------------------------------------------------------------------------


class NanobanaClient:
    # Using the standard generate endpoint for image-to-image editing
    # When imageUrls is provided, API automatically treats it as image-to-image
    # Pricing: Nano Banana Edit (image-to-image) = $0.02/image = ~2 credits
    _GENERATE_URL = "https://api.nanobananaapi.ai/api/v1/nanobanana/generate"
    _STATUS_URL = "https://api.nanobananaapi.ai/api/v1/nanobanana/record-info"

    def __init__(self) -> None:
        self._headers = {
            "Authorization": f"Bearer {settings.NANOBANA_API_KEY}",
            "Content-Type": "application/json",
        }

    async def enhance_image(
        self, image_url: str, *, prompt: str | None = None
    ) -> bytes:
        """
        Send a background-removed image URL to Nanobana for scene enhancement.

        Parameters
        ----------
        image_url : str
            Public URL of the background-removed image produced by Reve.
        prompt : str | None
            Override the generation prompt.  When *None* (default), falls back
            to ``settings.NANOBANA_PROMPT`` so single-image and multi-variant
            callers both work correctly.

        Returns
        -------
        bytes
            Raw PNG bytes of the generated image.
        """
        active_prompt = prompt if prompt is not None else settings.NANOBANA_PROMPT
        # The API documentation shows type must be "IMAGETOIMAGE" but it's now
        # rejecting that. Try sending the minimal payload to see what works.
        payload = {
            "prompt": active_prompt,
            "imageUrls": [image_url],
        }
        try:
            # ── Step 1: Submit task (short timeout — just an HTTP POST) ────────
            async with httpx.AsyncClient(timeout=60.0) as submit_client:
                logger.info(f"Starting Nanobana task for {image_url}")
                response = await _request_with_retry(
                    submit_client,
                    "POST",
                    self._GENERATE_URL,
                    headers=self._headers,
                    json=payload,
                    max_retries=settings.MAX_RETRIES,
                )
                task_data = response.json()

            logger.info(f"Nanobana generate raw response: {task_data!r}")

            if not isinstance(task_data, dict):
                raise ValueError(
                    f"Nanobana generate returned unexpected body (expected JSON object): "
                    f"{task_data!r}"
                )

            # Use `or {}` (not `get("data", {})`) so an explicit null `data` value
            # is also handled — dict.get(key, default) only uses the default when
            # the key is absent, not when it is present with a null value.
            data_obj = task_data.get("data") or {}
            task_id = (
                task_data.get("taskId") or data_obj.get("taskId") or data_obj.get("id")
            )
            if not task_id:
                raise ValueError(f"Failed to get taskId from Nanobana: {task_data}")

            logger.info(f"Nanobana task submitted: {task_id}, beginning poll loop")

            # ── Step 2: Poll for completion (separate client — no global timeout)
            max_polls = 60  # 60 × 5 s = 5 min maximum
            poll_interval = 5  # seconds between polls

            for i in range(max_polls):
                await asyncio.sleep(poll_interval)

                async with httpx.AsyncClient(timeout=30.0) as poll_client:
                    status_response = await _request_with_retry(
                        poll_client,
                        "GET",
                        f"{self._STATUS_URL}?taskId={task_id}",
                        headers=self._headers,
                        max_retries=2,
                    )
                    status_data = status_response.json()

                # Log raw response every 3 polls so we can see the shape
                if i % 3 == 0:
                    logger.info(
                        f"Nanobana poll {i}/{max_polls} — taskId={task_id} "
                        f"raw_response={status_data}"
                    )

                data = status_data.get("data") or {}

                # successFlag can be int 1 or string "1" — handle both
                success = data.get("successFlag") in (1, "1") or status_data.get(
                    "successFlag"
                ) in (1, "1")

                if success:
                    # Try every known field path for the result image URL
                    res_url = (
                        (data.get("response") or {}).get("resultImageUrl")
                        or data.get("resultImageUrl")
                        or data.get("result_image_url")
                        or data.get("imageUrl")
                        or data.get("image_url")
                        or status_data.get("resultImageUrl")
                        or status_data.get("imageUrl")
                    )

                    if not res_url:
                        # Log the full response so we can identify the correct key
                        logger.error(
                            f"Nanobana task {task_id} succeeded but no image URL found. "
                            f"Full response: {status_data}"
                        )
                        raise ValueError(
                            f"Nanobana task {task_id} succeeded but result URL not found. "
                            f"Response: {status_data}"
                        )

                    logger.info(
                        f"Nanobana task {task_id} complete — result URL: {res_url}"
                    )

                    # ── Step 3: Download result (separate client, generous timeout) ─
                    async with httpx.AsyncClient(timeout=120.0) as dl_client:
                        img_resp = await dl_client.get(res_url, follow_redirects=True)
                        img_resp.raise_for_status()
                        logger.info(
                            f"Downloaded Nanobana result: {len(img_resp.content)} bytes"
                        )
                        return img_resp.content

                # Log failure flag if present
                fail_flag = data.get("failFlag") or status_data.get("failFlag")
                if fail_flag in (1, "1"):
                    raise RuntimeError(
                        f"Nanobana task {task_id} reported failure. Response: {status_data}"
                    )

            raise TimeoutError(
                f"Nanobana task {task_id} did not complete within "
                f"{max_polls * poll_interval}s ({max_polls} polls)"
            )

        except Exception as exc:
            logger.error(f"Nanobana enhance_image failed: {exc}", exc_info=True)
            raise


def _extract_image_bytes(response_json: dict) -> bytes:
    """
    Extract image bytes from a Nanobana (Gemini-style) response.

    Tries, in order:
    1. candidates[0].content.parts[*].inline_data.data  (base64 image)
    2. candidates[0].content.parts[*].text  (public URL — download it)
    3. Raw bytes if response was already binary (fallback).
    """
    try:
        candidates = response_json.get("candidates", [])
        if not candidates:
            raise ValueError(f"No candidates in Nanobana response: {response_json}")

        parts = candidates[0]["content"]["parts"]

        for part in parts:
            # ── base64 inline image ─────────────────────────────────────
            inline = part.get("inline_data")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"])

            # ── public URL ──────────────────────────────────────────────
            text = part.get("text", "")
            if text.startswith("http"):
                import httpx as _httpx

                r = _httpx.get(text, timeout=60.0, follow_redirects=True)
                r.raise_for_status()
                return r.content

        raise ValueError(f"Could not extract image from Nanobana parts: {parts}")

    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(
            f"Unexpected Nanobana response structure: {response_json}"
        ) from exc


# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

reve_client = ReveClient()
nanobana_client = NanobanaClient()
