-- Requires 012, onboarding_three_doors, and credit migrations 015 + 016.
-- Additive: reward invitations stay disabled until explicit activation after validation.
BEGIN;
ALTER TABLE public.credit_program ADD COLUMN referral_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE public.credit_lots DROP CONSTRAINT credit_lots_source_check;
ALTER TABLE public.credit_lots ADD CONSTRAINT credit_lots_source_check CHECK
 (source IN ('welcome','purchase','subscription','referral','promo','refund','admin','daily','invitation_gift','referral_bonus'));
ALTER TABLE public.referral_links
 ADD COLUMN policy_version INT NOT NULL DEFAULT 0 CHECK(policy_version IN (0,1)),
 ADD COLUMN gift_credits INT NOT NULL DEFAULT 0,
 ADD COLUMN base_credits INT NOT NULL DEFAULT 0,
 ADD COLUMN extra_credits INT NOT NULL DEFAULT 0,
 ADD COLUMN generation_key UUID,
 ADD COLUMN funding_ledger_id UUID REFERENCES public.credit_ledger(id),
 ADD COLUMN gift_ledger_id UUID REFERENCES public.credit_ledger(id),
 ADD COLUMN reward_ledger_id UUID REFERENCES public.credit_ledger(id),
 ADD COLUMN release_ledger_id UUID REFERENCES public.credit_ledger(id),
 ADD COLUMN funding_state TEXT NOT NULL DEFAULT 'legacy' CHECK(funding_state IN ('legacy','reserved','settled','released')),
 ADD COLUMN released_at TIMESTAMPTZ,
 ADD COLUMN release_reason TEXT CHECK(release_reason IN ('cancelled','expired','rejected')),
 ADD COLUMN settled_at TIMESTAMPTZ,
 ADD CONSTRAINT referral_gift_terms CHECK(policy_version = 0 OR
   (base_credits = 1000 AND gift_credits BETWEEN 1000 AND 10000 AND gift_credits % 500 = 0
    AND extra_credits = gift_credits - base_credits AND generation_key IS NOT NULL AND funding_state <> 'legacy'));
CREATE UNIQUE INDEX referral_generation_once ON public.referral_links(wholesaler_id,generation_key) WHERE generation_key IS NOT NULL;
CREATE INDEX referral_report_page ON public.referral_links(created_at DESC,id DESC);
-- Only trusted RPCs may create or edit funded promises.
REVOKE INSERT, UPDATE, DELETE ON public.referral_links FROM anon,authenticated;
DROP POLICY IF EXISTS "Wholesalers can create own referral links" ON public.referral_links;
CREATE TABLE public.referral_preferences(user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
 skip_guide BOOLEAN NOT NULL DEFAULT false,updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp());
ALTER TABLE public.referral_preferences ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.referral_preferences FROM anon,authenticated;
CREATE TABLE public.referral_events(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
 invitation_id UUID NOT NULL REFERENCES public.referral_links(id),event TEXT NOT NULL,actor TEXT NOT NULL,
 metadata JSONB NOT NULL DEFAULT '{}',created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp());
