-- Requires credit migrations through 008 and the iOS retailer wallet/plan/IAP migrations.
-- Additive deployment: daily mode stays OFF until credits_activate_daily() passes its paid-balance check.
BEGIN;
CREATE TABLE public.credit_program (
  singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK(singleton),
  daily_enabled BOOLEAN NOT NULL DEFAULT false,
  payments_enabled BOOLEAN NOT NULL DEFAULT false,
  payments_disabled_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  daily_allowance INT NOT NULL DEFAULT 2000 CHECK(daily_allowance = 2000),
  activated_at TIMESTAMPTZ
);
INSERT INTO public.credit_program DEFAULT VALUES;
ALTER TABLE public.credit_program ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.credit_program FROM anon, authenticated;
GRANT SELECT, UPDATE ON public.credit_program TO service_role;

ALTER TABLE public.credit_lots DROP CONSTRAINT credit_lots_source_check;
ALTER TABLE public.credit_lots ADD CONSTRAINT credit_lots_source_check CHECK
  (source IN ('welcome','purchase','subscription','referral','promo','refund','admin','daily'));
ALTER TABLE public.credit_lots ADD COLUMN budget_date DATE;
ALTER TABLE public.credit_lots ADD COLUMN archived_at TIMESTAMPTZ;
ALTER TABLE public.credit_lots ADD CONSTRAINT credit_lots_daily_date_check
  CHECK((source = 'daily') = (budget_date IS NOT NULL));
CREATE UNIQUE INDEX credit_lots_daily_once ON public.credit_lots(account_id, budget_date) WHERE source = 'daily';

CREATE FUNCTION public.credits_program_status() RETURNS JSONB
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT jsonb_build_object('daily_enabled', daily_enabled, 'payments_enabled', payments_enabled,
    'daily_allowance', daily_allowance, 'timezone', 'Asia/Kolkata', 'activated_at', activated_at,
    'payments_disabled_at', payments_disabled_at)
  FROM public.credit_program WHERE singleton;
$$;
REVOKE ALL ON FUNCTION public.credits_program_status() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.credits_program_status() TO anon, authenticated, service_role;

CREATE FUNCTION public.credits_resolve_owner(p_actor UUID) RETURNS UUID
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT owner FROM (
    SELECT user_id AS owner, 1 AS priority FROM public.wholesalers
      WHERE user_id = p_actor AND verification_status = 'verified'
    UNION ALL
    SELECT user_id, 2 FROM public.retailers WHERE user_id = p_actor AND verification_status = 'verified'
    UNION ALL
    SELECT r.user_id, 3 FROM public.employees e JOIN public.retailers r ON r.id = e.retailer_id
      WHERE e.auth_user_id = p_actor AND e.status = 'active' AND r.verification_status = 'verified'
  ) candidates ORDER BY priority LIMIT 1;
$$;

CREATE FUNCTION public.credits_my_audience() RETURNS TEXT
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT CASE WHEN EXISTS(SELECT 1 FROM public.retailers WHERE user_id = public.credits_resolve_owner(auth.uid()))
    THEN 'retailer' WHEN EXISTS(SELECT 1 FROM public.wholesalers WHERE user_id = public.credits_resolve_owner(auth.uid()))
    THEN 'wholesaler' ELSE 'none' END;
$$;
REVOKE ALL ON FUNCTION public.credits_my_audience() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.credits_my_audience() TO anon, authenticated, service_role;
DROP POLICY IF EXISTS "rate card is readable" ON public.credit_prices;
CREATE POLICY "rate card is readable" ON public.credit_prices FOR SELECT
  USING(is_active AND (audience = 'all' OR audience = public.credits_my_audience()));

-- Activation cannot silently replace a real purchased balance.
CREATE FUNCTION public.credits_activate_daily() RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_affected INT;
BEGIN
  -- Every wallet mutation takes FOR SHARE on this row first. Activation waits for them.
  PERFORM 1 FROM public.credit_program WHERE singleton FOR UPDATE;
  IF (SELECT daily_enabled FROM public.credit_program WHERE singleton) THEN
    RETURN jsonb_build_object('ok', true, 'replayed', true);
  END IF;
  SELECT count(DISTINCT account_id)::int INTO v_affected FROM public.credit_lots
    WHERE credits_remaining > 0 AND (source = 'purchase' OR purchase_id IS NOT NULL);
  IF v_affected > 0 THEN
    RETURN jsonb_build_object('ok', false, 'error', 'LEGACY_PAID_BALANCES_REQUIRE_DECISION', 'affected_accounts', v_affected);
  END IF;
  UPDATE public.credit_program SET daily_enabled = true, payments_enabled = false,
    activated_at = clock_timestamp() WHERE singleton;
  RETURN jsonb_build_object('ok', true, 'daily_allowance', 2000);
END;
$$;

