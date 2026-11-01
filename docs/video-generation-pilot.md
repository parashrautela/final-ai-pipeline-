# Video-generation pilot

Status: backend implemented; production deployment authorized, with both feature
and paid-generation switches off by default. No customer-wallet charge or paid
Kling generation has been performed.
Frontend work is separate. The two supplied samples are stored locally in the
git-ignored `.temp/video-references/` directory, not committed or uploaded.

## Agreed flow

1. Generate the normal product images.
2. In the catalogue, choose “Convert it into a video”.
3. Choose one of the actual available generated outputs (up to four).
4. Preview **Subtle showcase** or **Energetic editorial**.
5. Choose 5 or 10 seconds; complete the category-specific form if needed.
6. Review the server quote, then submit with an Idempotency-Key.
7. Leave/return safely; preview and download when ready. Every variation has
   its own row, source snapshot, prompt, price and optional parent attempt.

The backend refuses original uploads, missing/pending generated images, another
owner's product, unsupported models, arbitrary image URLs and changed quotes.
It never generates videos automatically after image generation.

## Reference interpretation

| Preset | Supplied source | Direction |
| --- | --- | --- |
| Subtle showcase | `6282cc5cbf7a5c51af854fcd3c002a5e_720w.mp4` (12.01s, 720×1280) | Restrained front arc/push-in, slow readable movement, hero presentation. The pilot adapts this into a continuous shot. |
| Energetic editorial | `db037e3a45cec7363d066b2064215010_720w.mp4` (22.69s, 720×1280) | More deliberate tracking/arcs, hero/detail cuts, controlled moving highlights and a premium high-contrast editorial look. |

Both references contain edited shots. Neither is an exact demonstration of one
continuous camera move. Both presets borrow video 2's editorial treatment while
preserving the selected starting image's background, palette, jewellery and display.
Do not copy the samples' jewellery, logos, text, props or product arrangements.
If the source image is a ring, never invent the necklace/earrings shown in a sample.

The reference footage is a **preview/creative benchmark**, not an input sent to
Kling. Generation sends only the selected image and the compiled prompt. This
retains image-to-video pricing and avoids copying other products into the output.
Exact product fidelity and reference matching still require paid sample review.

Energetic uses Kling 3.0's native storyboard: 3s hero + 2s detail for a 5s video;
4s hero + 3s detail + 3s return for a 10s video. It never recreates all 22 seconds
of the source montage. The cheaper 2.5 Turbo comparison supports the subtle
continuous preset only; the API rejects an energetic/2.5 combination.

## Prompt layers and category forms

`app/services/video_prompts.py` composes and snapshots:

The user's replacement master and current compiled examples are documented in
[video-prompts-v3.md](video-prompts-v3.md). The full user master is preserved
unchanged; the API uses a compact rendering because the full 4,079-character
master exceeds both selected models' limits.

- **Base:** image-to-video task, first-frame identity and scene continuity.
- **Global:** product preservation and the editorial lighting/detail treatment.
- **Category:** earrings, ring, necklace, bangle, chain or general rules. Existing
  jewellery aliases map into these families (jhumka → earrings, choker → necklace).
- **Local:** selected shot, duration-specific storyboard, focus and permitted motion.
- **Optional user note:** short additional direction (maximum 400 characters).

Earrings expose focus and still/subtle-sway choices; ring exposes whole-piece/
setting focus; necklace whole-piece/pendant; bangle whole-piece/pattern; chain
whole-piece/links. The server rejects form fields/choices from another category.
This is a first form draft for review, not a requirement for users to write prompts.

The complete compiled prompt is limited to 3,072 characters for Kling 3.0
(2,500 recommended by the provider) and 2,500 for Kling 2.5 Turbo.
It fails clearly instead of silently truncating preservation rules. The content
hash versions every attempt, including prompt overrides and storyboard timing.

Optional deployment prompt overrides: `VIDEO_BASE_PROMPT`, `VIDEO_GLOBAL_PROMPT`,
`VIDEO_SHOT_1_PROMPT`, `VIDEO_SHOT_2_PROMPT`. Supplied URLs for hosted previews can
later be configured with `VIDEO_SHOT_1_PREVIEW_URL`/`VIDEO_SHOT_2_PREVIEW_URL`.

## API contract

