-- User decision (3 October 2026): preserve unused purchased credits without expiry.
-- Apply after 017, BEFORE daily/referral activation. This migration changes no
-- balances until a trusted operator explicitly calls credits_preserve_purchased().
-- Original lot IDs, sources, purchase/receipt links, granted/remaining units and
-- lifetime totals stay intact. No replacement grant or duplicate lot is created.
BEGIN;
ALTER TABLE public.credit_lots ADD COLUMN paid_preserved_at TIMESTAMPTZ;
CREATE TABLE public.credit_paid_preservation (
  lot_id UUID PRIMARY KEY REFERENCES public.credit_lots(id),
  account_id UUID NOT NULL,
  purchase_id UUID,
  original_source TEXT NOT NULL,
  original_granted INT NOT NULL,
  preserved_units INT NOT NULL CHECK(preserved_units > 0),
  original_expires_at TIMESTAMPTZ,
  preserved_at TIMESTAMPTZ NOT NULL
);
ALTER TABLE public.credit_paid_preservation ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.credit_paid_preservation FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT ON public.credit_paid_preservation TO service_role;

CREATE FUNCTION public.credits_preserve_purchased() RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE v_lot public.credit_lots%ROWTYPE; v_owner UUID; v_count INT:=0; v_units BIGINT:=0;
  v_now TIMESTAMPTZ; v_balance INT;
BEGIN
  -- Same lock order as activation and every daily-mode wallet mutation.
  PERFORM 1 FROM public.credit_program WHERE singleton FOR UPDATE;
  IF EXISTS(SELECT 1 FROM public.credit_lots WHERE credits_remaining>0
    AND (source='purchase' OR purchase_id IS NOT NULL) AND paid_preserved_at IS NULL AND archived_at IS NOT NULL) THEN
    RETURN jsonb_build_object('ok',false,'error','ARCHIVED_PAID_BALANCES_REQUIRE_RECONCILIATION');
  END IF;
  IF (SELECT daily_enabled FROM public.credit_program WHERE singleton) AND EXISTS(
    SELECT 1 FROM public.credit_lots WHERE credits_remaining>0 AND (source='purchase' OR purchase_id IS NOT NULL)
      AND paid_preserved_at IS NULL) THEN
    RETURN jsonb_build_object('ok',false,'error','DAILY_ALREADY_ACTIVE');
  END IF;
  IF EXISTS(SELECT 1 FROM public.credit_lots l LEFT JOIN public.credit_purchases p ON p.id=l.purchase_id
    WHERE l.credits_remaining>0 AND l.paid_preserved_at IS NULL AND l.purchase_id IS NOT NULL
      AND (p.id IS NULL OR p.account_id IS DISTINCT FROM l.account_id OR p.status<>'paid')) THEN
    RETURN jsonb_build_object('ok',false,'error','PAID_RECEIPTS_REQUIRE_RECONCILIATION');
  END IF;
  FOR v_owner IN SELECT DISTINCT account_id FROM public.credit_lots
    WHERE credits_remaining>0 AND (source='purchase' OR purchase_id IS NOT NULL)
      AND paid_preserved_at IS NULL AND archived_at IS NULL ORDER BY account_id LOOP
    PERFORM 1 FROM public.credit_accounts WHERE wholesaler_id=v_owner FOR UPDATE;
    FOR v_lot IN SELECT * FROM public.credit_lots WHERE account_id=v_owner
      AND credits_remaining>0 AND (source='purchase' OR purchase_id IS NOT NULL)
      AND paid_preserved_at IS NULL AND archived_at IS NULL ORDER BY id FOR UPDATE LOOP
      v_now:=clock_timestamp();
      INSERT INTO public.credit_paid_preservation(lot_id,account_id,purchase_id,original_source,original_granted,
        preserved_units,original_expires_at,preserved_at)
        VALUES(v_lot.id,v_owner,v_lot.purchase_id,v_lot.source,v_lot.credits_granted,
          v_lot.credits_remaining,v_lot.expires_at,v_now);
      UPDATE public.credit_lots SET paid_preserved_at=v_now,expires_at=NULL WHERE id=v_lot.id;
      v_balance:=public.credits_recompute(v_owner);
      INSERT INTO public.credit_ledger(account_id,delta,kind,reference_type,reference_id,idempotency_key,balance_after,metadata)
        VALUES(v_owner,0,'adjustment','purchased_carryover',v_lot.id::text,'purchased-carryover:'||v_lot.id,v_balance,
          jsonb_build_object('source',v_lot.source,'purchase_id',v_lot.purchase_id,'preserved_units',v_lot.credits_remaining,
            'original_expires_at',v_lot.expires_at,'policy','non_expiring_paid_balance'));
      v_count:=v_count+1;v_units:=v_units+v_lot.credits_remaining;
    END LOOP;
  END LOOP;
  RETURN jsonb_build_object('ok',true,'preserved_lots',v_count,'preserved_units',v_units,'replayed',v_count=0);
