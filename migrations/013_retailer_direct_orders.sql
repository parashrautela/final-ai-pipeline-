-- ============================================================================
-- Migration 013 — retailer admins can place orders directly
-- ============================================================================
-- Existing orders require employee_id. The product-first retailer marketplace
-- needs a retailer admin to submit an order without manufacturing a fake
-- employee. Existing employee orders remain unchanged.
-- ============================================================================

BEGIN;

ALTER TABLE public.orders
    ALTER COLUMN employee_id DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS placed_by_user_id UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS placed_by_role TEXT;

-- Preserve actor information for every existing employee order.
UPDATE public.orders AS o
   SET placed_by_user_id = e.auth_user_id,
       placed_by_role = 'employee'
  FROM public.employees AS e
 WHERE o.employee_id = e.id
   AND o.placed_by_user_id IS NULL;

ALTER TABLE public.orders
    DROP CONSTRAINT IF EXISTS orders_placed_by_role_check;
ALTER TABLE public.orders
    ADD CONSTRAINT orders_placed_by_role_check
    CHECK (placed_by_role IS NULL OR placed_by_role IN ('employee', 'retailer'));

ALTER TABLE public.orders
    DROP CONSTRAINT IF EXISTS orders_actor_shape_check;
ALTER TABLE public.orders
    ADD CONSTRAINT orders_actor_shape_check
    CHECK (
        placed_by_role IS NULL
        OR (placed_by_role = 'employee' AND employee_id IS NOT NULL)
        OR (placed_by_role = 'retailer' AND employee_id IS NULL)
    );

CREATE INDEX IF NOT EXISTS idx_orders_placed_by_user
    ON public.orders (placed_by_user_id, created_at DESC);

-- Retailer admins can read orders belonging to their own verified store. All
-- writes still go through the service-role API.
DROP POLICY IF EXISTS "retailers_own_orders" ON public.orders;
CREATE POLICY "retailers_own_orders" ON public.orders
    FOR SELECT TO authenticated
    USING (
        retailer_id IN (
            SELECT id FROM public.retailers WHERE user_id = auth.uid()
        )
    );

COMMIT;
