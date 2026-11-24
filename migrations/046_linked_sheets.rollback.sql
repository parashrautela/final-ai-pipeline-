-- 046_linked_sheets.rollback.sql
-- Rollback for 046_linked_sheets.sql. Drops only what 046 added.
-- (sync_schedule is left alone: it existed before 046; set it back with
--  update public.catalogue_import_profiles set sync_schedule = null where sync_schedule = 'daily';)

begin;

drop index if exists public.catalogue_import_profiles_sync_due_idx;

alter table public.catalogue_import_profiles
  drop column if exists pending_job_id,
  drop column if exists last_check_error,
  drop column if exists last_content_hash,
  drop column if exists last_checked_at;

commit;
