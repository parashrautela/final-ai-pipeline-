-- Part 3 of 3 of migration 004 (Treasure Chest).
-- Run these IN ORDER: 004a, then 004b, then 004c.
-- Splitting them means a failure names the part that broke instead of
-- rolling back all 800 lines with one message.

-- ============================================================================
-- WELCOME GRANT
--   Fires when a wholesaler becomes 'verified' — not when they merely submit.
--   Keyed on the user id, so it can only ever pay out once per account.
--
--   NOTE: this stops one ACCOUNT from being granted twice. It does not stop one
--   PERSON from opening several accounts. Deduplicating by phone/PAN belongs in
--   onboarding, not here.
-- ============================================================================
CREATE OR REPLACE FUNCTION public.credits_grant_welcome()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_credits      INT := 100;   -- tune against real COGS; see migration notes
    v_days         INT := 30;
    v_was_verified BOOLEAN := false;
BEGIN
    -- OLD is unassigned on INSERT, and an OR is not guaranteed to short-circuit
    -- before it is dereferenced, so branch on TG_OP explicitly rather than
    -- relying on evaluation order.
    IF TG_OP = 'UPDATE' THEN
        v_was_verified := (OLD.verification_status = 'verified');
    END IF;

    IF NEW.verification_status = 'verified' AND NOT v_was_verified THEN
        PERFORM public.grant_credits(
            p_user            => NEW.user_id,
            p_credits         => v_credits,
            p_source          => 'welcome',
            p_idempotency_key => 'welcome:' || NEW.user_id,
            p_expires_at      => now() + (v_days || ' days')::interval,
            p_note            => 'Welcome gift'
        );
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_credits_welcome ON public.wholesalers;
CREATE TRIGGER trg_credits_welcome
    AFTER INSERT OR UPDATE OF verification_status ON public.wholesalers
    FOR EACH ROW EXECUTE FUNCTION public.credits_grant_welcome();


-- ============================================================================
-- RATE CARD SEED
--
--   ⚠️ PRICING STATUS
--   `chamak.*` is priced against confirmed COGS of ₹10–12 per fusion.
--   `product.*` is seeded at ZERO — deliberately. A product upload runs
--   IMAGE_GENERATION_COUNT (currently 4) Nano Banana generations plus a Reve
--   background removal, so it costs several times what one Chamak fusion does.
--   Metering it at a guessed price would be loss-making. It stays free until
--   the real per-upload cost is measured, at which point turning it on is:
--
--       UPDATE public.credit_prices SET credits = <n> WHERE feature_key = 'product.upload';
--
--   To soft-launch with the ledger running but NOTHING charged:
--       UPDATE public.credit_prices SET credits = 0;
-- ============================================================================
INSERT INTO public.credit_prices (feature_key, credits, label, description, sort_order) VALUES
    ('chamak.analyze',         0,  'Chamak Analysis',
     'Compare two designs and see what makes each one work. Always free.',            10),
    ('chamak.generate',        10, 'Chamak Fusion',
     'Fuse two designs into a new one.',                                              20),
    ('chamak.generate_custom', 12, 'Chamak Fusion (your photos)',
     'Fuse using photos you upload instead of catalogue designs.',                    30),
    ('chamak.reroll',          6,  'Try Again',
     'Generate a fresh result from the same designs and settings.',                   40),
    ('product.upload',         0,  'Product Upload',
     'Studio shots generated from one photo.',                                        50),
    ('product.reprocess',      0,  'Reprocess Product',
     'Regenerate the studio shots for a product already in your catalogue.',          60)
ON CONFLICT (feature_key) DO UPDATE
    SET label       = EXCLUDED.label,
        description = EXCLUDED.description,
        sort_order  = EXCLUDED.sort_order,
        updated_at  = now();
-- Note the DO UPDATE deliberately does NOT touch `credits`: re-running this
-- migration must never silently reset a price you have since tuned in prod.


-- ============================================================================
-- PRIVILEGES
--   The whole security model in one block.
-- ============================================================================

