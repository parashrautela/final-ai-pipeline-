-- Run after the customer-wishlist migration (20260919_02) in wholesaler ios.
-- Public access uses server-held capabilities, never public customer-table RLS.
BEGIN;

CREATE TABLE public.wishlist_shares (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  retailer_id UUID NOT NULL REFERENCES public.retailers(id) ON DELETE CASCADE,
  board_id UUID NOT NULL REFERENCES public.customer_boards(id) ON DELETE CASCADE,
  created_by UUID REFERENCES auth.users(id) ON DELETE SET NULL,
  token_hash TEXT NOT NULL UNIQUE CHECK (token_hash ~ '^[a-f0-9]{64}$'),
  public_title TEXT NOT NULL DEFAULT 'A wishlist for you' CHECK (length(public_title) BETWEEN 1 AND 80),
  max_viewers INT NOT NULL CHECK (max_viewers BETWEEN 1 AND 100),
  views_used INT NOT NULL DEFAULT 0 CHECK (views_used >= 0 AND views_used <= max_viewers),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  expires_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ
);
CREATE INDEX ON public.wishlist_shares(retailer_id, created_at DESC);
CREATE TABLE public.wishlist_share_items (
  share_id UUID NOT NULL REFERENCES public.wishlist_shares(id) ON DELETE CASCADE,
  product_id UUID NOT NULL REFERENCES public.products(id) ON DELETE CASCADE,
  position INT NOT NULL,
  PRIMARY KEY(share_id, product_id)
);
CREATE TABLE public.wishlist_share_sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  share_id UUID NOT NULL REFERENCES public.wishlist_shares(id) ON DELETE CASCADE,
  browser_hash TEXT NOT NULL CHECK (browser_hash ~ '^[a-f0-9]{64}$'),
  session_hash TEXT NOT NULL UNIQUE CHECK (session_hash ~ '^[a-f0-9]{64}$'),
  admitted_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  UNIQUE(share_id, browser_hash)
);
CREATE TABLE public.wishlist_share_events (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  share_id UUID NOT NULL REFERENCES public.wishlist_shares(id) ON DELETE CASCADE,
  actor_id UUID REFERENCES auth.users(id) ON DELETE SET NULL,
  event TEXT NOT NULL CHECK (event IN ('created','admitted','revoked')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE public.wishlist_share_rate_limits (
  bucket TEXT NOT NULL,
  window_start TIMESTAMPTZ NOT NULL,
  attempts INT NOT NULL DEFAULT 1,
  PRIMARY KEY(bucket, window_start)
);

ALTER TABLE public.wishlist_shares ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.wishlist_share_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.wishlist_share_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.wishlist_share_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.wishlist_share_rate_limits ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.wishlist_shares, public.wishlist_share_items,
  public.wishlist_share_sessions, public.wishlist_share_events,
  public.wishlist_share_rate_limits FROM anon, authenticated;
GRANT ALL ON public.wishlist_shares, public.wishlist_share_items,
  public.wishlist_share_sessions, public.wishlist_share_events,
  public.wishlist_share_rate_limits TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.wishlist_share_events_id_seq TO service_role;

-- The trusted API authenticates p_actor. Even staff must belong to a verified store.
CREATE FUNCTION public.wishlist_actor_store(p_actor UUID) RETURNS UUID
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT r.id FROM public.retailers r
  WHERE r.verification_status = 'verified'
    AND (r.user_id = p_actor OR EXISTS (
      SELECT 1 FROM public.employees e WHERE e.retailer_id = r.id
      AND e.auth_user_id = p_actor AND e.status = 'active'))
  ORDER BY (r.user_id = p_actor) DESC LIMIT 1;
$$;

CREATE FUNCTION public.wishlist_share_rate_limit(p_bucket TEXT, p_limit INT DEFAULT 60)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_attempts INT; v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
  INSERT INTO public.wishlist_share_rate_limits(bucket, window_start)
    VALUES (p_bucket, date_trunc('minute', v_now))
  ON CONFLICT(bucket, window_start) DO UPDATE
    SET attempts = wishlist_share_rate_limits.attempts + 1
  RETURNING attempts INTO v_attempts;
  DELETE FROM public.wishlist_share_rate_limits WHERE window_start < v_now - interval '10 minutes';
  RETURN v_attempts <= LEAST(GREATEST(p_limit, 1), 120);
END;
$$;

CREATE FUNCTION public.wishlist_share_create(
  p_actor UUID, p_board UUID, p_token_hash TEXT, p_max_viewers INT, p_duration_minutes INT
) RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_store UUID; v_share public.wishlist_shares%ROWTYPE; v_now TIMESTAMPTZ;
BEGIN
  v_store := public.wishlist_actor_store(p_actor);
  IF v_store IS NULL THEN RETURN jsonb_build_object('ok', false, 'error', 'NOT_VERIFIED'); END IF;
  IF p_token_hash IS NULL OR p_token_hash !~ '^[a-f0-9]{64}$'
    OR p_max_viewers IS NULL OR p_max_viewers NOT BETWEEN 1 AND 100
    OR p_duration_minutes IS NULL OR p_duration_minutes NOT BETWEEN 5 AND 10080 THEN
    RETURN jsonb_build_object('ok', false, 'error', 'INVALID_SETTINGS');
  END IF;
  PERFORM 1 FROM public.retailers WHERE id = v_store FOR UPDATE;
  v_now := clock_timestamp();
  -- Recheck membership after waiting on the store lock.
  IF public.wishlist_actor_store(p_actor) IS DISTINCT FROM v_store THEN
    RETURN jsonb_build_object('ok', false, 'error', 'NOT_VERIFIED');
  END IF;
  PERFORM 1 FROM public.customer_boards b JOIN public.retailer_customers c ON c.id = b.customer_id
    WHERE b.id = p_board AND b.retailer_id = v_store AND c.retailer_id = v_store FOR UPDATE OF b;
  IF NOT FOUND THEN RETURN jsonb_build_object('ok', false, 'error', 'NOT_FOUND'); END IF;
  IF (SELECT count(*) FROM public.wishlist_shares WHERE retailer_id = v_store
      AND revoked_at IS NULL AND expires_at > v_now AND views_used < max_viewers) >= 100 THEN
    RETURN jsonb_build_object('ok', false, 'error', 'TOO_MANY_LINKS');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.customer_board_items i JOIN public.products p ON p.id = i.product_id
    WHERE i.board_id = p_board AND i.retailer_id = v_store AND p.is_published) THEN
    RETURN jsonb_build_object('ok', false, 'error', 'EMPTY_WISHLIST');
  END IF;
  INSERT INTO public.wishlist_shares(retailer_id, board_id, created_by, token_hash, max_viewers, expires_at)
    VALUES(v_store, p_board, p_actor, p_token_hash, p_max_viewers,
      v_now + make_interval(mins => p_duration_minutes)) RETURNING * INTO v_share;
  INSERT INTO public.wishlist_share_items(share_id, product_id, position)
    SELECT v_share.id, i.product_id, row_number() OVER (ORDER BY i.created_at DESC, i.product_id)::int
    FROM public.customer_board_items i JOIN public.products p ON p.id = i.product_id
    WHERE i.board_id = p_board AND i.retailer_id = v_store AND p.is_published;
  IF NOT FOUND THEN
    DELETE FROM public.wishlist_shares WHERE id = v_share.id;
    RETURN jsonb_build_object('ok',false,'error','EMPTY_WISHLIST');
  END IF;
  INSERT INTO public.wishlist_share_events(share_id, actor_id, event) VALUES(v_share.id, p_actor, 'created');
  RETURN jsonb_build_object('ok', true, 'id', v_share.id, 'expires_at', v_share.expires_at,
    'max_viewers', v_share.max_viewers, 'views_used', 0);
END;
$$;

CREATE FUNCTION public.wishlist_share_claim(p_token_hash TEXT, p_browser_hash TEXT, p_session_hash TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_share public.wishlist_shares%ROWTYPE; v_session public.wishlist_share_sessions%ROWTYPE; v_now TIMESTAMPTZ;
BEGIN
  IF p_token_hash IS NULL OR p_token_hash !~ '^[a-f0-9]{64}$'
    OR p_browser_hash IS NULL OR p_browser_hash !~ '^[a-f0-9]{64}$'
    OR p_session_hash IS NULL OR p_session_hash !~ '^[a-f0-9]{64}$' THEN
    RETURN jsonb_build_object('ok', false, 'error', 'UNAVAILABLE');
  END IF;
  SELECT * INTO v_share FROM public.wishlist_shares WHERE token_hash = p_token_hash FOR UPDATE;
  v_now := clock_timestamp();
  IF NOT FOUND OR v_share.revoked_at IS NOT NULL THEN
    RETURN jsonb_build_object('ok', false, 'error', 'UNAVAILABLE');
  END IF;
  IF v_share.expires_at <= v_now THEN RETURN jsonb_build_object('ok', false, 'error', 'EXPIRED'); END IF;
  IF NOT EXISTS (SELECT 1 FROM public.retailers WHERE id = v_share.retailer_id AND verification_status = 'verified') THEN
    RETURN jsonb_build_object('ok', false, 'error', 'UNAVAILABLE');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.wishlist_share_items i JOIN public.products p ON p.id = i.product_id
    WHERE i.share_id = v_share.id AND p.is_published) THEN
    RETURN jsonb_build_object('ok', false, 'error', 'UNAVAILABLE');
  END IF;
  SELECT * INTO v_session FROM public.wishlist_share_sessions
    WHERE share_id = v_share.id AND browser_hash = p_browser_hash;
  IF FOUND THEN
    IF v_session.expires_at <= v_now THEN
      RETURN jsonb_build_object('ok', false, 'error', 'SESSION_ENDED');
    END IF;
    -- Response-loss retry: rotate the cookie but retain this admission/deadline.
    UPDATE public.wishlist_share_sessions SET session_hash = p_session_hash WHERE id = v_session.id;
  ELSE
    IF v_share.views_used >= v_share.max_viewers THEN RETURN jsonb_build_object('ok', false, 'error', 'FULL'); END IF;
    UPDATE public.wishlist_shares SET views_used = views_used + 1 WHERE id = v_share.id;
    INSERT INTO public.wishlist_share_sessions(share_id, browser_hash, session_hash, admitted_at, expires_at)
      VALUES(v_share.id, p_browser_hash, p_session_hash, v_now,
        LEAST(v_share.expires_at, v_now + interval '30 minutes')) RETURNING * INTO v_session;
    INSERT INTO public.wishlist_share_events(share_id, event) VALUES(v_share.id, 'admitted');
  END IF;
  RETURN jsonb_build_object('ok', true, 'share_id', v_share.id, 'expires_at', v_session.expires_at,
    'server_now', v_now);
END;
$$;

CREATE FUNCTION public.wishlist_share_content(p_session_hash TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_session public.wishlist_share_sessions%ROWTYPE; v_share public.wishlist_shares%ROWTYPE;
  v_now TIMESTAMPTZ := clock_timestamp(); v_products JSONB; v_store TEXT;
BEGIN
  SELECT * INTO v_session FROM public.wishlist_share_sessions WHERE session_hash = p_session_hash;
  IF NOT FOUND THEN RETURN jsonb_build_object('ok', false, 'error', 'UNAVAILABLE'); END IF;
  SELECT * INTO v_share FROM public.wishlist_shares WHERE id = v_session.share_id;
  IF NOT FOUND OR v_share.revoked_at IS NOT NULL THEN RETURN jsonb_build_object('ok', false, 'error', 'UNAVAILABLE'); END IF;
  IF v_share.expires_at <= v_now THEN RETURN jsonb_build_object('ok', false, 'error', 'EXPIRED'); END IF;
  IF v_session.expires_at <= v_now THEN RETURN jsonb_build_object('ok', false, 'error', 'SESSION_ENDED'); END IF;
  SELECT business_name INTO v_store FROM public.retailers
    WHERE id = v_share.retailer_id AND verification_status = 'verified';
  IF NOT FOUND THEN RETURN jsonb_build_object('ok', false, 'error', 'UNAVAILABLE'); END IF;
  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'id', p.id, 'title', coalesce(nullif(p.title, ''), p.jewellery_type, 'Jewellery design'),
    'jewellery_type', p.jewellery_type, 'net_weight', p.net_weight,
    'image_url', coalesce(to_jsonb(p)->'showcase_image_urls'->>0,
      to_jsonb(p)->'generated_image_urls'->>0, p.processed_image_url, to_jsonb(p)->>'image_url')
  ) ORDER BY i.position), '[]'::jsonb) INTO v_products
    FROM public.wishlist_share_items i JOIN public.products p ON p.id = i.product_id
    WHERE i.share_id = v_share.id AND p.is_published;
  IF jsonb_array_length(v_products) = 0 THEN RETURN jsonb_build_object('ok', false, 'error', 'UNAVAILABLE'); END IF;
  RETURN jsonb_build_object('ok', true, 'title', v_share.public_title, 'store_name', v_store,
    'products', v_products, 'expires_at', LEAST(v_share.expires_at, v_session.expires_at), 'server_now', v_now);