CREATE OR REPLACE FUNCTION public.credits_recompute(p_user UUID) RETURNS INT
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE v_balance INT; v_daily BOOLEAN; v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
  SELECT daily_enabled INTO v_daily FROM public.credit_program WHERE singleton;
  SELECT coalesce(sum(credits_remaining), 0)::int INTO v_balance FROM public.credit_lots
    WHERE account_id = p_user AND archived_at IS NULL AND credits_remaining > 0
    AND (expires_at IS NULL OR expires_at > v_now) AND (NOT v_daily OR source = 'daily');
  UPDATE public.credit_accounts SET balance_cached = v_balance, updated_at = v_now,
    low_balance_notified_at = CASE WHEN v_balance > low_balance_threshold THEN NULL ELSE low_balance_notified_at END
    WHERE wholesaler_id = p_user;
  RETURN v_balance;
END;
$$;

CREATE FUNCTION public.credits_ensure_daily_budget(p_user UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_daily BOOLEAN; v_now TIMESTAMPTZ; v_date DATE; v_end TIMESTAMPTZ;
  v_lot public.credit_lots%ROWTYPE; v_legacy INT; v_balance INT;
BEGIN
  SELECT daily_enabled INTO v_daily FROM public.credit_program WHERE singleton FOR SHARE;
  IF public.credits_resolve_owner(p_user) IS DISTINCT FROM p_user THEN
    RETURN jsonb_build_object('ok', false, 'error', 'NOT_VERIFIED');
  END IF;
  PERFORM public.credits_ensure_account(p_user);
  PERFORM 1 FROM public.credit_accounts WHERE wholesaler_id = p_user FOR UPDATE;
  IF public.credits_resolve_owner(p_user) IS DISTINCT FROM p_user THEN
    RETURN jsonb_build_object('ok',false,'error','NOT_VERIFIED');
  END IF;
  v_now := clock_timestamp(); -- Sample AFTER waiting for the account lock.
  v_date := (v_now AT TIME ZONE 'Asia/Kolkata')::date;
  v_end := (v_date + 1)::timestamp AT TIME ZONE 'Asia/Kolkata';
  IF NOT v_daily THEN
    RETURN jsonb_build_object('ok', true, 'mode', 'legacy', 'balance', public.credits_recompute(p_user));
  END IF;
  FOR v_lot IN SELECT * FROM public.credit_lots WHERE account_id = p_user AND source = 'daily'
    AND credits_remaining > 0 AND expires_at <= v_now FOR UPDATE LOOP
    UPDATE public.credit_lots SET credits_remaining = 0 WHERE id = v_lot.id;
    UPDATE public.credit_accounts SET lifetime_expired = lifetime_expired + v_lot.credits_remaining WHERE wholesaler_id = p_user;
    INSERT INTO public.credit_ledger(account_id, delta, kind, idempotency_key, balance_after, metadata)
      VALUES(p_user, -v_lot.credits_remaining, 'expiry', 'daily-expiry:' || v_lot.id, 0,
        jsonb_build_object('budget_date', v_lot.budget_date, 'source', 'daily'));
  END LOOP;
  SELECT coalesce(sum(credits_remaining), 0)::int INTO v_legacy FROM public.credit_lots
    WHERE account_id = p_user AND source <> 'daily' AND archived_at IS NULL AND credits_remaining > 0
      AND (expires_at IS NULL OR expires_at > v_now);
  UPDATE public.credit_lots SET archived_at = v_now
    WHERE account_id = p_user AND source <> 'daily' AND archived_at IS NULL;
  IF v_legacy > 0 THEN
    INSERT INTO public.credit_ledger(account_id, delta, kind, idempotency_key, balance_after, metadata)
      VALUES(p_user, -v_legacy, 'adjustment', 'daily-conversion:' || p_user, 0,
        jsonb_build_object('reason', 'Previous promotional balance preserved separately', 'legacy_delta', 0, 'preserved_legacy_units', v_legacy));
  END IF;
  IF NOT EXISTS(SELECT 1 FROM public.credit_lots WHERE account_id = p_user AND source = 'daily' AND budget_date = v_date) THEN
    INSERT INTO public.credit_lots(account_id, source, credits_granted, credits_remaining, expires_at, budget_date, note)
      VALUES(p_user, 'daily', 2000, 2000, v_end, v_date, 'Daily allowance');
    UPDATE public.credit_accounts SET lifetime_granted = lifetime_granted + 2000 WHERE wholesaler_id = p_user;
    INSERT INTO public.credit_ledger(account_id, delta, kind, reference_type, idempotency_key, balance_after, metadata)
      VALUES(p_user, 2000, 'grant', 'daily', 'daily:' || p_user || ':' || v_date, 2000,
        jsonb_build_object('source', 'daily', 'budget_date', v_date));
  END IF;
  v_balance := public.credits_recompute(p_user);
  RETURN jsonb_build_object('ok', true, 'mode', 'daily', 'balance', v_balance, 'budget_date', v_date,
    'daily_allowance', 2000, 'resets_at', v_end, 'server_now', v_now);
END;
$$;

ALTER FUNCTION public.spend_credits(UUID,TEXT,TEXT,TEXT,TEXT,JSONB) RENAME TO spend_credits_legacy;
CREATE FUNCTION public.spend_credits(p_user UUID, p_feature_key TEXT, p_idempotency_key TEXT,
  p_reference_type TEXT DEFAULT NULL, p_reference_id TEXT DEFAULT NULL, p_metadata JSONB DEFAULT '{}'::jsonb)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_owner UUID; v_daily BOOLEAN; v_budget JSONB; v_existing public.credit_ledger%ROWTYPE;
  v_lot public.credit_lots%ROWTYPE; v_cost INT; v_balance INT; v_ledger UUID; v_created TIMESTAMPTZ;
BEGIN
  SELECT daily_enabled INTO v_daily FROM public.credit_program WHERE singleton FOR SHARE;
  IF p_feature_key LIKE 'plan.%' AND NOT (SELECT payments_enabled FROM public.credit_program WHERE singleton) THEN
    RETURN jsonb_build_object('ok',false,'error','PLANS_PAUSED');
  END IF;
  IF NOT v_daily THEN
    PERFORM public.credits_ensure_account(p_user);
    PERFORM 1 FROM public.credit_accounts WHERE wholesaler_id = p_user FOR UPDATE;
    RETURN public.spend_credits_legacy(p_user,p_feature_key,p_idempotency_key,p_reference_type,p_reference_id,p_metadata);
  END IF;
  v_owner := public.credits_resolve_owner(p_user);
  IF v_owner IS NULL THEN RETURN jsonb_build_object('ok', false, 'error', 'NOT_VERIFIED'); END IF;
  v_budget := public.credits_ensure_daily_budget(v_owner);
  IF NOT coalesce((v_budget->>'ok')::boolean, false) THEN RETURN v_budget; END IF;
  IF public.credits_resolve_owner(p_user) IS DISTINCT FROM v_owner THEN
    RETURN jsonb_build_object('ok',false,'error','NOT_VERIFIED');
  END IF;
  IF p_idempotency_key IS NULL OR btrim(p_idempotency_key) = '' THEN
    RETURN jsonb_build_object('ok', false, 'error', 'MISSING_IDEMPOTENCY_KEY');
  END IF;
  SELECT * INTO v_existing FROM public.credit_ledger WHERE idempotency_key = p_idempotency_key;
  IF FOUND THEN
    IF v_existing.feature_key IS DISTINCT FROM p_feature_key OR v_existing.metadata->>'actor_id' IS DISTINCT FROM p_user::text THEN
      RETURN jsonb_build_object('ok', false, 'error', 'IDEMPOTENCY_CONFLICT');
    END IF;
    RETURN public.credits_spend_replay(v_existing,v_owner,p_reference_type,p_reference_id)
      || jsonb_build_object('balance', (v_budget->>'balance')::int, 'resets_at', v_budget->>'resets_at');
  END IF;
  IF p_feature_key LIKE 'plan.%' THEN RETURN jsonb_build_object('ok', false, 'error', 'PLANS_PAUSED'); END IF;
  SELECT credits INTO v_cost FROM public.credit_prices WHERE feature_key = p_feature_key AND is_active
    AND (audience = 'all' OR audience = CASE WHEN EXISTS(SELECT 1 FROM public.retailers WHERE user_id = v_owner)
      THEN 'retailer' ELSE 'wholesaler' END);
  IF NOT FOUND THEN RETURN jsonb_build_object('ok', false, 'error', 'UNKNOWN_FEATURE'); END IF;
  v_balance := (v_budget->>'balance')::int;
  IF v_cost = 0 THEN RETURN v_budget || jsonb_build_object('charged', 0, 'free', true); END IF;
  IF v_balance < v_cost THEN RETURN jsonb_build_object('ok', false, 'error', 'INSUFFICIENT_CREDITS',
    'required', v_cost, 'balance', v_balance, 'short_by', v_cost - v_balance, 'resets_at', v_budget->>'resets_at'); END IF;
  SELECT * INTO v_lot FROM public.credit_lots WHERE account_id = v_owner AND source = 'daily'
    AND budget_date = (v_budget->>'budget_date')::date FOR UPDATE;
  UPDATE public.credit_lots SET credits_remaining = credits_remaining - v_cost WHERE id = v_lot.id;
  v_balance := v_balance - v_cost;
  UPDATE public.credit_accounts SET balance_cached = v_balance, lifetime_spent = lifetime_spent + v_cost,
    updated_at = clock_timestamp() WHERE wholesaler_id = v_owner;
  INSERT INTO public.credit_ledger(account_id,delta,kind,feature_key,reference_type,reference_id,idempotency_key,balance_after,metadata)
    VALUES(v_owner,-v_cost,'debit',p_feature_key,p_reference_type,p_reference_id,p_idempotency_key,v_balance,
      coalesce(p_metadata,'{}'::jsonb) || jsonb_build_object('actor_id',p_user,'budget_date',v_lot.budget_date,'mode','daily',
        'allocations',jsonb_build_array(jsonb_build_object('lot_id',v_lot.id,'credits',v_cost,'expires_at',v_lot.expires_at))))
    RETURNING id,created_at INTO v_ledger,v_created;
  RETURN jsonb_build_object('ok',true,'charged',v_cost,'balance',v_balance,'ledger_id',v_ledger,
    'created_at',v_created,'resets_at',v_budget->>'resets_at');
EXCEPTION WHEN unique_violation THEN
  SELECT * INTO v_existing FROM public.credit_ledger WHERE idempotency_key = p_idempotency_key;
  IF NOT FOUND THEN RAISE; END IF;
  IF v_existing.feature_key IS DISTINCT FROM p_feature_key OR v_existing.metadata->>'actor_id' IS DISTINCT FROM p_user::text THEN
    RETURN jsonb_build_object('ok',false,'error','IDEMPOTENCY_CONFLICT');
  END IF;
  RETURN public.credits_spend_replay(v_existing,v_owner,p_reference_type,p_reference_id);
END;
$$;

ALTER FUNCTION public.grant_credits(UUID,INT,TEXT,TEXT,TIMESTAMPTZ,UUID,TEXT,JSONB,TEXT,TEXT) RENAME TO grant_credits_legacy;
CREATE FUNCTION public.grant_credits(p_user UUID, p_credits INT, p_source TEXT, p_idempotency_key TEXT,
  p_expires_at TIMESTAMPTZ DEFAULT NULL, p_purchase_id UUID DEFAULT NULL, p_note TEXT DEFAULT NULL,
  p_metadata JSONB DEFAULT '{}'::jsonb, p_reference_type TEXT DEFAULT NULL, p_reference_id TEXT DEFAULT NULL)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_daily BOOLEAN; v_balance INT; v_existing public.credit_ledger%ROWTYPE;
BEGIN
  SELECT daily_enabled INTO v_daily FROM public.credit_program WHERE singleton FOR SHARE;
  PERFORM public.credits_ensure_account(p_user);
  PERFORM 1 FROM public.credit_accounts WHERE wholesaler_id = p_user FOR UPDATE;
  IF NOT v_daily THEN RETURN public.grant_credits_legacy(p_user,p_credits,p_source,p_idempotency_key,
    p_expires_at,p_purchase_id,p_note,p_metadata,p_reference_type,p_reference_id); END IF;
  IF p_credits IS NULL OR p_credits <= 0 OR p_idempotency_key IS NULL OR btrim(p_idempotency_key) = '' THEN
    RETURN jsonb_build_object('ok',false,'error','INVALID_ARGUMENTS');
  END IF;
  SELECT * INTO v_existing FROM public.credit_ledger WHERE idempotency_key = p_idempotency_key;
  IF FOUND THEN
    IF v_existing.account_id IS DISTINCT FROM p_user THEN RETURN jsonb_build_object('ok',false,'error','IDEMPOTENCY_CONFLICT'); END IF;
    RETURN jsonb_build_object('ok',true,'replayed',true,'granted',0,'balance',public.credits_recompute(p_user));
  END IF;
  IF p_source NOT IN ('welcome','purchase','subscription','referral','promo','refund','admin') OR p_source IS NULL THEN
    RETURN jsonb_build_object('ok',false,'error','INVALID_SOURCE');
  END IF;
  -- Legacy payments/refunds remain accounted for, but are not today's allowance.
  IF p_source IN ('purchase','refund') THEN
    INSERT INTO public.credit_lots(account_id,source,credits_granted,credits_remaining,expires_at,purchase_id,note,archived_at)
      VALUES(p_user,p_source,p_credits,p_credits,p_expires_at,p_purchase_id,p_note,clock_timestamp());
    UPDATE public.credit_accounts SET lifetime_granted = lifetime_granted + CASE WHEN p_source = 'purchase' THEN p_credits ELSE 0 END,
      lifetime_spent = greatest(0,lifetime_spent - CASE WHEN p_source = 'refund' THEN p_credits ELSE 0 END) WHERE wholesaler_id = p_user;
  END IF;
  v_balance := public.credits_recompute(p_user);
  INSERT INTO public.credit_ledger(account_id,delta,kind,reference_type,reference_id,idempotency_key,balance_after,metadata)
    VALUES(p_user,0,CASE WHEN p_source = 'refund' THEN 'refund' ELSE 'adjustment' END,
      coalesce(p_reference_type,p_source),p_reference_id,p_idempotency_key,v_balance,
      coalesce(p_metadata,'{}'::jsonb) || jsonb_build_object('source',p_source,'daily_program',true,
        'legacy_delta',CASE WHEN p_source IN ('purchase','refund') THEN p_credits ELSE 0 END,
        'suppressed_units',CASE WHEN p_source IN ('purchase','refund') THEN 0 ELSE p_credits END));
  RETURN jsonb_build_object('ok',true,'granted',0,'balance',v_balance,'legacy_recorded',
    CASE WHEN p_source IN ('purchase','refund') THEN p_credits ELSE 0 END);
END;
$$;

ALTER FUNCTION public.refund_debit(UUID,TEXT) RENAME TO refund_debit_legacy;
CREATE FUNCTION public.refund_debit(p_debit_id UUID,p_reason TEXT DEFAULT NULL) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_daily BOOLEAN; v_debit public.credit_ledger%ROWTYPE; v_budget JSONB; v_refund INT := 0;
  v_balance INT; v_lot UUID; v_date DATE; v_now TIMESTAMPTZ;
BEGIN
  SELECT daily_enabled INTO v_daily FROM public.credit_program WHERE singleton FOR SHARE;
  SELECT * INTO v_debit FROM public.credit_ledger WHERE id = p_debit_id AND kind = 'debit';
  IF NOT FOUND THEN RETURN jsonb_build_object('ok',true,'refunded',0,'reason','NO_DEBIT_FOUND'); END IF;
  PERFORM 1 FROM public.credit_accounts WHERE wholesaler_id = v_debit.account_id FOR UPDATE;
  IF v_debit.metadata->>'mode' IS DISTINCT FROM 'daily' THEN
    RETURN public.refund_debit_legacy(p_debit_id,p_reason);
  END IF;
  IF EXISTS(SELECT 1 FROM public.credit_ledger WHERE idempotency_key = 'refund:' || p_debit_id) THEN
    RETURN jsonb_build_object('ok',true,'replayed',true,'refunded',0,'refund_of',p_debit_id);
  END IF;
  v_now := clock_timestamp();
  v_date := (v_now AT TIME ZONE 'Asia/Kolkata')::date;
  v_lot := (v_debit.metadata->'allocations'->0->>'lot_id')::uuid;
  -- Ineligible/suspended accounts still receive an audit reversal, never a new allowance.
  IF v_daily AND public.credits_resolve_owner(v_debit.account_id) = v_debit.account_id
    AND (v_debit.metadata->>'budget_date')::date = v_date THEN
    UPDATE public.credit_lots SET credits_remaining = credits_remaining - v_debit.delta
      WHERE id = v_lot AND account_id = v_debit.account_id AND source = 'daily'
        AND budget_date = v_date AND expires_at > v_now AND archived_at IS NULL
        AND credits_remaining - v_debit.delta <= credits_granted;
    IF FOUND THEN v_refund := -v_debit.delta; END IF;
  END IF;
  v_balance := public.credits_recompute(v_debit.account_id);
  UPDATE public.credit_accounts SET lifetime_spent = greatest(0,lifetime_spent + v_debit.delta) WHERE wholesaler_id = v_debit.account_id;
  INSERT INTO public.credit_ledger(account_id,delta,kind,feature_key,reference_type,reference_id,idempotency_key,balance_after,metadata)
    VALUES(v_debit.account_id,v_refund,'refund',v_debit.feature_key,v_debit.reference_type,v_debit.reference_id,
      'refund:' || p_debit_id,v_balance,jsonb_build_object('refund_of',p_debit_id,'reason',p_reason,
        'budget_date',v_debit.metadata->>'budget_date','reversed_units',-v_debit.delta,'expired_units',-v_debit.delta-v_refund));
  RETURN jsonb_build_object('ok',true,'refunded',v_refund,'reversed_units',-v_debit.delta,'balance',v_balance,'refund_of',p_debit_id);
END;
$$;

CREATE OR REPLACE FUNCTION public.credits_wallet() RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_owner UUID := public.credits_resolve_owner(auth.uid()); v_budget JSONB; v_acct public.credit_accounts%ROWTYPE;
  v_expiring INT; v_next TIMESTAMPTZ; v_legacy INT;
BEGIN
  IF auth.uid() IS NULL THEN RETURN jsonb_build_object('ok',false,'error','NOT_AUTHENTICATED'); END IF;
  IF v_owner IS NULL THEN RETURN jsonb_build_object('ok',false,'error','NOT_VERIFIED'); END IF;
  v_budget := public.credits_ensure_daily_budget(v_owner);
  IF NOT coalesce((v_budget->>'ok')::boolean,false) THEN RETURN v_budget; END IF;
  SELECT * INTO v_acct FROM public.credit_accounts WHERE wholesaler_id = v_owner;
  SELECT coalesce(sum(credits_remaining),0)::int, min(expires_at) INTO v_expiring,v_next FROM public.credit_lots
    WHERE account_id = v_owner AND archived_at IS NULL AND credits_remaining > 0
      AND expires_at > clock_timestamp() AND expires_at <= clock_timestamp() + interval '7 days';
  SELECT coalesce(sum(credits_remaining),0)::int INTO v_legacy FROM public.credit_lots WHERE account_id = v_owner AND archived_at IS NOT NULL;
  RETURN v_budget || jsonb_build_object('available',(v_budget->>'balance')::int,'lifetime_spent',v_acct.lifetime_spent,
    'lifetime_granted',v_acct.lifetime_granted,'lifetime_expired',v_acct.lifetime_expired,'expiring_soon',v_expiring,
    'next_expiry',v_next,'low_balance',v_acct.balance_cached <= v_acct.low_balance_threshold,
    'low_balance_threshold',v_acct.low_balance_threshold,'recovery_owed',v_acct.recovery_owed,
    'legacy_preserved',v_legacy,'shared_business_wallet',v_owner <> auth.uid());
END;
$$;

CREATE FUNCTION public.credits_history(p_limit INT DEFAULT 50,p_offset INT DEFAULT 0,p_kind TEXT DEFAULT NULL,
  p_before TIMESTAMPTZ DEFAULT NULL,p_before_id UUID DEFAULT NULL)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_owner UUID := public.credits_resolve_owner(auth.uid()); v_count INT; v_rows JSONB;
BEGIN
  IF v_owner IS NULL THEN RETURN jsonb_build_object('ok',false,'error','NOT_VERIFIED'); END IF;
  SELECT count(*)::int INTO v_count FROM public.credit_ledger WHERE account_id = v_owner AND (p_kind IS NULL OR kind = p_kind)
    AND (p_before IS NULL OR (p_before_id IS NULL AND created_at < p_before) OR (created_at,id) < (p_before,p_before_id));
  SELECT coalesce(jsonb_agg(to_jsonb(l)),'[]'::jsonb) INTO v_rows FROM (
    SELECT id,delta,kind,feature_key,reference_type,reference_id,balance_after,metadata,created_at
      FROM public.credit_ledger WHERE account_id = v_owner AND (p_kind IS NULL OR kind = p_kind)
        AND (p_before IS NULL OR (p_before_id IS NULL AND created_at < p_before) OR (created_at,id) < (p_before,p_before_id))
      ORDER BY created_at DESC,id DESC LIMIT LEAST(GREATEST(p_limit,1),100) OFFSET GREATEST(p_offset,0)
  ) l;
  RETURN jsonb_build_object('ok',true,'data',v_rows,'count',v_count);
END;
$$;

CREATE OR REPLACE FUNCTION public.credits_expire_due() RETURNS INT
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_account UUID; v_lot public.credit_lots%ROWTYPE; v_balance INT; v_count INT := 0;
BEGIN
  PERFORM 1 FROM public.credit_program WHERE singleton FOR SHARE;
  FOR v_account IN SELECT DISTINCT account_id FROM public.credit_lots WHERE archived_at IS NULL
    AND credits_remaining > 0 AND expires_at <= clock_timestamp() ORDER BY account_id LOOP
    PERFORM 1 FROM public.credit_accounts WHERE wholesaler_id = v_account FOR UPDATE;
    FOR v_lot IN SELECT * FROM public.credit_lots WHERE account_id = v_account AND archived_at IS NULL
      AND credits_remaining > 0 AND expires_at <= clock_timestamp() FOR UPDATE LOOP
      UPDATE public.credit_lots SET credits_remaining = 0 WHERE id = v_lot.id;
      v_balance := public.credits_recompute(v_account);
      UPDATE public.credit_accounts SET lifetime_expired = lifetime_expired + v_lot.credits_remaining WHERE wholesaler_id = v_account;
      INSERT INTO public.credit_ledger(account_id,delta,kind,idempotency_key,balance_after,metadata)
        VALUES(v_account,-v_lot.credits_remaining,'expiry','daily-expiry:' || v_lot.id,v_balance,
          jsonb_build_object('source',v_lot.source,'budget_date',v_lot.budget_date));
    END LOOP;
    v_count := v_count + 1;
  END LOOP;
  RETURN v_count;
END;
$$;

-- Delayed Razorpay callbacks retain the receipt and open a settlement item.
-- They cannot buy additional daily credits or disappear from the accounting trail.
ALTER FUNCTION public.record_razorpay_purchase(UUID,TEXT,INT,TEXT,NUMERIC,NUMERIC,TEXT,TEXT,TEXT,JSONB)
  RENAME TO record_razorpay_purchase_legacy;
CREATE FUNCTION public.record_razorpay_purchase(p_user UUID,p_payment_id TEXT,p_credits INT,p_pack_key TEXT,
  p_amount_inr NUMERIC,p_gst_inr NUMERIC,p_provider_ref TEXT DEFAULT NULL,p_buyer_gstin TEXT DEFAULT NULL,
  p_buyer_state TEXT DEFAULT NULL,p_receipt_json JSONB DEFAULT NULL)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_daily BOOLEAN; v_result JSONB;
BEGIN
  SELECT daily_enabled INTO v_daily FROM public.credit_program WHERE singleton FOR SHARE;
  PERFORM public.credits_ensure_account(p_user);
  PERFORM 1 FROM public.credit_accounts WHERE wholesaler_id = p_user FOR UPDATE;
  v_result := public.record_razorpay_purchase_legacy(p_user,p_payment_id,p_credits,p_pack_key,p_amount_inr,p_gst_inr,
    p_provider_ref,p_buyer_gstin,p_buyer_state,p_receipt_json);
  IF v_daily AND coalesce((v_result->>'ok')::boolean,false) AND NOT coalesce((v_result->>'replayed')::boolean,false) THEN
    INSERT INTO public.credit_purchase_issues(razorpay_payment_id,razorpay_link_id,event,reason,raw_payload)
      VALUES(btrim(p_payment_id),p_provider_ref,'payment.after_daily_cutover',
        'Daily program: late payment preserved separately; manual refund or settlement required',coalesce(p_receipt_json,'{}'::jsonb))
      ON CONFLICT(razorpay_payment_id) DO UPDATE SET resolved_at=NULL,resolved_note=NULL,
        event=EXCLUDED.event,reason=EXCLUDED.reason,raw_payload=EXCLUDED.raw_payload;
  END IF;
  RETURN v_result;
END;
$$;
REVOKE ALL ON FUNCTION public.record_razorpay_purchase_legacy(UUID,TEXT,INT,TEXT,NUMERIC,NUMERIC,TEXT,TEXT,TEXT,JSONB)
  FROM PUBLIC,anon,authenticated,service_role;
REVOKE ALL ON FUNCTION public.record_razorpay_purchase(UUID,TEXT,INT,TEXT,NUMERIC,NUMERIC,TEXT,TEXT,TEXT,JSONB) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.record_razorpay_purchase(UUID,TEXT,INT,TEXT,NUMERIC,NUMERIC,TEXT,TEXT,TEXT,JSONB) TO service_role;

-- Provider refunds take the same program/account lock order as all other mutations.
CREATE OR REPLACE FUNCTION public.record_apple_credit_refund(p_transaction_id TEXT,p_notification JSONB)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_purchase public.credit_purchases%ROWTYPE; v_unused INT; v_current_removed INT; v_balance INT; v_daily BOOLEAN;
BEGIN
  SELECT daily_enabled INTO v_daily FROM public.credit_program WHERE singleton FOR SHARE;
  SELECT * INTO v_purchase FROM public.credit_purchases
    WHERE provider = 'apple' AND provider_txn_id = 'apple:' || btrim(coalesce(p_transaction_id,''));
  IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'error','PURCHASE_NOT_FOUND'); END IF;
  PERFORM 1 FROM public.credit_accounts WHERE wholesaler_id = v_purchase.account_id FOR UPDATE;
  SELECT * INTO v_purchase FROM public.credit_purchases WHERE id = v_purchase.id FOR UPDATE;
  IF v_purchase.status = 'refunded' THEN RETURN jsonb_build_object('ok',true,'replayed',true); END IF;
  PERFORM 1 FROM public.credit_lots WHERE purchase_id = v_purchase.id FOR UPDATE;
  SELECT coalesce(sum(credits_remaining),0)::int,
    coalesce(sum(credits_remaining) FILTER(WHERE archived_at IS NULL AND NOT v_daily),0)::int
    INTO v_unused,v_current_removed FROM public.credit_lots WHERE purchase_id = v_purchase.id;
  UPDATE public.credit_lots SET credits_remaining = 0 WHERE purchase_id = v_purchase.id;
  v_balance := public.credits_recompute(v_purchase.account_id);
  UPDATE public.credit_accounts SET lifetime_granted = greatest(lifetime_granted - v_unused,0),
    recovery_owed = recovery_owed + greatest(v_purchase.credits - v_unused,0) WHERE wholesaler_id = v_purchase.account_id;
  UPDATE public.credit_purchases SET status = 'refunded',receipt_json = coalesce(receipt_json,'{}'::jsonb)
    || jsonb_build_object('refund_notification',p_notification) WHERE id = v_purchase.id;
  INSERT INTO public.credit_ledger(account_id,delta,kind,reference_type,reference_id,idempotency_key,balance_after,metadata)
    VALUES(v_purchase.account_id,-v_current_removed,'adjustment','purchase',v_purchase.id::text,
      'apple-refund:' || p_transaction_id,v_balance,jsonb_build_object('provider','apple',
        'legacy_delta',-(v_unused-v_current_removed),'spent_credits',v_purchase.credits-v_unused));
  RETURN jsonb_build_object('ok',true,'removed',v_unused,'balance',v_balance);
