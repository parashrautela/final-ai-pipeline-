"""app/services/catalogue_import/photos.py

Photo matching, validation, EXIF stripping, and attachment for catalogue imports.
Implements specifications from 01_ANTIGRAVITY_PLAN.md Section 8:
- Matching, first hit wins:
  1. Sheet's photo column holds a filename (base name match, case/ext/folders ignored).
  2. Sheet's photo column holds a URL (fetched via safe_fetch_image).
  3. Base name equals product code.
  4. Code + suffix -> extra photo in products.images.
- Ignore __MACOSX/, dotfiles, Thumbs.db, desktop.ini, and non-image extensions.
- Validate: magic bytes (JPEG/PNG/WebP), <= 10 MB, decodable by PIL.
- Rejected: oversize ("over 10 MB") or corrupt ("not a valid image").
- EXIF/GPS stripping by re-saving via PIL at quality 95.
- Images-only upload (source_kind='images_only'): attaches to existing products, creates 0.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
import io
import os
from pathlib import Path
import re
from typing import Any, BinaryIO, Optional
import zipfile

from PIL import Image

from app.services.catalogue_import.safe_fetch import (
    MAX_IMAGE_BYTES,
    PhotoFetchError,
    SSRFError,
    safe_fetch_image,
    validate_magic_bytes,
)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".jfif"}
IGNORED_FILENAMES = {"thumbs.db", "desktop.ini", ".ds_store"}

SUFFIX_PATTERN = re.compile(
    r"^[_ -]+(\d+|back|side|front|top|bottom|extra|detail|angle|\(\d+\))$",
    re.IGNORECASE,
)

# In-memory LRU cache capped at 100 MB total bytes (Item 19)
MAX_CACHE_BYTES = 100 * 1024 * 1024  # 100 MB
_url_photo_cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
_current_cache_bytes: int = 0
PHOTO_SEMAPHORE = asyncio.Semaphore(4)


def get_cached_photo_url(url: str) -> Optional[tuple[bytes, str]]:
    """Retrieve pre-downloaded (bytes, mime) for a URL so commit doesn't download twice."""
    u = url.strip()
    if u in _url_photo_cache:
        _url_photo_cache.move_to_end(u)
        return _url_photo_cache[u]
    u_lower = u.lower()
    if u_lower in _url_photo_cache:
        _url_photo_cache.move_to_end(u_lower)
        return _url_photo_cache[u_lower]
    return None


def cache_photo_url(url: str, data: bytes, mime: str) -> None:
    """Store pre-downloaded photo in cache, evicting oldest items when total bytes exceeds 100 MB (Item 19)."""
    global _current_cache_bytes
    u = url.strip()
    new_bytes = len(data)

    if u in _url_photo_cache:
        old_data, _ = _url_photo_cache.pop(u)
        _current_cache_bytes -= len(old_data)

    while _url_photo_cache and (_current_cache_bytes + new_bytes > MAX_CACHE_BYTES):
        old_key, (old_val, _) = _url_photo_cache.popitem(last=False)
        _current_cache_bytes -= len(old_val)

    if new_bytes <= MAX_CACHE_BYTES:
        _url_photo_cache[u] = (data, mime)
        _current_cache_bytes += new_bytes


def get_current_cache_bytes() -> int:
    return _current_cache_bytes


def clear_photo_cache() -> None:
    global _current_cache_bytes
    _url_photo_cache.clear()
    _current_cache_bytes = 0



@dataclass
class ProcessedPhoto:
    filename: str  # Original path/name in zip or upload
    basename: str  # Clean base name without ext
    clean_bytes: bytes
    mime_type: str


@dataclass
class PhotoSummary:
    matched: int = 0
    extra_photos: int = 0
    unmatched_files: list[str] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    ignored: list[str] = field(default_factory=list)
    products_without_photo: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "extra_photos": self.extra_photos,
            "unmatched_files": sorted(self.unmatched_files),
            "rejected": self.rejected,
            "ignored": sorted(self.ignored),
            "products_without_photo": sorted(self.products_without_photo),
        }


