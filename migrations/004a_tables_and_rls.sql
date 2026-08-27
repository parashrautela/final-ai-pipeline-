-- Part 1 of 3 of migration 004 (Treasure Chest).
-- Run these IN ORDER: 004a, then 004b, then 004c.
-- Splitting them means a failure names the part that broke instead of
-- rolling back all 800 lines with one message.

-- ============================================================================
-- Migration 004: Treasure Chest — credit wallet, ledger, and pricing
-- ============================================================================
--
-- Security model, in one paragraph:
--   Wholesalers can READ their own wallet and the public rate card. That is all.
--   Every mutation (spend / refund / grant / expire) runs through a
--   SECURITY DEFINER function that is EXECUTE-able only by `service_role`.
--   The iOS app and the web app never move credits themselves — they call the
--   AI pipeline (which holds the service-role key) and it does the debit.
--   This means an attacker holding the anon key, or the app binary, cannot
--   mint, spend, or refund a single credit.
--
-- Negative balances are impossible by construction: CHECK (>= 0) is the
-- backstop, SELECT ... FOR UPDATE is the mechanism.
--
-- Depends on: public.wholesalers (user_id, verification_status)
-- ============================================================================


-- ─────────────────────────────────────────────────────────────────────────────
-- 1. ACCOUNTS — one row per wholesaler
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.credit_accounts (
    wholesaler_id           UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    -- Denormalised for fast reads. `credit_lots` remains the source of truth;
    -- every mutating function recomputes this in the same transaction.
    balance_cached          INT NOT NULL DEFAULT 0 CHECK (balance_cached >= 0),
    lifetime_granted        INT NOT NULL DEFAULT 0,
    lifetime_spent          INT NOT NULL DEFAULT 0,
    lifetime_expired        INT NOT NULL DEFAULT 0,
    low_balance_threshold   INT NOT NULL DEFAULT 20,
    -- Set when we warn them, cleared on top-up, so the banner fires once per
    -- crossing instead of on every app launch.
    low_balance_notified_at TIMESTAMPTZ,
    -- Money was refunded/charged back after the credits had already been spent.
    -- Balance clamps at 0 rather than going negative; this records the shortfall.
    recovery_owed           INT NOT NULL DEFAULT 0 CHECK (recovery_owed >= 0),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ─────────────────────────────────────────────────────────────────────────────
-- 2. PURCHASES — money in, both rails
--    Declared before credit_lots because lots reference it.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.credit_purchases (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID NOT NULL REFERENCES public.credit_accounts(wholesaler_id) ON DELETE CASCADE,
    provider        TEXT NOT NULL CHECK (provider IN ('razorpay', 'apple', 'manual')),
    -- Razorpay payment_id / Apple transaction_id. UNIQUE is what makes webhook
    -- retries and receipt replay harmless — Razorpay retries on any non-200.
    provider_txn_id TEXT UNIQUE,
    provider_ref    TEXT,                       -- order_id / payment_link_id, for reconciliation
    pack_key        TEXT NOT NULL,
    credits         INT  NOT NULL CHECK (credits > 0),
    amount_inr      NUMERIC(10,2),              -- taxable value, excluding GST
    gst_inr         NUMERIC(10,2),
    -- Place of supply drives CGST+SGST vs IGST. Captured at purchase because
    -- it cannot be reconstructed later.
    buyer_gstin     TEXT,
    buyer_state     TEXT,
    invoice_number  TEXT UNIQUE,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'paid', 'failed', 'refunded', 'chargeback')),
    receipt_json    JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_credit_purchases_account
    ON public.credit_purchases (account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_credit_purchases_status
    ON public.credit_purchases (status) WHERE status = 'pending';


-- ─────────────────────────────────────────────────────────────────────────────
-- 3. LOTS — every grant is a lot; spending consumes them expiring-soonest-first
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.credit_lots (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id        UUID NOT NULL REFERENCES public.credit_accounts(wholesaler_id) ON DELETE CASCADE,
    source            TEXT NOT NULL CHECK (source IN
                        ('welcome', 'purchase', 'subscription', 'referral', 'promo', 'refund', 'admin')),
    credits_granted   INT  NOT NULL CHECK (credits_granted > 0),
    credits_remaining INT  NOT NULL CHECK (credits_remaining >= 0),
    granted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULL = never expires. ALL purchased credits are NULL: expiring credits
    -- somebody paid for is a trust landmine in a tight-knit trade, and prepaid
    -- credits are vouchers under Indian GST.
    expires_at        TIMESTAMPTZ,
    purchase_id       UUID REFERENCES public.credit_purchases(id) ON DELETE SET NULL,
    note              TEXT,
    CONSTRAINT credit_lots_remaining_lte_granted CHECK (credits_remaining <= credits_granted)
);

-- The exact shape spend_credits() walks: live lots, soonest expiry first.
CREATE INDEX IF NOT EXISTS idx_credit_lots_spendable
    ON public.credit_lots (account_id, expires_at NULLS LAST, granted_at)
    WHERE credits_remaining > 0;

-- Driver for the expiry cron.
CREATE INDEX IF NOT EXISTS idx_credit_lots_expiring
    ON public.credit_lots (expires_at)
    WHERE credits_remaining > 0 AND expires_at IS NOT NULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- 4. LEDGER — append only. Never UPDATE. Never DELETE.
--    One row per user-visible transaction; per-lot allocation lives in metadata.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.credit_ledger (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID NOT NULL REFERENCES public.credit_accounts(wholesaler_id) ON DELETE CASCADE,
    delta           INT  NOT NULL,   -- +grant / -debit / +refund / -expiry
    kind            TEXT NOT NULL CHECK (kind IN ('grant', 'debit', 'refund', 'expiry', 'adjustment')),
    feature_key     TEXT,            -- 'chamak.generate', 'product.upload', …
    reference_type  TEXT,            -- 'chamak_generation' | 'product' | 'purchase'
    reference_id    TEXT,
    -- ⭐ The single column that prevents every double-charge bug: double-taps,
    -- network retries, app relaunches, webhook redelivery.
    idempotency_key TEXT UNIQUE,
    balance_after   INT NOT NULL,
    -- Holds {allocations: [{lot_id, credits}]} and, for debits, the REAL AI
    -- cost reported by the pipeline. That is how we learn true margin while
    -- still billing a flat price.
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_credit_ledger_account
    ON public.credit_ledger (account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_credit_ledger_reference
    ON public.credit_ledger (reference_type, reference_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- 5. PRICES — the rate card. The apps READ this; costs are never hardcoded.
--    Changing a price is an UPDATE here, not an App Store release.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.credit_prices (
    feature_key TEXT PRIMARY KEY,
    credits     INT  NOT NULL CHECK (credits >= 0),
    label       TEXT NOT NULL,
    description TEXT,
    sort_order  INT  NOT NULL DEFAULT 0,
    is_active   BOOLEAN NOT NULL DEFAULT true,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ============================================================================
-- ROW LEVEL SECURITY
--   Read your own wallet. Read the rate card. Nothing else.
--   No INSERT / UPDATE / DELETE policy exists for any client role — the
--   absence is deliberate, not an oversight.
-- ============================================================================

ALTER TABLE public.credit_accounts  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.credit_lots      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.credit_ledger    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.credit_purchases ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.credit_prices    ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "own credit account" ON public.credit_accounts;
CREATE POLICY "own credit account" ON public.credit_accounts
    FOR SELECT USING (auth.uid() = wholesaler_id);

DROP POLICY IF EXISTS "own credit lots" ON public.credit_lots;
CREATE POLICY "own credit lots" ON public.credit_lots
    FOR SELECT USING (auth.uid() = account_id);

DROP POLICY IF EXISTS "own credit ledger" ON public.credit_ledger;
CREATE POLICY "own credit ledger" ON public.credit_ledger
    FOR SELECT USING (auth.uid() = account_id);

DROP POLICY IF EXISTS "own credit purchases" ON public.credit_purchases;
CREATE POLICY "own credit purchases" ON public.credit_purchases
    FOR SELECT USING (auth.uid() = account_id);

-- The rate card is public: the app must render costs before the user is
-- necessarily looking at their own wallet.
DROP POLICY IF EXISTS "rate card is readable" ON public.credit_prices;
CREATE POLICY "rate card is readable" ON public.credit_prices
    FOR SELECT USING (is_active);
