-- Awarded custom enquiries participate in the normal order lifecycle.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS manufacturing_request_id uuid REFERENCES public.manufacturing_requests(id) ON DELETE CASCADE;
CREATE UNIQUE INDEX IF NOT EXISTS orders_manufacturing_request_unique ON public.orders(manufacturing_request_id);
ALTER TABLE public.orders ALTER COLUMN product_id DROP NOT NULL;
ALTER TABLE public.orders DROP CONSTRAINT IF EXISTS orders_source_check;
ALTER TABLE public.orders ADD CONSTRAINT orders_source_check CHECK ((product_id IS NOT NULL AND manufacturing_request_id IS NULL) OR (product_id IS NULL AND manufacturing_request_id IS NOT NULL));

CREATE OR REPLACE FUNCTION public.manufacturing_create_assigned_order()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE supplier_user uuid;
BEGIN
 IF NEW.state != 'assigned' THEN RETURN NEW; END IF;
 SELECT w.user_id INTO supplier_user FROM public.wholesalers w
 JOIN public.manufacturing_quotes q ON q.wholesaler_id=w.id
 WHERE w.id=NEW.assigned_wholesaler_id AND q.id=NEW.accepted_quote_id AND q.request_id=NEW.id;
 IF supplier_user IS NULL THEN RAISE EXCEPTION 'Assigned request must have a matching supplier quote'; END IF;
 INSERT INTO public.orders(manufacturing_request_id,product_id,retailer_id,wholesaler_id,status,customization_note,created_at,accepted_at,placed_by_user_id,placed_by_role)
 VALUES(NEW.id,NULL,NEW.retailer_id,supplier_user,'accepted',NEW.notes,COALESCE(NEW.assigned_at,clock_timestamp()),COALESCE(NEW.assigned_at,clock_timestamp()),NEW.created_by_user_id,'retailer')
 ON CONFLICT(manufacturing_request_id) DO NOTHING;
 RETURN NEW;
END; $$;
REVOKE ALL ON FUNCTION public.manufacturing_create_assigned_order() FROM PUBLIC,anon,authenticated;
DROP TRIGGER IF EXISTS manufacturing_assigned_order ON public.manufacturing_requests;
CREATE TRIGGER manufacturing_assigned_order AFTER INSERT OR UPDATE OF state,assigned_wholesaler_id,accepted_quote_id ON public.manufacturing_requests
 FOR EACH ROW WHEN (NEW.state='assigned') EXECUTE FUNCTION public.manufacturing_create_assigned_order();

-- Bring already assigned requests into Orders without re-awarding or notifying suppliers.
INSERT INTO public.orders(manufacturing_request_id,product_id,retailer_id,wholesaler_id,status,customization_note,created_at,accepted_at,placed_by_user_id,placed_by_role)
 SELECT r.id,NULL,r.retailer_id,w.user_id,'accepted',r.notes,COALESCE(r.assigned_at,r.updated_at),COALESCE(r.assigned_at,r.updated_at),r.created_by_user_id,'retailer'
 FROM public.manufacturing_requests r JOIN public.wholesalers w ON w.id=r.assigned_wholesaler_id
 JOIN public.manufacturing_quotes q ON q.id=r.accepted_quote_id AND q.request_id=r.id AND q.wholesaler_id=w.id
 WHERE r.state='assigned' ON CONFLICT(manufacturing_request_id) DO NOTHING;

CREATE OR REPLACE FUNCTION public.wholesaler_orders()
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=public,pg_temp AS $$
 SELECT COALESCE(jsonb_agg(row ORDER BY (row->>'created_at') DESC),'[]'::jsonb) FROM (
  SELECT to_jsonb(o) || jsonb_build_object(
   'product_title',COALESCE(p.title,'Custom '||m.category||' order'),
   'product_image',COALESCE(p.processed_image_url,p.image_url,p.raw_image_url),
   'product_type',COALESCE(p.jewellery_type,m.category),
   'manufacturing_offer_id',q.offer_id,
   'store_name',COALESCE(NULLIF(r.business_name,''),r.full_name),
   'store_city',NULLIF(concat_ws(', ',NULLIF(r.city,''),NULLIF(r.state,'')),''),
   'placed_by',CASE WHEN o.employee_id IS NOT NULL THEN e.full_name ELSE r.full_name END,
   'placed_by_staff',o.employee_id IS NOT NULL) AS row
  FROM public.orders o LEFT JOIN public.products p ON p.id=o.product_id
  LEFT JOIN public.retailers r ON r.id=o.retailer_id LEFT JOIN public.employees e ON e.id=o.employee_id
  LEFT JOIN public.manufacturing_requests m ON m.id=o.manufacturing_request_id
  LEFT JOIN public.manufacturing_quotes q ON q.id=m.accepted_quote_id
  WHERE o.wholesaler_id=auth.uid() AND COALESCE(o.is_visible_to_wholesaler,true)
 ) rows;
$$;
REVOKE ALL ON FUNCTION public.wholesaler_orders() FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION public.wholesaler_orders() TO authenticated;
COMMIT;
