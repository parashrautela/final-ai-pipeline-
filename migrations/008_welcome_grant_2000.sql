-- ============================================================================
-- Migration 008 — welcome grant: 2,000 credits (10 Fusions)
-- ============================================================================
-- Run AFTER 007. Safe to re-run.
--
-- Decided 2026-09-12. Under 007's unit (10 credits = ₹1, Fusion = 200) the
-- old 100-credit welcome was half a Fusion. It is now 2,000 — 10 Fusions,
-- the same "ten free fusions" 004 intended — still expiring after 30 days.
--
-- Only the amount changes; the function is otherwise 004c's, verbatim. The
-- idempotency key is still 'welcome:<user id>', so nobody is ever granted
-- twice. Wholesalers verified before this ran get theirs from the one-off
-- backfill run alongside it at go-live.
-- ============================================================================

BEGIN;

CREATE OR REPLACE FUNCTION public.credits_grant_welcome()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_credits      INT := 2000;  -- 10 Fusions at 200 credits each (migration 008)
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

COMMIT;
