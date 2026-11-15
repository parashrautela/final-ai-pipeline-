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


@pytest.mark.asyncio
async def test_api_cross_tenant_file_path_rejected():
    """Item 1: Wholesaler A cannot create an import job pointing at Wholesaler B's file."""
    from fastapi import HTTPException
    from app.routers.catalogue_import import CreateJobRequest, create_import_job

    user_a = "e21f5582-1cac-4e33-8fc6-1c582edd655b"
    user_b = "fb20d512-d17b-4a3d-a793-9cce8a1eed5b"

    req = CreateJobRequest(
        source_kind="file",
        source_files=[{"name": "secret.xlsx", "path": f"{user_b}/secret.xlsx"}],
    )
    with pytest.raises(HTTPException) as exc_info:
        await create_import_job(req=req, user_id=user_a)
    assert exc_info.value.status_code == 403
    assert "does not belong to wholesaler" in exc_info.value.detail


@pytest.mark.asyncio
async def test_api_cross_tenant_profile_rejected():
    """Item 2: Wholesaler A cannot use Wholesaler B's mapping profile."""
    from fastapi import HTTPException
    from app.routers.catalogue_import import CreateJobRequest, create_import_job

    user_a = "e21f5582-1cac-4e33-8fc6-1c582edd655b"
    fake_b_profile_id = "33333333-3333-3333-3333-333333333333"

    mock_sb = MagicMock()
    mock_sb.table().select().eq().eq().execute.return_value = MagicMock(data=[])

    req = CreateJobRequest(
        source_kind="file",
        profile_id=fake_b_profile_id,
        source_files=[{"name": "stock.xlsx", "path": f"{user_a}/stock.xlsx"}],
    )
    with patch("app.routers.catalogue_import.get_supabase", return_value=mock_sb):
        with pytest.raises(HTTPException) as exc_info:
            await create_import_job(req=req, user_id=user_a)
        assert exc_info.value.status_code == 403
        assert "Profile not found or access denied" in exc_info.value.detail


def test_find_best_matching_profile_helper():
    """Item 10: Verify find_best_matching_profile selects the highest match >= 70%."""
    from app.services.catalogue_import.mapper import find_best_matching_profile

    headers = ["Design No.", "Item", "Metal", "Kt", "G.Wt (gm)", "N.Wt", "Style", "Size"]
    matching_profile = {
        "id": "prof-1",
        "header_fingerprint": ["Design No.", "Item", "Metal", "Kt", "G.Wt (gm)", "N.Wt", "Style", "Size"],
    }
    unrelated_profile = {
        "id": "prof-2",
        "header_fingerprint": ["Code", "Description", "Weight"],
    }

    # Match found
    best = find_best_matching_profile(headers, [unrelated_profile, matching_profile])
    assert best is not None
    assert best["id"] == "prof-1"

    # Overlap below 70% returns None
    few_headers = ["Design No.", "RandomCol1", "RandomCol2", "RandomCol3"]
    assert find_best_matching_profile(few_headers, [matching_profile]) is None


