-- Part 2 of 3 of migration 004 (Treasure Chest).
-- Run these IN ORDER: 004a, then 004b, then 004c.
-- Splitting them means a failure names the part that broke instead of
-- rolling back all 800 lines with one message.

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
