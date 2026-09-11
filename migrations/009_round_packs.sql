-- ============================================================================
-- Migration 009 — round packs for buying credits in the app
-- ============================================================================
-- Run AFTER 008. Safe to re-run: every row is set to absolute values.
--
-- The app's Top Up screen now lists these packs (via the credits-topup Edge
-- Function) and wholesalers pay for one themselves. Prices EXCLUDE GST; the
-- buyer pays price × 1.18. The webhook grants from the amount paid, so a
-- pack's `credits` is only what the app shows; keep it at price ×
-- CREDITS_PER_RUPEE (10).
--
--   pack      price   pays (incl. GST)   credits   Fusions
--   starter    ₹500        ₹590            5,000       25
--   popular  ₹1,000      ₹1,180           10,000       50
--   pro      ₹2,500      ₹2,950           25,000      125
--   bulk     ₹5,000      ₹5,900           50,000      250
--   test         ₹1        ₹1.18              10        —   (off)
--
-- `test` stays inactive. Switch it on for a few minutes to try a real
-- in-app payment for ₹1.18, then off again (re-running this file also turns
-- it off):
--   UPDATE public.credit_packs SET active = true  WHERE key = 'test';
--   UPDATE public.credit_packs SET active = false WHERE key = 'test';
-- ============================================================================

BEGIN;

INSERT INTO public.credit_packs (key, label, credits, price_inr_ex_gst, active, sort) VALUES
    ('starter', 'Starter',  5000,  500, true, 10),
    ('popular', 'Popular', 10000, 1000, true, 20),
    ('pro',     'Pro',     25000, 2500, true, 30),
    ('bulk',    'Bulk',    50000, 5000, true, 40),
    ('test',    'Test',       10,    1, false, 90)
ON CONFLICT (key) DO UPDATE
    SET label            = EXCLUDED.label,
        credits          = EXCLUDED.credits,
        price_inr_ex_gst = EXCLUDED.price_inr_ex_gst,
        active           = EXCLUDED.active,
        sort             = EXCLUDED.sort,
        updated_at       = now();

COMMIT;

-- Verify (read-only):
-- SELECT key, label, credits, price_inr_ex_gst, active, sort FROM public.credit_packs ORDER BY sort;
