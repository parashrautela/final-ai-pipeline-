import os
import pathlib
import sys
import tempfile
import uuid
import pytest
import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

MIGRATION_DIR = pathlib.Path(__file__).resolve().parent.parent / 'migrations'
MIGRATION_030 = MIGRATION_DIR / '030_feature_controls.sql'
MIGRATION_032 = MIGRATION_DIR / '032_feature_controls_chamak.sql'
ROLLBACK_032 = MIGRATION_DIR / '032_feature_controls_chamak.rollback.sql'

import pgserver

@pytest.fixture(scope='session')
def pg_server():
    temp_dir = tempfile.mkdtemp(prefix='jewel-fc-3b-')
    server = pgserver.get_server(temp_dir, cleanup_mode='stop')
    yield server
    server.cleanup()

@pytest.fixture
def db_conn(pg_server):
    admin_uri = pg_server.get_uri()
    db_name = f'test_fc3b_{uuid.uuid4().hex[:8]}'
    with psycopg.connect(admin_uri, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE {db_name}')

    db_uri = psycopg.conninfo.make_conninfo(admin_uri, dbname=db_name)

    with psycopg.connect(db_uri, autocommit=True) as conn:
        conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN CREATE ROLE anon NOLOGIN; END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN CREATE ROLE authenticated NOLOGIN; END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN CREATE ROLE service_role NOLOGIN BYPASSRLS; END IF;
            END $$;

            CREATE TABLE IF NOT EXISTS public.wholesalers (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID UNIQUE NOT NULL,
                company_name TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS public.chamak_generations (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                wholesaler_id UUID NOT NULL,
                mode TEXT NOT NULL DEFAULT 'fusion',
                status TEXT NOT NULL DEFAULT 'queued',
                error_message TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
            GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
        """)

        # Apply 030 and 032
        with open(MIGRATION_030) as f:
            conn.execute(f.read())
        with open(MIGRATION_032) as f:
            conn.execute(f.read())

    yield db_uri

    with psycopg.connect(admin_uri, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS {db_name}')

def test_chamak_trigger_modes_and_enforcement(db_conn):
    with psycopg.connect(db_conn, autocommit=True) as conn:
        conn.execute('SET ROLE service_role;')
        w_user = uuid.uuid4()
        conn.execute(f"INSERT INTO public.wholesalers (user_id, company_name) VALUES ('{w_user}', 'Chamak Test W');")

        # Disable chamak
        conn.execute(f"SELECT public.admin_set_feature('{w_user}', 'chamak', false, 'Chamak test', 'admin', 0);")

        # 1. Mode 'off': trigger does NOT block
        conn.execute(f"INSERT INTO public.chamak_generations (wholesaler_id, mode) VALUES ('{w_user}', 'fusion');")

        # 2. Mode 'log': trigger does NOT block
        conn.execute("SELECT public.admin_set_catalog('chamak', true, 'log', 'Log mode test', 'admin');")
        conn.execute(f"INSERT INTO public.chamak_generations (wholesaler_id, mode) VALUES ('{w_user}', 'fusion');")

        # 3. Mode 'enforce': trigger raises PT403 with FEATURE_DISABLED
        conn.execute("SELECT public.admin_set_catalog('chamak', true, 'enforce', 'Enforce mode test', 'admin');")
        with pytest.raises(psycopg.Error) as exc_info:
            conn.execute(f"INSERT INTO public.chamak_generations (wholesaler_id, mode) VALUES ('{w_user}', 'fusion');")
        assert exc_info.value.sqlstate == 'PT403'
        assert 'FEATURE_DISABLED' in str(exc_info.value)

        # 4. Mode 'set_creation': parent chamak disabled forces set_creation trigger block
        conn.execute("SELECT public.admin_set_catalog('set_creation', true, 'enforce', 'Enforce child', 'admin');")
        with pytest.raises(psycopg.Error) as exc_info:
            conn.execute(f"INSERT INTO public.chamak_generations (wholesaler_id, mode) VALUES ('{w_user}', 'set_creation');")
        assert exc_info.value.sqlstate == 'PT403'

        # 5. Bypass setting allows insert even under mode 'enforce'
        with psycopg.connect(db_conn) as tx_conn:
            tx_conn.autocommit = False
            with tx_conn.cursor() as cur:
                cur.execute('SET ROLE service_role;')
                cur.execute("SET LOCAL jewelindia.feature_bypass = 'on';")
                cur.execute(f"INSERT INTO public.chamak_generations (wholesaler_id, mode) VALUES ('{w_user}', 'fusion');")
            tx_conn.commit()

def test_rollback_032(db_conn):
    with psycopg.connect(db_conn, autocommit=True) as conn:
        conn.execute('RESET ROLE;')
        with open(ROLLBACK_032) as f:
            conn.execute(f.read())
        # Function and trigger should be dropped
        res = conn.execute("SELECT to_regprocedure('public.check_chamak_feature_controls()');").fetchone()[0]
        assert res is None

from unittest.mock import AsyncMock, patch
import httpx
from unittest.mock import AsyncMock, patch
from app.main import app

@pytest.mark.asyncio
async def test_chamak_api_blocked_before_credits():
    transport = httpx.ASGITransport(app=app)
    user_id = str(uuid.uuid4())
    generation_id = str(uuid.uuid4())
    locked_msg = "Chamak has been locked for your account by Jewel India admin. Contact support to unlock it."

    with patch("app.features.get_feature_status", return_value=(True, locked_msg)), \
         patch("app.auth.resolve_user_id", return_value=user_id), \
         patch("app.main._charge_or_reject", new_callable=AsyncMock) as mock_charge:

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer token-{user_id}"}
            resp = await client.post(
                "/api/chamak/generate",
                json={"generation_id": generation_id},
                headers=headers
            )

            # Must reject with 403 and plain string detail
            assert resp.status_code == 403
            assert resp.json()["detail"] == locked_msg
            assert resp.headers.get("X-Feature-Disabled") == "chamak"

            # Crucial check: _charge_or_reject must NOT have been called!
            assert mock_charge.call_count == 0

@pytest.mark.asyncio
async def test_set_creation_api_blocked_before_credits():
    transport = httpx.ASGITransport(app=app)
    user_id = str(uuid.uuid4())
    generation_id = str(uuid.uuid4())
    locked_msg = "Set Creation has been locked for your account by Jewel India admin. Contact support to unlock it."

    with patch("app.features.get_feature_status", return_value=(True, locked_msg)), \
         patch("app.auth.resolve_user_id", return_value=user_id), \
         patch("app.main._charge_or_reject", new_callable=AsyncMock) as mock_charge:

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer token-{user_id}"}
            resp = await client.post(
                "/api/set-creation/generate",
                json={"generation_id": generation_id},
                headers=headers
            )

            # Must reject with 403 and plain string detail
            assert resp.status_code == 403
            assert resp.json()["detail"] == locked_msg
            assert resp.headers.get("X-Feature-Disabled") == "set_creation"

            # Crucial check: _charge_or_reject must NOT have been called!
            assert mock_charge.call_count == 0

