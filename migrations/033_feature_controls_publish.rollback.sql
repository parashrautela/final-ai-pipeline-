-- Rollback 033: Enforcement Trigger for Publish to Retailer
DROP TRIGGER IF EXISTS trg_products_publish_feature_controls ON public.products;
DROP FUNCTION IF EXISTS public.check_publish_feature_controls();