END; $$;
REVOKE ALL ON FUNCTION public.credits_preserve_purchased() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.credits_preserve_purchased() TO service_role;


CREATE OR REPLACE FUNCTION public.credits_recompute(p_user UUID) RETURNS INT
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE v_balance INT; v_daily BOOLEAN; v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
  SELECT daily_enabled INTO v_daily FROM public.credit_program WHERE singleton;
  SELECT coalesce(sum(credits_remaining), 0)::int INTO v_balance FROM public.credit_lots
    WHERE account_id = p_user AND archived_at IS NULL AND credits_remaining > 0
    AND (expires_at IS NULL OR expires_at > v_now) AND (NOT v_daily OR (source IN ('daily','invitation_gift','referral_bonus') OR paid_preserved_at IS NOT NULL));
  UPDATE public.credit_accounts SET balance_cached = v_balance, updated_at = v_now,
    low_balance_notified_at = CASE WHEN v_balance > low_balance_threshold THEN NULL ELSE low_balance_notified_at END
    WHERE wholesaler_id = p_user;
  RETURN v_balance;
END;
$$;

CREATE OR REPLACE FUNCTION public.credits_ensure_daily_budget(p_user UUID) RETURNS JSONB
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
    WHERE account_id = p_user AND source NOT IN ('daily','invitation_gift','referral_bonus') AND paid_preserved_at IS NULL AND archived_at IS NULL AND credits_remaining > 0
      AND (expires_at IS NULL OR expires_at > v_now);
  UPDATE public.credit_lots SET archived_at = v_now
    WHERE account_id = p_user AND source NOT IN ('daily','invitation_gift','referral_bonus') AND paid_preserved_at IS NULL AND archived_at IS NULL;
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
      VALUES(p_user, 2000, 'grant', 'daily', 'daily:' || p_user || ':' || v_date, public.credits_recompute(p_user),
        jsonb_build_object('source', 'daily', 'budget_date', v_date));
  END IF;
  v_balance := public.credits_recompute(p_user);
  RETURN jsonb_build_object('ok', true, 'mode', 'daily', 'balance', v_balance, 'budget_date', v_date,
    'daily_allowance', 2000, 'resets_at', v_end, 'server_now', v_now);
END;
$$;

