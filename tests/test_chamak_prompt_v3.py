import re
import unittest
from unittest.mock import AsyncMock, patch

from app.services.chamak import (
    SET_PHOTO_MESSAGE,
    _slider_lines,
    call_openai_vision_analysis,
    compile_chamak_prompt,
    run_stage1_vision_analysis,
)
from app.services.chamak_prompt_v3 import (
    JEWELLERY_TYPES,
    STYLE_CHIPS,
    compile_chamak_prompt_v3,
    normalize_jewellery_type,
)


def build(analysis, form=None, note=None):
    form = form or {}
    return compile_chamak_prompt_v3(analysis, form, note, _slider_lines(analysis, form))


class PromptV3(unittest.TestCase):
    def test_every_type_fills_every_placeholder(self):
        for jewellery_type in JEWELLERY_TYPES:
            for other in ("necklace", "earrings", None):
                prompt = build({"image1_type": jewellery_type, "image2_type": other, "type_mismatch": other is None})
                self.assertIsNone(re.search(r"\{[a-z_0-9]+\}", prompt), (jewellery_type, other))

    def test_necklace_with_jhumka_ideas_translates_and_uses_neck_form(self):
        prompt = build({"image1_type": "necklace", "image2_type": "jhumka"})
        self.assertIn("Make exactly one new necklace.", prompt)
        self.assertIn("Design 2 is a pair of jhumka earrings, but the new piece must be a necklace.", prompt)
        self.assertIn("The form ENDS AT THE NECK", prompt)
        self.assertNotIn("EARRINGS — ONE MATCHING PAIR", prompt)

    def test_two_earrings_make_one_pair_on_a_stand_without_cross_type(self):
        prompt = build({"image1_type": "earrings", "image2_type": "jhumka"})
        self.assertIn("Make exactly one new pair of earrings.", prompt)
        self.assertIn("EARRINGS — ONE MATCHING PAIR", prompt)
        self.assertIn("velvet earring display stand", prompt)
        self.assertNotIn("TRANSLATING IDEAS", prompt)
        self.assertNotIn("DISPLAY NECK FORM", prompt)

    def test_rows_analysed_before_v3_still_compile(self):
        same = build({"jewelry_type": "necklace"})
        self.assertIn("Make exactly one new necklace.", same)
        self.assertNotIn("TRANSLATING IDEAS", same)
        mixed = build({"jewelry_type": "ring", "type_mismatch": True})
        self.assertIn("Design 2 is a different kind of jewellery, but the new piece must be a ring.", mixed)
        self.assertIn("Use exactly the metal colour of Design 1, exactly as it appears in Image 1.", mixed)

    def test_slider_lines_match_the_original_prompt_exactly(self):
        analysis = {"image1_strengths": ["floral dome"], "image2_weaknesses": ["flat disc"]}
        form = {"slider_weights": {"attr_0": 0.75}}
        line = _slider_lines(analysis, form)[0]
        self.assertIn(line, compile_chamak_prompt(analysis, form, None))
        self.assertIn(line, build(analysis, form))
        self.assertIn("25% Design 1 (floral dome); 75% Design 2 (flat disc).", line)

    def test_pick_one_chip_groups_keep_the_first_and_craft_allows_several(self):
        prompt = build({"image1_type": "ring"}, {"style_chips": ["matte", "high_polish", "kundan", "filigree", "rose_gold", "bogus"]})
        self.assertIn(STYLE_CHIPS["matte"][1], prompt)
        self.assertNotIn(STYLE_CHIPS["high_polish"][1], prompt)
        self.assertIn(STYLE_CHIPS["kundan"][1], prompt)
        self.assertIn(STYLE_CHIPS["filigree"][1], prompt)
        self.assertIn(STYLE_CHIPS["rose_gold"][1], prompt)
        self.assertNotIn("FINISH AND STYLE CHOICES", build({"image1_type": "ring"}))

    def test_user_text_with_braces_is_passed_through_untouched(self):
        prompt = build({"image1_type": "ring"}, {"slider_weights": {"{shape}": 0.5}}, "Add {stones} on top")
        self.assertIn("Add {stones} on top", prompt)
        self.assertIn("{shape}: 50% Design 1", prompt)

    def test_worst_case_stays_well_under_the_vendor_limit(self):
        analysis = {
            "image1_type": "necklace", "image2_type": "earrings",
            "image1_strengths": ["x" * 60] * 5, "image2_strengths": ["y" * 60] * 5,
            "image2_weaknesses": ["z" * 60] * 5, "image1_summary": "s" * 300,
            "image2_summary": "s" * 300, "image1_metal": "m" * 120,
        }
        form = {"slider_weights": {f"attr_{i}": 0.5 for i in range(6)}, "style_chips": list(STYLE_CHIPS)}
        self.assertLess(len(build(analysis, form, "n" * 5000)), 26000)

    def test_type_names_from_the_analysis_are_normalised(self):
        self.assertEqual(normalize_jewellery_type("Jhumkas"), "jhumka")
        self.assertEqual(normalize_jewellery_type("rani haar"), "long_necklace")
        self.assertEqual(normalize_jewellery_type("brooch"), "other")
        self.assertIsNone(normalize_jewellery_type(""))


