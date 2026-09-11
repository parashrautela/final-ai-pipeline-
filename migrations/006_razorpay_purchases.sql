-- ============================================================================
-- Migration 006 — Razorpay purchases, one pack list, ledger hardening
-- ============================================================================
-- Run AFTER 004 (004a → 004b → 004c) and 005. Safe to re-run: every object is
-- CREATE ... IF NOT EXISTS / CREATE OR REPLACE, and the seed never overwrites a
-- tuned price. Wrapped in one transaction, so it applies completely or not at
-- all.
--
-- What it adds
--   1. credit_packs            — the ONE server-side pack list (apps read it,
--                                the Razorpay webhook grants from it).
--   2. credit_purchase_issues  — payments the webhook could not match or grant,
--                                kept for a human. A payment is never dropped.
--   3. record_razorpay_purchase(...) — purchase row + credit grant in ONE
--                                transaction, deduped on the Razorpay payment id.
--   4. razorpay_find_wholesaler(...) / record_razorpay_issue(...) — the
--                                webhook's lookup and its manual-handling inbox.
--
-- What it fixes (CREATE OR REPLACE, identical signatures — callers unchanged)
--   5. spend_credits — a replayed idempotency key used to come back
--      `ok, replayed` whoever sent it and whatever it was for, so one paid key
--      unlocked unlimited free generations. A replay is now only honoured for
--      the SAME wallet and the SAME reference; anything else is
--      IDEMPOTENCY_CONFLICT. Replays also report whether that charge was since
--      refunded, so the pipeline can tell "retry of a live charge" (start no
--      new work) from "retry after a refunded failure" (bill a fresh attempt).
--   6. grant_credits — refunds were written as kind 'grant' and inflated
--      lifetime_granted. They are now kind 'refund', do not count as granted,
--      and net off lifetime_spent (so balance = granted − spent − expired
--      still holds).
--   7. refund_credits — its key was 'refund:<type>:<id>', so a generation could
--      be refunded only ONCE: the second failed re-roll on the same row was
--      silently never refunded. Refunds are now keyed to the debit ledger row
--      ('refund:<debit id>'). New refund_debit(id) refunds one exact debit.
--
-- Every function here is SECURITY DEFINER (or internal) with a pinned
-- search_path, and EXECUTE is revoked from PUBLIC / anon / authenticated at the
-- bottom of THIS file. Supabase grants EXECUTE on every new function to anon
-- and authenticated by default — a forgotten REVOKE is a public free-credits
-- API.
-- ============================================================================

BEGIN;


