"""tests/catalogue_import/test_mapper.py

Phase 3 gate tests for mapper service:
Tests profile matching, synonym dictionary, questions generation for ambiguous
columns and unsupported values, questions limits, and LLM disabled mode.
"""

from pathlib import Path
import pytest

from app.config import settings
from app.services.catalogue_import.reader import read_source_file
from app.services.catalogue_import.mapper import analyze_import

FIXTURES_DIR = Path(__file__).resolve().parents[4] / "plans" / "bulk-catalogue-import" / "test-data" / "fixtures"


def test_a1_mapper():
    """A1 asks <= 6 questions incl. sheet / Temple / 21K / Karigar / Remarks."""
    file_path = FIXTURES_DIR / "A1_shree_balaji_stock_register.xlsx"
    data = file_path.read_bytes()
    read_res = read_source_file(data, file_path.name)

    map_res = analyze_import(read_res)

    # Must ask <= 6 questions
    assert len(map_res.questions) <= 6

    q_texts = " ".join(q.text.lower() for q in map_res.questions)
    q_ids = [q.id for q in map_res.questions]

    # Verify questions about Temple, 21K, Karigar or Remarks
    assert any("temple" in q_id.lower() or "temple" in q_texts for q_id in q_ids)
    assert any("21k" in q_id.lower() or "21k" in q_texts for q_id in q_ids)

    # Verify auto-mapped columns
    assert map_res.column_map.get("Design No.") == "product_code"
    assert map_res.column_map.get("Item") == "jewellery_type"
    assert map_res.column_map.get("Metal") == "category"
    assert map_res.column_map.get("G.Wt (gm)") == "gross_weight"
    assert map_res.column_map.get("N.Wt") == "net_weight"


def test_b_hinglish_mapper():
    """B maps every Hinglish column with <= 4 questions."""
    file_path = FIXTURES_DIR / "B_rajesh_codes_export.csv"
    data = file_path.read_bytes()
    read_res = read_source_file(data, file_path.name)

    map_res = analyze_import(read_res)

    # Must map Hinglish columns
    assert map_res.column_map.get("Design Code") == "product_code"
    assert map_res.column_map.get("Prakar") == "jewellery_type"
    assert map_res.column_map.get("Dhatu") == "category"
    assert map_res.column_map.get("Shuddhata") == "metal_purity"
    assert map_res.column_map.get("Kul Vajan (gm)") == "gross_weight"
    assert map_res.column_map.get("Nag Vajan") == "stone_weight"
    assert map_res.column_map.get("Shuddh Vajan") == "net_weight"
    assert map_res.column_map.get("Photo Link") == "photo"
    assert map_res.column_map.get("Stock") == "stock_available"
    assert map_res.column_map.get("Banwai Din") == "make_to_order_days"

    # Hinglish mapping must ask <= 4 questions (in fact, ideally 0)
    assert len(map_res.questions) <= 4


def test_c_ambiguous_wt_and_rate_mapper():
    """C must ask about 'Wt' and 'Rate' (never silently assign them)."""
    file_path = FIXTURES_DIR / "C_legacy_billing_export.xls"
    data = file_path.read_bytes()
    read_res = read_source_file(data, file_path.name)

    map_res = analyze_import(read_res)

    # 'Wt' and 'Rate' must NOT be silently assigned
    assert "Wt" not in map_res.column_map
    assert "Rate" not in map_res.column_map

    q_ids = [q.id for q in map_res.questions]
    q_texts = " ".join(q.text.lower() for q in map_res.questions)

    # Questions MUST be asked about Wt and Rate
    assert any("wt" in q_id.lower() or "wt" in q_texts for q_id in q_ids)
    assert any("rate" in q_id.lower() or "rate" in q_texts for q_id in q_ids)

    # Unsupported type question for Anklet/Payal
    assert any("anklet" in q_id.lower() or "payal" in q_id.lower() or "anklet" in q_texts for q_id in q_ids)

    # Total questions <= 6
    assert len(map_res.questions) <= 6


def test_a2_reusing_a1_profile():
    """A2 asks <= 1 question when reusing profile from A1."""
    # Build saved profile from A1 answers
    a1_file = FIXTURES_DIR / "A1_shree_balaji_stock_register.xlsx"
    a1_res = read_source_file(a1_file.read_bytes(), a1_file.name)
    a1_headers = [h for s in a1_res.sheets if s.name == "Stock Register" for h in s.headers]

    profile = {
        "id": "11111111-1111-1111-1111-111111111111",
        "header_fingerprint": a1_headers,
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
            "style": {"temple": "traditional"},
            "metal_purity": {"21k": "22k"},
        },
        "sheet_rules": {"sheets": ["Stock Register"]},
    }

    a2_file = FIXTURES_DIR / "A2_shree_balaji_stock_register_v2.xlsx"
    a2_res = read_source_file(a2_file.read_bytes(), a2_file.name)

    a2_map = analyze_import(a2_res, profile=profile)

    # Profile reused -> asks <= 1 question
    assert len(a2_map.questions) <= 1
    assert a2_map.column_map.get("Design No.") == "product_code"


def test_b_ods_after_b_csv_profile():
    """B .ods after B .csv asks 0 questions when profile is saved."""
    b_csv = FIXTURES_DIR / "B_rajesh_codes_export.csv"
    b_res = read_source_file(b_csv.read_bytes(), b_csv.name)
    b_map = analyze_import(b_res)

    saved_profile = {
        "id": "22222222-2222-2222-2222-222222222222",
        "header_fingerprint": b_res.sheets[0].headers,
        "column_map": b_map.column_map,
        "value_maps": b_map.value_maps,
        "sheet_rules": {},
    }

    b_ods = FIXTURES_DIR / "B_rajesh_codes_export.ods"
    b_ods_res = read_source_file(b_ods.read_bytes(), b_ods.name)
    b_ods_map = analyze_import(b_ods_res, profile=saved_profile)

    # Asks 0 questions
    assert len(b_ods_map.questions) == 0
    assert b_ods_map.is_ready is True


def test_llm_disabled_mode(monkeypatch):
    """With LLM disabled (env flag), all fixtures still complete without crashing."""
    monkeypatch.setattr(settings, "CATALOGUE_IMPORT_LLM_ENABLED", False)

    for f_name in ["A1_shree_balaji_stock_register.xlsx", "B_rajesh_codes_export.csv", "C_legacy_billing_export.xls"]:
        p = FIXTURES_DIR / f_name
        res = read_source_file(p.read_bytes(), p.name)
        m = analyze_import(res)
        assert m.llm_calls == 0
        assert isinstance(m.questions, list)


def test_answers_value_maps_purity():
    """Verify that q_val_purity_21k maps to value_maps['metal_purity']['21k']."""
    file_path = FIXTURES_DIR / "A1_shree_balaji_stock_register.xlsx"
    data = file_path.read_bytes()
    read_res = read_source_file(data, file_path.name)

    answers = {
        "q_val_purity_21k": "22k",
        "q_val_style_temple": "traditional",
    }
    map_res = analyze_import(read_res, answers=answers)
    assert map_res.value_maps.get("metal_purity", {}).get("21k") == "22k"
    assert map_res.value_maps.get("style", {}).get("temple") == "traditional"

