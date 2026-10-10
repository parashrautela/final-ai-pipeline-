-- Canonical, previously unreleased migration 023. Requires daily program 015,
-- invitation accounting 017 and purchased-credit preservation 018.
-- No activation or payment flags are changed by this migration.
BEGIN;
DO $$ BEGIN
  IF to_regclass('public.credit_paid_preservation') IS NULL
     OR to_regprocedure('public.credits_ensure_daily_budget(uuid)') IS NULL THEN
    RAISE EXCEPTION 'Apply and reconcile credit migrations 015, 017, 018 first';
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.credit_allowance_policies (
  wholesaler_user_id UUID PRIMARY KEY REFERENCES auth.users(id),
  wholesaler_id UUID REFERENCES public.wholesalers(id) ON DELETE CASCADE,
  daily_allowance INT NOT NULL CHECK(daily_allowance BETWEEN 0 AND 100000),
  is_paused BOOLEAN NOT NULL DEFAULT false,
  reason TEXT, updated_by TEXT NOT NULL DEFAULT 'admin-web',
  version INT NOT NULL DEFAULT 1 CHECK(version > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
-- Keep a versioned row when resetting, but restore default inheritance.
ALTER TABLE public.credit_allowance_policies ADD COLUMN IF NOT EXISTS inherit_default BOOLEAN NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS public.credit_allowance_audit (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(), wholesaler_user_id UUID NOT NULL,
  wholesaler_id UUID, previous_allowance INT, new_allowance INT NOT NULL,
  previous_paused BOOLEAN, new_paused BOOLEAN NOT NULL, reason TEXT,
  actor TEXT NOT NULL, policy_version INT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE public.credit_allowance_audit ADD COLUMN IF NOT EXISTS inherited_default BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE public.credit_allowance_audit ADD COLUMN IF NOT EXISTS effective_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_credit_allowance_audit_owner ON public.credit_allowance_audit(wholesaler_user_id,created_at DESC,id);
CREATE TABLE IF NOT EXISTS public.credit_allowance_requests (
  request_key UUID PRIMARY KEY, wholesaler_user_id UUID NOT NULL REFERENCES auth.users(id),
  payload JSONB NOT NULL, result JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS public.credit_allowance_cycles (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(), account_id UUID NOT NULL REFERENCES public.credit_accounts(wholesaler_id),
  issued_at TIMESTAMPTZ NOT NULL, ends_at TIMESTAMPTZ,
  allowance INT NOT NULL CHECK(allowance BETWEEN 0 AND 100000), policy_version INT NOT NULL,
  lot_id UUID UNIQUE REFERENCES public.credit_lots(id),
  CHECK((allowance = 0 AND ends_at IS NULL AND lot_id IS NULL) OR (allowance > 0 AND ends_at = issued_at + interval '24 hours'))
);
CREATE INDEX IF NOT EXISTS idx_credit_allowance_cycles_owner ON public.credit_allowance_cycles(account_id,issued_at DESC,id DESC);
ALTER TABLE public.credit_lots ADD COLUMN IF NOT EXISTS allowance_cycle_id UUID REFERENCES public.credit_allowance_cycles(id);
DROP INDEX IF EXISTS public.credit_lots_daily_once;
CREATE UNIQUE INDEX IF NOT EXISTS credit_lots_daily_calendar_once ON public.credit_lots(account_id,budget_date)
  WHERE source='daily' AND allowance_cycle_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS credit_lots_allowance_cycle_once ON public.credit_lots(allowance_cycle_id)
  WHERE allowance_cycle_id IS NOT NULL;

ALTER TABLE public.credit_allowance_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.credit_allowance_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.credit_allowance_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.credit_allowance_cycles ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.credit_allowance_policies,public.credit_allowance_audit,public.credit_allowance_requests,public.credit_allowance_cycles FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE ON public.credit_allowance_policies,public.credit_allowance_cycles TO service_role;
GRANT SELECT,INSERT ON public.credit_allowance_audit,public.credit_allowance_requests TO service_role;
-- Deny any inherited DELETE/UPDATE on append-only audit/replay records.
REVOKE UPDATE,DELETE,TRUNCATE ON public.credit_allowance_audit,public.credit_allowance_requests FROM service_role;

-- Keep the current retailer/staff calendar implementation. This block is replay-safe.
DO $$ BEGIN
  IF to_regprocedure('public.credits_ensure_daily_budget_calendar(uuid)') IS NULL THEN
    ALTER FUNCTION public.credits_ensure_daily_budget(UUID) RENAME TO credits_ensure_daily_budget_calendar;
  END IF;
END $$;
REVOKE ALL ON FUNCTION public.credits_ensure_daily_budget_calendar(UUID) FROM PUBLIC,anon,authenticated,service_role;

CREATE OR REPLACE FUNCTION public.credits_recompute_at(p_user UUID,p_now TIMESTAMPTZ) RETURNS INT
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE v_balance INT; v_daily BOOLEAN;
BEGIN
 SELECT daily_enabled INTO v_daily FROM public.credit_program WHERE singleton;
 SELECT coalesce(sum(credits_remaining),0)::int INTO v_balance FROM public.credit_lots
 WHERE account_id=p_user AND archived_at IS NULL AND credits_remaining>0 AND (expires_at IS NULL OR expires_at>p_now)
 AND (NOT v_daily OR source IN ('daily','invitation_gift','referral_bonus') OR paid_preserved_at IS NOT NULL);
 UPDATE public.credit_accounts SET balance_cached=v_balance,updated_at=p_now,
 low_balance_notified_at=CASE WHEN v_balance>low_balance_threshold THEN NULL ELSE low_balance_notified_at END WHERE wholesaler_id=p_user;
 RETURN v_balance;
END $$;
REVOKE ALL ON FUNCTION public.credits_recompute_at(UUID,TIMESTAMPTZ) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.credits_recompute_at(UUID,TIMESTAMPTZ) TO service_role;

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
  -- Only wholesalers change to rolling issuance. Retailer/staff budgets remain IST calendar days.
  IF NOT EXISTS(SELECT 1 FROM public.wholesalers WHERE user_id=p_user AND verification_status='verified') THEN
    RETURN public.credits_ensure_daily_budget_calendar(p_user) || jsonb_build_object('policy_type','calendar_day');
  END IF;
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
DROP FUNCTION IF EXISTS public.admin_reset_credit_allowance(UUID,TEXT,TEXT);
DROP FUNCTION IF EXISTS public.admin_set_credit_allowance(UUID,INT,TEXT,TEXT,INT);
DROP FUNCTION IF EXISTS public.admin_list_credit_allowances(TEXT,INT,INT);

CREATE OR REPLACE FUNCTION public.admin_set_credit_allowance(p_wholesaler_id UUID,p_daily_allowance INT,p_reason TEXT,
  p_expected_version INT,p_request_key UUID,p_reset BOOLEAN DEFAULT false) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE v_w public.wholesalers%ROWTYPE; v_policy public.credit_allowance_policies%ROWTYPE;
  v_request public.credit_allowance_requests%ROWTYPE; v_payload JSONB; v_result JSONB;
  v_default INT; v_enabled BOOLEAN; v_old INT; v_version INT; v_amount INT; v_now TIMESTAMPTZ; v_effective TIMESTAMPTZ;
BEGIN
  IF p_wholesaler_id IS NULL OR p_request_key IS NULL OR p_expected_version IS NULL OR p_expected_version<0
     OR p_reset IS NULL OR p_reason IS NULL OR length(btrim(p_reason)) NOT BETWEEN 1 AND 500
     OR (NOT p_reset AND (p_daily_allowance IS NULL OR p_daily_allowance NOT BETWEEN 0 AND 100000)) THEN
    RETURN jsonb_build_object('ok',false,'error','INVALID_ARGUMENTS'); END IF;
  SELECT daily_allowance,daily_enabled INTO v_default,v_enabled FROM public.credit_program WHERE singleton FOR SHARE;
  IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'error','CREDIT_PROGRAM_UNAVAILABLE'); END IF;
  SELECT * INTO v_w FROM public.wholesalers WHERE id=p_wholesaler_id;
  IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'error','WHOLESALER_NOT_FOUND'); END IF;
  IF v_w.user_id IS NULL OR v_w.verification_status IS DISTINCT FROM 'verified' THEN
    RETURN jsonb_build_object('ok',false,'error','NOT_VERIFIED'); END IF;
  -- Serialize a request key even when it is reused against a different account.
  PERFORM pg_advisory_xact_lock(hashtextextended(p_request_key::text,0));
  v_payload:=jsonb_build_object('wholesaler_id',p_wholesaler_id,'amount',CASE WHEN p_reset THEN NULL ELSE p_daily_allowance END,
    'reason',btrim(p_reason),'expected_version',p_expected_version,'reset',p_reset);
  SELECT * INTO v_request FROM public.credit_allowance_requests WHERE request_key=p_request_key;
  IF FOUND THEN
    IF v_request.payload IS DISTINCT FROM v_payload THEN RETURN jsonb_build_object('ok',false,'error','IDEMPOTENCY_CONFLICT'); END IF;
    RETURN v_request.result || jsonb_build_object('replayed',true);
  END IF;
  PERFORM public.credits_ensure_account(v_w.user_id);
  PERFORM 1 FROM public.credit_accounts WHERE wholesaler_id=v_w.user_id FOR UPDATE;
  SELECT * INTO v_w FROM public.wholesalers WHERE id=p_wholesaler_id;
  IF v_w.verification_status IS DISTINCT FROM 'verified' THEN RETURN jsonb_build_object('ok',false,'error','NOT_VERIFIED'); END IF;
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
  INSERT INTO public.credit_allowance_policies(wholesaler_user_id,wholesaler_id,daily_allowance,is_paused,inherit_default,reason,updated_by,version,updated_at)
    VALUES(v_w.user_id,v_w.id,v_amount,v_amount=0,p_reset,btrim(p_reason),'admin-web',v_version+1,v_now)
    ON CONFLICT(wholesaler_user_id) DO UPDATE SET daily_allowance=excluded.daily_allowance,is_paused=excluded.is_paused,
      inherit_default=excluded.inherit_default,reason=excluded.reason,updated_by=excluded.updated_by,version=excluded.version,updated_at=excluded.updated_at;
  INSERT INTO public.credit_allowance_audit(wholesaler_user_id,wholesaler_id,previous_allowance,new_allowance,previous_paused,new_paused,
    reason,actor,policy_version,inherited_default,effective_at,created_at)
    VALUES(v_w.user_id,v_w.id,v_old,v_amount,v_old=0,v_amount=0,btrim(p_reason),'admin-web',v_version+1,p_reset,v_effective,v_now);
  v_result:=jsonb_build_object('ok',true,'wholesaler_id',v_w.id,'daily_allowance',v_amount,'next_allowance',v_amount,
    'is_custom',NOT p_reset,'is_paused',v_amount=0,'version',v_version+1,'effective_at',v_effective,'program_active',v_enabled,'updated_at',v_now);
  INSERT INTO public.credit_allowance_requests(request_key,wholesaler_user_id,payload,result) VALUES(p_request_key,v_w.user_id,v_payload,v_result);
  -- No wallet call: saving or viewing policies never issues credits or starts a window.
  RETURN v_result;
END $$;
REVOKE ALL ON FUNCTION public.admin_set_credit_allowance(UUID,INT,TEXT,INT,UUID,BOOLEAN) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.admin_set_credit_allowance(UUID,INT,TEXT,INT,UUID,BOOLEAN) TO service_role;

CREATE OR REPLACE FUNCTION public.admin_list_credit_allowances(p_search TEXT DEFAULT NULL,p_page INT DEFAULT 0,
  p_page_size INT DEFAULT 25,p_wholesaler_id UUID DEFAULT NULL) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE v_now TIMESTAMPTZ:=clock_timestamp(); v_default INT; v_enabled BOOLEAN;
  v_limit INT:=greatest(1,least(coalesce(p_page_size,25),100)); v_page INT:=greatest(0,least(coalesce(p_page,0),10000));
  v_search TEXT:=nullif(btrim(p_search),''); v_count INT; v_rows JSONB;
BEGIN
  SELECT daily_allowance,daily_enabled INTO v_default,v_enabled FROM public.credit_program WHERE singleton;
  IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'error','CREDIT_PROGRAM_UNAVAILABLE'); END IF;
  SELECT count(*)::int INTO v_count FROM public.wholesalers w
    WHERE (p_wholesaler_id IS NULL OR w.id=p_wholesaler_id)
    AND (v_search IS NULL OR w.business_name ILIKE '%'||v_search||'%' OR w.full_name ILIKE '%'||v_search||'%' OR w.phone ILIKE '%'||v_search||'%');
  SELECT coalesce(jsonb_agg(item ORDER BY name,id),'[]'::jsonb) INTO v_rows FROM (
    SELECT w.business_name AS name,w.id AS id,jsonb_build_object('wholesaler_id',w.id,'user_id',w.user_id,
      'business_name',w.business_name,'full_name',w.full_name,'phone',w.phone,'verification_status',w.verification_status,
      'daily_allowance',CASE WHEN p.wholesaler_user_id IS NULL OR p.inherit_default THEN v_default ELSE p.daily_allowance END,
      'is_custom',p.wholesaler_user_id IS NOT NULL AND NOT p.inherit_default,'policy_version',coalesce(p.version,0),
      'last_reason',p.reason,'policy_updated_at',p.updated_at,
      'current_allowance',CASE WHEN c.ends_at>v_now THEN c.allowance WHEN c.id IS NULL AND l.granted_at+interval '24 hours'>v_now THEN l.credits_granted END,
      'next_refill_at',CASE WHEN c.ends_at>v_now THEN c.ends_at WHEN c.id IS NULL AND l.granted_at+interval '24 hours'>v_now THEN l.granted_at+interval '24 hours' END,
      'recurring_available',coalesce(b.recurring,0),'gift_available',coalesce(b.gifts,0),'paid_available',coalesce(b.paid,0),
      'available',coalesce(b.total,0),'lifetime_spent',coalesce(a.lifetime_spent,0),'lifetime_granted',coalesce(a.lifetime_granted,0)) AS item
    FROM public.wholesalers w LEFT JOIN public.credit_allowance_policies p ON p.wholesaler_user_id=w.user_id
    LEFT JOIN public.credit_accounts a ON a.wholesaler_id=w.user_id
    LEFT JOIN LATERAL(SELECT * FROM public.credit_allowance_cycles WHERE account_id=w.user_id ORDER BY issued_at DESC,id DESC LIMIT 1)c ON true
    LEFT JOIN LATERAL(SELECT * FROM public.credit_lots WHERE account_id=w.user_id AND source='daily' AND archived_at IS NULL
      AND allowance_cycle_id IS NULL ORDER BY granted_at DESC,id DESC LIMIT 1)l ON true
    LEFT JOIN LATERAL(SELECT sum(credits_remaining) FILTER(WHERE source='daily')::int recurring,
      sum(credits_remaining) FILTER(WHERE source IN ('invitation_gift','referral_bonus'))::int gifts,
      sum(credits_remaining) FILTER(WHERE paid_preserved_at IS NOT NULL)::int paid,sum(credits_remaining)::int total
      FROM public.credit_lots WHERE account_id=w.user_id AND archived_at IS NULL AND (expires_at IS NULL OR expires_at>v_now)
      AND (NOT v_enabled OR source IN ('daily','invitation_gift','referral_bonus') OR paid_preserved_at IS NOT NULL))b ON true
    WHERE (p_wholesaler_id IS NULL OR w.id=p_wholesaler_id)
      AND (v_search IS NULL OR w.business_name ILIKE '%'||v_search||'%' OR w.full_name ILIKE '%'||v_search||'%' OR w.phone ILIKE '%'||v_search||'%')
    ORDER BY w.business_name,w.id OFFSET v_page*v_limit LIMIT v_limit
  )q;
  RETURN jsonb_build_object('ok',true,'items',v_rows,'total_count',v_count,'page',v_page,'page_size',v_limit,
    'server_now',v_now,'program_active',v_enabled,'default_allowance',v_default);
END $$;
REVOKE ALL ON FUNCTION public.admin_list_credit_allowances(TEXT,INT,INT,UUID) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.admin_list_credit_allowances(TEXT,INT,INT,UUID) TO service_role;

-- Preserve legacy/paid accounting while allocating at the budget authorization instant.
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
    AND (source IN ('daily','invitation_gift','referral_bonus') OR paid_preserved_at IS NOT NULL) AND credits_remaining > 0
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
  AND credits_remaining>0 AND (source IN ('daily','invitation_gift','referral_bonus') OR paid_preserved_at IS NOT NULL)
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
  v_now TIMESTAMPTZ; v_paid INT; v_gifts INT; v_expiring INT; v_next TIMESTAMPTZ; v_legacy INT; v_daily INT; v_bonus INT;
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
    coalesce(sum(credits_remaining) FILTER(WHERE (source IN ('invitation_gift','referral_bonus') OR paid_preserved_at IS NOT NULL)),0)::int
    INTO v_daily,v_bonus FROM public.credit_lots WHERE account_id = v_owner AND archived_at IS NULL
    AND (expires_at IS NULL OR expires_at > v_now);
  SELECT coalesce(sum(credits_remaining) FILTER(WHERE paid_preserved_at IS NOT NULL),0)::int,
    coalesce(sum(credits_remaining) FILTER(WHERE source IN ('invitation_gift','referral_bonus')),0)::int INTO v_paid,v_gifts
    FROM public.credit_lots WHERE account_id=v_owner AND archived_at IS NULL AND (expires_at IS NULL OR expires_at>v_now);
  RETURN v_budget || jsonb_build_object('paid_available',v_paid,'gift_available',v_gifts,'daily_available',v_daily,'bonus_available',v_bonus,'available',(v_budget->>'balance')::int,'lifetime_spent',v_acct.lifetime_spent,
    'lifetime_granted',v_acct.lifetime_granted,'lifetime_expired',v_acct.lifetime_expired,'expiring_soon',v_expiring,
    'next_expiry',v_next,'low_balance',v_acct.balance_cached <= v_acct.low_balance_threshold,
    'low_balance_threshold',v_acct.low_balance_threshold,'recovery_owed',v_acct.recovery_owed,
    'legacy_preserved',v_legacy,'shared_business_wallet',v_owner <> auth.uid());
END;
$$;

REVOKE ALL ON FUNCTION public.spend_credits(UUID,TEXT,TEXT,TEXT,TEXT,JSONB),public.referral_fund(UUID,INT,UUID) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.spend_credits(UUID,TEXT,TEXT,TEXT,TEXT,JSONB),public.referral_fund(UUID,INT,UUID) TO service_role;
REVOKE ALL ON FUNCTION public.credits_wallet() FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION public.credits_wallet() TO authenticated,service_role;
CREATE OR REPLACE FUNCTION public.credits_program_status() RETURNS JSONB
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=public,pg_temp AS $$
 SELECT jsonb_build_object('daily_enabled',daily_enabled,'payments_enabled',payments_enabled,'daily_allowance',daily_allowance,
 'timezone','Asia/Kolkata','wholesaler_policy','rolling_24h','retailer_policy','calendar_day',
 'activated_at',activated_at,'payments_disabled_at',payments_disabled_at) FROM public.credit_program WHERE singleton;
$$;
REVOKE ALL ON FUNCTION public.credits_program_status() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.credits_program_status() TO anon,authenticated,service_role;
NOTIFY pgrst,'reload schema';
COMMIT;
