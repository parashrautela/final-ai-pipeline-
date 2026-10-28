import re
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from app.config import Settings, parse_openai_image_size, settings
from app.services.chamak import (
    SET_PHOTO_MESSAGE,
    _slider_lines,
    call_openai_vision_analysis,
    compile_chamak_prompt,
    run_stage1_vision_analysis,
    run_stage4_generation_openai,
)
from app.services.chamak_prompt_v3 import (
    DEFAULT_TEMPLATE,
    JEWELLERY_TYPES,
    STYLE_CHIPS,
    compile_chamak_prompt_v3,
    normalize_jewellery_type,
    parse_level,
    resolve_template,
)


def png(opaque):
    out = BytesIO()
    Image.new("RGBA", (4, 4), (200, 160, 40, 255 if opaque else 0)).save(out, "PNG")
    return out.getvalue()


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
        self.assertIn("Use exactly the metal colour of Design 1: the exact colour it has in Image 1.", mixed)

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


class RailwayTemplate(unittest.TestCase):
    def test_blank_variable_means_the_built_in_prompt(self):
        self.assertEqual(resolve_template(""), (DEFAULT_TEMPLATE, None))
        self.assertEqual(resolve_template("   "), (DEFAULT_TEMPLATE, None))

    def test_a_valid_template_is_used_and_labelled(self):
        raw = "MY PROMPT for {{TYPE_1}}\n{{TYPE_MODULE}}\n{{BLEND_DIRECTIONS}}\n{{STYLE_CHOICES}}\n{{WHOLESALER_NOTE}}\nEND"
        template, label = resolve_template(raw)
        added = ("{{WILDNESS}}\n\n", "{{STRUCTURE}}\n\n", "\n\n{{BACKGROUND_MODE}}")
        for slot in added:
            self.assertIn(slot, template)
            template_without = template.replace(slot, "") if slot == added[0] else template_without.replace(slot, "")
        self.assertEqual(template_without, raw)
        self.assertRegex(label, r"^railway-[0-9a-f]{8}$")
        prompt = compile_chamak_prompt_v3({"image1_type": "ring"}, {}, "Thin band", [], template)
        self.assertTrue(prompt.startswith("MY PROMPT for a ring\nRING — COMPLETENESS"))
        self.assertIn("Thin band", prompt)
        self.assertNotIn("{{", prompt)

    def test_a_template_missing_a_required_token_is_not_used(self):
        self.assertEqual(resolve_template("Just make it pretty. {{TYPE_MODULE}}"), (DEFAULT_TEMPLATE, None))
        self.assertEqual(resolve_template("Old style {{jewelry_type}} template"), (DEFAULT_TEMPLATE, None))

    def test_one_line_value_with_literal_newlines_is_unescaped(self):
        raw = "A\\n{{TYPE_MODULE}}\\n{{BLEND_DIRECTIONS}}\\n{{STYLE_CHOICES}}\\n{{WHOLESALER_NOTE}}"
        template, _ = resolve_template(raw)
        self.assertEqual(template.splitlines()[0], "A")

    def test_note_text_is_never_treated_as_a_token(self):
        prompt = build({"image1_type": "ring"}, {}, "keep {{TYPE_1}} literally")
        self.assertIn("keep {{TYPE_1}} literally", prompt)


class StructureAndBackground(unittest.TestCase):
    def test_each_structure_band_is_distinct_and_sits_after_wildness(self):
        names = {0: "KEEP THE SHAPE", 2: "SMALL SHAPE CHANGES", 5: "SOME SHAPE CHANGES", 7: "BIG SHAPE CHANGES", 10: "COMPLETELY NEW SHAPE"}
        for level, name in names.items():
            prompt = compile_chamak_prompt_v3({"image1_type": "ring"}, {}, None, [], DEFAULT_TEMPLATE, 5, level)
            self.assertIn(f"STRUCTURE — LEVEL {level} OF 10 ({name})", prompt)
            self.assertIn("the new piece stays a ring", prompt)
            self.assertLess(prompt.index("CREATIVE FREEDOM"), prompt.index("STRUCTURE — LEVEL"))

    def test_transparent_mode_overrides_every_display(self):
        prompt = compile_chamak_prompt_v3({"image1_type": "necklace"}, {}, None, [], DEFAULT_TEMPLATE, transparent=True)
        self.assertIn("TRANSPARENT BACKGROUND (this overrides BACKGROUND AND DISPLAY MATERIAL", prompt)
        self.assertLess(prompt.index("BACKGROUND AND DISPLAY MATERIAL"), prompt.index("TRANSPARENT BACKGROUND"))
        self.assertNotIn("TRANSPARENT BACKGROUND", build({"image1_type": "necklace"}))