CREATE UNIQUE INDEX referral_review_audit_once ON public.referral_events(invitation_id,event,actor) WHERE event='reviewed';
ALTER TABLE public.referral_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.referral_events FROM anon,authenticated;
GRANT ALL ON public.referral_preferences,public.referral_events TO service_role;
CREATE OR REPLACE FUNCTION public.credits_recompute(p_user UUID) RETURNS INT
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE v_balance INT; v_daily BOOLEAN; v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
  SELECT daily_enabled INTO v_daily FROM public.credit_program WHERE singleton;
  SELECT coalesce(sum(credits_remaining), 0)::int INTO v_balance FROM public.credit_lots
    WHERE account_id = p_user AND archived_at IS NULL AND credits_remaining > 0
    AND (expires_at IS NULL OR expires_at > v_now) AND (NOT v_daily OR source IN ('daily','invitation_gift','referral_bonus'));
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
    WHERE account_id = p_user AND source NOT IN ('daily','invitation_gift','referral_bonus') AND archived_at IS NULL AND credits_remaining > 0
      AND (expires_at IS NULL OR expires_at > v_now);
  UPDATE public.credit_lots SET archived_at = v_now
    WHERE account_id = p_user AND source NOT IN ('daily','invitation_gift','referral_bonus') AND archived_at IS NULL;
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
    AND source IN ('daily','invitation_gift','referral_bonus') AND credits_remaining > 0
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
        AND source IN ('daily','invitation_gift','referral_bonus')
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
    coalesce(sum(credits_remaining) FILTER(WHERE source IN ('invitation_gift','referral_bonus')),0)::int
    INTO v_daily,v_bonus FROM public.credit_lots WHERE account_id = v_owner AND archived_at IS NULL
    AND (expires_at IS NULL OR expires_at > clock_timestamp());
  RETURN v_budget || jsonb_build_object('daily_available',v_daily,'bonus_available',v_bonus,'available',(v_budget->>'balance')::int,'lifetime_spent',v_acct.lifetime_spent,
    'lifetime_granted',v_acct.lifetime_granted,'lifetime_expired',v_acct.lifetime_expired,'expiring_soon',v_expiring,
    'next_expiry',v_next,'low_balance',v_acct.balance_cached <= v_acct.low_balance_threshold,
    'low_balance_threshold',v_acct.low_balance_threshold,'recovery_owed',v_acct.recovery_owed,
    'legacy_preserved',v_legacy,'shared_business_wallet',v_owner <> auth.uid());
END;
$$;

CREATE FUNCTION public.referral_settings(p_user UUID,p_skip BOOLEAN DEFAULT NULL) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public,pg_temp AS $$
DECLARE v_skip BOOLEAN; v_budget JSONB;
BEGIN
 IF NOT EXISTS(SELECT 1 FROM wholesalers WHERE user_id=p_user AND verification_status='verified') THEN
  RETURN jsonb_build_object('ok',false,'error','NOT_VERIFIED'); END IF;
 IF p_skip IS NOT NULL THEN
  INSERT INTO referral_preferences(user_id,skip_guide) VALUES(p_user,p_skip)
  ON CONFLICT(user_id) DO UPDATE SET skip_guide=EXCLUDED.skip_guide,updated_at=clock_timestamp();
 END IF;
 SELECT skip_guide INTO v_skip FROM referral_preferences WHERE user_id=p_user;
 v_budget := credits_ensure_daily_budget(p_user);
 IF NOT coalesce((v_budget->>'ok')::boolean,false) THEN RETURN v_budget; END IF;
 RETURN jsonb_build_object('ok',true,'skip_guide',coalesce(v_skip,false),'enabled',
  (SELECT referral_enabled AND daily_enabled FROM credit_program WHERE singleton),
  'available',coalesce((v_budget->>'balance')::int,0),'minimum',1000,'step',500,'maximum',10000,'reward',1000);
END; $$;

-- Arbitrary amounts are restricted to trusted callers. The inviter cannot mint or price a gift.
CREATE FUNCTION public.referral_fund(p_user UUID,p_units INT,p_invitation UUID) RETURNS UUID
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
  AND credits_remaining>0 AND source IN ('daily','invitation_gift','referral_bonus')
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

