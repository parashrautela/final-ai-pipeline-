-- Migration 031: Enforcement for Custom Inquiries
-- Updates request_visible_to_current_wholesaler and candidate routing to respect feature_blocked.

CREATE OR REPLACE FUNCTION public.request_visible_to_current_wholesaler(p_request_id UUID)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT (
        EXISTS (
            SELECT 1
            FROM public.manufacturing_offers mo
            WHERE mo.request_id = p_request_id
              AND mo.wholesaler_id = public.my_verified_wholesaler_id()
        ) OR EXISTS (
            SELECT 1
            FROM public.manufacturing_requests mr
            WHERE mr.id = p_request_id
              AND mr.assigned_wholesaler_id = public.my_verified_wholesaler_id()
        )
    ) AND NOT public.feature_blocked(auth.uid(), 'custom_inquiries');
$$;

REVOKE ALL ON FUNCTION public.request_visible_to_current_wholesaler(UUID) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.request_visible_to_current_wholesaler(UUID) TO authenticated, service_role;

CREATE OR REPLACE FUNCTION public.manufacturing_request_create(
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
    p_offer_duration_seconds INTEGER DEFAULT 1800,
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
        PERFORM pg_advisory_xact_lock(hashtextextended(v_user_id::text || ':REQUEST_CREATE:' || btrim(p_idempotency_key), 0));
    END IF;

    -- Atomic idempotency check
    IF p_idempotency_key IS NOT NULL AND btrim(p_idempotency_key) != '' THEN
        SELECT response_body INTO v_cached_body
          FROM public.manufacturing_idempotency
         WHERE actor_user_id = v_user_id
           AND action = 'REQUEST_CREATE'
           AND idempotency_key = btrim(p_idempotency_key);

        IF FOUND THEN
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
    IF p_making_budget_mode NOT IN ('per_gram', 'fixed_total', 'percentage') THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVALID_BUDGET_MODE', 'message', 'Budget mode must be per_gram, fixed_total, or percentage.');
    END IF;
    IF p_making_budget_amount IS NULL OR p_making_budget_amount <= 0 THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVALID_BUDGET_AMOUNT', 'message', 'Budget amount must be positive.');
    END IF;
    IF p_delivery_needed_date IS NULL OR p_delivery_needed_date <= CURRENT_DATE THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVALID_DELIVERY_DATE', 'message', 'Delivery needed date must be in the future.');
    END IF;

    -- Validate asset ownership and lock asset row
    SELECT * INTO v_asset
      FROM public.manufacturing_request_assets
     WHERE id = p_asset_id
       FOR UPDATE;

    IF NOT FOUND OR v_asset.owner_retailer_id != v_retailer_id THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVALID_ASSET', 'message', 'Asset not found or not owned by caller.');
    END IF;

    -- Create request record
    INSERT INTO public.manufacturing_requests (
        retailer_id, created_by_user_id, category, min_weight_grams, max_weight_grams,
        material, purity, gemstone_preference, quantity, making_budget_mode,
        making_budget_amount, currency, metal_rate_snapshot, metal_rate_basis,
        delivery_needed_date, notes, state, superseded_request_id
    ) VALUES (
        v_retailer_id, v_user_id, btrim(p_category), p_min_weight, p_max_weight,
        btrim(p_material), btrim(p_purity), COALESCE(btrim(p_gemstone_preference), 'none'),
        p_quantity, p_making_budget_mode, p_making_budget_amount, COALESCE(p_currency, 'INR'),
        p_metal_rate_snapshot, p_metal_rate_basis, p_delivery_needed_date,
        btrim(p_notes), 'routing', p_superseded_request_id
    ) RETURNING id INTO v_request_id;

    -- Associate asset
    UPDATE public.manufacturing_request_assets
       SET request_id = v_request_id, status = 'attached'
     WHERE id = p_asset_id;

    -- Candidate selection: deterministic, fair ordering of verified wholesalers
    FOR v_cand IN
        SELECT w.id AS wholesaler_id, w.user_id
          FROM public.wholesalers w
         WHERE w.verification_status = 'verified'
           AND NOT public.feature_blocked(w.user_id, 'custom_inquiries')
         ORDER BY (
             SELECT COALESCE(MAX(mo.offered_at), '1970-01-01'::timestamptz)
               FROM public.manufacturing_offers mo
              WHERE mo.wholesaler_id = w.id
         ) ASC,
         w.id ASC
    LOOP
        v_count := v_count + 1;
        INSERT INTO public.manufacturing_request_candidates (
            request_id, wholesaler_id, rank, status
        ) VALUES (
            v_request_id, v_cand.wholesaler_id, v_count,
            CASE WHEN v_count = 1 THEN 'offered' ELSE 'queued' END
        );

        IF v_count = 1 THEN
            v_first_wholesaler := v_cand.wholesaler_id;
            v_first_wholesaler_user := v_cand.user_id;
        END IF;
    END LOOP;

    -- If no verified wholesaler exists, mark exhausted immediately
    IF v_count = 0 THEN
        UPDATE public.manufacturing_requests
           SET state = 'exhausted', updated_at = clock_timestamp()
         WHERE id = v_request_id;

        INSERT INTO public.manufacturing_events (
            request_id, actor_user_id, actor_role, event_type, from_state, to_state, metadata
        ) VALUES (
            v_request_id, v_user_id, 'retailer', 'REQUEST_EXHAUSTED_NO_SUPPLIERS', 'routing', 'exhausted',
            jsonb_build_object('reason', 'No eligible verified wholesalers available')
        );

        v_result := jsonb_build_object('ok', true, 'request_id', v_request_id, 'state', 'exhausted');
        IF p_idempotency_key IS NOT NULL AND btrim(p_idempotency_key) != '' THEN
            INSERT INTO public.manufacturing_idempotency (
                actor_user_id, action, idempotency_key, request_hash, response_code, response_body
            ) VALUES (
                v_user_id, 'REQUEST_CREATE', btrim(p_idempotency_key), p_request_hash, 200, v_result
            ) ON CONFLICT (actor_user_id, action, idempotency_key) DO NOTHING;
        END IF;

        RETURN v_result;
    END IF;

    -- Activate Rank 1 offer
    v_expires_at := clock_timestamp() + (COALESCE(p_offer_duration_seconds, 1800) || ' seconds')::interval;

    INSERT INTO public.manufacturing_offers (
        request_id, wholesaler_id, rank, status, offered_at, expires_at
    ) VALUES (
        v_request_id, v_first_wholesaler, 1, 'active', clock_timestamp(), v_expires_at
    ) RETURNING id INTO v_first_offer_id;

    UPDATE public.manufacturing_requests
       SET active_offer_id = v_first_offer_id, updated_at = clock_timestamp()
     WHERE id = v_request_id;

    -- Record event and enqueue outbox notification
    INSERT INTO public.manufacturing_events (
        request_id, offer_id, actor_user_id, actor_role, event_type, from_state, to_state, metadata
    ) VALUES (
        v_request_id, v_first_offer_id, v_user_id, 'retailer', 'REQUEST_CREATED', NULL, 'routing',
        jsonb_build_object('category', p_category, 'candidates_count', v_count, 'offered_wholesaler_id', v_first_wholesaler)
    ) RETURNING id INTO v_event_id;

    IF v_first_wholesaler_user IS NOT NULL THEN
        INSERT INTO public.manufacturing_notification_outbox (
            event_id, recipient_user_id, kind, payload, dedup_key
        ) VALUES (
            v_event_id, v_first_wholesaler_user, 'NEW_MANUFACTURING_OFFER',
            jsonb_build_object('request_id', v_request_id, 'offer_id', v_first_offer_id, 'expires_at', v_expires_at),
            'mfg_offer_' || v_first_offer_id::text
        );
    END IF;

    v_result := jsonb_build_object(
        'ok', true,
        'request_id', v_request_id,
        'state', 'routing',
        'active_offer_id', v_first_offer_id,
        'expires_at', v_expires_at,
        'candidates_count', v_count
    );

    IF p_idempotency_key IS NOT NULL AND btrim(p_idempotency_key) != '' THEN
        INSERT INTO public.manufacturing_idempotency (
            actor_user_id, action, idempotency_key, request_hash, response_code, response_body
        ) VALUES (
            v_user_id, 'REQUEST_CREATE', btrim(p_idempotency_key), p_request_hash, 200, v_result
        ) ON CONFLICT (actor_user_id, action, idempotency_key) DO NOTHING;
    END IF;

    RETURN v_result;
END;
$$;

REVOKE ALL ON FUNCTION public.manufacturing_request_create FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.manufacturing_request_create TO authenticated;



CREATE OR REPLACE FUNCTION public.manufacturing_offer_expire_and_advance(
    p_request_id UUID,
    p_offer_duration_seconds INTEGER DEFAULT 1800
)
RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'public', 'pg_temp'
AS $$
DECLARE
    v_request public.manufacturing_requests%ROWTYPE;
    v_offer public.manufacturing_offers%ROWTYPE;
    v_next_candidate RECORD;
    v_new_offer_id UUID;
    v_expires_at TIMESTAMPTZ;
    v_event_id UUID;
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    -- Row lock request
    SELECT * INTO v_request
      FROM public.manufacturing_requests
     WHERE id = p_request_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'error', 'REQUEST_NOT_FOUND');
    END IF;

    IF v_request.state != 'routing' OR v_request.active_offer_id IS NULL THEN
        RETURN jsonb_build_object('ok', true, 'advanced', false, 'message', 'Request is not routing or has no active offer.');
    END IF;

    -- Row lock offer
    SELECT * INTO v_offer
      FROM public.manufacturing_offers
     WHERE id = v_request.active_offer_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'error', 'ACTIVE_OFFER_NOT_FOUND');
    END IF;

    v_now := clock_timestamp();

    -- Check if strictly expired
    IF v_now < v_offer.expires_at THEN
        RETURN jsonb_build_object('ok', true, 'advanced', false, 'time_remaining_seconds', EXTRACT(EPOCH FROM (v_offer.expires_at - v_now))::integer);
    END IF;

    -- Mark expired
    UPDATE public.manufacturing_offers
       SET status = 'expired', responded_at = v_now, version = version + 1, updated_at = v_now
     WHERE id = v_offer.id;

    INSERT INTO public.manufacturing_events (
        request_id, offer_id, actor_role, event_type, from_state, to_state, metadata
    ) VALUES (
        v_request.id, v_offer.id, 'system', 'OFFER_EXPIRED', 'active', 'expired',
        jsonb_build_object('wholesaler_id', v_offer.wholesaler_id, 'expires_at', v_offer.expires_at)
    );

    -- Find next queued candidate who is still verified
    SELECT c.wholesaler_id, c.rank, w.user_id
      INTO v_next_candidate
      FROM public.manufacturing_request_candidates c
      JOIN public.wholesalers w ON w.id = c.wholesaler_id
     WHERE c.request_id = v_request.id
       AND c.rank > v_offer.rank
       AND c.status = 'queued'
       AND w.verification_status = 'verified'
     ORDER BY c.rank ASC
     LIMIT 1;

    IF v_next_candidate.wholesaler_id IS NOT NULL THEN
        UPDATE public.manufacturing_request_candidates
           SET status = 'offered', updated_at = v_now
         WHERE request_id = v_request.id AND wholesaler_id = v_next_candidate.wholesaler_id;

        v_expires_at := v_now + (COALESCE(p_offer_duration_seconds, 1800) || ' seconds')::interval;

        INSERT INTO public.manufacturing_offers (
            request_id, wholesaler_id, rank, status, offered_at, expires_at
        ) VALUES (
            v_request.id, v_next_candidate.wholesaler_id, v_next_candidate.rank, 'active', v_now, v_expires_at
        ) RETURNING id INTO v_new_offer_id;

        UPDATE public.manufacturing_requests
           SET active_offer_id = v_new_offer_id, version = version + 1, updated_at = v_now
         WHERE id = v_request.id;

        INSERT INTO public.manufacturing_events (
            request_id, offer_id, actor_role, event_type, from_state, to_state, metadata
        ) VALUES (
            v_request.id, v_new_offer_id, 'system', 'OFFER_ADVANCED_AFTER_EXPIRY', 'active', 'active',
            jsonb_build_object('previous_offer_id', v_offer.id, 'new_offer_id', v_new_offer_id, 'new_wholesaler_id', v_next_candidate.wholesaler_id)
        ) RETURNING id INTO v_event_id;

        IF v_next_candidate.user_id IS NOT NULL THEN
            INSERT INTO public.manufacturing_notification_outbox (
                event_id, recipient_user_id, kind, payload, dedup_key
            ) VALUES (
                v_event_id, v_next_candidate.user_id, 'NEW_MANUFACTURING_OFFER',
                jsonb_build_object('request_id', v_request.id, 'offer_id', v_new_offer_id, 'expires_at', v_expires_at),
                'mfg_offer_' || v_new_offer_id::text
            );
        END IF;

        RETURN jsonb_build_object('ok', true, 'advanced', true, 'new_offer_id', v_new_offer_id);
    ELSE
        -- No further candidates -> Request exhausted
        UPDATE public.manufacturing_requests
           SET state = 'exhausted', active_offer_id = NULL, version = version + 1, updated_at = v_now
         WHERE id = v_request.id;

        INSERT INTO public.manufacturing_events (
            request_id, actor_role, event_type, from_state, to_state, metadata
        ) VALUES (
            v_request.id, 'system', 'REQUEST_EXHAUSTED', 'routing', 'exhausted',
            jsonb_build_object('last_offer_id', v_offer.id, 'reason', 'All eligible candidates declined or expired')
        ) RETURNING id INTO v_event_id;

        INSERT INTO public.manufacturing_notification_outbox (
            event_id, recipient_user_id, kind, payload, dedup_key
        ) VALUES (
            v_event_id, v_request.created_by_user_id, 'MANUFACTURING_REQUEST_EXHAUSTED',
            jsonb_build_object('request_id', v_request.id),
            'mfg_exhausted_' || v_request.id::text
        );

        RETURN jsonb_build_object('ok', true, 'advanced', true, 'state', 'exhausted');
    END IF;
END;
$$;

REVOKE ALL ON FUNCTION public.manufacturing_offer_expire_and_advance FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.manufacturing_offer_expire_and_advance FROM authenticated;
GRANT EXECUTE ON FUNCTION public.manufacturing_offer_expire_and_advance TO service_role;


