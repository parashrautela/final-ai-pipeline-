-- Storyboard-studio videos reuse the Kling pilot's jobs, unit reservations and worker.
BEGIN;
ALTER TABLE public.video_generations DROP CONSTRAINT IF EXISTS video_generations_shot_id_check;
ALTER TABLE public.video_generations ADD CONSTRAINT video_generations_shot_id_check
    CHECK (shot_id IN ('option_1','option_2','storyboard'));
COMMIT;