CREATE FUNCTION public.referral_generate(p_user UUID,p_gift INT,p_key UUID,p_source TEXT DEFAULT 'web') RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public,pg_temp AS $$
DECLARE v_ws wholesalers%ROWTYPE; v_link referral_links%ROWTYPE; v_budget JSONB; v_id UUID := gen_random_uuid();
BEGIN
 PERFORM 1 FROM credit_program WHERE singleton FOR SHARE;
 SELECT * INTO v_ws FROM wholesalers WHERE user_id=p_user AND verification_status='verified';
 IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'error','NOT_VERIFIED'); END IF;
 IF p_key IS NULL OR p_gift IS NULL OR p_gift<1000 OR p_gift>10000 OR p_gift%500<>0 OR p_source IS NULL OR p_source NOT IN ('web','ios') THEN
  RETURN jsonb_build_object('ok',false,'error','INVALID_SETTINGS'); END IF;
 -- Serialize generation per inviter before checking replay; a lost response never charges twice.
 PERFORM credits_ensure_account(p_user);
 PERFORM 1 FROM credit_accounts WHERE wholesaler_id=p_user FOR UPDATE;
 SELECT * INTO v_link FROM referral_links WHERE wholesaler_id=v_ws.id AND generation_key=p_key;
 IF FOUND THEN
  IF v_link.gift_credits<>p_gift THEN RETURN jsonb_build_object('ok',false,'error','IDEMPOTENCY_CONFLICT'); END IF;
  RETURN jsonb_build_object('ok',true,'replayed',true)||to_jsonb(v_link);
 END IF;
 IF NOT (SELECT referral_enabled AND daily_enabled FROM credit_program WHERE singleton) THEN
  RETURN jsonb_build_object('ok',false,'error','REFERRALS_PAUSED'); END IF;
 v_budget:=credits_ensure_daily_budget(p_user);
 IF NOT coalesce((v_budget->>'ok')::boolean,false) THEN RETURN v_budget; END IF;
 IF (v_budget->>'balance')::int<p_gift-1000 THEN
  RETURN jsonb_build_object('ok',false,'error','INSUFFICIENT_CREDITS','balance',(v_budget->>'balance')::int,'required',p_gift-1000); END IF;
 INSERT INTO referral_links(id,wholesaler_id,code,max_uses,uses_count,is_active,source,expires_at,
  policy_version,gift_credits,base_credits,extra_credits,generation_key,funding_state)
 VALUES(v_id,v_ws.id,'JI-'||upper(replace(gen_random_uuid()::text,'-','')),1,0,true,p_source,clock_timestamp()+interval '7 days',
  1,p_gift,1000,p_gift-1000,p_key,'reserved');
 UPDATE referral_links SET funding_ledger_id=referral_fund(p_user,p_gift-1000,v_id) WHERE id=v_id RETURNING * INTO v_link;
 INSERT INTO referral_events(invitation_id,event,actor,metadata) VALUES(v_id,'generated',p_user::text,jsonb_build_object('gift',p_gift,'extra',p_gift-1000));
 RETURN jsonb_build_object('ok',true,'balance',credits_recompute(p_user))||to_jsonb(v_link);
END; $$;

CREATE FUNCTION public.referral_release(p_invitation UUID,p_reason TEXT,p_actor TEXT) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public,pg_temp AS $$
DECLARE v_link referral_links%ROWTYPE; v_result JSONB; v_refund UUID;
BEGIN
 PERFORM 1 FROM credit_program WHERE singleton FOR SHARE;
 SELECT * INTO v_link FROM referral_links WHERE id=p_invitation FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'error','NOT_FOUND'); END IF;
 IF v_link.policy_version<>1 OR v_link.funding_state='settled' THEN RETURN jsonb_build_object('ok',false,'error','NOT_RELEASABLE'); END IF;
 IF v_link.funding_state='released' THEN RETURN jsonb_build_object('ok',true,'replayed',true); END IF;
 IF p_reason NOT IN ('cancelled','expired','rejected') OR p_reason IS NULL THEN RAISE EXCEPTION 'INVALID_REASON'; END IF;
 IF p_reason IN ('cancelled','expired') AND v_link.accepted_by IS NOT NULL THEN RETURN jsonb_build_object('ok',false,'error','ALREADY_ACCEPTED'); END IF;
 IF p_reason='expired' AND v_link.expires_at>clock_timestamp() THEN RETURN jsonb_build_object('ok',false,'error','NOT_EXPIRED'); END IF;
 IF p_reason='rejected' AND NOT EXISTS(SELECT 1 FROM retailers WHERE user_id=v_link.accepted_by AND verification_status='rejected') THEN
  RETURN jsonb_build_object('ok',false,'error','NOT_REJECTED'); END IF;
 IF v_link.funding_ledger_id IS NOT NULL THEN
  v_result:=refund_debit(v_link.funding_ledger_id,'Invitation '||p_reason);
  IF NOT coalesce((v_result->>'ok')::boolean,false) THEN RAISE EXCEPTION 'INVITATION_REFUND_FAILED'; END IF;
  SELECT id INTO v_refund FROM credit_ledger WHERE idempotency_key='refund:'||v_link.funding_ledger_id;
 END IF;
 UPDATE referral_links SET funding_state='released',released_at=clock_timestamp(),release_reason=p_reason,
  release_ledger_id=v_refund,is_active=false,updated_at=clock_timestamp() WHERE id=p_invitation;
 INSERT INTO referral_events(invitation_id,event,actor,metadata) VALUES(p_invitation,p_reason,p_actor,coalesce(v_result,'{}'));
 RETURN jsonb_build_object('ok',true,'refunded',coalesce((v_result->>'refunded')::int,0));
