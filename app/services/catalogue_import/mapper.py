"""app/services/catalogue_import/mapper.py

Column and value mapping engine for catalogue import:
1. Profile match (>= 70% header match)
2. Rule-based synonym mapper (synonyms.py)
3. LLM fallback (JSON schema, temperature 0, graceful on failure)
4. Questions generator for ambiguous/unsupported fields.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import settings
from app.services.catalogue_import.reader import ReadResult
from app.services.catalogue_import.synonyms import (
    CANONICAL_CATEGORIES,
    CANONICAL_JEWELLERY_TYPES,
    CANONICAL_PURITIES,
    CANONICAL_SIZES,
    CANONICAL_STYLES,
    match_header_synonym,
    match_value_synonym,
    normalize_header_text,
)

logger = logging.getLogger(__name__)

UNSUPPORTED_TYPES_TO_PROMPT = {"anklet", "payal", "kamarbandh", "bichhiya", "waist belt"}
UNSUPPORTED_STYLES_TO_PROMPT = {"temple"}
UNSUPPORTED_PURITIES_TO_PROMPT = {"21k", "20k", "995"}


@dataclass
class Question:
    id: str
    kind: str  # "column" | "value" | "sheet" | "conflict_policy"
    text: str
    column: Optional[str] = None
    sample_values: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "column": self.column,
            "sample_values": self.sample_values,
            "options": self.options,
        }


@dataclass
class MappingResult:
    profile_id: Optional[str]
    column_map: dict[str, str]  # raw_header -> canonical_field or "extra:Name" or "ignore"
    value_maps: dict[str, dict[str, str]]  # field -> {raw_val -> canonical_val}
    sheet_rules: dict[str, Any]  # e.g. {"sheets": ["Stock Register"]}
    questions: list[Question]
    llm_calls: int = 0
    llm_tokens: dict[str, int] = field(default_factory=dict)
    is_ready: bool = False


def extract_sample_values(read_result: ReadResult, col_name: str, limit: int = 3) -> list[str]:
    """Returns up to `limit` non-empty distinct sample values for a column."""
    samples: list[str] = []
    for r in read_result.rows:
        val = r.raw.get(col_name, "").strip()
        if val and val not in samples:
            samples.append(val)
            if len(samples) >= limit:
                break
    return samples


def check_profile_match(headers: list[str], profile: dict[str, Any]) -> bool:
    """Returns True if >= 70% of normalized headers match profile fingerprint."""
    profile_fp = profile.get("header_fingerprint", [])
    if not profile_fp or not headers:
        return False
    norm_headers = {normalize_header_text(h) for h in headers}
    norm_profile = {normalize_header_text(h) for h in profile_fp}
    common = norm_headers & norm_profile
    ratio = len(common) / max(len(norm_headers), 1)
    return ratio >= 0.70


def try_llm_column_mapping(
    unresolved_headers: list[str],
    sample_rows: list[dict[str, str]],
) -> tuple[dict[str, str], int, dict[str, int]]:
    """
    Calls OpenAI with structured JSON schema for unresolved columns only.
    Falls back gracefully on any error without blocking import.
    """
    if not settings.CATALOGUE_IMPORT_LLM_ENABLED:
        return {}, 0, {}

    try:
        from openai import OpenAI
        client = OpenAI()
        
        prompt = (
            "You are an assistant mapping jewelry spreadsheet columns to canonical database fields.\n"
            "Canonical target fields:\n"
            "- product_code: unique identifier/SKU\n"
            "- title: product name or title\n"
            "- jewellery_type: type (e.g. rings, necklace, earrings, etc.)\n"
            "- category: material (gold, silver, diamond, platinum, etc.)\n"
            "- metal_purity: purity (22k, 18k, 925, etc.)\n"
            "- gross_weight: gross weight in grams\n"
            "- stone_weight: stone weight in grams\n"
            "- net_weight: net weight in grams\n"
            "- size: size (ring size, length)\n"
            "- style: style aesthetic\n"
            "- stock_available: in stock status\n"
            "- make_to_order_days: production lead time\n"
            "- photo: photo filename or URL\n"
            "- extra:<Name>: extra attribute to preserve\n"
            "- ignore: discard this column\n\n"
            f"Unresolved columns: {unresolved_headers}\n"
            f"Sample data rows (up to 5): {sample_rows[:5]}\n"
            "Map each unresolved column to the best target field or 'ignore' or 'extra:<HeaderName>'."
        )

        response = client.chat.completions.create(
            model=settings.CATALOGUE_IMPORT_MODEL,
            messages=[
                {"role": "system", "content": "You output JSON mapping column name to target field."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=500,
        )

        content = response.choices[0].message.content or "{}"
        parsed = json.loads(content)
        usage = {
            "input_tokens": response.usage.prompt_tokens if response.usage else 0,
            "output_tokens": response.usage.completion_tokens if response.usage else 0,
        }
        return parsed, 1, usage
    except Exception as exc:
        logger.warning("LLM column mapping failed; continuing rule-based: %s", exc)
        return {}, 0, {}


def analyze_import(
    read_result: ReadResult,
    profile: Optional[dict[str, Any]] = None,
    answers: Optional[dict[str, Any]] = None,
) -> MappingResult:
    """
    Main analysis pipeline:
    Determines column_map, value_maps, sheet_rules, and any required questions.
    """
    answers = answers or {}
    column_map: dict[str, str] = {}
    value_maps: dict[str, dict[str, str]] = {
        "jewellery_type": {},
        "category": {},
        "metal_purity": {},
        "style": {},
    }
    sheet_rules: dict[str, Any] = {}
    questions: list[Question] = []
    llm_calls = 0
    llm_tokens: dict[str, int] = {}

    # Extract all distinct headers across product sheets
    all_headers: list[str] = []
    for sheet in read_result.sheets:
        if sheet.is_product_sheet and not sheet.is_hidden:
            for h in sheet.headers:
                if h not in all_headers:
                    all_headers.append(h)

    # ── 1. Check Profile ──────────────────────────────────────────────────────
    profile_id = None
    if profile and check_profile_match(all_headers, profile):
        profile_id = str(profile.get("id")) if profile.get("id") else None
        column_map.update(profile.get("column_map", {}))
        for k, vm in profile.get("value_maps", {}).items():
            if k in value_maps:
                value_maps[k].update(vm)
        sheet_rules.update(profile.get("sheet_rules", {}))

    # Apply any explicit user answers provided in previous turns
    for ans_key, ans_val in answers.items():
        if ans_key == "q_sheets" or "sheet" in ans_key.lower():
            if isinstance(ans_val, list):
                sheet_rules["sheets"] = ans_val
            elif isinstance(ans_val, str):
                if "all" in ans_val.lower() or "both" in ans_val.lower():
                    sheet_rules["sheets"] = [s.name for s in read_result.sheets if s.is_product_sheet and not s.is_hidden]
                else:
                    # e.g. "Stock Register only" -> ["Stock Register"]
                    matched = [s.name for s in read_result.sheets if s.name.lower() in ans_val.lower()]
                    sheet_rules["sheets"] = matched or [ans_val]
        elif ans_key.startswith("q_val_"):
            # e.g. q_val_temple -> "traditional", q_val_21k -> "22k"
            parts = ans_key.split("_", 3)
            if len(parts) >= 4:
                _, _, field_name, raw_token = parts
                if field_name in value_maps:
                    value_maps[field_name][raw_token.lower()] = str(ans_val).strip().lower()
        elif ans_key.startswith("q_col_"):
            # e.g. q_col_wt -> "gross_weight"
            parts = ans_key.split("_", 2)
            if len(parts) >= 3:
                col_name = parts[2]
                ans_str = str(ans_val).strip()
                if "gross" in ans_str.lower():
                    column_map[col_name] = "gross_weight"
                elif "net" in ans_str.lower():
                    column_map[col_name] = "net_weight"
                elif "ignore" in ans_str.lower():
                    column_map[col_name] = "ignore"
                elif "extra" in ans_str.lower():
                    column_map[col_name] = f"extra:{col_name}"
                elif "stock" in ans_str.lower() or "ready" in ans_str.lower():
                    column_map[col_name] = "stock_available"
                else:
                    column_map[col_name] = ans_str

    # ── Sheet Questions (if multiple visible product sheets and not answered) ──
    if read_result.multiple_product_sheets and "sheets" not in sheet_rules:
        prod_sheets = [s.name for s in read_result.sheets if s.is_product_sheet and not s.is_hidden]
        questions.append(
            Question(
                id="q_sheets",
                kind="sheet",
                text=f"This file has multiple product sheets ({', '.join(prod_sheets)}). Which sheets should be imported?",
                options=[f"Both {prod_sheets[0]} and {prod_sheets[1]}" if len(prod_sheets) == 2 else "All sheets"]
                        + [f"{s} only" for s in prod_sheets] + ["Other"],
            )
        )

    # ── 2. Rule-Based Header Mapping & Questions ─────────────────────────────
    unresolved_headers: list[str] = []

    for h in all_headers:
        if h in column_map:
            continue

        h_norm = normalize_header_text(h)

        # Check duplicate headers (e.g. Weight and Weight (2))
        if re.search(r"\(\d+\)$", h.strip()):
            base = re.sub(r"\s*\(\d+\)$", "", h.strip())
            samples = extract_sample_values(read_result, h)
            questions.append(
                Question(
                    id=f"q_col_{h}",
                    kind="column",
                    text=f"The sheet has another column named '{base}' ({h}). What does this column contain?",
                    column=h,
                    sample_values=samples,
                    options=["Net weight", "Gross weight", "Stone weight", "Keep as extra info", "Ignore column", "Other"],
                )
            )
            continue

        target, conf = match_header_synonym(h)

        # Ambiguous headers must generate questions
        if target == "ambiguous_weight":
            samples = extract_sample_values(read_result, h)
            questions.append(
                Question(
                    id=f"q_col_{h}",
                    kind="column",
                    text=f"Column '{h}' represents weight. Is this Gross Weight or Net Weight?",
                    column=h,
                    sample_values=samples,
                    options=["Gross weight", "Net weight", "Other"],
                )
            )
            continue
        elif target == "ambiguous_rate":
            samples = extract_sample_values(read_result, h)
            questions.append(
                Question(
                    id=f"q_col_{h}",
                    kind="column",
                    text=f"Column '{h}' contains price/rate information. How should this be handled?",
                    column=h,
                    sample_values=samples,
                    options=["Ignore this column", "Store as price", "Keep as extra info", "Other"],
                )
            )
            continue
        elif target == "ambiguous_qty":
            samples = extract_sample_values(read_result, h)
            questions.append(
                Question(
                    id=f"q_col_{h}",
                    kind="column",
                    text=f"Column '{h}' contains quantity numbers. How should stock status be inferred?",
                    column=h,
                    sample_values=samples,
                    options=["Qty more than 0 means ready stock", "Ignore this column", "Keep as extra info", "Other"],
                )
            )
            continue

        if target and conf >= 0.85:
            column_map[h] = target
        else:
            # Check for extra info columns (e.g. Karigar, Remarks, Notes)
            if any(k in h_norm for k in ("karigar", "remark", "note", "tippani", "tag", "comment", "vendor", "shelf")):
                samples = extract_sample_values(read_result, h)
                questions.append(
                    Question(
                        id=f"q_col_{h}",
                        kind="column",
                        text=f"Column '{h}' contains custom details. How should it be imported?",
                        column=h,
                        sample_values=samples,
                        options=["Keep as extra info", "Ignore this column", "Other"],
                    )
                )
            else:
                unresolved_headers.append(h)

    # ── 3. LLM Fallback (for unresolved headers) ──────────────────────────────
    if unresolved_headers:
        sample_rows_for_llm = [r.raw for r in read_result.rows[:5]]
        llm_mapped, calls, usage = try_llm_column_mapping(unresolved_headers, sample_rows_for_llm)
        llm_calls += calls
        llm_tokens.update(usage)

        for uh in unresolved_headers:
            if uh in llm_mapped:
                target = llm_mapped[uh]
                if target in HEADER_SYNONYMS or target.startswith("extra:") or target == "ignore":
                    column_map[uh] = target
                else:
                    samples = extract_sample_values(read_result, uh)
                    questions.append(
                        Question(
                            id=f"q_col_{uh}",
                            kind="column",
                            text=f"What does column '{uh}' represent?",
                            column=uh,
                            sample_values=samples,
                            options=["Product title", "Description", "Extra attribute", "Ignore this column", "Other"],
                        )
                    )
            else:
                samples = extract_sample_values(read_result, uh)
                questions.append(
                    Question(
                        id=f"q_col_{uh}",
                        kind="column",
                        text=f"What does column '{uh}' represent?",
                        column=uh,
                        sample_values=samples,
                        options=["Product title", "Description", "Extra attribute", "Ignore this column", "Other"],
                    )
                )

    # ── 4. Value Scanning for Unsupported/Ambiguous Enums ────────────────────
    # Scan raw values in mapped columns
    for r in read_result.rows:
        for raw_col, val in r.raw.items():
            if not val:
                continue
            v_low = val.strip().lower()
            target_field = column_map.get(raw_col)

            # Check Style
            if target_field == "style":
                if v_low in UNSUPPORTED_STYLES_TO_PROMPT and v_low not in value_maps["style"]:
                    q_id = f"q_val_style_{v_low}"
                    if not any(q.id == q_id for q in questions):
                        questions.append(
                            Question(
                                id=q_id,
                                kind="value",
                                text=f"Found style aesthetic '{val}' in column '{raw_col}'. How should this be mapped?",
                                column=raw_col,
                                sample_values=[val],
                                options=["traditional", "antique", "fusion", "Leave blank", "Other"],
                            )
                        )

            # Check Purity
            elif target_field == "metal_purity":
                clean_purity = re.sub(r"\s+", "", v_low)
                if clean_purity in UNSUPPORTED_PURITIES_TO_PROMPT and clean_purity not in value_maps["metal_purity"]:
                    q_id = f"q_val_purity_{clean_purity}"
                    if not any(q.id == q_id for q in questions):
                        questions.append(
                            Question(
                                id=q_id,
                                kind="value",
                                text=f"Found metal purity '{val}' in column '{raw_col}'. How should this be mapped?",
                                column=raw_col,
                                sample_values=[val],
                                options=["22k", "18k", "24k", "14k", "Leave blank", "Other"],
                            )
                        )

            # Check Jewellery Type
            elif target_field == "jewellery_type":
                # Check if entire cell or any token is an unsupported type
                words = re.findall(r"\w+", v_low)
                for w in words:
                    if w in UNSUPPORTED_TYPES_TO_PROMPT and w not in value_maps["jewellery_type"]:
                        q_id = f"q_val_type_{w}"
                        if not any(q.id == q_id for q in questions):
                            questions.append(
                                Question(
                                    id=q_id,
                                    kind="value",
                                    text=f"'{val}' is not a standard jewellery type. How should items with this type be handled?",
                                    column=raw_col,
                                    sample_values=[val],
                                    options=["Skip rows with this type", "Leave type blank", "Other"],
                                )
                            )

            # If column is Title or Description, check for embedded unsupported types (like ANKLET / PAYAL in Fixture C)
            elif target_field in ("title", "description") or raw_col.lower() in ("description", "desc"):
                words = re.findall(r"\w+", v_low)
                for w in words:
                    if w in ("anklet", "payal", "kamarbandh") and w not in value_maps["jewellery_type"]:
                        q_id = f"q_val_type_{w}"
                        if not any(q.id == q_id for q in questions):
                            questions.append(
                                Question(
                                    id=q_id,
                                    kind="value",
                                    text=f"Description mentions '{w.upper()}', which is not a supported jewellery type. How should these items be handled?",
                                    column=raw_col,
                                    sample_values=[val],
                                    options=["Skip rows with this type", "Leave type blank", "Other"],
                                )
                            )

    # Deduplicate questions by ID and cap at 8 questions per round
    seen_ids = set()
    capped_questions: list[Question] = []
    for q in questions:
        if q.id not in seen_ids:
            seen_ids.add(q.id)
            capped_questions.append(q)
            if len(capped_questions) >= 8:
                break

    is_ready = len(capped_questions) == 0

    return MappingResult(
        profile_id=profile_id,
        column_map=column_map,
        value_maps=value_maps,
        sheet_rules=sheet_rules,
        questions=capped_questions,
        llm_calls=llm_calls,
        llm_tokens=llm_tokens,
        is_ready=is_ready,
    )
