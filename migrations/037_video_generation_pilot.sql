-- Internal pilot only. Applying this migration does not enable generation.
BEGIN;

CREATE TABLE IF NOT EXISTS public.video_generations (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    product_id uuid NOT NULL,
    parent_id uuid REFERENCES public.video_generations(id),
    idempotency_key uuid NOT NULL,
    request_hash text NOT NULL,
    budget_key text NOT NULL,
    pack_expires_at timestamptz NOT NULL,
    source_image_url text NOT NULL,
    source_sha256 text NOT NULL,
    source_path text NOT NULL,
    model text NOT NULL CHECK (model IN ('kling-3.0', 'kling-2.5-turbo')),
    duration integer NOT NULL CHECK (duration IN (5, 10)),
    shot_id text NOT NULL CHECK (shot_id IN ('option_1', 'option_2')),
    multi_shot boolean NOT NULL DEFAULT false CHECK (NOT multi_shot OR model='kling-3.0'),
    category text NOT NULL,
    category_inputs jsonb NOT NULL,
    prompt text NOT NULL CHECK (length(prompt) BETWEEN 1 AND CASE WHEN model='kling-3.0' THEN 3072 ELSE 2500 END),
    prompt_parts jsonb NOT NULL,
    prompt_version text NOT NULL,
    estimated_units numeric NOT NULL CHECK (estimated_units > 0),
    accounted_units numeric NOT NULL CHECK (accounted_units >= 0),
    billed_units numeric CHECK (billed_units >= 0),
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'submitting', 'generating', 'saving', 'ready', 'failed', 'needs_review', 'cancelled')),
    provider_task_id text,
    output_path text,
    error_code text,
    message text,
    lease_token uuid,
    lease_until timestamptz,
    next_poll_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(owner_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS video_generations_history ON public.video_generations(owner_id, product_id, created_at DESC);
CREATE INDEX IF NOT EXISTS video_generations_work ON public.video_generations(next_poll_at) WHERE status IN ('queued','submitting','generating','saving');
CREATE INDEX IF NOT EXISTS video_generations_budget ON public.video_generations(budget_key);
ALTER TABLE public.video_generations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.video_generations FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.video_generations TO service_role;

CREATE OR REPLACE FUNCTION public.video_reserve(p_job jsonb, p_limit numeric, p_expires timestamptz DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE existing public.video_generations; result public.video_generations; used numeric;
BEGIN
    IF p_limit IS NULL OR p_limit <= 0 OR p_limit > 1000000 OR p_limit::text IN ('NaN','Infinity','-Infinity') THEN
        RAISE EXCEPTION 'Invalid pilot limit';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('video-budget:' || (p_job->>'budget_key'), 0));
    SELECT * INTO existing FROM public.video_generations
        WHERE owner_id = (p_job->>'owner_id')::uuid AND idempotency_key = (p_job->>'idempotency_key')::uuid;
    IF FOUND THEN
        IF existing.request_hash <> p_job->>'request_hash' THEN RETURN jsonb_build_object('error','idempotency_conflict'); END IF;
        RETURN to_jsonb(existing);
    END IF;
    IF p_expires IS NOT NULL AND p_expires <= clock_timestamp() THEN RETURN jsonb_build_object('error','pack_expired'); END IF;
    SELECT coalesce(sum(accounted_units),0) INTO used FROM public.video_generations WHERE budget_key = p_job->>'budget_key';
    IF (p_job->>'estimated_units')::numeric::text IN ('NaN','Infinity','-Infinity') THEN RAISE EXCEPTION 'Invalid estimate'; END IF;
    IF used + (p_job->>'estimated_units')::numeric > p_limit THEN
        RETURN jsonb_build_object('error','budget_exhausted','available_units',greatest(0,p_limit-used));
    END IF;
    INSERT INTO public.video_generations(id,owner_id,product_id,parent_id,idempotency_key,request_hash,budget_key,pack_expires_at,
        source_image_url,source_sha256,source_path,model,duration,shot_id,multi_shot,category,category_inputs,
        prompt,prompt_parts,prompt_version,estimated_units,accounted_units)
    VALUES ((p_job->>'id')::uuid,(p_job->>'owner_id')::uuid,(p_job->>'product_id')::uuid,
        (p_job->>'parent_id')::uuid,(p_job->>'idempotency_key')::uuid,p_job->>'request_hash',p_job->>'budget_key',p_expires,
        p_job->>'source_image_url',p_job->>'source_sha256',p_job->>'source_path',p_job->>'model',
        (p_job->>'duration')::int,p_job->>'shot_id',coalesce((p_job->>'multi_shot')::boolean,false),p_job->>'category',p_job->'category_inputs',
        p_job->>'prompt',p_job->'prompt_parts',p_job->>'prompt_version',
        (p_job->>'estimated_units')::numeric,(p_job->>'estimated_units')::numeric)
    RETURNING * INTO result;
    RETURN to_jsonb(result);
END $$;

CREATE OR REPLACE FUNCTION public.video_claim(p_concurrency integer DEFAULT 2, p_lease_seconds integer DEFAULT 180)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE candidate public.video_generations; active integer; fresh boolean;
BEGIN
    IF p_concurrency NOT BETWEEN 1 AND 5 OR p_lease_seconds NOT BETWEEN 120 AND 600 THEN RAISE EXCEPTION 'Invalid worker limits'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('video-dispatch',0));
    SELECT count(*) INTO active FROM public.video_generations WHERE status IN ('submitting','generating','needs_review');
    SELECT * INTO candidate FROM public.video_generations
      WHERE status IN ('queued','submitting','generating','saving')
        AND next_poll_at <= clock_timestamp() AND (lease_until IS NULL OR lease_until < clock_timestamp())
        AND (status <> 'queued' OR active < p_concurrency)
      ORDER BY (status = 'queued'), next_poll_at, created_at FOR UPDATE SKIP LOCKED LIMIT 1;
    IF NOT FOUND THEN RETURN NULL; END IF;
    fresh := candidate.status = 'queued';
    UPDATE public.video_generations SET
        status = CASE WHEN fresh THEN 'submitting' ELSE status END,
        lease_token = gen_random_uuid(), lease_until = clock_timestamp() + make_interval(secs=>p_lease_seconds),
        updated_at = clock_timestamp()
      WHERE id = candidate.id RETURNING * INTO candidate;
    RETURN to_jsonb(candidate) || jsonb_build_object('fresh_submission',fresh);