END; $$;

CREATE FUNCTION public.referral_cancel(p_user UUID,p_invitation UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM referral_links l JOIN wholesalers w ON w.id=l.wholesaler_id
  WHERE l.id=p_invitation AND w.user_id=p_user) THEN RETURN jsonb_build_object('ok',false,'error','NOT_FOUND'); END IF;
 RETURN referral_release(p_invitation,'cancelled',p_user::text);
END; $$;

CREATE FUNCTION public.referral_expire_due(p_limit INT DEFAULT 100) RETURNS INT
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE v_id UUID; v_count INT:=0;
BEGIN
 PERFORM 1 FROM credit_program WHERE singleton FOR SHARE;
 FOR v_id IN SELECT id FROM referral_links WHERE policy_version=1 AND funding_state='reserved'
  AND accepted_by IS NULL AND expires_at<=clock_timestamp() ORDER BY id LIMIT least(greatest(p_limit,1),500) FOR UPDATE SKIP LOCKED LOOP
  PERFORM referral_release(v_id,'expired','system:expiry'); v_count:=v_count+1;
 END LOOP;
 RETURN v_count;
END; $$;

CREATE OR REPLACE FUNCTION public.spend_referral_code(p_code TEXT,p_retailer_user UUID) RETURNS UUID
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE v_link referral_links%ROWTYPE;
BEGIN
 PERFORM 1 FROM credit_program WHERE singleton FOR SHARE;
 IF p_retailer_user IS NULL OR coalesce(btrim(p_code),'')='' THEN RAISE EXCEPTION 'INVITE_CODE_REQUIRED' USING ERRCODE='23514'; END IF;
 SELECT * INTO v_link FROM referral_links WHERE upper(code)=upper(btrim(p_code)) FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'INVITE_CODE_NOT_FOUND' USING ERRCODE='23514'; END IF;
 IF v_link.accepted_by=p_retailer_user AND EXISTS(SELECT 1 FROM retailers WHERE user_id=p_retailer_user AND referred_by=v_link.wholesaler_id AND upper(referral_code)=upper(v_link.code)) THEN RETURN v_link.wholesaler_id; END IF;
 IF v_link.accepted_by IS NOT NULL OR coalesce(v_link.uses_count,0)>=1 THEN RAISE EXCEPTION 'INVITE_CODE_USED' USING ERRCODE='23514'; END IF;
 IF v_link.expires_at IS NULL OR v_link.expires_at<=clock_timestamp() THEN RAISE EXCEPTION 'INVITE_CODE_EXPIRED' USING ERRCODE='23514'; END IF;
 IF NOT v_link.is_active OR v_link.funding_state='released' OR NOT EXISTS(SELECT 1 FROM wholesalers WHERE id=v_link.wholesaler_id AND verification_status='verified') THEN RAISE EXCEPTION 'INVITE_CODE_INACTIVE' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM wholesalers WHERE id=v_link.wholesaler_id AND user_id=p_retailer_user) THEN RAISE EXCEPTION 'SELF_REFERRAL' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM retailers WHERE user_id=p_retailer_user AND (referred_by IS NOT NULL OR verification_status='verified')) THEN RAISE EXCEPTION 'RETAILER_ALREADY_ATTRIBUTED' USING ERRCODE='23514'; END IF;
 UPDATE referral_links SET accepted_by=p_retailer_user,accepted_at=clock_timestamp(),uses_count=1,max_uses=1,is_active=false,updated_at=clock_timestamp() WHERE id=v_link.id;
 INSERT INTO referral_events(invitation_id,event,actor) VALUES(v_link.id,'accepted',p_retailer_user::text);
 RETURN v_link.wholesaler_id;
