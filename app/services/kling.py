"""Kling's current image-to-video API. Paid submissions are never retried here."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import httpx
from app.config import settings

API_BASE = "https://api-singapore.klingai.com"
RATES = {"kling-3.0": Decimal("0.8"), "kling-2.5-turbo": Decimal("0.5")}
PROMPT_LIMITS = {"kling-3.0": 3072, "kling-2.5-turbo": 2500}


class SubmissionUnknown(Exception):
    """The provider may have accepted the paid request; reconcile, never repost."""


class SubmissionRejected(Exception):
    """The provider explicitly rejected the request before creating a task."""


def units(model: str, duration: int) -> Decimal:
    if model not in RATES or type(duration) is not int or duration not in (5, 10):
        raise ValueError("Choose Kling 3.0 or 2.5 Turbo and a duration of 5 or 10 seconds.")
    return RATES[model] * duration


def billing_units(task: dict) -> Decimal | None:
    entries = task.get("billing")
    if not isinstance(entries, list) or not entries:
        return None
    total = Decimal(0)
    for item in entries:
        if item.get("charge_type") != "unit" or item.get("package_type") != "video":
            raise ValueError("Unexpected provider billing type; review the account before generating again.")
        try:
            amount = Decimal(str(item["amount"]))
        except (KeyError, InvalidOperation):
            raise ValueError("Invalid provider billing amount.")
        if not amount.is_finite() or amount < 0:
            raise ValueError("Invalid provider billing amount.")
        total += amount
    return total


def valid_video_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    domains = ("klingai.com", "kling.ai", "kwaivgb.com", "kwaicdn.com")
    return parsed.scheme == "https" and not parsed.username and not parsed.password and parsed.port in (None, 443) and any(host == domain or host.endswith("." + domain) for domain in domains)


class KlingClient:
    def __init__(self, api_key: str, transport=None):
        self.client = httpx.AsyncClient(transport=transport, timeout=30, follow_redirects=False)
        self.api_key = api_key

    async def close(self):
        await self.client.aclose()

    async def submit(self, job: dict, image_base64: str, last_frame_base64: str | None = None) -> str:
        if not settings.VIDEO_ALLOW_PAID_GENERATION:
            raise SubmissionRejected("Paid video generation is disabled. No request was sent to Kling.")
        units(job["model"], job["duration"])
        if not isinstance(job.get("prompt"),str) or not 1 <= len(job["prompt"]) <= PROMPT_LIMITS[job["model"]]:
            raise ValueError("The video prompt exceeds this model's character limit.")
        body = {
            "contents": [{"type": "prompt", "text": job["prompt"]}, {"type": "first_frame", "url": image_base64}],
            "settings": {"resolution": "1080p", "duration": job["duration"]},
            "options": {"external_task_id": job["id"], "watermark_info": {"enabled": False}},
        }
        if last_frame_base64:
            body["contents"].append({"type": "last_frame", "url": last_frame_base64})
        if job["model"] == "kling-3.0":
            body["settings"].update(audio="off", multi_shot=job.get("multi_shot",False))
        try:
            response = await self.client.post(f"{API_BASE}/image-to-video/{job['model']}", json=body, headers={"Authorization": f"Bearer {self.api_key}"})
            if 400 <= response.status_code < 500 and response.status_code not in {408,409}:
                raise SubmissionRejected("Kling rejected this request. Check the API credentials, pack and input settings.")
            response.raise_for_status()
            data = response.json()
            code = data["code"]
            if type(code) is not int:
                raise ValueError("Invalid response code")
            if code != 0:
                raise SubmissionRejected("Kling rejected this request. Check the account and input settings.")
            task_id = data["data"]["id"]
            if not isinstance(task_id, str) or not task_id:
                raise ValueError("Missing task ID")
            return task_id
        except SubmissionRejected:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise SubmissionUnknown("Submission status is uncertain. Checking the existing attempt before any new generation.") from exc

    async def query(self, job: dict) -> dict | None:
        params = {"task_ids": job["provider_task_id"]} if job.get("provider_task_id") else {"external_task_ids": job["id"]}
        response = await self.client.get(f"{API_BASE}/tasks", params=params, headers={"Authorization": f"Bearer {self.api_key}"})
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0 or not isinstance(body.get("data"), list):
            raise ValueError("Could not verify the existing provider task.")
        if not body["data"]:
            return None
        task = body["data"][0]
        if len(body["data"]) != 1 or task.get("external_id") != job["id"] or (job.get("provider_task_id") and task.get("id") != job["provider_task_id"]):
            raise ValueError("Provider task identity does not match this attempt.")
        return task

    async def download(self, url: str) -> bytes:
        # Never attach API credentials to a CDN request or follow an unchecked redirect.
        for _ in range(4):
            if not valid_video_url(url):
                raise ValueError("The output URL is not on an approved Kling video host.")
            async with self.client.stream("GET", url, timeout=60) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers["location"]))
                    continue
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 100 * 1024 * 1024:
                        raise ValueError("Video exceeds the 100 MB pilot limit.")
            if len(content) < 12 or content[4:8] != b"ftyp":
                raise ValueError("The provider did not return an MP4 video.")
            return bytes(content)
        raise ValueError("Too many video download redirects.")
