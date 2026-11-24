-- 046_linked_sheets.sql
-- Linked Google Sheets: remember when a sheet was last checked, a fingerprint of its content,
-- the preview waiting for the wholesaler's review, and why the last check failed.
-- sync_schedule (already exists, was reserved) now holds 'daily' = check every night at 12 am IST.
--
-- Additive only: four new nullable columns on catalogue_import_profiles plus one index.
-- No shipped app reads or writes these columns; nothing existing changes.
-- pending_job_id states ON DELETE SET NULL: a job is deleted by its own wholesaler's cascade
-- (or by hand) and must never block anything.

begin;

alter table public.catalogue_import_profiles
  add column if not exists last_checked_at   timestamptz,
  add column if not exists last_content_hash text,
  add column if not exists last_check_error  text,
  add column if not exists pending_job_id    uuid
    references public.catalogue_import_jobs(id) on delete set null;

create index if not exists catalogue_import_profiles_sync_due_idx
  on public.catalogue_import_profiles (last_checked_at)
  where gsheet_url is not null and sync_schedule = 'daily';

commit;