END; $$;

CREATE OR REPLACE FUNCTION public.claim_retailer_referral(p_code TEXT,p_retailer_user UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE v_r retailers%ROWTYPE; v_ws UUID;
BEGIN
 SELECT * INTO v_r FROM retailers WHERE user_id=p_retailer_user FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'error','RETAILER_NOT_FOUND'); END IF;
 IF v_r.referred_by IS NOT NULL THEN
  IF upper(v_r.referral_code)=upper(btrim(p_code)) THEN RETURN jsonb_build_object('ok',true,'replayed',true,'wholesaler_id',v_r.referred_by); END IF;
  RETURN jsonb_build_object('ok',false,'error','RETAILER_ALREADY_ATTRIBUTED'); END IF;
 IF upper(v_r.referral_code) IS DISTINCT FROM upper(btrim(p_code)) THEN RETURN jsonb_build_object('ok',false,'error','INVITATION_NOT_ATTACHED'); END IF;
 v_ws:=spend_referral_code(p_code,p_retailer_user);
 PERFORM set_config('jewel.trusted','on',true);
 UPDATE retailers SET referred_by=v_ws,referral_code=upper(btrim(p_code)) WHERE id=v_r.id;
 RETURN jsonb_build_object('ok',true,'wholesaler_id',v_ws);
END; $$;

CREATE FUNCTION public.referral_bonus_grant(p_user UUID,p_units INT,p_source TEXT,p_invitation UUID) RETURNS UUID
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE v_id UUID; v_lot UUID; v_existing credit_ledger%ROWTYPE; v_key TEXT:=p_source||':'||p_invitation; v_budget JSONB;
BEGIN
 IF p_source NOT IN ('invitation_gift','referral_bonus') OR p_units<1000 THEN RAISE EXCEPTION 'INVALID_BONUS'; END IF;
 SELECT * INTO v_existing FROM credit_ledger WHERE idempotency_key=v_key;
 IF FOUND THEN
  IF v_existing.account_id IS DISTINCT FROM p_user OR v_existing.delta IS DISTINCT FROM p_units
   OR v_existing.kind<>'grant' OR v_existing.reference_type IS DISTINCT FROM p_source
   OR v_existing.reference_id IS DISTINCT FROM p_invitation::text
   OR NOT EXISTS(SELECT 1 FROM credit_lots WHERE id=(v_existing.metadata->>'lot_id')::uuid
    AND account_id=p_user AND source=p_source AND credits_granted=p_units AND expires_at IS NULL AND archived_at IS NULL) THEN RAISE EXCEPTION 'BONUS_LEDGER_CONFLICT'; END IF;
  RETURN v_existing.id; END IF;
 v_budget:=credits_ensure_daily_budget(p_user);
 IF NOT coalesce((v_budget->>'ok')::boolean,false) THEN RAISE EXCEPTION 'BONUS_ACCOUNT_NOT_ELIGIBLE'; END IF;
 INSERT INTO credit_lots(account_id,source,credits_granted,credits_remaining,note)
 VALUES(p_user,p_source,p_units,p_units,CASE WHEN p_source='invitation_gift' THEN 'Verified invitation gift' ELSE 'Verified retailer referral reward' END) RETURNING id INTO v_lot;
 UPDATE credit_accounts SET lifetime_granted=lifetime_granted+p_units WHERE wholesaler_id=p_user;
 INSERT INTO credit_ledger(account_id,delta,kind,reference_type,reference_id,idempotency_key,balance_after,metadata)
 VALUES(p_user,p_units,'grant',p_source,p_invitation::text,v_key,credits_recompute(p_user),jsonb_build_object('source',p_source,'lot_id',v_lot,'never_expires',true)) RETURNING id INTO v_id;
 RETURN v_id;
