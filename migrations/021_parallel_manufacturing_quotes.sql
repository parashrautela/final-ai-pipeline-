-- Parallel quotations: new requests only; legacy queues remain intact.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
ALTER TABLE public.manufacturing_requests ALTER COLUMN making_budget_mode DROP NOT NULL;
ALTER TABLE public.manufacturing_requests ALTER COLUMN making_budget_amount DROP NOT NULL;
ALTER TABLE public.manufacturing_requests DROP CONSTRAINT IF EXISTS manufacturing_requests_budget_pair_check;
ALTER TABLE public.manufacturing_requests ADD CONSTRAINT manufacturing_requests_budget_pair_check CHECK ((making_budget_mode IS NULL AND making_budget_amount IS NULL) OR (making_budget_mode IS NOT NULL AND making_budget_amount IS NOT NULL));
ALTER TABLE public.manufacturing_requests ADD COLUMN IF NOT EXISTS broadcast_mode text NOT NULL DEFAULT 'sequential' CHECK (broadcast_mode IN ('sequential','parallel'));
ALTER TABLE public.manufacturing_requests ADD COLUMN IF NOT EXISTS quotation_deadline timestamptz;
ALTER TABLE public.manufacturing_requests DROP CONSTRAINT IF EXISTS manufacturing_requests_state_check;
ALTER TABLE public.manufacturing_requests ADD CONSTRAINT manufacturing_requests_state_check CHECK (state IN ('routing','collecting','reviewing','assigned','exhausted','cancelled'));
ALTER TABLE public.manufacturing_offers DROP CONSTRAINT IF EXISTS manufacturing_offers_status_check;
ALTER TABLE public.manufacturing_offers ADD CONSTRAINT manufacturing_offers_status_check CHECK (status IN ('active','open','quoted','not_selected','accepted','declined','expired','cancelled','skipped'));
-- Keep the old unique active-offer index: parallel invitations use 'open'.
CREATE INDEX IF NOT EXISTS idx_mfg_parallel_deadline ON public.manufacturing_requests(quotation_deadline) WHERE state='collecting';
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


