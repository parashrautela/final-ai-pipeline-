"""app/services/catalogue_import/merge.py

3-Way Merge Engine and intra-job duplicate detection (section 7).
Compares sheet, base (import_snapshot), and mine (current products table row)
to determine row actions (create, update, unchanged, conflict, error, skipped, duplicate_skipped).
"""

from __future__ import annotations

from typing import Any, Optional
from app.services.catalogue_import.normalize import NormalizedRow

MERGE_FIELDS = (
    "title", "jewellery_type", "category", "style", "size", "metal_purity",
    "gross_weight", "stone_weight", "net_weight", "stock_available",
    "make_to_order_days", "extra_attributes"
)

NUMERIC_FIELDS = {"gross_weight", "stone_weight", "net_weight"}


def values_equal(v1: Any, v2: Any, field_name: str) -> bool:
    """Compares two field values for equality with floating-point tolerance."""
    if field_name == "stock_available":
        return bool(v1) == bool(v2)

    if field_name == "extra_attributes":
        d1 = v1 if isinstance(v1, dict) else {}
        d2 = v2 if isinstance(v2, dict) else {}
        if not d1 and not d2:
            return True
        # If sheet row has no extra attributes, it does not clear existing extra attributes
        if not d1:
            return True
        if set(d1.keys()) != set(d2.keys()):
            return False
        return all(str(d1[k]).strip() == str(d2[k]).strip() for k in d1)

    if field_name == "stone_weight":
        # 0.0, 0, and None are considered equivalent for stone weight
        s1 = float(v1) if v1 not in (None, "") else 0.0
        s2 = float(v2) if v2 not in (None, "") else 0.0
        return abs(s1 - s2) < 0.001

    if v1 is None and v2 is None:
        return True
    if v1 is None or v2 is None:
        return False

    if field_name in NUMERIC_FIELDS:
        try:
            return abs(float(v1) - float(v2)) < 0.001
        except (ValueError, TypeError):
            return str(v1).strip() == str(v2).strip()

    return str(v1).strip().lower() == str(v2).strip().lower()


def detect_intra_job_duplicates(rows: list[NormalizedRow]):
    """
    Checks for duplicate codes within one job:
    - Identical rows: keep one, mark other as 'duplicate_skipped'.
    - Same code with different values: mark both rows as 'error'.
    """
    by_code: dict[str, list[NormalizedRow]] = {}
    for r in rows:
        if not r.product_code or r.is_error or r.is_skipped:
            continue
        c = r.product_code.upper()
        by_code.setdefault(c, []).append(r)

    for code, group in by_code.items():
        if len(group) <= 1:
            continue

        first = group[0]
        first_dict = first.to_normalized_dict()

        # Check if all rows in group have identical normalized values
        all_identical = True
        for other in group[1:]:
            other_dict = other.to_normalized_dict()
            for f in MERGE_FIELDS:
                if not values_equal(first_dict.get(f), other_dict.get(f), f):
                    all_identical = False
                    break
            if not all_identical:
                break

        if all_identical:
            # Keep first row; mark subsequent as duplicate_skipped
            for dup in group[1:]:
                dup.action = "duplicate_skipped"
                dup.messages.append({"level": "info", "field": "product_code", "text": "identical duplicate row skipped"})
        else:
            # Same code with different values -> ALL get error!
            for r in group:
                r.action = "error"
                r.messages.append({
                    "level": "error",
                    "field": "product_code",
                    "text": "same code appears twice with different values",
                })


def merge_rows_with_existing(
    rows: list[NormalizedRow],
    existing_products: list[dict[str, Any]],
    relevant_previous_codes: Optional[set[str]] = None,
) -> dict[str, int]:
    """
    Performs the 3-Way Merge against existing products.
    Returns summary dict.
    """
    # 1. Run duplicate check within job
    detect_intra_job_duplicates(rows)

    by_code: dict[str, dict[str, Any]] = {}
    for p in existing_products:
        pc = p.get("product_code")
        if pc:
            by_code[str(pc).upper()] = p

    summary = {
        "create": 0,
        "update": 0,
        "unchanged": 0,
        "conflict": 0,
        "error": 0,
        "skipped": 0,
        "duplicate_skipped": 0,
        "not_in_file": 0,
    }

    seen_codes_in_file: set[str] = set()
    seen_error_codes: set[str] = set()

    for r in rows:
        if r.action == "duplicate_skipped":
            summary["duplicate_skipped"] += 1
            continue
        if r.action == "skipped":
            summary["skipped"] += 1
            continue
        if r.is_error or r.action == "error":
            r.action = "error"
            code = (r.product_code or "").strip().upper()
            if code:
                if code not in seen_error_codes:
                    seen_error_codes.add(code)
                    summary["error"] += 1
            else:
                summary["error"] += 1
            continue

        code = r.product_code.upper() if r.product_code else ""
        if not code:
            r.action = "error"
            summary["error"] += 1
            continue

        seen_codes_in_file.add(code)

        if code not in by_code:
            r.action = "create"
            summary["create"] += 1
            continue

        # Existing product -> 3-way merge
        mine = by_code[code]
        base = mine.get("import_snapshot") or {}
        sheet_vals = r.to_normalized_dict()

        conflicts: dict[str, Any] = {}
        updates: dict[str, Any] = {}

        for f in MERGE_FIELDS:
            sheet_val = sheet_vals.get(f)
            mine_val = mine.get(f)
            base_val = base.get(f)

            sheet_eq_mine = values_equal(sheet_val, mine_val, f)
            mine_eq_base = values_equal(mine_val, base_val, f)
            sheet_eq_base = values_equal(sheet_val, base_val, f)

            if sheet_eq_mine:
                continue

            # sheet != mine
            if mine_eq_base:
                # Wholesaler did NOT edit in app -> update to sheet
                updates[f] = {"from": mine_val, "to": sheet_val}
            elif sheet_eq_base:
                # Only wholesaler edited in app -> keep mine
                continue
            else:
                # Both changed -> CONFLICT!
                conflicts[f] = {
                    "from": base_val,
                    "to": sheet_val,
                    "mine": mine_val,
                }

        if conflicts:
            r.action = "conflict"
            r.changes = {**updates, **conflicts}
            summary["conflict"] += 1
        elif updates:
            r.action = "update"
            r.changes = updates
            summary["update"] += 1
        else:
            r.action = "unchanged"
            summary["unchanged"] += 1

    # Calculate not_in_file for existing imported products absent from file.
    # Only count products whose previous import came from the same series/profile/sheet.
    candidate_codes = relevant_previous_codes if relevant_previous_codes is not None else set(by_code.keys())
    for pc in candidate_codes:
        if pc in by_code and pc not in seen_codes_in_file:
            summary["not_in_file"] += 1

    return summary