END; $$;

CREATE FUNCTION public.referral_settle(p_retailer UUID,p_actor TEXT DEFAULT 'system:verification') RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE v_r retailers%ROWTYPE; v_link referral_links%ROWTYPE; v_user UUID; v_gift UUID; v_reward UUID; v_owner UUID;
BEGIN
 PERFORM 1 FROM credit_program WHERE singleton FOR SHARE;
 SELECT * INTO v_r FROM retailers WHERE id=p_retailer FOR UPDATE;
 IF NOT FOUND OR v_r.verification_status IS DISTINCT FROM 'verified' THEN RETURN jsonb_build_object('ok',false,'error','NOT_VERIFIED'); END IF;
 SELECT * INTO v_link FROM referral_links WHERE wholesaler_id=v_r.referred_by AND upper(code)=upper(v_r.referral_code) FOR UPDATE;
 IF NOT FOUND OR v_link.policy_version<>1 THEN RETURN jsonb_build_object('ok',true,'legacy',true); END IF;
 IF v_link.funding_state='settled' THEN RETURN jsonb_build_object('ok',true,'replayed',true); END IF;
 IF v_link.funding_state<>'reserved' OR v_link.accepted_by IS DISTINCT FROM v_r.user_id THEN RAISE EXCEPTION 'REFERRAL_NOT_SETTLEABLE'; END IF;
 SELECT user_id INTO v_user FROM wholesalers WHERE id=v_link.wholesaler_id AND verification_status='verified';
 IF v_user IS NULL OR v_user=v_r.user_id THEN RAISE EXCEPTION 'INVITER_NOT_ELIGIBLE'; END IF;
 IF v_link.extra_credits>0 AND NOT EXISTS(SELECT 1 FROM credit_ledger WHERE id=v_link.funding_ledger_id
  AND account_id=v_user AND kind='debit' AND delta=-v_link.extra_credits AND reference_type='invitation_funding'
  AND reference_id=v_link.id::text) THEN RAISE EXCEPTION 'INVITATION_FUNDING_MISMATCH'; END IF;
 -- Sorted wallet locks prevent two unrelated approvals from deadlocking.
 FOR v_owner IN SELECT u FROM unnest(ARRAY[v_user,v_r.user_id]) u ORDER BY u LOOP
  PERFORM credits_ensure_account(v_owner);
  PERFORM 1 FROM credit_accounts WHERE wholesaler_id=v_owner FOR UPDATE;
 END LOOP;
 v_gift:=referral_bonus_grant(v_r.user_id,v_link.gift_credits,'invitation_gift',v_link.id);
 v_reward:=referral_bonus_grant(v_user,1000,'referral_bonus',v_link.id);
 UPDATE referral_links SET funding_state='settled',gift_ledger_id=v_gift,reward_ledger_id=v_reward,
  settled_at=clock_timestamp(),rewarded_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=v_link.id;
 INSERT INTO referral_events(invitation_id,event,actor,metadata) VALUES(v_link.id,'settled',p_actor,jsonb_build_object('gift_ledger',v_gift,'reward_ledger',v_reward));
 RETURN jsonb_build_object('ok',true,'gift',v_link.gift_credits,'reward',1000);
