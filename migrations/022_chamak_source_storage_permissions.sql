-- Repair source-photo uploads for Chamak/Set Creation.
-- Run in the Supabase SQL editor as the database administrator.
-- Matches migration 005's ownership boundary; keeps RLS enabled.
BEGIN;

DROP POLICY IF EXISTS "Wholesalers can upload own raw source images" ON storage.objects;
CREATE POLICY "Wholesalers can upload own raw source images"
ON storage.objects FOR INSERT TO authenticated
WITH CHECK (
    bucket_id = 'plant-images'
    AND (storage.foldername(name))[1] = 'raw'
    AND (storage.foldername(name))[2] = auth.uid()::text
);

-- Overwrite clients need SELECT as well as INSERT and UPDATE.
DROP POLICY IF EXISTS "Wholesalers can read own raw source images" ON storage.objects;
CREATE POLICY "Wholesalers can read own raw source images"
ON storage.objects FOR SELECT TO authenticated
USING (
    bucket_id = 'plant-images'
    AND (storage.foldername(name))[1] = 'raw'
    AND (storage.foldername(name))[2] = auth.uid()::text
);

DROP POLICY IF EXISTS "Wholesalers can update own raw source images" ON storage.objects;
CREATE POLICY "Wholesalers can update own raw source images"
ON storage.objects FOR UPDATE TO authenticated
USING (
    bucket_id = 'plant-images'
    AND (storage.foldername(name))[1] = 'raw'
    AND (storage.foldername(name))[2] = auth.uid()::text
)
WITH CHECK (
    bucket_id = 'plant-images'
    AND (storage.foldername(name))[1] = 'raw'
    AND (storage.foldername(name))[2] = auth.uid()::text
);
COMMIT;

-- Inspect deployed policies if generation creation still fails:
-- SELECT schemaname, tablename, policyname, cmd, roles, qual, with_check
-- FROM pg_policies
-- WHERE (schemaname = 'storage' AND tablename = 'objects')
--    OR (schemaname = 'public' AND tablename = 'chamak_generations');
-- Generation INSERT and SELECT must both require auth.uid() = wholesaler_id,
-- as defined in create_chamak_generations.sql. Do not disable RLS.
