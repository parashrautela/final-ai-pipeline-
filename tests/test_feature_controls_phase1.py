import os
import pathlib
import pytest
import tempfile
import uuid
import pgserver
import psycopg

MIGRATION_DIR = pathlib.Path(__file__).resolve().parent.parent / 'migrations'
MIGRATION_SQL = MIGRATION_DIR / '030_feature_controls.sql'
ROLLBACK_SQL = MIGRATION_DIR / '030_feature_controls.rollback.sql'

@pytest.fixture(scope='session')
def pg_server():
    temp_dir = tempfile.mkdtemp(prefix='jewel-feat-ctrl-test-')
    server = pgserver.get_server(temp_dir, cleanup_mode='stop')
    yield server
    server.cleanup()

@pytest.fixture
def db_conn(pg_server):
    admin_uri = pg_server.get_uri()
    db_name = f'test_fc_{uuid.uuid4().hex[:8]}'
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

            CREATE SCHEMA IF NOT EXISTS auth;
            CREATE TABLE IF NOT EXISTS auth.users (
                id UUID PRIMARY KEY,
                email TEXT
            );

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
                verification_status TEXT NOT NULL DEFAULT 'pending'
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

            GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
            GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
        """)

        # Apply migration 030
        with open(MIGRATION_SQL) as f:
            conn.execute(f.read())

    yield db_uri

    with psycopg.connect(admin_uri, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS {db_name}')

def test_privileges_and_rls(db_conn):
    with psycopg.connect(db_conn, autocommit=True) as conn:
        # 1. As anon: cannot SELECT from tables
        for tbl in ['feature_catalog', 'wholesaler_feature_overrides', 'feature_flag_audit']:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f'SET ROLE anon; SELECT * FROM public.{tbl};')

        # 2. As authenticated: cannot SELECT from tables
        for tbl in ['feature_catalog', 'wholesaler_feature_overrides', 'feature_flag_audit']:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f'SET ROLE authenticated; SELECT * FROM public.{tbl};')

        # 3. As anon: cannot execute admin or enforcement functions
        for fn in [
            "admin_set_feature('00000000-0000-0000-0000-000000000001'::uuid, 'chamak', true, 'reason', 'actor', 0)",
            "admin_reset_feature('00000000-0000-0000-0000-000000000001'::uuid, 'chamak', 'reason', 'actor', 0)",
            "admin_set_catalog('chamak', true, 'off', 'reason', 'actor')",
            "admin_list_features('00000000-0000-0000-0000-000000000001'::uuid)",
            "feature_enabled('00000000-0000-0000-0000-000000000001'::uuid, 'chamak')",
            "feature_blocked('00000000-0000-0000-0000-000000000001'::uuid, 'chamak')",
            "assert_feature('00000000-0000-0000-0000-000000000001'::uuid, 'chamak')",
            "feature_owner('00000000-0000-0000-0000-000000000001'::uuid)"
        ]:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f'SET ROLE anon; SELECT public.{fn};')

        # 4. As authenticated: cannot execute admin functions
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SET ROLE authenticated; SELECT public.admin_set_feature('00000000-0000-0000-0000-000000000001'::uuid, 'chamak', true, 'reason', 'actor', 0);")

        # 5. As authenticated: CAN execute my_features()
        conn.execute("""
            SET ROLE authenticated;
            SELECT set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-000000000001', false);
            SELECT set_config('request.jwt.claim.role', 'authenticated', false);
        """)
        res = conn.execute('SELECT public.my_features();').fetchone()[0]
        assert isinstance(res, dict)
        assert res.get('chamak') is True

def test_audit_append_only(db_conn):
    with psycopg.connect(db_conn, autocommit=True) as conn:
        conn.execute('SET ROLE service_role;')
        # Can insert
        conn.execute("""
            INSERT INTO public.feature_flag_audit (wholesaler_user_id, feature_key, action, old_value, new_value, reason, actor)
            VALUES (gen_random_uuid(), 'chamak', 'set', null, '{"enabled": true}'::jsonb, 'initial test', 'admin');
        """)
        # Cannot update
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE public.feature_flag_audit SET reason = 'tampered';")
        # Cannot delete
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute('DELETE FROM public.feature_flag_audit;')

def test_feature_defaults_and_overrides(db_conn):
    with psycopg.connect(db_conn, autocommit=True) as conn:
        conn.execute('SET ROLE service_role;')
        w1_user = uuid.uuid4()
        w2_user = uuid.uuid4()
        conn.execute(f"INSERT INTO public.wholesalers (user_id, company_name) VALUES ('{w1_user}', 'W1'), ('{w2_user}', 'W2');")

        # All 7 features default to true
        for key in ['chamak', 'set_creation', 'publish_to_retailer', 'publish_to_wholesaler', 'custom_inquiries', 'orders', 'messages']:
            assert conn.execute(f"SELECT public.feature_enabled('{w1_user}', '{key}');").fetchone()[0] is True

        # Override w1 chamak to false
        res = conn.execute(f"SELECT public.admin_set_feature('{w1_user}', 'chamak', false, 'Policy violation', 'admin', 0);").fetchone()[0]
        assert res['ok'] is True
        assert res['version'] == 1

        # w1 chamak is false, w2 chamak is still true
        assert conn.execute(f"SELECT public.feature_enabled('{w1_user}', 'chamak');").fetchone()[0] is False
        assert conn.execute(f"SELECT public.feature_enabled('{w2_user}', 'chamak');").fetchone()[0] is True

def test_hierarchy_chamak_set_creation(db_conn):
    with psycopg.connect(db_conn, autocommit=True) as conn:
        conn.execute('SET ROLE service_role;')
        w_user = uuid.uuid4()
        conn.execute(f"INSERT INTO public.wholesalers (user_id, company_name) VALUES ('{w_user}', 'W Hierarchy');")

        # Initially both true
        assert conn.execute(f"SELECT public.feature_enabled('{w_user}', 'chamak');").fetchone()[0] is True
        assert conn.execute(f"SELECT public.feature_enabled('{w_user}', 'set_creation');").fetchone()[0] is True

        # Disabling parent chamak automatically disables child set_creation
        conn.execute(f"SELECT public.admin_set_feature('{w_user}', 'chamak', false, 'Disable chamak', 'admin', 0);")
        assert conn.execute(f"SELECT public.feature_enabled('{w_user}', 'chamak');").fetchone()[0] is False
        assert conn.execute(f"SELECT public.feature_enabled('{w_user}', 'set_creation');").fetchone()[0] is False

        # Even if set_creation explicitly overridden to true, parent off forces child off
        conn.execute(f"SELECT public.admin_set_feature('{w_user}', 'set_creation', true, 'Explicit enable child', 'admin', 0);")
        assert conn.execute(f"SELECT public.feature_enabled('{w_user}', 'set_creation');").fetchone()[0] is False

        # Re-enabling chamak restores child set_creation
        conn.execute(f"SELECT public.admin_set_feature('{w_user}', 'chamak', true, 'Re-enable chamak', 'admin', 1);")
        assert conn.execute(f"SELECT public.feature_enabled('{w_user}', 'chamak');").fetchone()[0] is True
        assert conn.execute(f"SELECT public.feature_enabled('{w_user}', 'set_creation');").fetchone()[0] is True

        # Disabling set_creation alone leaves chamak true
        conn.execute(f"SELECT public.admin_set_feature('{w_user}', 'set_creation', false, 'Disable child only', 'admin', 1);")
        assert conn.execute(f"SELECT public.feature_enabled('{w_user}', 'chamak');").fetchone()[0] is True
        assert conn.execute(f"SELECT public.feature_enabled('{w_user}', 'set_creation');").fetchone()[0] is False

def test_configured_vs_enforced_modes(db_conn):
    with psycopg.connect(db_conn, autocommit=True) as conn:
        conn.execute('SET ROLE service_role;')
        w_user = uuid.uuid4()
        conn.execute(f"INSERT INTO public.wholesalers (user_id, company_name) VALUES ('{w_user}', 'W Modes');")

        # Disable orders for w_user
        conn.execute(f"SELECT public.admin_set_feature('{w_user}', 'orders', false, 'Testing modes', 'admin', 0);")

        # 1. Mode 'off' (default)
        assert conn.execute(f"SELECT public.feature_enabled('{w_user}', 'orders');").fetchone()[0] is False
        assert conn.execute(f"SELECT public.feature_blocked('{w_user}', 'orders');").fetchone()[0] is False
        # assert_feature does not raise
        conn.execute(f"SELECT public.assert_feature('{w_user}', 'orders');")

        # 2. Mode 'log'
        conn.execute("SELECT public.admin_set_catalog('orders', true, 'log', 'Testing log mode', 'admin');")
        assert conn.execute(f"SELECT public.feature_enabled('{w_user}', 'orders');").fetchone()[0] is False
        assert conn.execute(f"SELECT public.feature_blocked('{w_user}', 'orders');").fetchone()[0] is False
        conn.execute(f"SELECT public.assert_feature('{w_user}', 'orders');")

        # 3. Mode 'enforce'
        conn.execute("SELECT public.admin_set_catalog('orders', true, 'enforce', 'Testing enforce mode', 'admin');")
        assert conn.execute(f"SELECT public.feature_enabled('{w_user}', 'orders');").fetchone()[0] is False
        assert conn.execute(f"SELECT public.feature_blocked('{w_user}', 'orders');").fetchone()[0] is True
        with pytest.raises(psycopg.Error) as exc_info:
            conn.execute(f"SELECT public.assert_feature('{w_user}', 'orders');")
        assert exc_info.value.sqlstate == 'PT403'
        assert 'FEATURE_DISABLED' in str(exc_info.value)

def test_bypass_setting(db_conn):
    with psycopg.connect(db_conn) as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute('SET ROLE service_role;')
            w_user = uuid.uuid4()
            cur.execute(f"INSERT INTO public.wholesalers (user_id, company_name) VALUES ('{w_user}', 'W Bypass');")
            cur.execute(f"SELECT public.admin_set_feature('{w_user}', 'orders', false, 'Disable', 'admin', 0);")
            cur.execute("SELECT public.admin_set_catalog('orders', true, 'enforce', 'Enforce', 'admin');")

            # Blocked normally
            cur.execute(f"SELECT public.feature_blocked('{w_user}', 'orders');")
            assert cur.fetchone()[0] is True

            # Enable bypass transaction-locally
            cur.execute("SET LOCAL jewelindia.feature_bypass = 'on';")
            cur.execute(f"SELECT public.feature_blocked('{w_user}', 'orders');")
            assert cur.fetchone()[0] is False
            cur.execute(f"SELECT public.assert_feature('{w_user}', 'orders');")

        conn.commit()

        # In a new transaction, bypass is automatically gone
        with conn.cursor() as cur:
            cur.execute(f"SELECT public.feature_blocked('{w_user}', 'orders');")
            assert cur.fetchone()[0] is True

def test_admin_set_and_reset_validations(db_conn):
    with psycopg.connect(db_conn, autocommit=True) as conn:
        conn.execute('SET ROLE service_role;')
        w_user = uuid.uuid4()
        # Wholesaler in pending state (correction 3: not requiring verified status)
        conn.execute(f"INSERT INTO public.wholesalers (user_id, company_name, verification_status) VALUES ('{w_user}', 'Pending Wholesaler', 'pending');")

        # 1. Invalid reasons
        res = conn.execute(f"SELECT public.admin_set_feature('{w_user}', 'orders', false, '', 'admin', 0);").fetchone()[0]
        assert res == {'ok': False, 'error': 'INVALID_REASON'}
        res = conn.execute(f"SELECT public.admin_set_feature('{w_user}', 'orders', false, '{'x'*501}', 'admin', 0);").fetchone()[0]
        assert res == {'ok': False, 'error': 'INVALID_REASON'}

        # Verify no audit row written for failed validation
        audit_count = conn.execute("SELECT count(*) FROM public.feature_flag_audit WHERE wholesaler_user_id = %s;", (w_user,)).fetchone()[0]
        assert audit_count == 0

        # 2. Unknown feature
        res = conn.execute(f"SELECT public.admin_set_feature('{w_user}', 'nonexistent_key', false, 'reason', 'admin', 0);").fetchone()[0]
        assert res == {'ok': False, 'error': 'UNKNOWN_FEATURE'}

        # 3. Not a wholesaler
        random_user = uuid.uuid4()
        res = conn.execute(f"SELECT public.admin_set_feature('{random_user}', 'orders', false, 'reason', 'admin', 0);").fetchone()[0]
        assert res == {'ok': False, 'error': 'NOT_A_WHOLESALER'}

        # 4. Valid set on pending wholesaler works
        res = conn.execute(f"SELECT public.admin_set_feature('{w_user}', 'orders', false, 'First override', 'admin', 0);").fetchone()[0]
        assert res == {'ok': True, 'version': 1}

        # 5. Version conflict
        res = conn.execute(f"SELECT public.admin_set_feature('{w_user}', 'orders', true, 'Conflict test', 'admin', 0);").fetchone()[0]
        assert res['ok'] is False
        assert res['error'] == 'VERSION_CONFLICT'
        assert res['current_version'] == 1

        # 6. Valid update with expected version 1 -> version 2
        res = conn.execute(f"SELECT public.admin_set_feature('{w_user}', 'orders', true, 'Second override', 'admin', 1);").fetchone()[0]
        assert res == {'ok': True, 'version': 2}

        # 7. Reset to default
        res = conn.execute(f"SELECT public.admin_reset_feature('{w_user}', 'orders', 'Resetting', 'admin', 2);").fetchone()[0]
        assert res == {'ok': True, 'version': 0}

        # Check audit rows: set(v1), set(v2), reset
        rows = conn.execute("SELECT action, old_value, new_value FROM public.feature_flag_audit WHERE wholesaler_user_id = %s ORDER BY id;", (w_user,)).fetchall()
        assert len(rows) == 3
        assert rows[0][0] == 'set' and rows[0][1] is None and rows[0][2]['version'] == 1
        assert rows[1][0] == 'set' and rows[1][1]['version'] == 1 and rows[1][2]['version'] == 2
        assert rows[2][0] == 'reset' and rows[2][1]['version'] == 2 and rows[2][2] is None

def test_admin_list_features(db_conn):
    with psycopg.connect(db_conn, autocommit=True) as conn:
        conn.execute('SET ROLE service_role;')
        w_user = uuid.uuid4()
        conn.execute(f"INSERT INTO public.wholesalers (user_id, company_name) VALUES ('{w_user}', 'W List');")

        # Disable chamak
        conn.execute(f"SELECT public.admin_set_feature('{w_user}', 'chamak', false, 'Chamak off', 'admin', 0);")

        res = conn.execute(f"SELECT public.admin_list_features('{w_user}');").fetchone()[0]
        assert res['ok'] is True
        features = {f['key']: f for f in res['features']}

        assert len(features) == 7
        assert features['chamak']['override_enabled'] is False
        assert features['chamak']['effective_enabled'] is False
        assert features['chamak']['parent_locked'] is False

        # set_creation is parent_locked because chamak is off
        assert features['set_creation']['override_enabled'] is None
        assert features['set_creation']['effective_enabled'] is False
        assert features['set_creation']['parent_locked'] is True

def test_idempotency_and_preservation(db_conn):
    with psycopg.connect(db_conn, autocommit=True) as conn:
        conn.execute('SET ROLE service_role;')
        # Admin customizes catalog
        conn.execute("UPDATE public.feature_catalog SET default_enabled = false, enforcement_mode = 'enforce' WHERE key = 'messages';")

        # Re-apply migration as postgres/migration-runner
        conn.execute('RESET ROLE;')
        with open(MIGRATION_SQL) as f:
            conn.execute(f.read())

        row = conn.execute("SELECT default_enabled, enforcement_mode FROM public.feature_catalog WHERE key = 'messages';").fetchone()
        assert row[0] is False
        assert row[1] == 'enforce'

def test_rollback(db_conn):
    with psycopg.connect(db_conn, autocommit=True) as conn:
        conn.execute('RESET ROLE;')
        with open(ROLLBACK_SQL) as f:
            conn.execute(f.read())

        for tbl in ['feature_catalog', 'wholesaler_feature_overrides', 'feature_flag_audit']:
            res = conn.execute(f"SELECT to_regclass('public.{tbl}');").fetchone()[0]
            assert res is None
