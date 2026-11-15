"""app/services/catalogue_import/reader.py

Reads spreadsheet and text files of any format (.xlsx, .xls, .ods, .csv/.tsv, .zip).
Handles magic byte detection, sheet selection, header row detection, total/blank row skipping,
encoding and delimiter sniffing, and hard limits (row limits, file size, zip bomb protection).
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

import charset_normalizer
import openpyxl
import xlrd
from odf import opendocument
from odf.table import Table, TableCell, TableRow
from odf.text import P

# Limits from plan section 5
MAX_DATA_ROWS = 5000
MAX_SHEET_BYTES = 25 * 1024 * 1024       # 25 MB
MAX_ZIP_BYTES = 200 * 1024 * 1024         # 200 MB
MAX_ZIP_UNCOMPRESSED = 1024 * 1024 * 1024 # 1 GB
MAX_ZIP_COMPRESSION_RATIO = 100           # zip bomb threshold for large entries
MAX_IMAGES = 2000

# Total row keywords (case-insensitive)
TOTAL_KEYWORDS = {
    "total", "grand total", "subtotal", "sub total", "kul", "kul vajan",
    "कुल", "कुल वजन", "योग", "महायोग"
}


class ReaderLimitExceeded(ValueError):
    """Raised when a file or job exceeds size or row limits."""
    pass


@dataclass
class RawRow:
    sheet: str
    row_number: int  # 1-based row index in sheet (including titles/headers)
    raw: dict[str, str]  # header -> string cell value


@dataclass
class SheetInfo:
    name: str
    is_hidden: bool
    is_product_sheet: bool
    header_row_index: int  # 1-based row index of detected header
    headers: list[str]
    row_count: int


@dataclass
class ReadResult:
    source_kind: str  # "xlsx", "xls", "ods", "csv"
    sheets: list[SheetInfo]
    rows: list[RawRow]
    duplicate_headers_by_sheet: dict[str, list[str]] = field(default_factory=dict)
    multiple_product_sheets: bool = False
    encoding: Optional[str] = None
    delimiter: Optional[str] = None


def detect_format_by_content(data: bytes, filename: str = "") -> str:
    """Detect file format using magic bytes first, fallback to extension."""
    if len(data) >= 4 and data[:4] == b"PK\x03\x04":
        # Zip archive: could be xlsx, ods, or plain zip
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
                if "[Content_Types].xml" in names or any(n.startswith("xl/") for n in names):
                    return "xlsx"
                if "mimetype" in names:
                    mimetype = zf.read("mimetype").decode("utf-8", errors="ignore").strip()
                    if "opendocument.spreadsheet" in mimetype:
                        return "ods"
                if "content.xml" in names:
                    return "ods"
                return "zip"
        except zipfile.BadZipFile:
            return "zip"

    if len(data) >= 8 and data[:8] == b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1":
        return "xls"

    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in ("csv", "tsv", "txt"):
        return "csv"

    # Default to delimited text
    return "csv"


def check_zip_security(data: bytes):
    """Inspect zip entries for path traversal and compression bomb limits."""
    if len(data) > MAX_ZIP_BYTES:
        raise ReaderLimitExceeded(f"ZIP file size {len(data)} exceeds maximum {MAX_ZIP_BYTES} bytes (200 MB)")

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        total_uncompressed = 0
        for info in zf.infolist():
            # Check path traversal
            if info.filename.startswith("/") or ".." in info.filename.split("/"):
                raise ReaderLimitExceeded(f"Security error: ZIP entry '{info.filename}' contains path traversal")

            total_uncompressed += info.file_size
            if total_uncompressed > MAX_ZIP_UNCOMPRESSED:
                raise ReaderLimitExceeded(
                    f"ZIP uncompressed size exceeds limit of 1 GB (potential zip bomb)"
                )
            if info.compress_size > 0 and info.file_size > 10 * 1024 * 1024:
                ratio = info.file_size / info.compress_size
                if ratio > MAX_ZIP_COMPRESSION_RATIO:
                    raise ReaderLimitExceeded(
                        f"ZIP entry '{info.filename}' has excessive compression ratio {ratio:.1f} (potential zip bomb)"
                    )


def format_cell_value(val: Any) -> str:
    """Format any spreadsheet cell into string representation preserving codes and numbers."""
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        # If float is an exact integer, format as int without '.0'
        if isinstance(val, float) and val.is_integer():
            return str(int(val))
        return str(val)
    s = str(val).strip()
    # If string is a pure number ending in .0 (e.g. '110005.0' from Excel text copy)
    if s.endswith(".0") and s[:-2].replace("-", "").isdigit():
        return s[:-2]
    return s


def is_blank_or_separator_row(cells: list[str]) -> bool:
    """Returns True if row is empty or contains only separators (e.g. ---, ===)."""
    non_empty = [c.strip() for c in cells if c and c.strip()]
    if not non_empty:
        return True
    # If all cells are dashes, equals, or dots
    if all(re.fullmatch(r"[-=_.*#]+", c) for c in non_empty):
        return True
    return False


def is_total_row(cells: list[str], header_names: list[str]) -> bool:
    """Detects total / grand total / subtotal rows."""
    non_empty = [c.strip() for c in cells if c and c.strip()]
    if not non_empty:
        return False

    # Check for keyword in any text cell
    for c in non_empty:
        low = c.lower()
        if low in TOTAL_KEYWORDS:
            return True
        for kw in TOTAL_KEYWORDS:
            if kw in low and len(c) < 30:
                return True

    # If first 1-3 cells are empty or text like 'Total', and rest are numbers
    # Also check if no product code exists but numeric sum exists
    return False


def deduplicate_headers(headers: list[str]) -> tuple[list[str], list[str]]:
    """Assigns suffixes to duplicate headers (e.g. Weight -> Weight (2)) and tracks duplicates."""
    counts: dict[str, int] = {}
    result: list[str] = []
    duplicates_found: list[str] = []

    for h in headers:
        clean = h.strip() if h else "Column"
        if not clean:
            clean = "Column"
        if clean in counts:
            counts[clean] += 1
            new_name = f"{clean} ({counts[clean]})"
            result.append(new_name)
            if clean not in duplicates_found:
                duplicates_found.append(clean)
        else:
            counts[clean] = 1
            result.append(clean)

    return result, duplicates_found


def detect_header_row(all_rows: list[list[str]]) -> tuple[int, list[str], list[str]]:
    """
    Header is the first row in top 15 where >= 3 cells are non-empty short text
    and the row below looks like data. Returns (1-based header row index, headers, duplicates).
    """
    max_check = min(15, len(all_rows))
    for i in range(max_check):
        row = all_rows[i]
        non_empty = [c.strip() for c in row if c and c.strip()]
        if len(non_empty) >= 3:
            # Check cell lengths (headers are short titles, < 60 chars)
            if all(len(c) < 60 for c in non_empty):
                # Check that next row exists and has content (looks like data or headers)
                if i + 1 < len(all_rows):
                    next_row = all_rows[i + 1]
                    next_non_empty = [c.strip() for c in next_row if c and c.strip()]
                    if len(next_non_empty) >= 2:
                        raw_headers = [c.strip() for c in row]
                        # Trim trailing empty header cells
                        while raw_headers and not raw_headers[-1]:
                            raw_headers.pop()
                        headers, dups = deduplicate_headers(raw_headers)
                        return i + 1, headers, dups

    # Fallback to row 1 if rows exist
    if all_rows:
        raw_headers = [c.strip() for c in all_rows[0]]
        while raw_headers and not raw_headers[-1]:
            raw_headers.pop()
        headers, dups = deduplicate_headers(raw_headers)
        return 1, headers, dups
    return 1, [], []


def is_product_sheet_candidate(sheet_name: str, headers: list[str], row_count: int) -> bool:
    """Heuristic to determine if a sheet contains product rows vs simple summary/notes."""
    if row_count < 1:
        return False
    # If summary sheet has very few columns (e.g. <= 2 columns like 'Item', 'Count')
    if len(headers) <= 2 and any(h.lower() in ("count", "total", "summary", "qty", "no") for h in headers):
        return False
    # Typical product sheets have >= 3 columns
    return len(headers) >= 3


def sniff_text_encoding_and_delimiter(data: bytes) -> tuple[str, str]:
    """Sniffs encoding (handling BOM) and delimiter for delimited text."""
    # Check BOMs first
    if data.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    elif data.startswith(b"\xff\xfe"):
        encoding = "utf-16-le"
    elif data.startswith(b"\xfe\xff"):
        encoding = "utf-16-be"
    else:
        best = charset_normalizer.from_bytes(data[:50000]).best()
        encoding = best.encoding if best and best.encoding else "utf-8"

    sample = data[:10000].decode(encoding, errors="replace")

    # Sniff delimiter
    candidates = [";", ",", "\t", "|"]
    first_lines = [line for line in sample.splitlines()[:10] if line.strip()]

    # Try standard csv Sniffer
    try:
        dialect = csv.Sniffer().sniff(sample[:2048], delimiters=";,|\t")
        if dialect.delimiter in candidates:
            return encoding, dialect.delimiter
    except Exception:
        pass

    # Count occurrences per candidate across first lines
    best_delim = ","
    best_score = -1
    for d in candidates:
        counts = [line.count(d) for line in first_lines]
        if counts and counts[0] > 0 and len(set(counts)) <= 2:
            score = counts[0] * 10 - len(set(counts))
            if score > best_score:
                best_score = score
                best_delim = d

    return encoding, best_delim


def read_xlsx(data: bytes, selected_sheets: Optional[list[str]] = None) -> tuple[list[SheetInfo], list[RawRow], dict[str, list[str]]]:
    """Reads .xlsx using openpyxl in read-only and data-only mode (no formulas evaluated)."""
    wb = openpyxl.load_workbook(filename=io.BytesIO(data), read_only=True, data_only=True)
    all_sheet_names = wb.sheetnames

    sheet_infos: list[SheetInfo] = []
    all_raw_rows: list[RawRow] = []
    duplicate_headers_map: dict[str, list[str]] = {}

    for name in all_sheet_names:
        ws = wb[name]
        is_hidden = ws.sheet_state != "visible"

        # Read all rows into memory as formatted text
        rows_data: list[list[str]] = []
        for row in ws.iter_rows(values_only=True):
            cells = [format_cell_value(c) for c in row]
            rows_data.append(cells)

        header_idx, headers, dups = detect_header_row(rows_data)
        if dups:
            duplicate_headers_map[name] = dups

        # Count candidate product rows
        candidate_count = 0
        parsed_rows: list[RawRow] = []
        for r_idx, row in enumerate(rows_data[header_idx:], start=header_idx + 1):
            if is_blank_or_separator_row(row):
                continue
            if is_total_row(row, headers):
                continue
            # Build raw dict
            row_dict = {}
            for col_idx, h in enumerate(headers):
                val = row[col_idx] if col_idx < len(row) else ""
                row_dict[h] = val
            parsed_rows.append(RawRow(sheet=name, row_number=r_idx, raw=row_dict))
            candidate_count += 1

        is_prod = is_product_sheet_candidate(name, headers, candidate_count)
        sheet_info = SheetInfo(
            name=name,
            is_hidden=is_hidden,
            is_product_sheet=is_prod,
            header_row_index=header_idx,
            headers=headers,
            row_count=candidate_count,
        )
        sheet_infos.append(sheet_info)

        # Decide whether to include rows:
        # If selected_sheets is given: must be in selected_sheets
        # Else: must be visible and is_product_sheet
        should_include = False
        if selected_sheets is not None:
            should_include = name in selected_sheets
        else:
            should_include = not is_hidden and is_prod

        if should_include:
            all_raw_rows.extend(parsed_rows)

    wb.close()
    return sheet_infos, all_raw_rows, duplicate_headers_map


def read_xls(data: bytes, selected_sheets: Optional[list[str]] = None) -> tuple[list[SheetInfo], list[RawRow], dict[str, list[str]]]:
    """Reads legacy .xls using xlrd."""
    wb = xlrd.open_workbook(file_contents=data)
    sheet_infos: list[SheetInfo] = []
    all_raw_rows: list[RawRow] = []
    duplicate_headers_map: dict[str, list[str]] = {}

    for name in wb.sheet_names():
        ws = wb.sheet_by_name(name)
        is_hidden = getattr(ws, "visibility", 0) != 0

        rows_data: list[list[str]] = []
        for r in range(ws.nrows):
            row_cells = []
            for c in range(ws.ncols):
                cell_type = ws.cell_type(r, c)
                cell_val = ws.cell_value(r, c)
                if cell_type == xlrd.XL_CELL_NUMBER:
                    if float(cell_val).is_integer():
                        row_cells.append(str(int(cell_val)))
                    else:
                        row_cells.append(str(cell_val))
                elif cell_type == xlrd.XL_CELL_BOOLEAN:
                    row_cells.append("true" if cell_val else "false")
                else:
                    row_cells.append(format_cell_value(cell_val))
            rows_data.append(row_cells)

        header_idx, headers, dups = detect_header_row(rows_data)
        if dups:
            duplicate_headers_map[name] = dups

        candidate_count = 0
        parsed_rows: list[RawRow] = []
        for r_idx, row in enumerate(rows_data[header_idx:], start=header_idx + 1):
            if is_blank_or_separator_row(row):
                continue
            if is_total_row(row, headers):
                continue
            row_dict = {}
            for col_idx, h in enumerate(headers):
                val = row[col_idx] if col_idx < len(row) else ""
                row_dict[h] = val
            parsed_rows.append(RawRow(sheet=name, row_number=r_idx, raw=row_dict))
            candidate_count += 1

        is_prod = is_product_sheet_candidate(name, headers, candidate_count)
        sheet_info = SheetInfo(
            name=name,
            is_hidden=is_hidden,
            is_product_sheet=is_prod,
            header_row_index=header_idx,
            headers=headers,
            row_count=candidate_count,
        )
        sheet_infos.append(sheet_info)

        should_include = False
        if selected_sheets is not None:
            should_include = name in selected_sheets
        else:
            should_include = not is_hidden and is_prod

        if should_include:
            all_raw_rows.extend(parsed_rows)

    return sheet_infos, all_raw_rows, duplicate_headers_map


def read_ods(data: bytes, selected_sheets: Optional[list[str]] = None) -> tuple[list[SheetInfo], list[RawRow], dict[str, list[str]]]:
    """Reads .ods using odfpy."""
    doc = opendocument.load(io.BytesIO(data))
    sheet_infos: list[SheetInfo] = []
    all_raw_rows: list[RawRow] = []
    duplicate_headers_map: dict[str, list[str]] = {}

    for table in doc.getElementsByType(Table):
        name = table.getAttribute("name") or "Sheet"
        # Check hidden table attribute
        is_hidden = False

        rows_data: list[list[str]] = []
        for row in table.getElementsByType(TableRow):
            row_cells: list[str] = []
            for cell in row.getElementsByType(TableCell):
                repeat = cell.getAttribute("numbercolumnsrepeated")
                count = int(repeat) if repeat else 1
                if count > 100:  # Ignore unbounded repeated empty cells
                    count = 1
                # Extract text
                paras = cell.getElementsByType(P)
                cell_text = "\n".join(str(p) for p in paras).strip()
                for _ in range(count):
                    row_cells.append(format_cell_value(cell_text))
            rows_data.append(row_cells)

        header_idx, headers, dups = detect_header_row(rows_data)
        if dups:
            duplicate_headers_map[name] = dups

        candidate_count = 0
        parsed_rows: list[RawRow] = []
        for r_idx, row in enumerate(rows_data[header_idx:], start=header_idx + 1):
            if is_blank_or_separator_row(row):
                continue
            if is_total_row(row, headers):
                continue
            row_dict = {}
            for col_idx, h in enumerate(headers):
                val = row[col_idx] if col_idx < len(row) else ""
                row_dict[h] = val
            parsed_rows.append(RawRow(sheet=name, row_number=r_idx, raw=row_dict))
            candidate_count += 1

        is_prod = is_product_sheet_candidate(name, headers, candidate_count)
        sheet_info = SheetInfo(
            name=name,
            is_hidden=is_hidden,
            is_product_sheet=is_prod,
            header_row_index=header_idx,
            headers=headers,
            row_count=candidate_count,
        )
        sheet_infos.append(sheet_info)

        should_include = False
        if selected_sheets is not None:
            should_include = name in selected_sheets
        else:
            should_include = not is_hidden and is_prod

        if should_include:
            all_raw_rows.extend(parsed_rows)

    return sheet_infos, all_raw_rows, duplicate_headers_map


def read_csv(data: bytes, filename: str = "data.csv") -> tuple[list[SheetInfo], list[RawRow], dict[str, list[str]], str, str]:
    """Reads CSV / TSV / text files with encoding and delimiter sniffing."""
    encoding, delimiter = sniff_text_encoding_and_delimiter(data)
    text = data.decode(encoding, errors="replace")
    sheet_name = filename.rsplit(".", 1)[0] if "." in filename else "CSV"

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows_data: list[list[str]] = []
    for r in reader:
        rows_data.append([format_cell_value(c) for c in r])

    header_idx, headers, dups = detect_header_row(rows_data)
    duplicate_headers_map: dict[str, list[str]] = {}
    if dups:
        duplicate_headers_map[sheet_name] = dups

    parsed_rows: list[RawRow] = []
    candidate_count = 0
    for r_idx, row in enumerate(rows_data[header_idx:], start=header_idx + 1):
        if is_blank_or_separator_row(row):
            continue
        if is_total_row(row, headers):
            continue
        row_dict = {}
        for col_idx, h in enumerate(headers):
            val = row[col_idx] if col_idx < len(row) else ""
            row_dict[h] = val
        parsed_rows.append(RawRow(sheet=sheet_name, row_number=r_idx, raw=row_dict))
        candidate_count += 1

    sheet_info = SheetInfo(
        name=sheet_name,
        is_hidden=False,
        is_product_sheet=True,
        header_row_index=header_idx,
        headers=headers,
        row_count=candidate_count,
    )

    return [sheet_info], parsed_rows, duplicate_headers_map, encoding, delimiter


def read_source_file(data: bytes, filename: str = "", selected_sheets: Optional[list[str]] = None) -> ReadResult:
    """
    Main entry point for reading any uploaded spreadsheet file.
    Validates limits (file size and 5,000 data row cap).
    """
    if len(data) > MAX_SHEET_BYTES:
        raise ReaderLimitExceeded(
            f"File size ({len(data)} bytes) exceeds the 25 MB limit for spreadsheets"
        )

    fmt = detect_format_by_content(data, filename)

    if fmt == "xlsx":
        sheets, rows, dups = read_xlsx(data, selected_sheets)
        enc, delim = None, None
    elif fmt == "xls":
        sheets, rows, dups = read_xls(data, selected_sheets)
        enc, delim = None, None
    elif fmt == "ods":
        sheets, rows, dups = read_ods(data, selected_sheets)
        enc, delim = None, None
    elif fmt == "csv":
        sheets, rows, dups, enc, delim = read_csv(data, filename or "data.csv")
    else:
        raise ValueError(f"Unsupported spreadsheet format: {fmt}")

    # Enforce 5,000 row hard limit
    total_rows = len(rows)
    if total_rows > MAX_DATA_ROWS:
        raise ReaderLimitExceeded(
            f"File exceeds maximum limit of 5,000 rows ({total_rows} rows found). Please split the file into smaller batches."
        )

    # Check for multiple visible product sheets
    prod_sheets = [s.name for s in sheets if not s.is_hidden and s.is_product_sheet]
    multiple_product_sheets = len(prod_sheets) > 1

    return ReadResult(
        source_kind=fmt,
        sheets=sheets,
        rows=rows,
        duplicate_headers_by_sheet=dups,
        multiple_product_sheets=multiple_product_sheets,
        encoding=enc,
        delimiter=delim,
    )
