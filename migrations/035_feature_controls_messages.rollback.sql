-- Rollback for Migration 035: Enforcement Triggers for Messages

DROP TRIGGER IF EXISTS trg_messages_feature_controls ON public.messages;
DROP FUNCTION IF EXISTS public.check_messages_feature_controls();

DROP TRIGGER IF EXISTS trg_conversations_feature_controls ON public.conversations;
DROP FUNCTION IF EXISTS public.check_conversations_feature_controls();
