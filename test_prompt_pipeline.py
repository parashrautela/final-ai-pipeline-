import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.db.repository import (
    fetch_active_prompt_modules,
    log_ai_generation_complete,
    log_ai_generation_start,
)
from app.services.prompt_composer import (
    DEFAULT_BASE_PROMPT,
    DEFAULT_CATEGORY_PROMPTS,
    PromptComposer,
    PromptModuleItem,
    prompt_composer,
)
from app.validation import (
    ValidationError,
    validate_jewellery_type_dynamic,
    validate_product_input,
)


class TestPromptComposer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.composer = PromptComposer(ttl=60.0)

    async def test_compose_prompt_jhumka(self):
        result = await self.composer.get_composed_prompt(
            jewellery_type="jhumka",
            item_description="Gold Polki Jhumka",
        )
        self.assertEqual(result.jewellery_type_matched, "jhumka")
        self.assertEqual(result.base_module_version, 1)
        self.assertEqual(result.category_module_version, 1)
        self.assertIn("Gold Polki Jhumka", result.composed_prompt)
        self.assertIn("CATEGORY RULES — JHUMKA", result.composed_prompt)
        self.assertIn("STRICT HIGH-END JEWELLERY COMPOSITING TASK", result.composed_prompt)

    async def test_compose_prompt_mangalsutra(self):
        result = await self.composer.get_composed_prompt(
            jewellery_type="mangalsutra",
            item_description="Traditional 22k Mangalsutra",
        )
        self.assertEqual(result.jewellery_type_matched, "mangalsutra")
        self.assertIn("Traditional 22k Mangalsutra", result.composed_prompt)
        self.assertIn("CATEGORY RULES — MANGALSUTRA", result.composed_prompt)
        self.assertIn("black-and-gold auspicious bead chain", result.composed_prompt)

    async def test_compose_prompt_fallback_to_other(self):
        result = await self.composer.get_composed_prompt(
            jewellery_type="unknown_custom_piece",
            item_description="Custom Brooch",
        )
        self.assertEqual(result.jewellery_type_matched, "other")
        self.assertEqual(result.jewellery_type_requested, "unknown_custom_piece")
        self.assertIn("CATEGORY RULES — GENERAL JEWELLERY FALLBACK", result.composed_prompt)

    async def test_dynamic_db_modules_and_caching(self):
        mock_db_modules = [
            {
                "id": "base-uuid",
                "module_type": "base",
                "jewellery_type": None,
                "prompt_text": "DYNAMIC BASE PROMPT: {item_description}.",
                "version": 4,
                "is_active": True,
            },
            {
                "id": "jhumka-uuid",
                "module_type": "category",
                "jewellery_type": "jhumka",
                "prompt_text": "DYNAMIC JHUMKA PROMPT v2.",
                "version": 2,
                "is_active": True,
            },
            {
                "id": "nosepin-uuid",
                "module_type": "category",
                "jewellery_type": "nosepin",
                "prompt_text": "DYNAMIC NOSEPIN PROMPT v1.",
                "version": 1,
                "is_active": True,
            },
        ]

        with patch("app.services.prompt_composer.fetch_active_prompt_modules", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_db_modules
            composer = PromptComposer(ttl=60.0)

            # First call fetches from DB
            valid_types = await composer.get_valid_jewellery_types()
            self.assertIn("jhumka", valid_types)
            self.assertIn("nosepin", valid_types)
            self.assertIn("other", valid_types)
            self.assertEqual(mock_fetch.call_count, 1)

            # Second call uses cache (no DB fetch)
            res = await composer.get_composed_prompt("nosepin", item_description="Diamond Nosepin")
            self.assertEqual(mock_fetch.call_count, 1)
            self.assertEqual(res.base_module_version, 4)
            self.assertEqual(res.category_module_version, 1)
            self.assertIn("DYNAMIC BASE PROMPT: Diamond Nosepin.", res.composed_prompt)
            self.assertIn("DYNAMIC NOSEPIN PROMPT v1.", res.composed_prompt)

            # Invalidate cache forces reload
            composer.invalidate_cache()
            await composer.get_valid_jewellery_types()
            self.assertEqual(mock_fetch.call_count, 2)


class TestDynamicValidation(unittest.IsolatedAsyncioTestCase):
    async def test_validate_jewellery_type_dynamic_valid(self):
        with patch.object(prompt_composer, "get_valid_jewellery_types", new_callable=AsyncMock) as mock_types:
            mock_types.return_value = {"jhumka", "mangalsutra", "necklace", "ring", "bangle", "other"}
            val = await validate_jewellery_type_dynamic("JHUMKA")
            self.assertEqual(val, "jhumka")

            val2 = await validate_jewellery_type_dynamic("  mangalsutra  ")
            self.assertEqual(val2, "mangalsutra")

    async def test_validate_jewellery_type_dynamic_invalid(self):
        with patch.object(prompt_composer, "get_valid_jewellery_types", new_callable=AsyncMock) as mock_types:
            mock_types.return_value = {"jhumka", "mangalsutra", "necklace", "ring", "bangle", "other"}
            with self.assertRaises(ValidationError) as ctx:
                await validate_jewellery_type_dynamic("shoes")
            self.assertIn("Invalid jewellery_type 'shoes'", str(ctx.exception))

            with self.assertRaises(ValidationError):
                await validate_jewellery_type_dynamic("")

    def test_validate_product_input_requires_jewellery_type(self):
        with self.assertRaises(ValidationError):
            validate_product_input(title="Gold Pendant", jewellery_type="")

        with self.assertRaises(ValidationError):
            validate_product_input(title="Gold Pendant", jewellery_type=None)

        prod = validate_product_input(title="Gold Jhumka", jewellery_type="jhumka")
        self.assertEqual(prod.title, "Gold Jhumka")
        self.assertEqual(prod.jewellery_type, "jhumka")


class TestAiGenerationLogsRepository(unittest.IsolatedAsyncioTestCase):
    @patch("app.db.repository.get_supabase")
    async def test_log_ai_generation_start_and_complete(self, mock_get_supabase):
        mock_table = MagicMock()
        mock_insert = MagicMock()
        mock_update = MagicMock()

        mock_insert.execute.return_value.data = [{"id": "log-1234-uuid"}]
        mock_update.execute.return_value.data = [{"id": "log-1234-uuid"}]

        mock_table.insert.return_value = mock_insert
        mock_table.update.return_value = mock_update
        mock_table.update.return_value.eq.return_value = mock_update

        mock_client = MagicMock()
        mock_client.table.return_value = mock_table
        mock_get_supabase.return_value = mock_client

        log_id = await log_ai_generation_start(
            product_id="prod-uuid-1",
            jewellery_type="jhumka",
            composed_prompt="Sample composed prompt",
            base_module_version=1,
            category_module_version=2,
        )

        self.assertEqual(log_id, "log-1234-uuid")
        mock_client.table.assert_called_with("ai_generation_logs")

        await log_ai_generation_complete(log_id, status="success")
        mock_table.update.assert_called()


class TestPipelinePromptIntegration(unittest.IsolatedAsyncioTestCase):
    @patch("app.services.pipeline.upload_processed_image_variant", return_value="https://storage.com/processed_v1.png")
    @patch("app.services.pipeline.upload_file_to_storage", return_value="https://storage.com/reve.png")
    @patch("app.services.pipeline.resolve_product_image", return_value=b"fake_raw_bytes")
    @patch("app.services.pipeline.reve_client.remove_background", new_callable=AsyncMock)
    @patch("app.services.pipeline.nanobana_client.enhance_image", new_callable=AsyncMock)
    @patch("app.services.pipeline.update_product_generated_images", new_callable=AsyncMock)
    @patch("app.services.pipeline.log_ai_generation_start", new_callable=AsyncMock)
    @patch("app.services.pipeline.log_ai_generation_complete", new_callable=AsyncMock)
    async def test_process_product_image_logs_composed_prompts(
        self,
        mock_log_complete,
        mock_log_start,
        mock_update_images,
        mock_nanobana,
        mock_reve,
        mock_resolve,
        mock_upload_storage,
        mock_upload_variant,
    ):
        mock_reve.return_value = b"fake_reve_bytes"
        mock_nanobana.return_value = b"fake_nanobana_bytes"
        mock_log_start.return_value = "log-id-test"

        from app.services.pipeline import process_product_image

        product = {
            "id": str(uuid4()),
            "title": "Royal Kundan Jhumka",
            "jewellery_type": "jhumka",
            "wholesaler_id": str(uuid4()),
        }

        with patch("app.config.settings.TEST_MODE", True):
            urls = await process_product_image(product)

            self.assertEqual(len(urls), 1)
            self.assertEqual(urls[0], "https://storage.com/processed_v1.png")

            # Check that log_ai_generation_start was called with exact composed prompt
            self.assertEqual(mock_log_start.call_count, 1)
            call_kwargs = mock_log_start.call_args[1]
            self.assertEqual(call_kwargs["jewellery_type"], "jhumka")
            self.assertEqual(call_kwargs["base_module_version"], 1)
            self.assertEqual(call_kwargs["category_module_version"], 1)
            self.assertIn("CATEGORY RULES — JHUMKA", call_kwargs["composed_prompt"])
            self.assertIn("Royal Kundan Jhumka", call_kwargs["composed_prompt"])
            self.assertIn("SCENE 1", call_kwargs["composed_prompt"])

            # Check that nanobana_client was passed the prompt
            mock_nanobana.assert_called_once()
            _, nb_kwargs = mock_nanobana.call_args
            self.assertIn("CATEGORY RULES — JHUMKA", nb_kwargs["prompt"])

            # Check completion log
            mock_log_complete.assert_called_once_with("log-id-test", status="success")


if __name__ == "__main__":
    unittest.main()
