-- Rollback 030: Per-Wholesaler Feature Controls Foundation

DROP FUNCTION IF EXISTS public.admin_list_features(uuid);
DROP FUNCTION IF EXISTS public.admin_set_catalog(text, boolean, text, text, text);
DROP FUNCTION IF EXISTS public.admin_reset_feature(uuid, text, text, text, int);
DROP FUNCTION IF EXISTS public.admin_set_feature(uuid, text, boolean, text, text, int);
DROP FUNCTION IF EXISTS public.my_features();
DROP FUNCTION IF EXISTS public.assert_feature(uuid, text);
DROP FUNCTION IF EXISTS public.feature_blocked(uuid, text);
DROP FUNCTION IF EXISTS public.feature_enabled(uuid, text);
DROP FUNCTION IF EXISTS public.feature_owner(uuid);

DROP TABLE IF EXISTS public.feature_flag_audit CASCADE;
DROP TABLE IF EXISTS public.wholesaler_feature_overrides CASCADE;
DROP TABLE IF EXISTS public.feature_catalog CASCADE;