-- ─────────────────────────────────────────────────────────────────────────────
-- 1. PACKS — one list, read by the apps, granted from by the webhook
--    Prices are EXCLUSIVE of GST (TREASURE_CHEST_BUILD_PLAN.md §2.5).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.credit_packs (
    key              TEXT PRIMARY KEY CHECK (key ~ '^[a-z0-9_]+$'),
    label            TEXT NOT NULL,
    credits          INT  NOT NULL CHECK (credits > 0),
    price_inr_ex_gst NUMERIC(10,2) NOT NULL CHECK (price_inr_ex_gst > 0),
    active           BOOLEAN NOT NULL DEFAULT true,
    sort             INT  NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO public.credit_packs (key, label, credits, price_inr_ex_gst, sort) VALUES
    ('starter', 'Starter',   50,  499.00, 10),
    ('popular', 'Popular',  220, 1999.00, 20),
    ('pro',     'Pro',      600, 4999.00, 30),
    ('bulk',    'Bulk',    1300, 9999.00, 40)
ON CONFLICT (key) DO UPDATE
    SET label      = EXCLUDED.label,
        sort       = EXCLUDED.sort,
        updated_at = now();
-- Like the credit_prices seed in 004: re-running this never resets `credits`,
-- `price_inr_ex_gst` or `active` — a pack tuned in prod stays tuned.

ALTER TABLE public.credit_packs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "packs are readable when signed in" ON public.credit_packs;
CREATE POLICY "packs are readable when signed in" ON public.credit_packs
    FOR SELECT TO authenticated USING (active);


-- ─────────────────────────────────────────────────────────────────────────────
-- 2. ISSUES — paid, but not granted automatically. A human resolves these.
--    Service-role only: RLS on with NO policies, and no client grants.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.credit_purchase_issues (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    razorpay_payment_id TEXT NOT NULL UNIQUE,
    razorpay_link_id    TEXT,
    event               TEXT,
    reason              TEXT NOT NULL,
    raw_payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at         TIMESTAMPTZ,
    resolved_note       TEXT
);

CREATE INDEX IF NOT EXISTS idx_credit_purchase_issues_open
    ON public.credit_purchase_issues (created_at DESC)
    WHERE resolved_at IS NULL;

ALTER TABLE public.credit_purchase_issues ENABLE ROW LEVEL SECURITY;


-- ─────────────────────────────────────────────────────────────────────────────
-- 3. grant_credits — refunds become kind 'refund' (fix 6)
--    Same signature and defaults as 004b. Only the two refund branches changed.
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
    v_existing  public.credit_ledger%ROWTYPE;
    v_lot_id    UUID;
    v_balance   INT;
    -- A refund gives back credits that were already granted once, so it is
    -- not new money in: it must not inflate lifetime_granted.
    v_is_refund BOOLEAN := (p_source = 'refund');
BEGIN
    IF p_credits IS NULL OR p_credits <= 0 THEN
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

    IF v_is_refund THEN
        -- Net the refund off what was spent, so "lifetime spent" means credits
        -- actually consumed by work that completed.
        UPDATE public.credit_accounts
           SET lifetime_spent = GREATEST(lifetime_spent - p_credits, 0)
         WHERE wholesaler_id = p_user;
    ELSE
        UPDATE public.credit_accounts
           SET lifetime_granted = lifetime_granted + p_credits
         WHERE wholesaler_id = p_user;
    END IF;

    INSERT INTO public.credit_ledger (
        account_id, delta, kind, reference_type, reference_id,
        idempotency_key, balance_after, metadata
    ) VALUES (
        p_user, p_credits,
        CASE WHEN v_is_refund THEN 'refund' ELSE 'grant' END,
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
-- 4a. refund_debit — give back ONE exact debit (new; fix 7)
--     Keyed 'refund:<debit id>', so each debit can be refunded once — and
--     every debit on a generation can be, not just the first one.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.refund_debit(
    p_debit_id UUID,
    p_reason   TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_debit     public.credit_ledger%ROWTYPE;
    v_expiry    TIMESTAMPTZ;
    v_has_never BOOLEAN;
    v_result    JSONB;
    v_paid_back BOOLEAN;
BEGIN
    SELECT * INTO v_debit
      FROM public.credit_ledger
     WHERE id = p_debit_id AND kind = 'debit';

    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', true, 'refunded', 0, 'reason', 'NO_DEBIT_FOUND');
    END IF;

    -- Already refunded? Refunds written before this migration used the
    -- per-reference key 'refund:<type>:<id>' and kind 'grant', but always named
    -- the debit they reversed in metadata.refund_of — check both, so an old
    -- refund is never paid a second time under the new key.
    IF EXISTS (SELECT 1 FROM public.credit_ledger
                WHERE idempotency_key = 'refund:' || v_debit.id::text)
       OR EXISTS (SELECT 1 FROM public.credit_ledger
                   WHERE reference_type = v_debit.reference_type
                     AND reference_id   = v_debit.reference_id
                     AND metadata->>'refund_of' = v_debit.id::text)
    THEN
        RETURN jsonb_build_object('ok', true, 'replayed', true, 'refunded', 0,
                                  'refund_of', v_debit.id);
    END IF;

    -- The expiry to restore (unchanged from 004): a NULL among the allocations
    -- means part of the spend came from never-expiring credits, so the refund
    -- never expires; an already-past expiry gets 30 fresh days.
    SELECT bool_or(alloc->>'expires_at' IS NULL),
           min((alloc->>'expires_at')::timestamptz)
      INTO v_has_never, v_expiry
      FROM jsonb_array_elements(COALESCE(v_debit.metadata->'allocations', '[]'::jsonb)) AS alloc;

    IF COALESCE(v_has_never, false) THEN
        v_expiry := NULL;
    ELSIF v_expiry IS NOT NULL AND v_expiry <= now() THEN
        v_expiry := now() + INTERVAL '30 days';
    END IF;

    v_result := public.grant_credits(
        p_user            => v_debit.account_id,
        p_credits         => -v_debit.delta,
        p_source          => 'refund',
        p_idempotency_key => 'refund:' || v_debit.id::text,
        p_expires_at      => v_expiry,
        p_note            => COALESCE(p_reason, 'Automatic refund'),
        p_reference_type  => v_debit.reference_type,
        p_reference_id    => v_debit.reference_id,
        p_metadata        => jsonb_build_object(
            'refund_of',   v_debit.id,
            'feature_key', v_debit.feature_key,
            'reason',      p_reason
        )
    );

    v_paid_back := COALESCE((v_result->>'ok')::boolean, false)
               AND NOT COALESCE((v_result->>'replayed')::boolean, false);

    RETURN v_result || jsonb_build_object(
        'refund_of', v_debit.id,
        'refunded',  CASE WHEN v_paid_back THEN -v_debit.delta ELSE 0 END
    );
END;
$$;


-- ─────────────────────────────────────────────────────────────────────────────
-- 4b. refund_credits — same signature as 004; now refunds the LATEST debit on
--     the reference through refund_debit (per-debit key). Kept for callers
--     that only know the reference, e.g. app/services/chamak.py when the ledger
--     predates this migration.
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
    v_debit_id UUID;
BEGIN
    SELECT id INTO v_debit_id
      FROM public.credit_ledger
     WHERE kind = 'debit'
       AND reference_type = p_reference_type
       AND reference_id   = p_reference_id
     ORDER BY created_at DESC, id DESC
     LIMIT 1;

    IF NOT FOUND THEN
        -- Nothing was charged (free feature, or it failed before the debit).
        -- Not an error: refunding nothing is the correct outcome.
        RETURN jsonb_build_object('ok', true, 'refunded', 0, 'reason', 'NO_DEBIT_FOUND');
    END IF;

    RETURN public.refund_debit(v_debit_id, p_reason);
END;
$$;


-- ─────────────────────────────────────────────────────────────────────────────
-- 5a. credits_spend_replay — internal helper for spend_credits (fix 5)
--     Decides what an idempotency-key replay is allowed to mean.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.credits_spend_replay(
    p_existing       public.credit_ledger,
    p_user           UUID,
    p_reference_type TEXT,
    p_reference_id   TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SET search_path = public, pg_temp
AS $$
DECLARE
    v_refunded   BOOLEAN;
    v_superseded BOOLEAN;
BEGIN
    -- A key is only a replay of the SAME charge: same wallet, same thing paid
    -- for, and it must actually be a debit. Anything else — another user's key,
    -- a key from a different generation, a key that names a grant/refund row —
    -- is a conflict, never a free pass.
    IF p_existing.kind IS DISTINCT FROM 'debit'
       OR p_existing.account_id IS DISTINCT FROM p_user
       OR p_existing.reference_type IS DISTINCT FROM p_reference_type
       OR p_existing.reference_id IS DISTINCT FROM p_reference_id
    THEN
        RETURN jsonb_build_object('ok', false, 'error', 'IDEMPOTENCY_CONFLICT');
    END IF;

    -- Was this charge given back (its work failed)? Checks the 006 key and the
    -- pre-006 refund shape (metadata.refund_of).
    v_refunded :=
        EXISTS (SELECT 1 FROM public.credit_ledger r
                 WHERE r.idempotency_key = 'refund:' || p_existing.id::text)
        OR EXISTS (SELECT 1 FROM public.credit_ledger r
                    WHERE r.reference_type = p_existing.reference_type
                      AND r.reference_id   = p_existing.reference_id
                      AND r.metadata->>'refund_of' = p_existing.id::text);

    -- Has a later charge on the same reference replaced this one?
    v_superseded :=
        EXISTS (SELECT 1 FROM public.credit_ledger l
                 WHERE l.kind = 'debit'
                   AND l.reference_type = p_existing.reference_type
                   AND l.reference_id   = p_existing.reference_id
                   AND l.created_at     > p_existing.created_at);

    RETURN jsonb_build_object(
        'ok',          true,
        'replayed',    true,
        'charged',     -p_existing.delta,
        'balance',     p_existing.balance_after,
        'ledger_id',   p_existing.id,
        'feature_key', p_existing.feature_key,
        'created_at',  p_existing.created_at,
        'refunded',    v_refunded,
        'superseded',  v_superseded
    );
END;
$$;


-- ─────────────────────────────────────────────────────────────────────────────
-- 5b. spend_credits — same signature and pricing logic as 004b (fix 5)
--
--   Returns, on success:  {ok:true, charged, balance, ledger_id, created_at}
--   Returns, when broke:  {ok:false, error:'INSUFFICIENT_CREDITS', required,
--                          balance, short_by}
--   Returns, on replay:   {ok:true, replayed:true, charged, balance, ledger_id,
--                          created_at, refunded, superseded, feature_key}
--                         — ONLY when the existing row is a debit on the same
--                         wallet and the same reference.
--   Returns, otherwise:   {ok:false, error:'IDEMPOTENCY_CONFLICT'}
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
    v_ledger_id UUID;
    v_created   TIMESTAMPTZ;
BEGIN
    -- Without a key there is no replay protection at all (UNIQUE ignores NULL).
    IF p_idempotency_key IS NULL OR p_idempotency_key = '' THEN
        RETURN jsonb_build_object('ok', false, 'error', 'MISSING_IDEMPOTENCY_KEY');
    END IF;

    -- 1. Idempotent replay — but only of the SAME charge (see the helper).
    SELECT * INTO v_existing
      FROM public.credit_ledger
     WHERE idempotency_key = p_idempotency_key;

    IF FOUND THEN
        RETURN public.credits_spend_replay(v_existing, p_user, p_reference_type, p_reference_id);
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

    -- 3. Free feature (cost 0) — nothing to charge, nothing written.
    IF v_cost = 0 THEN
        SELECT balance_cached INTO v_balance
          FROM public.credit_accounts WHERE wholesaler_id = p_user;
        RETURN jsonb_build_object('ok', true, 'charged', 0,
                                  'balance', COALESCE(v_balance, 0), 'free', true);
    END IF;

    -- 4. Lock this wholesaler's live lots, then total them.
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

    -- 5. Consume lots expiring-soonest-first.
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
    )
    RETURNING id, created_at INTO v_ledger_id, v_created;

    -- ledger_id lets the caller refund exactly THIS debit if its work fails.
    RETURN jsonb_build_object('ok', true, 'charged', v_cost, 'balance', v_balance,
                              'ledger_id', v_ledger_id, 'created_at', v_created);

EXCEPTION
    -- Two identical calls raced past the replay check; the unique index
    -- rejected the loser, and this block's lot updates rolled back with it.
    -- Answer exactly as a replay would — including the same-charge check.
    WHEN unique_violation THEN
        SELECT * INTO v_existing
          FROM public.credit_ledger WHERE idempotency_key = p_idempotency_key;
        IF NOT FOUND THEN
            RAISE;
        END IF;
        RETURN public.credits_spend_replay(v_existing, p_user, p_reference_type, p_reference_id);
END;
$$;


-- ─────────────────────────────────────────────────────────────────────────────
-- 6. credits_normalize_in_phone — Indian mobile → its 10 digits, or NULL
--    Accepts +91 / 91 / 0 / 0091 prefixes, spaces, dashes, brackets.
--    Mirrors normalizeIndianMobile() in the webhook's lib.ts.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.credits_normalize_in_phone(p_raw TEXT)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
SET search_path = public, pg_temp
AS $$
    SELECT substring(
        regexp_replace(COALESCE(p_raw, ''), '[^0-9]', '', 'g')
        FROM '^(?:0091|091|91|0)?([6-9][0-9]{9})$'
    );
$$;


-- ─────────────────────────────────────────────────────────────────────────────
-- 7. razorpay_find_wholesaler — every wholesaler a payment could belong to
--
--   Returns one row per (wholesaler, reason it matched). The webhook decides:
--   exactly one wholesaler → grant; none or several → manual handling.
--
--   wholesaler_user_id is wholesalers.user_id — the auth uid that
--   credit_accounts is keyed on — whichever way the wholesaler was found.
--
--   Phones are matched against auth.users.phone (where sign-up puts them) and
--   against wholesalers.email, because the iOS onboarding writes
--   `user.email ?? user.phone` into that column for phone-OTP sign-ups.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.razorpay_find_wholesaler(
    p_wholesaler_ref TEXT DEFAULT NULL,
    p_phone          TEXT DEFAULT NULL,
    p_email          TEXT DEFAULT NULL
)
RETURNS TABLE (wholesaler_user_id UUID, matched_on TEXT, wholesaler_state TEXT)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_ref   UUID;
    v_phone TEXT := public.credits_normalize_in_phone(p_phone);
    v_email TEXT := NULLIF(lower(btrim(COALESCE(p_email, ''))), '');
BEGIN
    -- Hand-typed, so tolerate anything: a non-UUID simply matches nothing.
    BEGIN
        v_ref := NULLIF(btrim(COALESCE(p_wholesaler_ref, '')), '')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        v_ref := NULL;
    END;

    -- (a) the id the team typed into the link's notes: wholesalers.user_id
    --     (the auth uid) or wholesalers.id (what the admin panel shows).
    IF v_ref IS NOT NULL THEN
        RETURN QUERY
            SELECT w.user_id, 'wholesaler_id'::text, w.state
              FROM public.wholesalers w
             WHERE w.user_id IS NOT NULL
               AND (w.user_id = v_ref OR w.id = v_ref);
    END IF;

    -- (b) the link's customer phone.
    IF v_phone IS NOT NULL THEN
        RETURN QUERY
            SELECT DISTINCT w.user_id, 'phone'::text, w.state
              FROM public.wholesalers w
              LEFT JOIN auth.users u ON u.id = w.user_id
             WHERE w.user_id IS NOT NULL
               AND (public.credits_normalize_in_phone(u.phone) = v_phone
                    OR (strpos(COALESCE(w.email, ''), '@') = 0
                        AND public.credits_normalize_in_phone(w.email) = v_phone));
    END IF;

    -- (b) the link's customer email.
    IF v_email IS NOT NULL THEN
        RETURN QUERY
            SELECT DISTINCT w.user_id, 'email'::text, w.state
              FROM public.wholesalers w
              LEFT JOIN auth.users u ON u.id = w.user_id
             WHERE w.user_id IS NOT NULL
               AND (lower(btrim(w.email)) = v_email
                    OR lower(btrim(u.email)) = v_email);
    END IF;
END;
$$;


-- ─────────────────────────────────────────────────────────────────────────────
-- 8. record_razorpay_purchase — purchase row + grant, ONE transaction
--
--   Deduped on the Razorpay payment id (credit_purchases.provider_txn_id is
--   UNIQUE): Razorpay retries webhooks, and a replay must grant nothing.
--   Purchased credits never expire. If the grant is refused the function
--   RAISEs, which rolls the purchase row back with it.
--
--   Money columns: amount_inr is the TAXABLE value (ex-GST) and gst_inr the
--   tax, as credit_purchases documents them — so amount_inr + gst_inr is
--   exactly what was paid.
--
--   Also the manual path: after fixing up a credit_purchase_issues row, run
--   this from the SQL editor with the right p_user; it resolves the issue.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.record_razorpay_purchase(
    p_user         UUID,
    p_payment_id   TEXT,
    p_credits      INT,
    p_pack_key     TEXT,
    p_amount_inr   NUMERIC,
    p_gst_inr      NUMERIC,
    p_provider_ref TEXT  DEFAULT NULL,
    p_buyer_gstin  TEXT  DEFAULT NULL,
    p_buyer_state  TEXT  DEFAULT NULL,
    p_receipt_json JSONB DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_payment_id  TEXT := btrim(COALESCE(p_payment_id, ''));
    v_pack_key    TEXT := COALESCE(NULLIF(btrim(COALESCE(p_pack_key, '')), ''), 'custom');
    v_purchase_id UUID;
    v_existing    public.credit_purchases%ROWTYPE;
    v_grant       JSONB;
BEGIN
    IF p_user IS NULL OR v_payment_id = '' THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVALID_ARGUMENTS');
    END IF;
    IF p_credits IS NULL OR p_credits <= 0 THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVALID_AMOUNT');
    END IF;

    PERFORM public.credits_ensure_account(p_user);

    INSERT INTO public.credit_purchases (
        account_id, provider, provider_txn_id, provider_ref, pack_key, credits,
        amount_inr, gst_inr, buyer_gstin, buyer_state, status, receipt_json, settled_at
    ) VALUES (
        p_user, 'razorpay', v_payment_id, NULLIF(btrim(COALESCE(p_provider_ref, '')), ''),
        v_pack_key, p_credits,
        p_amount_inr, p_gst_inr,
        NULLIF(upper(btrim(COALESCE(p_buyer_gstin, ''))), ''),
        NULLIF(btrim(COALESCE(p_buyer_state, '')), ''),
        'paid', p_receipt_json, now()
    )
    ON CONFLICT (provider_txn_id) DO NOTHING
    RETURNING id INTO v_purchase_id;

    IF v_purchase_id IS NULL THEN
        -- Already recorded: a webhook retry. Grant nothing.
        SELECT * INTO v_existing
          FROM public.credit_purchases WHERE provider_txn_id = v_payment_id;
        RETURN jsonb_build_object(
            'ok', true, 'replayed', true,
            'purchase_id', v_existing.id,
            'account_id',  v_existing.account_id,
            'credits',     v_existing.credits,
            'status',      v_existing.status
        );
    END IF;

    v_grant := public.grant_credits(
        p_user            => p_user,
        p_credits         => p_credits,
        p_source          => 'purchase',
        p_idempotency_key => 'razorpay:' || v_payment_id,
        p_expires_at      => NULL,          -- purchased credits never expire
        p_purchase_id     => v_purchase_id,
        p_note            => 'Razorpay ' || v_pack_key || ' (' || v_payment_id || ')',
        p_metadata        => jsonb_build_object(
            'provider',     'razorpay',
            'payment_id',   v_payment_id,
            'provider_ref', p_provider_ref,
            'pack_key',     v_pack_key
        )
    );

    IF NOT COALESCE((v_grant->>'ok')::boolean, false) THEN
        RAISE EXCEPTION 'record_razorpay_purchase: grant refused for payment %: %',
            v_payment_id, v_grant;
    END IF;

    -- Any open manual-handling ticket for this payment is now settled.
    UPDATE public.credit_purchase_issues
       SET resolved_at   = now(),
           resolved_note = COALESCE(resolved_note, 'Credits granted by record_razorpay_purchase'),
           updated_at    = now()
     WHERE razorpay_payment_id = v_payment_id
       AND resolved_at IS NULL;

    RETURN jsonb_build_object(
        'ok',          true,
        'replayed',    false,
        'purchase_id', v_purchase_id,
        'credits',     p_credits,
        'balance',     v_grant->'balance',
        'lot_id',      v_grant->'lot_id',
        'grant',       v_grant
    );
END;
$$;


-- ─────────────────────────────────────────────────────────────────────────────
-- 9. record_razorpay_issue — the manual-handling inbox
--    Upserts by payment id and keeps the latest reason while unresolved. A
--    payment that has meanwhile been granted is not an issue.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.record_razorpay_issue(
    p_payment_id TEXT,
    p_link_id    TEXT,
    p_event      TEXT,
    p_reason     TEXT,
    p_payload    JSONB DEFAULT '{}'::jsonb
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_payment_id TEXT := btrim(COALESCE(p_payment_id, ''));
    v_issue_id   UUID;
BEGIN
    IF v_payment_id = '' THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVALID_ARGUMENTS');
    END IF;

    IF EXISTS (SELECT 1 FROM public.credit_purchases WHERE provider_txn_id = v_payment_id) THEN
        RETURN jsonb_build_object('ok', true, 'recorded', false, 'reason', 'ALREADY_GRANTED');
    END IF;

    INSERT INTO public.credit_purchase_issues AS i (
        razorpay_payment_id, razorpay_link_id, event, reason, raw_payload
    ) VALUES (
        v_payment_id, NULLIF(btrim(COALESCE(p_link_id, '')), ''), p_event,
        COALESCE(NULLIF(btrim(COALESCE(p_reason, '')), ''), 'unspecified'),
        COALESCE(p_payload, '{}'::jsonb)
    )
    ON CONFLICT (razorpay_payment_id) DO UPDATE
        SET reason           = EXCLUDED.reason,
            raw_payload      = EXCLUDED.raw_payload,
            event            = EXCLUDED.event,
            razorpay_link_id = COALESCE(EXCLUDED.razorpay_link_id, i.razorpay_link_id),
            updated_at       = now()
        WHERE i.resolved_at IS NULL
    RETURNING i.id INTO v_issue_id;

    RETURN jsonb_build_object('ok', true, 'recorded', v_issue_id IS NOT NULL,
                              'issue_id', v_issue_id);
END;
$$;


-- ============================================================================
-- PRIVILEGES
-- ============================================================================

-- Tables. Supabase's default privileges hand every new public table to anon
-- and authenticated; take that back, then grant exactly what is meant.
REVOKE ALL ON public.credit_packs           FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.credit_purchase_issues FROM PUBLIC, anon, authenticated;

GRANT SELECT ON public.credit_packs TO authenticated;          -- RLS: active packs only
GRANT SELECT, INSERT, UPDATE, DELETE ON public.credit_packs  TO service_role;
GRANT SELECT, INSERT, UPDATE ON public.credit_purchase_issues TO service_role;

-- New functions: service_role only (the two helpers are internal — no grant).
REVOKE ALL ON FUNCTION public.credits_spend_replay(public.credit_ledger, UUID, TEXT, TEXT)                 FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.credits_normalize_in_phone(TEXT)                                              FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.refund_debit(UUID, TEXT)                                                      FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.razorpay_find_wholesaler(TEXT, TEXT, TEXT)                                    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.record_razorpay_purchase(UUID, TEXT, INT, TEXT, NUMERIC, NUMERIC, TEXT, TEXT, TEXT, JSONB) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.record_razorpay_issue(TEXT, TEXT, TEXT, TEXT, JSONB)                          FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.refund_debit(UUID, TEXT)                                                   TO service_role;
GRANT EXECUTE ON FUNCTION public.razorpay_find_wholesaler(TEXT, TEXT, TEXT)                                 TO service_role;
GRANT EXECUTE ON FUNCTION public.record_razorpay_purchase(UUID, TEXT, INT, TEXT, NUMERIC, NUMERIC, TEXT, TEXT, TEXT, JSONB) TO service_role;
GRANT EXECUTE ON FUNCTION public.record_razorpay_issue(TEXT, TEXT, TEXT, TEXT, JSONB)                       TO service_role;

-- Re-assert 004c for the functions this file replaced or builds on. CREATE OR
-- REPLACE keeps existing grants, but if 004c never ran these would still be
-- open to anon — restating it here costs nothing.
REVOKE ALL ON FUNCTION public.spend_credits(UUID, TEXT, TEXT, TEXT, TEXT, JSONB)                            FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.grant_credits(UUID, INT, TEXT, TEXT, TIMESTAMPTZ, UUID, TEXT, JSONB, TEXT, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.refund_credits(TEXT, TEXT, TEXT)                                              FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.credits_expire_due()                                                          FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.credits_ensure_account(UUID)                                                  FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.credits_recompute(UUID)                                                       FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.spend_credits(UUID, TEXT, TEXT, TEXT, TEXT, JSONB)                         TO service_role;
GRANT EXECUTE ON FUNCTION public.grant_credits(UUID, INT, TEXT, TEXT, TIMESTAMPTZ, UUID, TEXT, JSONB, TEXT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.refund_credits(TEXT, TEXT, TEXT)                                           TO service_role;
GRANT EXECUTE ON FUNCTION public.credits_expire_due()                                                       TO service_role;

COMMIT;

-- Make the new RPCs visible to PostgREST immediately.
NOTIFY pgrst, 'reload schema';


-- ============================================================================
-- Verify (read-only)
-- ============================================================================
-- Every row should say false:
-- SELECT p.oid::regprocedure AS fn, r AS role,
--        has_function_privilege(r, p.oid, 'EXECUTE') AS can_execute
--   FROM pg_proc p
--   JOIN pg_namespace n ON n.oid = p.pronamespace AND n.nspname = 'public'
--  CROSS JOIN unnest(ARRAY['anon', 'authenticated']) AS r
--  WHERE p.proname IN ('spend_credits', 'grant_credits', 'refund_credits',
--                      'refund_debit', 'credits_expire_due', 'credits_ensure_account',
--                      'credits_recompute', 'credits_spend_replay',
--                      'credits_normalize_in_phone', 'razorpay_find_wholesaler',
--                      'record_razorpay_purchase', 'record_razorpay_issue')
--  ORDER BY 1, 2;
--
-- SELECT key, label, credits, price_inr_ex_gst, active FROM public.credit_packs ORDER BY sort;
-- SELECT * FROM public.credit_purchase_issues WHERE resolved_at IS NULL ORDER BY created_at DESC;
-- ============================================================================
