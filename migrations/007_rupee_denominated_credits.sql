-- ============================================================================
-- Migration 007 — credits are worth 10 paise: 10 credits = ₹1
-- ============================================================================
-- Run AFTER 006. Safe to re-run: every statement sets absolute values.
--
-- Decided 2026-09-12. The Razorpay webhook grants credits from the amount
-- paid (excluding GST) × CREDITS_PER_RUPEE — 10 — so the rate card is
-- re-stated in the same unit. Fusion is priced at ₹20, and every other
-- feature keeps its old ratio to Fusion:
--
--   feature                       old   new    ≈ ₹
--   chamak.generate                10   200    20
--   chamak.reroll                   6   120    12
--   chamak.set_creation             8   160    16
--   chamak.generate_custom         12   240    24
--   chamak.set_creation_custom     10   200    20
--   chamak.analyze / product.*      0     0     —
--
-- Packs only label a purchase now (the amount decides the credits), so each
-- pack's credits are simply its price × 10.
--
-- The low-balance warning moves from 20 credits (two fusions at the old
-- price) to 400 (two fusions at the new one).
--
-- NOT here: the welcome grant. It stays whatever credits_grant_welcome says
-- until it is decided separately.
-- ============================================================================

BEGIN;

UPDATE public.credit_prices SET credits = 200 WHERE feature_key = 'chamak.generate';
UPDATE public.credit_prices SET credits = 120 WHERE feature_key = 'chamak.reroll';
UPDATE public.credit_prices SET credits = 160 WHERE feature_key = 'chamak.set_creation';
UPDATE public.credit_prices SET credits = 240 WHERE feature_key = 'chamak.generate_custom';
UPDATE public.credit_prices SET credits = 200 WHERE feature_key = 'chamak.set_creation_custom';

UPDATE public.credit_packs
   SET credits    = (price_inr_ex_gst * 10)::int,
       updated_at = now();

ALTER TABLE public.credit_accounts ALTER COLUMN low_balance_threshold SET DEFAULT 400;
UPDATE public.credit_accounts SET low_balance_threshold = 400 WHERE low_balance_threshold = 20;

COMMIT;

-- Verify (read-only):
-- SELECT feature_key, credits FROM public.credit_prices ORDER BY sort_order;
-- SELECT key, credits, price_inr_ex_gst FROM public.credit_packs ORDER BY sort;