def clean_base_name(filename: str) -> str:
    """Extract clean lowercase base name without directory and extension."""
    base = os.path.basename(filename).strip()
    stem, _ = os.path.splitext(base)
    return stem.strip().lower()


def is_ignored_file(filepath: str) -> bool:
    """Check if file should be placed in ignored list."""
    norm = filepath.replace("\\", "/")
    if "__MACOSX/" in norm or norm.startswith("__MACOSX/"):
        return True

    base = os.path.basename(norm).strip()
    if base.startswith("."):
        return True

    if base.lower() in IGNORED_FILENAMES:
        return True

    _, ext = os.path.splitext(base)
    if ext.lower() not in IMAGE_EXTENSIONS:
        return True

    return False


def process_and_strip_exif(raw_bytes: bytes) -> tuple[bytes, str]:
    """Validate image bytes, strip all EXIF metadata, and re-save."""
    validate_magic_bytes(raw_bytes)

    try:
        # Open and verify image integrity
        img = Image.open(io.BytesIO(raw_bytes))
        img.verify()

        # Reopen to read pixel data after verify()
        img = Image.open(io.BytesIO(raw_bytes))
        fmt = (img.format or "").upper()
        if fmt not in ("JPEG", "PNG", "WEBP"):
            raise ValueError(f"Unsupported image format: {fmt}")

        out = io.BytesIO()
        if fmt == "JPEG":
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            # Saving without exif parameter strips EXIF/GPS completely
            img.save(out, format="JPEG", quality=95)
            mime = "image/jpeg"
        elif fmt == "PNG":
            img.save(out, format="PNG")
            mime = "image/png"
        elif fmt == "WEBP":
            img.save(out, format="WEBP", quality=95)
            mime = "image/webp"
        else:
            raise ValueError(f"Unsupported format: {fmt}")

        return out.getvalue(), mime

    except Exception as exc:
        raise PhotoFetchError(f"Corrupt or unreadable image: {exc}")


def extract_photos_from_zip(
    zip_bytes: bytes, summary: PhotoSummary
) -> list[ProcessedPhoto]:
    """Read a ZIP archive and return validated, EXIF-stripped photos."""
    valid_photos: list[ProcessedPhoto] = []

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue

                filename = info.filename
                if is_ignored_file(filename):
                    summary.ignored.append(filename)
                    continue

                # Check file size limit
                if info.file_size > MAX_IMAGE_BYTES:
                    # Record using entry base name
                    summary.rejected[os.path.basename(filename)] = "over 10 MB"
                    continue

                data = z.read(info)
                if len(data) > MAX_IMAGE_BYTES:
                    summary.rejected[os.path.basename(filename)] = "over 10 MB"
                    continue

                try:
                    clean_bytes, mime = process_and_strip_exif(data)
                    valid_photos.append(
                        ProcessedPhoto(
                            filename=filename,
                            basename=clean_base_name(filename),
                            clean_bytes=clean_bytes,
                            mime_type=mime,
                        )
                    )
                except Exception:
                    summary.rejected[os.path.basename(filename)] = "not a valid image"

    except zipfile.BadZipFile:
        pass

    return valid_photos


def process_loose_files(
    files: list[tuple[str, bytes]], summary: PhotoSummary
) -> list[ProcessedPhoto]:
    """Process loose uploaded image files."""
    valid_photos: list[ProcessedPhoto] = []

    for filename, data in files:
        if is_ignored_file(filename):
            summary.ignored.append(filename)
            continue

        if len(data) > MAX_IMAGE_BYTES:
            summary.rejected[os.path.basename(filename)] = "over 10 MB"
            continue

        try:
            clean_bytes, mime = process_and_strip_exif(data)
            valid_photos.append(
                ProcessedPhoto(
                    filename=filename,
                    basename=clean_base_name(filename),
                    clean_bytes=clean_bytes,
                    mime_type=mime,
                )
            )
        except Exception:
            summary.rejected[os.path.basename(filename)] = "not a valid image"

    return valid_photos


