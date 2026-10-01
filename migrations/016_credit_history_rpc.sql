-- Independent activity-history repair. Requires the existing credit ledger and
-- retailer/staff schema only. Can run BEFORE migration 015.
-- Does not change balances, credit lots, payments, subscriptions or refunds.
BEGIN;

CREATE OR REPLACE FUNCTION public.credits_history(p_limit INT DEFAULT 50,p_offset INT DEFAULT 0,p_kind TEXT DEFAULT NULL,
  p_before TIMESTAMPTZ DEFAULT NULL,p_before_id UUID DEFAULT NULL)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_owner UUID; v_count INT; v_rows JSONB;
BEGIN
  -- Resolve only the authenticated business; callers cannot select another wallet.
  SELECT owner INTO v_owner FROM (
    SELECT user_id AS owner, 1 AS priority FROM public.wholesalers
      WHERE user_id = auth.uid() AND verification_status = 'verified'
    UNION ALL
    SELECT user_id, 2 FROM public.retailers
      WHERE user_id = auth.uid() AND verification_status = 'verified'
    UNION ALL
    SELECT r.user_id, 3 FROM public.employees e JOIN public.retailers r ON r.id = e.retailer_id
      WHERE e.auth_user_id = auth.uid() AND e.status = 'active' AND r.verification_status = 'verified'
  ) candidates ORDER BY priority LIMIT 1;
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
REVOKE ALL ON FUNCTION public.credits_history(INT,INT,TEXT,TIMESTAMPTZ,UUID) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.credits_history(INT,INT,TEXT,TIMESTAMPTZ,UUID) TO authenticated, service_role;
NOTIFY pgrst, 'reload schema';
COMMIT;