@pytest.mark.asyncio
async def test_auto_profile_reuse_and_not_in_file_calculation():
    """
    Item 10: When a job is analyzed with no profile_id:
    - Auto-detects matching profile from wholesaler's saved profiles
    - Saves profile_id to the job
    - Reuses learned column/value maps so questions <= 1
    - Correctly calculates not_in_file and conflicts for the profile series
    - Commit writes profile_id back to job
    """
    from pathlib import Path
    from app.routers.catalogue_import import _run_analyze_pipeline

    user_id = "e21f5582-1cac-4e33-8fc6-1c582edd655b"
    job_id = "job-a2-auto-match"
    prof_id = "prof-balaji-saved"

    fixtures_dir = Path(__file__).resolve().parents[4] / "plans" / "bulk-catalogue-import" / "test-data" / "fixtures"
    a2_path = fixtures_dir / "A2_shree_balaji_stock_register_v2.xlsx"
    if not a2_path.exists():
        pytest.skip(f"Fixture A2 not found at {a2_path}")
    a2_bytes = a2_path.read_bytes()

    saved_profile = {
        "id": prof_id,
        "wholesaler_id": user_id,
        "name": "Shree Balaji Template",
        "header_fingerprint": [
            "Sr No", "Design No.", "Item Name", "Item", "Metal", "Kt", "G.Wt (gm)",
            "St.Wt", "N.Wt", "Size", "Style", "Ready/Order", "Days", "Photo", "Karigar", "Remarks"
        ],
        "column_map": {
            "Sr No": "ignore",
            "Design No.": "product_code",
            "Item Name": "title",
            "Item": "jewellery_type",
            "Metal": "category",
            "Kt": "metal_purity",
            "G.Wt (gm)": "gross_weight",
            "St.Wt": "stone_weight",
            "N.Wt": "net_weight",
            "Size": "size",
            "Style": "style",
            "Ready/Order": "stock_available",
            "Days": "make_to_order_days",
            "Photo": "photo",
            "Karigar": "extra:Karigar",
            "Remarks": "extra:Remarks",
        },
        "value_maps": {
            "metal_purity": {"21k": "22k"},
            "style": {"temple": "traditional"},
        },
        "sheet_rules": {"sheets": ["Stock Register"]},
    }

    job_data = {
        "id": job_id,
        "wholesaler_id": user_id,
        "status": "read",
        "source_kind": "file",
        "profile_id": None,  # NOT sent by caller
        "source_files": [{"name": "A2_shree_balaji_updated.xlsx", "path": f"{user_id}/A2_shree_balaji_updated.xlsx"}],
        "answers": {},
    }

    mock_sb = MagicMock()
    # Profile query returns saved_profile
    mock_sb.table().select().eq().execute.return_value = MagicMock(data=[saved_profile])

    # Related jobs query returns A1 job
    a1_job_id = "job-a1-prior"
    mock_sb.table().select().eq().eq().execute.return_value = MagicMock(data=[{"id": a1_job_id}])

    # Existing products return A1 products (including 3 missing from A2: 170002, 180002, 190002)
    existing_mock_products = [
        {"id": "p-1", "product_code": "170002", "import_job_id": a1_job_id, "import_snapshot": {"gross_weight": 10.0}, "gross_weight": 10.0, "is_published": False, "ai_processing_state": "none", "extra_attributes": {}},
        {"id": "p-2", "product_code": "180002", "import_job_id": a1_job_id, "import_snapshot": {"gross_weight": 11.0}, "gross_weight": 11.0, "is_published": False, "ai_processing_state": "none", "extra_attributes": {}},
        {"id": "p-3", "product_code": "190002", "import_job_id": a1_job_id, "import_snapshot": {"gross_weight": 12.0}, "gross_weight": 12.0, "is_published": False, "ai_processing_state": "none", "extra_attributes": {}},
        # Conflicting product (app edited gross_weight from 15.0 to 20.0)
        {"id": "p-4", "product_code": "110001", "import_job_id": a1_job_id, "import_snapshot": {"gross_weight": 15.0}, "gross_weight": 20.0, "is_published": False, "ai_processing_state": "none", "extra_attributes": {}},
        {"id": "p-5", "product_code": "110002", "import_job_id": a1_job_id, "import_snapshot": {"gross_weight": 16.0}, "gross_weight": 22.0, "is_published": False, "ai_processing_state": "none", "extra_attributes": {}},
    ]
    # Existing products query
    mock_sb.table().select().eq().execute.side_effect = [
        MagicMock(data=[saved_profile]),  # catalogue_import_profiles lookup
        MagicMock(data=existing_mock_products),  # products query
    ]

    with patch("app.routers.catalogue_import.get_supabase", return_value=mock_sb), \
         patch("app.routers.catalogue_import.download_from_storage", return_value=a2_bytes):
        result = await _run_analyze_pipeline(job=job_data, user_id=user_id)

    # 1. Profile was auto-detected and saved
    assert job_data["profile_id"] == prof_id
    # 2. Reused mapping: 0 questions (<= 1) and status is ready
    assert result["status"] == "ready"
    summary = result["summary"]
    # 3. Not in file correctly identifies the 3 missing products from the series
    assert summary["not_in_file"] == 3
    # 4. Conflicts identified
    assert summary["conflict"] >= 2


