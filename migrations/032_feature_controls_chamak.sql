-- Migration 032: Enforcement Trigger for Chamak and Set Creation
-- Trigger BEFORE INSERT ON public.chamak_generations

CREATE OR REPLACE FUNCTION public.check_chamak_feature_controls()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  PERFORM public.assert_feature(
    NEW.wholesaler_id,
    CASE WHEN NEW.mode = 'set_creation' THEN 'set_creation' ELSE 'chamak' END
  );
  RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.check_chamak_feature_controls() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.check_chamak_feature_controls() TO service_role;

DROP TRIGGER IF EXISTS trg_chamak_feature_controls ON public.chamak_generations;
CREATE TRIGGER trg_chamak_feature_controls
  BEFORE INSERT ON public.chamak_generations
  FOR EACH ROW
  EXECUTE FUNCTION public.check_chamak_feature_controls();
