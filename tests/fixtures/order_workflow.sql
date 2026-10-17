-- Existing production order schema/functions needed by manufacturing integration tests.

              ALTER TABLE retailers ADD COLUMN full_name text, ADD COLUMN city text, ADD COLUMN state text;
              ALTER TABLE employees ADD COLUMN full_name text;
              CREATE TABLE products(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),title text,processed_image_url text,image_url text,raw_image_url text,jewellery_type text);
              CREATE TABLE orders(
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),product_id uuid NOT NULL REFERENCES products(id),
                retailer_id uuid NOT NULL REFERENCES retailers(id),wholesaler_id uuid NOT NULL REFERENCES auth.users(id),
                employee_id uuid REFERENCES employees(id),status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected','in_production','packed','dispatched','received','completed')),
                customization_note text,rejection_reason text,created_at timestamptz DEFAULT now(),updated_at timestamptz DEFAULT now(),
                accepted_at timestamptz,rejected_at timestamptz,production_at timestamptz,packed_at timestamptz,dispatched_at timestamptz,received_at timestamptz,completed_at timestamptz,
                is_visible_to_wholesaler boolean DEFAULT true,placed_by_user_id uuid,placed_by_role text);
              CREATE OR REPLACE FUNCTION public.my_employee_id() RETURNS uuid LANGUAGE sql AS $$ SELECT id FROM employees WHERE auth_user_id=auth.uid() $$;
CREATE OR REPLACE FUNCTION public.order_set_status(p_order UUID, p_status TEXT, p_reason TEXT DEFAULT NULL)
RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'public', 'pg_temp'
AS $$
DECLARE
    v_user   UUID := auth.uid();
    v_order  public.orders%ROWTYPE;
    v_side   TEXT;
    v_ok     BOOLEAN;
    v_reason TEXT := NULLIF(btrim(COALESCE(p_reason, '')), '');
BEGIN
    IF v_user IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error', 'NOT_AUTHENTICATED');
    END IF;

    SELECT * INTO v_order FROM public.orders WHERE id = p_order FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'error', 'NOT_FOUND');
    END IF;

    IF v_order.wholesaler_id = v_user THEN
        v_side := 'wholesaler';
    ELSIF EXISTS (SELECT 1 FROM public.retailers
                   WHERE id = v_order.retailer_id AND user_id = v_user) THEN
        v_side := 'store';
    ELSIF v_order.employee_id IS NOT NULL AND v_order.employee_id = public.my_employee_id() THEN
        v_side := 'store';
    ELSE
        RETURN jsonb_build_object('ok', false, 'error', 'NOT_FOUND');
    END IF;

    v_ok := CASE v_side
        WHEN 'wholesaler' THEN
            (v_order.status = 'pending'       AND p_status IN ('accepted', 'rejected'))
         OR (v_order.status = 'accepted'      AND p_status IN ('in_production', 'packed'))
         OR (v_order.status = 'in_production' AND p_status = 'packed')
         OR (v_order.status = 'packed'        AND p_status = 'dispatched')
        ELSE
            (v_order.status = 'dispatched'    AND p_status = 'received')
         OR (v_order.status = 'received'      AND p_status = 'completed')
    END;

    IF NOT COALESCE(v_ok, false) THEN
        IF v_order.status = p_status THEN
            RETURN jsonb_build_object('ok', true, 'status', v_order.status, 'unchanged', true);
        END IF;
        RETURN jsonb_build_object('ok', false, 'error', 'INVALID_TRANSITION',
                                  'from', v_order.status, 'to', p_status);
    END IF;

    IF p_status = 'rejected' AND (v_reason IS NULL OR length(v_reason) > 500) THEN
        RETURN jsonb_build_object('ok', false, 'error', 'REASON_REQUIRED');
    END IF;

    UPDATE public.orders SET
        status           = p_status,
        updated_at       = now(),
        rejection_reason = CASE WHEN p_status = 'rejected' THEN v_reason ELSE rejection_reason END,
        accepted_at      = CASE WHEN p_status = 'accepted'      THEN now() ELSE accepted_at END,
        rejected_at      = CASE WHEN p_status = 'rejected'      THEN now() ELSE rejected_at END,
        production_at    = CASE WHEN p_status = 'in_production' THEN now() ELSE production_at END,
        packed_at        = CASE WHEN p_status = 'packed'        THEN now() ELSE packed_at END,
        dispatched_at    = CASE WHEN p_status = 'dispatched'    THEN now() ELSE dispatched_at END,
        received_at      = CASE WHEN p_status = 'received'      THEN now() ELSE received_at END,
        completed_at     = CASE WHEN p_status = 'completed'     THEN now() ELSE completed_at END
     WHERE id = p_order;

    RETURN jsonb_build_object('ok', true, 'status', p_status);
END;
$$;