END;
$$;

-- Only verified transactions initiated before purchase retirement may be reconciled.
ALTER FUNCTION public.record_apple_credit_purchase(UUID,TEXT,TEXT,JSONB) RENAME TO record_apple_credit_purchase_legacy;
CREATE FUNCTION public.record_apple_credit_purchase(p_user UUID,p_transaction_id TEXT,p_product_id TEXT,p_signed_transaction JSONB)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_program public.credit_program%ROWTYPE; v_purchase_ms NUMERIC;
BEGIN
  SELECT * INTO v_program FROM public.credit_program WHERE singleton FOR SHARE;
  BEGIN v_purchase_ms := (p_signed_transaction->>'purchaseDate')::numeric;
  EXCEPTION WHEN invalid_text_representation THEN v_purchase_ms := NULL; END;
  IF NOT v_program.payments_enabled AND (v_purchase_ms IS NULL OR v_purchase_ms <= 0
    OR v_purchase_ms > extract(epoch FROM v_program.payments_disabled_at) * 1000) THEN
    PERFORM public.record_razorpay_issue('apple:' || coalesce(p_transaction_id,''),NULL,'apple.purchase',
      'PAYMENTS_DISABLED: verified transaction requires manual settlement',p_signed_transaction);
    RETURN jsonb_build_object('ok',false,'error','PAYMENTS_DISABLED');
  END IF;
  PERFORM public.credits_ensure_account(p_user);
  PERFORM 1 FROM public.credit_accounts WHERE wholesaler_id = p_user FOR UPDATE;
  RETURN public.record_apple_credit_purchase_legacy(p_user,p_transaction_id,p_product_id,p_signed_transaction);
