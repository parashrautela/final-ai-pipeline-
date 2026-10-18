-- Rollback 032: Enforcement Trigger for Chamak and Set Creation
DROP TRIGGER IF EXISTS trg_chamak_feature_controls ON public.chamak_generations;
DROP FUNCTION IF EXISTS public.check_chamak_feature_controls();
