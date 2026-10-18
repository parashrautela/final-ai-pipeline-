-- Migration 035: Enforcement Triggers for Messages
-- Triggers BEFORE INSERT ON public.conversations and public.messages
-- Prevents creating new conversations or sending messages when 'messages' feature is blocked.

CREATE OR REPLACE FUNCTION public.check_conversations_feature_controls()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_wholesaler_user uuid;
BEGIN
  SELECT COALESCE(w.user_id, NEW.wholesaler_id)
    INTO v_wholesaler_user
    FROM (SELECT NEW.wholesaler_id AS id) src
    LEFT JOIN public.wholesalers w ON w.id = src.id
   LIMIT 1;

  PERFORM public.assert_feature(COALESCE(v_wholesaler_user, NEW.wholesaler_id), 'messages');
  RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.check_conversations_feature_controls() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.check_conversations_feature_controls() TO service_role;

DROP TRIGGER IF EXISTS trg_conversations_feature_controls ON public.conversations;
CREATE TRIGGER trg_conversations_feature_controls
  BEFORE INSERT ON public.conversations
  FOR EACH ROW
  EXECUTE FUNCTION public.check_conversations_feature_controls();


CREATE OR REPLACE FUNCTION public.check_messages_feature_controls()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_conv_wholesaler uuid;
  v_wholesaler_user uuid;
BEGIN
  SELECT c.wholesaler_id
    INTO v_conv_wholesaler
    FROM public.conversations c
   WHERE c.id = NEW.conversation_id;

  IF v_conv_wholesaler IS NOT NULL THEN
    SELECT COALESCE(w.user_id, v_conv_wholesaler)
      INTO v_wholesaler_user
      FROM (SELECT v_conv_wholesaler AS id) src
      LEFT JOIN public.wholesalers w ON w.id = src.id
     LIMIT 1;

    PERFORM public.assert_feature(COALESCE(v_wholesaler_user, v_conv_wholesaler), 'messages');
  END IF;

  RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.check_messages_feature_controls() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.check_messages_feature_controls() TO service_role;

DROP TRIGGER IF EXISTS trg_messages_feature_controls ON public.messages;
CREATE TRIGGER trg_messages_feature_controls
  BEFORE INSERT ON public.messages
  FOR EACH ROW
  EXECUTE FUNCTION public.check_messages_feature_controls();
