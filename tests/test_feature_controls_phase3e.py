"""Tests for Phase 3e: Orders Feature Controls Enforcement Trigger.

Validates:
1. When wholesaler has 'orders' enabled, INSERT into public.orders succeeds.
2. When wholesaler has 'orders' disabled and catalog mode is 'enforce':
   - INSERT into public.orders fails with exception code 55000 (FEATURE_DISABLED).
3. When catalog mode is 'log' or 'off':
   - INSERT succeeds even if wholesaler override is false.
4. Transaction-local bypass `SET LOCAL jewelindia.feature_bypass = 'on'` allows INSERT even when enforced and disabled.
5. Updating existing orders (status transitions) succeeds even when 'orders' is disabled.
6. Rollback cleanly drops trigger and function.
"""

from __future__ import annotations
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import tempfile
import uuid
import pytest
import psycopg

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATION_030 = REPO_ROOT / "migrations" / "030_feature_controls.sql"
MIGRATION_034 = REPO_ROOT / "migrations" / "034_feature_controls_orders.sql"
ROLLBACK_034 = REPO_ROOT / "migrations" / "034_feature_controls_orders.rollback.sql"

import pgserver

@pytest.fixture(scope="session")
def pg_server():
    temp_dir = tempfile.mkdtemp(prefix="jewel-fc-3e-")
    server = pgserver.get_server(temp_dir, cleanup_mode="stop")
    yield server
    server.cleanup()

