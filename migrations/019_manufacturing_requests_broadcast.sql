-- ============================================================================
-- Jewel India: Native Jewellery Request Broadcast and Wholesaler Queue
-- Migration: 20261006_01_manufacturing_requests_broadcast.sql
-- ============================================================================

BEGIN;

-- ── 1. IDENTITY HELPERS ──────────────────────────────────────────────────────

-- The caller's verified retailer ID, strictly checking verification_status and
-- rejecting employees (including a retailer owner in employee mode).
CREATE OR REPLACE FUNCTION public.my_verified_retailer_id()
RETURNS UUID
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path TO 'public', 'pg_temp'
AS $$
    SELECT r.id
      FROM public.retailers r
     WHERE r.user_id = auth.uid()
       AND r.verification_status = 'verified'
       AND NOT EXISTS (
           SELECT 1 FROM public.employees e
            WHERE e.auth_user_id = auth.uid()
              AND e.status = 'active'
       )
     LIMIT 1;
$$;

REVOKE ALL ON FUNCTION public.my_verified_retailer_id() FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.my_verified_retailer_id() TO authenticated;

-- The caller's verified wholesaler ID, strictly checking verification_status.
CREATE OR REPLACE FUNCTION public.my_verified_wholesaler_id()
RETURNS UUID
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path TO 'public', 'pg_temp'
AS $$
    SELECT w.id
      FROM public.wholesalers w
     WHERE w.user_id = auth.uid()
       AND w.verification_status = 'verified'
     LIMIT 1;
$$;

REVOKE ALL ON FUNCTION public.my_verified_wholesaler_id() FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.my_verified_wholesaler_id() TO authenticated;

-- ── 2. TABLES ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.manufacturing_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    retailer_id UUID NOT NULL REFERENCES public.retailers(id),
    created_by_user_id UUID NOT NULL REFERENCES auth.users(id),
    category TEXT NOT NULL,
    min_weight_grams NUMERIC(8, 3) NOT NULL CHECK (min_weight_grams > 0),
    max_weight_grams NUMERIC(8, 3) NOT NULL CHECK (max_weight_grams >= min_weight_grams),
    material TEXT NOT NULL,
    purity TEXT NOT NULL,
    gemstone_preference TEXT NOT NULL DEFAULT 'none',
    quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
    making_budget_mode TEXT NOT NULL CHECK (making_budget_mode IN ('per_gram', 'fixed_total', 'percentage')),
    making_budget_amount NUMERIC(12, 2) NOT NULL CHECK (making_budget_amount > 0),
    currency TEXT NOT NULL DEFAULT 'INR',
    metal_rate_snapshot NUMERIC(12, 2),
    metal_rate_basis TEXT,
    delivery_needed_date DATE NOT NULL,
    notes TEXT CHECK (length(notes) <= 2000),
    state TEXT NOT NULL DEFAULT 'routing' CHECK (state IN ('routing', 'assigned', 'exhausted', 'cancelled')),
    active_offer_id UUID,
    assigned_wholesaler_id UUID REFERENCES public.wholesalers(id),
    accepted_quote_id UUID,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    assigned_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    superseded_request_id UUID REFERENCES public.manufacturing_requests(id)
);

CREATE TABLE IF NOT EXISTS public.manufacturing_request_assets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id UUID REFERENCES public.manufacturing_requests(id) ON DELETE SET NULL,
    owner_retailer_id UUID NOT NULL REFERENCES public.retailers(id),
    storage_bucket TEXT NOT NULL DEFAULT 'manufacturing-requests',
    storage_path TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    checksum TEXT,
    status TEXT NOT NULL DEFAULT 'uploaded' CHECK (status IN ('uploaded', 'attached', 'abandoned')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS public.manufacturing_request_candidates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id UUID NOT NULL REFERENCES public.manufacturing_requests(id) ON DELETE CASCADE,
    wholesaler_id UUID NOT NULL REFERENCES public.wholesalers(id),
    rank INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'offered', 'skipped')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_mfg_req_wholesaler UNIQUE (request_id, wholesaler_id),
    CONSTRAINT uq_mfg_req_rank UNIQUE (request_id, rank)
);

