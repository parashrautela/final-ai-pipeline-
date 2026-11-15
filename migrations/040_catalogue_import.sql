-- 040_catalogue_import.sql
-- Bulk catalogue import schema: products columns, profiles, jobs, rows, and storage bucket/policies.

-- 1. products: identity for re-imports + where unmapped columns go
alter table public.products
  add column if not exists product_code      text,
  add column if not exists import_job_id     uuid,
  add column if not exists import_snapshot   jsonb,     -- last values written by an import, per field (3-way merge base)
  add column if not exists extra_attributes  jsonb not null default '{}'::jsonb;  -- e.g. {"Karigar":"Ramesh","Remarks":"Hallmarked"}

create unique index if not exists products_wholesaler_code_uniq
  on public.products (wholesaler_id, upper(product_code)) where product_code is not null;
create index if not exists products_wholesaler_idx on public.products (wholesaler_id);   -- was missing

-- 2. what we learned about one code-keeper's format, reused next time
create table if not exists public.catalogue_import_profiles (
  id                  uuid primary key default gen_random_uuid(),
  wholesaler_id       uuid not null references auth.users(id) on delete cascade,
  name                text not null,                       -- "Balaji Designs register"
  header_fingerprint  text[] not null,                     -- normalized header names seen
  column_map          jsonb not null,                      -- {"Design No.":"product_code","G.Wt (gm)":"gross_weight",...,"Karigar":"extra:Karigar","Sr No":"ignore"}
  value_maps          jsonb not null default '{}'::jsonb,  -- {"jewellery_type":{"rg":"rings","nkl":"necklace"},"style":{"temple":"traditional"}}
  sheet_rules         jsonb not null default '{}'::jsonb,  -- {"sheets":["Stock Register"],"header_row":4,"skip_hidden":true}
  gsheet_url          text,                                -- set when the source is a Google Sheet
  sync_schedule       text,                                -- reserved for later auto-sync; null now
  last_synced_at      timestamptz,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

-- 3. one upload attempt
create table if not exists public.catalogue_import_jobs (
  id               uuid primary key default gen_random_uuid(),
  wholesaler_id    uuid not null references auth.users(id) on delete cascade,
  profile_id       uuid references public.catalogue_import_profiles(id) on delete set null,
  status           text not null default 'uploaded'
                   check (status in ('uploaded','reading','read','needs_answers','ready','committing','done','failed','cancelled')),
  source_kind      text not null check (source_kind in ('file','gsheet','images_only')),
  source_files     jsonb not null default '[]'::jsonb,  -- [{"path":"<wid>/<job>/a.xlsx","name":"A1.xlsx","bytes":10106,"detected":"xlsx"}]
  gsheet_url       text,
  questions        jsonb not null default '[]'::jsonb,  -- [{"id":"q1","kind":"column|value|sheet|conflict_policy","text":"…","options":[…]}]
  answers          jsonb not null default '{}'::jsonb,
  summary          jsonb not null default '{}'::jsonb,  -- {"create":37,"update":0,"unchanged":0,"conflict":0,"not_in_file":0,"error":3,"skipped":0,"duplicate_skipped":1}
  photo_summary    jsonb not null default '{}'::jsonb,  -- {"matched":31,"extra_photos":1,"unmatched_files":[…],"rejected":{"x.png":"over 10 MB"},"ignored":[…]}
  llm_usage        jsonb not null default '{}'::jsonb,  -- {"calls":1,"input_tokens":…,"output_tokens":…,"model":"…"}
  error            text,
  created_at       timestamptz not null default now(),
  committed_at     timestamptz
);
create index if not exists catalogue_import_jobs_wholesaler_idx on public.catalogue_import_jobs (wholesaler_id, created_at desc);
-- one job committing at a time per wholesaler
create unique index if not exists catalogue_import_one_commit on public.catalogue_import_jobs (wholesaler_id) where status = 'committing';

-- 4. every row of every file, raw and normalized (the audit trail + the preview)
create table if not exists public.catalogue_import_rows (
  id            bigint generated always as identity primary key,
  job_id        uuid not null references public.catalogue_import_jobs(id) on delete cascade,
  sheet         text,
  row_number    int not null,                         -- as the wholesaler sees it in Excel (1-based, incl. header rows)
  raw           jsonb not null,                       -- {"Design No.":"110023","Item":"Rg",…} exactly as read
  normalized    jsonb,                                -- canonical field -> value
  product_code  text,
  photo_refs    text[] not null default '{}',         -- names/URLs from the sheet + matched zip entries
  action        text check (action in ('create','update','unchanged','conflict','error','skipped','duplicate_skipped')),
  changes       jsonb,                                -- {"gross_weight":{"from":12.5,"to":13.75}} ; for conflicts also "mine"
  resolution    text check (resolution in ('sheet','mine')),
  messages      jsonb not null default '[]'::jsonb,   -- [{"level":"error|warning","field":"stone_weight","text":"…"}]
  product_id    uuid references public.products(id) on delete set null,
  unique (job_id, sheet, row_number)
);
create index if not exists catalogue_import_rows_job_action_idx on public.catalogue_import_rows (job_id, action);

-- RLS: a wholesaler sees only their own; all writes go through ai-pipeline (service role) after ownership checks.
alter table public.catalogue_import_profiles enable row level security;
alter table public.catalogue_import_jobs     enable row level security;
alter table public.catalogue_import_rows     enable row level security;

drop policy if exists own_profiles on public.catalogue_import_profiles;
create policy own_profiles on public.catalogue_import_profiles for select using (wholesaler_id = auth.uid());

drop policy if exists own_jobs on public.catalogue_import_jobs;
create policy own_jobs on public.catalogue_import_jobs for select using (wholesaler_id = auth.uid());

drop policy if exists own_rows on public.catalogue_import_rows;
create policy own_rows on public.catalogue_import_rows for select
  using (exists (select 1 from public.catalogue_import_jobs j where j.id = job_id and j.wholesaler_id = auth.uid()));

-- 5. Storage bucket: private bucket 'catalogue-imports'
insert into storage.buckets (id, name, public, file_size_limit)
values ('catalogue-imports', 'catalogue-imports', false, 209715200)
on conflict (id) do update set
  public = false,
  file_size_limit = 209715200;

-- Storage RLS policies for catalogue-imports
drop policy if exists "Wholesalers can upload own catalogue imports" on storage.objects;
create policy "Wholesalers can upload own catalogue imports"
  on storage.objects for insert to authenticated
  with check (
    bucket_id = 'catalogue-imports'
    and (storage.foldername(name))[1] = auth.uid()::text
  );

drop policy if exists "Wholesalers can select own catalogue imports" on storage.objects;
create policy "Wholesalers can select own catalogue imports"
  on storage.objects for select to authenticated
  using (
    bucket_id = 'catalogue-imports'
    and (storage.foldername(name))[1] = auth.uid()::text
  );

drop policy if exists "Wholesalers can update own catalogue imports" on storage.objects;
create policy "Wholesalers can update own catalogue imports"
  on storage.objects for update to authenticated
  using (
    bucket_id = 'catalogue-imports'
    and (storage.foldername(name))[1] = auth.uid()::text
  )
  with check (
    bucket_id = 'catalogue-imports'
    and (storage.foldername(name))[1] = auth.uid()::text
  );

drop policy if exists "Wholesalers can delete own catalogue imports" on storage.objects;
create policy "Wholesalers can delete own catalogue imports"
  on storage.objects for delete to authenticated
  using (
    bucket_id = 'catalogue-imports'
    and (storage.foldername(name))[1] = auth.uid()::text
  );
