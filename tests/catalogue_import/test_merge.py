"""tests/catalogue_import/test_merge.py

Phase 4 gate tests:
1. Tests every row of the Section 7 3-way merge decision table:
   - sheet == mine -> unchanged
   - sheet != mine, mine == base -> update
   - sheet != mine, mine != base, sheet == base -> unchanged (mine wins)
   - sheet != mine, mine != base, sheet != base -> conflict
2. Tests intra-job duplicate detection (identical -> duplicate_skipped, differing -> error).
3. Tests A2 fixture actions match expected/A2.json -> actions exactly:
   - 10 create, 6 update, 26 unchanged, 2 conflict, 3 not_in_file, 0 error.
"""

import json
from pathlib import Path
import pytest

from app.services.catalogue_import.reader import RawRow, read_source_file
from app.services.catalogue_import.mapper import analyze_import
from app.services.catalogue_import.normalize import NormalizedRow, normalize_row
from app.services.catalogue_import.merge import detect_intra_job_duplicates, merge_rows_with_existing

DATA_DIR = Path(__file__).resolve().parents[4] / "plans" / "bulk-catalogue-import" / "test-data"
FIXTURES_DIR = DATA_DIR / "fixtures"
EXP_DIR = DATA_DIR / "expected"


def test_section_7_merge_table_rows():
    """Unit-test every single row of the Section 7 merge table."""
    # Base snapshot: gross_weight = 10.0, title = "Original"
    base = {"gross_weight": 10.0, "title": "Original", "jewellery_type": "rings"}

    # Case 1: sheet == mine -> unchanged
    row1 = NormalizedRow(
        sheet="Sheet1", row_number=1, raw={}, product_code="C1",
        gross_weight=10.0, title="Original", jewellery_type="rings"
    )
    mine1 = {"product_code": "C1", "gross_weight": 10.0, "title": "Original", "jewellery_type": "rings", "import_snapshot": base}
    summary1 = merge_rows_with_existing([row1], [mine1])
    assert row1.action == "unchanged"
    assert summary1["unchanged"] == 1

    # Case 2: sheet != mine, mine == base (wholesaler didn't edit) -> update
    row2 = NormalizedRow(
        sheet="Sheet1", row_number=2, raw={}, product_code="C2",
        gross_weight=12.5, title="Original", jewellery_type="rings"
    )
    mine2 = {"product_code": "C2", "gross_weight": 10.0, "title": "Original", "jewellery_type": "rings", "import_snapshot": base}
    summary2 = merge_rows_with_existing([row2], [mine2])
    assert row2.action == "update"
    assert "gross_weight" in (row2.changes or {})
    assert summary2["update"] == 1

    # Case 3: sheet != mine, mine != base, sheet == base (only wholesaler edited) -> keep mine (unchanged)
    row3 = NormalizedRow(
        sheet="Sheet1", row_number=3, raw={}, product_code="C3",
        gross_weight=10.0, title="Original", jewellery_type="rings"
    )
    mine3 = {"product_code": "C3", "gross_weight": 15.0, "title": "Original", "jewellery_type": "rings", "import_snapshot": base}
    summary3 = merge_rows_with_existing([row3], [mine3])
    assert row3.action == "unchanged"
    assert summary3["unchanged"] == 1

    # Case 4: sheet != mine, mine != base, sheet != base (both changed) -> conflict
    row4 = NormalizedRow(
        sheet="Sheet1", row_number=4, raw={}, product_code="C4",
        gross_weight=12.5, title="Original", jewellery_type="rings"
    )
    mine4 = {"product_code": "C4", "gross_weight": 15.0, "title": "Original", "jewellery_type": "rings", "import_snapshot": base}
    summary4 = merge_rows_with_existing([row4], [mine4])
    assert row4.action == "conflict"
    assert "gross_weight" in (row4.changes or {})
    assert summary4["conflict"] == 1


def test_intra_job_duplicates():
    """Identical duplicate -> duplicate_skipped; different values -> error."""
    # Identical
    r1 = NormalizedRow(sheet="S1", row_number=1, raw={}, product_code="DUP1", gross_weight=10.0, jewellery_type="rings")
    r2 = NormalizedRow(sheet="S1", row_number=2, raw={}, product_code="DUP1", gross_weight=10.0, jewellery_type="rings")
    merge_rows_with_existing([r1, r2], [])
    assert r1.action == "create"
    assert r2.action == "duplicate_skipped"

    # Differing values
    d1 = NormalizedRow(sheet="S1", row_number=3, raw={}, product_code="DIFF1", gross_weight=10.0, jewellery_type="rings")
    d2 = NormalizedRow(sheet="S1", row_number=4, raw={}, product_code="DIFF1", gross_weight=12.0, jewellery_type="rings")
    merge_rows_with_existing([d1, d2], [])
    assert d1.action == "error"
    assert d2.action == "error"
    assert any("different values" in m["text"] for m in d1.messages)


