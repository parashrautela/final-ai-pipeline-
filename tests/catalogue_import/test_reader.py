"""tests/catalogue_import/test_reader.py

Phase 2 gate tests for the reader service:
Verifies parsing of all formats (.xlsx, .xls, .ods, .csv, .zip), sheet filtering,
header row detection, total row exclusion, text/code formatting, encodings,
delimiters, and size/row limits against the test fixtures.
"""

from pathlib import Path
import pytest

from app.services.catalogue_import.reader import (
    read_source_file,
    detect_format_by_content,
    check_zip_security,
    ReaderLimitExceeded,
)

FIXTURES_DIR = Path(__file__).resolve().parents[4] / "plans" / "bulk-catalogue-import" / "test-data" / "fixtures"


def test_a1_xlsx_reader():
    """Test A1 (.xlsx): title rows above header, hidden sheet skipped, summary skipped, totals skipped."""
    file_path = FIXTURES_DIR / "A1_shree_balaji_stock_register.xlsx"
    assert file_path.exists(), f"Fixture missing: {file_path}"

    data = file_path.read_bytes()
    fmt = detect_format_by_content(data, file_path.name)
    assert fmt == "xlsx"

    res = read_source_file(data, file_path.name)
    assert res.source_kind == "xlsx"

    # Verify sheet detection
    sheet_names = [s.name for s in res.sheets]
    assert "Stock Register" in sheet_names
    assert "Summary" in sheet_names
    assert "Old 2024" in sheet_names

    # Verify Old 2024 is hidden
    old_sheet = next(s for s in res.sheets if s.name == "Old 2024")
    assert old_sheet.is_hidden is True

    # Verify Stock Register header row is row 4 (1-based)
    sr_sheet = next(s for s in res.sheets if s.name == "Stock Register")
    assert sr_sheet.is_hidden is False
    assert sr_sheet.header_row_index == 4
    assert "Design No." in sr_sheet.headers
    assert "G.Wt (gm)" in sr_sheet.headers

    # Summary sheet has only 2 columns -> not a product sheet
    sum_sheet = next(s for s in res.sheets if s.name == "Summary")
    assert sum_sheet.is_product_sheet is False

    # Stock Register product rows: 40 products + 1 identical duplicate + 1 duplicate with diff wt = 42 rows
    # The TOTAL row must be excluded!
    assert len(res.rows) == 42
    assert all(r.sheet == "Stock Register" for r in res.rows)

    # Check cell reading: code as text without trailing .0, spaces stripped
    codes = [r.raw.get("Design No.", "") for r in res.rows]
    assert any(".0" not in c and len(c) >= 5 for c in codes)
    # Total row is NOT in codes
    assert "TOTAL" not in codes
    assert all("total" not in c.lower() for c in codes)


def test_b_csv_reader():
    """Test B (.csv): UTF-8 BOM, semicolon delimiter, decimal commas."""
    file_path = FIXTURES_DIR / "B_rajesh_codes_export.csv"
    assert file_path.exists()

    data = file_path.read_bytes()
    fmt = detect_format_by_content(data, file_path.name)
    assert fmt == "csv"

    res = read_source_file(data, file_path.name)
    assert res.source_kind == "csv"
    assert res.delimiter == ";"
    assert res.encoding == "utf-8-sig"
    assert len(res.sheets) == 1

    sheet = res.sheets[0]
    assert sheet.header_row_index == 1
    assert "Design Code" in sheet.headers
    assert "Kul Vajan (gm)" in sheet.headers

    # 25 rows
    assert len(res.rows) == 25

    # Decimal commas preserved in raw
    weights = [r.raw.get("Kul Vajan (gm)", "") for r in res.rows if r.raw.get("Kul Vajan (gm)")]
    assert any("," in w for w in weights)