CREATE TABLE IF NOT EXISTS public.manufacturing_offers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id UUID NOT NULL REFERENCES public.manufacturing_requests(id) ON DELETE CASCADE,
    wholesaler_id UUID NOT NULL REFERENCES public.wholesalers(id),
    rank INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'accepted', 'declined', 'expired', 'cancelled', 'skipped')),
    offered_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    responded_at TIMESTAMPTZ,
    decline_reason TEXT CHECK (length(decline_reason) <= 1000),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS public.manufacturing_quotes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    offer_id UUID NOT NULL REFERENCES public.manufacturing_offers(id),
    request_id UUID NOT NULL REFERENCES public.manufacturing_requests(id),
    wholesaler_id UUID NOT NULL REFERENCES public.wholesalers(id),
    making_charge_mode TEXT NOT NULL CHECK (making_charge_mode IN ('per_gram', 'fixed_total', 'percentage')),
    making_charge_amount NUMERIC(12, 2) NOT NULL CHECK (making_charge_amount > 0),
    metal_estimate_amount NUMERIC(12, 2) DEFAULT 0,
    gemstone_estimate_amount NUMERIC(12, 2) DEFAULT 0,
    other_estimate_amount NUMERIC(12, 2) DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'INR',
    proposed_delivery_date DATE NOT NULL,
    comments TEXT CHECK (length(comments) <= 2000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS public.manufacturing_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id UUID NOT NULL REFERENCES public.manufacturing_requests(id) ON DELETE CASCADE,
    offer_id UUID REFERENCES public.manufacturing_offers(id),
    actor_user_id UUID REFERENCES auth.users(id),
    actor_role TEXT NOT NULL,
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS public.manufacturing_notification_outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID REFERENCES public.manufacturing_events(id),
    recipient_user_id UUID NOT NULL REFERENCES auth.users(id),
    kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered', 'failed', 'abandoned', 'pending_config')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    delivered_at TIMESTAMPTZ,
    last_error TEXT,
    lease_token TEXT,
    lease_deadline TIMESTAMPTZ,
    dedup_key TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS public.manufacturing_device_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    device_token TEXT NOT NULL,
    environment TEXT NOT NULL DEFAULT 'production' CHECK (environment IN ('development', 'sandbox', 'production')),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_mfg_user_device UNIQUE (user_id, device_token)
);

CREATE TABLE IF NOT EXISTS public.manufacturing_idempotency (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id UUID NOT NULL REFERENCES auth.users(id),
    action TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_code INTEGER NOT NULL,
    response_body JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_mfg_idempotency UNIQUE (actor_user_id, action, idempotency_key)
);

-- ── 3. CONSTRAINTS & PARTIAL UNIQUE INDEXES ──────────────────────────────────

-- Invariant: At most ONE active offer per request
CREATE UNIQUE INDEX IF NOT EXISTS uq_mfg_active_offer
    ON public.manufacturing_offers(request_id)
 WHERE status = 'active';

-- Invariant: At most ONE accepted offer per request
CREATE UNIQUE INDEX IF NOT EXISTS uq_mfg_accepted_offer
    ON public.manufacturing_offers(request_id)
 WHERE status = 'accepted';

-- Index for worker scanning expired active offers
CREATE INDEX IF NOT EXISTS idx_mfg_active_offers_expiry
    ON public.manufacturing_offers(expires_at)
 WHERE status = 'active';

-- Index for pending notifications outbox
CREATE INDEX IF NOT EXISTS idx_mfg_notification_pending
    ON public.manufacturing_notification_outbox(available_at)
 WHERE status = 'pending';

-- ── 4. RLS POLICIES & SECURITY DEFINER HELPERS ──────────────────────────────
-- Non-recursive helper functions to prevent RLS recursion between requests and offers.
CREATE OR REPLACE FUNCTION public.request_visible_to_current_wholesaler(p_request_id UUID)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM public.manufacturing_offers mo
        WHERE mo.request_id = p_request_id
          AND mo.wholesaler_id = public.my_verified_wholesaler_id()
    ) OR EXISTS (
        SELECT 1
        FROM public.manufacturing_requests mr
        WHERE mr.id = p_request_id
          AND mr.assigned_wholesaler_id = public.my_verified_wholesaler_id()
    );
