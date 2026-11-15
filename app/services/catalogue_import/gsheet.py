"""app/services/catalogue_import/gsheet.py

Google Sheets fetcher for catalogue import.
Requirements from 01_ANTIGRAVITY_PLAN.md Section 9:
- Only docs.google.com/spreadsheets/d/{id}/... host is accepted.
- Downloads https://docs.google.com/spreadsheets/d/{id}/export?format=xlsx
- Non-public sheet (redirect to login / 401 / 403 / HTML response) raises:
  "Open the sheet → Share → Anyone with the link: Viewer, then try again."
- Never asks for Google credentials.
"""

from __future__ import annotations

import io
import re
from urllib.parse import urlsplit

import httpx

from app.services.catalogue_import.safe_fetch import PhotoFetchError

GSHEET_HOST = "docs.google.com"
GSHEET_PATH_PATTERN = re.compile(r"^/spreadsheets/d/([a-zA-Z0-9_-]+)")
UNSHARED_SHEET_MESSAGE = "Open the sheet → Share → Anyone with the link: Viewer, then try again."


class GoogleSheetError(Exception):
    """Raised when Google Sheet fetch fails or permissions are inadequate."""
    pass


def extract_spreadsheet_id(url: str) -> str:
    """Validate Google Sheets URL and extract the spreadsheet ID."""
    try:
        parsed = urlsplit(url.strip())
    except Exception:
        raise GoogleSheetError("Invalid URL format.")

    if parsed.scheme.lower() not in ("http", "https"):
        raise GoogleSheetError("Invalid URL scheme. Must be HTTPS.")

    if parsed.netloc.lower() != GSHEET_HOST:
        raise GoogleSheetError(f"Invalid host '{parsed.netloc}'. Only Google Sheets (docs.google.com) is supported.")

    m = GSHEET_PATH_PATTERN.match(parsed.path)
    if not m:
        raise GoogleSheetError("URL must follow the format https://docs.google.com/spreadsheets/d/{spreadsheet_id}/...")

    return m.group(1)


async def fetch_google_sheet_xlsx(url: str, timeout: float = 30.0) -> tuple[bytes, str]:
    """Download Google Sheet as XLSX.

    Returns:
        tuple[bytes, str]: (xlsx_bytes, filename)
    """
    spreadsheet_id = extract_spreadsheet_id(url)
    export_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=xlsx"

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            resp = await client.get(export_url)

            # Check if redirected to Google sign-in or returned 401/403/404
            if resp.status_code in (401, 403) or "accounts.google.com" in str(resp.url):
                raise GoogleSheetError(UNSHARED_SHEET_MESSAGE)

            if resp.status_code != 200:
                raise GoogleSheetError(f"{UNSHARED_SHEET_MESSAGE} (HTTP {resp.status_code})")

            content_type = resp.headers.get("Content-Type", "").lower()
            content = resp.content

            # If response is HTML, it's almost certainly a sign-in or error page
            if "text/html" in content_type or content.startswith(b"<!DOCTYPE html") or content.startswith(b"<html"):
                raise GoogleSheetError(UNSHARED_SHEET_MESSAGE)

            # Check for ZIP/XLSX magic bytes (PK\x03\x04)
            if not content.startswith(b"PK\x03\x04"):
                raise GoogleSheetError(UNSHARED_SHEET_MESSAGE)

            filename = f"google_sheet_{spreadsheet_id}.xlsx"
            return content, filename

    except GoogleSheetError:
        raise
    except httpx.RequestError as exc:
        raise GoogleSheetError(f"Network error downloading Google Sheet: {exc}")
    except Exception as exc:
        raise GoogleSheetError(f"Failed to download Google Sheet: {exc}")