END; $$;

-- Preserve legacy behavior; never auto-backfill old rewarded_at records.
ALTER FUNCTION public.credits_grant_retailer_referral() RENAME TO credits_grant_retailer_referral_legacy;
CREATE FUNCTION public.credits_grant_retailer_referral() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE v_link referral_links%ROWTYPE;
BEGIN
 SELECT * INTO v_link FROM referral_links WHERE wholesaler_id=NEW.referred_by AND upper(code)=upper(NEW.referral_code);
 IF v_link.policy_version=1 THEN
  IF NEW.verification_status='verified' THEN PERFORM referral_settle(NEW.id,coalesce(nullif(current_setting('jewel.admin_actor',true),''),'system:verification'));
  ELSIF NEW.verification_status='rejected' AND v_link.funding_state='reserved' THEN
   PERFORM referral_release(v_link.id,'rejected',coalesce(nullif(current_setting('jewel.admin_actor',true),''),'system:rejection'));
  END IF;
 ELSIF NEW.verification_status='verified' AND (TG_OP='INSERT' OR OLD.verification_status IS DISTINCT FROM 'verified') AND NEW.referred_by IS NOT NULL THEN
  -- Exactly the original 012 behavior, using its original ledger key.
  PERFORM grant_credits(w.user_id,1000,'referral','retailer-referral:'||NEW.id,NULL,NULL,
   'Verified retailer referral reward',jsonb_build_object('retailer_id',NEW.id),'retailer_referral',NEW.id::text)
   FROM wholesalers w WHERE w.id=NEW.referred_by;
  UPDATE referral_links SET rewarded_at=coalesce(rewarded_at,clock_timestamp()) WHERE id=v_link.id;
 END IF;
 RETURN NEW;
END; $$;
DROP TRIGGER trg_credits_retailer_referral ON retailers;
CREATE TRIGGER trg_credits_retailer_referral AFTER INSERT OR UPDATE OF verification_status ON retailers
 FOR EACH ROW EXECUTE FUNCTION credits_grant_retailer_referral();

CREATE OR REPLACE FUNCTION public.validate_referral_code(p_code TEXT) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE v_link referral_links%ROWTYPE; v_ws wholesalers%ROWTYPE;
BEGIN
 SELECT * INTO v_link FROM referral_links WHERE upper(code)=upper(btrim(p_code));
 IF NOT FOUND THEN RETURN jsonb_build_object('valid',false,'reason','not_found'); END IF;
 IF v_link.accepted_by IS NOT NULL OR coalesce(v_link.uses_count,0)>=1 THEN RETURN jsonb_build_object('valid',false,'reason','used'); END IF;
 IF v_link.expires_at IS NULL OR v_link.expires_at<=clock_timestamp() THEN RETURN jsonb_build_object('valid',false,'reason','expired'); END IF;
 SELECT * INTO v_ws FROM wholesalers WHERE id=v_link.wholesaler_id AND verification_status='verified';
 IF NOT FOUND OR NOT v_link.is_active OR v_link.funding_state='released' THEN RETURN jsonb_build_object('valid',false,'reason','inactive'); END IF;
 RETURN jsonb_build_object('valid',true,'code',v_link.code,'business_name',v_ws.business_name,'wholesaler_name',v_ws.business_name,
  'business_logo_url',v_ws.business_logo_url,'wholesaler_logo_url',v_ws.business_logo_url,'expires_at',v_link.expires_at,
  'gift_credits',v_link.gift_credits,'policy_version',v_link.policy_version);
END; $$;