END;
$$;

CREATE FUNCTION public.wishlist_share_revoke(p_actor UUID, p_share UUID)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_store UUID := public.wishlist_actor_store(p_actor); v_share public.wishlist_shares%ROWTYPE;
BEGIN
  SELECT * INTO v_share FROM public.wishlist_shares
    WHERE id = p_share AND retailer_id = v_store FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('ok', false, 'error', 'NOT_FOUND'); END IF;
  IF public.wishlist_actor_store(p_actor) IS DISTINCT FROM v_store THEN
    RETURN jsonb_build_object('ok', false, 'error', 'NOT_FOUND');
  END IF;
  IF v_share.revoked_at IS NULL THEN
    UPDATE public.wishlist_shares SET revoked_at = clock_timestamp() WHERE id = p_share;
    INSERT INTO public.wishlist_share_events(share_id, actor_id, event) VALUES(p_share, p_actor, 'revoked');
  END IF;
  RETURN jsonb_build_object('ok', true);
END;
$$;

-- The admin Edge Function checks its server-side admin secret before this RPC.
CREATE FUNCTION public.wishlist_share_admin_revoke(p_share UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  PERFORM 1 FROM public.wishlist_shares WHERE id = p_share FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'error','NOT_FOUND'); END IF;
  UPDATE public.wishlist_shares SET revoked_at = clock_timestamp() WHERE id = p_share AND revoked_at IS NULL;
  IF FOUND THEN INSERT INTO public.wishlist_share_events(share_id,event) VALUES(p_share,'revoked'); END IF;
  RETURN jsonb_build_object('ok',true);
END;
$$;
REVOKE ALL ON FUNCTION public.wishlist_share_admin_revoke(UUID) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.wishlist_share_admin_revoke(UUID) TO service_role;

REVOKE ALL ON FUNCTION public.wishlist_actor_store(UUID),
  public.wishlist_share_rate_limit(TEXT, INT), public.wishlist_share_create(UUID, UUID, TEXT, INT, INT),
  public.wishlist_share_claim(TEXT, TEXT, TEXT), public.wishlist_share_content(TEXT),
  public.wishlist_share_revoke(UUID, UUID) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.wishlist_actor_store(UUID),
  public.wishlist_share_rate_limit(TEXT, INT), public.wishlist_share_create(UUID, UUID, TEXT, INT, INT),
  public.wishlist_share_claim(TEXT, TEXT, TEXT), public.wishlist_share_content(TEXT),
  public.wishlist_share_revoke(UUID, UUID) TO service_role;
COMMIT;
