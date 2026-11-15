"""app/services/catalogue_import/normalize.py

Normalizes raw spreadsheet rows according to the specification in section 6.
Enforces canonical taxonomy, safe weights, plain-text injection resistance,
and extra attributes storage.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.services.catalogue_import.reader import RawRow
from app.services.catalogue_import.synonyms import (
    CANONICAL_CATEGORIES,
    CANONICAL_JEWELLERY_TYPES,
    CANONICAL_PURITIES,
    CANONICAL_SIZES,
    CANONICAL_STYLES,
    match_value_synonym,
)

LABEL_BY_TYPE = {
    "rings": "Rings",
    "necklace": "Necklace",
    "chain": "Chains",
    "earrings": "Earrings",
    "haram": "Haram",
    "pendant": "Pendant",
    "bangles": "Bangles",
    "nosepins": "Nosepins",
    "mangalsutra": "Mangalsutra",
}

UNIT_MULTIPLIERS = {
    "mg": 0.001,
    "g": 1.0,
    "gm": 1.0,
    "gms": 1.0,
    "gram": 1.0,
    "grams": 1.0,
    "kg": 1000.0,
    "kgs": 1000.0,
    "tola": 11.6638,
}


@dataclass
class NormalizedRow:
    sheet: str
    row_number: int
    raw: dict[str, str]
    product_code: Optional[str] = None
    title: Optional[str] = None
    jewellery_type: Optional[str] = None
    category: Optional[str] = None
    style: Optional[str] = None
    size: Optional[str] = None
    metal_purity: Optional[str] = None
    gross_weight: Optional[float] = None
    stone_weight: Optional[float] = None
    net_weight: Optional[float] = None
    stock_available: bool = False
    make_to_order_days: Optional[int] = None
    extra_attributes: dict[str, str] = field(default_factory=dict)
    photo_refs: list[str] = field(default_factory=list)
    sheet_photo_value: Optional[str] = None
    photo_file: Any = None
    extra_photos: list[Any] = field(default_factory=list)
    photo_error: Optional[str] = None
    messages: list[dict[str, str]] = field(default_factory=list)
    action: Optional[str] = None  # create, update, unchanged, conflict, error, skipped, duplicate_skipped
    changes: Optional[dict[str, Any]] = None
    resolution: Optional[str] = None  # "sheet" | "mine"

    @property
    def is_error(self) -> bool:
        return any(m.get("level") == "error" for m in self.messages)

    @property
    def is_skipped(self) -> bool:
        return self.action == "skipped"

    def to_normalized_dict(self) -> dict[str, Any]:
        """Returns dict of canonical fields."""
        return {
            "title": self.title,
            "jewellery_type": self.jewellery_type,
            "category": self.category,
            "style": self.style,
            "size": self.size,
            "metal_purity": self.metal_purity,
            "gross_weight": self.gross_weight,
            "stone_weight": self.stone_weight,
            "net_weight": self.net_weight,
            "stock_available": self.stock_available,
            "make_to_order_days": self.make_to_order_days,
            "extra_attributes": self.extra_attributes,
        }


def parse_numeric_weight(raw_str: str) -> tuple[Optional[float], Optional[str]]:
    """
    Parses a weight string into grams.
    Handles decimal commas ('12,5'), scientific notation/absurd numbers, and units (mg, g, kg, tola).
    Returns (weight_in_grams, error_msg).
    """
    if raw_str is None or str(raw_str).strip() == "":
        return None, None

    s = str(raw_str).strip().lower()
    # Replace decimal comma with decimal dot if there is only one comma
    if s.count(",") == 1 and "." not in s:
        s = s.replace(",", ".")

    # Match number and optional unit
    m = re.match(r"^([+-]?\d+(?:\.\d+)?(?:e[+-]?\d+)?)\s*([a-z]+)?$", s)
    if not m:
        return None, f"weight '{raw_str}' is not a valid number"

    num_str, unit = m.group(1), m.group(2)
    try:
        val = float(num_str)
    except ValueError:
        return None, f"weight '{raw_str}' could not be parsed as float"

    if math.isnan(val) or math.isinf(val):
        return None, f"weight '{raw_str}' is out of range"

    # Apply unit multiplier
    if unit:
        if unit in UNIT_MULTIPLIERS:
            val *= UNIT_MULTIPLIERS[unit]
        else:
            return None, f"unknown weight unit '{unit}' in '{raw_str}'"

    val = round(val, 4)

    if val <= 0:
        return None, f"weight {val} must be greater than 0"
    if val > 5000:
        return None, f"weight {val} exceeds maximum allowed 5000 grams"

    return val, None


def normalize_chain_type(value: Optional[str]) -> Optional[str]:
    """Ensures chain aliases map to canonical 'chain'."""
    if not value:
        return value
    v = value.strip().lower()
    if v in ("chain", "chains", "neck chain", "neck chains"):
        return "chain"
    return v


def normalize_row(
    raw_row: RawRow,
    column_map: dict[str, str],
    value_maps: dict[str, dict[str, str]],
) -> NormalizedRow:
    """
    Normalizes a single RawRow into a NormalizedRow.
    Applies the Normalization Contract (section 6).
    """
    norm = NormalizedRow(sheet=raw_row.sheet, row_number=raw_row.row_number, raw=raw_row.raw)
    raw = raw_row.raw

    # 1. Product Code
    code_col = next((c for c in raw if column_map.get(c) == "product_code"), None)
    raw_code = raw.get(code_col, "") if code_col else ""
    raw_code_str = str(raw_code).strip()

    # Strip trailing .0 from numeric cells
    if raw_code_str.endswith(".0") and raw_code_str[:-2].isdigit():
        raw_code_str = raw_code_str[:-2]
    # Collapse inner whitespace and uppercase
    code_cleaned = re.sub(r"\s+", " ", raw_code_str).strip().upper()

    if not code_cleaned:
        norm.messages.append({"level": "error", "field": "product_code", "text": "product code is missing"})
    else:
        norm.product_code = code_cleaned

    # 2. Extra attributes
    for col, target in column_map.items():
        if target.startswith("extra:") and col in raw:
            attr_name = target.split(":", 1)[1]
            val = str(raw.get(col, "")).strip()
            if val:
                # Max 2000 chars
                if len(val) > 2000:
                    val = val[:2000]
                    norm.messages.append({
                        "level": "warning",
                        "field": f"extra:{attr_name}",
                        "text": f"extra attribute '{attr_name}' was truncated to 2,000 characters",
                    })
                norm.extra_attributes[attr_name] = val

    # Cap extra attributes at 30 keys
    if len(norm.extra_attributes) > 30:
        keys = list(norm.extra_attributes.keys())[30:]
        for k in keys:
            del norm.extra_attributes[k]
        norm.messages.append({"level": "warning", "field": "extra_attributes", "text": "extra attributes capped at 30 keys"})

    # 3. Jewellery Type (Required)
    type_col = next((c for c in raw if column_map.get(c) == "jewellery_type"), None)
    raw_type = str(raw.get(type_col, "")).strip() if type_col else ""

    # Also check if Description/Title contains type if type column is missing or blank
    if not raw_type:
        desc_col = next((c for c in raw if column_map.get(c) in ("description", "title") or c.lower() in ("description", "desc")), None)
        if desc_col:
            raw_type = str(raw.get(desc_col, "")).strip()

    if not raw_type:
        norm.messages.append({"level": "error", "field": "jewellery_type", "text": "jewellery type is missing"})
    else:
        # Check value maps first (e.g. user answered 'rg' -> 'rings')
        canon_type = None
        for k, v in value_maps.get("jewellery_type", {}).items():
            if k.lower() in raw_type.lower():
                canon_type = v
                break

        if not canon_type:
            canon_type, _ = match_value_synonym("jewellery_type", raw_type)

        if canon_type == "skip":
            norm.action = "skipped"
            norm.messages.append({"level": "info", "field": "jewellery_type", "text": f"row skipped due to type '{raw_type}'"})
        elif canon_type in CANONICAL_JEWELLERY_TYPES:
            norm.jewellery_type = normalize_chain_type(canon_type)
        else:
            norm.messages.append({"level": "error", "field": "jewellery_type", "text": f"unsupported jewellery type '{raw_type}'"})

    # 4. Category (Material)
    cat_col = next((c for c in raw if column_map.get(c) == "category"), None)
    raw_cat = str(raw.get(cat_col, "")).strip() if cat_col else ""
    if not raw_cat and not cat_col:
        # Check Description if category not in columns
        desc_col = next((c for c in raw if c.lower() in ("description", "desc")), None)
        if desc_col:
            raw_cat = str(raw.get(desc_col, "")).strip()

    if raw_cat:
        canon_cat = None
        for k, v in value_maps.get("category", {}).items():
            if k.lower() in raw_cat.lower():
                canon_cat = v
                break
        if not canon_cat:
            canon_cat, _ = match_value_synonym("category", raw_cat)
        if canon_cat in CANONICAL_CATEGORIES:
            norm.category = canon_cat

    # 5. Metal Purity
    pur_col = next((c for c in raw if column_map.get(c) == "metal_purity"), None)
    raw_pur = str(raw.get(pur_col, "")).strip() if pur_col else ""
    if not raw_pur and not pur_col:
        desc_col = next((c for c in raw if c.lower() in ("description", "desc")), None)
        if desc_col:
            raw_pur = str(raw.get(desc_col, "")).strip()

    if raw_pur:
        canon_pur = None
        # Check value_maps
        for k, v in value_maps.get("metal_purity", {}).items():
            if k.lower() in raw_pur.lower():
                canon_pur = v
                break
        if not canon_pur:
            canon_pur, _ = match_value_synonym("metal_purity", raw_pur)
        if canon_pur in CANONICAL_PURITIES:
            norm.metal_purity = canon_pur

    # 6. Style
    style_col = next((c for c in raw if column_map.get(c) == "style"), None)
    raw_style = str(raw.get(style_col, "")).strip() if style_col else ""
    if raw_style:
        canon_style = None
        for k, v in value_maps.get("style", {}).items():
            if k.lower() in raw_style.lower():
                canon_style = v
                break
        if not canon_style:
            canon_style, _ = match_value_synonym("style", raw_style)
        if canon_style in CANONICAL_STYLES:
            norm.style = canon_style

    # 7. Size
    size_col = next((c for c in raw if column_map.get(c) == "size"), None)
    raw_size = str(raw.get(size_col, "")).strip() if size_col else ""
    if raw_size:
        size_low = raw_size.lower()
        if size_low in ("free", "free size", "fs", "freesize", "adjustable"):
            norm.size = "freesize"
        elif size_low in ("xs", "s", "m", "l", "xl"):
            norm.size = size_low
        else:
            # Keep as trimmed text up to 30 chars
            norm.size = raw_size[:30].strip()

    # 8. Weights (gross, stone, net)
    gross_col = next((c for c in raw if column_map.get(c) == "gross_weight"), None)
    stone_col = next((c for c in raw if column_map.get(c) == "stone_weight"), None)
    net_col = next((c for c in raw if column_map.get(c) == "net_weight"), None)


    # Gross weight
    if gross_col and raw.get(gross_col) is not None and str(raw.get(gross_col)).strip():
        gw, err = parse_numeric_weight(raw.get(gross_col))
        if err:
            norm.messages.append({"level": "error", "field": "gross_weight", "text": err})
        else:
            norm.gross_weight = gw

    # Stone weight (can be blank -> 0.0 or None)
    if stone_col and raw.get(stone_col) is not None and str(raw.get(stone_col)).strip():
        raw_st = str(raw.get(stone_col)).strip()
        # Parse stone weight; accept 0
        st, err = parse_numeric_weight(raw_st)
        if err and "must be greater than 0" in err:
            st = 0.0
            err = None
        if err:
            norm.messages.append({"level": "error", "field": "stone_weight", "text": err})
        else:
            norm.stone_weight = st

    # Net weight
    if net_col and raw.get(net_col) is not None and str(raw.get(net_col)).strip():
        nw, err = parse_numeric_weight(raw.get(net_col))
        if err:
            norm.messages.append({"level": "error", "field": "net_weight", "text": err})
        else:
            norm.net_weight = nw

    # Weight relationships and calculations
    if norm.gross_weight is not None:
        # Check stone weight relation
        if norm.stone_weight is not None and norm.stone_weight > norm.gross_weight:
            norm.messages.append({
                "level": "error",
                "field": "stone_weight",
                "text": f"stone weight ({norm.stone_weight}g) is greater than gross weight ({norm.gross_weight}g)",
            })

        # Calculate net weight if missing
        if norm.net_weight is None and not any(m.get("field") == "net_weight" for m in norm.messages):
            st_val = norm.stone_weight or 0.0
            if st_val <= norm.gross_weight:
                norm.net_weight = round(norm.gross_weight - st_val, 4)
                norm.messages.append({
                    "level": "warning",
                    "field": "net_weight",
                    "text": f"net weight calculated as gross - stone ({norm.net_weight}g)",
                })

        # Check all three consistency
        if norm.gross_weight is not None and norm.stone_weight is not None and norm.net_weight is not None:
            expected_net = round(norm.gross_weight - norm.stone_weight, 4)
            if abs(expected_net - norm.net_weight) > 0.01:
                norm.messages.append({
                    "level": "warning",
                    "field": "net_weight",
                    "text": f"weights disagree: gross ({norm.gross_weight}g) - stone ({norm.stone_weight}g) = {expected_net}g, but net is {norm.net_weight}g",
                })

    # 9. Stock Available & Make To Order Days
    stock_col = next((c for c in raw if column_map.get(c) == "stock_available"), None)
    if stock_col and raw.get(stock_col) is not None:
        s_val = str(raw.get(stock_col)).strip().lower()
        if s_val in ("ready", "r", "stock", "in stock", "haan", "ha", "yes", "y", "1", "✓", "true"):
            norm.stock_available = True
        elif s_val in ("order", "o", "mto", "nahi", "no", "n", "0", "false"):
            norm.stock_available = False
        elif s_val.isdigit():
            # If quantity: > 0 means ready stock
            norm.stock_available = int(s_val) > 0

    days_col = next((c for c in raw if column_map.get(c) == "make_to_order_days"), None)
    if days_col and raw.get(days_col) is not None and str(raw.get(days_col)).strip():
        try:
            d = int(float(str(raw.get(days_col)).strip()))
            if 1 <= d <= 365:
                norm.make_to_order_days = d
            else:
                norm.messages.append({"level": "warning", "field": "make_to_order_days", "text": f"days {d} out of range (1-365)"})
        except ValueError:
            norm.messages.append({"level": "warning", "field": "make_to_order_days", "text": f"days '{raw.get(days_col)}' is not an integer"})

    # 10. Photo Refs
    photo_col = next((c for c in raw if column_map.get(c) == "photo"), None)
    if photo_col and raw.get(photo_col):
        photo_val = str(raw.get(photo_col)).strip()
        if photo_val:
            norm.photo_refs.append(photo_val)
            norm.sheet_photo_value = photo_val

    # 11. Title
    title_col = next((c for c in raw if column_map.get(c) == "title"), None)
    raw_title = str(raw.get(title_col, "")).strip() if title_col else ""

    if raw_title:
        norm.title = raw_title[:200]
    else:
        # Default generated title: "<Label> <product_code>"
        type_label = LABEL_BY_TYPE.get(norm.jewellery_type or "", "Jewellery")
        norm.title = f"{type_label} {norm.product_code}" if norm.product_code else type_label

    return norm
