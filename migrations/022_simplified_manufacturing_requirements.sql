-- Simplified enquiry specifications and one total price per supplier response.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
ALTER TABLE public.manufacturing_requests ALTER COLUMN purity DROP NOT NULL;
ALTER TABLE public.manufacturing_quotes DROP CONSTRAINT IF EXISTS manufacturing_quotes_making_charge_mode_check;
ALTER TABLE public.manufacturing_quotes ADD CONSTRAINT manufacturing_quotes_making_charge_mode_check CHECK (making_charge_mode IN ('per_gram','fixed_total','percentage','total_quote'));
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
 IF p_making_charge_mode IS NULL OR p_making_charge_mode NOT IN ('per_gram','fixed_total','percentage','total_quote') OR (p_making_charge_mode!='total_quote' AND r.making_budget_mode IS NOT NULL AND p_making_charge_mode!=r.making_budget_mode) OR p_making_charge_amount IS NULL OR p_making_charge_amount<=0 OR (p_making_charge_mode!='total_quote' AND r.making_budget_amount IS NOT NULL AND p_making_charge_amount>r.making_budget_amount)
    OR (p_making_charge_mode='percentage' AND COALESCE(p_metal_estimate_amount,0)<=0) OR COALESCE(p_metal_estimate_amount,0)<0 OR COALESCE(p_gemstone_estimate_amount,0)<0 OR COALESCE(p_other_estimate_amount,0)<0
    OR (p_making_charge_mode='total_quote' AND (COALESCE(p_metal_estimate_amount,0)!=0 OR COALESCE(p_gemstone_estimate_amount,0)!=0 OR COALESCE(p_other_estimate_amount,0)!=0))
    OR p_proposed_delivery_date IS NULL OR p_proposed_delivery_date<CURRENT_DATE OR p_proposed_delivery_date>r.delivery_needed_date
    OR length(p_comments)>2000 THEN
   RETURN jsonb_build_object('ok',false,'error','INVALID_QUOTE','message','Enter a valid price and delivery date. Legacy breakdowns must meet their original requirements.'); END IF;
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

COMMIT;
