"""Chains taxonomy and prompt activation tests; no external AI calls."""
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from app.jewellery_types import normalize_chain_type
from app.services.chamak import canonicalize_jewellery_type, validate_set_manifest
from app.services.prompt_composer import PromptComposer, DEFAULT_BASE_PROMPT
from app.validation import validate_product_input, validate_jewellery_type_dynamic, ValidationError
from app.services.prompt_composer import ComposedPromptResult
from app.services.storage import StoredImage

class ChainsTests(unittest.IsolatedAsyncioTestCase):
    def test_aliases_and_set_duplicates(self):
        for alias in ("chain", "chains", "Neck Chain", " neck chains "):
            self.assertEqual(normalize_chain_type(alias), "chain")
            self.assertEqual(canonicalize_jewellery_type(alias), "chain")
            self.assertEqual(validate_product_input(jewellery_type=alias).jewellery_type, "chain")
        self.assertEqual(validate_set_manifest([{"jewellery_type":"chains"}, {"jewellery_type":"pendant"}]), ["chain", "pendant"])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            validate_set_manifest([{"jewellery_type":"chain"}, {"jewellery_type":"neck chains"}])

    async def test_generation_requires_active_chain_module(self):
        with patch("app.services.prompt_composer.fetch_active_prompt_modules", new=AsyncMock(return_value=[])):
            with self.assertRaisesRegex(ValueError, "chain prompt"):
                await PromptComposer().get_composed_prompt("chains")
        with patch("app.services.prompt_composer.prompt_composer.get_valid_jewellery_types", new=AsyncMock(return_value={"other"})):
            with self.assertRaisesRegex(ValidationError, "chain prompt"):
                await validate_jewellery_type_dynamic("chain")

    async def test_active_module_alias_routes_to_chain(self):
        modules = [dict(id="base",module_type="base",prompt_text=DEFAULT_BASE_PROMPT,version=1),
                   dict(id="chain",module_type="category",jewellery_type="chains",prompt_text="TEST CHAIN MODULE",version=3)]
        with patch("app.services.prompt_composer.fetch_active_prompt_modules", new=AsyncMock(return_value=modules)):
            composer = PromptComposer()
            self.assertIn("chain", await composer.get_valid_jewellery_types())
            result = await composer.get_composed_prompt("neck chains")
            self.assertEqual(result.jewellery_type_matched, "chain")
            self.assertEqual(result.category_module_version, 3)
            self.assertIn("TEST CHAIN MODULE", result.composed_prompt)
            self.assertIsNone(result.variant_scenes)
        with patch("app.services.prompt_composer.prompt_composer.get_valid_jewellery_types", new=AsyncMock(return_value={"chain"})):
            self.assertEqual(await validate_jewellery_type_dynamic("Neck Chains"), "chain")

    async def test_empty_chain_module_does_not_enable_generation(self):
        modules = [dict(id="chain", module_type="category", jewellery_type="chain",
                        prompt_text="   ", version=1)]
        with patch("app.services.prompt_composer.fetch_active_prompt_modules", new=AsyncMock(return_value=modules)):
            composer = PromptComposer()
            self.assertNotIn("chain", await composer.get_valid_jewellery_types())
            with self.assertRaisesRegex(ValueError, "chain prompt"):
                await composer.get_composed_prompt("chains")

    async def test_requested_image_count_uses_first_scenes_in_order(self):
        from app.services.pipeline import process_product_image, VARIANT_SCENE_SETTINGS
        scenes = [f"SCENE {i} — CHAIN CUSTOM SCENE {i}" for i in range(1, 5)]
        composed = ComposedPromptResult("GLOBAL RULES\n\nCHAIN RULES", 1, 1, "chain", "chain", scenes)

        async def generate(**kwargs):
            return StoredImage(f"https://example.test/scene-{kwargs['variant_index']}.png", {})

        for count in range(1, 5):
            with self.subTest(count=count), \
                 patch("app.services.pipeline.settings.TEST_MODE", False), \
                 patch("app.services.pipeline.prompt_composer.get_composed_prompt", new=AsyncMock(return_value=composed)), \
                 patch("app.services.pipeline._generate_variant", new=AsyncMock(side_effect=generate)) as mock_generate, \
                 patch("app.services.pipeline.update_product_generated_images", new=AsyncMock()):
                urls = await process_product_image(
                    {"id": "chain-test", "jewellery_type": "chain", "raw_image_url": "https://example.test/raw.png"},
                    image_count=count,
                )
                self.assertEqual(urls, [f"https://example.test/scene-{i}.png" for i in range(1, count + 1)])
                self.assertEqual(mock_generate.await_count, count)
                calls = sorted(mock_generate.await_args_list, key=lambda call: call.kwargs["variant_index"])
                self.assertEqual([call.kwargs["prompt"] for call in calls],
                                 [f"{composed.composed_prompt}\n\n{scene}" for scene in scenes[:count]])

    async def test_database_chain_scenes_and_budget(self):
        folder = Path(__file__).parent / "docs" / "prompts"
        modules = [dict(id="base", module_type="base", prompt_text=(folder / "global-base-v2.txt").read_text(), version=2),
                   dict(id="chain", module_type="category", jewellery_type="chain", prompt_text=(folder / "chain-v2.txt").read_text(), version=2)]
        with patch("app.services.prompt_composer.fetch_active_prompt_modules", new=AsyncMock(return_value=modules)):
            result = await PromptComposer().get_composed_prompt("neck chains", item_description="X" * 240)
        self.assertEqual(len(result.variant_scenes), 4)
        self.assertNotIn("SCENE 1", result.composed_prompt)
        for index, scene in enumerate(result.variant_scenes, 1):
            self.assertTrue(scene.startswith(f"SCENE {index} — "))
            self.assertLessEqual(len(result.composed_prompt + "\n\n" + scene), 5000)

    async def test_bad_scene_order_rejected(self):
        module = dict(id="chain", module_type="category", jewellery_type="chain", version=2,
                      prompt_text="CHAIN RULES\n\nSCENE 2 — WRONG ORDER")
        with patch("app.services.prompt_composer.fetch_active_prompt_modules", new=AsyncMock(return_value=[module])):
            with self.assertRaisesRegex(ValueError, "four ordered scenes"):
                await PromptComposer().get_composed_prompt("chain")

    async def test_over_budget_scenes_rejected_before_paid_generation(self):
        from app.services.pipeline import process_product_image
        composed = ComposedPromptResult("X" * 5000, 2, 2, "chain", "chain", ["SCENE 1 — DETAIL"] * 4)
        with patch("app.services.pipeline.prompt_composer.get_composed_prompt", new=AsyncMock(return_value=composed)), \
             patch("app.services.pipeline._generate_variant", new=AsyncMock()) as generate:
            with self.assertRaisesRegex(ValueError, "character limit"):
                await process_product_image({"id": "chain-test", "jewellery_type": "chain", "raw_image_url": "https://example.test/raw.png"}, image_count=2)
            generate.assert_not_awaited()

if __name__ == "__main__": unittest.main()
