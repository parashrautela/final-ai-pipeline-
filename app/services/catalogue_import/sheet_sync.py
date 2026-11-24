"""app/services/catalogue_import/sheet_sync.py

Linked Google Sheets: "has my sheet changed?" checks, the nightly sweep and the push.

A linked sheet is a catalogue_import_profiles row with a gsheet_url. A check fetches the sheet,
fingerprints its content, and, when it changed (or the waiting preview has gone stale), builds a
fresh import job through the normal read + analyze pipeline. That job is the "update available"
preview the wholesaler reviews. A check never writes products: only the wholesaler's own
"Update now" (the normal /commit) does. Nothing is published, deleted or run through AI.

Nightly: sheets with sync_schedule = 'daily' are due when last_checked_at is before today 00:00 IST.
That makes the sweep self-healing (a missed midnight runs on the next tick) and safe to repeat.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException

from app.db.repository import get_supabase
from app.logging import logger
from app.services.catalogue_import.gsheet import fetch_google_sheet_xlsx
from app.services.catalogue_import.reader import read_source_file

IST = timezone(timedelta(hours=5, minutes=30))
SCHEDULE_DAILY = "daily"
REBUILD_AFTER = timedelta(minutes=15)  # an older waiting preview is rebuilt before review: products may have been edited since
SWEEP_EVERY_SECONDS = 600
MAX_SHEETS_PER_SWEEP = 100
CHECK_TIMEOUT_SECONDS = 180

_check_tasks: dict[str, asyncio.Task] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def start_of_today_ist(now: Optional[datetime] = None) -> datetime:
    local = (now or _now()).astimezone(IST)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def content_hash(read_res: Any) -> str:
    """Fingerprint of the sheet's cell values (not the file bytes: Google's export differs each time)."""
    rows = [[r.sheet, r.row_number, r.raw] for r in read_res.rows]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def is_actionable(summary: Optional[dict]) -> bool:
    """Something the wholesaler can apply. 'Not in file' and unchanged rows are not an update."""
    return any((summary or {}).get(k, 0) for k in ("create", "update", "conflict"))


def _pending_job(sb: Any, profile: dict) -> Optional[dict]:
    pid = profile.get("pending_job_id")
    if not pid:
        return None
    rows = sb.table("catalogue_import_jobs").select(
        "id, status, summary, photo_summary, created_at"
    ).eq("id", pid).execute().data or []
    job = rows[0] if rows else None
    if not job or job["status"] not in ("ready", "needs_answers"):
        return None
    return job


def sheet_state(sb: Any, profile: dict, **extra: Any) -> dict:
    """What the app shows for one linked sheet."""
    pending = _pending_job(sb, profile)
    return {
        "id": profile["id"],
        "name": profile.get("name") or "Google Sheet",
        "gsheet_url": profile.get("gsheet_url"),
        "auto_sync": profile.get("sync_schedule") == SCHEDULE_DAILY,
        "last_checked_at": profile.get("last_checked_at"),
        "last_synced_at": profile.get("last_synced_at"),
        "error": profile.get("last_check_error"),
        "pending": None if not pending else {
            "job_id": pending["id"],
            "status": pending["status"],
            "summary": pending.get("summary") or {},
            "photo_summary": pending.get("photo_summary") or {},
            "created_at": pending.get("created_at"),
        },
        **extra,
    }


def list_linked_sheets(user_id: str) -> list[dict]:
    sb = get_supabase()
    rows = (
        sb.table("catalogue_import_profiles").select("*")
        .eq("wholesaler_id", user_id).not_.is_("gsheet_url", "null")
        .order("updated_at", desc=True).execute().data or []
    )
    return [sheet_state(sb, p) for p in rows]


async def notify_wholesaler(user_id: str, title: str, body: str, profile_id: str) -> None:
    """Push to the wholesaler's registered phones. Quietly does nothing when APNs isn't configured."""
    from app.services.manufacturing_scheduler import apns_service  # lazy: heavy module, optional here

    if not apns_service.is_configured:
        return
    sb = get_supabase()
    tokens = sb.table("manufacturing_device_tokens").select(
        "id, device_token, environment"
    ).eq("user_id", user_id).execute().data or []
    for t in tokens:
        ok, status, err = await asyncio.to_thread(
            apns_service.send_push, t["device_token"], title, body,
            {"kind": "catalogue_sheet_update", "profile_id": profile_id},
            t.get("environment") or "production",
        )
        if status == 410:  # unregistered phone: drop its token
            sb.table("manufacturing_device_tokens").delete().eq("id", t["id"]).execute()
        elif not ok:
            logger.warning(f"Sheet update push failed: HTTP {status} {err}")


def _summary_text(summary: dict) -> str:
    parts = []
    if summary.get("create"):
        parts.append(f"{summary['create']} new")
    if summary.get("update"):
        parts.append(f"{summary['update']} updated")
    if summary.get("conflict"):
        parts.append(f"{summary['conflict']} to decide")
    return ", ".join(parts) or "changes"


async def check_linked_sheet(profile: dict, *, notify: bool = False) -> dict:
    """Fetch the sheet now; build a fresh review preview if it changed. Returns the sheet state."""
    from app.routers import catalogue_import as ci  # lazy: the router imports this module

    sb = get_supabase()
    pid, user_id = profile["id"], str(profile["wholesaler_id"])
    now_iso = _now().isoformat()
    last_hash = profile.get("last_content_hash")

    def stamp(**fields: Any) -> dict:
        res = sb.table("catalogue_import_profiles").update(
            {"last_checked_at": now_iso, **fields}
        ).eq("id", pid).execute()
        return (res.data or [profile])[0]

    try:
        xlsx, name = await fetch_google_sheet_xlsx(profile["gsheet_url"])
        read_res = await asyncio.to_thread(read_source_file, xlsx, name)
    except Exception as exc:  # any failure (private sheet, bad file, too many rows) goes to the wholesaler, never raised
        message = str(exc) or "Couldn't read your Google Sheet."
        first_failure = not profile.get("last_check_error")
        updated = stamp(last_check_error=message)
        if notify and first_failure:
            await notify_wholesaler(user_id, "We can't read your sheet", message, pid)
        return sheet_state(sb, updated, changed=False)

    new_hash = content_hash(read_res)
    pending = _pending_job(sb, profile)
    age = _now() - (_parse(pending["created_at"]) or _now()) if pending else timedelta(0)
    stale = bool(pending and pending["status"] == "ready" and age > REBUILD_AFTER)

    if new_hash == last_hash and not stale:
        return sheet_state(sb, stamp(last_check_error=None), changed=False)

    job_id = (sb.table("catalogue_import_jobs").insert({
        "wholesaler_id": user_id, "source_kind": "gsheet", "gsheet_url": profile["gsheet_url"],
        "profile_id": pid, "status": "uploaded", "created_at": now_iso,
    }).execute().data or [{}])[0].get("id")

    try:
        await ci.read_job(job_id, user_id)
        result = await ci.analyze_job_endpoint(job_id, user_id)
    except HTTPException as exc:
        return sheet_state(sb, stamp(last_check_error=str(exc.detail)), changed=False)

    summary = result.get("summary") or {}
    actionable = result.get("status") == "needs_answers" or is_actionable(summary)
    if pending:  # the old waiting preview is replaced
        sb.table("catalogue_import_jobs").update({"status": "cancelled"}).eq("id", pending["id"]).in_(
            "status", ["ready", "needs_answers"]
        ).execute()
    if not actionable:  # nothing to apply (e.g. only formatting changed): drop the empty job
        sb.table("catalogue_import_jobs").update({"status": "cancelled"}).eq("id", job_id).execute()

    updated = stamp(
        last_content_hash=new_hash, last_check_error=None, pending_job_id=job_id if actionable else None,
    )
    really_new = actionable and new_hash != last_hash  # not just a stale rebuild of the same content
    if notify and really_new:
        await notify_wholesaler(
            user_id, "Your Google Sheet has updates", f"{updated.get('name') or 'Your sheet'}: {_summary_text(summary)}. Tap to review.", pid,
        )
    return sheet_state(sb, updated, changed=really_new)


async def check_once(profile: dict, *, notify: bool = False) -> dict:
    """One check per sheet at a time (a double tap or the nightly run must not duplicate work).
    The check keeps running if the phone disconnects."""
    pid = profile["id"]
    task = _check_tasks.get(pid)
    if task is None or task.done():
        task = asyncio.create_task(check_linked_sheet(profile, notify=notify))
        _check_tasks[pid] = task
        task.add_done_callback(lambda _t: _check_tasks.pop(pid, None))
    return await asyncio.shield(task)


async def run_sweep(limit: int = MAX_SHEETS_PER_SWEEP) -> int:
    """Check every nightly-enabled sheet not yet checked since today 00:00 IST, one at a time."""
    sb = get_supabase()
    cutoff = start_of_today_ist()
    rows = (
        sb.table("catalogue_import_profiles").select("*")
        .eq("sync_schedule", SCHEDULE_DAILY).not_.is_("gsheet_url", "null").execute().data or []
    )
    due = [p for p in rows if (_parse(p.get("last_checked_at")) or datetime.min.replace(tzinfo=timezone.utc)) < cutoff][:limit]
    for p in due:
        try:
            await asyncio.wait_for(check_once(p, notify=True), timeout=CHECK_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.error(f"Nightly sheet check failed for {p['id']}: {exc}", exc_info=exc)
            # stamp it so a broken sheet isn't retried every ten minutes until tomorrow
            sb.table("catalogue_import_profiles").update({"last_checked_at": _now().isoformat()}).eq("id", p["id"]).execute()
    return len(due)


async def nightly_loop() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await run_sweep()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Sheet sweep failed: {exc}", exc_info=exc)
        await asyncio.sleep(SWEEP_EVERY_SECONDS)
