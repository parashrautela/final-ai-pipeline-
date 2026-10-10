-- Extend the reviewed allowance contract to retailer/store owners.
-- Staff retain their owner's shared wallet. No flags are activated here.
BEGIN;
ALTER TABLE public.credit_allowance_policies ADD COLUMN IF NOT EXISTS business_type TEXT NOT NULL DEFAULT 'wholesaler' CHECK(business_type IN ('wholesaler','retailer'));
ALTER TABLE public.credit_allowance_policies ADD COLUMN IF NOT EXISTS business_id UUID;
ALTER TABLE public.credit_allowance_audit ADD COLUMN IF NOT EXISTS business_type TEXT NOT NULL DEFAULT 'wholesaler' CHECK(business_type IN ('wholesaler','retailer'));
ALTER TABLE public.credit_allowance_audit ADD COLUMN IF NOT EXISTS business_id UUID;
CREATE OR REPLACE VIEW public.credit_allowance_businesses WITH (security_invoker=true) AS
 SELECT 'wholesaler'::text business_type,id,user_id,business_name,full_name,coalesce(to_jsonb(w)->>'phone',to_jsonb(w)->>'phone_number',to_jsonb(w)->>'contact_number') phone,verification_status FROM public.wholesalers w
 UNION ALL SELECT 'retailer'::text,id,user_id,business_name,full_name,coalesce(to_jsonb(r)->>'phone',to_jsonb(r)->>'phone_number',to_jsonb(r)->>'contact_number'),verification_status FROM public.retailers r;
REVOKE ALL ON public.credit_allowance_businesses FROM PUBLIC,anon,authenticated;
GRANT SELECT ON public.credit_allowance_businesses TO service_role;
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
    WHERE account_id=p_user AND source NOT IN ('daily','invitation_gift','referral_bonus') AND paid_preserved_at IS NULL
      AND archived_at IS NULL AND (expires_at IS NULL OR expires_at>v_now);
  UPDATE public.credit_lots SET archived_at=v_now WHERE account_id=p_user AND source NOT IN ('daily','invitation_gift','referral_bonus')
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
REVOKE ALL ON FUNCTION public.credits_ensure_daily_budget(UUID) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.credits_ensure_daily_budget(UUID) TO service_role;

-- Remove old overloads: ordinary service callers must not bypass versions/replay keys.
CREATE OR REPLACE FUNCTION public.admin_set_business_credit_allowance(p_business_type TEXT,p_wholesaler_id UUID,p_daily_allowance INT,p_reason TEXT,
  p_expected_version INT,p_request_key UUID,p_reset BOOLEAN DEFAULT false) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE v_w RECORD; v_policy public.credit_allowance_policies%ROWTYPE;
  v_request public.credit_allowance_requests%ROWTYPE; v_payload JSONB; v_result JSONB;
  v_locked_owner UUID; v_default INT; v_enabled BOOLEAN; v_old INT; v_version INT; v_amount INT; v_now TIMESTAMPTZ; v_effective TIMESTAMPTZ;