@pytest.mark.asyncio
async def test_commit_conflict_resolutions_mine_and_sheet():
    """Assert conflict resolution: 'mine' preserves current product attributes while updating snapshot,

    while 'sheet' updates product attributes with incoming data."""
    job_id = "job-conflict-res-test"
    wid = "wholesaler-res-test"

    job_mock = {
        "id": job_id,
        "wholesaler_id": wid,
        "status": "ready",
        "source_kind": "file",
        "source_files": [],
        "summary": {"conflict": 2},
    }

    # Two rows: code-1 resolved 'mine', code-2 resolved 'sheet'
    row_mine = {
        "id": 101,
        "action": "conflict",
        "resolution": "mine",
        "product_code": "CODE1",
        "normalized": {"title": "Sheet Title 1", "gross_weight": 5.0},
        "photo_refs": [],
    }
    row_sheet = {
        "id": 102,
        "action": "conflict",
        "resolution": "sheet",
        "product_code": "CODE2",
        "normalized": {"title": "Sheet Title 2", "gross_weight": 10.0},
        "photo_refs": [],
    }

    existing_p1 = {
        "id": "prod-1",
        "product_code": "CODE1",
        "title": "App Title 1",
        "gross_weight": 9.99,
        "import_snapshot": {"gross_weight": 4.0},
        "ai_processing_state": "none",
        "extra_attributes": {},
    }
    existing_p2 = {
        "id": "prod-2",
        "product_code": "CODE2",
        "title": "App Title 2",
        "gross_weight": 8.0,
        "import_snapshot": {"gross_weight": 7.0},
        "ai_processing_state": "none",
        "extra_attributes": {},
    }

    mock_sb = MagicMock()
    products_table = MagicMock()
    rows_table = MagicMock()
    jobs_table = MagicMock()

    def get_table(name):
        if name == "products":
            return products_table
        elif name == "catalogue_import_rows":
            return rows_table
        return jobs_table

    mock_sb.table.side_effect = get_table

    # Jobs table: fetch job & final job
    jobs_table.select().eq().execute.return_value = MagicMock(data=[job_mock])
    jobs_table.update().eq().execute.return_value = MagicMock(data=[])

    # Products table: existing products
    products_table.select().eq().execute.return_value = MagicMock(data=[existing_p1, existing_p2])

    # Rows table: first chunk returns 2 rows, next returns empty
    rows_table.select().eq().in_().is_().order().limit().execute.side_effect = [
        MagicMock(data=[row_mine, row_sheet]),
        MagicMock(data=[]),
    ]
    rows_table.update().eq().execute.return_value = MagicMock(data=[])

    updated_products = {}
    def mock_prod_update(payload):
        mock_chain = MagicMock()
        def mock_eq(col, val):
            updated_products[val] = payload
            return MagicMock(execute=MagicMock(return_value=MagicMock(data=[{"id": val}])))
        mock_chain.eq = mock_eq
        return mock_chain

    products_table.update = mock_prod_update

    with patch("app.services.catalogue_import.commit.get_supabase", return_value=mock_sb), \
         patch("app.services.catalogue_import.commit._update_or_create_profile", new_callable=AsyncMock):
        res = await commit_catalogue_import_job(job_id=job_id, wholesaler_id=wid, stored_photos={})

    # Verify: prod-1 kept gross_weight=9.99, only snapshot was updated
    assert "prod-1" in updated_products
    assert "gross_weight" not in updated_products["prod-1"]  # Not overwritten
    assert updated_products["prod-1"]["import_snapshot"]["gross_weight"] == 5.0

    # Verify: prod-2 accepted sheet gross_weight=10.0
    assert "prod-2" in updated_products
    assert updated_products["prod-2"]["gross_weight"] == 10.0


@pytest.mark.asyncio
async def test_commit_resolves_url_photos():
    """Verify photo URLs in photo_refs are fetched, EXIF-stripped, and uploaded to storage."""
    from app.services.catalogue_import.commit import _resolve_photo_urls

    dummy_img = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"
    with patch("app.services.catalogue_import.commit.safe_fetch_image", return_value=(dummy_img, "image/jpeg")), \
         patch("app.services.catalogue_import.commit.process_and_strip_exif", return_value=(dummy_img, "image/jpeg")), \
         patch("app.services.catalogue_import.commit.upload_file_to_storage", return_value="https://storage.example.com/raw/user/photo1.jpg") as mock_upload:
        main_url, extra = _resolve_photo_urls(
            wholesaler_id="user-123",
            job_id="job-456",
            photo_refs=["https://test.com/photo1.jpg"],
            stored_photos=None,
        )
        assert main_url == "https://storage.example.com/raw/user/photo1.jpg"
        assert extra == []
        mock_upload.assert_called_once()