def match_photos_to_rows(
    rows: list[Any],  # list of NormalizedRow
    photos: list[ProcessedPhoto],
    summary: PhotoSummary,
    url_fetch_enabled: bool = True,
) -> None:
    """Match photos to rows in-place based on Section 8 rules (first hit wins)."""
    # Build fast lookups by clean base name
    # Base name -> list of photos
    photo_by_basename: dict[str, list[ProcessedPhoto]] = {}
    for p in photos:
        photo_by_basename.setdefault(p.basename, []).append(p)

    used_photos: set[str] = set()

    # Create mapping of code -> row
    row_by_code: dict[str, Any] = {}
    for r in rows:
        if r.product_code and not r.is_error and r.action != "skipped":
            row_by_code[r.product_code.strip().upper()] = r

    # 1. First hit wins for each row
    for r in rows:
        if not r.product_code or r.is_error or r.action in ("skipped", "duplicate_skipped"):
            continue

        code = r.product_code.strip()
        code_upper = code.upper()
        clean_code = code.lower()

        # Rule 1: Sheet's photo column holds a filename
        sheet_photo_col = (r.sheet_photo_value or "").strip()
        if sheet_photo_col and not sheet_photo_col.lower().startswith("http"):
            target_base = clean_base_name(sheet_photo_col)
            candidates = photo_by_basename.get(target_base, [])
            for cand in candidates:
                if cand.filename not in used_photos:
                    r.photo_file = cand
                    used_photos.add(cand.filename)
                    summary.matched += 1
                    break

        # Rule 2: Sheet's photo column holds a URL
        if not r.photo_file and sheet_photo_col and sheet_photo_col.lower().startswith("http"):
            if url_fetch_enabled:
                try:
                    cached = get_cached_photo_url(sheet_photo_col)
                    if cached:
                        clean_bytes, clean_mime = cached
                    else:
                        data, mime = safe_fetch_image(sheet_photo_col)
                        clean_bytes, clean_mime = process_and_strip_exif(data)
                        cache_photo_url(sheet_photo_col, clean_bytes, clean_mime)
                    p = ProcessedPhoto(
                        filename=sheet_photo_col,
                        basename=clean_base_name(sheet_photo_col) or clean_code,
                        clean_bytes=clean_bytes,
                        mime_type=clean_mime,
                    )
                    r.photo_file = p
                    summary.matched += 1
                except Exception as exc:
                    # Failed URL never fails the row: product created without photo
                    r.photo_error = f"URL fetch failed: {exc}"
                    # Record rejected reason if applicable
                    if "exceed" in str(exc).lower():
                        summary.rejected[sheet_photo_col] = "over 10 MB"

        # Rule 3: File whose base name equals the product code
        if not r.photo_file:
            candidates = photo_by_basename.get(clean_code, [])
            for cand in candidates:
                if cand.filename not in used_photos:
                    r.photo_file = cand
                    used_photos.add(cand.filename)
                    summary.matched += 1
                    break

    # Rule 4: Extra photos (Code + suffix)
    # Check remaining unused photos
    for p in photos:
        if p.filename in used_photos:
            continue

        # Check if basename matches a code + suffix
        # e.g. '420001_2' -> prefix '420001'
        matched_code: str | None = None

        for code_upper, r in row_by_code.items():
            code_lower = code_upper.lower()
            if p.basename.startswith(code_lower):
                remainder = p.basename[len(code_lower) :]
                if SUFFIX_PATTERN.match(remainder):
                    matched_code = code_upper
                    break

        if matched_code:
            target_row = row_by_code[matched_code]
            target_row.extra_photos.append(p)
            used_photos.add(p.filename)
            summary.extra_photos += 1

    # Remaining unused valid images -> unmatched_files
    for p in photos:
        if p.filename not in used_photos:
            summary.unmatched_files.append(os.path.basename(p.filename))

    # Track products without photo
    no_photo_codes = set()
    for r in rows:
        if not r.product_code or r.is_error or r.action in ("skipped", "duplicate_skipped"):
            continue
        if not r.photo_file:
            no_photo_codes.add(r.product_code)
    summary.products_without_photo = sorted(no_photo_codes)


