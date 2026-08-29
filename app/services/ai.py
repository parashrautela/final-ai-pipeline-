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

    # Using generate-pro endpoint for image-to-image editing at 2K.
    # 2K is the deliberate choice here, not 4K: 4K carries a real credit
    # premium over 2K on this API for the same image, and 2K is judged the
    # better tradeoff against IMAGE_GENERATION_COUNT variants per product —
    # more images at 2K beats fewer at 4K for the same spend.
    _GENERATE_URL = "https://api.nanobananaapi.ai/api/v1/nanobanana/generate-pro"
    _STATUS_URL = "https://api.nanobananaapi.ai/api/v1/nanobanana/record-info"

    def __init__(self) -> None:
        self._headers = {
            "Authorization": f"Bearer {settings.NANOBANA_API_KEY}",
            "Content-Type": "application/json",
        }

    # Hard API limit, verified against the live endpoint: generate-pro answers
    # 422 "Prompt length must be between 3 and 5000" above this. Measured
    # exactly — 5000 is accepted, 5001 is rejected. This is a limit of the
    # nanobananaapi.ai wrapper, NOT of the underlying model (which takes a 64K
    # token context), so it cannot be raised by switching model or resolution.
    #
    # NOTE: the composed base + category module + scene currently runs ~18k
    # chars, so ~72% of every prompt is being discarded here. Truncation is a
    # safety net, not a solution — the prompt_modules rows need to be rewritten
    # to fit inside this budget for the category rules to actually reach the model.
    _MAX_PROMPT_CHARS = 5000

    # Truncation keeps this many trailing chars. `pipeline.py` appends the
    # per-variant SCENE directive LAST (longest is ~226 chars), so a plain
    # head-slice would delete the only text that differs between variants and
    # render 4 identical images.
    _PROMPT_TAIL_CHARS = 400

    async def enhance_image(
        self, image_url: str, *, prompt: str | None = None
    ) -> bytes:
        """Send image URL to Nanobana Pro, return 2K enhanced image bytes."""
        active_prompt = prompt if prompt is not None else settings.NANOBANA_PROMPT

        # Keep the head and the tail so the trailing SCENE directive survives —
        # see _PROMPT_TAIL_CHARS. The elision marker counts against the budget,
        # so subtract it too: an off-by-one here is a 422 from the API.
        if len(active_prompt) > self._MAX_PROMPT_CHARS:
            elision = "\n\n[...]\n\n"
            head_chars = (
                self._MAX_PROMPT_CHARS - self._PROMPT_TAIL_CHARS - len(elision)
            )
            logger.warning(
                f"Prompt is {len(active_prompt)} chars, over the {self._MAX_PROMPT_CHARS} "
                f"limit — keeping the first {head_chars} and last {self._PROMPT_TAIL_CHARS}. "
                f"Category-module rules in the middle are being dropped."
            )
            active_prompt = (
                active_prompt[:head_chars]
                + elision
                + active_prompt[-self._PROMPT_TAIL_CHARS :]
            )

        # generate-pro requires "resolution" (not "image_size" — that belongs
        # to the plain /generate endpoint and 422s here). Value comes from
        # settings.nanobana_resolution (NANOBANA_IMAGE_SIZE on Railway) rather
        # than a literal — see the class-level comment for why 2K is the
        # default.
        payload = {
            "prompt": active_prompt,
            "type": "IMAGETOIAMGE",
            "imageUrls": [image_url],
            "resolution": settings.nanobana_resolution,
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

    # ── Set Creation ────────────────────────────────────────────────────────
    # Deliberately separate from enhance_image() above rather than a flag on
    # it, for two reasons:
    #
    # 1. Different endpoint. enhance_image uses /generate-pro; this uses plain
    #    /generate, which NANOBANA_API_REFERENCE.md measures at ~2 credits per
    #    image against generate-pro's 9-12. Set Creation does not need the pro
    #    endpoint, and switching enhance_image over is a live-pricing change to
    #    the product pipeline that belongs in its own reviewed commit.
    # 2. Different input shape. This sends MULTIPLE imageUrls; enhance_image
    #    has only ever sent one.
    #
    # Verified live against the API on 2026-08-29: two URLs in `imageUrls`
    # are accepted, both are echoed back in the task's paramJson, and both
    # appear faithfully in the output image.
    _GENERATE_URL_BASIC = "https://api.nanobananaapi.ai/api/v1/nanobanana/generate"

    # The API caps prompts the same way on both endpoints.
    _SET_MAX_PROMPT_CHARS = 5000

    async def _await_task(self, task_id: str, *, label: str = "task") -> bytes:
        """Poll record-info until the task finishes, then download the result.

        Mirrors the polling in enhance_image(). Kept as its own method so the
        live product/fusion path is not touched while Set Creation is being
        proven; fold the two together once it is.
        """
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

            data = status_data.get("data") or {}

            if i % 5 == 0:
                logger.info(
                    f"Nanobana {label} waiting... {(i + 1) * poll_interval}s elapsed "
                    f"(poll {i + 1}/{max_polls}) taskId={task_id}"
                )

            if data.get("successFlag") in (1, "1"):
                res_url = (
                    (data.get("response") or {}).get("resultImageUrl")
                    or data.get("resultImageUrl")
                    or data.get("imageUrl")
                )
                if not res_url:
                    raise ValueError(
                        f"Nanobana {label} {task_id} succeeded but returned no image URL. "
                        f"Response: {status_data}"
                    )
                async with httpx.AsyncClient(timeout=120.0) as dl_client:
                    img = await dl_client.get(res_url, follow_redirects=True)
                    img.raise_for_status()
                    logger.info(f"Downloaded Nanobana {label} result: {len(img.content)} bytes")
                    return img.content

            if data.get("failFlag") in (1, "1") or status_data.get("failFlag") in (1, "1"):
                raise RuntimeError(
                    f"Nanobana {label} {task_id} failed: "
                    f"{data.get('errorMessage') or status_data}"
                )

        raise TimeoutError(
            f"Nanobana {label} {task_id} did not finish within "
            f"{max_polls * poll_interval}s"
        )

    async def compose_set(
        self,
        image_urls: list[str],
        *,
        prompt: str,
        image_size: str = "3:4",
    ) -> bytes:
        """Compose several source images into ONE staged photograph.

        Every URL must be publicly fetchable — the API downloads them itself
        and will not accept inline bytes or a Supabase signed URL that has
        already expired.

        Order is load-bearing: the prompt refers to "Image 1" and "Image 2" and
        hangs position-dependent instructions off both, so the caller must pass
        them in the order the prompt describes.
        """
        if not image_urls:
            raise ValueError("compose_set requires at least one image URL")

        active_prompt = prompt
        if len(active_prompt) > self._SET_MAX_PROMPT_CHARS:
            logger.warning(
                f"Set prompt is {len(active_prompt)} chars, over the "
                f"{self._SET_MAX_PROMPT_CHARS} limit — truncating."
            )
            active_prompt = active_prompt[: self._SET_MAX_PROMPT_CHARS]

        payload = {
            "prompt": active_prompt,
            # The vendor's own spelling. Not a typo on our side — see
            # NANOBANA_API_REFERENCE.md; "IMAGETOIMAGE" is rejected.
            "type": "IMAGETOIAMGE",
            "imageUrls": image_urls,
            "numImages": 1,
            "image_size": image_size,
            "callBackUrl": "https://api.nanobananaapi.ai/callback",
        }

        logger.info(
            f"Nanobana set composition — {len(image_urls)} source images, "
            f"image_size={image_size}, prompt={len(active_prompt)} chars"
        )

        async with httpx.AsyncClient(timeout=60.0) as submit_client:
            response = await _request_with_retry(
                submit_client,
                "POST",
                self._GENERATE_URL_BASIC,
                headers=self._headers,
                json=payload,
                max_retries=settings.MAX_RETRIES,
            )
            task_data = response.json()

        data_obj = task_data.get("data") or {}
        task_id = (
            task_data.get("taskId") or data_obj.get("taskId") or data_obj.get("id")
        )
        if not task_id:
            raise ValueError(f"Failed to get taskId from Nanobana: {task_data}")

        logger.info(f"Nanobana set task queued — taskId={task_id}")
        return await self._await_task(task_id, label="set")


class OpenAIImageClient:
    """OpenAI image generation — the renderer behind Chamak 2.0.

    Exists to answer one question the Nanobana path could not: what a fusion
    looks like when BOTH source designs actually reach the image model.

    `NanobanaClient.enhance_image` — the renderer Chamak 1.0 calls — accepts a
    single `image_url` and has only ever sent one. Design 2 reaches that model
    as prose in the compiled prompt and never as a picture. This client sends
    both images as real reference inputs, so 1.0 and 2.0 can be compared on
    identical inputs with that one variable changed.

    Unlike Nanobana this endpoint is synchronous: the image comes back in the
    response body, so there is no task id and nothing to poll.
    """

    _EDITS_URL = "https://api.openai.com/v1/images/edits"

    # The endpoint's own documented ceiling is far higher than anything the
    # compiler produces, but truncating beats a 400 on a paid call.
    _MAX_PROMPT_CHARS = 30000

    def __init__(self) -> None:
        self._api_key = settings.OPENAI_API_KEY

    @property
    def _headers(self) -> dict:
        # Content-Type is deliberately absent: httpx sets it, with the
        # multipart boundary, when `files=` is passed. Setting it by hand
        # produces a boundary-less header and a 400.
        return {"Authorization": f"Bearer {self._api_key}"}

    @staticmethod
    def _filename_for(mime: str, index: int) -> str:
        ext = {
            "image/png": "png",
            "image/webp": "webp",
            "image/jpeg": "jpg",
        }.get(mime, "jpg")
        return f"design{index}.{ext}"

    async def fuse_images(
        self,
        images: list[tuple[bytes, str]],
        *,
        prompt: str,
    ) -> bytes:
        """Blend several reference images into ONE new image.

        `images` is a list of (bytes, mime_type). Order is load-bearing — the
        compiled prompt refers to "Image 1" and "Image 2" positionally, so the
        caller must pass them in the order the prompt describes.

        Returns the raw bytes of the generated image.
        """
        if not images:
            raise ValueError("fuse_images requires at least one image")
        if not self._api_key:
            raise ValueError("OPENAI_API_KEY is not configured in settings")

        active_prompt = prompt
        if len(active_prompt) > self._MAX_PROMPT_CHARS:
            logger.warning(
                f"OpenAI prompt is {len(active_prompt)} chars, over the "
                f"{self._MAX_PROMPT_CHARS} limit — truncating."
            )
            active_prompt = active_prompt[: self._MAX_PROMPT_CHARS]

        # Repeated `image[]` parts is the vendor's own multipart form for
        # multi-reference edits. Each part carries an explicit filename and
        # content type — without them the part is sent as
        # application/octet-stream and rejected with a 400 that names the
        # image, not the cause.
        files = [
            ("image[]", (self._filename_for(mime, i + 1), data, mime))
            for i, (data, mime) in enumerate(images)
        ]
        form = {
            "model": settings.OPENAI_IMAGE_MODEL,
            "prompt": active_prompt,
            "size": settings.OPENAI_IMAGE_SIZE,
            "n": "1",
        }

        logger.info(
            f"OpenAI image fusion — {len(images)} source images, "
            f"model={settings.OPENAI_IMAGE_MODEL}, "
            f"size={settings.OPENAI_IMAGE_SIZE}, "
            f"prompt={len(active_prompt)} chars"
        )

        timeout = settings.OPENAI_IMAGE_TIMEOUT_SECONDS
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await _request_with_retry(
                client,
                "POST",
                self._EDITS_URL,
                headers=self._headers,
                files=files,
                data=form,
                max_retries=settings.MAX_RETRIES,
            )
            payload = response.json()

        entries = payload.get("data") or []
        if not entries:
            raise ValueError(f"OpenAI returned no image data: {payload}")

        # GPT-image models always return base64; the `url` field that older
        # DALL-E responses populated is never set, so there is deliberately no
        # URL fallback here — a missing b64_json is a real error, not a
        # different response shape to accommodate.
        b64 = entries[0].get("b64_json")
        if not b64:
            raise ValueError(
                f"OpenAI response had no b64_json. Keys present: {list(entries[0].keys())}"
            )

        image_bytes = base64.b64decode(b64)
        usage = payload.get("usage") or {}
        logger.info(
            f"OpenAI image fusion complete — {len(image_bytes)} bytes, usage={usage}"
        )
        return image_bytes


# Instantiated once at module load and reused across requests.
# Keeps API key parsing and header setup out of every request path.
reve_client = ReveClient()
nanobana_client = NanobanaClient()
openai_image_client = OpenAIImageClient()
