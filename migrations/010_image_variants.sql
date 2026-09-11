-- ============================================================================
-- Migration 010 — where the small copies of each image live
-- ============================================================================
-- Safe to re-run. Adds columns only; nothing is rewritten.
--
-- Images are stored at 2048px and up to 3 MB, and the apps draw them in
-- ~540px cards: a catalogue screen downloads ~36 MB to show ~0.5 MB of
-- pixels, every time, because Supabase also served them as `no-cache`.
-- The pipeline now writes three WebP copies next to every image
-- (card 540px ≈ 41 KB, detail 1200px ≈ 176 KB, full 2048px ≈ 474 KB) and
-- records where they went. The originals stay exactly as they are.
--
--   products.image_variants
--     { "<original url>": { "card": url, "detail": url, "full": url }, … }
--     Keyed by the URL already held in image_url / generated_image_urls, so
--     a client looks up whatever URL it has and falls back to it when the
--     key is missing (old rows, or a copy that failed to build).
--
--   chamak_generations.output_variants
--     { "card": path, "detail": path, "full": path }
--     Bucket-relative paths, because chamak-outputs is private and the app
--     signs the size it needs.
--
-- Both are nullable and both are ignored by anything that doesn't know about
-- them: an app build from before this migration keeps using the originals.
-- ============================================================================

BEGIN;

ALTER TABLE public.products
    ADD COLUMN IF NOT EXISTS image_variants JSONB;

ALTER TABLE public.chamak_generations
    ADD COLUMN IF NOT EXISTS output_variants JSONB;

COMMENT ON COLUMN public.products.image_variants IS
    'Small WebP copies of each image URL: {"<original url>": {"card"|"detail"|"full": url}}. Written by ai-pipeline; clients fall back to the original when absent.';

COMMENT ON COLUMN public.chamak_generations.output_variants IS
    'Small WebP copies of output_image_url, as paths inside the private chamak-outputs bucket: {"card"|"detail"|"full": path}.';

COMMIT;

-- Verify (read-only):
-- SELECT count(*) FILTER (WHERE image_variants IS NOT NULL) AS with_variants, count(*) FROM public.products;
-- SELECT count(*) FILTER (WHERE output_variants IS NOT NULL) AS with_variants, count(*) FROM public.chamak_generations;
