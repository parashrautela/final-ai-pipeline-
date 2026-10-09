-- Migration: 020_marketplace_pagination_and_performance.sql
-- Optimizes keyset pagination, category filtering, and retailer selections lookup.

BEGIN;

-- 1. Index on published products ordered by created_at DESC, id DESC (keyset pagination)
CREATE INDEX IF NOT EXISTS idx_products_published_keyset 
  ON public.products (created_at DESC, id DESC) 
  WHERE (is_published = true);

-- 2. Index on published products with jewellery_type for category filtering
CREATE INDEX IF NOT EXISTS idx_products_published_category_keyset 
  ON public.products (jewellery_type, created_at DESC, id DESC) 
  WHERE (is_published = true);

-- 3. Composite index on retailer_selections for fast per-page selection status checks
CREATE INDEX IF NOT EXISTS idx_retailer_selections_retailer_product 
  ON public.retailer_selections (retailer_id, product_id);

COMMIT;