BEGIN
  IF p_business_type IS NULL OR p_business_type NOT IN ('wholesaler','retailer') OR p_wholesaler_id IS NULL OR p_request_key IS NULL OR p_expected_version IS NULL OR p_expected_version<0
     OR p_reset IS NULL OR p_reason IS NULL OR length(btrim(p_reason)) NOT BETWEEN 1 AND 500
     OR (NOT p_reset AND (p_daily_allowance IS NULL OR p_daily_allowance NOT BETWEEN 0 AND 100000)) THEN
    RETURN jsonb_build_object('ok',false,'error','INVALID_ARGUMENTS'); END IF;
  SELECT daily_allowance,daily_enabled INTO v_default,v_enabled FROM public.credit_program WHERE singleton FOR SHARE;
  IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'error','CREDIT_PROGRAM_UNAVAILABLE'); END IF;
  SELECT * INTO v_w FROM public.credit_allowance_businesses WHERE id=p_wholesaler_id AND business_type=p_business_type;
  IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'error','WHOLESALER_NOT_FOUND'); END IF;
  IF v_w.user_id IS NULL OR v_w.verification_status IS DISTINCT FROM 'verified' THEN
    RETURN jsonb_build_object('ok',false,'error','NOT_VERIFIED'); END IF;
  v_locked_owner:=v_w.user_id;
  -- Serialize a request key even when it is reused against a different account.
  PERFORM pg_advisory_xact_lock(hashtextextended(p_request_key::text,0));
  v_payload:=jsonb_build_object('wholesaler_id',p_wholesaler_id,'amount',CASE WHEN p_reset THEN NULL ELSE p_daily_allowance END,
    'reason',btrim(p_reason),'expected_version',p_expected_version,'reset',p_reset) || CASE WHEN p_business_type='retailer' THEN jsonb_build_object('business_type',p_business_type) ELSE '{}'::jsonb END;
  SELECT * INTO v_request FROM public.credit_allowance_requests WHERE request_key=p_request_key;
  IF FOUND THEN
    IF v_request.payload IS DISTINCT FROM v_payload THEN RETURN jsonb_build_object('ok',false,'error','IDEMPOTENCY_CONFLICT'); END IF;
    RETURN v_request.result || jsonb_build_object('replayed',true);
  END IF;
  PERFORM public.credits_ensure_account(v_w.user_id);
  PERFORM 1 FROM public.credit_accounts WHERE wholesaler_id=v_w.user_id FOR UPDATE;
  SELECT * INTO v_w FROM public.credit_allowance_businesses WHERE id=p_wholesaler_id AND business_type=p_business_type;
  IF v_w.user_id IS DISTINCT FROM v_locked_owner OR v_w.verification_status IS DISTINCT FROM 'verified' THEN RETURN jsonb_build_object('ok',false,'error','NOT_VERIFIED'); END IF;
  v_now:=clock_timestamp();
  SELECT * INTO v_policy FROM public.credit_allowance_policies WHERE wholesaler_user_id=v_w.user_id;
  v_version:=coalesce(v_policy.version,0);
  IF v_version<>p_expected_version THEN RETURN jsonb_build_object('ok',false,'error','VERSION_CONFLICT','current_version',v_version); END IF;
  v_old:=CASE WHEN v_policy.wholesaler_user_id IS NULL OR v_policy.inherit_default THEN v_default ELSE v_policy.daily_allowance END;
  v_amount:=CASE WHEN p_reset THEN v_default ELSE p_daily_allowance END;
  SELECT ends_at INTO v_effective FROM public.credit_allowance_cycles WHERE account_id=v_w.user_id ORDER BY issued_at DESC,id DESC LIMIT 1;
  IF v_effective IS NULL THEN
    SELECT granted_at+interval '24 hours' INTO v_effective FROM public.credit_lots WHERE account_id=v_w.user_id
      AND source='daily' AND allowance_cycle_id IS NULL AND archived_at IS NULL ORDER BY granted_at DESC,id DESC LIMIT 1;
  END IF;
  v_effective:=greatest(v_now,coalesce(v_effective,v_now));
  INSERT INTO public.credit_allowance_policies(wholesaler_user_id,wholesaler_id,daily_allowance,is_paused,inherit_default,reason,updated_by,version,updated_at,business_type,business_id)
    VALUES(v_w.user_id,CASE WHEN p_business_type='wholesaler' THEN v_w.id END,v_amount,v_amount=0,p_reset,btrim(p_reason),'admin-web',v_version+1,v_now,p_business_type,v_w.id)
    ON CONFLICT(wholesaler_user_id) DO UPDATE SET daily_allowance=excluded.daily_allowance,is_paused=excluded.is_paused,
      inherit_default=excluded.inherit_default,reason=excluded.reason,updated_by=excluded.updated_by,version=excluded.version,updated_at=excluded.updated_at,business_type=excluded.business_type,business_id=excluded.business_id;
  INSERT INTO public.credit_allowance_audit(wholesaler_user_id,wholesaler_id,previous_allowance,new_allowance,previous_paused,new_paused,
    reason,actor,policy_version,inherited_default,effective_at,created_at,business_type,business_id)
    VALUES(v_w.user_id,CASE WHEN p_business_type='wholesaler' THEN v_w.id END,v_old,v_amount,v_old=0,v_amount=0,btrim(p_reason),'admin-web',v_version+1,p_reset,v_effective,v_now,p_business_type,v_w.id);
  v_result:=jsonb_build_object('ok',true,'business_type',p_business_type,'business_id',v_w.id,'wholesaler_id',v_w.id,'daily_allowance',v_amount,'next_allowance',v_amount,
    'is_custom',NOT p_reset,'is_paused',v_amount=0,'version',v_version+1,'effective_at',v_effective,'program_active',v_enabled,'updated_at',v_now);
  INSERT INTO public.credit_allowance_requests(request_key,wholesaler_user_id,payload,result) VALUES(p_request_key,v_w.user_id,v_payload,v_result);
  -- No wallet call: saving or viewing policies never issues credits or starts a window.
  RETURN v_result;
