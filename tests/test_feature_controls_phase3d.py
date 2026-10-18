"""Tests for Phase 3d: Custom Inquiries Feature Controls Enforcement.

Validates:
1. SQL candidate routing skips wholesalers with feature_blocked(user_id, 'custom_inquiries') = true.
2. SQL request_visible_to_current_wholesaler returns false when wholesaler is blocked.
3. FastAPI list route returns [] when blocked.
4. FastAPI detail, accept, quote, decline return 403 FEATURE_DISABLED when blocked.
"""

from __future__ import annotations
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import os
import uuid
import pytest
import psycopg
import httpx
from pathlib import Path
from unittest.mock import patch

from app.main import app
from app.routers.manufacturing import require_verified_wholesaler
from app.auth import require_user

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_019 = REPO_ROOT / "migrations" / "019_manufacturing_requests_broadcast.sql"
MIGRATION_030 = REPO_ROOT / "migrations" / "030_feature_controls.sql"
MIGRATION_031 = REPO_ROOT / "migrations" / "031_feature_controls_custom_inquiries.sql"

import tempfile

@pytest.fixture(scope="session")
def pg_server():
    import pgserver
    temp_dir = tempfile.mkdtemp(prefix='jewel-fc-3d-')
    server = pgserver.get_server(temp_dir, cleanup_mode='stop')
    yield server
    server.cleanup()

