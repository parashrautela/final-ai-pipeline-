-- 040_catalogue_import.rollback.sql
-- Rollback for 040_catalogue_import.sql

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