-- All parallel mutations lock the request BEFORE its offer: quote/award/close races serialize.
CREATE OR REPLACE FUNCTION public.manufacturing_quote_submit(
 p_offer_id uuid,p_making_charge_mode text,p_making_charge_amount numeric,
 p_metal_estimate_amount numeric,p_gemstone_estimate_amount numeric,p_other_estimate_amount numeric,
 p_proposed_delivery_date date,p_comments text DEFAULT NULL,p_expected_version integer DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE o public.manufacturing_offers%ROWTYPE; r public.manufacturing_requests%ROWTYPE; q public.manufacturing_quotes%ROWTYPE;
 wid uuid:=public.my_verified_wholesaler_id(); rid uuid; eid uuid; qid uuid;
BEGIN
 IF wid IS NULL THEN RETURN jsonb_build_object('ok',false,'error','UNAUTHORIZED','message','Verified wholesaler required.'); END IF;
 SELECT request_id INTO rid FROM public.manufacturing_offers WHERE id=p_offer_id AND wholesaler_id=wid;
 IF rid IS NULL THEN RETURN jsonb_build_object('ok',false,'error','NOT_FOUND','message','Invitation not found.'); END IF;
 SELECT * INTO r FROM public.manufacturing_requests WHERE id=rid FOR UPDATE;
 SELECT * INTO o FROM public.manufacturing_offers WHERE id=p_offer_id FOR UPDATE;
 IF r.broadcast_mode!='parallel' THEN RETURN jsonb_build_object('ok',false,'error','INVALID_STATE','message','This enquiry uses the original acceptance flow.'); END IF;
 SELECT * INTO q FROM public.manufacturing_quotes WHERE offer_id=o.id ORDER BY created_at LIMIT 1;
 IF FOUND THEN
   IF q.making_charge_mode=p_making_charge_mode AND q.making_charge_amount=p_making_charge_amount
      AND q.metal_estimate_amount=COALESCE(p_metal_estimate_amount,0) AND q.gemstone_estimate_amount=COALESCE(p_gemstone_estimate_amount,0)
      AND q.other_estimate_amount=COALESCE(p_other_estimate_amount,0) AND q.proposed_delivery_date=p_proposed_delivery_date
      AND q.comments IS NOT DISTINCT FROM NULLIF(btrim(p_comments),'') THEN
      RETURN jsonb_build_object('ok',true,'quote_id',q.id,'request_id',r.id,'status','quoted');
   END IF;
   RETURN jsonb_build_object('ok',false,'error','QUOTE_EXISTS','message','Your quote has already been submitted. Refresh to view it.');
 END IF;
 IF r.state!='collecting' OR o.status!='open' OR clock_timestamp()>=r.quotation_deadline THEN
   RETURN jsonb_build_object('ok',false,'error','CLOSED','message','This enquiry is no longer accepting quotes.'); END IF;
 IF p_making_charge_mode IS NULL OR p_making_charge_mode NOT IN ('per_gram','fixed_total','percentage') OR (r.making_budget_mode IS NOT NULL AND p_making_charge_mode!=r.making_budget_mode) OR p_making_charge_amount IS NULL OR p_making_charge_amount<=0 OR (r.making_budget_amount IS NOT NULL AND p_making_charge_amount>r.making_budget_amount)
    OR (p_making_charge_mode='percentage' AND COALESCE(p_metal_estimate_amount,0)<=0) OR COALESCE(p_metal_estimate_amount,0)<0 OR COALESCE(p_gemstone_estimate_amount,0)<0 OR COALESCE(p_other_estimate_amount,0)<0
    OR p_proposed_delivery_date IS NULL OR p_proposed_delivery_date<CURRENT_DATE OR p_proposed_delivery_date>r.delivery_needed_date
    OR length(p_comments)>2000 THEN
   RETURN jsonb_build_object('ok',false,'error','INVALID_QUOTE','message','Quote must meet the budget and delivery requirements, with nonnegative estimates.'); END IF;
 INSERT INTO public.manufacturing_quotes(offer_id,request_id,wholesaler_id,making_charge_mode,making_charge_amount,metal_estimate_amount,gemstone_estimate_amount,other_estimate_amount,currency,proposed_delivery_date,comments)
 VALUES(o.id,r.id,wid,p_making_charge_mode,p_making_charge_amount,COALESCE(p_metal_estimate_amount,0),COALESCE(p_gemstone_estimate_amount,0),COALESCE(p_other_estimate_amount,0),r.currency,p_proposed_delivery_date,NULLIF(btrim(p_comments),'')) RETURNING id INTO qid;
 UPDATE public.manufacturing_offers SET status='quoted',responded_at=clock_timestamp(),version=version+1,updated_at=clock_timestamp() WHERE id=o.id;
 INSERT INTO public.manufacturing_events(request_id,offer_id,actor_user_id,actor_role,event_type,from_state,to_state,metadata)
 VALUES(r.id,o.id,auth.uid(),'wholesaler','QUOTE_SUBMITTED','open','quoted',jsonb_build_object('quote_id',qid)) RETURNING id INTO eid;
 INSERT INTO public.manufacturing_notification_outbox(event_id,recipient_user_id,kind,payload,dedup_key)
 VALUES(eid,r.created_by_user_id,'MANUFACTURING_QUOTE_RECEIVED',jsonb_build_object('request_id',r.id,'quote_id',qid),'mfg_quote_'||qid::text);
 RETURN jsonb_build_object('ok',true,'quote_id',qid,'request_id',r.id,'status','quoted');
END; $$;
REVOKE ALL ON FUNCTION public.manufacturing_quote_submit FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION public.manufacturing_quote_submit TO authenticated;

CREATE OR REPLACE FUNCTION public.manufacturing_quote_award(p_request_id uuid,p_quote_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE r public.manufacturing_requests%ROWTYPE; q public.manufacturing_quotes%ROWTYPE; eid uuid; uid uuid; c record;
BEGIN
 IF public.my_verified_retailer_id() IS NULL THEN RETURN jsonb_build_object('ok',false,'error','UNAUTHORIZED','message','Verified retailer required.'); END IF;
 SELECT * INTO r FROM public.manufacturing_requests WHERE id=p_request_id AND retailer_id=public.my_verified_retailer_id() FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'error','NOT_FOUND','message','Enquiry not found.'); END IF;
 IF r.state='assigned' AND r.accepted_quote_id=p_quote_id THEN RETURN jsonb_build_object('ok',true,'state','assigned','quote_id',p_quote_id); END IF;
 IF r.broadcast_mode!='parallel' OR r.state NOT IN ('collecting','reviewing') THEN RETURN jsonb_build_object('ok',false,'error','CLOSED','message','Enquiry has already closed or been awarded.'); END IF;
 SELECT * INTO q FROM public.manufacturing_quotes WHERE id=p_quote_id AND request_id=r.id;
 IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'error','NOT_FOUND','message','Quote does not belong to this enquiry.'); END IF;
 IF q.proposed_delivery_date<CURRENT_DATE OR NOT EXISTS(SELECT 1 FROM public.wholesalers WHERE id=q.wholesaler_id AND verification_status='verified') THEN
   RETURN jsonb_build_object('ok',false,'error','INVALID_QUOTE','message','Supplier or delivery date is no longer eligible.'); END IF;
 UPDATE public.manufacturing_requests SET state='assigned',assigned_wholesaler_id=q.wholesaler_id,accepted_quote_id=q.id,assigned_at=clock_timestamp(),active_offer_id=NULL,version=version+1,updated_at=clock_timestamp() WHERE id=r.id;
 UPDATE public.manufacturing_offers SET status=CASE WHEN id=q.offer_id THEN 'accepted' ELSE 'not_selected' END,version=version+1,updated_at=clock_timestamp() WHERE request_id=r.id AND status IN ('open','quoted');
 INSERT INTO public.manufacturing_events(request_id,offer_id,actor_user_id,actor_role,event_type,from_state,to_state,metadata)
 VALUES(r.id,q.offer_id,auth.uid(),'retailer','QUOTE_AWARDED',r.state,'assigned',jsonb_build_object('quote_id',q.id,'wholesaler_id',q.wholesaler_id)) RETURNING id INTO eid;
 FOR c IN SELECT o.id,o.wholesaler_id,w.user_id FROM public.manufacturing_offers o JOIN public.wholesalers w ON w.id=o.wholesaler_id WHERE o.request_id=r.id AND o.status IN ('accepted','not_selected') LOOP
   INSERT INTO public.manufacturing_notification_outbox(event_id,recipient_user_id,kind,payload,dedup_key)
   VALUES(eid,c.user_id,CASE WHEN c.wholesaler_id=q.wholesaler_id THEN 'MANUFACTURING_QUOTE_AWARDED' ELSE 'MANUFACTURING_ENQUIRY_CLOSED' END,jsonb_build_object('request_id',r.id,'offer_id',c.id),'mfg_award_'||r.id::text||'_'||c.id::text) ON CONFLICT(dedup_key) DO NOTHING;
 END LOOP;
 RETURN jsonb_build_object('ok',true,'state','assigned','request_id',r.id,'quote_id',q.id);