CREATE OR REPLACE FUNCTION public.spend_credits(p_user UUID, p_feature_key TEXT, p_idempotency_key TEXT,
  p_reference_type TEXT DEFAULT NULL, p_reference_id TEXT DEFAULT NULL, p_metadata JSONB DEFAULT '{}'::jsonb)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_owner UUID; v_daily BOOLEAN; v_budget JSONB; v_existing public.credit_ledger%ROWTYPE;
  v_lot public.credit_lots%ROWTYPE; v_cost INT; v_balance INT; v_ledger UUID; v_created TIMESTAMPTZ; v_remaining INT; v_take INT; v_allocations JSONB := '[]';
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
  v_remaining := v_cost;
  FOR v_lot IN SELECT * FROM public.credit_lots WHERE account_id = v_owner AND archived_at IS NULL
    AND (source IN ('daily','invitation_gift','referral_bonus') OR paid_preserved_at IS NOT NULL) AND credits_remaining > 0
    AND (expires_at IS NULL OR expires_at > clock_timestamp()) ORDER BY expires_at NULLS LAST,granted_at,id FOR UPDATE LOOP
    EXIT WHEN v_remaining = 0;
    v_take := least(v_remaining,v_lot.credits_remaining);
    UPDATE public.credit_lots SET credits_remaining = credits_remaining - v_take WHERE id = v_lot.id;
    v_allocations := v_allocations || jsonb_build_array(jsonb_build_object('lot_id',v_lot.id,'credits',v_take,'expires_at',v_lot.expires_at));
    v_remaining := v_remaining - v_take;
  END LOOP;
  IF v_remaining <> 0 THEN RAISE EXCEPTION 'CREDIT_ALLOCATION_FAILED'; END IF;
  v_balance := v_balance - v_cost;
  UPDATE public.credit_accounts SET balance_cached = v_balance, lifetime_spent = lifetime_spent + v_cost,
    updated_at = clock_timestamp() WHERE wholesaler_id = v_owner;
  INSERT INTO public.credit_ledger(account_id,delta,kind,feature_key,reference_type,reference_id,idempotency_key,balance_after,metadata)
    VALUES(v_owner,-v_cost,'debit',p_feature_key,p_reference_type,p_reference_id,p_idempotency_key,v_balance,
      coalesce(p_metadata,'{}'::jsonb) || jsonb_build_object('actor_id',p_user,'budget_date',v_budget->>'budget_date','mode','daily',
        'allocations',v_allocations))
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

CREATE OR REPLACE FUNCTION public.refund_debit(p_debit_id UUID,p_reason TEXT DEFAULT NULL) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_daily BOOLEAN; v_debit public.credit_ledger%ROWTYPE; v_budget JSONB; v_refund INT := 0;
  v_balance INT; v_lot UUID; v_date DATE; v_now TIMESTAMPTZ; v_allocation JSONB; v_units INT;
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
  -- Restore each original lot only while it remains live. Persistent bonuses survive midnight.
  FOR v_allocation IN SELECT value FROM jsonb_array_elements(v_debit.metadata->'allocations') LOOP
    v_lot := (v_allocation->>'lot_id')::uuid; v_units := (v_allocation->>'credits')::int;
    UPDATE public.credit_lots SET credits_remaining = credits_remaining + v_units
      WHERE id = v_lot AND account_id = v_debit.account_id AND archived_at IS NULL
        AND (expires_at IS NULL OR expires_at > v_now)
        AND (source IN ('daily','invitation_gift','referral_bonus') OR paid_preserved_at IS NOT NULL)
        AND credits_remaining + v_units <= credits_granted;
    IF FOUND THEN v_refund := v_refund + v_units; END IF;
  END LOOP;
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
  v_expiring INT; v_next TIMESTAMPTZ; v_legacy INT; v_daily INT; v_bonus INT;
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
  SELECT coalesce(sum(credits_remaining) FILTER(WHERE source = 'daily'),0)::int,
    coalesce(sum(credits_remaining) FILTER(WHERE (source IN ('invitation_gift','referral_bonus') OR paid_preserved_at IS NOT NULL)),0)::int
    INTO v_daily,v_bonus FROM public.credit_lots WHERE account_id = v_owner AND archived_at IS NULL
    AND (expires_at IS NULL OR expires_at > clock_timestamp());
  RETURN v_budget || jsonb_build_object('daily_available',v_daily,'bonus_available',v_bonus,'available',(v_budget->>'balance')::int,'lifetime_spent',v_acct.lifetime_spent,
    'lifetime_granted',v_acct.lifetime_granted,'lifetime_expired',v_acct.lifetime_expired,'expiring_soon',v_expiring,
    'next_expiry',v_next,'low_balance',v_acct.balance_cached <= v_acct.low_balance_threshold,
    'low_balance_threshold',v_acct.low_balance_threshold,'recovery_owed',v_acct.recovery_owed,
    'legacy_preserved',v_legacy,'shared_business_wallet',v_owner <> auth.uid());
END;
$$;