CREATE VIEW public.referral_report WITH (security_invoker=true) AS
 SELECT l.*,w.user_id AS inviter_user_id,w.business_name AS inviter_name,r.id AS retailer_id,r.business_name AS retailer_name,
 r.verification_status AS retailer_status,coalesce(refund.delta,0)::int AS refunded_credits,
 CASE WHEN l.funding_state='settled' THEN 'rewarded' WHEN l.release_reason IS NOT NULL THEN l.release_reason
  WHEN l.policy_version=0 AND r.verification_status='verified' THEN 'legacy'
  WHEN l.accepted_by IS NOT NULL THEN 'pending' WHEN l.expires_at<=clock_timestamp() THEN 'expired'
  WHEN NOT l.is_active THEN 'inactive' ELSE 'unclaimed' END AS status
 FROM referral_links l JOIN wholesalers w ON w.id=l.wholesaler_id
 LEFT JOIN retailers r ON r.user_id=l.accepted_by AND r.referred_by=l.wholesaler_id
 LEFT JOIN credit_ledger refund ON refund.id=l.release_ledger_id;
REVOKE ALL ON public.referral_report FROM anon,authenticated;
GRANT SELECT ON public.referral_report TO service_role;

CREATE FUNCTION public.referral_admin_review(p_retailer UUID,p_status TEXT,p_reason TEXT,p_request UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE v_id UUID;
BEGIN
 IF p_status NOT IN ('verified','rejected') OR p_request IS NULL THEN RAISE EXCEPTION 'INVALID_REVIEW'; END IF;
 PERFORM set_config('jewel.admin_actor','admin-password:'||p_request,true);
 UPDATE retailers SET verification_status=p_status,rejection_reason=CASE WHEN p_status='rejected' THEN p_reason ELSE NULL END,
  notification_message=CASE WHEN p_status='verified' THEN 'You are verified! You can now access your full dashboard.' ELSE 'Verification failed. '||coalesce(p_reason,'Contact support.') END,
  notified=false WHERE id=p_retailer RETURNING id INTO v_id;
 IF v_id IS NULL THEN RETURN jsonb_build_object('ok',false,'error','NOT_FOUND'); END IF;
 INSERT INTO referral_events(invitation_id,event,actor,metadata) SELECT l.id,'reviewed','admin-password:'||p_request,
  jsonb_build_object('status',p_status,'reason',p_reason) FROM referral_links l JOIN retailers r
  ON r.user_id=l.accepted_by AND r.referred_by=l.wholesaler_id WHERE r.id=p_retailer ON CONFLICT DO NOTHING;
 RETURN jsonb_build_object('ok',true);
END; $$;

CREATE FUNCTION public.referral_activate() RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
BEGIN
 PERFORM 1 FROM credit_program WHERE singleton FOR UPDATE;
 IF NOT (SELECT daily_enabled FROM credit_program WHERE singleton) THEN
  RETURN jsonb_build_object('ok',false,'error','DAILY_PROGRAM_REQUIRED'); END IF;
 UPDATE credit_program SET referral_enabled=true WHERE singleton;
 RETURN jsonb_build_object('ok',true);
END; $$;

-- Expiry is also lazy on history reads; 017b schedules cleanup every five minutes.
DO $$ DECLARE f RECORD; BEGIN
 FOR f IN SELECT p.oid::regprocedure AS signature FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE n.nspname='public' AND (p.proname LIKE 'referral_%' OR p.proname IN ('spend_referral_code','claim_retailer_referral','credits_grant_retailer_referral_legacy')) LOOP
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC,anon,authenticated',f.signature);
 END LOOP;
END $$;
GRANT EXECUTE ON FUNCTION public.referral_activate(),public.referral_settings(UUID,BOOLEAN),public.referral_generate(UUID,INT,UUID,TEXT),
 public.referral_cancel(UUID,UUID),public.referral_expire_due(INT),public.referral_admin_review(UUID,TEXT,TEXT,UUID),
 public.referral_settle(UUID,TEXT),public.claim_retailer_referral(TEXT,UUID) TO service_role;
REVOKE ALL ON FUNCTION public.validate_referral_code(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.validate_referral_code(TEXT) TO anon,authenticated,service_role;
COMMIT;
