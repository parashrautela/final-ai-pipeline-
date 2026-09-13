-- Preserve every image produced by configurable Set Creation output counts.
ALTER TABLE public.chamak_generations
    ADD COLUMN IF NOT EXISTS output_images JSONB;

COMMENT ON COLUMN public.chamak_generations.output_images IS
    'Set Creation outputs: [{path: text, variants: {card, detail, full}}].';