END; $$;
REVOKE ALL ON FUNCTION public.manufacturing_quote_award FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION public.manufacturing_quote_award TO authenticated;

CREATE OR REPLACE FUNCTION public.manufacturing_broadcast_decline(p_offer_id uuid,p_reason text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE r public.manufacturing_requests%ROWTYPE; o public.manufacturing_offers%ROWTYPE; rid uuid;
BEGIN
 IF public.my_verified_wholesaler_id() IS NULL THEN RETURN jsonb_build_object('ok',false,'error','UNAUTHORIZED','message','Verified wholesaler required.'); END IF;
 SELECT request_id INTO rid FROM public.manufacturing_offers WHERE id=p_offer_id AND wholesaler_id=public.my_verified_wholesaler_id();
 IF rid IS NULL THEN RETURN jsonb_build_object('ok',false,'error','NOT_FOUND','message','Invitation not found.'); END IF;
 SELECT * INTO r FROM public.manufacturing_requests WHERE id=rid FOR UPDATE;
 SELECT * INTO o FROM public.manufacturing_offers WHERE id=p_offer_id FOR UPDATE;
 IF o.status='declined' THEN RETURN jsonb_build_object('ok',true,'status','declined'); END IF;
 IF r.broadcast_mode!='parallel' OR r.state!='collecting' OR o.status!='open' OR clock_timestamp()>=r.quotation_deadline THEN RETURN jsonb_build_object('ok',false,'error','CLOSED','message','Invitation is no longer open.'); END IF;
 IF length(p_reason)>1000 THEN RETURN jsonb_build_object('ok',false,'error','INVALID_REASON','message','Reason is too long.'); END IF;
 UPDATE public.manufacturing_offers SET status='declined',decline_reason=NULLIF(btrim(p_reason),''),responded_at=clock_timestamp(),version=version+1,updated_at=clock_timestamp() WHERE id=o.id;
 INSERT INTO public.manufacturing_events(request_id,offer_id,actor_user_id,actor_role,event_type,from_state,to_state) VALUES(r.id,o.id,auth.uid(),'wholesaler','OFFER_DECLINED','open','declined');
 RETURN jsonb_build_object('ok',true,'status','declined','advanced',false);
END; $$;
REVOKE ALL ON FUNCTION public.manufacturing_broadcast_decline FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION public.manufacturing_broadcast_decline TO authenticated;

CREATE OR REPLACE FUNCTION public.manufacturing_close_quotation_windows(p_batch_size integer DEFAULT 25)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE r record; n integer:=0; s text; eid uuid;
BEGIN
 FOR r IN SELECT * FROM public.manufacturing_requests WHERE broadcast_mode='parallel' AND state='collecting' AND quotation_deadline<=clock_timestamp() ORDER BY quotation_deadline LIMIT p_batch_size FOR UPDATE SKIP LOCKED LOOP
   s:=CASE WHEN EXISTS(SELECT 1 FROM public.manufacturing_quotes WHERE request_id=r.id) THEN 'reviewing' ELSE 'exhausted' END;
   UPDATE public.manufacturing_requests SET state=s,version=version+1,updated_at=clock_timestamp() WHERE id=r.id;
   UPDATE public.manufacturing_offers SET status='expired',version=version+1,updated_at=clock_timestamp() WHERE request_id=r.id AND status='open';
   INSERT INTO public.manufacturing_events(request_id,actor_role,event_type,from_state,to_state) VALUES(r.id,'system','QUOTATION_WINDOW_CLOSED','collecting',s) RETURNING id INTO eid;
   INSERT INTO public.manufacturing_notification_outbox(event_id,recipient_user_id,kind,payload,dedup_key) VALUES(eid,r.created_by_user_id,'MANUFACTURING_QUOTES_READY',jsonb_build_object('request_id',r.id,'state',s),'mfg_window_'||r.id::text);
   n:=n+1;
 END LOOP;
 RETURN jsonb_build_object('closed_count',n);
END; $$;
REVOKE ALL ON FUNCTION public.manufacturing_close_quotation_windows FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.manufacturing_close_quotation_windows TO service_role;
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

    IF EXISTS(SELECT 1 FROM public.manufacturing_offers o JOIN public.manufacturing_requests r ON r.id=o.request_id WHERE o.id=p_offer_id AND r.broadcast_mode='parallel') THEN
        RETURN jsonb_build_object('ok',false,'error','UPDATE_REQUIRED','message','This is a quotation enquiry. Update the app to submit a quote; the retailer selects the supplier.');
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

    IF EXISTS(SELECT 1 FROM public.manufacturing_offers o JOIN public.manufacturing_requests r ON r.id=o.request_id WHERE o.id=p_offer_id AND r.broadcast_mode='parallel') THEN
        RETURN public.manufacturing_broadcast_decline(p_offer_id,p_reason);
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
    v_event_id UUID;
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

    IF v_request.broadcast_mode='parallel' THEN
        UPDATE public.manufacturing_offers SET status='cancelled',version=version+1,updated_at=v_now WHERE request_id=v_request.id AND status IN ('open','quoted');
    END IF;
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
    ) RETURNING id INTO v_event_id;
    IF v_request.broadcast_mode='parallel' THEN
        INSERT INTO public.manufacturing_notification_outbox(event_id,recipient_user_id,kind,payload,dedup_key)
        SELECT v_event_id,w.user_id,'MANUFACTURING_ENQUIRY_CANCELLED',jsonb_build_object('request_id',v_request.id,'offer_id',o.id),'mfg_cancel_'||o.id::text
        FROM public.manufacturing_offers o JOIN public.wholesalers w ON w.id=o.wholesaler_id
        WHERE o.request_id=v_request.id AND w.user_id IS NOT NULL ON CONFLICT(dedup_key) DO NOTHING;
    END IF;

    RETURN jsonb_build_object('ok', true, 'state', 'cancelled');
END;
$$;

REVOKE ALL ON FUNCTION public.manufacturing_request_cancel FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.manufacturing_request_cancel TO authenticated;


COMMIT;