END;
$$;

UPDATE public.credit_prices SET description = 'Studio images generated for this upload'
  WHERE feature_key LIKE 'product.images_%' AND description LIKE '%₹%';

-- Temporary plan access has no subscription row, renewal, or permanent unlock.
ALTER FUNCTION public.my_plan() RENAME TO my_plan_legacy;
CREATE FUNCTION public.my_plan() RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_daily BOOLEAN; v_verified BOOLEAN; v_payments BOOLEAN; v_last public.retailer_subscriptions%ROWTYPE;
BEGIN
  SELECT daily_enabled,payments_enabled INTO v_daily,v_payments FROM public.credit_program WHERE singleton FOR SHARE;
  IF NOT v_daily THEN
    IF v_payments THEN RETURN public.my_plan_legacy(); END IF;
    SELECT * INTO v_last FROM public.retailer_subscriptions WHERE user_id = auth.uid() ORDER BY expires_at DESC LIMIT 1;
    RETURN jsonb_build_object('ok',true,'active',coalesce(v_last.expires_at > clock_timestamp(),false),
      'plan_key',v_last.plan_key,'expires_at',v_last.expires_at,'auto_renew',false,'renewed_now',false);
  END IF;
  SELECT EXISTS(SELECT 1 FROM public.retailers WHERE user_id = public.credits_resolve_owner(auth.uid())
    AND verification_status = 'verified') INTO v_verified;
  RETURN jsonb_build_object('active',v_verified,'plan_key',CASE WHEN v_verified THEN 'daily_access' ELSE NULL END,
    'expires_at',NULL,'auto_renew',false,'renewed_now',false,'temporary_access',v_verified);