-- The pipeline reads these tables directly (count_prior_debits) as well as
-- through the RPCs. Supabase normally grants new public tables to service_role
-- via default privileges, but stating it here means this migration does not
-- depend on that project setting being intact.
GRANT SELECT, INSERT, UPDATE ON public.credit_accounts  TO service_role;
GRANT SELECT, INSERT, UPDATE ON public.credit_lots      TO service_role;
GRANT SELECT, INSERT         ON public.credit_ledger    TO service_role;
GRANT SELECT, INSERT, UPDATE ON public.credit_purchases TO service_role;
GRANT SELECT, INSERT, UPDATE ON public.credit_prices    TO service_role;

-- Clients read their own rows (RLS decides which) and nothing more.
GRANT SELECT ON public.credit_accounts  TO authenticated;
GRANT SELECT ON public.credit_lots      TO authenticated;
GRANT SELECT ON public.credit_ledger    TO authenticated;
GRANT SELECT ON public.credit_purchases TO authenticated;
GRANT SELECT ON public.credit_prices    TO authenticated, anon;

-- No client may ever write to a credit table directly.
REVOKE INSERT, UPDATE, DELETE ON public.credit_accounts  FROM authenticated, anon;
REVOKE INSERT, UPDATE, DELETE ON public.credit_lots      FROM authenticated, anon;
REVOKE INSERT, UPDATE, DELETE ON public.credit_ledger    FROM authenticated, anon;
REVOKE INSERT, UPDATE, DELETE ON public.credit_purchases FROM authenticated, anon;
REVOKE INSERT, UPDATE, DELETE ON public.credit_prices    FROM authenticated, anon;

-- Mutating functions are service-role only. The apps cannot call these at all.
REVOKE ALL ON FUNCTION public.spend_credits(UUID, TEXT, TEXT, TEXT, TEXT, JSONB)                       FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.grant_credits(UUID, INT, TEXT, TEXT, TIMESTAMPTZ, UUID, TEXT, JSONB, TEXT, TEXT)     FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.refund_credits(TEXT, TEXT, TEXT)                                         FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.credits_expire_due()                                                     FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.credits_ensure_account(UUID)                                             FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.credits_recompute(UUID)                                                  FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.spend_credits(UUID, TEXT, TEXT, TEXT, TEXT, JSONB)                    TO service_role;
GRANT EXECUTE ON FUNCTION public.grant_credits(UUID, INT, TEXT, TEXT, TIMESTAMPTZ, UUID, TEXT, JSONB, TEXT, TEXT)  TO service_role;
GRANT EXECUTE ON FUNCTION public.refund_credits(TEXT, TEXT, TEXT)                                      TO service_role;
GRANT EXECUTE ON FUNCTION public.credits_expire_due()                                                  TO service_role;

-- The one function the apps DO call. It ignores any argument and reads
-- auth.uid(), so it can only ever return the caller's own wallet.
GRANT EXECUTE ON FUNCTION public.credits_wallet() TO authenticated;


-- ============================================================================
-- NIGHTLY EXPIRY SWEEP
--   Requires pg_cron, which on Supabase is enabled from
--   Dashboard → Database → Extensions. Run this block after enabling it.
--   03:30 UTC = 09:00 IST — after the overnight, before the trading day.
-- ============================================================================
-- CREATE EXTENSION IF NOT EXISTS pg_cron;
--
-- SELECT cron.schedule(
--     'credits-expire-due',
--     '30 3 * * *',
--     $cron$ SELECT public.credits_expire_due(); $cron$
-- );


-- ============================================================================
-- BACKFILL — give every already-verified wholesaler their welcome grant.
--   Idempotent: the 'welcome:<uuid>' key means re-running pays out nothing.
--   Review the cost before running this on production.
-- ============================================================================
-- DO $backfill$
-- DECLARE r RECORD;
-- BEGIN
--     FOR r IN SELECT user_id FROM public.wholesalers WHERE verification_status = 'verified'
--     LOOP
--         PERFORM public.grant_credits(
--             p_user => r.user_id, p_credits => 100, p_source => 'welcome',
--             p_idempotency_key => 'welcome:' || r.user_id,
--             p_expires_at => now() + INTERVAL '30 days', p_note => 'Welcome gift (backfill)');
--     END LOOP;
-- END
-- $backfill$;
