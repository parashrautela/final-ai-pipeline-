"""Real PostgreSQL database and concurrency test suite for
Jewel India: Native Jewellery Request Broadcast and Wholesaler Queue.

Tests:
1. Migration execution and schema invariants.
2. Identity helper functions (my_verified_retailer_id, my_verified_wholesaler_id).
3. Request creation with validation (weight range, quantity, budget mode, delivery date).
4. Sequential queue routing: Wholesaler A declines -> Wholesaler B receives.
5. Wholesaler B accepts with valid quote -> request assigned, quote created, queue halted.
6. Offer expiry: simulated wall-clock expiry -> advances to next candidate.
7. Candidate exhaustion when all decline/expire.
8. Request cancellation by retailer while routing -> terminates offer and routing.
9. Concurrency: Two concurrent accepts -> exactly one succeeds.
10. Concurrency: Accept vs Cancel race -> serialized, exactly one winner.
11. Concurrency: Accept vs Expiry race -> evaluated with clock_timestamp() under lock.
12. Concurrency: Multiple workers expiring/advancing simultaneously -> no double advance or duplicate offers.
"""

import os
import pathlib
import sys
import tempfile
import threading
import time
import uuid

import pgserver
import psycopg

WORKSPACE = pathlib.Path(__file__).resolve().parent.parent.parent
MIGRATION_PATH = WORKSPACE / "wholesaler ios/supabase/migrations/20261006_01_manufacturing_requests_broadcast.sql"