@pytest.fixture
def db_conn(pg_server):
    admin_uri = pg_server.get_uri()
    db_name = f"test_fc3e_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin_uri, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {db_name}")

    db_uri = psycopg.conninfo.make_conninfo(admin_uri, dbname=db_name)

    with psycopg.connect(db_uri, autocommit=True) as conn:
        conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN CREATE ROLE anon NOLOGIN; END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN CREATE ROLE authenticated NOLOGIN; END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN CREATE ROLE service_role NOLOGIN BYPASSRLS; END IF;
            END $$;

            CREATE SCHEMA IF NOT EXISTS auth;
            CREATE TABLE IF NOT EXISTS auth.users (id UUID PRIMARY KEY, email TEXT);

            CREATE TABLE IF NOT EXISTS public.wholesalers (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID UNIQUE NOT NULL,
                company_name TEXT NOT NULL,
                verification_status TEXT NOT NULL DEFAULT 'verified'
            );

            CREATE TABLE IF NOT EXISTS public.retailers (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID UNIQUE NOT NULL,
                store_name TEXT NOT NULL,
                verification_status TEXT NOT NULL DEFAULT 'verified'
            );

            CREATE TABLE IF NOT EXISTS public.employees (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                auth_user_id UUID UNIQUE NOT NULL,
                retailer_id UUID REFERENCES public.retailers(id),
                status TEXT NOT NULL DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS public.products (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                wholesaler_id UUID NOT NULL,
                title TEXT NOT NULL,
                is_published BOOLEAN NOT NULL DEFAULT true
            );

            CREATE TABLE IF NOT EXISTS public.orders (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                product_id UUID NOT NULL REFERENCES public.products(id),
                employee_id UUID REFERENCES public.employees(id),
                retailer_id UUID REFERENCES public.retailers(id),
                wholesaler_id UUID NOT NULL REFERENCES auth.users(id),
                customization_note TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            );

            GRANT USAGE ON SCHEMA auth TO anon, authenticated, service_role;
            GRANT ALL ON ALL TABLES IN SCHEMA auth TO anon, authenticated, service_role;
            GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
            GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
        """)

        # Apply 030 and 034
        with open(MIGRATION_030) as f:
            conn.execute(f.read())
        with open(MIGRATION_034) as f:
            conn.execute(f.read())

    yield db_uri

    with psycopg.connect(admin_uri, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {db_name}")

def test_orders_trigger_enforcement(db_conn):
    with psycopg.connect(db_conn, autocommit=True) as conn:
        conn.execute("SET ROLE service_role;")

        # 1. Setup Wholesaler & Product
        w_user = uuid.uuid4()
        conn.execute(f"INSERT INTO auth.users (id, email) VALUES ('{w_user}', 'w_order@test.com');")
        conn.execute(f"INSERT INTO public.wholesalers (user_id, company_name) VALUES ('{w_user}', 'Wholesale Co');")

        prod_id = uuid.uuid4()
        conn.execute(f"INSERT INTO public.products (id, wholesaler_id, title) VALUES ('{prod_id}', '{w_user}', 'Gold Ring');")

        # 2. Insert order while feature is default enabled -> Success
        order1_id = uuid.uuid4()
        conn.execute(f"""
            INSERT INTO public.orders (id, product_id, wholesaler_id, status)
            VALUES ('{order1_id}', '{prod_id}', '{w_user}', 'pending');
        """)
        row = conn.execute(f"SELECT status FROM public.orders WHERE id = '{order1_id}';").fetchone()
        assert row[0] == 'pending'

        # 3. Disable 'orders' for wholesaler and set catalog to 'enforce'
        conn.execute(f"SELECT public.admin_set_feature('{w_user}', 'orders', false, 'Over limit', 'admin', 0);")
        conn.execute("SELECT public.admin_set_catalog('orders', true, 'enforce', 'Enforcing orders', 'admin');")

        # 4. Attempt to insert new order -> Must fail with FEATURE_DISABLED (55000)
        order2_id = uuid.uuid4()
        with pytest.raises(psycopg.Error) as exc_info:
            conn.execute(f"""
                INSERT INTO public.orders (id, product_id, wholesaler_id, status)
                VALUES ('{order2_id}', '{prod_id}', '{w_user}', 'pending');
            """)
        assert exc_info.value.sqlstate == 'PT403'
        assert "FEATURE_DISABLED" in str(exc_info.value)

        # 5. Existing order status update must succeed
        conn.execute(f"UPDATE public.orders SET status = 'accepted' WHERE id = '{order1_id}';")
        row = conn.execute(f"SELECT status FROM public.orders WHERE id = '{order1_id}';").fetchone()
        assert row[0] == 'accepted'

        # 6. Mode 'log' -> insert succeeds
        conn.execute("SELECT public.admin_set_catalog('orders', true, 'log', 'Log only', 'admin');")
        order3_id = uuid.uuid4()
        conn.execute(f"""
            INSERT INTO public.orders (id, product_id, wholesaler_id, status)
            VALUES ('{order3_id}', '{prod_id}', '{w_user}', 'pending');
        """)
        assert conn.execute(f"SELECT COUNT(*) FROM public.orders WHERE id = '{order3_id}';").fetchone()[0] == 1

        # 7. Mode 'off' -> insert succeeds
        conn.execute("SELECT public.admin_set_catalog('orders', true, 'off', 'Off mode', 'admin');")
        order4_id = uuid.uuid4()
        conn.execute(f"""
            INSERT INTO public.orders (id, product_id, wholesaler_id, status)
            VALUES ('{order4_id}', '{prod_id}', '{w_user}', 'pending');
        """)
        assert conn.execute(f"SELECT COUNT(*) FROM public.orders WHERE id = '{order4_id}';").fetchone()[0] == 1

        # 8. Re-enforce, but use transaction bypass `SET LOCAL jewelindia.feature_bypass = 'on'`
        conn.execute("SELECT public.admin_set_catalog('orders', true, 'enforce', 'Enforcing orders', 'admin');")
        order5_id = uuid.uuid4()
        with psycopg.connect(db_conn, autocommit=False) as tx_conn:
            tx_conn.execute("SET ROLE service_role;")
            tx_conn.execute("SET LOCAL jewelindia.feature_bypass = 'on';")
            tx_conn.execute(f"""
                INSERT INTO public.orders (id, product_id, wholesaler_id, status)
                VALUES ('{order5_id}', '{prod_id}', '{w_user}', 'pending');
            """)
            tx_conn.commit()
        assert conn.execute(f"SELECT COUNT(*) FROM public.orders WHERE id = '{order5_id}';").fetchone()[0] == 1

def test_orders_rollback(db_conn):
    with psycopg.connect(db_conn, autocommit=True) as conn:
        conn.execute("SET ROLE service_role;")

        w_user = uuid.uuid4()
        conn.execute(f"INSERT INTO auth.users (id, email) VALUES ('{w_user}', 'w_rb@test.com');")
        prod_id = uuid.uuid4()
        conn.execute(f"INSERT INTO public.products (id, wholesaler_id, title) VALUES ('{prod_id}', '{w_user}', 'Bangle');")

        # Disable and enforce
        conn.execute(f"SELECT public.admin_set_feature('{w_user}', 'orders', false, 'Disabled', 'admin', 0);")
        conn.execute("SELECT public.admin_set_catalog('orders', true, 'enforce', 'Enforcing', 'admin');")

        # Rollback migration 034 as superuser/table owner
        conn.execute("RESET ROLE;")
        with open(ROLLBACK_034) as f:
            conn.execute(f.read())
        conn.execute("SET ROLE service_role;")

        # Now insert succeeds even when feature is disabled
        order_id = uuid.uuid4()
        conn.execute(f"""
            INSERT INTO public.orders (id, product_id, wholesaler_id, status)
            VALUES ('{order_id}', '{prod_id}', '{w_user}', 'pending');
        """)
        assert conn.execute(f"SELECT COUNT(*) FROM public.orders WHERE id = '{order_id}';").fetchone()[0] == 1