All endpoints require a real Supabase bearer token, even when the old image-credit
feature is turned off. `VIDEO_PILOT_USER_IDS` accepts selected UUIDs or an explicit
`*` for all authenticated accounts. Ownership checks always remain enforced.
All users share the configured provider-unit budget; no image wallets are debited.

| Method/path | Purpose |
| --- | --- |
| `GET /api/videos/products/{product_id}/options` | Eligibility, selected-output choices, category form, references, durations and local budget |
| `POST /api/videos/quote` | Validate source, normalize/hash image, compose prompt and calculate units; no paid call |
| `POST /api/videos/generate` | Verify the quote, snapshot the source, atomically reserve budget and queue one attempt |
| `GET /api/videos/{id}` | Current state and fresh one-hour signed media URLs |
| `GET /api/videos/products/{product_id}/history` | Last 50 owned attempts, including their status and pricing |
| `POST /api/videos/{id}/cancel` | Cancel an unclaimed queued job; release its reservation |
| `GET /api/videos/references/{option_1\|option_2}` | Authenticated local-only sample video preview |

Quote body:

```json
{
  "product_id": "20000000-0000-4000-8000-000000000001",
  "image_index": 0,
  "shot_id": "option_1",
  "duration": 5,
  "model": "kling-3.0",
  "category_inputs": {"focus": "whole_piece"},
  "note": ""
}
```

Generate uses that exact body plus the returned `quote_hash`, and a fresh UUID
`Idempotency-Key` header. A network retry reuses **the exact body and key**. A
deliberate new variation uses a new key and may include `parent_id`.

Changing the image's bytes, selected URL, prompt or pricing after quoting returns
409 and requires a fresh quote. The snapshot remains unchanged if the product is
subsequently reprocessed. Replaying a submitted key returns its original attempt.

The UI should fetch local preview videos with Authorization and play a blob URL;
an ordinary unauthenticated `<video src>` request intentionally gets 401. Hosted
reference previews are an explicit later asset-publishing step.

## Units and billing

All results are 1080p and silent. General-pack rates verified against Kling's
current pricing: https://kling.ai/document-api/pricing/base/video

| Model | 5s | 10s |
| --- | ---: | ---: |
| Kling 3.0 | 4 units | 8 units |
| Kling 2.5 Turbo | 2.5 units | 5 units |

This is an internal **provider-unit** pilot; it never touches customer wallets.
The SQL reservation serializes concurrent requests against one shared budget key.
Queued/submitting/generating attempts reserve their estimate. Terminal provider
billing replaces the estimate only when explicitly reported. Missing/uncertain
billing stays reserved; a known pre-submission rejection or queued cancellation
releases it. A reported zero on an in-progress task is not treated as a refund.

The default ceiling is 100 units. This ledger covers only calls made by this
integration; any manual/account-external usage must be reconciled before setting
the cap. Do not reset the budget key merely to make units appear available.
The purchased pack expiry must be configured with its timezone. New submissions
and jobs still queued at expiry are blocked.

## Recovery and storage

`queued → submitting → generating → saving → ready`, plus failed, cancelled and
needs_review states. Database leases prevent duplicate dispatch and stale writes.
Two provider tasks can run by default (maximum configurable value: five).

A submitted POST is never automatically retried. If its response is lost, the
worker queries Kling using the attempt UUID as `external_task_id`. An unconfirmed
attempt pauses for review after five minutes, keeping units reserved. An active
generation exceeding 30 minutes also pauses for review. Review the existing task
in Kling before an operator resumes it or reconciles its charge.

Saving errors retry the existing output download/upload, not video generation.
Expired leases recover the existing provider task. CDN downloads carry no API
credential, validate every redirect and host, and are capped at 100 MB. Source
images must be from our processed Supabase storage folder, at least 300px per
edge, at most 20 megapixels, and between 2:5 and 5:2. They are normalized to PNG
without cropping. Transparent cut-outs are rejected until given a final scene.

The migration creates the private `video-outputs` bucket. SDK uploads verify its
private flag; it must never be replaced by a public fallback. Media signing errors
return the job status with `media_unavailable: true` so the UI can retry preview
access without creating a new attempt.
An additional restrictive storage policy blocks direct client-role access to this
bucket even when existing folder policies allow access to other buckets.

If an enqueue transaction's response is uncertain, keep the snapshot and replay
the same key. Known duplicate/rejected reservations remove unused snapshots when
possible. A crash before enqueue can leave an unreferenced source snapshot; clean
only paths absent from `video_generations` after confirming no request is active.

