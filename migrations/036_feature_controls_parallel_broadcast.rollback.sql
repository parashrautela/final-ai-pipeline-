-- Rollback 036: Feature Controls for Parallel Manufacturing Broadcast
-- Restores original manufacturing_broadcast_create definition from migration 021.

CREATE OR REPLACE FUNCTION public.manufacturing_broadcast_create(
    p_asset_id UUID,
    p_category TEXT,
    p_min_weight NUMERIC,
    p_max_weight NUMERIC,
    p_material TEXT,
    p_purity TEXT,
    p_gemstone_preference TEXT,
    p_quantity INTEGER,
    p_making_budget_mode TEXT,
    p_making_budget_amount NUMERIC,
    p_currency TEXT,
    p_metal_rate_snapshot NUMERIC,
    p_metal_rate_basis TEXT,
    p_delivery_needed_date DATE,
    p_notes TEXT,
    p_offer_duration_seconds INTEGER DEFAULT 86400,
    p_superseded_request_id UUID DEFAULT NULL,
    p_idempotency_key TEXT DEFAULT NULL,
    p_request_hash TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'public', 'pg_temp'
AS $$
DECLARE
    v_user_id UUID := auth.uid();
    v_retailer_id UUID := public.my_verified_retailer_id();
    v_asset public.manufacturing_request_assets%ROWTYPE;
    v_request_id UUID;
    v_first_wholesaler UUID;
    v_first_wholesaler_user UUID;
    v_first_offer_id UUID;
    v_expires_at TIMESTAMPTZ;
    v_rank INTEGER := 1;
    v_cand RECORD;
    v_count INTEGER := 0;
    v_event_id UUID;
    v_cached_body JSONB;
    v_result JSONB;
BEGIN
    IF v_user_id IS NULL OR v_retailer_id IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error', 'UNAUTHORIZED', 'message', 'Only verified retailer owners can create manufacturing requests.');
    END IF;

    -- Serialise same-key retries before creating a request or attaching its asset.
    IF p_idempotency_key IS NOT NULL AND btrim(p_idempotency_key) != '' THEN
        PERFORM pg_advisory_xact_lock(hashtextextended(v_user_id::text || ':BROADCAST_CREATE:' || btrim(p_idempotency_key), 0));
    END IF;

    -- Atomic idempotency check
    IF p_idempotency_key IS NOT NULL AND btrim(p_idempotency_key) != '' THEN
        SELECT response_body INTO v_cached_body
          FROM public.manufacturing_idempotency
         WHERE actor_user_id = v_user_id
           AND action = 'BROADCAST_CREATE'
           AND idempotency_key = btrim(p_idempotency_key);

        IF FOUND THEN
            IF EXISTS (SELECT 1 FROM public.manufacturing_idempotency WHERE actor_user_id=v_user_id AND action='BROADCAST_CREATE' AND idempotency_key=btrim(p_idempotency_key) AND request_hash IS DISTINCT FROM p_request_hash) THEN
                RETURN jsonb_build_object('ok',false,'error','IDEMPOTENCY_CONFLICT','message','This retry key belongs to different enquiry details.');
            END IF;
            RETURN v_cached_body;
        END IF;
    END IF;

    -- Validate fields
    IF p_asset_id IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error', 'ASSET_REQUIRED', 'message', 'A reference image asset is required.');
    END IF;
    IF p_category IS NULL OR btrim(p_category) = '' THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVALID_CATEGORY', 'message', 'Jewellery category is required.');
    END IF;
    IF p_min_weight IS NULL OR p_min_weight <= 0 OR p_max_weight IS NULL OR p_max_weight < p_min_weight OR p_max_weight > 5000 THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVALID_WEIGHT', 'message', 'Invalid weight range.');
    END IF;
    IF p_quantity IS NULL OR p_quantity <= 0 OR p_quantity > 1000 THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVALID_QUANTITY', 'message', 'Quantity must be between 1 and 1000.');
    END IF;
    IF (p_making_budget_mode IS NULL) != (p_making_budget_amount IS NULL) OR (p_making_budget_mode IS NOT NULL AND p_making_budget_mode NOT IN ('per_gram', 'fixed_total', 'percentage')) THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVALID_BUDGET_MODE', 'message', 'Budget mode must be per_gram, fixed_total, or percentage.');
    END IF;
    IF p_making_budget_amount IS NOT NULL AND p_making_budget_amount <= 0 THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVALID_BUDGET_AMOUNT', 'message', 'Budget amount must be positive.');
    END IF;
    IF p_delivery_needed_date IS NULL OR p_delivery_needed_date <= CURRENT_DATE THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVALID_DELIVERY_DATE', 'message', 'Delivery needed date must be in the future.');
    END IF;

    IF p_offer_duration_seconds NOT BETWEEN 3600 AND 172800 THEN
        RETURN jsonb_build_object('ok',false,'error','INVALID_DEADLINE','message','Quotation window must be between 1 and 48 hours.');
    END IF;
    v_expires_at := LEAST(clock_timestamp() + make_interval(secs => p_offer_duration_seconds), p_delivery_needed_date::timestamptz);
    -- Validate asset ownership and lock asset row
    SELECT * INTO v_asset
      FROM public.manufacturing_request_assets
     WHERE id = p_asset_id
       FOR UPDATE;

    IF NOT FOUND OR v_asset.owner_retailer_id != v_retailer_id THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVALID_ASSET', 'message', 'Asset not found or not owned by caller.');
    END IF;

    IF v_asset.status != 'uploaded' OR v_asset.request_id IS NOT NULL THEN
        RETURN jsonb_build_object('ok',false,'error','ASSET_ATTACHED','message','This reference image already belongs to another enquiry.');
    END IF;
    IF p_superseded_request_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM public.manufacturing_requests WHERE id=p_superseded_request_id AND retailer_id=v_retailer_id) THEN
        RETURN jsonb_build_object('ok',false,'error','FORBIDDEN','message','Original enquiry is not yours.');
    END IF;
    -- Create request record
    INSERT INTO public.manufacturing_requests (
        retailer_id, created_by_user_id, category, min_weight_grams, max_weight_grams,
        material, purity, gemstone_preference, quantity, making_budget_mode,
        making_budget_amount, currency, metal_rate_snapshot, metal_rate_basis,
        delivery_needed_date, notes, state, superseded_request_id, broadcast_mode, quotation_deadline
    ) VALUES (
        v_retailer_id, v_user_id, btrim(p_category), p_min_weight, p_max_weight,
        btrim(p_material), btrim(p_purity), COALESCE(btrim(p_gemstone_preference), 'none'),
        p_quantity, p_making_budget_mode, p_making_budget_amount, COALESCE(p_currency, 'INR'),
        p_metal_rate_snapshot, p_metal_rate_basis, p_delivery_needed_date,
        btrim(p_notes), 'collecting', p_superseded_request_id, 'parallel', v_expires_at
    ) RETURNING id INTO v_request_id;

    -- Associate asset
    UPDATE public.manufacturing_request_assets
       SET request_id = v_request_id, status = 'attached'
     WHERE id = p_asset_id;

    INSERT INTO public.manufacturing_events(request_id,actor_user_id,actor_role,event_type,to_state)
    VALUES(v_request_id,v_user_id,'retailer','BROADCAST_CREATED','collecting') RETURNING id INTO v_event_id;
    FOR v_cand IN SELECT w.id AS wholesaler_id,w.user_id FROM public.wholesalers w WHERE w.verification_status='verified' AND w.user_id IS NOT NULL ORDER BY w.id LOOP
        v_count := v_count+1;
        INSERT INTO public.manufacturing_request_candidates(request_id,wholesaler_id,rank,status)
        VALUES(v_request_id,v_cand.wholesaler_id,v_count,'offered');
        INSERT INTO public.manufacturing_offers(request_id,wholesaler_id,rank,status,expires_at)
        VALUES(v_request_id,v_cand.wholesaler_id,v_count,'open',v_expires_at) RETURNING id INTO v_first_offer_id;
        INSERT INTO public.manufacturing_notification_outbox(event_id,recipient_user_id,kind,payload,dedup_key)
        VALUES(v_event_id,v_cand.user_id,'NEW_MANUFACTURING_OFFER',jsonb_build_object('request_id',v_request_id,'offer_id',v_first_offer_id,'expires_at',v_expires_at,'broadcast_mode','parallel'),'mfg_offer_'||v_first_offer_id::text);
    END LOOP;
    IF v_count=0 THEN
        UPDATE public.manufacturing_requests SET state='exhausted' WHERE id=v_request_id;
    END IF;
    v_result := jsonb_build_object('ok',true,'request_id',v_request_id,'state',CASE WHEN v_count=0 THEN 'exhausted' ELSE 'collecting' END,'quotation_deadline',v_expires_at,'candidates_count',v_count,'broadcast_mode','parallel');

    IF p_idempotency_key IS NOT NULL AND btrim(p_idempotency_key) != '' THEN
        INSERT INTO public.manufacturing_idempotency (
            actor_user_id, action, idempotency_key, request_hash, response_code, response_body
        ) VALUES (
            v_user_id, 'BROADCAST_CREATE', btrim(p_idempotency_key), p_request_hash, 200, v_result
        ) ON CONFLICT (actor_user_id, action, idempotency_key) DO NOTHING;
    END IF;

    RETURN v_result;
END;
$$;

REVOKE ALL ON FUNCTION public.manufacturing_broadcast_create FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.manufacturing_broadcast_create TO authenticated;
