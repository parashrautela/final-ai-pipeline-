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
MIGRATION_033 = MIGRATION_DIR / '033_feature_controls_publish.sql'
ROLLBACK_033 = MIGRATION_DIR / '033_feature_controls_publish.rollback.sql'

import pgserver

@pytest.fixture(scope='session')
def pg_server():
    temp_dir = tempfile.mkdtemp(prefix='jewel-fc-3c-')
    server = pgserver.get_server(temp_dir, cleanup_mode='stop')
    yield server
    server.cleanup()

@pytest.fixture
def db_conn(pg_server):
    admin_uri = pg_server.get_uri()
    db_name = f'test_fc3c_{uuid.uuid4().hex[:8]}'
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

            CREATE TABLE IF NOT EXISTS public.products (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                wholesaler_id UUID NOT NULL,
                name TEXT NOT NULL,
                is_published BOOLEAN NOT NULL DEFAULT false
            );

            GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
            GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
        """)

        # Apply 030 and 033
        with open(MIGRATION_030) as f:
            conn.execute(f.read())
        with open(MIGRATION_033) as f:
            conn.execute(f.read())

    yield db_uri

    with psycopg.connect(admin_uri, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS {db_name}')

def test_publish_trigger_enforcement(db_conn):
    with psycopg.connect(db_conn, autocommit=True) as conn:
        conn.execute('SET ROLE service_role;')
        w_user = uuid.uuid4()
        conn.execute(f"INSERT INTO public.wholesalers (user_id, company_name) VALUES ('{w_user}', 'Publish Test W');")

        # Disable publish_to_retailer
        conn.execute(f"SELECT public.admin_set_feature('{w_user}', 'publish_to_retailer', false, 'Publish disabled', 'admin', 0);")

        # 1. Mode 'off': publishing succeeds
        prod1 = uuid.uuid4()
        conn.execute(f"INSERT INTO public.products (id, wholesaler_id, name, is_published) VALUES ('{prod1}', '{w_user}', 'Ring 1', true);")

        # 2. Mode 'enforce'
        conn.execute("SELECT public.admin_set_catalog('publish_to_retailer', true, 'enforce', 'Enforcing publish', 'admin');")

        # Inserting published product raises PT403
        prod2 = uuid.uuid4()
        with pytest.raises(psycopg.Error) as exc_info:
            conn.execute(f"INSERT INTO public.products (id, wholesaler_id, name, is_published) VALUES ('{prod2}', '{w_user}', 'Ring 2', true);")
        assert exc_info.value.sqlstate == 'PT403'
        assert 'FEATURE_DISABLED' in str(exc_info.value)

        # Inserting UNPUBLISHED product succeeds
        conn.execute(f"INSERT INTO public.products (id, wholesaler_id, name, is_published) VALUES ('{prod2}', '{w_user}', 'Ring 2', false);")

        # Updating unpublished product to published raises PT403
        with pytest.raises(psycopg.Error) as exc_info:
            conn.execute(f"UPDATE public.products SET is_published = true WHERE id = '{prod2}';")
        assert exc_info.value.sqlstate == 'PT403'

        # Updating other columns (name) on existing published product (prod1) succeeds
        conn.execute(f"UPDATE public.products SET name = 'Renamed Ring 1' WHERE id = '{prod1}';")

        # Unpublishing prod1 always succeeds
        conn.execute(f"UPDATE public.products SET is_published = false WHERE id = '{prod1}';")

        # Bypass allows publishing even when enforce is on
        with psycopg.connect(db_conn) as tx_conn:
            tx_conn.autocommit = False
            with tx_conn.cursor() as cur:
                cur.execute('SET ROLE service_role;')
                cur.execute("SET LOCAL jewelindia.feature_bypass = 'on';")
                cur.execute(f"UPDATE public.products SET is_published = true WHERE id = '{prod2}';")
            tx_conn.commit()

def test_rollback_033(db_conn):
    with psycopg.connect(db_conn, autocommit=True) as conn:
        conn.execute('RESET ROLE;')
        with open(ROLLBACK_033) as f:
            conn.execute(f.read())
        res = conn.execute("SELECT to_regprocedure('public.check_publish_feature_controls()');").fetchone()[0]
        assert res is None
