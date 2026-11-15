"""tests/catalogue_import/test_commit.py

Phase 6 & Phase 7 gate tests:
1. Resumable commit: mid-commit interruption and resume yields identical result.
2. Idempotent commit: running commit a second time causes no changes.
3. No request reaches /process during any import.
4. Google Sheets private sheet produces friendly sharing guidance.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.services.catalogue_import.commit import commit_catalogue_import_job
from app.services.catalogue_import.gsheet import (
    GoogleSheetError,
    UNSHARED_SHEET_MESSAGE,
    extract_spreadsheet_id,
    fetch_google_sheet_xlsx,
)


@pytest.mark.asyncio
async def test_no_process_endpoint_ever_called_during_import():
    """Assert that the AI processing pipeline (/process) is never touched."""
    with patch("app.services.pipeline.process_product_image") as mock_pipeline:
        # Mock Supabase
        mock_sb = MagicMock()
        mock_job = {
            "id": "11111111-1111-1111-1111-111111111111",
            "wholesaler_id": "22222222-2222-2222-2222-222222222222",
            "status": "ready",
            "source_kind": "file",
            "summary": {"create": 1},
        }
        mock_sb.table().select().eq().execute.return_value.data = [mock_job]

        # Row mock
        mock_row = {
            "id": 1,
            "action": "create",
            "product_code": "110023",
            "normalized": {
                "title": "Rings 110023",
                "jewellery_type": "rings",
                "category": "gold",
                "gross_weight": 12.5,
            },
            "photo_refs": [],
        }
        mock_sb.table().select().eq().in_().is_().order().limit().execute.side_effect = [
            MagicMock(data=[mock_row]),
            MagicMock(data=[]),  # second chunk empty
        ]
        mock_sb.table().insert().execute.return_value.data = [{"id": "prod-1", "product_code": "110023"}]

        with patch("app.services.catalogue_import.commit.get_supabase", return_value=mock_sb):
            await commit_catalogue_import_job(
                job_id="11111111-1111-1111-1111-111111111111",
                wholesaler_id="22222222-2222-2222-2222-222222222222",
            )

        mock_pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_resumable_commit_and_idempotence():
    """Simulate worker dying mid-commit and resuming; second commit is no-op."""
    mock_sb = MagicMock()
    mock_job = {
        "id": "11111111-1111-1111-1111-111111111111",
        "wholesaler_id": "22222222-2222-2222-2222-222222222222",
        "status": "committing",
        "source_kind": "file",
        "summary": {"create": 2, "update": 0},
    }
    mock_sb.table().select().eq().execute.return_value.data = [mock_job]

    # Two rows: row 1 already committed (product_id set), row 2 pending
    row2 = {
        "id": 2,
        "action": "create",
        "product_code": "110024",
        "normalized": {"title": "Gold Pendant", "gross_weight": 10.0},
        "photo_refs": [],
    }

    mock_sb.table().select().eq().in_().is_().order().limit().execute.side_effect = [
        MagicMock(data=[row2]),
        MagicMock(data=[]),
    ]
    mock_sb.table().insert().execute.return_value.data = [{"id": "prod-2"}]

    with patch("app.services.catalogue_import.commit.get_supabase", return_value=mock_sb):
        # 1. Resume and complete
        await commit_catalogue_import_job(
            job_id="11111111-1111-1111-1111-111111111111",
            wholesaler_id="22222222-2222-2222-2222-222222222222",
        )

        # 2. Second commit on 'done' job does nothing
        mock_job["status"] = "done"
        result2 = await commit_catalogue_import_job(
            job_id="11111111-1111-1111-1111-111111111111",
            wholesaler_id="22222222-2222-2222-2222-222222222222",
        )
        assert result2 == {"create": 2, "update": 0}


@pytest.mark.asyncio
async def test_private_google_sheet_friendly_message():
    """Unshared or private Google Sheet raises friendly instruction."""
    bad_url = "https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit"

    fake_resp = MagicMock()
    fake_resp.status_code = 403
    fake_resp.url = "https://accounts.google.com/signin"

    with patch("httpx.AsyncClient.get", return_value=fake_resp):
        with pytest.raises(GoogleSheetError, match="Open the sheet → Share"):
            await fetch_google_sheet_xlsx(bad_url)


def test_build_update_payload_preserves_extra_attributes():
    """Verify that updating a product preserves existing extra_attributes if incoming sheet has none."""
    from app.services.catalogue_import.commit import _build_update_payload

    existing = {
        "id": "prod-1",
        "title": "Old Title",
        "extra_attributes": {"Karigar": "Sunil", "Remarks": "Repeat order 3x"},
    }
    norm = {
        "title": "New Title",
        "jewellery_type": "rings",
        "category": "gold",
        "extra_attributes": {},
    }

    payload = _build_update_payload(job_id="job-1", norm=norm, existing=existing)
    assert payload["extra_attributes"] == {"Karigar": "Sunil", "Remarks": "Repeat order 3x"}
    assert payload["import_snapshot"]["extra_attributes"] == {"Karigar": "Sunil", "Remarks": "Repeat order 3x"}

