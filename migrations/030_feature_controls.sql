-- Migration 030: Per-Wholesaler Feature Controls Foundation
-- Idempotent schema, catalog seeds, RLS, functions, and audit logging.

CREATE TABLE IF NOT EXISTS public.feature_catalog (
  key text PRIMARY KEY,
  label text NOT NULL,
  description text NOT NULL,
  parent_key text NULL REFERENCES public.feature_catalog(key) ON DELETE SET NULL,
  default_enabled boolean NOT NULL DEFAULT true,
  enforcement_mode text NOT NULL DEFAULT 'off' CHECK (enforcement_mode IN ('off', 'log', 'enforce')),
  locked_message text NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Idempotently ensure locked_message column exists
ALTER TABLE public.feature_catalog ADD COLUMN IF NOT EXISTS locked_message text NULL;

-- Seed seven core features with ON CONFLICT DO NOTHING to preserve admin edits
INSERT INTO public.feature_catalog (key, label, description, parent_key, default_enabled, enforcement_mode, locked_message)
VALUES
  ('chamak', 'Chamak', 'All Chamak generation (Fusion and Set Creation)', NULL, true, 'off', 'Chamak has been locked for your account by Jewel India admin. Contact support to unlock it.'),
  ('set_creation', 'Set Creation', 'Set Creation only (child of chamak)', 'chamak', true, 'off', 'Set Creation has been locked for your account by Jewel India admin. Contact support to unlock it.'),
  ('publish_to_retailer', 'Publish to Retailer', 'Publish products to retailers (products.is_published)', NULL, true, 'off', 'Publishing has been locked for your account by Jewel India admin. Contact support to unlock it.'),
  ('publish_to_wholesaler', 'Publish to Wholesaler', 'Publish to wholesalers (catalog row only, not yet enforced)', NULL, true, 'off', 'This feature has been locked for your account by Jewel India admin. Contact support to unlock it.'),
  ('custom_inquiries', 'Custom Inquiries', 'Wholesaler side of manufacturing requests and offers', NULL, true, 'off', 'Custom inquiries have been locked for your account by Jewel India admin. Contact support to unlock it.'),
  ('orders', 'Orders', 'New orders placed with this wholesaler', NULL, true, 'off', 'Orders have been locked for your account by Jewel India admin. Contact support to unlock it.'),
  ('messages', 'Messages', 'New chat conversations and messages with this wholesaler', NULL, true, 'off', 'Messaging has been locked for your account by Jewel India admin. Contact support to unlock it.')
ON CONFLICT (key) DO NOTHING;

-- Seed locked_message for any pre-existing rows where locked_message IS NULL (preserves admin edits)
UPDATE public.feature_catalog SET locked_message = 'Chamak has been locked for your account by Jewel India admin. Contact support to unlock it.' WHERE key = 'chamak' AND locked_message IS NULL;
UPDATE public.feature_catalog SET locked_message = 'Set Creation has been locked for your account by Jewel India admin. Contact support to unlock it.' WHERE key = 'set_creation' AND locked_message IS NULL;
UPDATE public.feature_catalog SET locked_message = 'Publishing has been locked for your account by Jewel India admin. Contact support to unlock it.' WHERE key = 'publish_to_retailer' AND locked_message IS NULL;
UPDATE public.feature_catalog SET locked_message = 'Custom inquiries have been locked for your account by Jewel India admin. Contact support to unlock it.' WHERE key = 'custom_inquiries' AND locked_message IS NULL;
UPDATE public.feature_catalog SET locked_message = 'Orders have been locked for your account by Jewel India admin. Contact support to unlock it.' WHERE key = 'orders' AND locked_message IS NULL;
UPDATE public.feature_catalog SET locked_message = 'Messaging has been locked for your account by Jewel India admin. Contact support to unlock it.' WHERE key = 'messages' AND locked_message IS NULL;
UPDATE public.feature_catalog SET locked_message = 'This feature has been locked for your account by Jewel India admin. Contact support to unlock it.' WHERE key = 'publish_to_wholesaler' AND locked_message IS NULL;

CREATE TABLE IF NOT EXISTS public.wholesaler_feature_overrides (
  wholesaler_user_id uuid NOT NULL,
  feature_key text NOT NULL REFERENCES public.feature_catalog(key) ON DELETE CASCADE,
  enabled boolean NOT NULL,
  reason text NOT NULL CHECK (length(btrim(reason)) >= 1 AND length(reason) <= 500),
  updated_by text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  version int NOT NULL DEFAULT 1,
  PRIMARY KEY (wholesaler_user_id, feature_key)
);

CREATE TABLE IF NOT EXISTS public.feature_flag_audit (
  id bigserial PRIMARY KEY,
  wholesaler_user_id uuid NULL,
  feature_key text NOT NULL,
  action text NOT NULL CHECK (action IN ('set', 'reset', 'catalog_default', 'catalog_mode')),
  old_value jsonb NULL,
  new_value jsonb NULL,
  reason text NOT NULL,
  actor text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Row Level Security
ALTER TABLE public.feature_catalog ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.wholesaler_feature_overrides ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.feature_flag_audit ENABLE ROW LEVEL SECURITY;

-- Revoke all table privileges from PUBLIC, anon, authenticated
REVOKE ALL ON TABLE public.feature_catalog FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE public.wholesaler_feature_overrides FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE public.feature_flag_audit FROM PUBLIC, anon, authenticated;

-- Service role grants (audit table is strictly append-only: NO UPDATE, DELETE, TRUNCATE)
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.feature_catalog TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.wholesaler_feature_overrides TO service_role;
GRANT SELECT, INSERT ON TABLE public.feature_flag_audit TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.feature_flag_audit_id_seq TO service_role;

-- 1. feature_owner: returns p_user directly; no employees branch, no exception swallow
CREATE OR REPLACE FUNCTION public.feature_owner(p_user uuid)
RETURNS uuid
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  RETURN p_user;
END;
$$;

-- 2. feature_enabled: returns configured state (ignores enforcement_mode)
CREATE OR REPLACE FUNCTION public.feature_enabled(p_user uuid, p_key text)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_owner uuid;
  v_parent_key text;
  v_default_enabled boolean;
  v_override_enabled boolean;
BEGIN
  IF p_user IS NULL OR p_key IS NULL THEN
    RETURN true;
  END IF;

  v_owner := public.feature_owner(p_user);

  SELECT parent_key, default_enabled
  INTO v_parent_key, v_default_enabled
  FROM public.feature_catalog
  WHERE key = p_key;

  IF NOT FOUND THEN
    RAISE WARNING 'Unknown feature key: %', p_key;
    RETURN true;
  END IF;

  -- Hierarchy rule: parent disabled forces child disabled
  IF v_parent_key IS NOT NULL THEN
    IF NOT public.feature_enabled(p_user, v_parent_key) THEN
      RETURN false;
    END IF;
  END IF;

  -- Override check
  SELECT enabled INTO v_override_enabled
  FROM public.wholesaler_feature_overrides
  WHERE wholesaler_user_id = v_owner AND feature_key = p_key;

  IF FOUND THEN
    RETURN v_override_enabled;
  END IF;

  RETURN v_default_enabled;
END;
$$;

-- 3. feature_blocked: returns whether feature is blocked for enforcement
CREATE OR REPLACE FUNCTION public.feature_blocked(p_user uuid, p_key text)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_bypass text;
  v_enabled boolean;
  v_mode text;
BEGIN
  IF p_user IS NULL OR p_key IS NULL THEN
    RETURN false;
  END IF;

  -- Transaction-local bypass check
  v_bypass := current_setting('jewelindia.feature_bypass', true);
  IF v_bypass = 'on' THEN
    RETURN false;
  END IF;

  v_enabled := public.feature_enabled(p_user, p_key);
  IF v_enabled THEN
    RETURN false;
  END IF;

  SELECT enforcement_mode INTO v_mode
  FROM public.feature_catalog
  WHERE key = p_key;

  IF NOT FOUND OR v_mode = 'off' THEN
    RETURN false;
  ELSIF v_mode = 'log' THEN
    RAISE LOG 'FEATURE_BLOCKED_LOG: user=%, feature=%', p_user, p_key;
    RETURN false;
  ELSIF v_mode = 'enforce' THEN
    RETURN true;
  END IF;

  RETURN false;
END;
$$;

-- Drop old 2-argument assert_feature to prevent ambiguity
DROP FUNCTION IF EXISTS public.assert_feature(uuid, text);

-- 4. assert_feature: raises PT403 if blocked with human message per audience
CREATE OR REPLACE FUNCTION public.assert_feature(
  p_user uuid,
  p_key text,
  p_audience text DEFAULT 'wholesaler'
)
RETURNS void
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_bypass text;
  v_mode text;
  v_locked_msg text;
  v_message text;
BEGIN
  IF p_user IS NULL OR p_key IS NULL THEN
    RETURN;
  END IF;

  -- Transaction-local bypass check
  v_bypass := current_setting('jewelindia.feature_bypass', true);
  IF v_bypass = 'on' THEN
    RETURN;
  END IF;

  SELECT enforcement_mode, locked_message
  INTO v_mode, v_locked_msg
  FROM public.feature_catalog
  WHERE key = p_key;

  IF NOT FOUND OR v_mode = 'off' THEN
    RETURN;
  END IF;

  -- Check configured state (parent hierarchy and overrides)
  IF NOT public.feature_enabled(p_user, p_key) THEN
    IF v_mode = 'log' THEN
      RAISE LOG 'FEATURE_BLOCKED_LOG: user=%, feature=%', p_user, p_key;
      RETURN;
    ELSIF v_mode = 'enforce' THEN
      IF p_audience = 'retailer' THEN
        IF p_key = 'orders' THEN
          v_message := 'This supplier is not accepting orders right now.';
        ELSIF p_key = 'messages' THEN
          v_message := 'Messaging is not available for this supplier right now.';
        ELSE
          v_message := 'This is not available for this supplier right now.';
        END IF;
      ELSE
        v_message := COALESCE(v_locked_msg, 'This feature has been locked for your account by Jewel India admin. Contact support to unlock it.');
      END IF;

      RAISE EXCEPTION '%', v_message
        USING ERRCODE = 'PT403',
              DETAIL = p_key,
              HINT = 'FEATURE_DISABLED';
    END IF;
  END IF;
END;
$$;

-- 5. feature_check: returns atomic {blocked, message} for FastAPI and client caching
CREATE OR REPLACE FUNCTION public.feature_check(p_user uuid, p_key text)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_blocked boolean;
  v_message text;
BEGIN
  v_blocked := public.feature_blocked(p_user, p_key);
  SELECT locked_message INTO v_message
  FROM public.feature_catalog
  WHERE key = p_key;

  IF v_message IS NULL THEN
    v_message := 'This feature has been locked for your account by Jewel India admin. Contact support to unlock it.';
  END IF;

  RETURN jsonb_build_object('blocked', v_blocked, 'message', v_message);
END;
$$;

-- 6. my_features: returns exactly { "<key>": { "available": <bool>, "message": <locked_message or null> }, ... }
CREATE OR REPLACE FUNCTION public.my_features()
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_user uuid;
  v_owner uuid;
  v_result jsonb := '{}'::jsonb;
  v_rec record;
  v_available boolean;
  v_msg text;
BEGIN
  v_user := auth.uid();
  IF v_user IS NULL THEN
    RETURN '{}'::jsonb;
  END IF;

  v_owner := public.feature_owner(v_user);

  FOR v_rec IN
    SELECT key, enforcement_mode, locked_message
    FROM public.feature_catalog
    ORDER BY key
  LOOP
    -- available = NOT blocked evaluated without RAISE LOG side effect
    IF v_rec.enforcement_mode = 'enforce' AND NOT public.feature_enabled(v_owner, v_rec.key) THEN
      v_available := false;
      v_msg := COALESCE(v_rec.locked_message, 'This feature has been locked for your account by Jewel India admin. Contact support to unlock it.');
    ELSE
      v_available := true;
      v_msg := NULL;
    END IF;

    v_result := jsonb_set(
      v_result,
      ARRAY[v_rec.key],
      jsonb_build_object('available', v_available, 'message', v_msg)
    );
  END LOOP;

  RETURN v_result;
END;
$$;

-- 7. admin_set_feature
CREATE OR REPLACE FUNCTION public.admin_set_feature(
  p_wholesaler_user_id uuid,
  p_key text,
  p_enabled boolean,
  p_reason text,
  p_actor text,
  p_expected_version int DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_old_enabled boolean;
  v_current_version int;
  v_new_version int;
  v_old_json jsonb;
BEGIN
  IF p_reason IS NULL OR length(btrim(p_reason)) < 1 OR length(p_reason) > 500 THEN
    RETURN jsonb_build_object('ok', false, 'error', 'INVALID_REASON');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM public.feature_catalog WHERE key = p_key) THEN
    RETURN jsonb_build_object('ok', false, 'error', 'UNKNOWN_FEATURE');
  END IF;

  IF to_regclass('public.wholesalers') IS NOT NULL THEN
    IF NOT EXISTS (SELECT 1 FROM public.wholesalers WHERE user_id = p_wholesaler_user_id) THEN
      RETURN jsonb_build_object('ok', false, 'error', 'NOT_A_WHOLESALER');
    END IF;
  END IF;

  SELECT enabled, version INTO v_old_enabled, v_current_version
  FROM public.wholesaler_feature_overrides
  WHERE wholesaler_user_id = p_wholesaler_user_id AND feature_key = p_key
  FOR UPDATE;

  IF FOUND THEN
    IF p_expected_version IS NOT NULL AND p_expected_version <> v_current_version THEN
      RETURN jsonb_build_object('ok', false, 'error', 'VERSION_CONFLICT', 'current_version', v_current_version);
    END IF;
    v_new_version := v_current_version + 1;
    UPDATE public.wholesaler_feature_overrides
    SET enabled = p_enabled,
        reason = btrim(p_reason),
        updated_by = coalesce(p_actor, 'admin'),
        updated_at = now(),
        version = v_new_version
    WHERE wholesaler_user_id = p_wholesaler_user_id AND feature_key = p_key;

    v_old_json := jsonb_build_object('enabled', v_old_enabled, 'version', v_current_version);
  ELSE
    IF p_expected_version IS NOT NULL AND p_expected_version <> 0 THEN
      RETURN jsonb_build_object('ok', false, 'error', 'VERSION_CONFLICT', 'current_version', 0);
    END IF;
    v_new_version := 1;
    INSERT INTO public.wholesaler_feature_overrides
      (wholesaler_user_id, feature_key, enabled, reason, updated_by, updated_at, version)
    VALUES
      (p_wholesaler_user_id, p_key, p_enabled, btrim(p_reason), coalesce(p_actor, 'admin'), now(), 1);

    v_old_json := NULL;
  END IF;

  INSERT INTO public.feature_flag_audit
    (wholesaler_user_id, feature_key, action, old_value, new_value, reason, actor, created_at)
  VALUES
    (p_wholesaler_user_id, p_key, 'set', v_old_json, jsonb_build_object('enabled', p_enabled, 'version', v_new_version), btrim(p_reason), coalesce(p_actor, 'admin'), now());

  RETURN jsonb_build_object('ok', true, 'version', v_new_version);
END;
$$;

-- 8. admin_reset_feature
CREATE OR REPLACE FUNCTION public.admin_reset_feature(
  p_wholesaler_user_id uuid,
  p_key text,
  p_reason text,
  p_actor text,
  p_expected_version int DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_old_enabled boolean;
  v_current_version int;
BEGIN
  IF p_reason IS NULL OR length(btrim(p_reason)) < 1 OR length(p_reason) > 500 THEN
    RETURN jsonb_build_object('ok', false, 'error', 'INVALID_REASON');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM public.feature_catalog WHERE key = p_key) THEN
    RETURN jsonb_build_object('ok', false, 'error', 'UNKNOWN_FEATURE');
  END IF;

  IF to_regclass('public.wholesalers') IS NOT NULL THEN
    IF NOT EXISTS (SELECT 1 FROM public.wholesalers WHERE user_id = p_wholesaler_user_id) THEN
      RETURN jsonb_build_object('ok', false, 'error', 'NOT_A_WHOLESALER');
    END IF;
  END IF;

  SELECT enabled, version INTO v_old_enabled, v_current_version
  FROM public.wholesaler_feature_overrides
  WHERE wholesaler_user_id = p_wholesaler_user_id AND feature_key = p_key
  FOR UPDATE;

  IF NOT FOUND THEN
    IF p_expected_version IS NOT NULL AND p_expected_version <> 0 THEN
      RETURN jsonb_build_object('ok', false, 'error', 'VERSION_CONFLICT', 'current_version', 0);
    END IF;
    RETURN jsonb_build_object('ok', true, 'version', 0);
  END IF;

  IF p_expected_version IS NOT NULL AND p_expected_version <> v_current_version THEN
    RETURN jsonb_build_object('ok', false, 'error', 'VERSION_CONFLICT', 'current_version', v_current_version);
  END IF;

  DELETE FROM public.wholesaler_feature_overrides
  WHERE wholesaler_user_id = p_wholesaler_user_id AND feature_key = p_key;

  INSERT INTO public.feature_flag_audit
    (wholesaler_user_id, feature_key, action, old_value, new_value, reason, actor, created_at)
  VALUES
    (p_wholesaler_user_id, p_key, 'reset', jsonb_build_object('enabled', v_old_enabled, 'version', v_current_version), NULL, btrim(p_reason), coalesce(p_actor, 'admin'), now());

  RETURN jsonb_build_object('ok', true, 'version', 0);
END;
$$;

-- 9. admin_bulk_set_features: atomic multi-feature update in one transaction
CREATE OR REPLACE FUNCTION public.admin_bulk_set_features(
  p_wholesaler_user_id uuid,
  p_changes jsonb,
  p_reason text,
  p_actor text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_item jsonb;
  v_key text;
  v_enabled boolean;
  v_expected_ver int;
  v_old_enabled boolean;
  v_curr_ver int;
  v_new_ver int;
  v_old_json jsonb;
  v_count int := 0;
BEGIN
  IF p_reason IS NULL OR length(btrim(p_reason)) < 1 OR length(p_reason) > 500 THEN
    RETURN jsonb_build_object('ok', false, 'error', 'INVALID_REASON');
  END IF;

  IF p_changes IS NULL OR jsonb_typeof(p_changes) <> 'array' OR jsonb_array_length(p_changes) = 0 THEN
    RETURN jsonb_build_object('ok', false, 'error', 'INVALID_CHANGES');
  END IF;

  IF to_regclass('public.wholesalers') IS NOT NULL THEN
    IF NOT EXISTS (SELECT 1 FROM public.wholesalers WHERE user_id = p_wholesaler_user_id) THEN
      RETURN jsonb_build_object('ok', false, 'error', 'NOT_A_WHOLESALER');
    END IF;
  END IF;

  -- Pass 1: Validate all feature keys and versions under lock
  FOR v_item IN SELECT * FROM jsonb_array_elements(p_changes) LOOP
    v_key := COALESCE(v_item->>'feature_key', v_item->>'key');
    IF v_key IS NULL OR NOT EXISTS (SELECT 1 FROM public.feature_catalog WHERE key = v_key) THEN
      RETURN jsonb_build_object('ok', false, 'error', 'UNKNOWN_FEATURE', 'feature_key', v_key);
    END IF;

    IF v_item ? 'expected_version' AND v_item->>'expected_version' IS NOT NULL THEN
      v_expected_ver := (v_item->>'expected_version')::int;
      SELECT version INTO v_curr_ver
      FROM public.wholesaler_feature_overrides
      WHERE wholesaler_user_id = p_wholesaler_user_id AND feature_key = v_key
      FOR UPDATE;

      IF FOUND THEN
        IF v_expected_ver <> v_curr_ver THEN
          RETURN jsonb_build_object('ok', false, 'error', 'VERSION_CONFLICT', 'feature_key', v_key, 'current_version', v_curr_ver);
        END IF;
      ELSE
        IF v_expected_ver <> 0 THEN
          RETURN jsonb_build_object('ok', false, 'error', 'VERSION_CONFLICT', 'feature_key', v_key, 'current_version', 0);
        END IF;
      END IF;
    END IF;
  END LOOP;

  -- Pass 2: Apply all changes and record audit entries
  FOR v_item IN SELECT * FROM jsonb_array_elements(p_changes) LOOP
    v_key := COALESCE(v_item->>'feature_key', v_item->>'key');
    v_enabled := (v_item->>'enabled')::boolean;

    SELECT enabled, version INTO v_old_enabled, v_curr_ver
    FROM public.wholesaler_feature_overrides
    WHERE wholesaler_user_id = p_wholesaler_user_id AND feature_key = v_key
    FOR UPDATE;

    IF FOUND THEN
      v_new_ver := v_curr_ver + 1;
      UPDATE public.wholesaler_feature_overrides
      SET enabled = v_enabled,
          reason = btrim(p_reason),
          updated_by = coalesce(p_actor, 'admin'),
          updated_at = now(),
          version = v_new_ver
      WHERE wholesaler_user_id = p_wholesaler_user_id AND feature_key = v_key;

      v_old_json := jsonb_build_object('enabled', v_old_enabled, 'version', v_curr_ver);
    ELSE
      v_new_ver := 1;
      INSERT INTO public.wholesaler_feature_overrides
        (wholesaler_user_id, feature_key, enabled, reason, updated_by, updated_at, version)
      VALUES
        (p_wholesaler_user_id, v_key, v_enabled, btrim(p_reason), coalesce(p_actor, 'admin'), now(), 1);

      v_old_json := NULL;
    END IF;

    INSERT INTO public.feature_flag_audit
      (wholesaler_user_id, feature_key, action, old_value, new_value, reason, actor, created_at)
    VALUES
      (p_wholesaler_user_id, v_key, 'set', v_old_json, jsonb_build_object('enabled', v_enabled, 'version', v_new_ver), btrim(p_reason), coalesce(p_actor, 'admin'), now());

    v_count := v_count + 1;
  END LOOP;

  RETURN jsonb_build_object('ok', true, 'count', v_count);
END;
$$;

-- 10. admin_set_catalog
CREATE OR REPLACE FUNCTION public.admin_set_catalog(
  p_key text,
  p_default_enabled boolean,
  p_mode text,
  p_reason text,
  p_actor text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_old_default boolean;
  v_old_mode text;
  v_new_default boolean;
  v_new_mode text;
BEGIN
  IF p_reason IS NULL OR length(btrim(p_reason)) < 1 OR length(p_reason) > 500 THEN
    RETURN jsonb_build_object('ok', false, 'error', 'INVALID_REASON');
  END IF;

  IF p_mode IS NOT NULL AND p_mode NOT IN ('off', 'log', 'enforce') THEN
    RETURN jsonb_build_object('ok', false, 'error', 'INVALID_MODE');
  END IF;

  SELECT default_enabled, enforcement_mode INTO v_old_default, v_old_mode
  FROM public.feature_catalog
  WHERE key = p_key
  FOR UPDATE;

  IF NOT FOUND THEN
    RETURN jsonb_build_object('ok', false, 'error', 'UNKNOWN_FEATURE');
  END IF;

  v_new_default := coalesce(p_default_enabled, v_old_default);
  v_new_mode := coalesce(p_mode, v_old_mode);

  IF v_new_default IS DISTINCT FROM v_old_default OR v_new_mode IS DISTINCT FROM v_old_mode THEN
    UPDATE public.feature_catalog
    SET default_enabled = v_new_default,
        enforcement_mode = v_new_mode,
        updated_at = now()
    WHERE key = p_key;

    INSERT INTO public.feature_flag_audit
      (wholesaler_user_id, feature_key, action, old_value, new_value, reason, actor, created_at)
    VALUES
      (NULL, p_key,
       CASE WHEN v_new_default IS DISTINCT FROM v_old_default THEN 'catalog_default' ELSE 'catalog_mode' END,
       jsonb_build_object('default_enabled', v_old_default, 'enforcement_mode', v_old_mode),
       jsonb_build_object('default_enabled', v_new_default, 'enforcement_mode', v_new_mode),
       btrim(p_reason), coalesce(p_actor, 'admin'), now());
  END IF;

  RETURN jsonb_build_object('ok', true);
END;
$$;

-- 11. admin_list_features: returns full feature status for admin panel
CREATE OR REPLACE FUNCTION public.admin_list_features(p_wholesaler_user_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_features jsonb := '[]'::jsonb;
  v_rec record;
  v_chamak_effective boolean;
BEGIN
  v_chamak_effective := public.feature_enabled(p_wholesaler_user_id, 'chamak');

  FOR v_rec IN
    SELECT
      c.key,
      c.label,
      c.description,
      c.parent_key,
      c.default_enabled,
      c.enforcement_mode,
      c.locked_message,
      o.enabled AS override_enabled,
      o.reason AS override_reason,
      o.updated_by AS override_updated_by,
      o.updated_at AS override_updated_at,
      coalesce(o.version, 0) AS version,
      public.feature_enabled(p_wholesaler_user_id, c.key) AS effective_enabled,
      CASE
        WHEN c.parent_key = 'chamak' AND NOT v_chamak_effective THEN true
        ELSE false
      END AS parent_locked
    FROM public.feature_catalog c
    LEFT JOIN public.wholesaler_feature_overrides o
      ON o.feature_key = c.key AND o.wholesaler_user_id = p_wholesaler_user_id
    ORDER BY c.key
  LOOP
    v_features := v_features || jsonb_build_object(
      'key', v_rec.key,
      'label', v_rec.label,
      'description', v_rec.description,
      'parent_key', v_rec.parent_key,
      'default_enabled', v_rec.default_enabled,
      'enforcement_mode', v_rec.enforcement_mode,
      'locked_message', v_rec.locked_message,
      'override_enabled', v_rec.override_enabled,
      'effective_enabled', v_rec.effective_enabled,
      'reason', v_rec.override_reason,
      'updated_by', v_rec.override_updated_by,
      'updated_at', v_rec.override_updated_at,
      'version', v_rec.version,
      'parent_locked', v_rec.parent_locked
    );
  END LOOP;

  RETURN jsonb_build_object('ok', true, 'features', v_features);
END;
$$;

-- Function execution privileges: REVOKE from public, anon, authenticated; GRANT to service_role
REVOKE ALL ON FUNCTION public.feature_owner(uuid) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.feature_enabled(uuid, text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.feature_blocked(uuid, text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.assert_feature(uuid, text, text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.feature_check(uuid, text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.my_features() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_set_feature(uuid, text, boolean, text, text, int) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_reset_feature(uuid, text, text, text, int) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_bulk_set_features(uuid, jsonb, text, text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_set_catalog(text, boolean, text, text, text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_list_features(uuid) FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.feature_owner(uuid) TO service_role;
GRANT EXECUTE ON FUNCTION public.feature_enabled(uuid, text) TO service_role;
GRANT EXECUTE ON FUNCTION public.feature_blocked(uuid, text) TO service_role;
GRANT EXECUTE ON FUNCTION public.assert_feature(uuid, text, text) TO service_role;
GRANT EXECUTE ON FUNCTION public.feature_check(uuid, text) TO service_role;
GRANT EXECUTE ON FUNCTION public.admin_set_feature(uuid, text, boolean, text, text, int) TO service_role;
GRANT EXECUTE ON FUNCTION public.admin_reset_feature(uuid, text, text, text, int) TO service_role;
GRANT EXECUTE ON FUNCTION public.admin_bulk_set_features(uuid, jsonb, text, text) TO service_role;
GRANT EXECUTE ON FUNCTION public.admin_set_catalog(text, boolean, text, text, text) TO service_role;
GRANT EXECUTE ON FUNCTION public.admin_list_features(uuid) TO service_role;

-- my_features: authenticated users can query their own features
GRANT EXECUTE ON FUNCTION public.my_features() TO authenticated, service_role;
