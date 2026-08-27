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


-- ============================================================================
-- FUNCTIONS
--   Every one of these is SECURITY DEFINER with a pinned search_path, and is
--   EXECUTE-able only by service_role (grants at the bottom of this file).
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- credits_ensure_account — idempotent account creation
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.credits_ensure_account(p_user UUID)
RETURNS VOID
LANGUAGE sql
SET search_path = public, pg_temp
AS $$
    INSERT INTO public.credit_accounts (wholesaler_id)
    VALUES (p_user)
    ON CONFLICT (wholesaler_id) DO NOTHING;
$$;


-- ─────────────────────────────────────────────────────────────────────────────
-- credits_recompute — rebuild the cached balance from the lots.
--   Called after every mutation. We recompute rather than increment so the
--   cache can never drift away from the truth and trip the CHECK constraint.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.credits_recompute(p_user UUID)
RETURNS INT
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
    v_balance INT;
BEGIN
    SELECT COALESCE(SUM(credits_remaining), 0) INTO v_balance
      FROM public.credit_lots
     WHERE account_id = p_user
       AND credits_remaining > 0
       AND (expires_at IS NULL OR expires_at > now());

    UPDATE public.credit_accounts
       SET balance_cached = v_balance,
           updated_at     = now(),
           -- Clear the low-balance flag once they are comfortably back above
           -- the threshold, so the banner can fire again on the next crossing.
           low_balance_notified_at = CASE
               WHEN v_balance > low_balance_threshold THEN NULL
               ELSE low_balance_notified_at
           END
     WHERE wholesaler_id = p_user;

    RETURN v_balance;
END;
$$;