END $$;
REVOKE ALL ON FUNCTION public.admin_set_business_credit_allowance(TEXT,UUID,INT,TEXT,INT,UUID,BOOLEAN) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.admin_set_business_credit_allowance(TEXT,UUID,INT,TEXT,INT,UUID,BOOLEAN) TO service_role;

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
      'recurring_available',coalesce(b.recurring,0),'gift_available',coalesce(b.gifts,0),'paid_available',coalesce(b.paid,0),
      'available',coalesce(b.total,0),'lifetime_spent',coalesce(a.lifetime_spent,0),'lifetime_granted',coalesce(a.lifetime_granted,0)) AS item
    FROM public.credit_allowance_businesses w LEFT JOIN public.credit_allowance_policies p ON p.wholesaler_user_id=w.user_id
    LEFT JOIN public.credit_accounts a ON a.wholesaler_id=w.user_id
    LEFT JOIN LATERAL(SELECT * FROM public.credit_allowance_cycles WHERE account_id=w.user_id ORDER BY issued_at DESC,id DESC LIMIT 1)c ON true
    LEFT JOIN LATERAL(SELECT * FROM public.credit_lots WHERE account_id=w.user_id AND source='daily' AND archived_at IS NULL
      AND allowance_cycle_id IS NULL ORDER BY granted_at DESC,id DESC LIMIT 1)l ON true
    LEFT JOIN LATERAL(SELECT sum(credits_remaining) FILTER(WHERE source='daily')::int recurring,
      sum(credits_remaining) FILTER(WHERE source IN ('invitation_gift','referral_bonus'))::int gifts,
      sum(credits_remaining) FILTER(WHERE paid_preserved_at IS NOT NULL)::int paid,sum(credits_remaining)::int total
      FROM public.credit_lots WHERE account_id=w.user_id AND archived_at IS NULL AND (expires_at IS NULL OR expires_at>v_now)
      AND (NOT v_enabled OR source IN ('daily','invitation_gift','referral_bonus') OR paid_preserved_at IS NOT NULL))b ON true
    WHERE w.business_type=p_business_type AND (p_wholesaler_id IS NULL OR w.id=p_wholesaler_id)
      AND (v_search IS NULL OR w.business_name ILIKE '%'||v_search||'%' OR w.full_name ILIKE '%'||v_search||'%' OR w.phone ILIKE '%'||v_search||'%')
    ORDER BY w.business_name,w.id OFFSET v_page*v_limit LIMIT v_limit
  )q;
  RETURN jsonb_build_object('ok',true,'items',v_rows,'total_count',v_count,'page',v_page,'page_size',v_limit,
    'server_now',v_now,'program_active',v_enabled,'default_allowance',v_default);
END $$;
REVOKE ALL ON FUNCTION public.admin_list_business_credit_allowances(TEXT,TEXT,INT,INT,UUID) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.admin_list_business_credit_allowances(TEXT,TEXT,INT,INT,UUID) TO service_role;

CREATE OR REPLACE FUNCTION public.admin_set_credit_allowance(p_wholesaler_id UUID,p_daily_allowance INT,p_reason TEXT,p_expected_version INT,p_request_key UUID,p_reset BOOLEAN DEFAULT false) RETURNS JSONB
LANGUAGE sql SECURITY DEFINER SET search_path=public,pg_temp AS $$
 SELECT public.admin_set_business_credit_allowance('wholesaler',p_wholesaler_id,p_daily_allowance,p_reason,p_expected_version,p_request_key,p_reset);
$$;
REVOKE ALL ON FUNCTION public.admin_set_credit_allowance(UUID,INT,TEXT,INT,UUID,BOOLEAN) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.admin_set_credit_allowance(UUID,INT,TEXT,INT,UUID,BOOLEAN) TO service_role;
CREATE OR REPLACE FUNCTION public.admin_list_credit_allowances(p_search TEXT DEFAULT NULL,p_page INT DEFAULT 0,p_page_size INT DEFAULT 25,p_wholesaler_id UUID DEFAULT NULL) RETURNS JSONB
LANGUAGE sql SECURITY DEFINER SET search_path=public,pg_temp AS $$
 SELECT public.admin_list_business_credit_allowances('wholesaler',p_search,p_page,p_page_size,p_wholesaler_id);
$$;
REVOKE ALL ON FUNCTION public.admin_list_credit_allowances(TEXT,INT,INT,UUID) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.admin_list_credit_allowances(TEXT,INT,INT,UUID) TO service_role;
CREATE OR REPLACE FUNCTION public.credits_program_status() RETURNS JSONB
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=public,pg_temp AS $$
 SELECT jsonb_build_object('daily_enabled',daily_enabled,'payments_enabled',payments_enabled,'daily_allowance',daily_allowance,
 'timezone','Asia/Kolkata','wholesaler_policy','rolling_24h','retailer_policy','rolling_24h',
 'activated_at',activated_at,'payments_disabled_at',payments_disabled_at) FROM public.credit_program WHERE singleton;
$$;
REVOKE ALL ON FUNCTION public.credits_program_status() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.credits_program_status() TO anon,authenticated,service_role;
NOTIFY pgrst,'reload schema';
COMMIT;