END;
$$;
ALTER FUNCTION public.subscribe_plan(TEXT) RENAME TO subscribe_plan_legacy;
CREATE FUNCTION public.subscribe_plan(p_plan TEXT) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  PERFORM 1 FROM public.credit_program WHERE singleton FOR SHARE;
  IF NOT (SELECT payments_enabled FROM public.credit_program WHERE singleton) OR (SELECT daily_enabled FROM public.credit_program WHERE singleton) THEN
    RETURN jsonb_build_object('ok',false,'error','PLANS_PAUSED');
  END IF;
  RETURN public.subscribe_plan_legacy(p_plan);
END;
$$;
CREATE OR REPLACE FUNCTION public.has_entitlement(p_user UUID,p_key TEXT) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT EXISTS(SELECT 1 FROM public.entitlements WHERE user_id = p_user AND entitlement_key = p_key
    AND (expires_at IS NULL OR expires_at > now()))
    OR (p_key LIKE 'theme.%' AND public.has_active_plan(p_user))
    OR (p_key LIKE 'theme.%' AND (SELECT daily_enabled FROM public.credit_program WHERE singleton)
      AND EXISTS(SELECT 1 FROM public.retailers WHERE user_id = p_user AND verification_status = 'verified'));
$$;

REVOKE ALL ON FUNCTION public.spend_credits_legacy(UUID,TEXT,TEXT,TEXT,TEXT,JSONB),
  public.grant_credits_legacy(UUID,INT,TEXT,TEXT,TIMESTAMPTZ,UUID,TEXT,JSONB,TEXT,TEXT),
  public.refund_debit_legacy(UUID,TEXT), public.my_plan_legacy(),public.subscribe_plan_legacy(TEXT),
  public.record_apple_credit_purchase_legacy(UUID,TEXT,TEXT,JSONB)
  FROM PUBLIC,anon,authenticated,service_role;
