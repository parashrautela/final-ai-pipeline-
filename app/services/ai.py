from __future__ import annotations

import asyncio
import base64

import httpx
from app.config import settings
from app.logging import logger


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
    """Generic request helper with exponential back-off on 429/5xx."""
    response = await client.request(method, url, headers=headers, **kwargs)

    # 429 means we're being throttled; 5xx means the upstream had a hiccup.
    # Both are worth retrying with a back-off — 4xx errors other than 429 are
    # our fault and retrying won't help.
    if (
        response.status_code == 429 or response.status_code >= 500
    ) and retry_count < max_retries:
        wait = 2**retry_count
        logger.warning(
            f"HTTP {response.status_code} from {url} — retrying in {wait}s ({retry_count + 1}/{max_retries})"
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


def _extract_image_bytes(response_json: dict) -> bytes:
    """Extract image bytes from a Gemini-style response."""
    try:
        candidates = response_json.get("candidates", [])
        if not candidates:
            raise ValueError(f"No candidates in response: {response_json}")

        parts = candidates[0]["content"]["parts"]

        for part in parts:
            inline = part.get("inline_data")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"])

            text = part.get("text", "")
            if text.startswith("http"):
                r = httpx.get(text, timeout=60.0, follow_redirects=True)
                r.raise_for_status()
                return r.content

        raise ValueError(f"Could not extract image from parts: {parts}")
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"Unexpected response structure: {response_json}") from exc


class ReveClient:
    """Client for Reve background removal API."""

    _BASE_URL = "https://api.reve.com/v1/image/edit"

    def __init__(self) -> None:
        self._headers = {"Authorization": f"Bearer {settings.REVE_API_KEY}"}

    async def remove_background(self, image_bytes: bytes) -> bytes:
        """Send image to Reve for background removal, return processed bytes."""
        base64_image = base64.b64encode(image_bytes).decode("utf-8")
        json_data = {
            "edit_instruction": settings.REVE_PROMPT,
            "reference_image": base64_image,
            "version": "latest",
        }

        try:
            async with httpx.AsyncClient(timeout=150.0) as client:
                response = await _request_with_retry(
                    client,
                    "POST",
                    self._BASE_URL,
                    headers=self._headers,
                    json=json_data,
                    max_retries=settings.MAX_RETRIES,
                )

            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                data = response.json()
                # Some Reve response shapes return base64 directly under "image";
                # others use the Gemini-style candidates structure.
                if "image" in data:
                    return base64.b64decode(data["image"])
                return _extract_image_bytes(data)

            logger.info(
                f"Reve response: {response.status_code}, {len(response.content)} bytes"
            )
            return response.content

        except Exception as exc:
            logger.error(f"Reve remove_background failed: {exc}")
            raise


class NanobanaClient:
    """Client for Nanobana scene enhancement API."""

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
        """Send background-removed image URL to Nanobana, return enhanced image bytes."""
        active_prompt = prompt if prompt is not None else settings.NANOBANA_PROMPT
        # According to official docs: https://docs.nanobananaapi.ai/nanobanana-api/generate-or-edit-image
        # type must be "IMAGETOIAMGE" (note the typo in API - it's IAMGE not IMAGE!)
        payload = {
            "prompt": active_prompt,
            "type": "IMAGETOIAMGE",
            "imageUrls": [image_url],
            "callBackUrl": "https://api.nanobananaapi.ai/callback",  # Required by API
        }
        try:
            # Step 1: Submit the generation task and get back a task ID.
            async with httpx.AsyncClient(timeout=60.0) as submit_client:
                logger.info(
                    f"Nanobana request — URL: {self._GENERATE_URL}, "
                    f"payload: {payload}"
                )
                response = await _request_with_retry(
                    submit_client,
                    "POST",
                    self._GENERATE_URL,
                    headers=self._headers,
                    json=payload,
                    max_retries=settings.MAX_RETRIES,
                )
                task_data = response.json()
                logger.info(f"Nanobana response: {task_data}")

            if not isinstance(task_data, dict):
                raise ValueError(f"Unexpected response (expected dict): {task_data!r}")

            data_obj = task_data.get("data") or {}
            task_id = (
                task_data.get("taskId") or data_obj.get("taskId") or data_obj.get("id")
            )
            if not task_id:
                raise ValueError(f"Failed to get taskId from Nanobana: {task_data}")

            logger.info(f"Nanobana task queued — taskId={task_id}")

            # Step 2: Poll the status endpoint until finished or timed out.
            # Nanobana is async — generation typically takes 10-40 seconds.
            max_polls = 60
            poll_interval = 5

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

                # Log every 5th poll so you can see it's alive without flooding
                if i % 5 == 0:
                    elapsed = (i + 1) * poll_interval
                    logger.info(
                        f"Nanobana waiting... {elapsed}s elapsed (poll {i + 1}/{max_polls})  taskId={task_id}"
                    )

                data = status_data.get("data") or {}
                success = data.get("successFlag") in (1, "1") or status_data.get(
                    "successFlag"
                ) in (1, "1")

                if success:
                    # The result URL can appear in several places depending on the API version.
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
                        logger.error(
                            f"Nanobana task {task_id} succeeded but no image URL. Response: {status_data}"
                        )
                        raise ValueError(
                            f"Nanobana task {task_id} succeeded but no URL. Response: {status_data}"
                        )

                    logger.info(
                        f"Nanobana task {task_id} complete — result URL: {res_url}"
                    )

                    # Step 3: Download the generated image from the result URL.
                    async with httpx.AsyncClient(timeout=120.0) as dl_client:
                        img_resp = await dl_client.get(res_url, follow_redirects=True)
                        img_resp.raise_for_status()
                        logger.info(
                            f"Downloaded Nanobana result: {len(img_resp.content)} bytes"
                        )
                        return img_resp.content

                fail_flag = data.get("failFlag") or status_data.get("failFlag")
                if fail_flag in (1, "1"):
                    raise RuntimeError(
                        f"Nanobana task {task_id} failed. Response: {status_data}"
                    )

            raise TimeoutError(
                f"Nanobana task {task_id} did not complete within {max_polls * poll_interval}s"
            )

        except Exception as exc:
            logger.error(f"Nanobana enhance_image failed: {exc}", exc_info=True)
            raise


# Instantiated once at module load and reused across requests.
# Keeps API key parsing and header setup out of every request path.
reve_client = ReveClient()
nanobana_client = NanobanaClient()
