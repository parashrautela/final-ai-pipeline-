# Video Studio backend contract v1

Status: local development foundation, 4 November 2026. Separate from the existing Kling pilot. Final video rendering and quotes are unavailable; no endpoint calls a video provider.

Provider constraint checked against [official OpenAI documentation](https://developers.openai.com/api/docs/deprecations#2026-03-24-sora-2-video-generation-models-and-videos-api) on 4 November 2026: OpenAI's Videos API and Sora 2 models were removed on 24 September 2026, with no replacement listed. The user's current authorization allows OpenAI generation calls only. OpenAI image generation can support wearing previews/storyboards; actual video generation needs a separately selected and authorized provider. Keep video submission disabled while that remains unresolved.

## Availability and authentication

All routes use `/api/video-studio`, a Supabase bearer token, product ownership and a separate `VIDEO_STUDIO_ENABLED=false` default. Only accounts listed in `VIDEO_STUDIO_USER_IDS` (comma-separated auth user ids) may use the routes; the list is empty by default. `VIDEO_STUDIO_RENDER_MODE=mock` produces visibly labelled development images without inference. OpenAI rendering additionally requires `VIDEO_STUDIO_ALLOW_OPENAI=true`, `VIDEO_STUDIO_RENDER_MODE=openai` and a configured server key. No other image/video provider is used. These settings are not changed by this implementation.

## Endpoints

| Method/path | Request / result |
| --- | --- |
| `GET /products/{product_id}/options` | All eligible completed generated catalogue images with their original indexes, title/category, derived body placement, maximum two selected references, nine panels, development timing choices 5/10 seconds, render mode, and `video_generation_available:false`. |
| `POST /projects` | Create a persistent owned draft. Header `Idempotency-Key: UUID`; body is the configuration below. Duplicate identical keys return the same project; changed requests using that key return 409. |
| `GET /projects/{id}` | Current configuration/version, immutable source snapshots, wearing/storyboard state, nine shots, explicit approval, fresh signed asset URLs and honest renderer availability. |
| `GET /products/{product_id}/projects` | Latest 50 projects for the owned product. This is storyboard-project history, not a fake video library. |
| `PATCH /projects/{id}` | Header `Idempotency-Key: UUID`; full replacement configuration, optional replacement nine-shot plan, and `expected_version`. Increments the version and clears approval/storyboard; source/mode/category/human changes also clear wearing preview. Stale writes return 409. |
| `POST /projects/{id}/wearing` | `{expected_version}` plus `Idempotency-Key`. Human-led only. Creates one wearing-preview asset; clears dependent storyboard/approval. |
| `POST /projects/{id}/storyboard` | `{expected_version}` plus `Idempotency-Key`. Produces one numbered 3×3 contact sheet and its nine-shot mapping. Human-led requires the current wearing preview first. Regeneration creates a new project version and clears approval. |
| `POST /projects/{id}/approve` | `{expected_version}` plus `Idempotency-Key`. Approves the exact current storyboard version; missing/stale asset or in-progress work is rejected. Repeating the same request is safe. |
| `POST /projects/{id}/video` | `{expected_version}`. Enforces current storyboard approval, then returns an explicit `VIDEO_RENDERER_UNAVAILABLE` response. No quote, paid submission, video job, or fabricated ready video is created. |

Creation/configuration shape:

```json
{
  "product_id": "uuid",
  "image_indexes": [0, 4],
  "main_image_index": 4,
  "mode": "human_led",
  "duration": 10,
  "category": null,
  "human": {
    "ethnicity": "user-selected description",
    "skin_tone": "user-selected description",
    "appearance": "optional styling/hair/clothing direction"
  }
}
```

Indexes reference actual available outputs; there is no four-image ceiling. One or two unique indexes must come from this one owned product, and Main must be selected. Main is always reference image 1 for OpenAI. Supporting is another view/detail of the same product, never a second design to fuse. Sources are normalized, hashed and privately snapshotted before rendering. Category imports from product metadata; a supported correction is accepted only when the imported category is unknown. Human placement is derived from category. Human rendering uses an adult model as the proposed safety/default presentation, without inferring ethnicity from skin tone.

An update adds `expected_version` to the same configuration. `product_id` cannot change. Optional `shots` replaces exactly nine entries in order, each `{panel:1..9,direction:"..."}`. The backend derives equal panel timing from selected duration; a future final renderer must explicitly support its own timing and shot-input contract. Five/ten seconds here are development planning choices, not advertised support by an integrated OpenAI video renderer.

## State, concurrency and review

The server owns monotonic project versions and approval. Configuration/render changes invalidate approval and quotes (quotes do not yet exist). Approval records the current project version, storyboard asset identity and content hash. A source or creative change cannot reuse it. Edits do not regenerate automatically. Wearing-preview generation must finish before human-led storyboard generation. Storyboard regeneration requires another explicit approval.

Render operations persist an idempotency record before any inference. A project accepts one mutation/render at a time. Duplicate requests return the existing operation/project; conflicting keys, stale versions and concurrent edits return 409. Uncertain/crashed rendering stays visibly pending/review-needed and is never automatically resubmitted. An explicit new key/version is required for a deliberate retry. Private assets use expiring signed URLs; refresh the project to renew access.

A caught rendering/storage exception clears the active lock, records `needs_review`, and permits a deliberate new attempt after review. A process crash or unconfirmed database finish can leave an operation pending with its lock held. GET/replay exposes `operation_status:pending`, `active_operation_id` and a reconciliation notice. This foundation has no automatic restart worker or operator reconciliation endpoint; further mutations remain blocked until an operator inspects the saved asset/provider request and reconciles the persisted operation. Replaying the same key only reads its state and never resubmits inference.

Mock wearing previews are source-image placeholders labelled as such, not generated humans. Mock storyboards are numbered reference-image contact sheets with a development banner. Mock assets cannot become a ready video. Product/human consistency from OpenAI remains subject to user inspection; a prompt cannot guarantee fidelity or exact panel layout.

## Local setup and limits

The additive migration `038_video_studio.sql` is for an explicitly selected local disposable database only in this assignment. It is not applied to hosted Supabase. Private assets reuse the existing `video-outputs` bucket/storage helpers created by migration 037. The new flow neither reads nor modifies legacy video jobs, customer wallets or provider-unit billing.

The OpenAI adapter is implemented but no inference is performed during development verification. It reuses `OpenAIImageClient.fuse_images` with automatic retries disabled for this flow. No new production worker is started. Rendering currently completes within the initiating request; a lost response is recovered by replaying the same idempotency key or reading the project, never by automatically generating again.

Run the isolated API with `.venv/bin/python -m uvicorn app.video_studio_local:app --host 127.0.0.1 --port 8002` from `ai-pipeline`, using a separately configured **local** Supabase environment. This entry point refuses hosted Supabase and starts no legacy workers. Do not start `app.main` to test this feature against existing workspace configuration.

Project responses include `config`, `references` (Main first), `shots`, `wearing`, `storyboard`, `approval`, `approval_valid`, `operation_status`, `operation_in_progress`, `media_unavailable`, and `video_generation_available:false`. Each rendered asset has `id`, `provider` (`mock`/`openai`), `is_mock`, `version`, `sha256`, `input_hash` and signed `preview_url`; storage paths stay server-side. Mock storyboard approval is a development-only review artifact: `live_video_approval_valid` is always false. Even an approved non-mock storyboard is refused at the video endpoint because final rendering is not integrated.
