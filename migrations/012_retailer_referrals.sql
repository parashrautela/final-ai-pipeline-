-- ============================================================================
-- Migration 012 — retailer acquisition referrals
-- ============================================================================
-- Product rules:
--   * one link can onboard one retailer
--   * a link expires seven days after it is generated
--   * a retailer can be attributed to only one wholesaler
--   * verification by a platform admin grants the inviter 1,000 credits
--
-- `claim_retailer_referral` is the only supported redemption path. It locks
-- both rows so two devices cannot consume the same invitation concurrently.
-- The verification trigger uses grant_credits' idempotency key, which makes
-- repeated admin updates and webhook retries harmless.
-- ============================================================================

BEGIN;

ALTER TABLE public.referral_links
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS opened_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS accepted_by UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS rewarded_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'web',
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Bring legacy links under the new policy. Already-consumed links remain
-- consumed; unused legacy links receive a seven-day window from creation.
UPDATE public.referral_links
   SET expires_at = COALESCE(expires_at, created_at + interval '7 days'),
       max_uses = 1,
       is_active = CASE
           WHEN COALESCE(uses_count, 0) >= 1 THEN false
           WHEN COALESCE(expires_at, created_at + interval '7 days') <= now() THEN false
           ELSE is_active
       END,
       updated_at = now()
 WHERE expires_at IS NULL
    OR max_uses IS DISTINCT FROM 1
    OR (COALESCE(uses_count, 0) >= 1 AND is_active);

ALTER TABLE public.referral_links
    ALTER COLUMN max_uses SET DEFAULT 1,
    ALTER COLUMN expires_at SET DEFAULT (now() + interval '7 days');

CREATE INDEX IF NOT EXISTS idx_referral_links_available
    ON public.referral_links (code, expires_at)
    WHERE is_active = true AND accepted_by IS NULL;

CREATE INDEX IF NOT EXISTS idx_referral_links_accepted_by
    ON public.referral_links (accepted_by)
    WHERE accepted_by IS NOT NULL;


-- Atomically attaches an invitation to the retailer row created by onboarding.
-- The onboarding client records referral_code first; requiring that exact code
-- here prevents an existing account from attaching an invitation later.
CREATE OR REPLACE FUNCTION public.claim_retailer_referral(
    p_code          TEXT,
    p_retailer_user UUID
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_code     TEXT := btrim(p_code);
    v_retailer public.retailers%ROWTYPE;
    v_link     public.referral_links%ROWTYPE;
BEGIN
    IF v_code IS NULL OR v_code = '' OR p_retailer_user IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVALID_ARGUMENT');
    END IF;

    SELECT * INTO v_retailer
      FROM public.retailers
     WHERE user_id = p_retailer_user
     FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'error', 'RETAILER_NOT_FOUND');
    END IF;

    -- A claimed retailer is immutable from the app. Replaying the same claim
    -- succeeds, while every different invitation is rejected.
    IF v_retailer.referred_by IS NOT NULL THEN
        IF v_retailer.referral_code = v_code THEN
            RETURN jsonb_build_object(
                'ok', true,
                'replayed', true,
                'wholesaler_id', v_retailer.referred_by
            );
        END IF;
        RETURN jsonb_build_object('ok', false, 'error', 'RETAILER_ALREADY_ATTRIBUTED');
    END IF;

    IF v_retailer.referral_code IS DISTINCT FROM v_code THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVITATION_NOT_ATTACHED');
    END IF;

    SELECT * INTO v_link
      FROM public.referral_links
     WHERE code = v_code
     FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVITATION_NOT_FOUND');
    END IF;

    IF NOT v_link.is_active THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVITATION_INACTIVE');
    END IF;

    IF v_link.expires_at IS NULL OR v_link.expires_at <= now() THEN
        UPDATE public.referral_links
           SET is_active = false, updated_at = now()
         WHERE id = v_link.id;
        RETURN jsonb_build_object('ok', false, 'error', 'INVITATION_EXPIRED');
    END IF;

    IF v_link.accepted_by IS NOT NULL
       OR COALESCE(v_link.uses_count, 0) >= 1 THEN
        RETURN jsonb_build_object('ok', false, 'error', 'INVITATION_ALREADY_USED');
    END IF;

    UPDATE public.retailers
       SET referred_by = v_link.wholesaler_id,
           referral_code = v_code
     WHERE user_id = p_retailer_user;

    UPDATE public.referral_links
       SET uses_count = 1,
           max_uses = 1,
           is_active = false,
           accepted_at = now(),
           accepted_by = p_retailer_user,
           updated_at = now()
     WHERE id = v_link.id;

    RETURN jsonb_build_object(
        'ok', true,
        'wholesaler_id', v_link.wholesaler_id,
        'expires_at', v_link.expires_at
    );
END;
$$;


-- Reward only when a platform admin changes the retailer to verified. The
-- retailer remains unable to enter the application until that status change;
-- this trigger only handles the resulting inviter reward.
CREATE OR REPLACE FUNCTION public.credits_grant_retailer_referral()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_was_verified   BOOLEAN := false;
    v_wholesaler_uid UUID;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        v_was_verified := (OLD.verification_status = 'verified');
    END IF;

    IF NEW.verification_status = 'verified'
       AND NOT v_was_verified
       AND NEW.referred_by IS NOT NULL THEN
        SELECT user_id INTO v_wholesaler_uid
          FROM public.wholesalers
         WHERE id = NEW.referred_by;

        IF v_wholesaler_uid IS NOT NULL THEN
            PERFORM public.grant_credits(
                p_user            => v_wholesaler_uid,
                p_credits         => 1000,
                p_source          => 'referral',
                p_idempotency_key => 'retailer-referral:' || NEW.id,
                p_expires_at      => NULL,
                p_note            => 'Verified retailer referral reward',
                p_metadata        => jsonb_build_object(
                    'retailer_id', NEW.id,
                    'retailer_user_id', NEW.user_id,
                    'referral_code', NEW.referral_code
                ),
                p_reference_type  => 'retailer_referral',
                p_reference_id    => NEW.id::text
            );

            UPDATE public.referral_links
               SET rewarded_at = COALESCE(rewarded_at, now()),
                   updated_at = now()
             WHERE wholesaler_id = NEW.referred_by
               AND code = NEW.referral_code;
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_credits_retailer_referral ON public.retailers;
CREATE TRIGGER trg_credits_retailer_referral
    AFTER INSERT OR UPDATE OF verification_status ON public.retailers
    FOR EACH ROW EXECUTE FUNCTION public.credits_grant_retailer_referral();

REVOKE ALL ON FUNCTION public.claim_retailer_referral(TEXT, UUID)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_retailer_referral(TEXT, UUID)
    TO service_role;

COMMIT;