$$;

CREATE OR REPLACE FUNCTION public.request_owned_by_current_retailer(p_request_id UUID)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM public.manufacturing_requests mr
        WHERE mr.id = p_request_id
          AND mr.retailer_id = public.my_verified_retailer_id()
    );
$$;

ALTER TABLE public.manufacturing_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.manufacturing_request_assets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.manufacturing_request_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.manufacturing_offers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.manufacturing_quotes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.manufacturing_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.manufacturing_notification_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.manufacturing_device_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.manufacturing_idempotency ENABLE ROW LEVEL SECURITY;

-- Retailers can view their own requests.
-- Assigned/offered wholesalers can view through non-recursive helper.
DROP POLICY IF EXISTS "mfg_requests_select" ON public.manufacturing_requests;
CREATE POLICY "mfg_requests_select" ON public.manufacturing_requests
FOR SELECT USING (
    retailer_id = public.my_verified_retailer_id()
    OR assigned_wholesaler_id = public.my_verified_wholesaler_id()
    OR public.request_visible_to_current_wholesaler(id)
);

-- Assets can be viewed by the owning retailer, or wholesalers offered/assigned to the request.
DROP POLICY IF EXISTS "mfg_assets_select" ON public.manufacturing_request_assets;
CREATE POLICY "mfg_assets_select" ON public.manufacturing_request_assets
FOR SELECT USING (
    owner_retailer_id = public.my_verified_retailer_id()
    OR (request_id IS NOT NULL AND public.request_visible_to_current_wholesaler(request_id))
);

-- Wholesalers see only offers addressed to them.
-- Retailers see offers for their requests via non-recursive helper.
DROP POLICY IF EXISTS "mfg_offers_select" ON public.manufacturing_offers;
CREATE POLICY "mfg_offers_select" ON public.manufacturing_offers
FOR SELECT USING (
    wholesaler_id = public.my_verified_wholesaler_id()
    OR public.request_owned_by_current_retailer(request_id)
);

-- Quotes visible to the submitting wholesaler and the owning retailer.
DROP POLICY IF EXISTS "mfg_quotes_select" ON public.manufacturing_quotes;
CREATE POLICY "mfg_quotes_select" ON public.manufacturing_quotes
FOR SELECT USING (
    wholesaler_id = public.my_verified_wholesaler_id()
    OR public.request_owned_by_current_retailer(request_id)
);

-- Device tokens: users manage only their own tokens.
DROP POLICY IF EXISTS "mfg_device_tokens_own" ON public.manufacturing_device_tokens;
CREATE POLICY "mfg_device_tokens_own" ON public.manufacturing_device_tokens
FOR ALL USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());

-- Candidates, internal events, outbox, and idempotency are internal only (service role bypasses RLS).

-- ── 5. TRANSACTIONAL RPCS ────────────────────────────────────────────────────

-- 5.1 Request Creation
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

