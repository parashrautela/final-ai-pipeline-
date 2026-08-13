# AI Jewellery Image Pipeline — Design Document

This document covers the complete design of the pipeline in two parts:

1. **High-Level Design** — what happens end-to-end, from the moment a user uploads an image to when styled variants appear on their screen.
2. **Low-Level Design** — what each file inside `app/` does, what functions it exposes, and how it fits into the bigger picture.

---

## Part 1 — High-Level Design

### What this pipeline does

A user uploads a raw hii jewellery photo (e.g. a ring on a white cloth). The pipeline automatically:

1. Strips the background from the photo using **Reve AI**.
2. Generates **4 professionally styled product shots** from the cleaned image using **Nanobana AI** — each on a different backdrop (stone, velvet, marble, charcoal).
3. Uploads every output to **Supabase Storage**.
4. Writes the image URLs back to the **Supabase database**, so the frontend can poll and display results.

The user gets a response **immediately** (within ~1 second) and then polls for the final images.

---

### System components

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLIENT (Browser / Mobile)                │
│                                                                 │
│  POST /process  ──►  GET /product/{id}  (poll every 5s)        │
└───────────────────────────┬─────────────────────────────────────┘
                            │ HTTP
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                    FastAPI Server  (app/main.py)                │
│                                                                 │
│  Rate Limiter → Input Validation → File Validation             │
│  → Create DB row → Upload raw image → Queue background task    │
│                            │                                    │
│                            ▼ (background, non-blocking)         │
│                   Pipeline Orchestrator (app/services/pipeline) │
└───────┬───────────────────────────────────┬─────────────────────┘
        │                                   │
        ▼                                   ▼
┌───────────────┐                 ┌──────────────────────┐
│  Reve AI API  │                 │  Nanobana AI API      │
│  (background  │                 │  (scene enhancement)  │
│   removal)    │                 │  × 4 concurrently     │
└───────┬───────┘                 └──────────┬────────────┘
        │                                    │
        └───────────────────┬────────────────┘
                            ▼
              ┌─────────────────────────────┐
              │     Supabase Storage        │
              │  (stores all image files)   │
              └─────────────────────────────┘
                            │
                            ▼
              ┌─────────────────────────────┐
              │     Supabase Database       │
              │  (stores product row + URLs)│
              └─────────────────────────────┘
```

---

### End-to-end flow — step by step

#### Phase 1: Upload (happens synchronously, ~1 second)

```
Client
  │
  │  POST /process  (multipart/form-data: file, title, jewellery_type)
  │
  ▼
Rate Limiter  ─── 5 req/min per IP ──► 429 if exceeded
  │
  ▼
Input Validation  ─── sanitize title + jewellery_type, check for injection ──► 422 if bad
  │
  ▼
File Validation  ─── MIME type check, 10MB size cap ──► 413 / 415 if bad
  │
  ▼
Create product row in Supabase DB
  (id, title, jewellery_type, created_at → stored)
  │
  ▼
Upload raw image to Supabase Storage
  → plant-images/products/{product_id}.jpg
  │
  ▼
Write raw image URL to DB (image_url field)
  │
  ▼
Schedule background task
  │
  ▼
Return 202 Accepted  →  { product_id, raw_image_url }
```

The client now has a `product_id` and can start polling `GET /product/{id}`.

---

#### Phase 2: Background Pipeline (happens async, ~30–120 seconds)

```
Background Task starts
  │
  ▼
Step 1 — Download raw image from Supabase Storage
  (parse Supabase URL → extract bucket + path → download bytes)
  │
  ▼
Step 2 — Reve AI: Background Removal
  - Encode image as base64
  - POST to https://api.reve.com/v1/image/edit
  - Receive cleaned PNG (background removed, jewellery isolated)
  - Retry up to MAX_RETRIES times on 429 / 5xx with exponential back-off
  │
  ▼
Step 3 — Upload Reve output to temp storage
  → plant-images/products/temp/reve_{product_id}.png
  - This gives Nanobana a stable public URL to read from
  │
  ▼