class Wildness(unittest.TestCase):
    def test_unset_wildness_changes_nothing(self):
        self.assertEqual(build({"image1_type": "ring"}), compile_chamak_prompt_v3({"image1_type": "ring"}, {}, None, [], DEFAULT_TEMPLATE, None))
        self.assertNotIn("CREATIVE FREEDOM", build({"image1_type": "ring"}))

    def test_each_band_gives_a_distinct_instruction_and_keeps_the_fixed_rules(self):
        names = {0: "FAITHFUL", 1: "FAITHFUL", 2: "REFINED BLEND", 5: "REINTERPRET", 8: "BOLD NEW DESIGN", 10: "WILD"}
        for level, name in names.items():
            prompt = compile_chamak_prompt_v3({"image1_type": "ring"}, {}, None, [], DEFAULT_TEMPLATE, level)
            self.assertIn(f"CREATIVE FREEDOM — LEVEL {level} OF 10 ({name})", prompt)
            self.assertIn("At every level these stay fixed: the new piece is a ring", prompt)
            self.assertLess(prompt.index("CREATIVE FREEDOM"), prompt.index("DESIGN RESTRAINT"))

    def test_values_are_parsed_and_clamped(self):
        for raw, expected in (("7", 7), (" 3 ", 3), (6.6, 7), ("15", 10), ("-2", 0), (4, 4)):
            self.assertEqual(parse_level(raw), expected, raw)
        for raw in ("", None, "wild", True, float("nan")):
            self.assertIsNone(parse_level(raw), raw)

    def test_a_railway_template_without_a_wildness_slot_gets_one(self):
        raw = "A\n{{TYPE_MODULE}}\n{{BLEND_DIRECTIONS}}\n{{STYLE_CHOICES}}\n{{WHOLESALER_NOTE}}"
        template, label = resolve_template(raw)
        self.assertIn("{{WILDNESS}}\n\n{{STRUCTURE}}\n\n{{BLEND_DIRECTIONS}}", template)
        self.assertEqual(label, resolve_template(raw)[1])
        prompt = compile_chamak_prompt_v3({"image1_type": "ring"}, {}, None, [], template, 9)
        self.assertIn("(WILD)", prompt)


class RailwayOutputSettings(unittest.TestCase):
    def test_resolution_accepts_aliases_and_valid_sizes_only(self):
        self.assertEqual(parse_openai_image_size("2K"), "2048x2048")
        self.assertEqual(parse_openai_image_size(" 1.5k "), "1536x1536")
        self.assertEqual(parse_openai_image_size("1536x1024"), "1536x1024")
        self.assertEqual(parse_openai_image_size("auto"), "auto")
        for bad in ("", "4000x4000", "1000x1000", "3840x3840", "4096x1024", "big"):
            self.assertEqual(parse_openai_image_size(bad), "", bad)

    def test_count_is_clamped_and_typos_fall_back_to_one(self):
        for raw, expected in (("1", 1), ("3", 3), ("9", 4), ("0", 1), ("two", 1), (" 2 ", 2)):
            self.assertEqual(Settings(_env_file=None, CHAMAK_OUTPUT_COUNT=raw).chamak_output_count, expected, raw)

    def test_bad_resolution_falls_back_to_openai_image_size(self):
        s = Settings(_env_file=None, CHAMAK_OUTPUT_RESOLUTION="huge", OPENAI_IMAGE_SIZE="1536x1536")
        self.assertEqual(s.chamak_output_size, "1536x1536")
        self.assertEqual(Settings(_env_file=None, CHAMAK_OUTPUT_RESOLUTION="1K").chamak_output_size, "1024x1024")


