-- Migration 033: Enforcement Trigger for Publish to Retailer
-- Trigger BEFORE INSERT OR UPDATE OF is_published ON public.products

CREATE OR REPLACE FUNCTION public.check_publish_feature_controls()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  -- Only enforce when publishing (is_published is true and was either inserted or was previously not true)
  IF NEW.is_published = true AND (TG_OP = 'INSERT' OR OLD.is_published IS DISTINCT FROM true) THEN
    PERFORM public.assert_feature(NEW.wholesaler_id, 'publish_to_retailer');
  END IF;
  RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.check_publish_feature_controls() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.check_publish_feature_controls() TO service_role;

DROP TRIGGER IF EXISTS trg_products_publish_feature_controls ON public.products;
CREATE TRIGGER trg_products_publish_feature_controls
  BEFORE INSERT OR UPDATE OF is_published ON public.products
  FOR EACH ROW
  EXECUTE FUNCTION public.check_publish_feature_controls();