-- ─────────────────────────────────────────────────────────────────────────────
-- spend_credits — the debit. Atomic, idempotent, cannot go negative.
--
--   Returns, on success:  {ok:true, charged:<int>, balance:<int>, cost:<int>}
--   Returns, when broke:  {ok:false, error:'INSUFFICIENT_CREDITS',
--                          required, balance, short_by}
--   Returns, on replay:   {ok:true, replayed:true, …}   ← same key twice
--
--   The caller does NOT pass a price. The price is read from credit_prices
--   inside this transaction, so a tampered client cannot charge itself 1 credit
--   for a 10-credit action.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.spend_credits(
    p_user            UUID,
    p_feature_key     TEXT,
    p_idempotency_key TEXT,
    p_reference_type  TEXT  DEFAULT NULL,
    p_reference_id    TEXT  DEFAULT NULL,
    p_metadata        JSONB DEFAULT '{}'::jsonb
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_existing  public.credit_ledger%ROWTYPE;
    v_cost      INT;
    v_available INT;
    v_remaining INT;
    v_take      INT;
    v_lot       RECORD;
    v_allocs    JSONB := '[]'::jsonb;
    v_balance   INT;
BEGIN
    -- 1. Idempotent replay. A double-tap, a retried request and a relaunched
    --    app all arrive with the same key and must charge exactly once.
    SELECT * INTO v_existing
      FROM public.credit_ledger
     WHERE idempotency_key = p_idempotency_key;

    IF FOUND THEN
        RETURN jsonb_build_object(
            'ok', true, 'replayed', true,
            'charged', -v_existing.delta,
            'balance', v_existing.balance_after,
            'ledger_id', v_existing.id
        );
    END IF;

    -- 2. Server-side pricing.
    SELECT credits INTO v_cost
      FROM public.credit_prices
     WHERE feature_key = p_feature_key AND is_active;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'error', 'UNKNOWN_FEATURE',
                                  'feature_key', p_feature_key);
    END IF;

    PERFORM public.credits_ensure_account(p_user);

    -- 3. Free feature (cost 0) — e.g. Chamak analysis. Nothing to charge and
    --    nothing worth cluttering the history with.
    IF v_cost = 0 THEN
        SELECT balance_cached INTO v_balance
          FROM public.credit_accounts WHERE wholesaler_id = p_user;
        RETURN jsonb_build_object('ok', true, 'charged', 0,
                                  'balance', COALESCE(v_balance, 0), 'free', true);
    END IF;

    -- 4. Lock this wholesaler's live lots, then total them. The lock is what
    --    makes concurrent spends (two devices, double-tap) safe — not any
    --    check in Swift or Python.
    PERFORM 1 FROM public.credit_lots
     WHERE account_id = p_user
       AND credits_remaining > 0
       AND (expires_at IS NULL OR expires_at > now())
     FOR UPDATE;

    SELECT COALESCE(SUM(credits_remaining), 0) INTO v_available
      FROM public.credit_lots
     WHERE account_id = p_user
       AND credits_remaining > 0
       AND (expires_at IS NULL OR expires_at > now());

    IF v_available < v_cost THEN
        RETURN jsonb_build_object(
            'ok', false, 'error', 'INSUFFICIENT_CREDITS',
            'required', v_cost, 'balance', v_available,
            'short_by', v_cost - v_available
        );
    END IF;

    -- 5. Consume lots expiring-soonest-first, so nothing expires that could
    --    have been spent, and free credits go before purchased ones.
    v_remaining := v_cost;
    FOR v_lot IN
        SELECT id, credits_remaining, expires_at
          FROM public.credit_lots
         WHERE account_id = p_user
           AND credits_remaining > 0
           AND (expires_at IS NULL OR expires_at > now())
         ORDER BY expires_at NULLS LAST, granted_at
    LOOP
        EXIT WHEN v_remaining <= 0;
        v_take := LEAST(v_lot.credits_remaining, v_remaining);

        UPDATE public.credit_lots
           SET credits_remaining = credits_remaining - v_take
         WHERE id = v_lot.id;

        v_allocs := v_allocs || jsonb_build_object(
            'lot_id', v_lot.id, 'credits', v_take, 'expires_at', v_lot.expires_at);
        v_remaining := v_remaining - v_take;
    END LOOP;

    v_balance := public.credits_recompute(p_user);

    UPDATE public.credit_accounts
       SET lifetime_spent = lifetime_spent + v_cost
     WHERE wholesaler_id = p_user;

    INSERT INTO public.credit_ledger (
        account_id, delta, kind, feature_key,
        reference_type, reference_id, idempotency_key, balance_after, metadata
    ) VALUES (
        p_user, -v_cost, 'debit', p_feature_key,
        p_reference_type, p_reference_id, p_idempotency_key, v_balance,
        COALESCE(p_metadata, '{}'::jsonb) || jsonb_build_object('allocations', v_allocs)
    );

    RETURN jsonb_build_object('ok', true, 'charged', v_cost, 'balance', v_balance);

EXCEPTION
    -- Two identical calls that raced past the replay check both reach the
    -- INSERT; the unique index rejects the loser. Return the winner's result
    -- rather than an error — from the caller's side it charged exactly once.
    WHEN unique_violation THEN
        SELECT * INTO v_existing
          FROM public.credit_ledger WHERE idempotency_key = p_idempotency_key;
        RETURN jsonb_build_object(
            'ok', true, 'replayed', true,
            'charged', -v_existing.delta,
            'balance', v_existing.balance_after,
            'ledger_id', v_existing.id
        );
END;
$$;