Step 4 — Nanobana: 4 Variants  (all 4 run CONCURRENTLY via asyncio.gather)
  │
  ├── Variant 1: Stone backdrop
  │     POST /nanobanana/generate  (type=IMAGETOIMAGE, prompt=stone)
  │     GET  /nanobanana/record-info?taskId=...  (poll every 5s, up to 60 polls)
  │     Download result image bytes
  │     Upload → plant-images/products/processed/{product_id}_v1.png
  │
  ├── Variant 2: Velvet backdrop      (same flow as above)
  │     Upload → .../{product_id}_v2.png
  │
  ├── Variant 3: Marble backdrop      (same flow as above)
  │     Upload → .../{product_id}_v3.png
  │
  └── Variant 4: Charcoal backdrop    (same flow as above)
        Upload → .../{product_id}_v4.png
  │
  ▼
Collect successful URLs
  (if 1+ variants succeeded, continue; if all 4 failed, raise error)
  │
  ▼
Step 5 — Write results to Supabase DB
  - generated_image_urls = [url1, url2, url3, url4]
  - image_url = url1  (first variant, backwards-compatible)
```

---

#### Phase 3: Frontend Polling

```
Client polls  GET /product/{product_id}  every ~5 seconds
                │
                ▼
           DB row returned
                │
   generated_image_urls == []  →  still processing, keep polling
                │
   generated_image_urls == [url1, url2, url3, url4]  →  done, display images
```

---

### Storage layout

```
plant-images/  (Supabase Storage bucket)
│
├── products/
│   ├── {product_id}.jpg          ← raw upload (phase 1)
│   │
│   ├── temp/
│   │   └── reve_{product_id}.png ← Reve output, used as Nanobana input
│   │
│   └── processed/
│       ├── {product_id}_v1.png   ← Stone variant
│       ├── {product_id}_v2.png   ← Velvet variant
│       ├── {product_id}_v3.png   ← Marble variant
│       └── {product_id}_v4.png   ← Charcoal variant
```

---

### Database schema (Supabase — `images` table)

| Column                 | Type        | Set when                         |
|------------------------|-------------|----------------------------------|
| `id`                   | uuid (PK)   | Row created (Phase 1)            |
| `title`                | text        | Row created                      |
| `jewellery_type`       | text        | Row created                      |
| `image_url`            | text        | Phase 1 (raw), then v1 after pipeline |
| `generated_image_urls` | jsonb[]     | End of pipeline (all 4 URLs)     |
| `created_at`           | timestamptz | Row created                      |
| `updated_at`           | timestamptz | Any update                       |
| `status`               | text        | pending → processing → done/error |

---

### API surface

| Method | Path                   | Rate limit   | Purpose                                          |
|--------|------------------------|--------------|--------------------------------------------------|
| GET    | `/health`              | 30/min       | Uptime check                                     |
| POST   | `/process`             | 5/min per IP | Upload image, create product, start pipeline     |
| POST   | `/process/{image_id}`  | 10/min per IP| Re-run pipeline for existing product             |
| GET    | `/product/{product_id}`| 60/min per IP| Poll product status and retrieve image URLs      |

---

### Security measures

| Layer                  | Mechanism                                                       |
|------------------------|-----------------------------------------------------------------|
| Rate limiting          | slowapi, per-IP, route-level caps                              |
| Input validation       | Pydantic models + custom sanitization (injection + XSS checks) |
| Prompt injection guard | Regex pattern list checked before HTML escaping                |
| UUID validation        | Parsed + `version == 4` check (prevents path traversal)        |
| MIME type check        | Allowlist before file bytes are read                           |
| File size cap          | 10MB enforced in memory before DB write                        |
| CORS                   | Explicit origin allowlist, no wildcard                         |

---

### Error resilience

| Scenario                              | Behaviour                                                      |
|---------------------------------------|----------------------------------------------------------------|
| 1–3 Nanobana variants fail            | Remaining variants still used; partial result saved to DB      |
| All 4 Nanobana variants fail          | Pipeline raises, product stays in `processing` state           |
| Reve API fails after retries          | Pipeline fails, no image URLs written                          |
| Server crashes mid-pipeline           | `reset_stale_jobs()` resets stuck rows to `pending` after timeout |
| Upstream returns 429 or 5xx          | Exponential back-off retry (1s, 2s, 4s …)                     |

---

## Part 2 — Low-Level Design

### File map

```
app/
├── main.py            ← HTTP layer: routes, middleware, rate limiting
├── config.py          ← All settings (env vars) + 4 variant prompts
├── logging.py         ← JSON structured logger setup
├── worker.py          ← Background worker coroutine (lifecycle hook)
├── validation.py      ← Input sanitization, injection detection, UUID validation
├── db/
│   └── repository.py  ← Supabase DB client + all CRUD functions
└── services/
    ├── ai.py          ← Reve and Nanobana API clients
    ├── pipeline.py    ← Pipeline orchestration (calls ai + storage + db)
    └── storage.py     ← Supabase Storage upload/download helpers