class SetPhotoRejection(unittest.IsolatedAsyncioTestCase):
    async def run_stage1(self, analysis):
        row = {"source_image_1_url": "one", "source_image_2_url": "two"}
        with patch("app.services.chamak.fetch_chamak_generation", AsyncMock(return_value=row)), \
             patch("app.services.chamak.fetch_image_bytes_and_content_type", AsyncMock(return_value=(b"i", "image/jpeg"))), \
             patch("app.services.chamak.call_openai_vision_analysis", AsyncMock(return_value=analysis)), \
             patch("app.services.chamak.update_chamak_generation", AsyncMock()) as update, \
             patch("app.services.chamak._refund_failed_generation", AsyncMock()) as refund:
            await run_stage1_vision_analysis("job", {"charged": 10})
        return update.call_args.args[1], refund

    async def test_a_set_photo_fails_with_a_clear_message_and_a_refund(self):
        fields, refund = await self.run_stage1({"content_flag": "ok", "image2_is_set": True})
        self.assertEqual(fields["status"], "failed")
        self.assertEqual(fields["error_message"], SET_PHOTO_MESSAGE)
        self.assertEqual(fields["content_flag_hit"], "ok")
        self.assertEqual(refund.call_args.args[1], "Set photo rejected")

    async def test_single_pieces_go_through(self):
        fields, refund = await self.run_stage1({"content_flag": "ok", "image1_is_set": False, "image2_is_set": False})
        self.assertEqual(fields["status"], "awaiting_input")
        self.assertNotIn("error_message", fields)
        refund.assert_not_awaited()

    async def test_analysis_returns_the_new_fields(self):
        payload = ('{"content_flag":"ok","jewelry_type":"necklace","image1_type":"Necklace","image2_type":"jhumkas",'
                   '"image1_is_set":false,"image2_is_set":"yes","image1_summary":"A gold choker.","image1_metal":"22K yellow"}')
        response = AsyncMock()
        response.json = lambda: {"choices": [{"message": {"content": payload}}]}
        with patch("app.services.chamak.settings.OPENAI_API_KEY", "key"), \
             patch("app.services.chamak._request_with_retry", AsyncMock(return_value=response)):
            result = await call_openai_vision_analysis(b"1", "image/jpeg", b"2", "image/jpeg")
        self.assertEqual(result["image1_type"], "necklace")
        self.assertEqual(result["image2_type"], "jhumka")
        self.assertFalse(result["image2_is_set"])  # only a real true counts
        self.assertEqual(result["image1_summary"], "A gold choker.")
        self.assertEqual(result["image2_summary"], "")
        self.assertEqual(result["jewelry_type"], "necklace")


if __name__ == "__main__":
    unittest.main()
