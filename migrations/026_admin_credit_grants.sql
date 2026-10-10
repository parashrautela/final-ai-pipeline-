-- Immediate, non-expiring admin bonuses; never alter the recurring cycle.
BEGIN;
ALTER TABLE public.credit_lots DROP CONSTRAINT IF EXISTS credit_lots_source_check;
ALTER TABLE public.credit_lots ADD CONSTRAINT credit_lots_source_check CHECK(source IN ('welcome','purchase','subscription','referral','promo','refund','admin','daily','invitation_gift','referral_bonus','admin_bonus'));
CREATE TABLE IF NOT EXISTS public.credit_admin_grants (
 request_key UUID PRIMARY KEY,
 business_type TEXT NOT NULL CHECK(business_type IN ('wholesaler','retailer')),
 business_id UUID NOT NULL,
 account_id UUID NOT NULL REFERENCES public.credit_accounts(wholesaler_id),
 credits INT NOT NULL CHECK(credits BETWEEN 1 AND 100000),
 reason TEXT NOT NULL CHECK(length(btrim(reason)) BETWEEN 1 AND 500),
 actor TEXT NOT NULL,
 lot_id UUID NOT NULL REFERENCES public.credit_lots(id),
 ledger_id UUID NOT NULL REFERENCES public.credit_ledger(id),
 result JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE public.credit_admin_grants ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.credit_admin_grants FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT,INSERT ON public.credit_admin_grants TO service_role;

CREATE OR REPLACE FUNCTION public.credits_recompute(p_user UUID) RETURNS INT
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE v_balance INT; v_daily BOOLEAN; v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
  SELECT daily_enabled INTO v_daily FROM public.credit_program WHERE singleton;
  SELECT coalesce(sum(credits_remaining), 0)::int INTO v_balance FROM public.credit_lots
    WHERE account_id = p_user AND archived_at IS NULL AND credits_remaining > 0
    AND (expires_at IS NULL OR expires_at > v_now) AND (NOT v_daily OR (source IN ('daily','invitation_gift','referral_bonus','admin_bonus') OR paid_preserved_at IS NOT NULL));
  UPDATE public.credit_accounts SET balance_cached = v_balance, updated_at = v_now,
    low_balance_notified_at = CASE WHEN v_balance > low_balance_threshold THEN NULL ELSE low_balance_notified_at END
    WHERE wholesaler_id = p_user;
  RETURN v_balance;
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
        AND (source IN ('daily','invitation_gift','referral_bonus','admin_bonus') OR paid_preserved_at IS NOT NULL)
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

CREATE OR REPLACE FUNCTION public.credits_recompute_at(p_user UUID,p_now TIMESTAMPTZ) RETURNS INT
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE v_balance INT; v_daily BOOLEAN;
BEGIN
 SELECT daily_enabled INTO v_daily FROM public.credit_program WHERE singleton;
 SELECT coalesce(sum(credits_remaining),0)::int INTO v_balance FROM public.credit_lots
 WHERE account_id=p_user AND archived_at IS NULL AND credits_remaining>0 AND (expires_at IS NULL OR expires_at>p_now)
 AND (NOT v_daily OR source IN ('daily','invitation_gift','referral_bonus','admin_bonus') OR paid_preserved_at IS NOT NULL);
 UPDATE public.credit_accounts SET balance_cached=v_balance,updated_at=p_now,
 low_balance_notified_at=CASE WHEN v_balance>low_balance_threshold THEN NULL ELSE low_balance_notified_at END WHERE wholesaler_id=p_user;
 RETURN v_balance;
END $$;

CREATE OR REPLACE FUNCTION public.spend_credits(p_user UUID, p_feature_key TEXT, p_idempotency_key TEXT,
  p_reference_type TEXT DEFAULT NULL, p_reference_id TEXT DEFAULT NULL, p_metadata JSONB DEFAULT '{}'::jsonb)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_op_now TIMESTAMPTZ; v_owner UUID; v_daily BOOLEAN; v_budget JSONB; v_existing public.credit_ledger%ROWTYPE;
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
  v_op_now:=coalesce((v_budget->>'server_now')::timestamptz,clock_timestamp());
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
    AND (source IN ('daily','invitation_gift','referral_bonus','admin_bonus') OR paid_preserved_at IS NOT NULL) AND credits_remaining > 0
    AND (expires_at IS NULL OR expires_at > v_op_now) ORDER BY expires_at NULLS LAST,granted_at,id FOR UPDATE LOOP
    EXIT WHEN v_remaining = 0;
    v_take := least(v_remaining,v_lot.credits_remaining);
    UPDATE public.credit_lots SET credits_remaining = credits_remaining - v_take WHERE id = v_lot.id;
    v_allocations := v_allocations || jsonb_build_array(jsonb_build_object('lot_id',v_lot.id,'credits',v_take,'expires_at',v_lot.expires_at));
    v_remaining := v_remaining - v_take;
  END LOOP;
  IF v_remaining <> 0 THEN RAISE EXCEPTION 'CREDIT_ALLOCATION_FAILED'; END IF;
  v_balance := v_balance - v_cost;
  UPDATE public.credit_accounts SET balance_cached = v_balance, lifetime_spent = lifetime_spent + v_cost,
    updated_at = v_op_now WHERE wholesaler_id = v_owner;
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

CREATE OR REPLACE FUNCTION public.referral_fund(p_user UUID,p_units INT,p_invitation UUID) RETURNS UUID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public,pg_temp AS $$
DECLARE v_op_now TIMESTAMPTZ; v_budget JSONB; v_lot credit_lots%ROWTYPE; v_take INT; v_left INT := p_units;
 v_alloc JSONB := '[]'; v_balance INT; v_ledger UUID;
BEGIN
 IF p_units=0 THEN RETURN NULL; END IF;
 v_budget := credits_ensure_daily_budget(p_user);
 v_op_now:=coalesce((v_budget->>'server_now')::timestamptz,clock_timestamp());
 v_balance := (v_budget->>'balance')::int;
 IF NOT coalesce((v_budget->>'ok')::boolean,false) OR v_balance < p_units THEN
  RAISE EXCEPTION 'INSUFFICIENT_CREDITS' USING ERRCODE='23514'; END IF;
 FOR v_lot IN SELECT * FROM credit_lots WHERE account_id=p_user AND archived_at IS NULL
  AND credits_remaining>0 AND (source IN ('daily','invitation_gift','referral_bonus','admin_bonus') OR paid_preserved_at IS NOT NULL)
  AND (expires_at IS NULL OR expires_at>v_op_now) ORDER BY expires_at NULLS LAST,granted_at,id FOR UPDATE LOOP
  EXIT WHEN v_left=0;
  v_take:=least(v_left,v_lot.credits_remaining);
  UPDATE credit_lots SET credits_remaining=credits_remaining-v_take WHERE id=v_lot.id;
  v_alloc:=v_alloc||jsonb_build_array(jsonb_build_object('lot_id',v_lot.id,'credits',v_take,'expires_at',v_lot.expires_at));
  v_left:=v_left-v_take;
 END LOOP;
 IF v_left<>0 THEN RAISE EXCEPTION 'CREDIT_ALLOCATION_FAILED'; END IF;
 v_balance:=credits_recompute_at(p_user,v_op_now);
 UPDATE credit_accounts SET lifetime_spent=lifetime_spent+p_units WHERE wholesaler_id=p_user;
 INSERT INTO credit_ledger(account_id,delta,kind,reference_type,reference_id,idempotency_key,balance_after,metadata)
 VALUES(p_user,-p_units,'debit','invitation_funding',p_invitation::text,'invitation-fund:'||p_invitation,v_balance,
  jsonb_build_object('mode','daily','actor_id',p_user,'allocations',v_alloc,'budget_date',v_budget->>'budget_date')) RETURNING id INTO v_ledger;
 RETURN v_ledger;
END; $$;

CREATE OR REPLACE FUNCTION public.credits_wallet() RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_owner UUID := public.credits_resolve_owner(auth.uid()); v_budget JSONB; v_acct public.credit_accounts%ROWTYPE;
  v_now TIMESTAMPTZ; v_paid INT; v_gifts INT; v_admin INT; v_expiring INT; v_next TIMESTAMPTZ; v_legacy INT; v_daily INT; v_bonus INT;
BEGIN
  IF auth.uid() IS NULL THEN RETURN jsonb_build_object('ok',false,'error','NOT_AUTHENTICATED'); END IF;
  IF v_owner IS NULL THEN RETURN jsonb_build_object('ok',false,'error','NOT_VERIFIED'); END IF;
  v_budget := public.credits_ensure_daily_budget(v_owner);
  IF NOT coalesce((v_budget->>'ok')::boolean,false) THEN RETURN v_budget; END IF;
  v_now:=coalesce((v_budget->>'server_now')::timestamptz,clock_timestamp());
  SELECT * INTO v_acct FROM public.credit_accounts WHERE wholesaler_id = v_owner;
  SELECT coalesce(sum(credits_remaining),0)::int, min(expires_at) INTO v_expiring,v_next FROM public.credit_lots
    WHERE account_id = v_owner AND archived_at IS NULL AND credits_remaining > 0
      AND expires_at > v_now AND expires_at <= v_now + interval '7 days';
  SELECT coalesce(sum(credits_remaining),0)::int INTO v_legacy FROM public.credit_lots WHERE account_id = v_owner AND archived_at IS NOT NULL;
  SELECT coalesce(sum(credits_remaining) FILTER(WHERE source = 'daily'),0)::int,
    coalesce(sum(credits_remaining) FILTER(WHERE (source IN ('invitation_gift','referral_bonus','admin_bonus') OR paid_preserved_at IS NOT NULL)),0)::int
    INTO v_daily,v_bonus FROM public.credit_lots WHERE account_id = v_owner AND archived_at IS NULL
    AND (expires_at IS NULL OR expires_at > v_now);
  SELECT coalesce(sum(credits_remaining) FILTER(WHERE paid_preserved_at IS NOT NULL),0)::int,
    coalesce(sum(credits_remaining) FILTER(WHERE source IN ('invitation_gift','referral_bonus')),0)::int INTO v_paid,v_gifts
    FROM public.credit_lots WHERE account_id=v_owner AND archived_at IS NULL AND (expires_at IS NULL OR expires_at>v_now);
  SELECT coalesce(sum(credits_remaining),0)::int INTO v_admin FROM public.credit_lots WHERE account_id=v_owner AND source='admin_bonus' AND archived_at IS NULL AND (expires_at IS NULL OR expires_at>v_now);
  RETURN v_budget || jsonb_build_object('admin_available',v_admin,'paid_available',v_paid,'gift_available',v_gifts,'daily_available',v_daily,'bonus_available',v_bonus,'available',(v_budget->>'balance')::int,'lifetime_spent',v_acct.lifetime_spent,
    'lifetime_granted',v_acct.lifetime_granted,'lifetime_expired',v_acct.lifetime_expired,'expiring_soon',v_expiring,
    'next_expiry',v_next,'low_balance',v_acct.balance_cached <= v_acct.low_balance_threshold,
    'low_balance_threshold',v_acct.low_balance_threshold,'recovery_owed',v_acct.recovery_owed,
    'legacy_preserved',v_legacy,'shared_business_wallet',v_owner <> auth.uid());
END;
$$;

CREATE OR REPLACE FUNCTION public.credits_ensure_daily_budget(p_user UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE v_enabled BOOLEAN; v_default INT; v_amount INT; v_version INT; v_custom BOOLEAN;
  v_now TIMESTAMPTZ; v_cycle public.credit_allowance_cycles%ROWTYPE;
  v_lot public.credit_lots%ROWTYPE; v_id UUID; v_balance INT; v_legacy INT;
BEGIN
  SELECT daily_enabled,daily_allowance INTO v_enabled,v_default FROM public.credit_program WHERE singleton FOR SHARE;
  IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'error','CREDIT_PROGRAM_UNAVAILABLE'); END IF;
  IF public.credits_resolve_owner(p_user) IS DISTINCT FROM p_user THEN
    RETURN jsonb_build_object('ok',false,'error','NOT_VERIFIED'); END IF;
  PERFORM public.credits_ensure_account(p_user);
  PERFORM 1 FROM public.credit_accounts WHERE wholesaler_id=p_user FOR UPDATE;
  IF public.credits_resolve_owner(p_user) IS DISTINCT FROM p_user THEN
    RETURN jsonb_build_object('ok',false,'error','NOT_VERIFIED'); END IF;
  v_now:=clock_timestamp();
  IF NOT v_enabled THEN RETURN jsonb_build_object('ok',true,'mode','legacy','balance',public.credits_recompute_at(p_user,v_now)); END IF;
  SELECT CASE WHEN inherit_default THEN v_default ELSE daily_allowance END,version,NOT inherit_default
    INTO v_amount,v_version,v_custom FROM public.credit_allowance_policies WHERE wholesaler_user_id=p_user;
  IF NOT FOUND THEN v_amount:=v_default; v_version:=0; v_custom:=false; END IF;

  SELECT * INTO v_cycle FROM public.credit_allowance_cycles WHERE account_id=p_user ORDER BY issued_at DESC,id DESC LIMIT 1;
  IF NOT FOUND THEN
    -- Transition an existing calendar grant without issuing a second allowance.
    -- Keep its original issue time and extend a shorter midnight deadline to 24 hours.
    SELECT * INTO v_lot FROM public.credit_lots WHERE account_id=p_user AND source='daily'
      AND archived_at IS NULL AND allowance_cycle_id IS NULL ORDER BY granted_at DESC,id DESC LIMIT 1 FOR UPDATE;
    IF FOUND THEN
      INSERT INTO public.credit_allowance_cycles(account_id,issued_at,ends_at,allowance,policy_version,lot_id)
        VALUES(p_user,v_lot.granted_at,v_lot.granted_at+interval '24 hours',v_lot.credits_granted,0,v_lot.id) RETURNING * INTO v_cycle;
      UPDATE public.credit_lots SET allowance_cycle_id=v_cycle.id,expires_at=v_cycle.ends_at WHERE id=v_lot.id;
    END IF;
  END IF;
  -- Expire only the recurring allocation. Paid and gift lots remain untouched.
  FOR v_lot IN SELECT * FROM public.credit_lots WHERE account_id=p_user AND source='daily'
    AND credits_remaining>0 AND expires_at<=v_now FOR UPDATE LOOP
    UPDATE public.credit_lots SET credits_remaining=0 WHERE id=v_lot.id;
    UPDATE public.credit_accounts SET lifetime_expired=lifetime_expired+v_lot.credits_remaining WHERE wholesaler_id=p_user;
    INSERT INTO public.credit_ledger(account_id,delta,kind,idempotency_key,balance_after,metadata)
      VALUES(p_user,-v_lot.credits_remaining,'expiry','daily-expiry:'||v_lot.id,public.credits_recompute_at(p_user,v_now),
        jsonb_build_object('source','daily','cycle_id',v_lot.allowance_cycle_id,'budget_date',v_lot.budget_date));
  END LOOP;
  SELECT coalesce(sum(credits_remaining),0)::int INTO v_legacy FROM public.credit_lots
    WHERE account_id=p_user AND source NOT IN ('daily','invitation_gift','referral_bonus','admin_bonus') AND paid_preserved_at IS NULL
      AND archived_at IS NULL AND (expires_at IS NULL OR expires_at>v_now);
  UPDATE public.credit_lots SET archived_at=v_now WHERE account_id=p_user AND source NOT IN ('daily','invitation_gift','referral_bonus','admin_bonus')
    AND paid_preserved_at IS NULL AND archived_at IS NULL;
  IF v_legacy>0 THEN
    INSERT INTO public.credit_ledger(account_id,delta,kind,idempotency_key,balance_after,metadata)
      VALUES(p_user,-v_legacy,'adjustment','daily-conversion:'||p_user,public.credits_recompute_at(p_user,v_now),
        jsonb_build_object('reason','Previous promotional balance preserved separately','legacy_delta',0,'preserved_legacy_units',v_legacy));
  END IF;
  IF v_cycle.id IS NULL OR v_cycle.ends_at<=v_now OR (v_cycle.allowance=0 AND v_amount>0) THEN
    INSERT INTO public.credit_allowance_cycles(account_id,issued_at,ends_at,allowance,policy_version)
      VALUES(p_user,v_now,CASE WHEN v_amount>0 THEN v_now+interval '24 hours' END,v_amount,v_version) RETURNING * INTO v_cycle;
    IF v_amount>0 THEN
      INSERT INTO public.credit_lots(account_id,source,credits_granted,credits_remaining,granted_at,expires_at,budget_date,allowance_cycle_id,note)
        VALUES(p_user,'daily',v_amount,v_amount,v_now,v_cycle.ends_at,(v_now AT TIME ZONE 'Asia/Kolkata')::date,v_cycle.id,'24-hour allowance') RETURNING id INTO v_id;
      UPDATE public.credit_allowance_cycles SET lot_id=v_id WHERE id=v_cycle.id;
      UPDATE public.credit_accounts SET lifetime_granted=lifetime_granted+v_amount WHERE wholesaler_id=p_user;
      INSERT INTO public.credit_ledger(account_id,delta,kind,reference_type,idempotency_key,balance_after,metadata)
        VALUES(p_user,v_amount,'grant','daily','allowance:'||v_cycle.id,public.credits_recompute_at(p_user,v_now),
          jsonb_build_object('source','daily','cycle_id',v_cycle.id,'allowance',v_amount,'policy_version',v_version));
    END IF;
  END IF;
  v_balance:=public.credits_recompute_at(p_user,v_now);
  RETURN jsonb_build_object('ok',true,'mode','daily','policy_type','rolling_24h','balance',v_balance,
    'budget_date',(v_cycle.issued_at AT TIME ZONE 'Asia/Kolkata')::date,'daily_allowance',v_cycle.allowance,
    'next_allowance',v_amount,'policy_version',v_version,'cycle_policy_version',v_cycle.policy_version,
    'is_custom',v_custom,'is_paused',v_cycle.allowance=0,'pause_scheduled',v_amount=0 AND v_cycle.allowance>0,
    'issued_at',v_cycle.issued_at,'resets_at',v_cycle.ends_at,'server_now',v_now);
END $$;

CREATE OR REPLACE FUNCTION public.admin_list_business_credit_allowances(p_business_type TEXT,p_search TEXT DEFAULT NULL,p_page INT DEFAULT 0,
  p_page_size INT DEFAULT 25,p_wholesaler_id UUID DEFAULT NULL) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE v_now TIMESTAMPTZ:=clock_timestamp(); v_default INT; v_enabled BOOLEAN;
  v_limit INT:=greatest(1,least(coalesce(p_page_size,25),100)); v_page INT:=greatest(0,least(coalesce(p_page,0),10000));
  v_search TEXT:=nullif(btrim(p_search),''); v_count INT; v_rows JSONB;
BEGIN
  IF p_business_type IS NULL OR p_business_type NOT IN ('wholesaler','retailer') THEN RETURN jsonb_build_object('ok',false,'error','INVALID_ARGUMENTS'); END IF;
  SELECT daily_allowance,daily_enabled INTO v_default,v_enabled FROM public.credit_program WHERE singleton;
  IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'error','CREDIT_PROGRAM_UNAVAILABLE'); END IF;
  SELECT count(*)::int INTO v_count FROM public.credit_allowance_businesses w
    WHERE w.business_type=p_business_type AND (p_wholesaler_id IS NULL OR w.id=p_wholesaler_id)
    AND (v_search IS NULL OR w.business_name ILIKE '%'||v_search||'%' OR w.full_name ILIKE '%'||v_search||'%' OR w.phone ILIKE '%'||v_search||'%');
  SELECT coalesce(jsonb_agg(item ORDER BY name,id),'[]'::jsonb) INTO v_rows FROM (
    SELECT w.business_name AS name,w.id AS id,jsonb_build_object('business_type',w.business_type,'business_id',w.id,'wholesaler_id',w.id,'user_id',w.user_id,
      'business_name',w.business_name,'full_name',w.full_name,'phone',w.phone,'verification_status',w.verification_status,
      'daily_allowance',CASE WHEN p.wholesaler_user_id IS NULL OR p.inherit_default THEN v_default ELSE p.daily_allowance END,
      'is_custom',p.wholesaler_user_id IS NOT NULL AND NOT p.inherit_default,'policy_version',coalesce(p.version,0),
      'last_reason',p.reason,'policy_updated_at',p.updated_at,
      'current_allowance',CASE WHEN c.ends_at>v_now THEN c.allowance WHEN c.id IS NULL AND l.granted_at+interval '24 hours'>v_now THEN l.credits_granted END,
      'next_refill_at',CASE WHEN c.ends_at>v_now THEN c.ends_at WHEN c.id IS NULL AND l.granted_at+interval '24 hours'>v_now THEN l.granted_at+interval '24 hours' END,
      'admin_available',coalesce(b.admin_bonus,0),'recurring_available',coalesce(b.recurring,0),'gift_available',coalesce(b.gifts,0),'paid_available',coalesce(b.paid,0),
      'available',coalesce(b.total,0),'lifetime_spent',coalesce(a.lifetime_spent,0),'lifetime_granted',coalesce(a.lifetime_granted,0)) AS item
    FROM public.credit_allowance_businesses w LEFT JOIN public.credit_allowance_policies p ON p.wholesaler_user_id=w.user_id
    LEFT JOIN public.credit_accounts a ON a.wholesaler_id=w.user_id
    LEFT JOIN LATERAL(SELECT * FROM public.credit_allowance_cycles WHERE account_id=w.user_id ORDER BY issued_at DESC,id DESC LIMIT 1)c ON true
    LEFT JOIN LATERAL(SELECT * FROM public.credit_lots WHERE account_id=w.user_id AND source='daily' AND archived_at IS NULL
      AND allowance_cycle_id IS NULL ORDER BY granted_at DESC,id DESC LIMIT 1)l ON true
    LEFT JOIN LATERAL(SELECT sum(credits_remaining) FILTER(WHERE source='admin_bonus')::int admin_bonus,
      sum(credits_remaining) FILTER(WHERE source='daily')::int recurring,
      sum(credits_remaining) FILTER(WHERE source IN ('invitation_gift','referral_bonus'))::int gifts,
      sum(credits_remaining) FILTER(WHERE paid_preserved_at IS NOT NULL)::int paid,sum(credits_remaining)::int total
      FROM public.credit_lots WHERE account_id=w.user_id AND archived_at IS NULL AND (expires_at IS NULL OR expires_at>v_now)
      AND (NOT v_enabled OR source IN ('daily','invitation_gift','referral_bonus','admin_bonus') OR paid_preserved_at IS NOT NULL))b ON true
    WHERE w.business_type=p_business_type AND (p_wholesaler_id IS NULL OR w.id=p_wholesaler_id)
      AND (v_search IS NULL OR w.business_name ILIKE '%'||v_search||'%' OR w.full_name ILIKE '%'||v_search||'%' OR w.phone ILIKE '%'||v_search||'%')
    ORDER BY w.business_name,w.id OFFSET v_page*v_limit LIMIT v_limit
  )q;
  RETURN jsonb_build_object('ok',true,'items',v_rows,'total_count',v_count,'page',v_page,'page_size',v_limit,
    'server_now',v_now,'program_active',v_enabled,'default_allowance',v_default);