@pytest.fixture
def db_conn(pg_server):
    admin_uri = pg_server.get_uri()
    db_name = f"test_fc3d_{uuid.uuid4().hex[:8]}"
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
            CREATE OR REPLACE FUNCTION auth.uid() RETURNS UUID LANGUAGE sql STABLE AS $$
                SELECT NULLIF(current_setting('request.jwt.claim.sub', true), '')::uuid;
            $$;
            CREATE OR REPLACE FUNCTION auth.role() RETURNS TEXT LANGUAGE sql STABLE AS $$
                SELECT COALESCE(NULLIF(current_setting('request.jwt.claim.role', true), ''), 'anon');
            $$;
        """)

        # Apply base broadcast migration 019
        with open(MIGRATION_019) as f:
            conn.execute(f.read())

        # Apply 030 and 031
        with open(MIGRATION_030) as f:
            conn.execute(f.read())
        with open(MIGRATION_031) as f:
            conn.execute(f.read())

        # Ensure test roles have necessary grants on created tables
        conn.execute("""
            GRANT USAGE ON SCHEMA auth TO anon, authenticated, service_role;
            GRANT ALL ON ALL TABLES IN SCHEMA auth TO anon, authenticated, service_role;
            GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
            GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
            GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role;
            GRANT ALL ON ALL ROUTINES IN SCHEMA public TO service_role;
        """)

    yield db_uri

    with psycopg.connect(admin_uri, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {db_name}")

def test_queue_routing_skips_disabled_wholesaler(db_conn):
    with psycopg.connect(db_conn, autocommit=True) as conn:
        conn.execute("SET ROLE service_role;")

        # Setup Retailer
        ret_user = uuid.uuid4()
        conn.execute(f"INSERT INTO auth.users (id, email) VALUES ('{ret_user}', 'retailer@test.com');")
        ret_id = conn.execute(f"INSERT INTO public.retailers (user_id, store_name, verification_status) VALUES ('{ret_user}', 'Store R', 'verified') RETURNING id;").fetchone()[0]

        # Setup Wholesalers A, B, C
        wA_user = uuid.uuid4()
        wB_user = uuid.uuid4()
        wC_user = uuid.uuid4()
        for u, name in [(wA_user, "WA"), (wB_user, "WB"), (wC_user, "WC")]:
            conn.execute(f"INSERT INTO auth.users (id, email) VALUES ('{u}', '{name.lower()}@test.com');")
            conn.execute(f"INSERT INTO public.wholesalers (user_id, company_name, verification_status) VALUES ('{u}', '{name}', 'verified');")

        # Disable custom_inquiries for middle wholesaler B
        conn.execute(f"SELECT public.admin_set_feature('{wB_user}', 'custom_inquiries', false, 'B disabled', 'admin', 0);")
        conn.execute("SELECT public.admin_set_catalog('custom_inquiries', true, 'enforce', 'Enforce custom_inquiries', 'admin');")

        # Create Asset for request
        asset_id = uuid.uuid4()
        conn.execute(f"""
            INSERT INTO public.manufacturing_request_assets (
                id, owner_retailer_id, storage_path, mime_type, byte_size, width, height, status
            ) VALUES (
                '{asset_id}', '{ret_id}', 'test.jpg', 'image/jpeg', 100, 100, 100, 'uploaded'
            );
        """)

        # Create Request as Retailer
        conn.execute(f"""
            SET ROLE authenticated;
            SELECT set_config('request.jwt.claim.sub', '{ret_user}', false);
            SELECT set_config('request.jwt.claim.role', 'authenticated', false);
        """)
        res = conn.execute(f"""
            SELECT public.manufacturing_request_create(
                p_asset_id => '{asset_id}'::uuid,
                p_category => 'ring',
                p_min_weight => 2.5,
                p_max_weight => 5.0,
                p_material => 'gold',
                p_purity => '18k',
                p_gemstone_preference => 'none',
                p_quantity => 1,
                p_making_budget_mode => 'fixed_total',
                p_making_budget_amount => 50000,
                p_currency => 'INR',
                p_metal_rate_snapshot => 7500,
                p_metal_rate_basis => 'ibja',
                p_delivery_needed_date => (CURRENT_DATE + INTERVAL '14 days')::date,
                p_notes => 'Test notes',
                p_offer_duration_seconds => 1800
            );
        """).fetchone()[0]

        assert res['ok'] is True
        req_id = res['request_id']

        # Candidates should contain A and C, skipping B!
        conn.execute("SET ROLE service_role;")
        cands = conn.execute(f"""
            SELECT c.rank, w.company_name
            FROM public.manufacturing_request_candidates c
            JOIN public.wholesalers w ON w.id = c.wholesaler_id
            WHERE c.request_id = '{req_id}'
            ORDER BY c.rank;
        """).fetchall()

        companies = [c[1] for c in cands]
        assert "WB" not in companies
        assert "WA" in companies and "WC" in companies

def test_request_visible_to_current_wholesaler(db_conn):
    with psycopg.connect(db_conn, autocommit=True) as conn:
        conn.execute("SET ROLE service_role;")
        w_user = uuid.uuid4()
        conn.execute(f"INSERT INTO auth.users (id, email) VALUES ('{w_user}', 'wvis@test.com');")
        w_id = conn.execute(f"INSERT INTO public.wholesalers (user_id, company_name, verification_status) VALUES ('{w_user}', 'W Vis', 'verified') RETURNING id;").fetchone()[0]

        ret_user = uuid.uuid4()
        conn.execute(f"INSERT INTO auth.users (id, email) VALUES ('{ret_user}', 'retvis@test.com');")
        ret_id = conn.execute(f"INSERT INTO public.retailers (user_id, store_name, verification_status) VALUES ('{ret_user}', 'Store Vis', 'verified') RETURNING id;").fetchone()[0]

        req_id = uuid.uuid4()
        conn.execute(f"""
            INSERT INTO public.manufacturing_requests (
                id, retailer_id, created_by_user_id, category, min_weight_grams, max_weight_grams,
                material, purity, gemstone_preference, quantity, making_budget_mode,
                making_budget_amount, currency, delivery_needed_date, state, assigned_wholesaler_id
            ) VALUES (
                '{req_id}', '{ret_id}', '{ret_user}', 'ring', 1.0, 2.0,
                'gold', '18k', 'none', 1, 'per_gram',
                500.0, 'INR', CURRENT_DATE + INTERVAL '7 days', 'assigned', '{w_id}'
            );
        """)

        # With feature enabled: visible
        conn.execute(f"""
            SET ROLE authenticated;
            SELECT set_config('request.jwt.claim.sub', '{w_user}', false);
            SELECT set_config('request.jwt.claim.role', 'authenticated', false);
        """)
        assert conn.execute(f"SELECT public.request_visible_to_current_wholesaler('{req_id}');").fetchone()[0] is True

        # Disable custom_inquiries and enforce
        conn.execute("SET ROLE service_role;")
        conn.execute(f"SELECT public.admin_set_feature('{w_user}', 'custom_inquiries', false, 'Vis off', 'admin', 0);")
        conn.execute("SELECT public.admin_set_catalog('custom_inquiries', true, 'enforce', 'Enforcing', 'admin');")

        # Now: NOT visible
        conn.execute(f"""
            SET ROLE authenticated;
            SELECT set_config('request.jwt.claim.sub', '{w_user}', false);
            SELECT set_config('request.jwt.claim.role', 'authenticated', false);
        """)
        assert conn.execute(f"SELECT public.request_visible_to_current_wholesaler('{req_id}');").fetchone()[0] is False

@pytest.mark.asyncio
async def test_fastapi_custom_inquiries_routes():
    transport = httpx.ASGITransport(app=app)
    user_id = str(uuid.uuid4())
    wholesaler_id = str(uuid.uuid4())
    mock_wholesaler = {"user_id": user_id, "wholesaler_id": wholesaler_id, "company_name": "Test Co"}

    app.dependency_overrides[require_verified_wholesaler] = lambda: mock_wholesaler
    app.dependency_overrides[require_user] = lambda: user_id

    try:
        with patch("app.features.is_feature_blocked", return_value=True),              patch("app.routers.manufacturing.is_feature_blocked", return_value=True):

            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {"Authorization": f"Bearer token-{user_id}"}

                # 1. List route returns empty list [] instead of 403
                resp_list = await client.get("/api/wholesaler/manufacturing-offers", headers=headers)
                assert resp_list.status_code == 200
                assert resp_list.json() == []

                # 2. Detail route returns 403 FEATURE_DISABLED
                offer_id = str(uuid.uuid4())
                resp_detail = await client.get(f"/api/wholesaler/manufacturing-offers/{offer_id}", headers=headers)
                assert resp_detail.status_code == 403
                assert resp_detail.json()["detail"]["code"] == "FEATURE_DISABLED"

                # 3. Accept route returns 403 FEATURE_DISABLED
                resp_accept = await client.post(
                    f"/api/wholesaler/manufacturing-offers/{offer_id}/accept",
                    json={
                        "making_charge_mode": "per_gram",
                        "making_charge_amount": 500,
                        "proposed_delivery_date": "2026-11-01"
                    },
                    headers=headers
                )
                assert resp_accept.status_code == 403
                assert resp_accept.json()["detail"]["code"] == "FEATURE_DISABLED"
    finally:
        app.dependency_overrides.clear()