-- 5.2 Wholesaler Offer Acceptance
CREATE OR REPLACE FUNCTION public.manufacturing_offer_accept(
    p_offer_id UUID,
    p_making_charge_mode TEXT,
    p_making_charge_amount NUMERIC,
    p_metal_estimate_amount NUMERIC,
    p_gemstone_estimate_amount NUMERIC,
    p_other_estimate_amount NUMERIC,
    p_proposed_delivery_date DATE,
    p_comments TEXT DEFAULT NULL,
    p_expected_version INTEGER DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'public', 'pg_temp'
AS $$
DECLARE
    v_user_id UUID := auth.uid();
    v_wholesaler_id UUID := public.my_verified_wholesaler_id();
    v_offer public.manufacturing_offers%ROWTYPE;
    v_request public.manufacturing_requests%ROWTYPE;
    v_quote_id UUID;
    v_now TIMESTAMPTZ := clock_timestamp();
    v_event_id UUID;
BEGIN
    IF v_user_id IS NULL OR v_wholesaler_id IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error', 'UNAUTHORIZED', 'message', 'Only verified wholesalers can accept offers.');
    END IF;

    -- Lock offer row
    SELECT * INTO v_offer
      FROM public.manufacturing_offers
     WHERE id = p_offer_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'error', 'OFFER_NOT_FOUND', 'message', 'Offer not found.');
    END IF;

    IF v_offer.wholesaler_id != v_wholesaler_id THEN
        RETURN jsonb_build_object('ok', false, 'error', 'FORBIDDEN', 'message', 'This offer is not addressed to you.');
    END IF;

    -- Lock request row
    SELECT * INTO v_request
      FROM public.manufacturing_requests
     WHERE id = v_offer.request_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'error', 'REQUEST_NOT_FOUND', 'message', 'Parent request not found.');
    END IF;

    -- If already accepted by this caller, idempotent return
    IF v_offer.status = 'accepted' AND v_request.state = 'assigned' AND v_request.assigned_wholesaler_id = v_wholesaler_id THEN
        RETURN jsonb_build_object('ok', true, 'status', 'assigned', 'request_id', v_request.id, 'quote_id', v_request.accepted_quote_id, 'idempotent', true);
    END IF;

    IF p_expected_version IS NOT NULL AND v_offer.version != p_expected_version THEN
        RETURN jsonb_build_object('ok', false, 'error', 'VERSION_CONFLICT', 'message', 'Offer state was updated on another device.');
    END IF;

    -- Wall clock check evaluated strictly after acquiring row locks
    v_now := clock_timestamp();
    IF v_offer.status != 'active' OR v_request.state != 'routing' OR v_now >= v_offer.expires_at THEN
        -- Mark expired if still active
        IF v_offer.status = 'active' THEN
            UPDATE public.manufacturing_offers
               SET status = 'expired', responded_at = v_now, updated_at = v_now
             WHERE id = v_offer.id;
        END IF;
        RETURN jsonb_build_object('ok', false, 'error', 'OFFER_EXPIRED', 'message', 'Offer has expired and is no longer actionable.');
    END IF;

    -- Commercial constraints validation
    IF p_making_charge_mode != v_request.making_budget_mode THEN
        RETURN jsonb_build_object('ok', false, 'error', 'MODE_MISMATCH', 'message', 'Quote making charge mode must match the requested budget mode.');
    END IF;
    IF p_making_charge_amount > v_request.making_budget_amount THEN
        RETURN jsonb_build_object('ok', false, 'error', 'BUDGET_EXCEEDED', 'message', 'Making charge quote exceeds the requested budget constraint.');
    END IF;
    IF p_proposed_delivery_date > v_request.delivery_needed_date THEN
        RETURN jsonb_build_object('ok', false, 'error', 'DEADLINE_EXCEEDED', 'message', 'Proposed delivery date is later than the requested deadline.');
    END IF;

    -- Create quote
    INSERT INTO public.manufacturing_quotes (
        offer_id, request_id, wholesaler_id, making_charge_mode, making_charge_amount,
        metal_estimate_amount, gemstone_estimate_amount, other_estimate_amount,
        currency, proposed_delivery_date, comments
    ) VALUES (
        v_offer.id, v_request.id, v_wholesaler_id, p_making_charge_mode, p_making_charge_amount,
        COALESCE(p_metal_estimate_amount, 0), COALESCE(p_gemstone_estimate_amount, 0), COALESCE(p_other_estimate_amount, 0),
        v_request.currency, p_proposed_delivery_date, btrim(p_comments)
    ) RETURNING id INTO v_quote_id;

    -- Update offer
    UPDATE public.manufacturing_offers
       SET status = 'accepted', responded_at = v_now, version = version + 1, updated_at = v_now
     WHERE id = v_offer.id;

    -- Update request to assigned and clear active offer
    UPDATE public.manufacturing_requests
       SET state = 'assigned',
           assigned_wholesaler_id = v_wholesaler_id,
           accepted_quote_id = v_quote_id,
           active_offer_id = NULL,
           assigned_at = v_now,
           version = version + 1,
           updated_at = v_now
     WHERE id = v_request.id;

    -- Audit event
    INSERT INTO public.manufacturing_events (
        request_id, offer_id, actor_user_id, actor_role, event_type, from_state, to_state, metadata
    ) VALUES (
        v_request.id, v_offer.id, v_user_id, 'wholesaler', 'OFFER_ACCEPTED', 'routing', 'assigned',
        jsonb_build_object('quote_id', v_quote_id, 'wholesaler_id', v_wholesaler_id, 'making_charge_amount', p_making_charge_amount)
    ) RETURNING id INTO v_event_id;

    -- Enqueue notification for the retailer
    INSERT INTO public.manufacturing_notification_outbox (
        event_id, recipient_user_id, kind, payload, dedup_key
    ) VALUES (
        v_event_id, v_request.created_by_user_id, 'MANUFACTURING_REQUEST_ASSIGNED',
        jsonb_build_object('request_id', v_request.id, 'wholesaler_id', v_wholesaler_id, 'quote_id', v_quote_id),
        'mfg_assigned_' || v_request.id::text
    );

    RETURN jsonb_build_object(
        'ok', true,
        'status', 'assigned',
        'request_id', v_request.id,
        'offer_id', v_offer.id,
        'quote_id', v_quote_id
    );