END $$;

CREATE OR REPLACE FUNCTION public.admin_grant_business_credits(p_business_type TEXT,p_business_id UUID,p_credits INT,p_reason TEXT,p_request_key UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE business RECORD; prior public.credit_admin_grants%ROWTYPE; owner UUID; lot UUID; ledger UUID;
 result JSONB; balance INT; stamp TIMESTAMPTZ;
BEGIN
 IF p_business_type IS NULL OR p_business_type NOT IN ('wholesaler','retailer') OR p_business_id IS NULL
 OR p_request_key IS NULL OR p_credits IS NULL OR p_credits NOT BETWEEN 1 AND 100000
 OR p_reason IS NULL OR length(btrim(p_reason)) NOT BETWEEN 1 AND 500 THEN
 RETURN jsonb_build_object('ok',false,'error','INVALID_ARGUMENTS'); END IF;
 PERFORM 1 FROM public.credit_program WHERE singleton FOR SHARE;
 PERFORM pg_advisory_xact_lock(hashtextextended('admin-credit-grant:'||p_request_key::text,0));
 SELECT * INTO prior FROM public.credit_admin_grants WHERE request_key=p_request_key;
 IF FOUND THEN
  IF prior.business_type<>p_business_type OR prior.business_id<>p_business_id OR prior.credits<>p_credits OR prior.reason<>btrim(p_reason) THEN
   RETURN jsonb_build_object('ok',false,'error','IDEMPOTENCY_CONFLICT'); END IF;
  RETURN prior.result||jsonb_build_object('replayed',true);
 END IF;
 SELECT * INTO business FROM public.credit_allowance_businesses WHERE business_type=p_business_type AND id=p_business_id;
 IF NOT FOUND OR business.user_id IS NULL OR business.verification_status<>'verified' THEN
  RETURN jsonb_build_object('ok',false,'error','NOT_VERIFIED'); END IF;
 owner:=business.user_id;
 IF public.credits_resolve_owner(owner) IS DISTINCT FROM owner THEN RETURN jsonb_build_object('ok',false,'error','NOT_VERIFIED'); END IF;
 PERFORM public.credits_ensure_account(owner);
 PERFORM 1 FROM public.credit_accounts WHERE wholesaler_id=owner FOR UPDATE;
 SELECT * INTO business FROM public.credit_allowance_businesses WHERE business_type=p_business_type AND id=p_business_id;
 IF NOT FOUND OR business.user_id IS DISTINCT FROM owner OR business.verification_status<>'verified'
 OR public.credits_resolve_owner(owner) IS DISTINCT FROM owner THEN RETURN jsonb_build_object('ok',false,'error','NOT_VERIFIED'); END IF;
 stamp:=clock_timestamp();
 INSERT INTO public.credit_lots(account_id,source,credits_granted,credits_remaining,expires_at,granted_at,note)
 VALUES(owner,'admin_bonus',p_credits,p_credits,NULL,stamp,btrim(p_reason)) RETURNING id INTO lot;
 UPDATE public.credit_accounts SET lifetime_granted=lifetime_granted+p_credits WHERE wholesaler_id=owner;
 balance:=public.credits_recompute_at(owner,stamp);
 INSERT INTO public.credit_ledger(account_id,delta,kind,reference_type,reference_id,idempotency_key,balance_after,metadata)
 VALUES(owner,p_credits,'grant','admin_bonus',lot::text,'admin-bonus:'||p_request_key,balance,
 jsonb_build_object('source','admin_bonus','actor','admin-password','business_type',p_business_type,'business_id',p_business_id,'reason',btrim(p_reason),'request_key',p_request_key)) RETURNING id INTO ledger;
 result:=jsonb_build_object('ok',true,'granted',p_credits,'available',balance,'lot_id',lot,'ledger_id',ledger,'replayed',false,'expires_at',NULL);
 INSERT INTO public.credit_admin_grants(request_key,business_type,business_id,account_id,credits,reason,actor,lot_id,ledger_id,result,created_at)
 VALUES(p_request_key,p_business_type,p_business_id,owner,p_credits,btrim(p_reason),'admin-password',lot,ledger,result,stamp);
 RETURN result;
END $$;
REVOKE ALL ON FUNCTION public.admin_grant_business_credits(TEXT,UUID,INT,TEXT,UUID) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.admin_grant_business_credits(TEXT,UUID,INT,TEXT,UUID) TO service_role;
NOTIFY pgrst,'reload schema';
COMMIT;
