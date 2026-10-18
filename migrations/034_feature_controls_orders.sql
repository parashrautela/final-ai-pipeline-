-- Migration 034: Enforcement Trigger for Orders
-- Trigger BEFORE INSERT ON public.orders
-- Prevents placing new orders for wholesalers whose 'orders' feature is blocked.
-- Skips check when NEW.manufacturing_request_id IS NOT NULL (governed by custom_inquiries).
-- Passes audience 'wholesaler' if caller is the wholesaler, else 'retailer'.

CREATE OR REPLACE FUNCTION public.check_orders_feature_controls()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_audience text;
BEGIN
  -- Awarded manufacturing orders are governed by custom_inquiries, not orders
  IF NEW.manufacturing_request_id IS NOT NULL THEN
    RETURN NEW;
  END IF;

  IF auth.uid() IS NOT NULL AND public.feature_owner(auth.uid()) = NEW.wholesaler_id THEN
    v_audience := 'wholesaler';
  ELSE
    v_audience := 'retailer';
  END IF;

  PERFORM public.assert_feature(NEW.wholesaler_id, 'orders', v_audience);
  RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.check_orders_feature_controls() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.check_orders_feature_controls() TO service_role;

DROP TRIGGER IF EXISTS trg_orders_feature_controls ON public.orders;
CREATE TRIGGER trg_orders_feature_controls
  BEFORE INSERT ON public.orders
  FOR EACH ROW
  EXECUTE FUNCTION public.check_orders_feature_controls();