## Local run and verification

Offline verification uses a new disposable PostgreSQL instance, simulated storage,
and `httpx.MockTransport` for every provider response. No real keys or image uploads
are used. Test dependencies `pytest`, `psycopg`, `pgserver` are already installed in
this workspace's `.venv`; they are not new production dependencies.

```sh
.venv/bin/python -m pytest tests/test_video_pilot.py -q
```

Latest local verification: **99 passing tests**, covering the video pilot plus
existing reprocess/image-count, Chamak prompt and analysis-failure regressions.
Video checks include real PostgreSQL concurrency/permissions, authentication,
image prerequisites, both presets, quote invalidation, unit reservations,
idempotency, cancellation, uncertain submissions, restart recovery, save retries,
pack expiry, private storage and local-only reference/server access.
Provider media is simulated in these tests; this does not validate visual quality.

For an actual local API instance, use a **local Supabase** project, local auth user
and an owned product with completed generated-image rows/assets. Apply migration
037 to that local database only. Configure these backend environment values:

```text
ENVIRONMENT=development
SUPABASE_URL=http://127.0.0.1:54321
SUPABASE_SERVICE_ROLE_KEY=<local-project-service-key>
SUPABASE_JWT_SECRET=<local-project-JWT-secret>
VIDEO_ENABLED=true
VIDEO_ALLOW_PAID_GENERATION=false
VIDEO_PILOT_USER_IDS=<local-auth-user-UUID>
VIDEO_BUDGET_KEY=general-video-trial
VIDEO_BUDGET_UNITS=100
VIDEO_PACK_EXPIRES_AT=<actual-pack-expiry-with-timezone>
KLING_API_KEY=<set-as-a-local-backend-secret-when-ready>
```

Do not replace an existing production `.env` to run this experiment; use a separate
process environment/local config. Never put the API key in the frontend or source
control. Starting the video-only entry point refuses a hosted Supabase URL:

```sh
.venv/bin/python -m uvicorn app.video_local:app --host 127.0.0.1 --port 8001
```

Open `http://127.0.0.1:8001/docs` for the API. This entry point starts no image,
catalogue-index, manufacturing, or backfill workers. Quotes need no Kling key;
generation requires the key, expiry, private storage and local migration.
Actual generation still spends real Kling units even when the API runs locally.
`VIDEO_ALLOW_PAID_GENERATION=true` is a separate explicit spending switch. When
false, new jobs are rejected before reservation and the provider client refuses
paid submissions. Existing idempotent requests can still return their saved job.

## Production deployment

Use the existing Railway `ai-pipeline` service linked to this repository's `main`
branch. Apply only migration 037 to the linked Supabase project, then deploy the
backend commit; do not run all historical migrations. This additive migration
creates only the video ledger/functions and private video bucket/policy.

The initial deployment used `VIDEO_ENABLED=false` and
`VIDEO_ALLOW_PAID_GENERATION=false`. Enabling actual generation requires both true.
No Kling key is needed for a disabled deployment. `/health` exposes the video API
availability and both switches without exposing credentials. Paid generation
requires a later deliberate configuration change, pilot-user allowlist, backend
secret and confirmed pack expiry. The user subsequently authorized all-account
access through `VIDEO_PILOT_USER_IDS=*`, sharing the existing 100-unit pack and
its actual expiry. The native iPad flow is implemented in the separate iOS repo.

Rollback the Railway deployment to the preceding healthy release if health or
existing image routes regress. The additive schema can remain; both switches off
prevent video work. Never delete the ledger to roll back a release.

## First live comparison, once credentials and example images are ready

Start with one 5s subtle Kling 3.0 request (4 units), included in the allocation
below. Verify actual billed units and examine jewellery fidelity before continuing.

- 3 images × 2 presets × 2 runs on 3.0: 12 outputs, 48 units.
- 3 images × subtle preset × 2 runs on 2.5 Turbo: 6 outputs, 15 units.
- 2 presets × 2 ten-second runs on 3.0: 4 outputs, 32 units.
- Total: 22 attempts, 95 units; five units unallocated.

Score product accuracy first, then camera match, temporal stability, acceptance
rate, elapsed time and cost per accepted clip. Different starting jewellery types
must retain their own form and appearance; the sample film's product is never the
target. Paid visual quality has not yet been verified.

Customer pricing, reference-video publishing and frontend changes are separate
from this backend rollout.
