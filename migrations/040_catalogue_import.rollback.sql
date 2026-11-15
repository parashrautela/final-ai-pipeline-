-- 040_catalogue_import.rollback.sql
-- Rollback for 040_catalogue_import.sql
--
-- Note: Empty the 'catalogue-imports' bucket via the Storage API or Supabase dashboard first
-- before running this rollback. SQL deletes on storage.objects are blocked by storage triggers.

begin;

-- Verify the bucket is empty before proceeding.
do $$
declare
  v_count int;
begin
  select count(*) into v_count from storage.objects where bucket_id = 'catalogue-imports';
  if v_count > 0 then
    raise exception 'Bucket catalogue-imports is not empty (% objects found). Please empty all objects from catalogue-imports via the Storage API or Supabase dashboard before running rollback.', v_count;
  end if;
end $$;

-- 1. Storage policies and bucket
drop policy if exists "Wholesalers can delete own catalogue imports" on storage.objects;
drop policy if exists "Wholesalers can update own catalogue imports" on storage.objects;
drop policy if exists "Wholesalers can select own catalogue imports" on storage.objects;
drop policy if exists "Wholesalers can upload own catalogue imports" on storage.objects;

set local storage.allow_delete_query = 'true';
delete from storage.buckets where id = 'catalogue-imports';

-- 2. RLS policies
drop policy if exists own_rows on public.catalogue_import_rows;
drop policy if exists own_jobs on public.catalogue_import_jobs;
drop policy if exists own_profiles on public.catalogue_import_profiles;

-- 3. Import tables
drop table if exists public.catalogue_import_rows cascade;
drop table if exists public.catalogue_import_jobs cascade;
drop table if exists public.catalogue_import_profiles cascade;

-- 4. Products table indexes and columns
drop index if exists public.products_wholesaler_code_uniq;
drop index if exists public.products_wholesaler_idx;

alter table public.products
  drop column if exists product_code,
  drop column if exists import_job_id,
  drop column if exists import_snapshot,
  drop column if exists extra_attributes;

commit;
