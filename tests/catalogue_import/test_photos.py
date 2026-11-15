"""tests/catalogue_import/test_photos.py

Phase 5 gate tests:
1. A1 photo_summary matches expected/A1.json -> photo_summary exactly:
   - matched: 31
   - extra_photos: 1
   - unmatched_files: ["999999.jpg"]
   - rejected: {"460001.png": "over 10 MB", "350001.jpg": "not a valid image"}
   - ignored: ["__MACOSX/._110001.JPG", "Thumbs.db", "readme.txt"]
   - products_without_photo: ["160001", "170002", "180002", "190002", "350001", "460001"]
2. G fixture (images-only upload) matches expected/G.json:
   - attaches 3 photos to existing products without photos
   - attaches 1 extra photo to existing product with photo (110023)
   - creates 0 new products
3. EXIF and GPS tags are completely stripped from saved images.
"""

import io
import json
from pathlib import Path
from PIL import Image
import pytest

from app.services.catalogue_import.reader import read_source_file
from app.services.catalogue_import.mapper import analyze_import
from app.services.catalogue_import.normalize import NormalizedRow, normalize_row
from app.services.catalogue_import.merge import detect_intra_job_duplicates
from app.services.catalogue_import.photos import (
    MAX_CACHE_BYTES,
    PhotoSummary,
    cache_photo_url,
    clear_photo_cache,
    extract_photos_from_zip,
    get_cached_photo_url,
    get_current_cache_bytes,
    match_photos_images_only,
    match_photos_to_rows,
    match_photos_to_rows_async,
    process_and_strip_exif,
)

DATA_DIR = Path(__file__).resolve().parents[4] / "plans" / "bulk-catalogue-import" / "test-data"
FIXTURES_DIR = DATA_DIR / "fixtures"
EXP_DIR = DATA_DIR / "expected"


def test_a1_photo_summary_matches_expected():
    """A1 photo matching produces the exact photo_summary required by test plan."""
    exp_a1 = json.loads((EXP_DIR / "A1.json").read_text())
    expected_photo_summary = exp_a1["photo_summary"]

    # 1. Read sheet
    a1_sheet_path = FIXTURES_DIR / "A1_shree_balaji_stock_register.xlsx"
    read_res = read_source_file(a1_sheet_path.read_bytes(), a1_sheet_path.name)

    a1_answers = {
        "q_sheets": ["Stock Register"],
        "q_val_style_temple": "traditional",
        "q_val_purity_21k": "22k",
        "q_col_Karigar": "extra",
        "q_col_Remarks": "extra",
    }
    mapped = analyze_import(read_res, answers=a1_answers)

    rows: list[NormalizedRow] = []
    for raw_r in read_res.rows:
        norm = normalize_row(raw_r, mapped.column_map, mapped.value_maps)
        rows.append(norm)

    detect_intra_job_duplicates(rows)

    # 2. Extract photos from A1_photos.zip
    a1_zip_path = FIXTURES_DIR / "A1_photos.zip"
    summary = PhotoSummary()
    valid_photos = extract_photos_from_zip(a1_zip_path.read_bytes(), summary)

    # 3. Match photos to rows
    match_photos_to_rows(rows, valid_photos, summary, url_fetch_enabled=False)

    got = summary.to_dict()

    assert got["matched"] == expected_photo_summary["matched"], f"matched mismatch: {got['matched']} != {expected_photo_summary['matched']}"
    assert got["extra_photos"] == expected_photo_summary["extra_photos"], f"extra_photos mismatch: {got['extra_photos']} != {expected_photo_summary['extra_photos']}"
    assert sorted(got["unmatched_files"]) == sorted(expected_photo_summary["unmatched_files"])
    assert got["rejected"] == expected_photo_summary["rejected"]
    assert sorted(got["ignored"]) == sorted(expected_photo_summary["ignored"])
    assert sorted(got["products_without_photo"]) == sorted(expected_photo_summary["products_without_photo"])


def test_g_fixture_images_only_upload():
    """G fixture: images-only upload attaches 3 photos, 1 extra, and creates 0 products."""
    exp_g = json.loads((EXP_DIR / "G.json").read_text())
    exp_b = json.loads((EXP_DIR / "B.json").read_text())

    # Construct Account B's existing products in database
    existing_products: list[dict] = []
    for code, p in exp_b["products"].items():
        existing_products.append({
            "product_code": code,
            "raw_image_url": p.get("photo_source"),  # None for products without photo
            "images": list(p.get("extra_photo_sources") or []),
            **p,
        })

    # Read G zip
    g_zip_path = FIXTURES_DIR / "G_rajesh_photos_later.zip"
    summary = PhotoSummary()
    photos = extract_photos_from_zip(g_zip_path.read_bytes(), summary)

    result = match_photos_images_only(existing_products, photos, summary)

    assert summary.matched == exp_g["photo_summary"]["matched"]  # 3
    assert summary.extra_photos == exp_g["photo_summary"]["extra_photos"]  # 1

    # Product 110023 already had a photo, so 110023_extra was added as extra photo
    assert "110023" in result
    assert result["110023"]["main_photo"] is None
    assert len(result["110023"]["extra_photos"]) == 1

    # Products 230031, 240031, 150031 had no photo, received main photo
    for code in ("230031", "240031", "150031"):
        assert result[code]["main_photo"] is not None