```

---

### `app/main.py` — HTTP entry point

**Role:** Owns the HTTP server. Every request enters here. Nothing else imports from this file.

**What it does:**
- Defines the FastAPI `app` instance with a `lifespan` context that starts and stops the background worker.
- Registers two middleware layers: CORS (origin allowlist) and a fallback CORS-header injector for error responses.
- Registers the `slowapi` rate limiter and its `429` exception handler.
- Defines 4 routes:
  - `GET /health` — simple environment check.
  - `POST /process` — validate input → create DB row → upload raw image → schedule background pipeline.
  - `POST /process/{image_id}` — re-run pipeline for existing product (optionally with a new file).
  - `GET /product/{product_id}` — return the current DB row so the frontend can check if images are ready.
- `_run_product_pipeline()` — private async wrapper called by `background_tasks.add_task()`. It calls `process_product_image()` from `services/pipeline.py` and logs the outcome.

**Key decisions:**
- Returns `202 Accepted` immediately and runs the actual AI work in the background — the upload round-trip is ~1s, pipeline takes 30–120s.
- Validation (`validate_product_input`, `validate_product_id`) runs before any DB or storage operation.

---

### `app/config.py` — Settings

**Role:** Single source of truth for all configuration. No other file reads `.env` directly.

**What it does:**
- Defines `VARIANT_PROMPTS` — a list of 4 verbose text prompts (one per scene), embedded in code but overridable via env vars.
- Defines the `Settings` class (pydantic-settings `BaseSettings`). Every value maps to an environment variable or `.env` key.
- Groups settings by concern: Supabase, AI keys, 4 variant prompts, app behaviour, worker tuning, DB table name, storage bucket/folder names, allowed MIME types, max file size.
- Exposes `settings.NANOBANA_VARIANT_PROMPTS` as a computed property that returns all 4 active prompts as a list — so `pipeline.py` can iterate without knowing individual field names.

**Key field:** `PROCESSING_TIMEOUT_SECONDS = 300` — after 5 minutes, `reset_stale_jobs()` returns a stuck job to `pending`.

---

### `app/logging.py` — Structured logger

**Role:** Configure logging once at import time. All other files just do `from app.logging import logger`.

**What it does:**
- Defines `JSONFormatter` — formats every log record as a single-line JSON object with `timestamp`, `level`, `message`, `module`, `function`, and optionally `job_id` and `exception`.
- `setup_logging()` — replaces root logger handlers with a single stdout JSON handler. Also silences `httpx` at WARNING level (it logs every request header otherwise).
- Exports a module-level `logger` instance for import.

**Why JSON:** Makes logs parseable by Datadog, CloudWatch, or any log aggregator without custom parsing rules.

---

### `app/worker.py` — Background worker

**Role:** A long-living async coroutine that FastAPI's `lifespan` starts on boot and cancels on shutdown.

**What it does currently:**
- Logs that it has started, then sleeps indefinitely in a loop (`asyncio.sleep(3600)`).
- Jobs are triggered via `POST /process`, not by this worker polling the DB.

**Why it exists:** The lifespan mechanism needs a coroutine it can cancel cleanly on shutdown. When a job-queue polling strategy is added later, the `while True: sleep(3600)` block gets replaced with actual DB polling logic (`fetch_pending_job` → `process_job`).

---

### `app/validation.py` — Input validation and sanitization

**Role:** Security boundary between raw user input and the rest of the system.

**What it does:**

`sanitize_text(value, max_length, field_name)`:
- Strip + collapse whitespace.
- Block path traversal patterns (`../`, `..\\`).
- Check for prompt injection patterns **before** HTML escaping (important: angle-bracket tokens like `<|im_start|>` get escaped by `html.escape()` and would bypass the regex otherwise).
- Remove dangerous characters (`<>{}[]`, control chars, backticks).
- HTML-escape remaining text (XSS prevention).
- Enforce length limit.

`validate_uuid(value, field_name)`:
- Parse with `UUID(value)`.
- Explicitly assert `parsed.version == 4` — the `UUID(value, version=4)` constructor does NOT reject non-v4 UUIDs, it silently overwrites the version field.

`sanitize_for_prompt(text, max_length)`:
- Injection check first (before character stripping).
- Keeps only `[a-zA-Z0-9 .,!?-'"():;]` — safe for embedding in AI prompts.
- Truncates with word-boundary awareness.

`INJECTION_DETECTOR`:
- A compiled regex of 29 patterns covering: system prompt overrides, LLM model tokens (`[INST]`, `<<SYS>>`, `<|im_start|>`), roleplay/jailbreak phrases, DAN/developer mode triggers.

**Pydantic models:**
- `ProductCreate` — validates `title` (max 200) and `jewellery_type` (max 100) through `sanitize_text`.
- `ProductId` — validates a UUID4 with both a regex pattern and `validate_uuid`.

---

### `app/db/repository.py` — Database layer

**Role:** Every database read and write in the system goes through this file. No other file talks to Supabase Postgres directly.

**`get_supabase()`** — lazy-init singleton. Creates the `supabase-py` client once on first call. Handles the `httpx>=0.28.0` requirement (gotrue uses the `proxy=` kwarg which is only available in 0.28+).

**Functions:**

| Function | What it does |
|---|---|
| `create_product(title, jewellery_type)` | INSERT a new row, return the created dict |
| `fetch_pending_job()` | SELECT oldest pending row, then UPDATE status → processing in one optimistic-lock pattern. Returns None if another worker claimed it first. |
| `fetch_job_by_id(job_id)` | SELECT * for a single row by PK. Used by `GET /product/{id}`. |
| `reset_stale_jobs(timeout_seconds)` | UPDATE rows stuck in `processing` beyond the timeout back to `pending`. |
| `update_job_status(job_id, status, ...)` | Generic status write (used by `process_job` in the worker path). |
| `update_product_image_url(product_id, url)` | Write the raw/first-variant URL to `image_url`. |
| `update_product_generated_images(product_id, urls)` | Write the full list to `generated_image_urls` JSONB column and optionally update `image_url` to the first one. |

**Error handling:** `APIError` codes `PGRST205`, `PGRST200`, `PGRST106` indicate schema misconfiguration and are re-raised immediately (no point retrying).

---

### `app/services/ai.py` — AI API clients

**Role:** All outbound calls to Reve and Nanobana live here. Nothing else constructs HTTP requests to these APIs.

**`_request_with_retry(client, method, url, ...)`** — shared retry helper. Retries on `429` and `5xx` with exponential back-off (`2^retry_count` seconds). 4xx other than 429 are not retried (they indicate a bad request on our end).

**`_extract_image_bytes(response_json)`** — handles Gemini-style responses where the image is returned as base64 inside `candidates[0].content.parts[*].inline_data.data`.

**`ReveClient`**:
- `remove_background(image_bytes)` — base64-encodes the image, POSTs to Reve, handles two response shapes (direct `{"image": "<base64>"}` and Gemini candidates structure). Timeout 150s.

**`NanobanaClient`**:
- `enhance_image(image_url, prompt)` — three-phase operation:
  1. **Submit**: POST to `/generate` with `type=IMAGETOIMAGE`, `imageUrls`, `prompt`. Gets back a `taskId`.
  2. **Poll**: GET `/record-info?taskId=...` every 5 seconds, up to 60 times (5 minutes). Checks `successFlag` and `failFlag` in response.
  3. **Download**: Once `successFlag=1`, extracts `resultImageUrl` from the response (checks multiple possible field names for API version compatibility) and downloads the bytes.

**Module-level singletons:** `reve_client = ReveClient()` and `nanobana_client = NanobanaClient()` — created once, reused across all requests.

---

### `app/services/pipeline.py` — Pipeline orchestration

**Role:** Calls ai.py, storage.py, and db/repository.py in the right order. This is the "brain" of the pipeline.

**`_generate_variant(reve_url, product_id, variant_index, prompt)`**:
- Calls `nanobana_client.enhance_image()` with the variant-specific prompt.
- Uploads the result via `upload_processed_image_variant()` (wrapped in `asyncio.to_thread` because the Supabase storage SDK is synchronous).
- Returns the public URL, or `None` on failure — so one variant failing does not crash the others.

**`process_product_image(product)`**:
- The main pipeline function. Called by `main.py`'s background task.
- Executes the 4-step flow:
  1. `resolve_product_image()` — download raw image.
  2. `reve_client.remove_background()` — strip background.
  3. `upload_file_to_storage()` → get a public URL for the Reve result.
  4. `asyncio.gather(_generate_variant × 4)` — all 4 variants run in parallel. Since each Nanobana task takes 20–40s, running them sequentially would take 80–160s; concurrently they finish in one Nanobana cycle (~20–40s).
- Filters `None` results, raises if all failed, otherwise calls `update_product_generated_images()`.

**`process_job(job)`**:
- Alternative entry point used if the system switches to a polling-worker model. Calls the same AI clients but writes to the `status`, `processed_url` fields instead of `generated_image_urls`.

---

### `app/services/storage.py` — Storage helpers

**Role:** All Supabase Storage reads and writes. No other file calls `sb.storage` directly.

**`_ensure_bucket(bucket_name)`** — creates the bucket as public if it does not already exist. Called before every upload. Idempotent.

**`_storage_path_from_image_url(image_url)`** — parses Supabase public URLs of the form `https://<project>.supabase.co/storage/v1/object/public/<bucket>/<path>` and returns `(bucket, path)`. Falls back to treating the input as a raw path in the default bucket.

**Functions:**

| Function | Direction | Path pattern |
|---|---|---|
| `upload_raw_image(bytes, product_id, content_type)` | Write | `products/{id}.jpg` |
| `upload_file_to_storage(bytes, bucket, path)` | Write | arbitrary path, `x-upsert=true` |
| `upload_processed_image(bytes, product_id)` | Write | `products/processed/{id}.png` |
| `upload_processed_image_variant(bytes, product_id, n)` | Write | `products/processed/{id}_vN.png` |
| `download_from_storage(bucket, path)` | Read | SDK download |
| `download_image(url)` | Read | HTTP GET, validates MIME + size |
| `resolve_product_image(product)` | Read | Parses `image_url` → calls `download_from_storage`, with fallback |

**Note on URL construction:** The Supabase storage SDK does not return the public URL after upload. It is constructed manually: `{SUPABASE_URL}/storage/v1/object/public/{bucket}/{path}`.

---

### Dependency graph (who calls whom)

```
main.py
  ├── validation.py
  ├── db/repository.py
  ├── services/storage.py  (upload_raw_image)
  └── services/pipeline.py
        ├── services/ai.py
        │     └── config.py  (API keys, prompts, retries)
        ├── services/storage.py  (resolve, upload_file, upload_variant)
        └── db/repository.py  (update_product_generated_images)

config.py          ← imported by almost everything; no imports from app/
logging.py         ← imported by almost everything; depends only on config.py
worker.py          ← imported by main.py; depends only on logging.py
```

No circular imports. `config.py` and `logging.py` are leaf nodes that everything else builds on top of.