def match_photos_images_only(
    existing_products: list[dict[str, Any]],
    photos: list[ProcessedPhoto],
    summary: PhotoSummary,
) -> dict[str, dict[str, Any]]:
    """Match photos in an images-only upload (source_kind='images_only').

    Attaches to existing products by code (rules 3-4).
    Never creates products.
    Attach main photo only if product has none; otherwise add as extra.
    Returns: dict mapping product_code -> {'main_photo': photo, 'extra_photos': [photos]}
    """
    photo_by_basename: dict[str, list[ProcessedPhoto]] = {}
    for p in photos:
        photo_by_basename.setdefault(p.basename, []).append(p)

    used_photos: set[str] = set()
    result: dict[str, dict[str, Any]] = {}

    existing_by_code = {p["product_code"].strip().upper(): p for p in existing_products if p.get("product_code")}

    # Match Rule 3: exact code match
    for code_upper, prod in existing_by_code.items():
        clean_code = code_upper.lower()
        candidates = photo_by_basename.get(clean_code, [])

        for cand in candidates:
            if cand.filename in used_photos:
                continue

            entry = result.setdefault(code_upper, {"main_photo": None, "extra_photos": []})

            # If existing product has no main photo and none assigned yet:
            if not prod.get("raw_image_url") and entry["main_photo"] is None:
                entry["main_photo"] = cand
                used_photos.add(cand.filename)
                summary.matched += 1
            else:
                # Product already has main photo -> add as extra photo
                entry["extra_photos"].append(cand)
                used_photos.add(cand.filename)
                summary.extra_photos += 1

    # Match Rule 4: code + suffix
    for p in photos:
        if p.filename in used_photos:
            continue

        for code_upper, prod in existing_by_code.items():
            code_lower = code_upper.lower()
            if p.basename.startswith(code_lower):
                remainder = p.basename[len(code_lower) :]
                if SUFFIX_PATTERN.match(remainder):
                    entry = result.setdefault(code_upper, {"main_photo": None, "extra_photos": []})
                    entry["extra_photos"].append(p)
                    used_photos.add(p.filename)
                    summary.extra_photos += 1
                    break

    # Unmatched photos
    for p in photos:
        if p.filename not in used_photos:
            summary.unmatched_files.append(os.path.basename(p.filename))

    return result