def test_exif_and_gps_stripping():
    """Images with EXIF and GPS tags have them stripped cleanly."""
    # Create image with dummy EXIF metadata
    im = Image.new("RGB", (100, 100), color="gold")
    exif_data = im.getexif()
    # Tag 0x010E is ImageDescription, 0x0132 is DateTime
    exif_data[0x010E] = "Secret Wholesaler Shop Location"
    exif_data[0x0132] = "2026:11:15 12:00:00"

    raw_buf = io.BytesIO()
    im.save(raw_buf, format="JPEG", exif=exif_data)
    raw_bytes = raw_buf.getvalue()

    # Process and strip
    clean_bytes, mime = process_and_strip_exif(raw_bytes)
    assert mime == "image/jpeg"

    # Verify that EXIF is stripped
    clean_im = Image.open(io.BytesIO(clean_bytes))
    clean_exif = clean_im.getexif()
    assert 0x010E not in clean_exif
    assert 0x0132 not in clean_exif


def test_url_photo_cache_100mb_cap_item_19():
    """Item 19: Photo cache caps memory at 100 MB total bytes, dropping oldest entries."""
    clear_photo_cache()
    assert get_current_cache_bytes() == 0

    chunk_size = 5 * 1024 * 1024  # 5 MB
    dummy_payload = b"X" * chunk_size

    # Cache 25 items of 5 MB each = 125 MB total attempted
    for i in range(25):
        url = f"https://example.com/photo_{i:02d}.jpg"
        cache_photo_url(url, dummy_payload, "image/jpeg")

    # Assert cache byte total stays strictly <= 100 MB
    assert get_current_cache_bytes() <= MAX_CACHE_BYTES
    # 100 MB can hold at most 20 chunks of 5 MB
    assert get_current_cache_bytes() == 20 * chunk_size

    # The oldest entries (0-4) must have been evicted in LRU order
    for i in range(5):
        assert get_cached_photo_url(f"https://example.com/photo_{i:02d}.jpg") is None

    # The latest entries (5-24) must still be cached
    for i in range(5, 25):
        cached = get_cached_photo_url(f"https://example.com/photo_{i:02d}.jpg")
        assert cached is not None
        assert len(cached[0]) == chunk_size

    clear_photo_cache()
    assert get_current_cache_bytes() == 0


@pytest.mark.asyncio
async def test_match_photos_to_rows_async_non_blocking_event_loop_item_18(monkeypatch):
    """Item 18: Analyze with slow photo links downloads concurrently in threads without blocking event loop."""
    import time
    import asyncio

    # Create dummy 1x1 jpeg bytes
    im = Image.new("RGB", (10, 10), color="blue")
    buf = io.BytesIO()
    im.save(buf, format="JPEG")
    valid_jpeg_bytes = buf.getvalue()

    active_fetches = 0
    max_concurrent_fetches = 0

    def mock_safe_fetch(url: str):
        nonlocal active_fetches, max_concurrent_fetches
        active_fetches += 1
        if active_fetches > max_concurrent_fetches:
            max_concurrent_fetches = active_fetches
        # Simulate blocking socket read (0.05s per photo)
        time.sleep(0.05)
        active_fetches -= 1
        return valid_jpeg_bytes, "image/jpeg"

    monkeypatch.setattr("app.services.catalogue_import.photos.safe_fetch_image", mock_safe_fetch)

    rows = []
    for i in range(20):
        code = f"CODE{i:03d}"
        r = NormalizedRow(
            sheet="Sheet1",
            row_number=i + 2,
            raw={"Code": code, "Photo": f"https://slow-cdn.example.com/{code}.jpg"},
            product_code=code,
            sheet_photo_value=f"https://slow-cdn.example.com/{code}.jpg",
        )
        rows.append(r)

    summary = PhotoSummary()
    clear_photo_cache()

    max_loop_lag_ms = 0.0
    stop_probe = False

    async def latency_probe():
        nonlocal max_loop_lag_ms
        while not stop_probe:
            t0 = time.perf_counter()
            await asyncio.sleep(0.005)
            lag = (time.perf_counter() - t0 - 0.005) * 1000.0
            if lag > max_loop_lag_ms:
                max_loop_lag_ms = lag

    probe_task = asyncio.create_task(latency_probe())

    try:
        await match_photos_to_rows_async(rows, [], summary, url_fetch_enabled=True)
    finally:
        stop_probe = True
        await probe_task

    assert summary.matched == 20
    for r in rows:
        assert r.photo_file is not None
        assert r.photo_error is None

    assert max_concurrent_fetches <= 4
    assert max_loop_lag_ms < 100.0, f"Event loop lag too high: {max_loop_lag_ms:.2f} ms"