END $$;

CREATE OR REPLACE FUNCTION public.video_update(p_id uuid, p_token uuid, p_patch jsonb, p_release boolean DEFAULT true)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE result public.video_generations;
BEGIN
    UPDATE public.video_generations SET
        status = coalesce(p_patch->>'status',status),
        provider_task_id = coalesce(p_patch->>'provider_task_id',provider_task_id),
        output_path = coalesce(p_patch->>'output_path',output_path),
        billed_units = CASE WHEN p_patch ? 'billed_units' THEN (p_patch->>'billed_units')::numeric ELSE billed_units END,
        accounted_units = CASE WHEN p_patch ? 'billed_units' THEN (p_patch->>'billed_units')::numeric ELSE accounted_units END,
        error_code = p_patch->>'error_code', message = p_patch->>'message',
        next_poll_at = clock_timestamp() + make_interval(secs=>coalesce((p_patch->>'poll_after')::int,5)),
        lease_token = CASE WHEN p_release THEN NULL ELSE lease_token END,
        lease_until = CASE WHEN p_release THEN NULL ELSE lease_until END,
        updated_at = clock_timestamp()
      WHERE id = p_id AND lease_token = p_token AND lease_until > clock_timestamp()
      RETURNING * INTO result;
    IF NOT FOUND THEN RETURN NULL; END IF;
    -- The worker supplies billed_units only after a terminal provider result.
    -- Missing/late billing stays reserved; explicit rejection is unbilled.
    IF result.id IS NOT NULL AND p_patch->>'error_code' = 'submission_rejected' AND result.provider_task_id IS NULL THEN
        UPDATE public.video_generations SET accounted_units=0,billed_units=0 WHERE id=p_id RETURNING * INTO result;
    END IF;
    RETURN to_jsonb(result);
END $$;

CREATE OR REPLACE FUNCTION public.video_cancel(p_id uuid, p_owner uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE result public.video_generations;
BEGIN
    UPDATE public.video_generations SET status='cancelled',accounted_units=0,billed_units=0,updated_at=clock_timestamp()
      WHERE id=p_id AND owner_id=p_owner AND status='queued' RETURNING * INTO result;
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN to_jsonb(result);
END $$;

CREATE OR REPLACE FUNCTION public.video_budget(p_key text, p_limit numeric)
RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT jsonb_build_object('limit_units',p_limit,'accounted_units',coalesce(sum(accounted_units),0),
        'available_units',greatest(0,p_limit-coalesce(sum(accounted_units),0)),
        'reported_billed_units',coalesce(sum(billed_units),0)) FROM public.video_generations WHERE budget_key=p_key;
$$;

REVOKE ALL ON FUNCTION public.video_reserve(jsonb,numeric,timestamptz), public.video_claim(integer,integer),
    public.video_update(uuid,uuid,jsonb,boolean), public.video_cancel(uuid,uuid), public.video_budget(text,numeric)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.video_reserve(jsonb,numeric,timestamptz), public.video_claim(integer,integer),
    public.video_update(uuid,uuid,jsonb,boolean), public.video_cancel(uuid,uuid), public.video_budget(text,numeric)
    TO service_role;

-- SDK access is service-role only; downloads are issued by the authenticated API.
DO $$ BEGIN
    IF to_regclass('storage.buckets') IS NOT NULL THEN
        INSERT INTO storage.buckets(id,name,public) VALUES ('video-outputs','video-outputs',false) ON CONFLICT (id) DO NOTHING;
        IF EXISTS (SELECT 1 FROM storage.buckets WHERE id='video-outputs' AND public) THEN
            RAISE EXCEPTION 'video-outputs must be a private bucket';
        END IF;
    END IF;
    IF to_regclass('storage.objects') IS NOT NULL THEN
        -- Existing folder-based policies must not grant direct video access.
        DROP POLICY IF EXISTS video_outputs_server_only ON storage.objects;
        CREATE POLICY video_outputs_server_only ON storage.objects AS RESTRICTIVE
            FOR ALL TO anon, authenticated
            USING (bucket_id <> 'video-outputs') WITH CHECK (bucket_id <> 'video-outputs');
    END IF;
END $$;
COMMIT;
