"""Chains taxonomy and prompt activation tests; no external AI calls."""
import unittest
from unittest.mock import AsyncMock, patch
from app.jewellery_types import normalize_chain_type
from app.services.chamak import canonicalize_jewellery_type, validate_set_manifest
from app.services.prompt_composer import PromptComposer, DEFAULT_BASE_PROMPT
from app.validation import validate_product_input, validate_jewellery_type_dynamic, ValidationError

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
        with patch("app.services.prompt_composer.prompt_composer.get_valid_jewellery_types", new=AsyncMock(return_value={"chain"})):
            self.assertEqual(await validate_jewellery_type_dynamic("Neck Chains"), "chain")

if __name__ == "__main__": unittest.main()