REVOKE ALL ON FUNCTION public.credits_resolve_owner(UUID),public.credits_activate_daily(),
  public.credits_ensure_daily_budget(UUID),public.spend_credits(UUID,TEXT,TEXT,TEXT,TEXT,JSONB),
  public.grant_credits(UUID,INT,TEXT,TEXT,TIMESTAMPTZ,UUID,TEXT,JSONB,TEXT,TEXT), public.refund_debit(UUID,TEXT),
  public.record_apple_credit_purchase(UUID,TEXT,TEXT,JSONB)
  FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.credits_resolve_owner(UUID),public.credits_activate_daily(),
  public.credits_ensure_daily_budget(UUID),public.spend_credits(UUID,TEXT,TEXT,TEXT,TEXT,JSONB),
  public.grant_credits(UUID,INT,TEXT,TEXT,TIMESTAMPTZ,UUID,TEXT,JSONB,TEXT,TEXT),public.refund_debit(UUID,TEXT),
  public.record_apple_credit_purchase(UUID,TEXT,TEXT,JSONB) TO service_role;
REVOKE ALL ON FUNCTION public.credits_history(INT,INT,TEXT,TIMESTAMPTZ,UUID),public.credits_wallet(),public.my_plan(),public.subscribe_plan(TEXT)
  FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION public.credits_history(INT,INT,TEXT,TIMESTAMPTZ,UUID),public.credits_wallet(),public.my_plan(),public.subscribe_plan(TEXT)
  TO authenticated,service_role;
COMMIT;