def test_b_ods_reader():
    """Test B (.ods): OpenDocument spreadsheet format parity."""
    file_path = FIXTURES_DIR / "B_rajesh_codes_export.ods"
    assert file_path.exists()

    data = file_path.read_bytes()
    fmt = detect_format_by_content(data, file_path.name)
    assert fmt == "ods"

    res = read_source_file(data, file_path.name)
    assert res.source_kind == "ods"
    assert len(res.rows) == 25

    sheet = res.sheets[0]
    assert "Design Code" in sheet.headers
    assert "Kul Vajan (gm)" in sheet.headers


def test_c_xls_reader():
    """Test C (.xls): legacy Excel BIFF8, alphanumeric codes."""
    file_path = FIXTURES_DIR / "C_legacy_billing_export.xls"
    assert file_path.exists()

    data = file_path.read_bytes()
    fmt = detect_format_by_content(data, file_path.name)
    assert fmt == "xls"

    res = read_source_file(data, file_path.name)
    assert res.source_kind == "xls"
    assert len(res.rows) == 13

    codes = [r.raw.get("Item Code", "") for r in res.rows]
    assert "BJ-1001" in codes
    assert "bj-1013" in codes or "BJ-1013" in codes


def test_d_multiple_product_sheets():
    """Test D (.xlsx): two product tabs (Rings and Necklaces)."""
    file_path = FIXTURES_DIR / "D_google_sheet_source.xlsx"
    assert file_path.exists()

    data = file_path.read_bytes()
    res = read_source_file(data, file_path.name)
    assert res.source_kind == "xlsx"
    assert res.multiple_product_sheets is True

    # Both sheets are product sheets
    prod_sheets = [s.name for s in res.sheets if s.is_product_sheet and not s.is_hidden]
    assert "Rings" in prod_sheets
    assert "Necklaces" in prod_sheets

    # By default, reads both visible product sheets
    assert len(res.rows) == 12

    # Can select only one sheet
    rings_only = read_source_file(data, file_path.name, selected_sheets=["Rings"])
    assert len(rings_only.rows) == 6
    assert all(r.sheet == "Rings" for r in rings_only.rows)


def test_e_hostile_and_broken():
    """Test E (.csv): duplicate headers, blank rows, formula/script injection, leading zeros."""
    file_path = FIXTURES_DIR / "E_hostile_and_broken.csv"
    assert file_path.exists()

    data = file_path.read_bytes()
    res = read_source_file(data, file_path.name)

    # Check duplicate header renamed
    sheet = res.sheets[0]
    assert "Weight" in sheet.headers
    assert "Weight (2)" in sheet.headers
    assert "Weight" in res.duplicate_headers_by_sheet.get(sheet.name, [])

    # Row 000123 keeps leading zeros
    codes = [r.raw.get("Code", "") for r in res.rows]
    assert "000123" in codes

    # Injection titles stored as raw plain text
    titles = [r.raw.get("Title", "") for r in res.rows]
    assert any("HYPERLINK" in t for t in titles)
    assert any("<script>" in t for t in titles)

    # Blank row in middle skipped; exactly 10 data rows
    assert len(res.rows) == 10


def test_f_too_many_rows_refused():
    """Test F (.csv): 5,001 data rows -> refused with 5,000 row limit message."""
    file_path = FIXTURES_DIR / "F_too_many_rows.csv"
    assert file_path.exists()

    data = file_path.read_bytes()
    with pytest.raises(ReaderLimitExceeded) as exc_info:
        read_source_file(data, file_path.name)

    assert "5,000 rows" in str(exc_info.value)
    assert "split the file" in str(exc_info.value).lower()


def test_zip_security():
    """Test zip security: check limits and valid archives."""
    file_path = FIXTURES_DIR / "A1_photos.zip"
    assert file_path.exists()
    data = file_path.read_bytes()

    # Valid zip passes
    check_zip_security(data)

    # Excessive size test
    fake_oversize = b"PK\x03\x04" + b"0" * (201 * 1024 * 1024)
    with pytest.raises(ReaderLimitExceeded) as exc:
        check_zip_security(fake_oversize)
    assert "exceeds maximum" in str(exc.value)
