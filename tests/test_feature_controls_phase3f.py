"""Tests for Phase 3f: Messages Feature Controls Enforcement Triggers.

Validates:
1. When wholesaler has 'messages' enabled, INSERT into conversations and messages succeeds.
2. When wholesaler has 'messages' disabled and catalog mode is 'enforce':
   - INSERT into conversations fails with code PT403 (FEATURE_DISABLED).
   - INSERT into messages fails with code PT403 (FEATURE_DISABLED).
3. When catalog mode is 'log' or 'off':
   - INSERT into conversations and messages succeeds even if wholesaler override is false.
4. Transaction-local bypass `SET LOCAL jewelindia.feature_bypass = 'on'` allows INSERT even when enforced and disabled.
5. Rollback cleanly drops triggers and functions.
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
MIGRATION_035 = REPO_ROOT / "migrations" / "035_feature_controls_messages.sql"
ROLLBACK_035 = REPO_ROOT / "migrations" / "035_feature_controls_messages.rollback.sql"

import pgserver

@pytest.fixture(scope="session")
def pg_server():
    temp_dir = tempfile.mkdtemp(prefix="jewel-fc-3f-")
    server = pgserver.get_server(temp_dir, cleanup_mode="stop")
    yield server
    server.cleanup()

@pytest.fixture
def db_conn(pg_server):
    admin_uri = pg_server.get_uri()
    db_name = f"test_fc3f_{uuid.uuid4().hex[:8]}"
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
            CREATE OR REPLACE FUNCTION auth.uid() RETURNS UUID LANGUAGE sql STABLE AS $$
                SELECT NULLIF(current_setting('request.jwt.claim.sub', true), '')::uuid;
            $$;
            CREATE OR REPLACE FUNCTION auth.role() RETURNS TEXT LANGUAGE sql STABLE AS $$
                SELECT COALESCE(NULLIF(current_setting('request.jwt.claim.role', true), ''), 'anon');
            $$;

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

            CREATE TABLE IF NOT EXISTS public.conversations (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                product_id UUID REFERENCES public.products(id),
                wholesaler_id UUID NOT NULL,
                employee_id UUID REFERENCES public.employees(id),
                retailer_id UUID REFERENCES public.retailers(id),
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            );

            CREATE TABLE IF NOT EXISTS public.messages (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                conversation_id UUID REFERENCES public.conversations(id) ON DELETE CASCADE,
                sender_type TEXT,
                sender_id UUID NOT NULL,
                content TEXT NOT NULL,
                is_read BOOLEAN DEFAULT false,
                created_at TIMESTAMPTZ DEFAULT now()
            );

            GRANT USAGE ON SCHEMA auth TO anon, authenticated, service_role;
            GRANT ALL ON ALL TABLES IN SCHEMA auth TO anon, authenticated, service_role;
            GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
            GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
        """)

        # Apply 030 and 035
        with open(MIGRATION_030) as f:
            conn.execute(f.read())
        with open(MIGRATION_035) as f:
            conn.execute(f.read())

    yield db_uri

    with psycopg.connect(admin_uri, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {db_name}")

def test_messages_trigger_enforcement(db_conn):
    with psycopg.connect(db_conn, autocommit=True) as conn:
        conn.execute("SET ROLE service_role;")

        # Setup Wholesaler & Retailer & Employee & Product
        w_user = uuid.uuid4()
        conn.execute(f"INSERT INTO auth.users (id, email) VALUES ('{w_user}', 'w_msg@test.com');")
        w_id = conn.execute(f"INSERT INTO public.wholesalers (user_id, company_name) VALUES ('{w_user}', 'Wholesale Msg') RETURNING id;").fetchone()[0]

        ret_user = uuid.uuid4()
        conn.execute(f"INSERT INTO auth.users (id, email) VALUES ('{ret_user}', 'r_msg@test.com');")
        ret_id = conn.execute(f"INSERT INTO public.retailers (user_id, store_name) VALUES ('{ret_user}', 'Store Msg') RETURNING id;").fetchone()[0]

        emp_user = uuid.uuid4()
        conn.execute(f"INSERT INTO auth.users (id, email) VALUES ('{emp_user}', 'e_msg@test.com');")
        emp_id = conn.execute(f"INSERT INTO public.employees (auth_user_id, retailer_id) VALUES ('{emp_user}', '{ret_id}') RETURNING id;").fetchone()[0]

        prod_id = uuid.uuid4()
        conn.execute(f"INSERT INTO public.products (id, wholesaler_id, title) VALUES ('{prod_id}', '{w_user}', 'Necklace');")

        # 1. Feature default enabled -> create conversation and message succeeds
        conv1_id = uuid.uuid4()
        conn.execute(f"""
            INSERT INTO public.conversations (id, product_id, wholesaler_id, employee_id, retailer_id)
            VALUES ('{conv1_id}', '{prod_id}', '{w_user}', '{emp_id}', '{ret_id}');
        """)

        msg1_id = uuid.uuid4()
        conn.execute(f"""
            INSERT INTO public.messages (id, conversation_id, sender_type, sender_id, content)
            VALUES ('{msg1_id}', '{conv1_id}', 'employee', '{emp_user}', 'Hello!');
        """)
        assert conn.execute(f"SELECT COUNT(*) FROM public.messages WHERE id = '{msg1_id}';").fetchone()[0] == 1

        # 2. Disable 'messages' for wholesaler and set catalog to 'enforce'
        conn.execute(f"SELECT public.admin_set_feature('{w_user}', 'messages', false, 'Spam blocked', 'admin', 0);")
        conn.execute("SELECT public.admin_set_catalog('messages', true, 'enforce', 'Enforcing messages', 'admin');")

        # 3. New conversation creation must fail with PT403
        conv2_id = uuid.uuid4()
        with pytest.raises(psycopg.Error) as exc_conv:
            conn.execute(f"""
                INSERT INTO public.conversations (id, product_id, wholesaler_id, employee_id, retailer_id)
                VALUES ('{conv2_id}', '{prod_id}', '{w_user}', '{emp_id}', '{ret_id}');
            """)
        assert exc_conv.value.sqlstate == 'PT403'

        # 4. New message in existing conversation must fail with PT403
        msg2_id = uuid.uuid4()
        with pytest.raises(psycopg.Error) as exc_msg:
            conn.execute(f"""
                INSERT INTO public.messages (id, conversation_id, sender_type, sender_id, content)
                VALUES ('{msg2_id}', '{conv1_id}', 'employee', '{emp_user}', 'Can you see this?');
            """)
        assert exc_msg.value.sqlstate == 'PT403'

        # 5. Mode 'log' -> insert succeeds
        conn.execute("SELECT public.admin_set_catalog('messages', true, 'log', 'Log only', 'admin');")
        msg3_id = uuid.uuid4()
        conn.execute(f"""
            INSERT INTO public.messages (id, conversation_id, sender_type, sender_id, content)
            VALUES ('{msg3_id}', '{conv1_id}', 'employee', '{emp_user}', 'Log mode msg');
        """)
        assert conn.execute(f"SELECT COUNT(*) FROM public.messages WHERE id = '{msg3_id}';").fetchone()[0] == 1

        # 6. Mode 'off' -> insert succeeds
        conn.execute("SELECT public.admin_set_catalog('messages', true, 'off', 'Off mode', 'admin');")
        msg4_id = uuid.uuid4()
        conn.execute(f"""
            INSERT INTO public.messages (id, conversation_id, sender_type, sender_id, content)
            VALUES ('{msg4_id}', '{conv1_id}', 'employee', '{emp_user}', 'Off mode msg');
        """)
        assert conn.execute(f"SELECT COUNT(*) FROM public.messages WHERE id = '{msg4_id}';").fetchone()[0] == 1

        # 7. Re-enforce, but use transaction bypass `SET LOCAL jewelindia.feature_bypass = 'on'`
        conn.execute("SELECT public.admin_set_catalog('messages', true, 'enforce', 'Enforcing messages', 'admin');")
        msg5_id = uuid.uuid4()
        with psycopg.connect(db_conn, autocommit=False) as tx_conn:
            tx_conn.execute("SET ROLE service_role;")
            tx_conn.execute("SET LOCAL jewelindia.feature_bypass = 'on';")
            tx_conn.execute(f"""
                INSERT INTO public.messages (id, conversation_id, sender_type, sender_id, content)
                VALUES ('{msg5_id}', '{conv1_id}', 'employee', '{emp_user}', 'Bypassed msg');
            """)
            tx_conn.commit()
        assert conn.execute(f"SELECT COUNT(*) FROM public.messages WHERE id = '{msg5_id}';").fetchone()[0] == 1

def test_messages_rollback(db_conn):
    with psycopg.connect(db_conn, autocommit=True) as conn:
        conn.execute("SET ROLE service_role;")
        w_user = uuid.uuid4()
        conn.execute(f"INSERT INTO auth.users (id, email) VALUES ('{w_user}', 'w_rb2@test.com');")
        prod_id = uuid.uuid4()
        conn.execute(f"INSERT INTO public.products (id, wholesaler_id, title) VALUES ('{prod_id}', '{w_user}', 'Chain');")

        # Disable and enforce
        conn.execute(f"SELECT public.admin_set_feature('{w_user}', 'messages', false, 'Disabled', 'admin', 0);")
        conn.execute("SELECT public.admin_set_catalog('messages', true, 'enforce', 'Enforcing', 'admin');")

        # Rollback migration 035 as superuser
        conn.execute("RESET ROLE;")
        with open(ROLLBACK_035) as f:
            conn.execute(f.read())
        conn.execute("SET ROLE service_role;")

        # Now insert succeeds even when feature is disabled
        conv_id = uuid.uuid4()
        conn.execute(f"""
            INSERT INTO public.conversations (id, product_id, wholesaler_id)
            VALUES ('{conv_id}', '{prod_id}', '{w_user}');
        """)
        assert conn.execute(f"SELECT COUNT(*) FROM public.conversations WHERE id = '{conv_id}';").fetchone()[0] == 1
