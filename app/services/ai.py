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

    MODEL = "nano-banana-2"
    RESOLUTION = "2K"
    _GENERATE_URL = "https://api.nanobananaapi.ai/api/v1/nanobanana/generate-2"
    _STATUS_URL = "https://api.nanobananaapi.ai/api/v1/nanobanana/record-info"

    def __init__(self) -> None:
        self._headers = {
            "Authorization": f"Bearer {settings.NANOBANA_API_KEY}",
            "Content-Type": "application/json",
        }

    # Generate-2 supports 20,000 characters; never discard product/scene rules.
    _MAX_PROMPT_CHARS = 20000

    def _validate_prompt(self, prompt: str) -> None:
        if not 3 <= len(prompt) <= self._MAX_PROMPT_CHARS:
            raise ValueError(
                f"Nano Banana 2 prompt must be 3–{self._MAX_PROMPT_CHARS} characters"
            )

    async def enhance_image(
        self, image_url: str, *, prompt: str | None = None
    ) -> bytes:
        """Send a source image to Nano Banana 2 and return a 2K image."""
        active_prompt = prompt if prompt is not None else settings.NANOBANA_PROMPT
        self._validate_prompt(active_prompt)
        payload = {
            "prompt": active_prompt,
            "imageUrls": [image_url],
            "aspectRatio": "1:1",
            "resolution": self.RESOLUTION,
            "googleSearch": False,
            "outputFormat": "png",
        }
        try:
            # Step 1: Submit the generation task and get back a task ID.
            async with httpx.AsyncClient(timeout=60.0) as submit_client:
                logger.info(
                    f"Nanobana request — URL: {self._GENERATE_URL}, "
                    f"model={self.MODEL}, resolution={self.RESOLUTION}, prompt={len(active_prompt)} chars"
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

            return await self._await_task(task_id, label="image")

        except Exception as exc:
            logger.error(f"Nanobana enhance_image failed: {exc}", exc_info=True)
            raise

    # Set Creation sends multiple source images to the same model.
    _SET_GENERATE_URL = _GENERATE_URL
    _SET_MAX_PROMPT_CHARS = _MAX_PROMPT_CHARS

    async def _await_task(self, task_id: str, *, label: str = "task") -> bytes:
        """Poll record-info until the task finishes, then download the result."""
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

            flag = data.get("successFlag", status_data.get("successFlag"))
            if flag in (1, "1"):
                res_url = (
                    (data.get("response") or {}).get("resultImageUrl")
                    or data.get("resultImageUrl")
                    or data.get("imageUrl")
                    or data.get("result_image_url")
                    or data.get("image_url")
                    or status_data.get("resultImageUrl")
                    or status_data.get("imageUrl")
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

            if flag in (2, "2", 3, "3") or data.get("failFlag") in (1, "1") or status_data.get("failFlag") in (1, "1"):
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
        image_size: str = "2:3",
        output_count: int | None = None,
        resolution: str = "2K",
    ) -> list[bytes]:
        """Compose several source images into configurable staged photographs.

        Every URL must be publicly fetchable — the API downloads them itself
        and will not accept inline bytes or a Supabase signed URL that has
        already expired.

        Order is load-bearing: the prompt refers to "Image 1" and "Image 2" and
        hangs position-dependent instructions off both, so the caller must pass
        them in the order the prompt describes.
        """
        if not 1 <= len(image_urls) <= 14:
            raise ValueError("compose_set requires between 1 and 14 image URLs")

        active_prompt = prompt
        self._validate_prompt(active_prompt)
        # Keep the argument compatible with existing callers, but the selected
        # model policy is 2K even if Railway still supplies a legacy 4K setting.
        if (resolution or "").upper() != self.RESOLUTION:
            logger.warning("Ignoring legacy set resolution; Nano Banana 2 outputs use 2K")

        count = max(1, min(int(output_count or settings.set_creation_output_count), 4))

        async def generate_one(index: int) -> bytes:
            payload = {
                "prompt": active_prompt,
                "imageUrls": image_urls,
                "resolution": self.RESOLUTION,
                "aspectRatio": image_size,
                "googleSearch": False,
                "outputFormat": "png",
            }
            logger.info(
                f"Nano Banana 2 set composition {index + 1}/{count} — "
                f"resolution={payload['resolution']}, image_size={image_size}, "
                f"prompt={len(active_prompt)} chars"
            )
            async with httpx.AsyncClient(timeout=60.0) as submit_client:
                response = await _request_with_retry(
                    submit_client,
                    "POST",
                    self._SET_GENERATE_URL,
                    headers=self._headers,
                    json=payload,
                    max_retries=settings.MAX_RETRIES,
                )
                task_data = response.json()

            data_obj = task_data.get("data") or {}
            task_id = task_data.get("taskId") or data_obj.get("taskId") or data_obj.get("id")
            if not task_id:
                raise ValueError(f"Failed to get taskId from Nano Banana 2: {task_data}")
            logger.info(f"Nano Banana 2 set task queued — taskId={task_id}")
            return await self._await_task(task_id, label=f"set {index + 1}/{count}")

        return await asyncio.gather(*(generate_one(i) for i in range(count)))


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
