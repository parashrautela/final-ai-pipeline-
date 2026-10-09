-- Apply after 017, as database owner, once Supabase Cron is enabled.
-- Installation: https://supabase.com/docs/guides/cron/install
-- Named jobs overwrite their previous schedule, so this step is repeatable.
BEGIN;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_cron') THEN
  RAISE EXCEPTION 'Enable the Supabase Cron integration before scheduling referral expiry';
 END IF;
END $$;
SELECT cron.schedule('jewel-referral-expiry','*/5 * * * *','SELECT public.referral_expire_due(500);');
COMMIT;