async def match_photos_to_rows_async(
    rows: list[Any],
    photos: list[ProcessedPhoto],
    summary: PhotoSummary,
    url_fetch_enabled: bool = True,
) -> None:
    """Match photos to rows in-place based on Section 8 rules (first hit wins).

    Asynchronous variant that downloads photo URLs in threads bounded by PHOTO_SEMAPHORE (4)
    without blocking the FastAPI event loop (Item 18).
    """
    photo_by_basename: dict[str, list[ProcessedPhoto]] = {}
    for p in photos:
        photo_by_basename.setdefault(p.basename, []).append(p)

    used_photos: set[str] = set()

    row_by_code: dict[str, Any] = {}
    for r in rows:
        if r.product_code and not r.is_error and r.action != "skipped":
            row_by_code[r.product_code.strip().upper()] = r

    # 1. First hit wins for each row: Rule 1 (Filename)
    for r in rows:
        if not r.product_code or r.is_error or r.action in ("skipped", "duplicate_skipped"):
            continue

        sheet_photo_col = (r.sheet_photo_value or "").strip()
        if sheet_photo_col and not sheet_photo_col.lower().startswith("http"):
            target_base = clean_base_name(sheet_photo_col)
            candidates = photo_by_basename.get(target_base, [])
            for cand in candidates:
                if cand.filename not in used_photos:
                    r.photo_file = cand
                    used_photos.add(cand.filename)
                    summary.matched += 1
                    break

    # 2. Rule 2: Photo column holds a URL (downloaded concurrently in worker threads)
    if url_fetch_enabled:
        rows_needing_url = [
            r for r in rows
            if not r.photo_file
            and r.product_code
            and not r.is_error
            and r.action not in ("skipped", "duplicate_skipped")
            and (r.sheet_photo_value or "").strip().lower().startswith("http")
        ]

        if rows_needing_url:
            async def _fetch_url_for_row(r: Any) -> None:
                sheet_photo_col = (r.sheet_photo_value or "").strip()
                code = r.product_code.strip()
                clean_code = code.lower()

                cached = get_cached_photo_url(sheet_photo_col)
                if cached:
                    clean_bytes, clean_mime = cached
                    p = ProcessedPhoto(
                        filename=sheet_photo_col,
                        basename=clean_base_name(sheet_photo_col) or clean_code,
                        clean_bytes=clean_bytes,
                        mime_type=clean_mime,
                    )
                    r.photo_file = p
                    summary.matched += 1
                    return

                async with PHOTO_SEMAPHORE:
                    cached = get_cached_photo_url(sheet_photo_col)
                    if cached:
                        clean_bytes, clean_mime = cached
                        p = ProcessedPhoto(
                            filename=sheet_photo_col,
                            basename=clean_base_name(sheet_photo_col) or clean_code,
                            clean_bytes=clean_bytes,
                            mime_type=clean_mime,
                        )
                        r.photo_file = p
                        summary.matched += 1
                        return

                    try:
                        def _fetch_sync() -> tuple[bytes, str]:
                            raw, _ = safe_fetch_image(sheet_photo_col)
                            return process_and_strip_exif(raw)

                        clean_bytes, clean_mime = await asyncio.to_thread(_fetch_sync)
                        cache_photo_url(sheet_photo_col, clean_bytes, clean_mime)
                        p = ProcessedPhoto(
                            filename=sheet_photo_col,
                            basename=clean_base_name(sheet_photo_col) or clean_code,
                            clean_bytes=clean_bytes,
                            mime_type=clean_mime,
                        )
                        r.photo_file = p
                        summary.matched += 1
                    except Exception as exc:
                        r.photo_error = f"URL fetch failed: {exc}"
                        if "exceed" in str(exc).lower():
                            summary.rejected[sheet_photo_col] = "over 10 MB"

            await asyncio.gather(*[_fetch_url_for_row(r) for r in rows_needing_url])

    # 3. Rule 3: File whose base name equals the product code
    for r in rows:
        if not r.product_code or r.is_error or r.action in ("skipped", "duplicate_skipped"):
            continue
        if not r.photo_file:
            clean_code = r.product_code.strip().lower()
            candidates = photo_by_basename.get(clean_code, [])
            for cand in candidates:
                if cand.filename not in used_photos:
                    r.photo_file = cand
                    used_photos.add(cand.filename)
                    summary.matched += 1
                    break

    # 4. Rule 4: Extra photos (Code + suffix)
    for p in photos:
        if p.filename in used_photos:
            continue
        matched_code: str | None = None
        for code_upper, r in row_by_code.items():
            code_lower = code_upper.lower()
            if p.basename.startswith(code_lower):
                remainder = p.basename[len(code_lower) :]
                if SUFFIX_PATTERN.match(remainder):
                    matched_code = code_upper
                    break
        if matched_code:
            target_row = row_by_code[matched_code]
            target_row.extra_photos.append(p)
            used_photos.add(p.filename)
            summary.extra_photos += 1

    # Remaining unused valid images -> unmatched_files
    for p in photos:
        if p.filename not in used_photos:
            summary.unmatched_files.append(os.path.basename(p.filename))

    # Track products without photo
    no_photo_codes = set()
    for r in rows:
        if not r.product_code or r.is_error or r.action in ("skipped", "duplicate_skipped"):
            continue
        if not r.photo_file:
            no_photo_codes.add(r.product_code)
    summary.products_without_photo = sorted(no_photo_codes)