END;
$$;

REVOKE ALL ON FUNCTION public.manufacturing_offer_accept FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.manufacturing_offer_accept TO authenticated;

-- 5.3 Wholesaler Offer Decline
CREATE OR REPLACE FUNCTION public.manufacturing_offer_decline(
    p_offer_id UUID,
    p_reason TEXT DEFAULT NULL,
    p_offer_duration_seconds INTEGER DEFAULT 1800
)
RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'public', 'pg_temp'
AS $$
DECLARE
    v_user_id UUID := auth.uid();
    v_wholesaler_id UUID := public.my_verified_wholesaler_id();
    v_offer public.manufacturing_offers%ROWTYPE;
    v_request public.manufacturing_requests%ROWTYPE;
    v_next_candidate RECORD;
    v_new_offer_id UUID;
    v_expires_at TIMESTAMPTZ;
    v_event_id UUID;
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    IF v_user_id IS NULL OR v_wholesaler_id IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error', 'UNAUTHORIZED', 'message', 'Only verified wholesalers can decline offers.');
    END IF;

    -- Lock offer row
    SELECT * INTO v_offer
      FROM public.manufacturing_offers
     WHERE id = p_offer_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'error', 'OFFER_NOT_FOUND', 'message', 'Offer not found.');
    END IF;

    IF v_offer.wholesaler_id != v_wholesaler_id THEN
        RETURN jsonb_build_object('ok', false, 'error', 'FORBIDDEN', 'message', 'This offer is not addressed to you.');
    END IF;

    -- Lock request row
    SELECT * INTO v_request
      FROM public.manufacturing_requests
     WHERE id = v_offer.request_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'error', 'REQUEST_NOT_FOUND', 'message', 'Parent request not found.');
    END IF;

    IF v_offer.status = 'declined' THEN
        RETURN jsonb_build_object('ok', true, 'status', 'declined', 'idempotent', true);
    END IF;

    IF v_offer.status != 'active' OR v_request.state != 'routing' THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVALID_STATE', 'message', 'Offer is no longer active.');
    END IF;

    v_now := clock_timestamp();

    -- Mark offer declined
    UPDATE public.manufacturing_offers
       SET status = 'declined',
           decline_reason = btrim(p_reason),
           responded_at = v_now,
           version = version + 1,
           updated_at = v_now
     WHERE id = v_offer.id;

    -- Record decline event
    INSERT INTO public.manufacturing_events (
        request_id, offer_id, actor_user_id, actor_role, event_type, from_state, to_state, metadata
    ) VALUES (
        v_request.id, v_offer.id, v_user_id, 'wholesaler', 'OFFER_DECLINED', 'active', 'declined',
        jsonb_build_object('wholesaler_id', v_wholesaler_id, 'reason', p_reason)
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
        -- Mark candidate offered
        UPDATE public.manufacturing_request_candidates
           SET status = 'offered', updated_at = v_now
         WHERE request_id = v_request.id AND wholesaler_id = v_next_candidate.wholesaler_id;

        -- Create next offer
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
            request_id, offer_id, actor_user_id, actor_role, event_type, from_state, to_state, metadata
        ) VALUES (
            v_request.id, v_new_offer_id, v_user_id, 'system', 'OFFER_ADVANCED_AFTER_DECLINE', 'active', 'active',
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

        RETURN jsonb_build_object('ok', true, 'status', 'declined', 'advanced', true, 'new_offer_id', v_new_offer_id);
    ELSE
        -- No further candidates -> Request exhausted
        UPDATE public.manufacturing_requests
           SET state = 'exhausted', active_offer_id = NULL, version = version + 1, updated_at = v_now
         WHERE id = v_request.id;

        INSERT INTO public.manufacturing_events (
            request_id, actor_user_id, actor_role, event_type, from_state, to_state, metadata
        ) VALUES (
            v_request.id, v_user_id, 'system', 'REQUEST_EXHAUSTED', 'routing', 'exhausted',
            jsonb_build_object('last_offer_id', v_offer.id, 'reason', 'All eligible candidates declined or expired')
        ) RETURNING id INTO v_event_id;

        -- Notify retailer
        INSERT INTO public.manufacturing_notification_outbox (
            event_id, recipient_user_id, kind, payload, dedup_key
        ) VALUES (
            v_event_id, v_request.created_by_user_id, 'MANUFACTURING_REQUEST_EXHAUSTED',
            jsonb_build_object('request_id', v_request.id),
            'mfg_exhausted_' || v_request.id::text
        );

        RETURN jsonb_build_object('ok', true, 'status', 'declined', 'advanced', true, 'state', 'exhausted');
    END IF;
END;
$$;

REVOKE ALL ON FUNCTION public.manufacturing_offer_decline FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.manufacturing_offer_decline TO authenticated;

-- 5.4 Offer Expire and Advance (Executed by Worker or on Stale Check)
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

-- 5.5 Retailer Request Cancellation
CREATE OR REPLACE FUNCTION public.manufacturing_request_cancel(
    p_request_id UUID,
    p_reason TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'public', 'pg_temp'
AS $$
DECLARE
    v_user_id UUID := auth.uid();
    v_retailer_id UUID := public.my_verified_retailer_id();
    v_request public.manufacturing_requests%ROWTYPE;
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    IF v_user_id IS NULL OR v_retailer_id IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error', 'UNAUTHORIZED', 'message', 'Only verified retailer owners can cancel requests.');
    END IF;

    -- Row lock request
    SELECT * INTO v_request
      FROM public.manufacturing_requests
     WHERE id = p_request_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'error', 'NOT_FOUND', 'message', 'Request not found.');
    END IF;

    IF v_request.retailer_id != v_retailer_id THEN
        RETURN jsonb_build_object('ok', false, 'error', 'FORBIDDEN', 'message', 'Not authorized to cancel this request.');
    END IF;

    IF v_request.state = 'cancelled' THEN
        RETURN jsonb_build_object('ok', true, 'state', 'cancelled', 'idempotent', true);
    END IF;

    IF v_request.state = 'assigned' THEN
        RETURN jsonb_build_object('ok', false, 'error', 'ALREADY_ASSIGNED', 'message', 'Request has already been accepted and assigned to a wholesaler.');
    END IF;

    v_now := clock_timestamp();

    -- If active offer exists, cancel it
    IF v_request.active_offer_id IS NOT NULL THEN
        UPDATE public.manufacturing_offers
           SET status = 'cancelled', updated_at = v_now
         WHERE id = v_request.active_offer_id;
    END IF;

    UPDATE public.manufacturing_requests
       SET state = 'cancelled',
           active_offer_id = NULL,
           cancelled_at = v_now,
           version = version + 1,
           updated_at = v_now
     WHERE id = v_request.id;

    INSERT INTO public.manufacturing_events (
        request_id, actor_user_id, actor_role, event_type, from_state, to_state, metadata
    ) VALUES (
        v_request.id, v_user_id, 'retailer', 'REQUEST_CANCELLED', v_request.state, 'cancelled',
        jsonb_build_object('reason', p_reason)
    );

    RETURN jsonb_build_object('ok', true, 'state', 'cancelled');
END;
$$;

REVOKE ALL ON FUNCTION public.manufacturing_request_cancel FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.manufacturing_request_cancel TO authenticated;

-- 5.6 Worker Reconciliation: Sweep Expired Offers with FOR UPDATE SKIP LOCKED
CREATE OR REPLACE FUNCTION public.manufacturing_sweep_expired_offers(
    p_batch_size INT DEFAULT 25,
    p_offer_duration_seconds INT DEFAULT 1800
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_req RECORD;
    v_advanced INT := 0;
    v_results JSONB := '[]'::jsonb;
    v_step_res JSONB;
BEGIN
    FOR v_req IN
        SELECT r.id AS request_id
        FROM public.manufacturing_requests r
        JOIN public.manufacturing_offers o ON o.request_id = r.id AND o.status = 'active'
        WHERE r.state = 'routing'
          AND o.expires_at <= clock_timestamp()
        ORDER BY o.expires_at ASC
        LIMIT p_batch_size
        FOR UPDATE OF r SKIP LOCKED
    LOOP
        v_step_res := public.manufacturing_offer_expire_and_advance(v_req.request_id, p_offer_duration_seconds);
        v_results := v_results || jsonb_build_object('request_id', v_req.request_id, 'result', v_step_res);
        IF (v_step_res->>'advanced')::boolean IS TRUE THEN
            v_advanced := v_advanced + 1;
        END IF;
    END LOOP;

    RETURN jsonb_build_object(
        'advanced_count', v_advanced,
        'processed', jsonb_array_length(v_results),
        'details', v_results
    );
END;
$$;

REVOKE ALL ON FUNCTION public.manufacturing_sweep_expired_offers FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.manufacturing_sweep_expired_offers FROM authenticated;
GRANT EXECUTE ON FUNCTION public.manufacturing_sweep_expired_offers TO service_role;

-- 5.7 Worker Reconciliation: Claim Outbox Batch with FOR UPDATE SKIP LOCKED
CREATE OR REPLACE FUNCTION public.manufacturing_claim_outbox_batch(
    p_worker_id TEXT,
    p_batch_size INT DEFAULT 20,
    p_lease_seconds INT DEFAULT 60
)
RETURNS TABLE (
    id UUID,
    recipient_user_id UUID,
    kind TEXT,
    payload JSONB,
    attempts INT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_deadline TIMESTAMPTZ := v_now + (p_lease_seconds || ' seconds')::interval;
BEGIN
    RETURN QUERY
    WITH claimed AS (
        SELECT o.id
        FROM public.manufacturing_notification_outbox o
        WHERE o.status = 'pending'
          AND o.available_at <= v_now
          AND (o.lease_deadline IS NULL OR o.lease_deadline <= v_now)
        ORDER BY o.available_at ASC
        LIMIT p_batch_size
        FOR UPDATE SKIP LOCKED
    )
    UPDATE public.manufacturing_notification_outbox o
    SET lease_token = p_worker_id,
        lease_deadline = v_deadline,
        attempts = o.attempts + 1
    FROM claimed
    WHERE o.id = claimed.id
    RETURNING o.id, o.recipient_user_id, o.kind, o.payload, o.attempts;
END;
$$;

REVOKE ALL ON FUNCTION public.manufacturing_claim_outbox_batch FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.manufacturing_claim_outbox_batch FROM authenticated;
GRANT EXECUTE ON FUNCTION public.manufacturing_claim_outbox_batch TO service_role;

COMMIT;
