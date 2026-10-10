"""Loopback-only simulator integration server; never deploy this fixture.

Runs the real manufacturing routes and queue SQL against disposable PostgreSQL.
Supabase Auth/Storage are local adapters; no production offers are sent.
"""
from pathlib import Path
import importlib.util
import os
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('REVE_API_KEY', 'unused-local-fixture')
os.environ.setdefault('NANOBANA_API_KEY', 'unused-local-fixture')
os.environ.setdefault('SUPABASE_URL', 'http://127.0.0.1:8810')
os.environ.setdefault('SUPABASE_SERVICE_ROLE_KEY', 'unused-local-fixture')

spec = importlib.util.spec_from_file_location('mfg_test', ROOT / 'tests/test_manufacturing_api.py')
test = importlib.util.module_from_spec(spec)
spec.loader.exec_module(test)

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
import psycopg
from psycopg.types.json import JsonbDumper
import uvicorn
from app.routers import manufacturing as routes
from app.services import manufacturing_scheduler as worker

psycopg.adapters.register_dumper(dict, JsonbDumper)

pg, db_uri, temp_dir = test.setup_test_db()
test.bootstrap_db(db_uri)
with psycopg.connect(db_uri, autocommit=True) as db:
    db.execute((ROOT / "migrations/021_parallel_manufacturing_quotes.sql").read_text())
retailer_user = '10000000-0000-0000-0000-000000000001'
wholesaler_users = ['10000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000003']
with psycopg.connect(db_uri, autocommit=True) as db:
    for user in [retailer_user, *wholesaler_users]:
        db.execute('INSERT INTO auth.users(id,email) VALUES (%s,%s)', (user, user + '@fixture.invalid'))
    db.execute("INSERT INTO retailers(user_id,business_name,verification_status) VALUES (%s,'Simulator retailer','verified')", (retailer_user,))
    for user in wholesaler_users:
        db.execute("INSERT INTO wholesalers(user_id,business_name,verification_status) VALUES (%s,%s,'verified')", (user, 'Simulator supplier'))

assets = Path(temp_dir) / 'assets'
assets.mkdir()

class LocalBucket:
    def upload(self, path, content, file_options=None):
        (assets / Path(path).name).write_bytes(content)
        return {'Key': path}

    def create_signed_url(self, path, expires_in):
        return {'signedURL': 'http://127.0.0.1:8810/fixture-assets/' + Path(path).name}

def client(user=None):
    result = test.PostgresClientAdapter(db_uri, user_id=user)
    result.storage.from_.return_value = LocalBucket()
    return result

def authenticated_client(token):
    user = token.removeprefix('token-')
    if user not in [retailer_user, *wholesaler_users]:
        raise HTTPException(401, 'Unknown local fixture token')
    return client(user)

routes.get_supabase = lambda: client()
routes.get_authenticated_supabase = authenticated_client
worker.get_supabase = lambda: client()

@asynccontextmanager
async def lifespan(app):
    await worker.scheduler.start()
    yield
    await worker.scheduler.stop()
    pg._cleanup()

app = FastAPI(lifespan=lifespan)
app.include_router(routes.router)

@app.get('/fixture-assets/{name}')
def fixture_asset(name: str):
    try:
        uuid.UUID(Path(name).stem)
    except ValueError:
        raise HTTPException(404)
    path = assets / name
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)

if __name__ == '__main__':
    print('SIMULATOR_FIXTURE_READY: isolated database and loopback-only API', flush=True)
    uvicorn.run(app, host='127.0.0.1', port=8810)
