"""Linked Google Sheets: fingerprint, nightly cutoff, due selection, check behaviour."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.catalogue_import import sheet_sync
from app.services.catalogue_import.reader import read_source_file

CSV = b"Product Code,Product Name,Type,Metal,Gross Weight (g)\n110801,Ring A,Rings,Gold,6.42\n120801,Neck B,Necklace,Gold,38.6\n"
PROFILE = {
    "id": "prof-1", "wholesaler_id": "user-1", "name": "Stock Sheet", "gsheet_url": "https://docs.google.com/spreadsheets/d/abc/edit",
    "sync_schedule": "daily", "last_content_hash": None, "pending_job_id": None, "last_check_error": None,
}


def _hash(data: bytes) -> str:
    return sheet_sync.content_hash(read_source_file(data, "sheet.csv"))


def test_fingerprint_ignores_file_bytes_but_sees_a_cell_edit():
    assert _hash(CSV) == _hash(CSV)
    assert _hash(CSV) != _hash(CSV.replace(b"6.42", b"6.50"))


def test_midnight_cutoff_is_ist():
    # 19:00 UTC on 24 Nov is 00:30 IST on 25 Nov -> today's 12 am IST was 18:30 UTC on the 24th
    cutoff = sheet_sync.start_of_today_ist(datetime(2026, 11, 24, 19, 0, tzinfo=timezone.utc))
    assert cutoff.astimezone(timezone.utc) == datetime(2026, 11, 24, 18, 30, tzinfo=timezone.utc)


def test_only_create_update_conflict_count_as_an_update():
    assert sheet_sync.is_actionable({"update": 1})
    assert sheet_sync.is_actionable({"conflict": 2})
    assert not sheet_sync.is_actionable({"unchanged": 20, "not_in_file": 3})


@pytest.mark.asyncio
async def test_sweep_checks_only_sheets_not_yet_checked_today():
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {**PROFILE, "id": "never", "last_checked_at": None},
        {**PROFILE, "id": "yesterday", "last_checked_at": "2020-01-01T00:00:00+00:00"},
        {**PROFILE, "id": "today", "last_checked_at": now},
    ]
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.not_.is_.return_value.execute.return_value.data = rows
    with patch.object(sheet_sync, "get_supabase", return_value=sb), \
         patch.object(sheet_sync, "check_once", new=AsyncMock()) as check:
        assert await sheet_sync.run_sweep() == 2
    assert [c.args[0]["id"] for c in check.await_args_list] == ["never", "yesterday"]
    assert all(c.kwargs == {"notify": True} for c in check.await_args_list)


def _sb(job_id="job-1"):
    sb = MagicMock()
    sb.table.return_value.update.return_value.eq.return_value.execute.return_value.data = [dict(PROFILE)]
    sb.table.return_value.insert.return_value.execute.return_value.data = [{"id": job_id}]
    return sb


@pytest.mark.asyncio
async def test_unchanged_sheet_builds_nothing():
    sb = _sb()
    profile = {**PROFILE, "last_content_hash": _hash(CSV)}
    with patch.object(sheet_sync, "get_supabase", return_value=sb), \
         patch.object(sheet_sync, "fetch_google_sheet_xlsx", new=AsyncMock(return_value=(CSV, "sheet.csv"))), \
         patch.object(sheet_sync, "notify_wholesaler", new=AsyncMock()) as notify:
        state = await sheet_sync.check_linked_sheet(profile, notify=True)
    assert state["changed"] is False
    sb.table.return_value.insert.assert_not_called()  # no job created
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_sheet_builds_a_preview_and_notifies_once():
    sb = _sb("job-9")
    profile = {**PROFILE, "last_content_hash": "old"}
    with patch.object(sheet_sync, "get_supabase", return_value=sb), \
         patch.object(sheet_sync, "fetch_google_sheet_xlsx", new=AsyncMock(return_value=(CSV, "sheet.csv"))), \
         patch.object(sheet_sync, "notify_wholesaler", new=AsyncMock()) as notify, \
         patch("app.routers.catalogue_import.read_job", new=AsyncMock()), \
         patch("app.routers.catalogue_import.analyze_job_endpoint", new=AsyncMock(
             return_value={"status": "ready", "summary": {"create": 1, "update": 2}})):
        state = await sheet_sync.check_linked_sheet(profile, notify=True)
    assert state["changed"] is True
    stamped = [c.args[0] for c in sb.table.return_value.update.call_args_list if "pending_job_id" in c.args[0]]
    assert stamped and stamped[0]["pending_job_id"] == "job-9"
    notify.assert_awaited_once()
    assert "1 new, 2 updated" in notify.await_args.args[2]


@pytest.mark.asyncio
async def test_unreadable_sheet_reports_the_reason_and_keeps_everything_else():
    sb = _sb()
    with patch.object(sheet_sync, "get_supabase", return_value=sb), \
         patch.object(sheet_sync, "fetch_google_sheet_xlsx", new=AsyncMock(side_effect=Exception("Open the sheet → Share → Anyone with the link"))), \
         patch.object(sheet_sync, "notify_wholesaler", new=AsyncMock()) as notify:
        await sheet_sync.check_linked_sheet(dict(PROFILE), notify=True)
    sb.table.return_value.insert.assert_not_called()
    assert "last_check_error" in sb.table.return_value.update.call_args.args[0]
    notify.assert_awaited_once()  # first failure only
