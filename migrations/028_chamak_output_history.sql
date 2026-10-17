-- Migration 028 — keep every Chamak fusion attempt.
--
-- A retry re-runs generation on the same chamak_generations row. Until now the
-- output was written to '{wholesaler_id}/{generation_id}.png' with upsert, so
-- each retry overwrote the previous image in storage and on the row — the
-- earlier design was gone for good. The pipeline now saves each render to its
-- own path ('{wholesaler_id}/{generation_id}/{render_id}.png') and appends it
-- here, oldest first. output_image_url / output_variants still hold the latest
-- attempt, so existing readers are unaffected.
ALTER TABLE public.chamak_generations
    ADD COLUMN IF NOT EXISTS output_history JSONB;

COMMENT ON COLUMN public.chamak_generations.output_history IS
    'Fusion attempts, oldest first: [{attempt, path, variants, slider_weights, note, created_at}].';

-- Rows finished before this migration have exactly one image. Record it as
-- attempt 1 so the next retry adds to it instead of starting over.
UPDATE public.chamak_generations
SET output_history = jsonb_build_array(jsonb_build_object(
        'attempt', 1,
        'path', output_image_url,
        'variants', COALESCE(output_variants, '{}'::jsonb),
        'slider_weights', wholesaler_form_json -> 'slider_weights',
        'note', note_text,
        'created_at', completed_at
    ))
WHERE output_history IS NULL
  AND output_image_url IS NOT NULL
  AND mode = 'fusion';
