"""Recoverable video worker: reconcile uncertain submissions; retry saving only."""
import asyncio
import base64
import hashlib
import math
from datetime import datetime, timezone

from app.config import settings
from app.db import videos as db
from app.logging import logger
from app.services import video_storage
from app.services.kling import KlingClient, SubmissionRejected, SubmissionUnknown, billing_units


async def process_attempt(job: dict, client: KlingClient):
    if job.get("fresh_submission"):
        try:
            expires = datetime.fromisoformat(job["pack_expires_at"].replace("Z","+00:00"))
            if expires <= datetime.now(timezone.utc):
                raise ValueError("The purchased video pack has expired.")
            image = await video_storage.read_snapshot(job["source_path"])
            if hashlib.sha256(image).hexdigest() != job["source_sha256"]:
                raise ValueError("The saved source image changed.")
            parts = job.get("prompt_parts") or {}
            last_frame = None
            if parts.get("last_frame_path"):
                last_frame = await video_storage.read_snapshot(parts["last_frame_path"])
                if hashlib.sha256(last_frame).hexdigest() != parts.get("last_frame_sha256"):
                    raise ValueError("The saved closing frame changed.")
        except Exception:
            await db.update(job,{"status":"failed","error_code":"submission_rejected","message":"The saved source image could not be verified. No provider task was submitted."})
            return
        try:
            task_id = await client.submit(job,base64.b64encode(image).decode(),
                                          base64.b64encode(last_frame).decode() if last_frame else None)
        except SubmissionRejected as exc:
            await db.update(job,{"status":"failed","error_code":"submission_rejected","message":str(exc)})
            return
        except SubmissionUnknown as exc:
            await db.update(job,{"status":"submitting","error_code":"submission_unknown","message":str(exc),"poll_after":10})
            return
        await db.update(job,{"status":"generating","provider_task_id":task_id,"poll_after":5})
        return

    try:
        task = await client.query(job)
        if not task:
            created = datetime.fromisoformat(job["created_at"].replace("Z","+00:00"))
            if (datetime.now(timezone.utc)-created).total_seconds() > 300:
                await db.update(job,{"status":"needs_review","error_code":"task_unconfirmed","message":"The existing attempt could not be confirmed. Its units remain reserved; review it in Kling before starting another."})
            else:
                await db.update(job,{"poll_after":10,"message":"Checking the existing attempt. No new video has been submitted."})
            return
        state = task.get("status")
        reported = billing_units(task) if state in {"failed","succeeded"} else None
        patch = {"provider_task_id":task["id"]}
        if reported is not None:
            patch["billed_units"] = str(reported)
        if state == "failed":
            await db.update(job,{**patch,"status":"failed","error_code":"provider_failed","message":"Kling could not create this video. Review the attempt's billing before generating a new variation."})
        elif state in {"submitted","processing"}:
            created = datetime.fromisoformat(job["created_at"].replace("Z","+00:00"))
            if (datetime.now(timezone.utc)-created).total_seconds() > 1800:
                await db.update(job,{**patch,"status":"needs_review","error_code":"generation_delayed","message":"This video is taking unusually long. Review the existing task in Kling; no replacement has been submitted."})
            else:
                await db.update(job,{**patch,"status":"generating","poll_after":5})
        elif state == "succeeded":
            outputs = [item for item in task.get("outputs",[]) if item.get("type") == "video"]
            # Encoded frame timing can add a fraction of a second (e.g. 5.041s).
            if len(outputs) != 1 or not math.isclose(float(outputs[0].get("duration",0)),job["duration"],rel_tol=0,abs_tol=0.1):
                raise ValueError("Unexpected video output or duration.")
            # Preserve the lease through download/upload. A crash resumes saving,
            # querying the same task for a fresh output URL, never regenerating.
            job = await db.update(job,{**patch,"status":"saving"},release=False)
            video = await client.download(outputs[0]["url"])
            path = f"{job['owner_id']}/{job['id']}/result.mp4"
            await video_storage.upload(path,video,"video/mp4")
            await db.update(job,{"status":"ready","output_path":path})
        else:
            raise ValueError("Unknown provider task status.")
    except ValueError:
        await db.update(job,{"status":"needs_review","error_code":"provider_response_invalid","message":"This provider response needs review. No replacement video was submitted."})
    except Exception:
        logger.warning("Video polling or saving will retry",extra={"video_id":job["id"]})
        await db.update(job,{"poll_after":15,"message":"Still checking or saving this video. No replacement generation has been submitted."})


async def worker_loop():
    client = KlingClient(settings.KLING_API_KEY)
    try:
        while True:
            try:
                job = await db.rpc("video_claim",{"p_concurrency":settings.VIDEO_MAX_CONCURRENT,"p_lease_seconds":180})
                if job:
                    await process_attempt(job,client)
                else:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Video worker unavailable; persisted attempts will resume",exc_info=False)
                await asyncio.sleep(5)
    finally:
        await client.close()