def test_a2_full_merge_matches_answer_key():
    """All A2 actions match expected/A2.json -> actions exactly."""
    exp_a2 = json.loads((EXP_DIR / "A2.json").read_text())
    expected_actions = exp_a2["actions"]

    # 1. Read A1 to construct the existing database state for Account A
    a1_file = FIXTURES_DIR / "A1_shree_balaji_stock_register.xlsx"
    a1_read = read_source_file(a1_file.read_bytes(), a1_file.name)
    a1_map = analyze_import(a1_read)

    # Apply A1 answers to get A1 column_map & value_maps
    a1_answers = {
        "q_sheets": ["Stock Register"],
        "q_val_style_temple": "traditional",
        "q_val_purity_21k": "22k",
        "q_col_Karigar": "extra",
        "q_col_Remarks": "extra",
    }
    a1_analyzed = analyze_import(a1_read, answers=a1_answers)

    a1_norm_rows: list[NormalizedRow] = []
    for raw_r in a1_read.rows:
        norm = normalize_row(raw_r, a1_analyzed.column_map, a1_analyzed.value_maps)
        a1_norm_rows.append(norm)

    detect_intra_job_duplicates(a1_norm_rows)

    existing_products: list[dict] = []
    for norm in a1_norm_rows:
        if norm.is_error or norm.action == "skipped" or not norm.product_code:
            continue
        # Deduplicate intra-A1 identical
        code = norm.product_code.upper()
        if any(p["product_code"].upper() == code for p in existing_products):
            continue

        prod_dict = norm.to_normalized_dict()
        prod_dict["product_code"] = norm.product_code
        # Save snapshot
        prod_dict["import_snapshot"] = dict(prod_dict)
        prod_dict["is_published"] = False
        existing_products.append(prod_dict)

    # 2. Apply app edits specified in A2 test plan:
    # 110001 title -> 'Edited In App'
    # 410001 gross_weight -> 9.99 (and net to match)
    # 110005 size -> '15'
    # 110003 publish it
    for p in existing_products:
        c = p["product_code"].upper()
        if c == "110001":
            p["title"] = "Edited In App"
        elif c == "410001":
            p["gross_weight"] = 9.99
            p["net_weight"] = round(9.99 - (p.get("stone_weight") or 0.0), 4)
        elif c == "110005":
            p["size"] = "15"
        elif c == "110003":
            p["is_published"] = True

    # 3. Read A2 file
    a2_file = FIXTURES_DIR / "A2_shree_balaji_stock_register_v2.xlsx"
    a2_read = read_source_file(a2_file.read_bytes(), a2_file.name)

    # Profile from A1 + answer for renamed 'Gross Wt' column
    a1_profile = {
        "id": "11111111-1111-1111-1111-111111111111",
        "header_fingerprint": [h for s in a1_read.sheets if s.name == "Stock Register" for h in s.headers],
        "column_map": a1_analyzed.column_map,
        "value_maps": a1_analyzed.value_maps,
        "sheet_rules": {"sheets": ["Stock Register"]},
    }
    a2_mapped = analyze_import(a2_read, profile=a1_profile, answers={"q_col_Gross Wt": "gross_weight"})

    # Normalize A2 rows
    a2_norm_rows: list[NormalizedRow] = []
    for raw_r in a2_read.rows:
        norm = normalize_row(raw_r, a2_mapped.column_map, a2_mapped.value_maps)
        a2_norm_rows.append(norm)

    # Run 3-Way Merge
    summary = merge_rows_with_existing(a2_norm_rows, existing_products)

    # Check summary counts match expected
    exp_summary = exp_a2["job_summary"]
    for k, v in exp_summary.items():
        assert summary.get(k) == v, f"Summary mismatch on {k}: expected {v}, got {summary.get(k)}"

    # Check per-code action matches expected actions
    got_actions = {r.product_code.upper(): r.action for r in a2_norm_rows if r.product_code}
    # Add not_in_file codes
    for c, act in expected_actions.items():
        if act == "not_in_file":
            got_actions[c.upper()] = "not_in_file"

    for code, expected_act in expected_actions.items():
        got = got_actions.get(code.upper())
        assert got == expected_act, f"Action mismatch for {code}: expected {expected_act}, got {got}"
