-- Migration 034: Enforcement Trigger for Orders
-- Trigger BEFORE INSERT ON public.orders
-- Prevents placing new orders for wholesalers whose 'orders' feature is blocked.

CREATE OR REPLACE FUNCTION public.check_orders_feature_controls()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  PERFORM public.assert_feature(NEW.wholesaler_id, 'orders');
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