CREATE OR REPLACE FUNCTION public.referral_fund(p_user UUID,p_units INT,p_invitation UUID) RETURNS UUID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public,pg_temp AS $$
DECLARE v_budget JSONB; v_lot credit_lots%ROWTYPE; v_take INT; v_left INT := p_units;
 v_alloc JSONB := '[]'; v_balance INT; v_ledger UUID;
BEGIN
 IF p_units=0 THEN RETURN NULL; END IF;
 v_budget := credits_ensure_daily_budget(p_user);
 v_balance := (v_budget->>'balance')::int;
 IF NOT coalesce((v_budget->>'ok')::boolean,false) OR v_balance < p_units THEN
  RAISE EXCEPTION 'INSUFFICIENT_CREDITS' USING ERRCODE='23514'; END IF;
 FOR v_lot IN SELECT * FROM credit_lots WHERE account_id=p_user AND archived_at IS NULL
  AND credits_remaining>0 AND (source IN ('daily','invitation_gift','referral_bonus') OR paid_preserved_at IS NOT NULL)
  AND (expires_at IS NULL OR expires_at>clock_timestamp()) ORDER BY expires_at NULLS LAST,granted_at,id FOR UPDATE LOOP
  EXIT WHEN v_left=0;
  v_take:=least(v_left,v_lot.credits_remaining);
  UPDATE credit_lots SET credits_remaining=credits_remaining-v_take WHERE id=v_lot.id;
  v_alloc:=v_alloc||jsonb_build_array(jsonb_build_object('lot_id',v_lot.id,'credits',v_take,'expires_at',v_lot.expires_at));
  v_left:=v_left-v_take;
 END LOOP;
 IF v_left<>0 THEN RAISE EXCEPTION 'CREDIT_ALLOCATION_FAILED'; END IF;
 v_balance:=credits_recompute(p_user);
 UPDATE credit_accounts SET lifetime_spent=lifetime_spent+p_units WHERE wholesaler_id=p_user;
 INSERT INTO credit_ledger(account_id,delta,kind,reference_type,reference_id,idempotency_key,balance_after,metadata)
 VALUES(p_user,-p_units,'debit','invitation_funding',p_invitation::text,'invitation-fund:'||p_invitation,v_balance,
  jsonb_build_object('mode','daily','actor_id',p_user,'allocations',v_alloc,'budget_date',v_budget->>'budget_date')) RETURNING id INTO v_ledger;
 RETURN v_ledger;
END; $$;

CREATE OR REPLACE FUNCTION public.credits_activate_daily() RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_affected INT;
BEGIN
  -- Every wallet mutation takes FOR SHARE on this row first. Activation waits for them.
  PERFORM 1 FROM public.credit_program WHERE singleton FOR UPDATE;
  IF (SELECT daily_enabled FROM public.credit_program WHERE singleton) THEN
    RETURN jsonb_build_object('ok', true, 'replayed', true);
  END IF;
  SELECT count(DISTINCT account_id)::int INTO v_affected FROM public.credit_lots
    WHERE credits_remaining > 0 AND (source = 'purchase' OR purchase_id IS NOT NULL) AND paid_preserved_at IS NULL;
  IF v_affected > 0 THEN
    RETURN jsonb_build_object('ok', false, 'error', 'LEGACY_PAID_BALANCES_REQUIRE_DECISION', 'affected_accounts', v_affected);
  END IF;
  IF EXISTS(SELECT 1 FROM public.credit_lots l WHERE l.paid_preserved_at IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM public.credit_paid_preservation p WHERE p.lot_id=l.id AND p.account_id=l.account_id
      AND p.purchase_id IS NOT DISTINCT FROM l.purchase_id AND p.original_source=l.source
      AND p.original_granted=l.credits_granted AND p.preserved_at=l.paid_preserved_at)) THEN
    RETURN jsonb_build_object('ok',false,'error','PAID_PRESERVATION_AUDIT_MISMATCH');
  END IF;
  UPDATE public.credit_program SET daily_enabled = true, payments_enabled = false,
    activated_at = clock_timestamp() WHERE singleton;
  RETURN jsonb_build_object('ok', true, 'daily_allowance', 2000);
END;
$$;

NOTIFY pgrst, 'reload schema';
COMMIT;
