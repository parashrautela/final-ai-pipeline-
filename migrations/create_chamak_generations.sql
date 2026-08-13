-- Migration: Create chamak_generations table, chamak_feedback, and chamak-outputs bucket

-- 1. Create chamak_generations table
CREATE TABLE IF NOT EXISTS public.chamak_generations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wholesaler_id UUID NOT NULL,
    source_image_1_url TEXT NOT NULL,
    source_image_2_url TEXT NOT NULL,
    stage1_analysis_json JSONB,
    wholesaler_form_json JSONB,
    note_text TEXT,
    compiled_prompt_text TEXT,
    prompt_version TEXT DEFAULT 'v1.0-chamak',
    output_image_url TEXT,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'analyzing', 'awaiting_input', 'generating', 'done', 'failed')),
    content_flag_hit TEXT DEFAULT 'ok' CHECK (content_flag_hit IN ('ok', 'not_jewelry', 'inappropriate', 'too_unclear_to_assess')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

-- Indexes for fast querying
CREATE INDEX IF NOT EXISTS idx_chamak_generations_wholesaler_id ON public.chamak_generations(wholesaler_id);
CREATE INDEX IF NOT EXISTS idx_chamak_generations_status ON public.chamak_generations(status);
CREATE INDEX IF NOT EXISTS idx_chamak_generations_created_at ON public.chamak_generations(created_at DESC);

-- Row Level Security for chamak_generations
ALTER TABLE public.chamak_generations ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Wholesalers can view own chamak generations" ON public.chamak_generations;
CREATE POLICY "Wholesalers can view own chamak generations"
    ON public.chamak_generations FOR SELECT
    USING (auth.uid() = wholesaler_id);

DROP POLICY IF EXISTS "Wholesalers can insert own chamak generations" ON public.chamak_generations;
CREATE POLICY "Wholesalers can insert own chamak generations"
    ON public.chamak_generations FOR INSERT
    WITH CHECK (auth.uid() = wholesaler_id);

DROP POLICY IF EXISTS "Wholesalers can update own chamak generations" ON public.chamak_generations;
CREATE POLICY "Wholesalers can update own chamak generations"
    ON public.chamak_generations FOR UPDATE
    USING (auth.uid() = wholesaler_id);

-- 2. Create chamak_feedback table
CREATE TABLE IF NOT EXISTS public.chamak_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chamak_generation_id UUID NOT NULL REFERENCES public.chamak_generations(id) ON DELETE CASCADE,
    satisfied BOOLEAN NOT NULL,
    what_went_wrong_text TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chamak_feedback_generation ON public.chamak_feedback(chamak_generation_id);

ALTER TABLE public.chamak_feedback ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Wholesalers can insert feedback for own generations" ON public.chamak_feedback;
CREATE POLICY "Wholesalers can insert feedback for own generations"
    ON public.chamak_feedback FOR INSERT
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.chamak_generations
            WHERE id = chamak_generation_id AND wholesaler_id = auth.uid()
        )
    );

DROP POLICY IF EXISTS "Wholesalers can view feedback for own generations" ON public.chamak_feedback;
CREATE POLICY "Wholesalers can view feedback for own generations"
    ON public.chamak_feedback FOR SELECT
    USING (
        EXISTS (
            SELECT 1 FROM public.chamak_generations
            WHERE id = chamak_generation_id AND wholesaler_id = auth.uid()
        )
    );

-- 3. Dedicated Storage Bucket for Chamak outputs (Private, Signed URLs only)
INSERT INTO storage.buckets (id, name, public)
VALUES ('chamak-outputs', 'chamak-outputs', false)
ON CONFLICT (id) DO UPDATE SET public = false;

-- Storage RLS Policies
DROP POLICY IF EXISTS "Wholesalers can read own chamak output objects" ON storage.objects;
CREATE POLICY "Wholesalers can read own chamak output objects"
    ON storage.objects FOR SELECT
    USING (bucket_id = 'chamak-outputs' AND auth.uid()::text = (storage.foldername(name))[1]);

DROP POLICY IF EXISTS "Wholesalers can upload own chamak output objects" ON storage.objects;
CREATE POLICY "Wholesalers can upload own chamak output objects"
    ON storage.objects FOR INSERT
    WITH CHECK (bucket_id = 'chamak-outputs' AND auth.uid()::text = (storage.foldername(name))[1]);
