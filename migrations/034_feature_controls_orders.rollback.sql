-- Rollback for Migration 034: Enforcement Trigger for Orders

DROP TRIGGER IF EXISTS trg_orders_feature_controls ON public.orders;
DROP FUNCTION IF EXISTS public.check_orders_feature_controls();
