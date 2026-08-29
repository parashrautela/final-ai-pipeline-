-- ============================================================================
-- Migration 005 — Set Creation mode
-- ============================================================================
-- Set Creation is the inverse of Chamak Fusion. Fusion blends two designs of
-- the same category into ONE new piece. Set Creation takes two pieces of
-- DIFFERENT categories (necklace + jhumka) and stages BOTH of them, unchanged,
-- into a single matched-set catalogue photograph.
--
-- It reuses `chamak_generations` rather than getting its own table: same two
-- image inputs, same credits, same gallery, same RLS, same feedback. Only the
-- prompt and the middle step differ. Additive and back-compatible — every
-- existing row is a fusion, which is why `mode` defaults to 'fusion'.
-- ============================================================================

ALTER TABLE public.chamak_generations
    ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'fusion';

-- Added separately from the column so re-running the migration on a table that
-- already has `mode` still installs the constraint.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chamak_generations_mode_check'
    ) THEN
        ALTER TABLE public.chamak_generations
            ADD CONSTRAINT chamak_generations_mode_check
            CHECK (mode IN ('fusion', 'set_creation'));
    END IF;
END $$;

-- Backdrop preset id for set_creation rows ('velvet_bust', 'dark_slate',
-- 'festive', 'clean_studio'). NULL for fusion rows. Its own column rather than
-- buried in wholesaler_form_json so usage stays queryable — which preset the
-- wholesalers actually pick is worth knowing.
ALTER TABLE public.chamak_generations
    ADD COLUMN IF NOT EXISTS set_backdrop TEXT;

-- Gallery and the pipeline both filter on mode.
CREATE INDEX IF NOT EXISTS idx_chamak_generations_mode
    ON public.chamak_generations (wholesaler_id, mode, created_at DESC);

-- ----------------------------------------------------------------------------
-- Pricing. Set Creation is one image-generation call and NO vision-analysis
-- call (it skips stage 1 entirely — there is nothing to analyse when both
-- pieces are reproduced as-is), so it costs us less than a fusion. Priced at 8
-- against fusion's 10 to reflect that.
--
-- Re-roll deliberately reuses the existing `chamak.reroll` key.
-- ----------------------------------------------------------------------------
-- Guarded: the Treasure Chest credit schema (migration 004) has not been run
-- on every environment, and `credit_prices` simply does not exist there. This
-- pricing is optional — with CREDITS_ENABLED=false the pipeline never reads it,
-- and Set Creation works fine without it. So skip rather than fail the whole
-- migration on a database that has no credit system yet.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_schema = 'public' AND table_name = 'credit_prices'
    ) THEN
        INSERT INTO public.credit_prices (feature_key, credits, label, description, sort_order) VALUES
            ('chamak.set_creation',        8,  'Set Creation',
             'Stage two of your pieces together as one matched set photo.',            22),
            ('chamak.set_creation_custom', 10, 'Set Creation (your photos)',
             'Stage a set using photos you upload instead of catalogue designs.',      24)
        ON CONFLICT (feature_key) DO UPDATE
            SET label       = EXCLUDED.label,
                description = EXCLUDED.description,
                credits     = EXCLUDED.credits,
                sort_order  = EXCLUDED.sort_order;
        RAISE NOTICE 'Set Creation pricing installed.';
    ELSE
        RAISE NOTICE 'credit_prices not found - skipping Set Creation pricing. Run the Treasure Chest migration (004) first if you want metered billing.';
    END IF;
END $$;

-- ----------------------------------------------------------------------------
-- Storage: let a signed-in wholesaler upload their own source photos.
--
-- WHY THIS IS HERE: the `plant-images` bucket is public to READ, but nothing
-- ever granted authenticated users the right to WRITE to it. Every file in it
-- today sits under `products/` and was put there by the pipeline using the
-- service-role key, which bypasses RLS entirely.
--
-- The consequence, verified against the live bucket: `raw/` is completely
-- empty. Browser-side uploads have never worked — not for Set Creation, and
-- not for Chamak either. Both write to `raw/{auth.uid()}/...` and both get
-- "new row violates row-level security policy" from storage.objects.
--
-- These policies scope writes tightly: a wholesaler may only write inside a
-- folder named after their own auth uid, and only under `raw/`. They cannot
-- touch `products/`, and they cannot write into another wholesaler's folder.
-- ----------------------------------------------------------------------------

DROP POLICY IF EXISTS "Wholesalers can upload own raw source images" ON storage.objects;
CREATE POLICY "Wholesalers can upload own raw source images"
    ON storage.objects FOR INSERT
    TO authenticated
    WITH CHECK (
        bucket_id = 'plant-images'
        AND (storage.foldername(name))[1] = 'raw'
        AND (storage.foldername(name))[2] = auth.uid()::text
    );

-- Needed for the `upsert` path and for replacing a photo in the same slot.
DROP POLICY IF EXISTS "Wholesalers can update own raw source images" ON storage.objects;
CREATE POLICY "Wholesalers can update own raw source images"
    ON storage.objects FOR UPDATE
    TO authenticated
    USING (
        bucket_id = 'plant-images'
        AND (storage.foldername(name))[1] = 'raw'
        AND (storage.foldername(name))[2] = auth.uid()::text
    );

-- ============================================================================
-- Verify
-- ============================================================================
-- SELECT column_name, data_type, column_default
--   FROM information_schema.columns
--  WHERE table_name = 'chamak_generations' AND column_name IN ('mode','set_backdrop');
--
-- SELECT feature_key, credits FROM public.credit_prices
--  WHERE feature_key LIKE 'chamak.set_creation%';
--
-- SELECT policyname, cmd FROM pg_policies
--  WHERE tablename = 'objects' AND schemaname = 'storage'
--    AND policyname LIKE '%raw source images%';
-- ============================================================================