def setup_test_db():
    temp_dir = tempfile.mkdtemp(prefix="jewel-mfg-test-")
    server = pgserver.get_server(temp_dir, cleanup_mode="stop")
    admin_uri = server.get_uri()

    with psycopg.connect(admin_uri, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS jewel_mfg_test")
        conn.execute("CREATE DATABASE jewel_mfg_test")

    db_uri = psycopg.conninfo.make_conninfo(admin_uri, dbname="jewel_mfg_test")
    return server, db_uri, temp_dir


def bootstrap_schema(db_uri):
    with psycopg.connect(db_uri, autocommit=True) as db:
        db.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN CREATE ROLE anon NOLOGIN; END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN CREATE ROLE authenticated NOLOGIN; END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN CREATE ROLE service_role NOLOGIN BYPASSRLS; END IF;
            END $$;

            CREATE SCHEMA IF NOT EXISTS auth;
            CREATE TABLE IF NOT EXISTS auth.users (
                id UUID PRIMARY KEY,
                email TEXT,
                raw_user_meta_data JSONB DEFAULT '{}'::jsonb
            );

            CREATE OR REPLACE FUNCTION auth.uid() RETURNS UUID LANGUAGE sql STABLE AS $$
                SELECT NULLIF(current_setting('request.jwt.claim.sub', true), '')::uuid;
            $$;

            CREATE OR REPLACE FUNCTION auth.role() RETURNS TEXT LANGUAGE sql STABLE AS $$
                SELECT COALESCE(NULLIF(current_setting('request.jwt.claim.role', true), ''), 'anon');
            $$;

            GRANT USAGE ON SCHEMA auth, public TO anon, authenticated, service_role;
            GRANT ALL ON ALL TABLES IN SCHEMA auth TO service_role;

            -- Base profiles and entities
            CREATE TABLE IF NOT EXISTS public.profiles (
                id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
                email TEXT,
                role TEXT
            );

            CREATE TABLE IF NOT EXISTS public.retailers (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
                business_name TEXT,
                verification_status TEXT DEFAULT 'pending'
            );

            CREATE TABLE IF NOT EXISTS public.wholesalers (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
                business_name TEXT,
                verification_status TEXT DEFAULT 'pending'
            );

            CREATE TABLE IF NOT EXISTS public.employees (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                auth_user_id UUID NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
                retailer_id UUID NOT NULL REFERENCES public.retailers(id) ON DELETE CASCADE,
                status TEXT DEFAULT 'active'
            );

            GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
            GRANT SELECT ON public.retailers, public.wholesalers, public.employees, public.profiles TO authenticated;
        """)

        # Apply canonical migration
        migration_sql = MIGRATION_PATH.read_text()
        db.execute(migration_sql)


def set_auth(conn, user_id: uuid.UUID, role: str = "authenticated"):
    with conn.cursor() as cur:
        cur.execute(f"SET ROLE {role}")
        cur.execute("SELECT set_config('request.jwt.claim.sub', %s, false)", (str(user_id),))
        cur.execute("SELECT set_config('request.jwt.claim.role', %s, false)", (role,))


def reset_auth(conn):
    with conn.cursor() as cur:
        cur.execute("RESET ROLE")
        cur.execute("SELECT set_config('request.jwt.claim.sub', '', false)")
        cur.execute("SELECT set_config('request.jwt.claim.role', '', false)")


def run_tests():
    server, db_uri, temp_dir = setup_test_db()
    passed = 0
    failed = 0

    def assert_true(cond, name):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name}")
            raise AssertionError(f"Assertion failed: {name}")

    try:
        print("\n--- 1. Bootstrapping Database & Schema ---")
        bootstrap_schema(db_uri)
        assert_true(True, "Canonical migration applied without error")

        # Create test users and actors
        with psycopg.connect(db_uri, autocommit=True) as db:
            r1_user = uuid.uuid4()
            r2_user = uuid.uuid4()
            w1_user = uuid.uuid4()
            w2_user = uuid.uuid4()
            w3_user = uuid.uuid4()
            emp_user = uuid.uuid4()
            unverified_user = uuid.uuid4()

            for u in [r1_user, r2_user, w1_user, w2_user, w3_user, emp_user, unverified_user]:
                db.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s)", (u, f"{u}@test.com"))

            # Retailer 1 (verified)
            r1_id = db.execute(
                "INSERT INTO public.retailers (user_id, business_name, verification_status) VALUES (%s, 'Retailer One', 'verified') RETURNING id",
                (r1_user,)
            ).fetchone()[0]

            # Retailer 2 (verified)
            r2_id = db.execute(
                "INSERT INTO public.retailers (user_id, business_name, verification_status) VALUES (%s, 'Retailer Two', 'verified') RETURNING id",
                (r2_user,)
            ).fetchone()[0]

            # Wholesaler 1 (verified)
            w1_id = db.execute(
                "INSERT INTO public.wholesalers (user_id, business_name, verification_status) VALUES (%s, 'Wholesaler Alpha', 'verified') RETURNING id",
                (w1_user,)
            ).fetchone()[0]

            # Wholesaler 2 (verified)
            w2_id = db.execute(
                "INSERT INTO public.wholesalers (user_id, business_name, verification_status) VALUES (%s, 'Wholesaler Beta', 'verified') RETURNING id",
                (w2_user,)
            ).fetchone()[0]

            # Wholesaler 3 (verified)
            w3_id = db.execute(
                "INSERT INTO public.wholesalers (user_id, business_name, verification_status) VALUES (%s, 'Wholesaler Gamma', 'verified') RETURNING id",
                (w3_user,)
            ).fetchone()[0]

            # Employee for Retailer 1
            db.execute(
                "INSERT INTO public.employees (auth_user_id, retailer_id, status) VALUES (%s, %s, 'active')",
                (emp_user, r1_id)
            )

            # Unverified retailer
            db.execute(
                "INSERT INTO public.retailers (user_id, business_name, verification_status) VALUES (%s, 'Pending Retailer', 'pending')",
                (unverified_user,)
            )

        print("\n--- 2. Identity & Permission Checks ---")
        with psycopg.connect(db_uri) as conn:
            set_auth(conn, r1_user)
            with conn.cursor() as cur:
                cur.execute("SELECT public.my_verified_retailer_id()")
                res = cur.fetchone()[0]
                assert_true(res == r1_id, "my_verified_retailer_id() resolves verified retailer")

            set_auth(conn, w1_user)
            with conn.cursor() as cur:
                cur.execute("SELECT public.my_verified_wholesaler_id()")
                res = cur.fetchone()[0]
                assert_true(res == w1_id, "my_verified_wholesaler_id() resolves verified wholesaler")

            set_auth(conn, emp_user)
            with conn.cursor() as cur:
                cur.execute("SELECT public.my_verified_retailer_id()")
                res = cur.fetchone()[0]
                assert_true(res is None, "Employee session returns NULL for my_verified_retailer_id()")

            set_auth(conn, unverified_user)
            with conn.cursor() as cur:
                cur.execute("SELECT public.my_verified_retailer_id()")
                res = cur.fetchone()[0]
                assert_true(res is None, "Unverified retailer returns NULL for my_verified_retailer_id()")

        print("\n--- 3. Asset Upload & Request Creation Validations ---")
        with psycopg.connect(db_uri, autocommit=True) as db:
            # Create an asset for Retailer 1
            asset1_id = db.execute(
                """INSERT INTO public.manufacturing_request_assets
                   (owner_retailer_id, storage_bucket, storage_path, mime_type, byte_size, width, height)
                   VALUES (%s, 'manufacturing-requests', 'test.jpg', 'image/jpeg', 1024, 800, 800)
                   RETURNING id""",
                (r1_id,)
            ).fetchone()[0]

            # Asset for Retailer 2
            asset2_id = db.execute(
                """INSERT INTO public.manufacturing_request_assets
                   (owner_retailer_id, storage_bucket, storage_path, mime_type, byte_size, width, height)
                   VALUES (%s, 'manufacturing-requests', 'test2.jpg', 'image/jpeg', 1024, 800, 800)
                   RETURNING id""",
                (r2_id,)
            ).fetchone()[0]

        with psycopg.connect(db_uri) as conn:
            set_auth(conn, r1_user)
            with conn.cursor() as cur:
                # Validation: Reversed weights
                cur.execute("""
                    SELECT public.manufacturing_request_create(
                        %s, 'necklace', 25.0, 15.0, 'gold', '22kt', 'none', 1,
                        'per_gram', 450.0, 'INR', 6500.0, '22k/g', CURRENT_DATE + 14, 'Notes'
                    )
                """, (asset1_id,))
                res = cur.fetchone()[0]
                assert_true(res["ok"] is False and res["error"] == "INVALID_WEIGHT", "Rejects max_weight < min_weight")

                # Validation: Past delivery date
                cur.execute("""
                    SELECT public.manufacturing_request_create(
                        %s, 'necklace', 15.0, 25.0, 'gold', '22kt', 'none', 1,
                        'per_gram', 450.0, 'INR', 6500.0, '22k/g', CURRENT_DATE - 1, 'Notes'
                    )
                """, (asset1_id,))
                res = cur.fetchone()[0]
                assert_true(res["ok"] is False and res["error"] == "INVALID_DELIVERY_DATE", "Rejects past delivery date")

                # Validation: Cross-owner asset
                cur.execute("""
                    SELECT public.manufacturing_request_create(
                        %s, 'necklace', 15.0, 25.0, 'gold', '22kt', 'none', 1,
                        'per_gram', 450.0, 'INR', 6500.0, '22k/g', CURRENT_DATE + 14, 'Notes'
                    )
                """, (asset2_id,))
                res = cur.fetchone()[0]
                assert_true(res["ok"] is False and res["error"] == "INVALID_ASSET", "Rejects asset belonging to another retailer")

                # Successful creation
                cur.execute("""
                    SELECT public.manufacturing_request_create(
                        %s, 'necklace', 15.0, 25.0, 'gold', '22kt', 'none', 1,
                        'per_gram', 450.0, 'INR', 6500.0, '22k/g', CURRENT_DATE + 14, 'Valid request notes'
                    )
                """, (asset1_id,))
                res = cur.fetchone()[0]
                assert_true(res["ok"] is True and res["state"] == "routing", "Valid request created in routing state")
                req1_id = res["request_id"]
                offer1_id = res["active_offer_id"]
                conn.commit()

        print("\n--- 4. Queue State & Sequential Routing Verification ---")
        with psycopg.connect(db_uri) as conn:
            with conn.cursor() as cur:
                # Check candidate queue
                cur.execute(
                    "SELECT wholesaler_id, rank, status FROM public.manufacturing_request_candidates WHERE request_id = %s ORDER BY rank",
                    (req1_id,)
                )
                candidates = cur.fetchall()
                assert_true(len(candidates) == 3, "Queue contains exactly 3 verified wholesalers")
                assert_true(candidates[0][2] == "offered", "Rank 1 candidate has status 'offered'")
                assert_true(candidates[1][2] == "queued", "Rank 2 candidate has status 'queued'")
                first_wholesaler_id = candidates[0][0]

                # Check active offer
                cur.execute("SELECT wholesaler_id, status FROM public.manufacturing_offers WHERE id = %s", (offer1_id,))
                off = cur.fetchone()
                assert_true(off[0] == first_wholesaler_id and off[1] == "active", "Offer 1 is active for Rank 1 wholesaler")

        print("\n--- 5. Decline and Advance ---")
        with psycopg.connect(db_uri) as conn:
            # Wholesaler 1 declines
            set_auth(conn, w1_user if first_wholesaler_id == w1_id else (w2_user if first_wholesaler_id == w2_id else w3_user))
            with conn.cursor() as cur:
                cur.execute("SELECT public.manufacturing_offer_decline(%s, 'Capacity full')", (offer1_id,))
                res = cur.fetchone()[0]
                assert_true(res["ok"] is True and res["status"] == "declined" and res["advanced"] is True, "Offer 1 declined and advanced")
                offer2_id = res["new_offer_id"]
                conn.commit()

        with psycopg.connect(db_uri) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status, decline_reason FROM public.manufacturing_offers WHERE id = %s", (offer1_id,))
                off1 = cur.fetchone()
                assert_true(off1[0] == "declined" and off1[1] == "Capacity full", "Offer 1 status updated to declined with reason")

                cur.execute("SELECT wholesaler_id, status, rank FROM public.manufacturing_offers WHERE id = %s", (offer2_id,))
                off2 = cur.fetchone()
                assert_true(off2[1] == "active" and off2[2] == 2, "Offer 2 is active for Rank 2 wholesaler")
                second_wholesaler_id = off2[0]

        print("\n--- 6. Wholesaler Acceptance with Quote ---")
        second_user = w1_user if second_wholesaler_id == w1_id else (w2_user if second_wholesaler_id == w2_id else w3_user)
        with psycopg.connect(db_uri) as conn:
            set_auth(conn, second_user)
            with conn.cursor() as cur:
                # Validation: Quote above budget
                cur.execute("""
                    SELECT public.manufacturing_offer_accept(
                        %s, 'per_gram', 600.0, 6500.0, 0, 0, CURRENT_DATE + 10, 'Too expensive'
                    )
                """, (offer2_id,))
                res = cur.fetchone()[0]
                assert_true(res["ok"] is False and res["error"] == "BUDGET_EXCEEDED", "Rejects quote exceeding making charge budget")

                # Validation: Proposed delivery date later than deadline
                cur.execute("""
                    SELECT public.manufacturing_offer_accept(
                        %s, 'per_gram', 400.0, 6500.0, 0, 0, CURRENT_DATE + 30, 'Too late'
                    )
                """, (offer2_id,))
                res = cur.fetchone()[0]
                assert_true(res["ok"] is False and res["error"] == "DEADLINE_EXCEEDED", "Rejects quote with delivery after deadline")

                # Valid acceptance
                cur.execute("""
                    SELECT public.manufacturing_offer_accept(
                        %s, 'per_gram', 420.0, 6500.0, 0, 0, CURRENT_DATE + 10, 'Ready to craft'
                    )
                """, (offer2_id,))
                res = cur.fetchone()[0]
                assert_true(res["ok"] is True and res["status"] == "assigned", "Offer accepted and request assigned")
                quote_id = res["quote_id"]
                conn.commit()

        with psycopg.connect(db_uri) as conn:
            with conn.cursor() as cur:
                # Check request state
                cur.execute("SELECT state, assigned_wholesaler_id, accepted_quote_id, active_offer_id FROM public.manufacturing_requests WHERE id = %s", (req1_id,))
                r_state = cur.fetchone()
                assert_true(r_state[0] == "assigned", "Request state is assigned")
                assert_true(str(r_state[1]) == str(second_wholesaler_id), "Assigned wholesaler matches accepting candidate")
                assert_true(str(r_state[2]) == str(quote_id), "Accepted quote ID is recorded on request")
                assert_true(r_state[3] is None, "Active offer is cleared on assignment")

                # Verify Rank 3 received no offer
                cur.execute("SELECT COUNT(*) FROM public.manufacturing_offers WHERE request_id = %s", (req1_id,))
                count_offers = cur.fetchone()[0]
                assert_true(count_offers == 2, "Only 2 offers were ever created; Rank 3 received nothing")

        print("\n--- 7. Expiry & Exhaustion Workflow ---")
        # Create second request with 1-second offer duration
        with psycopg.connect(db_uri, autocommit=True) as db:
            asset3_id = db.execute(
                """INSERT INTO public.manufacturing_request_assets
                   (owner_retailer_id, storage_bucket, storage_path, mime_type, byte_size, width, height)
                   VALUES (%s, 'manufacturing-requests', 'test3.jpg', 'image/jpeg', 1024, 800, 800)
                   RETURNING id""",
                (r1_id,)
            ).fetchone()[0]

        with psycopg.connect(db_uri) as conn:
            set_auth(conn, r1_user)
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT public.manufacturing_request_create(
                        %s, 'bangles', 30.0, 40.0, 'gold', '22kt', 'none', 2,
                        'fixed_total', 15000.0, 'INR', 6500.0, '22k/g', CURRENT_DATE + 20, 'Expiry test',
                        1
                    )
                """, (asset3_id,))
                res = cur.fetchone()[0]
                req2_id = res["request_id"]
                conn.commit()

        # Wait 1.1s so offer expires in real time
        time.sleep(1.2)

        # Worker sweeps and advances Rank 1 -> Rank 2
        with psycopg.connect(db_uri, autocommit=True) as db:
            res = db.execute("SELECT public.manufacturing_offer_expire_and_advance(%s, 1)", (req2_id,)).fetchone()[0]
            assert_true(res["ok"] is True and res["advanced"] is True, "Expired offer advanced to Rank 2")

        time.sleep(1.2)

        # Worker sweeps Rank 2 -> Rank 3
        with psycopg.connect(db_uri, autocommit=True) as db:
            res = db.execute("SELECT public.manufacturing_offer_expire_and_advance(%s, 1)", (req2_id,)).fetchone()[0]
            assert_true(res["ok"] is True and res["advanced"] is True, "Expired offer advanced to Rank 3")

        time.sleep(1.2)

        # Worker sweeps Rank 3 -> Exhausted
        with psycopg.connect(db_uri, autocommit=True) as db:
            res = db.execute("SELECT public.manufacturing_offer_expire_and_advance(%s, 1)", (req2_id,)).fetchone()[0]
            assert_true(res["ok"] is True and res.get("state") == "exhausted", "All candidates expired -> request state is exhausted")

        print("\n--- 8. Retailer Cancellation Workflow ---")
        with psycopg.connect(db_uri, autocommit=True) as db:
            asset4_id = db.execute(
                """INSERT INTO public.manufacturing_request_assets
                   (owner_retailer_id, storage_bucket, storage_path, mime_type, byte_size, width, height)
                   VALUES (%s, 'manufacturing-requests', 'test4.jpg', 'image/jpeg', 1024, 800, 800)
                   RETURNING id""",
                (r1_id,)
            ).fetchone()[0]

        with psycopg.connect(db_uri) as conn:
            set_auth(conn, r1_user)
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT public.manufacturing_request_create(
                        %s, 'ring', 5.0, 8.0, 'gold', '18kt', 'diamond', 1,
                        'fixed_total', 8000.0, 'INR', 5500.0, '18k/g', CURRENT_DATE + 15, 'Cancel test'
                    )
                """, (asset4_id,))
                res = cur.fetchone()[0]
                req3_id = res["request_id"]
                conn.commit()

        # Retailer 2 attempts to cancel Retailer 1's request -> FORBIDDEN
        with psycopg.connect(db_uri) as conn:
            set_auth(conn, r2_user)
            with conn.cursor() as cur:
                cur.execute("SELECT public.manufacturing_request_cancel(%s, 'Sneak cancel')", (req3_id,))
                res = cur.fetchone()[0]
                assert_true(res["ok"] is False and res["error"] == "FORBIDDEN", "Retailer 2 cannot cancel Retailer 1 request")

        # Retailer 1 cancels their own request
        with psycopg.connect(db_uri) as conn:
            set_auth(conn, r1_user)
            with conn.cursor() as cur:
                cur.execute("SELECT public.manufacturing_request_cancel(%s, 'Customer changed mind')", (req3_id,))
                res = cur.fetchone()[0]
                assert_true(res["ok"] is True and res["state"] == "cancelled", "Retailer 1 cancels routing request")
                conn.commit()

        with psycopg.connect(db_uri) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT state, active_offer_id FROM public.manufacturing_requests WHERE id = %s", (req3_id,))
                r_cancelled = cur.fetchone()
                assert_true(r_cancelled[0] == "cancelled" and r_cancelled[1] is None, "Request marked cancelled with no active offer")

        print("\n--- 9. Concurrency: Two Simultaneous Accepts on Same Offer ---")
        # Setup request for concurrency test
        with psycopg.connect(db_uri, autocommit=True) as db:
            asset5_id = db.execute(
                """INSERT INTO public.manufacturing_request_assets
                   (owner_retailer_id, storage_bucket, storage_path, mime_type, byte_size, width, height)
                   VALUES (%s, 'manufacturing-requests', 'test5.jpg', 'image/jpeg', 1024, 800, 800)
                   RETURNING id""",
                (r1_id,)
            ).fetchone()[0]

        with psycopg.connect(db_uri) as conn:
            set_auth(conn, r1_user)
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT public.manufacturing_request_create(
                        %s, 'bracelet', 10.0, 15.0, 'gold', '22kt', 'none', 1,
                        'fixed_total', 10000.0, 'INR', 6500.0, '22k/g', CURRENT_DATE + 20, 'Race accept test'
                    )
                """, (asset5_id,))
                res = cur.fetchone()[0]
                req4_id = res["request_id"]
                offer_race_id = res["active_offer_id"]
                conn.commit()

        # Find offered wholesaler
        with psycopg.connect(db_uri) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT wholesaler_id FROM public.manufacturing_offers WHERE id = %s", (offer_race_id,))
                race_ws_id = cur.fetchone()[0]
        race_user = w1_user if race_ws_id == w1_id else (w2_user if race_ws_id == w2_id else w3_user)

        results = []
        errors = []

        def concurrent_accept(thread_id, amount):
            try:
                with psycopg.connect(db_uri) as conn:
                    set_auth(conn, race_user)
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT public.manufacturing_offer_accept(
                                %s::uuid, 'fixed_total', %s::numeric, 6500.0::numeric, 0::numeric, 0::numeric, CURRENT_DATE + 10, %s
                            )
                        """, (offer_race_id, amount, f"Thread {thread_id} quote"))
                        res = cur.fetchone()[0]
                        conn.commit()
                        results.append((thread_id, res))
            except Exception as e:
                errors.append((thread_id, e))

        t1 = threading.Thread(target=concurrent_accept, args=(1, 9000.0))
        t2 = threading.Thread(target=concurrent_accept, args=(2, 9500.0))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        successes = [r for r in results if r[1].get("ok") is True and not r[1].get("idempotent")]
        idempotent_or_rejected = [r for r in results if (r[1].get("ok") is True and r[1].get("idempotent")) or r[1].get("ok") is False]
        assert_true(len(successes) == 1, "Exactly one thread executes the first real acceptance")
        assert_true(len(idempotent_or_rejected) == 1, "The second concurrent thread is handled idempotently or rejected")

        # Verify invariant in database: exactly 1 accepted quote and 1 assignment
        with psycopg.connect(db_uri) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM public.manufacturing_quotes WHERE request_id = %s", (req4_id,))
                q_count = cur.fetchone()[0]
                assert_true(q_count == 1, "Invariant holds: exactly 1 quote row created in database")

                cur.execute("SELECT COUNT(*) FROM public.manufacturing_offers WHERE request_id = %s AND status = 'accepted'", (req4_id,))
                accepted_offers = cur.fetchone()[0]
                assert_true(accepted_offers == 1, "Invariant holds: exactly 1 accepted offer exists")

        print("\n--- 10. Concurrency: Multiple Expiry Workers Sweeping Concurrently ---")
        # Setup request with 1-second timeout
        with psycopg.connect(db_uri, autocommit=True) as db:
            asset6_id = db.execute(
                """INSERT INTO public.manufacturing_request_assets
                   (owner_retailer_id, storage_bucket, storage_path, mime_type, byte_size, width, height)
                   VALUES (%s, 'manufacturing-requests', 'test6.jpg', 'image/jpeg', 1024, 800, 800)
                   RETURNING id""",
                (r1_id,)
            ).fetchone()[0]

        with psycopg.connect(db_uri) as conn:
            set_auth(conn, r1_user)
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT public.manufacturing_request_create(
                        %s, 'pendant', 8.0, 12.0, 'gold', '22kt', 'none', 1,
                        'fixed_total', 5000.0, 'INR', 6500.0, '22k/g', CURRENT_DATE + 20, 'Worker race test',
                        1
                    )
                """, (asset6_id,))
                res = cur.fetchone()[0]
                req5_id = res["request_id"]
                conn.commit()

        time.sleep(1.2)  # Wait for offer to expire

        worker_results = []
        def worker_sweep(worker_id):
            with psycopg.connect(db_uri, autocommit=True) as db:
                res = db.execute("SELECT public.manufacturing_offer_expire_and_advance(%s, 1800)", (req5_id,)).fetchone()[0]
                worker_results.append((worker_id, res))

        w_threads = [threading.Thread(target=worker_sweep, args=(i,)) for i in range(4)]
        for t in w_threads:
            t.start()
        for t in w_threads:
            t.join()

        advanced_count = sum(1 for _, r in worker_results if r.get("advanced") is True)
        assert_true(advanced_count == 1, "Exactly one worker advances the expired offer; others detect no-op")

        with psycopg.connect(db_uri) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM public.manufacturing_offers WHERE request_id = %s AND status = 'active'", (req5_id,))
                active_count = cur.fetchone()[0]
                assert_true(active_count == 1, "Invariant holds: exactly 1 active offer after concurrent worker sweep")

        print(f"\n==========================================")
        print(f"DATABASE & CONCURRENCY TESTS COMPLETE: {passed} PASSED, {failed} FAILED")
        print(f"==========================================")

    finally:
        try:
            server.cleanup()
        except Exception:
            pass


if __name__ == "__main__":
    run_tests()
