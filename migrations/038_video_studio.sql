-- Separate development storyboard foundation; does not enable any provider.
BEGIN;
CREATE TABLE IF NOT EXISTS public.video_studio_projects (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    product_id uuid NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    data jsonb NOT NULL,
    active_operation uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.video_studio_requests (
    owner_id uuid NOT NULL,
    idempotency_key uuid NOT NULL,
    request_hash text NOT NULL,
    project_id uuid NOT NULL REFERENCES public.video_studio_projects(id),
    action text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending','complete','needs_review')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(owner_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS video_studio_product_history
    ON public.video_studio_projects(owner_id,product_id,created_at DESC);
ALTER TABLE public.video_studio_projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.video_studio_requests ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.video_studio_projects,public.video_studio_requests FROM PUBLIC,anon,authenticated;
GRANT ALL ON public.video_studio_projects,public.video_studio_requests TO service_role;

CREATE OR REPLACE FUNCTION public.video_studio_begin(
    p_id uuid,p_owner uuid,p_key uuid,p_hash text,p_expected integer,
    p_action text,p_product uuid,p_data jsonb,p_render boolean DEFAULT false
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE previous public.video_studio_requests; project public.video_studio_projects;
BEGIN
    -- Serialize replay keys before touching a project, including concurrent creates.
    PERFORM pg_advisory_xact_lock(hashtextextended('studio-key:'||p_owner::text||':'||p_key::text,0));
    SELECT * INTO previous FROM public.video_studio_requests
      WHERE owner_id=p_owner AND idempotency_key=p_key;
    IF FOUND THEN
        IF previous.request_hash<>p_hash THEN RETURN jsonb_build_object('error','idempotency_conflict'); END IF;
        SELECT * INTO project FROM public.video_studio_projects WHERE id=previous.project_id AND owner_id=p_owner;
        RETURN jsonb_build_object('duplicate',true,'operation_status',previous.status,'project',to_jsonb(project));
    END IF;
    IF p_action='create' THEN
        IF p_expected<>0 OR p_render THEN RAISE EXCEPTION 'Invalid create'; END IF;
        INSERT INTO public.video_studio_projects(id,owner_id,product_id,data)
          VALUES(p_id,p_owner,p_product,p_data) RETURNING * INTO project;
    ELSE
        IF p_action NOT IN ('edit','wearing','storyboard','approve') OR p_render<>(p_action IN ('wearing','storyboard')) THEN
            RAISE EXCEPTION 'Invalid studio action';
        END IF;
        SELECT * INTO project FROM public.video_studio_projects WHERE id=p_id AND owner_id=p_owner FOR UPDATE;
        IF NOT FOUND THEN RETURN jsonb_build_object('error','not_found'); END IF;
        IF project.version<>p_expected THEN RETURN jsonb_build_object('error','stale_version'); END IF;
        IF project.active_operation IS NOT NULL THEN RETURN jsonb_build_object('error','operation_in_progress'); END IF;
        UPDATE public.video_studio_projects SET
            data=p_data,
            version=version+CASE WHEN p_action='approve' THEN 0 ELSE 1 END,
            active_operation=CASE WHEN p_render THEN p_key ELSE NULL END,
            updated_at=clock_timestamp()
          WHERE id=p_id RETURNING * INTO project;
    END IF;
    INSERT INTO public.video_studio_requests(owner_id,idempotency_key,request_hash,project_id,action,status)
      VALUES(p_owner,p_key,p_hash,project.id,p_action,CASE WHEN p_render THEN 'pending' ELSE 'complete' END);
    RETURN jsonb_build_object('duplicate',false,'operation_status',CASE WHEN p_render THEN 'pending' ELSE 'complete' END,
                              'project',to_jsonb(project));
END $$;

CREATE OR REPLACE FUNCTION public.video_studio_finish(p_id uuid,p_owner uuid,p_key uuid,p_data jsonb,p_failed boolean DEFAULT false)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE project public.video_studio_projects;
BEGIN
    SELECT * INTO project FROM public.video_studio_projects WHERE id=p_id AND owner_id=p_owner FOR UPDATE;
    IF NOT FOUND OR project.active_operation IS DISTINCT FROM p_key THEN RETURN jsonb_build_object('error','operation_changed'); END IF;
    UPDATE public.video_studio_projects SET data=p_data,active_operation=NULL,updated_at=clock_timestamp()
      WHERE id=p_id RETURNING * INTO project;
    UPDATE public.video_studio_requests SET status=CASE WHEN p_failed THEN 'needs_review' ELSE 'complete' END
      WHERE owner_id=p_owner AND idempotency_key=p_key;
    RETURN jsonb_build_object('project',to_jsonb(project));
END $$;
REVOKE ALL ON FUNCTION public.video_studio_begin(uuid,uuid,uuid,text,integer,text,uuid,jsonb,boolean),
    public.video_studio_finish(uuid,uuid,uuid,jsonb,boolean) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.video_studio_begin(uuid,uuid,uuid,text,integer,text,uuid,jsonb,boolean),
    public.video_studio_finish(uuid,uuid,uuid,jsonb,boolean) TO service_role;
COMMIT;