-- ─────────────────────────────────────────────────────────────────────────────
-- grant_credits — the only way credits are ever created.
--   Used by: the Razorpay webhook, the Apple receipt verifier, the welcome
--   grant trigger, referral payouts, and audited admin goodwill.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.grant_credits(
    p_user            UUID,
    p_credits         INT,
    p_source          TEXT,
    p_idempotency_key TEXT,
    p_expires_at      TIMESTAMPTZ DEFAULT NULL,
    p_purchase_id     UUID  DEFAULT NULL,
    p_note            TEXT  DEFAULT NULL,
    p_metadata        JSONB DEFAULT '{}'::jsonb,
    p_reference_type  TEXT  DEFAULT NULL,
    p_reference_id    TEXT  DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_existing public.credit_ledger%ROWTYPE;
    v_lot_id   UUID;
    v_balance  INT;
BEGIN
    IF p_credits <= 0 THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVALID_AMOUNT');
    END IF;

    SELECT * INTO v_existing
      FROM public.credit_ledger WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        RETURN jsonb_build_object('ok', true, 'replayed', true,
                                  'granted', v_existing.delta,
                                  'balance', v_existing.balance_after);
    END IF;

    PERFORM public.credits_ensure_account(p_user);

    INSERT INTO public.credit_lots (
        account_id, source, credits_granted, credits_remaining, expires_at, purchase_id, note
    ) VALUES (
        p_user, p_source, p_credits, p_credits, p_expires_at, p_purchase_id, p_note
    ) RETURNING id INTO v_lot_id;

    v_balance := public.credits_recompute(p_user);

    UPDATE public.credit_accounts
       SET lifetime_granted = lifetime_granted + p_credits
     WHERE wholesaler_id = p_user;

    INSERT INTO public.credit_ledger (
        account_id, delta, kind, reference_type, reference_id,
        idempotency_key, balance_after, metadata
    ) VALUES (
        p_user, p_credits, 'grant',
        COALESCE(p_reference_type,
                 CASE WHEN p_purchase_id IS NOT NULL THEN 'purchase' ELSE p_source END),
        COALESCE(p_reference_id, p_purchase_id::text),
        p_idempotency_key, v_balance,
        COALESCE(p_metadata, '{}'::jsonb)
            || jsonb_build_object('lot_id', v_lot_id, 'source', p_source)
    );

    RETURN jsonb_build_object('ok', true, 'granted', p_credits,
                              'balance', v_balance, 'lot_id', v_lot_id);

EXCEPTION
    WHEN unique_violation THEN
        SELECT * INTO v_existing
          FROM public.credit_ledger WHERE idempotency_key = p_idempotency_key;
        RETURN jsonb_build_object('ok', true, 'replayed', true,
                                  'granted', v_existing.delta,
                                  'balance', v_existing.balance_after);
END;
$$;


-- ─────────────────────────────────────────────────────────────────────────────
-- refund_credits — automatic give-back when the work failed.
--   Backend-only. A client that can call this is a free-credits API.
--
--   Refunds into a NEW lot; a spent lot is never resurrected. The new lot
--   inherits the earliest expiry the original spend consumed, so a refund
--   cannot quietly convert expiring promo credits into permanent ones — but
--   if that expiry has already passed, they get 30 fresh days rather than
--   credits that are dead on arrival.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.refund_credits(
    p_reference_type  TEXT,
    p_reference_id    TEXT,
    p_reason          TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_debit      public.credit_ledger%ROWTYPE;
    v_key        TEXT;
    v_expiry     TIMESTAMPTZ;
    v_has_never  BOOLEAN;
BEGIN
    -- The debit being reversed.
    SELECT * INTO v_debit
      FROM public.credit_ledger
     WHERE kind = 'debit'
       AND reference_type = p_reference_type
       AND reference_id   = p_reference_id
     ORDER BY created_at DESC
     LIMIT 1;

    IF NOT FOUND THEN
        -- Nothing was charged (free feature, or it failed before the debit).
        -- Not an error: refunding nothing is the correct outcome.
        RETURN jsonb_build_object('ok', true, 'refunded', 0, 'reason', 'NO_DEBIT_FOUND');
    END IF;

    v_key := 'refund:' || p_reference_type || ':' || p_reference_id;

    -- Work out the expiry to restore. A NULL among the allocations means part
    -- of the spend came from never-expiring credits, so the refund never expires.
    SELECT bool_or(alloc->>'expires_at' IS NULL),
           min((alloc->>'expires_at')::timestamptz)
      INTO v_has_never, v_expiry
      FROM jsonb_array_elements(COALESCE(v_debit.metadata->'allocations', '[]'::jsonb)) AS alloc;

    IF COALESCE(v_has_never, false) THEN
        v_expiry := NULL;
    ELSIF v_expiry IS NOT NULL AND v_expiry <= now() THEN
        v_expiry := now() + INTERVAL '30 days';
    END IF;

    RETURN public.grant_credits(
        p_user            => v_debit.account_id,
        p_credits         => -v_debit.delta,
        p_source          => 'refund',
        p_idempotency_key => v_key,
        p_expires_at      => v_expiry,
        p_note            => COALESCE(p_reason, 'Automatic refund'),
        p_reference_type  => p_reference_type,
        p_reference_id    => p_reference_id,
        p_metadata        => jsonb_build_object(
            'refund_of',   v_debit.id,
            'feature_key', v_debit.feature_key,
            'reason',      p_reason
        )
    );
END;
$$;


-- ─────────────────────────────────────────────────────────────────────────────
-- credits_expire_due — the nightly sweep.
--   Writes a visible ledger row per account. Credits are never silently
--   zeroed; "where did they go" must always have an answer.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.credits_expire_due()
RETURNS INT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_acct    RECORD;
    v_balance INT;
    v_count   INT := 0;
BEGIN
    FOR v_acct IN
        SELECT account_id, SUM(credits_remaining)::int AS expiring
          FROM public.credit_lots
         WHERE credits_remaining > 0
           AND expires_at IS NOT NULL
           AND expires_at <= now()
         GROUP BY account_id
    LOOP
        UPDATE public.credit_lots
           SET credits_remaining = 0
         WHERE account_id = v_acct.account_id
           AND credits_remaining > 0
           AND expires_at IS NOT NULL
           AND expires_at <= now();

        v_balance := public.credits_recompute(v_acct.account_id);

        UPDATE public.credit_accounts
           SET lifetime_expired = lifetime_expired + v_acct.expiring
         WHERE wholesaler_id = v_acct.account_id;

        INSERT INTO public.credit_ledger (
            account_id, delta, kind, idempotency_key, balance_after, metadata
        ) VALUES (
            v_acct.account_id, -v_acct.expiring, 'expiry',
            'expiry:' || v_acct.account_id || ':' || to_char(now(), 'YYYY-MM-DD"T"HH24:MI:SS'),
            v_balance,
            jsonb_build_object('expired_credits', v_acct.expiring)
        );

        v_count := v_count + 1;
    END LOOP;

    RETURN v_count;
END;
$$;


-- ─────────────────────────────────────────────────────────────────────────────
-- credits_wallet — the read model the apps call.
--   SECURITY DEFINER but scoped to auth.uid(), so it can only ever return the
--   caller's own wallet. Composed server-side so iOS and web cannot disagree
--   about what "available" means.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.credits_wallet()
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_user    UUID := auth.uid();
    v_acct    public.credit_accounts%ROWTYPE;
    v_expiring INT;
    v_next_expiry TIMESTAMPTZ;
BEGIN
    IF v_user IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error', 'NOT_AUTHENTICATED');
    END IF;

    SELECT * INTO v_acct FROM public.credit_accounts WHERE wholesaler_id = v_user;

    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'ok', true, 'available', 0, 'lifetime_spent', 0,
            'lifetime_expired', 0, 'expiring_soon', 0,
            'next_expiry', NULL, 'low_balance', true,
            'low_balance_threshold', 20
        );
    END IF;

    SELECT COALESCE(SUM(credits_remaining), 0)::int, MIN(expires_at)
      INTO v_expiring, v_next_expiry
      FROM public.credit_lots
     WHERE account_id = v_user
       AND credits_remaining > 0
       AND expires_at IS NOT NULL
       AND expires_at <= now() + INTERVAL '7 days'
       AND expires_at > now();

    RETURN jsonb_build_object(
        'ok',                    true,
        'available',             v_acct.balance_cached,
        'lifetime_spent',        v_acct.lifetime_spent,
        'lifetime_granted',      v_acct.lifetime_granted,
        'lifetime_expired',      v_acct.lifetime_expired,
        'expiring_soon',         COALESCE(v_expiring, 0),
        'next_expiry',           v_next_expiry,
        'low_balance',           v_acct.balance_cached <= v_acct.low_balance_threshold,
        'low_balance_threshold', v_acct.low_balance_threshold,
        'recovery_owed',         v_acct.recovery_owed
    );
END;
$$;


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
