# AI Jewellery Image Pipeline

This repository hosts a FastAPI service that processes jewellery photos: it removes backgrounds (Reve AI), generates multiple stylised variants (Nanobana AI), stores results in Supabase Storage, and records metadata in Supabase Postgres.

This README has been updated to reflect the current, refactored layout where application code lives under the `app/` package. For a developer guide about internals see [app/README.md](app/README.md#L1).

---

## Quickstart (development)

1. Create and activate a virtual environment:

```powershell
python -m venv .venv
.venv\Scripts\activate
```

2. Install dependencies:

```powershell
pip install -r requirements.txt
```

3. Copy `.env.example` → `.env` and fill in keys (Supabase, REVE, NANOBANA, etc.).

4. Run the app (module path):

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Notes:
- Use the module path `app.main:app`. Do not pass a filesystem path like `./app/services/main:app`.
- For production, run without `--reload` and prefer containerized deployments.

### Set Creation Railway variables

Product images, Chamak 1 and Set Creation use Nano Banana 2 (`generate-2`) at
2K. Chamak 2 retains its OpenAI renderer. Set Creation reads these variables:

```text
SET_CREATION_OUTPUT_COUNT=4
SET_CREATION_RESOLUTION=2K
SET_CREATION_IMAGE_SIZE=2:3
```

Resolution is fixed at 2K; legacy 4K environment overrides are ignored.
Prompts are preserved up to the provider’s 20,000-character limit; longer
prompts fail before submission instead of silently losing category rules.

`SET_CREATION_OUTPUT_COUNT` is clamped to 1–4 and defaults to 4. Run
`migrations/011_set_creation_outputs.sql` before enabling multiple outputs so
all generated images are retained in the gallery.

---

## Repo layout (refactored)

```
ai-pipeline/
├── app/
│   ├── main.py            # FastAPI app + routes
│   ├── config.py          # Settings (pydantic) + prompts
│   ├── logging.py         # JSON structured logging
│   ├── worker.py          # Background worker loop
│   ├── db/
│   │   └── repository.py  # Supabase client + DB helpers
│   └── services/
│       ├── ai.py          # Reve & Nanobana API clients
│       ├── pipeline.py    # Pipeline orchestration
│       └── storage.py     # Supabase storage helpers
├── migrations/            # SQL migrations/snippets
├── requirements.txt
├── Dockerfile
├── .env.example           # template for local env vars
└── README.md              # this file (high-level quickstart)
```

For a detailed file-by-file explanation see [app/README.md](app/README.md#L1).

---

## Running with Docker

Build and run:

```bash
docker build -t ai-pipeline .
docker run -p 8000:8000 --env-file .env ai-pipeline
```

---

## Notes for developers

- The app uses module-level singletons (Supabase client, AI clients). When horizontally scaling, prefer multiple containers rather than multiple Uvicorn worker processes in a single container.
- The API route to start processing is `POST /process`. Check [app/main.py](app/main.py#L1) and [app/services/pipeline.py](app/services/pipeline.py#L1) for orchestration.
- If you change external client code, add logging to [app/services/ai.py](app/services/ai.py#L1) to capture raw API responses — this made debugging easier during refactor.

If you'd like, I can also:

- add a `.env.example` file to the repo,
- add a minimal `make`/PowerShell script to start the dev server,
- or create a GitHub Actions workflow for linting and tests.

# final-ai-pipeline-
# final-ai-pipeline-
# final-ai-pipeline-

## Shared invitation accounting

Invitation HTTP routes live in the Next.js repository, not this Python service. Shared accounting migrations live here: `017_invitation_gifts.sql`, `017b_referral_expiry_schedule.sql`, and `018_preserve_purchased_credits.sql`, with daily-credit migration 015 and the existing credit/onboarding prerequisites. See [the release handoff](../plans/invitation-referrals-release.md) for deployment order, grants, flags and checks.

The user selected preservation of unused purchased credits on 3 October 2026. Migration 018 keeps original lots, sources, receipt IDs and unit counts; the trusted `credits_preserve_purchased()` operation records their original expiry and makes them non-expiring. It creates a zero-delta audit ledger entry, not another grant. Preserved paid units join the bonus balance and may fund invitation extras after daily units; original-lot refunds retain that source. Unreconciled receipt statuses, missing preservation audit or unconverted paid balances block activation.

Migrations and activation have not been applied to production. The live invitation route/schema gap is recorded in the release handoff. Verify paid receipt/provider retirement prerequisites before activating. Local validation now covers 27 referral/preservation scenarios and 39 wallet regressions with real PostgreSQL.