class WorkerUsesRailwaySettings(unittest.IsolatedAsyncioTestCase):
    async def test_count_size_and_template_label_reach_the_render(self):
        row = {"wholesaler_id": "w1", "source_image_1_url": "one", "source_image_2_url": "two",
               "stage1_analysis_json": {"image1_type": "ring"}}
        template = "T {{TYPE_MODULE}} {{BLEND_DIRECTIONS}} {{STYLE_CHOICES}} {{WHOLESALER_NOTE}}"
        fuse = AsyncMock(side_effect=[b"a", RuntimeError("one render failed"), b"c"])
        uploads = [SimpleNamespace(url=f"w1/job/{n}.png", variants={}) for n in range(2)]
        with patch.object(settings, "CHAMAK_OUTPUT_COUNT", "3"), \
             patch.object(settings, "CHAMAK_OUTPUT_RESOLUTION", "1K"), \
             patch.object(settings, "CHAMAK_BASE_PROMPT_TEMPLATE", template), \
             patch.object(settings, "CHAMAK_WILDNESS", "7"), \
             patch("app.services.chamak.get_feature_status", return_value=(False, None)), \
             patch("app.services.chamak.fetch_chamak_generation", AsyncMock(return_value=row)), \
             patch("app.services.chamak.fetch_image_bytes_and_content_type", AsyncMock(return_value=(b"i", "image/jpeg"))), \
             patch("app.services.chamak.openai_image_client.fuse_images", fuse), \
             patch("app.services.chamak._upload_attempt", side_effect=uploads), \
             patch("app.services.chamak.update_chamak_generation", AsyncMock()) as update, \
             patch("app.services.chamak._refund_failed_generation", AsyncMock()) as refund:
            await run_stage4_generation_openai("job")
        self.assertEqual(fuse.await_count, 3)
        self.assertEqual({c.kwargs["size"] for c in fuse.await_args_list}, {"1024x1024"})
        self.assertTrue(fuse.await_args.kwargs["prompt"].startswith("T RING — COMPLETENESS"))
        self.assertIn("LEVEL 7 OF 10 (BOLD NEW DESIGN)", fuse.await_args.kwargs["prompt"])
        history = [c.args[1]["output_history"] for c in update.await_args_list if "output_history" in c.args[1]][-1]
        self.assertEqual([a["attempt"] for a in history], [1, 2])
        final = update.await_args.args[1]
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["output_image_url"], "w1/job/1.png")
        self.assertRegex(final["prompt_version"], r"^v3\.0-chamak-openai\+w7\+railway-[0-9a-f]{8}$")
        refund.assert_not_awaited()

    async def run_worker(self, form, fuse, extra_patches=()):
        row = {"wholesaler_id": "w1", "source_image_1_url": "one", "source_image_2_url": "two",
               "stage1_analysis_json": {"image1_type": "ring", "image1_strengths": ["Flower pattern"]},
               "wholesaler_form_json": form}
        with patch.object(settings, "CHAMAK_OUTPUT_COUNT", "1"), \
             patch.object(settings, "CHAMAK_BASE_PROMPT_TEMPLATE", ""), \
             patch.object(settings, "CHAMAK_WILDNESS", ""), \
             patch.object(settings, "CHAMAK_STRUCTURE", "2"), \
             patch("app.services.chamak.get_feature_status", return_value=(False, None)), \
             patch("app.services.chamak.fetch_chamak_generation", AsyncMock(return_value=row)), \
             patch("app.services.chamak.fetch_image_bytes_and_content_type", AsyncMock(return_value=(b"i", "image/jpeg"))), \
             patch("app.services.chamak.openai_image_client.fuse_images", fuse), \
             patch("app.services.chamak._upload_attempt", return_value=SimpleNamespace(url="w1/job/0.png", variants={})), \
             patch("app.services.chamak.update_chamak_generation", AsyncMock()) as update, \
             patch("app.services.chamak._refund_failed_generation", AsyncMock()):
            await run_stage4_generation_openai("job")
        history = [c.args[1]["output_history"] for c in update.await_args_list if "output_history" in c.args[1]][-1]
        return update.await_args.args[1], history[-1]

    async def test_every_attempt_saves_the_settings_that_made_it(self):
        form = {"slider_weights": {"attr_0": 0.7}, "attribute_context": [{"id": "attr_0", "attribute": "Flower pattern"}],
                "wildness": 6, "style_chips": ["matte"]}
        final, attempt = await self.run_worker(form, AsyncMock(return_value=png(opaque=True)))
        self.assertEqual(attempt["settings"], {
            "mix": [{"name": "Flower pattern", "design_2_percent": 70}], "wildness": 6, "structure": 2,
            "background": "kept", "style_chips": ["matte"], "note": None,
        })
        self.assertEqual(final["prompt_version"], "v3.0-chamak-openai+w6+s2")

    async def test_remove_background_asks_for_a_transparent_png_and_checks_it(self):
        fuse = AsyncMock(return_value=png(opaque=False))
        final, attempt = await self.run_worker({"background": "transparent", "structure": 0}, fuse)
        self.assertEqual(fuse.await_args.kwargs["background"], "transparent")
        self.assertIn("TRANSPARENT BACKGROUND", fuse.await_args.kwargs["prompt"])
        self.assertIn("LEVEL 0 OF 10 (KEEP THE SHAPE)", fuse.await_args.kwargs["prompt"])
        self.assertEqual(attempt["settings"]["background"], "removed")
        self.assertTrue(final["prompt_version"].endswith("+s0+bg-t"))

    async def test_an_opaque_or_refused_cut_out_is_recorded_honestly(self):
        _, attempt = await self.run_worker({"background": "transparent"}, AsyncMock(return_value=png(opaque=True)))
        self.assertEqual(attempt["settings"]["background"], "not_removed")
        refusal = httpx.HTTPStatusError("bad", request=httpx.Request("POST", "https://x"), response=httpx.Response(400))
        fuse = AsyncMock(side_effect=[refusal, png(opaque=True)])
        final, attempt = await self.run_worker({"background": "transparent"}, fuse)
        self.assertEqual(fuse.await_count, 2)
        self.assertNotIn("background", fuse.await_args.kwargs)
        self.assertEqual(final["status"], "done")
        self.assertEqual(attempt["settings"]["background"], "not_removed")

    async def test_all_renders_failing_still_fails_and_refunds(self):
        row = {"wholesaler_id": "w1", "source_image_1_url": "one", "source_image_2_url": "two"}
        with patch.object(settings, "CHAMAK_OUTPUT_COUNT", "2"), \
             patch("app.services.chamak.get_feature_status", return_value=(False, None)), \
             patch("app.services.chamak.fetch_chamak_generation", AsyncMock(return_value=row)), \
             patch("app.services.chamak.fetch_image_bytes_and_content_type", AsyncMock(return_value=(b"i", "image/jpeg"))), \
             patch("app.services.chamak.openai_image_client.fuse_images", AsyncMock(side_effect=RuntimeError("down"))), \
             patch("app.services.chamak.update_chamak_generation", AsyncMock()) as update, \
             patch("app.services.chamak._refund_failed_generation", AsyncMock()) as refund:
            await run_stage4_generation_openai("job")
        self.assertEqual(update.await_args.args[1]["status"], "failed")
        refund.assert_awaited_once()


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

    async def test_analysis_carries_the_railway_default_wildness_for_the_app(self):
        with patch.object(settings, "CHAMAK_WILDNESS", "6"), patch.object(settings, "CHAMAK_STRUCTURE", "1"):
            fields, _ = await self.run_stage1({"content_flag": "ok"})
        self.assertEqual(fields["stage1_analysis_json"]["default_wildness"], 6)
        self.assertEqual(fields["stage1_analysis_json"]["default_structure"], 1)
        with patch.object(settings, "CHAMAK_WILDNESS", ""):
            fields, _ = await self.run_stage1({"content_flag": "ok"})
        self.assertIsNone(fields["stage1_analysis_json"]["default_wildness"])

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
