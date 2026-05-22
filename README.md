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
