"""Credit gate + auth tests for the Chamak endpoints.

No database, no network: the repository layer is mocked, so what is under test
is exactly the part that matters — who may spend, what happens when they
cannot pay, and whether a failure ever leaks free work.

These call the endpoint coroutines directly rather than going through
`TestClient`, because starlette 0.36.3's TestClient is incompatible with the
pinned httpx 0.28.1 (`Client.__init__() got an unexpected keyword argument
'app'`). That is a pre-existing version clash in this repo, unrelated to
credits — worth fixing separately, but it should not block testing this.

Run:  PYTHONPATH=<pytest site-packages> .venv/bin/python -m pytest test_credits.py -v

Replay and refund semantics assume migration 006 (migrations/006_razorpay_purchases.sql).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from fastapi import HTTPException
from starlette.background import BackgroundTasks
from starlette.requests import Request

from app.config import settings
from app.validation import ChamakGenerationRequest

JWT_SECRET = "test-secret-for-unit-tests-only-32b+"
OWNER = str(uuid.uuid4())
STRANGER = str(uuid.uuid4())
GEN_ID = str(uuid.uuid4())


# ── helpers ──────────────────────────────────────────────────────────────────

def token_for(user_id: str, *, expired: bool = False, secret: str = JWT_SECRET) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    exp = now - dt.timedelta(hours=1) if expired else now + dt.timedelta(hours=1)
    return jwt.encode(
        {"sub": user_id, "aud": "authenticated", "exp": exp, "iat": now},
        secret,
        algorithm="HS256",
    )


def make_request() -> Request:
    """Minimal ASGI scope — slowapi's rate limiter needs `client` and `app`."""
    from app.main import app

    return Request({
        "type": "http",
        "method": "POST",
        "path": "/api/chamak/generate",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "app": app,
    })


def row(owner: str = OWNER) -> dict:
    return {"id": GEN_ID, "wholesaler_id": owner, "status": "awaiting_input"}


def call_generate(user_id=OWNER, *, idempotency_key=None, tasks=None):
    from app.main import chamak_generate

    return asyncio.run(chamak_generate(
        request=make_request(),
        body=ChamakGenerationRequest(generation_id=GEN_ID),
        background_tasks=tasks if tasks is not None else BackgroundTasks(),
        user_id=user_id,
        idempotency_key=idempotency_key,
    ))


def call_analyze(user_id=OWNER, tasks=None):
    from app.main import chamak_analyze

    return asyncio.run(chamak_analyze(
        request=make_request(),
        body=ChamakGenerationRequest(generation_id=GEN_ID),
        background_tasks=tasks if tasks is not None else BackgroundTasks(),
        user_id=user_id,
        idempotency_key=None,
    ))


def mocks(*, spend=None, spend_raises=None, prior=0, owner=OWNER):
    return (
        patch("app.main.fetch_chamak_generation", AsyncMock(return_value=row(owner))),
        patch("app.main.update_chamak_generation", AsyncMock()),
        patch("app.main.count_prior_debits", AsyncMock(return_value=prior)),
        patch("app.main.spend_credits",
              AsyncMock(return_value=spend, side_effect=spend_raises)),
    )


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    """Force local HS256 verification, switch metering on, and disable the
    rate limiter.

    The limiter is keyed on client IP and capped at 10/minute; every test here
    reports the same fake IP, so without this the suite starts 429-ing partway
    through and the failure looks like a credit bug when it is not.

    Also cuts the real Supabase client off. `.env` holds the production
    service-role key; a repository call a test forgot to mock must fail loudly
    here, never quietly reach the live database.
    """
    import app.auth
    import app.db.repository
    from app.main import limiter

    def _no_database(*_args, **_kwargs):
        raise RuntimeError("tests must not reach Supabase — mock the repository call")

    monkeypatch.setattr(settings, "SUPABASE_JWT_SECRET", JWT_SECRET)
    monkeypatch.setattr(settings, "CREDITS_ENABLED", True)
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(app.db.repository, "get_supabase", _no_database)
    monkeypatch.setattr(app.auth, "get_supabase", _no_database)


# ── authentication ───────────────────────────────────────────────────────────

def test_missing_token_is_rejected():
    """These endpoints used to be open to the internet. They are not now."""
    from app.auth import resolve_user_id

    with pytest.raises(HTTPException) as e:
        asyncio.run(resolve_user_id(None))
    assert e.value.status_code == 401


def test_valid_token_resolves_to_its_user():
    from app.auth import resolve_user_id

    assert asyncio.run(resolve_user_id(f"Bearer {token_for(OWNER)}")) == OWNER


def test_bare_token_without_the_bearer_scheme_still_works():
    from app.auth import resolve_user_id

    assert asyncio.run(resolve_user_id(token_for(OWNER))) == OWNER


@pytest.mark.parametrize("bad,label", [
    ("Bearer not-a-jwt", "garbage"),
    (None, "absent"),
])
def test_unusable_tokens_are_rejected(bad, label):
    from app.auth import resolve_user_id

    with pytest.raises(HTTPException) as e:
        asyncio.run(resolve_user_id(bad))
    assert e.value.status_code == 401, label


def test_expired_token_is_rejected():
    from app.auth import resolve_user_id

    with pytest.raises(HTTPException) as e:
        asyncio.run(resolve_user_id(f"Bearer {token_for(OWNER, expired=True)}"))
    assert e.value.status_code == 401


def test_token_signed_with_the_wrong_secret_is_rejected():
    from app.auth import resolve_user_id

    forged = token_for(OWNER, secret="a-different-secret-of-sufficient-length")
    with pytest.raises(HTTPException) as e:
        asyncio.run(resolve_user_id(f"Bearer {forged}"))
    assert e.value.status_code == 401


# ── ownership ────────────────────────────────────────────────────────────────

def test_cannot_touch_another_wholesalers_generation():
    """A valid token proves who you are, not that the row is yours."""
    m = mocks(spend={"ok": True, "charged": 10, "balance": 90}, owner=OWNER)
    with m[0], m[1], m[2], m[3] as spend:
        with pytest.raises(HTTPException) as e:
            call_generate(user_id=STRANGER)
    # 404, not 403 — never confirm that somebody else's id exists.
    assert e.value.status_code == 404
    spend.assert_not_awaited()


# ── the debit ────────────────────────────────────────────────────────────────

def test_successful_generate_charges_once_and_queues_the_work():
    tasks = BackgroundTasks()
    m = mocks(spend={"ok": True, "charged": 10, "balance": 90})
    with m[0], m[1], m[2], m[3] as spend:
        result = call_generate(tasks=tasks)
    assert result["status"] == "generating"
    spend.assert_awaited_once()
    assert spend.await_args.kwargs["feature_key"] == "chamak.generate"
    assert len(tasks.tasks) == 1              # generation actually queued


def test_out_of_credits_returns_402_and_generates_nothing():
    tasks = BackgroundTasks()
    m = mocks(spend={"ok": False, "error": "INSUFFICIENT_CREDITS",
                     "required": 10, "balance": 4, "short_by": 6})
    with m[0], m[1], m[2], m[3]:
        with pytest.raises(HTTPException) as e:
            call_generate(tasks=tasks)
    assert e.value.status_code == 402
    assert e.value.detail["error"] == "INSUFFICIENT_CREDITS"
    assert e.value.detail["short_by"] == 6    # the app needs this for the top-up sheet
    assert tasks.tasks == []                  # nothing was generated


def test_ledger_outage_fails_closed():
    """The old upload quota failed OPEN. Money must not."""
    tasks = BackgroundTasks()
    m = mocks(spend_raises=RuntimeError("ledger unreachable"))
    with m[0], m[1], m[2], m[3]:
        with pytest.raises(HTTPException) as e:
            call_generate(tasks=tasks)
    assert e.value.status_code == 503
    assert tasks.tasks == []                  # no free generation during an outage


def test_a_reroll_is_priced_as_a_reroll():
    """iOS `regenerate` reuses the SAME row, so the ledger decides — not the client."""
    m = mocks(spend={"ok": True, "charged": 6, "balance": 84}, prior=1)
    with m[0], m[1], m[2], m[3] as spend:
        call_generate()
    assert spend.await_args.kwargs["feature_key"] == "chamak.reroll"


def test_first_generation_is_not_priced_as_a_reroll():
    m = mocks(spend={"ok": True, "charged": 10, "balance": 90}, prior=0)
    with m[0], m[1], m[2], m[3] as spend:
        call_generate()
    assert spend.await_args.kwargs["feature_key"] == "chamak.generate"


def test_idempotency_key_is_namespaced_to_the_user_and_the_generation():
    """Stored verbatim, one paid key replayed on any other generation for free."""
    m = mocks(spend={"ok": True, "charged": 10, "balance": 90})
    with m[0], m[1], m[2], m[3] as spend:
        call_generate(idempotency_key="user-action-abc")
    assert spend.await_args.kwargs["idempotency_key"] == f"{OWNER}:{GEN_ID}:user-action-abc"


def test_without_the_header_the_key_is_row_derived():
    """The fallback must never double-charge a retry, even if it undercharges."""
    m = mocks(spend={"ok": True, "charged": 10, "balance": 90})
    with m[0], m[1], m[2], m[3] as spend:
        call_generate(idempotency_key=None)
    assert spend.await_args.kwargs["idempotency_key"] == f"chamak:chamak.generate:{GEN_ID}"


def test_the_debit_names_the_generation_it_paid_for():
    """Without this the refund path has nothing to look up."""
    m = mocks(spend={"ok": True, "charged": 10, "balance": 90})
    with m[0], m[1], m[2], m[3] as spend:
        call_generate()
    assert spend.await_args.kwargs["reference_type"] == "chamak_generation"
    assert spend.await_args.kwargs["reference_id"] == GEN_ID


def test_analysis_is_free_but_still_goes_through_the_meter():
    m = mocks(spend={"ok": True, "charged": 0, "balance": 90, "free": True})
    with m[0], m[1], m[2], m[3] as spend:
        result = call_analyze()
    assert result["status"] == "analyzing"
    assert spend.await_args.kwargs["feature_key"] == "chamak.analyze"


def test_metering_switched_off_charges_nothing(monkeypatch):
    """CREDITS_ENABLED=false must behave exactly as before this change."""
    monkeypatch.setattr(settings, "CREDITS_ENABLED", False)
    tasks = BackgroundTasks()
    m = mocks(spend={"ok": True})
    with m[0], m[1], m[2], m[3] as spend:
        result = call_generate(tasks=tasks)
    assert result["status"] == "generating"
    spend.assert_not_awaited()
    assert len(tasks.tasks) == 1


# ── refunds ──────────────────────────────────────────────────────────────────

def test_failed_generation_refunds_the_wholesaler():
    from app.services.chamak import _refund_failed_generation

    with patch("app.services.chamak.refund_credits",
               AsyncMock(return_value={"ok": True, "refunded": 10})) as refund:
        asyncio.run(_refund_failed_generation(GEN_ID, "Generation failed"))
    refund.assert_awaited_once_with("chamak_generation", GEN_ID, "Generation failed")


def test_a_broken_refund_never_masks_the_original_failure():
    """The generation status still has to be written even if the refund dies."""
    from app.services.chamak import _refund_failed_generation

    with patch("app.services.chamak.refund_credits",
               AsyncMock(side_effect=RuntimeError("ledger down"))):
        asyncio.run(_refund_failed_generation(GEN_ID, "Generation failed"))  # must not raise


def test_no_refund_attempted_when_metering_is_off(monkeypatch):
    from app.services.chamak import _refund_failed_generation

    monkeypatch.setattr(settings, "CREDITS_ENABLED", False)
    with patch("app.services.chamak.refund_credits", AsyncMock()) as refund:
        asyncio.run(_refund_failed_generation(GEN_ID, "Generation failed"))
    refund.assert_not_awaited()


# ── staged rollout ───────────────────────────────────────────────────────────
#
# The backend has to be deployable BEFORE the iOS and web clients learn to send
# a token, or shipping it would 401 every live app. These pin that behaviour.

def test_no_token_is_allowed_while_metering_is_off(monkeypatch):
    from app.auth import resolve_user_id

    monkeypatch.setattr(settings, "CREDITS_ENABLED", False)
    assert asyncio.run(resolve_user_id(None)) is None


def test_no_token_is_refused_once_metering_is_on(monkeypatch):
    """There is no honest way to debit a wallet you cannot identify."""
    from app.auth import resolve_user_id

    monkeypatch.setattr(settings, "CREDITS_ENABLED", True)
    with pytest.raises(HTTPException) as e:
        asyncio.run(resolve_user_id(None))
    assert e.value.status_code == 401


def test_a_bad_token_is_still_refused_while_metering_is_off(monkeypatch):
    """Metering off relaxes 'no token'. It must never relax 'wrong token'."""
    from app.auth import resolve_user_id

    monkeypatch.setattr(settings, "CREDITS_ENABLED", False)
    forged = token_for(OWNER, secret="a-different-secret-of-sufficient-length")
    with pytest.raises(HTTPException) as e:
        asyncio.run(resolve_user_id(f"Bearer {forged}"))
    assert e.value.status_code == 401


def test_old_client_still_works_end_to_end_while_metering_is_off(monkeypatch):
    """An un-updated app sends no token: it must behave exactly as before."""
    monkeypatch.setattr(settings, "CREDITS_ENABLED", False)
    tasks = BackgroundTasks()
    m = mocks(spend={"ok": True})
    with m[0], m[1], m[2], m[3] as spend:
        result = call_generate(user_id=None, tasks=tasks)
    assert result["status"] == "generating"
    spend.assert_not_awaited()
    assert len(tasks.tasks) == 1


# ── replays: a paid key must never buy a second output ───────────────────────
#
# The bug these pin: `spend_credits` answered `ok, replayed` for ANY key it had
# seen, and the endpoints then queued a brand-new job. One paid request's key,
# re-sent, ran unlimited generations — on other rows, or as endless re-rolls
# of the same one. `FakeLedger` below mirrors migration 006's spend_credits so
# the whole path (key derivation → ledger → endpoint) is exercised together.

GEN_B = str(uuid.uuid4())

ENDPOINTS = {
    "generate": "chamak_generate",
    "generate-v2": "chamak_generate_v2",
    "set-creation": "set_creation_generate",
}


def gen_row(gen_id: str = GEN_ID, owner: str = OWNER, **extra) -> dict:
    return {
        "id": gen_id,
        "wholesaler_id": owner,
        "status": "awaiting_input",
        "mode": "set_creation",
        "source_image_1_url": "https://example.com/1.jpg",
        "source_image_2_url": "https://example.com/2.jpg",
        **extra,
    }


class FakeLedger:
    """spend_credits / refund_debit as migration 006 defines them, in memory."""

    PRICES = {"chamak.analyze": 0, "chamak.generate": 10, "chamak.reroll": 6,
              "chamak.set_creation": 8}

    def __init__(self, balance: int = 100):
        self.balance = balance
        self.by_key: dict[str, dict] = {}
        self.refunded: set[str] = set()
        self.seq = 0

    def debits(self, reference_id=None) -> list[dict]:
        return [r for r in self.by_key.values()
                if r["kind"] == "debit" and reference_id in (None, r["reference"][1])]

    async def spend(self, *, user_id, feature_key, idempotency_key,
                    reference_type=None, reference_id=None, metadata=None):
        existing = self.by_key.get(idempotency_key)
        if existing:
            if (existing["kind"] != "debit" or existing["user"] != user_id
                    or existing["reference"] != (reference_type, reference_id)):
                return {"ok": False, "error": "IDEMPOTENCY_CONFLICT"}
            later = any(r["seq"] > existing["seq"] for r in self.debits(reference_id))
            return {"ok": True, "replayed": True, "charged": existing["cost"],
                    "balance": self.balance, "ledger_id": existing["id"],
                    "created_at": existing["created_at"],
                    "refunded": existing["id"] in self.refunded, "superseded": later}
        cost = self.PRICES[feature_key]
        if cost == 0:
            return {"ok": True, "charged": 0, "balance": self.balance, "free": True}
        if self.balance < cost:
            return {"ok": False, "error": "INSUFFICIENT_CREDITS", "required": cost,
                    "balance": self.balance, "short_by": cost - self.balance}
        self.seq += 1
        self.balance -= cost
        row = {"id": f"ledger-{self.seq}", "seq": self.seq, "kind": "debit",
               "user": user_id, "reference": (reference_type, reference_id),
               "cost": cost, "feature_key": feature_key,
               "created_at": f"2026-09-11T10:00:{self.seq:02d}+00:00"}
        self.by_key[idempotency_key] = row
        return {"ok": True, "charged": cost, "balance": self.balance,
                "ledger_id": row["id"], "created_at": row["created_at"]}

    def refund(self, ledger_id: str) -> None:
        row = next(r for r in self.by_key.values() if r["id"] == ledger_id)
        if ledger_id not in self.refunded:
            self.refunded.add(ledger_id)
            self.balance += row["cost"]

    async def count_prior(self, reference_type, reference_id) -> int:
        return len(self.debits(reference_id))


def ledger_patches(ledger: FakeLedger, rows: dict):
    return (
        patch("app.main.fetch_chamak_generation",
              AsyncMock(side_effect=lambda gid: rows.get(gid))),
        patch("app.main.update_chamak_generation", AsyncMock()),
        patch("app.main.count_prior_debits", AsyncMock(side_effect=ledger.count_prior)),
        patch("app.main.spend_credits", AsyncMock(side_effect=ledger.spend)),
    )


async def acall(endpoint="generate", *, gen_id=GEN_ID, user_id=OWNER,
                idempotency_key=None, tasks=None):
    import app.main as main

    return await getattr(main, ENDPOINTS[endpoint])(
        request=make_request(),
        body=ChamakGenerationRequest(generation_id=gen_id),
        background_tasks=tasks if tasks is not None else BackgroundTasks(),
        user_id=user_id,
        idempotency_key=idempotency_key,
    )


def call(endpoint="generate", **kwargs):
    return asyncio.run(acall(endpoint, **kwargs))


def test_a_paid_key_replayed_on_another_generation_is_charged_again():
    """The attack: pay once on A, then send A's key with B. B must be billed."""
    ledger = FakeLedger(balance=100)
    tasks = BackgroundTasks()
    p = ledger_patches(ledger, {GEN_ID: gen_row(GEN_ID), GEN_B: gen_row(GEN_B)})
    with p[0], p[1], p[2], p[3]:
        call(gen_id=GEN_ID, idempotency_key="K", tasks=tasks)
        second = call(gen_id=GEN_B, idempotency_key="K", tasks=tasks)
    assert second.get("replayed") is None
    assert len(ledger.debits()) == 2            # two outputs, two charges
    assert ledger.balance == 80
    assert len(tasks.tasks) == 2


def test_replaying_a_paid_key_on_the_same_generation_starts_no_new_job():
    """Re-sending one paid key must not buy endless re-rolls of the same row."""
    ledger = FakeLedger(balance=100)
    tasks = BackgroundTasks()
    p = ledger_patches(ledger, {GEN_ID: gen_row(status="generating")})
    with p[0], p[1], p[2], p[3]:
        call(idempotency_key="K", tasks=tasks)
        replays = [call(idempotency_key="K", tasks=tasks) for _ in range(3)]
    assert len(tasks.tasks) == 1                 # only the paid job
    assert len(ledger.debits()) == 1
    assert ledger.balance == 90
    assert all(r["replayed"] is True for r in replays)
    assert all(r["status"] == "generating" for r in replays)


def test_a_double_tap_charges_once_and_runs_once():
    ledger = FakeLedger(balance=100)
    tasks = BackgroundTasks()
    p = ledger_patches(ledger, {GEN_ID: gen_row()})

    async def both():
        return await asyncio.gather(
            acall(idempotency_key="tap", tasks=tasks),
            acall(idempotency_key="tap", tasks=tasks),
        )

    with p[0], p[1], p[2], p[3]:
        asyncio.run(both())
    assert len(ledger.debits()) == 1
    assert len(tasks.tasks) == 1


def test_a_reroll_with_a_new_key_is_charged_and_runs():
    ledger = FakeLedger(balance=100)
    tasks = BackgroundTasks()
    p = ledger_patches(ledger, {GEN_ID: gen_row()})
    with p[0], p[1], p[2], p[3]:
        call(idempotency_key="first", tasks=tasks)
        call(idempotency_key="reroll-1", tasks=tasks)
    assert [d["feature_key"] for d in ledger.debits()] == ["chamak.generate", "chamak.reroll"]
    assert ledger.balance == 84
    assert len(tasks.tasks) == 2


@pytest.mark.parametrize("endpoint", list(ENDPOINTS))
def test_no_paid_endpoint_runs_a_replayed_charge(endpoint):
    tasks = BackgroundTasks()
    replay = {"ok": True, "replayed": True, "charged": 10, "balance": 90,
              "ledger_id": "ledger-1", "created_at": "2026-09-11T10:00:00+00:00",
              "refunded": False, "superseded": False}
    update = AsyncMock()
    with patch("app.main.fetch_chamak_generation", AsyncMock(return_value=gen_row(status="generating"))), \
         patch("app.main.update_chamak_generation", update), \
         patch("app.main.count_prior_debits", AsyncMock(return_value=1)), \
         patch("app.main.spend_credits", AsyncMock(return_value=replay)):
        result = call(endpoint, idempotency_key="K", tasks=tasks)
    assert tasks.tasks == []
    update.assert_not_awaited()                  # the row is not re-marked either
    assert result["replayed"] is True
    assert result["status"] == "generating"


def test_retry_after_a_refunded_failure_is_billed_as_a_new_attempt():
    """iOS keeps its key until a generation succeeds. If the paid job failed
    and was refunded, retrying with that key must run again — and pay again —
    rather than hang on a job that is never coming, or run for free."""
    ledger = FakeLedger(balance=100)
    tasks = BackgroundTasks()
    p = ledger_patches(ledger, {GEN_ID: gen_row()})
    with p[0], p[1], p[2], p[3] as spend:
        call(idempotency_key="K", tasks=tasks)
        ledger.refund("ledger-1")                # the job failed; credits went back
        call(idempotency_key="K", tasks=tasks)
        keys = [c.kwargs["idempotency_key"] for c in spend.await_args_list]
        call(idempotency_key="K", tasks=tasks)   # and a retry of THAT is a replay
    assert len(tasks.tasks) == 2
    assert len(ledger.debits()) == 2
    assert keys[-1] == f"{OWNER}:{GEN_ID}:K:after-refund:1"
    assert ledger.balance == 100 - 10 + 10 - 6


def test_an_idempotency_conflict_is_a_409_and_runs_nothing():
    tasks = BackgroundTasks()
    m = mocks(spend={"ok": False, "error": "IDEMPOTENCY_CONFLICT"})
    with m[0], m[1], m[2], m[3]:
        with pytest.raises(HTTPException) as e:
            call_generate(idempotency_key="K", tasks=tasks)
    assert e.value.status_code == 409
    assert tasks.tasks == []


def test_a_replay_puts_back_done_when_the_paid_job_already_finished():
    """Clients write `generating` themselves before calling. If the paid job
    had finished, that hides the result; the replay restores it."""
    replay = {"ok": True, "replayed": True, "charged": 10,
              "ledger_id": "ledger-1", "created_at": "2026-09-11T10:00:00+00:00",
              "refunded": False, "superseded": False}
    row_now = gen_row(status="generating", output_image_url=f"{OWNER}/{GEN_ID}.png",
                      completed_at="2026-09-11T10:01:30+00:00")
    update = AsyncMock()
    tasks = BackgroundTasks()
    with patch("app.main.fetch_chamak_generation", AsyncMock(return_value=row_now)), \
         patch("app.main.update_chamak_generation", update), \
         patch("app.main.count_prior_debits", AsyncMock(return_value=1)), \
         patch("app.main.spend_credits", AsyncMock(return_value=replay)):
        result = call(idempotency_key="K", tasks=tasks)
    update.assert_awaited_once_with(GEN_ID, {"status": "done"})
    assert result["status"] == "done"
    assert tasks.tasks == []


@pytest.mark.parametrize("change,label", [
    ({"superseded": True}, "a later charge replaced this one"),
    ({"created_at": "2026-09-11T10:05:00+00:00"}, "the output predates this charge"),
])
def test_a_replay_leaves_the_row_alone_unless_this_charge_delivered(change, label):
    replay = {"ok": True, "replayed": True, "charged": 6,
              "ledger_id": "ledger-2", "created_at": "2026-09-11T10:00:00+00:00",
              "refunded": False, "superseded": False, **change}
    row_now = gen_row(status="generating", output_image_url="x.png",
                      completed_at="2026-09-11T10:01:30+00:00")
    update = AsyncMock()
    with patch("app.main.fetch_chamak_generation", AsyncMock(return_value=row_now)), \
         patch("app.main.update_chamak_generation", update), \
         patch("app.main.count_prior_debits", AsyncMock(return_value=2)), \
         patch("app.main.spend_credits", AsyncMock(return_value=replay)):
        result = call(idempotency_key="K")
    assert not update.await_count, label
    assert result["status"] == "generating", label


def test_the_job_carries_the_charge_that_paid_for_it():
    """So its failure refunds exactly that debit and nothing else."""
    tasks = BackgroundTasks()
    charge = {"ok": True, "charged": 10, "balance": 90, "ledger_id": "ledger-9",
              "created_at": "2026-09-11T10:00:00+00:00"}
    m = mocks(spend=charge)
    with m[0], m[1], m[2], m[3]:
        call_generate(idempotency_key="K", tasks=tasks)
    assert tasks.tasks[0].args == (GEN_ID, charge)


def test_if_the_job_cannot_be_started_the_charge_is_refunded_first():
    tasks = BackgroundTasks()
    charge = {"ok": True, "charged": 10, "balance": 90, "ledger_id": "ledger-3"}
    with patch("app.main.fetch_chamak_generation", AsyncMock(return_value=gen_row())), \
         patch("app.main.update_chamak_generation", AsyncMock(side_effect=RuntimeError("db blip"))), \
         patch("app.main.count_prior_debits", AsyncMock(return_value=0)), \
         patch("app.main.spend_credits", AsyncMock(return_value=charge)), \
         patch("app.services.chamak.refund_debit", AsyncMock(return_value={"ok": True, "refunded": 10})) as refund:
        with pytest.raises(RuntimeError):
            call_generate(idempotency_key="K", tasks=tasks)
    refund.assert_awaited_once_with("ledger-3", "Could not start the job")
    assert tasks.tasks == []


def test_an_oversized_client_key_is_hashed_not_rejected():
    m = mocks(spend={"ok": True, "charged": 10, "balance": 90})
    with m[0], m[1], m[2], m[3] as spend:
        call_generate(idempotency_key="x" * 5000)
    key = spend.await_args.kwargs["idempotency_key"]
    assert key.startswith(f"{OWNER}:{GEN_ID}:sha256:")
    assert len(key) < 200


def test_a_blank_header_falls_back_to_the_row_derived_key():
    m = mocks(spend={"ok": True, "charged": 10, "balance": 90})
    with m[0], m[1], m[2], m[3] as spend:
        call_generate(idempotency_key="   ")
    assert spend.await_args.kwargs["idempotency_key"] == f"chamak:chamak.generate:{GEN_ID}"


# ── refunds are exact ────────────────────────────────────────────────────────

def test_a_failed_job_refunds_exactly_its_own_debit():
    from app.services.chamak import _refund_failed_generation

    charge = {"ok": True, "charged": 6, "ledger_id": "ledger-7"}
    with patch("app.services.chamak.refund_debit",
               AsyncMock(return_value={"ok": True, "refunded": 6})) as exact, \
         patch("app.services.chamak.refund_credits", AsyncMock()) as by_generation:
        asyncio.run(_refund_failed_generation(GEN_ID, "Generation failed", charge))
    exact.assert_awaited_once_with("ledger-7", "Generation failed")
    by_generation.assert_not_awaited()


def test_an_unbilled_job_refunds_nothing():
    """A free job's failure must not hand back somebody else's charge."""
    from app.services.chamak import _refund_failed_generation

    with patch("app.services.chamak.refund_debit", AsyncMock()) as exact, \
         patch("app.services.chamak.refund_credits", AsyncMock()) as by_generation:
        asyncio.run(_refund_failed_generation(
            GEN_ID, "Analysis failed", {"ok": True, "charged": 0, "free": True}))
    exact.assert_not_awaited()
    by_generation.assert_not_awaited()


def test_a_charge_from_an_older_ledger_falls_back_to_the_generation():
    """Before migration 006 the debit's id is not returned; refund as before."""
    from app.services.chamak import _refund_failed_generation

    with patch("app.services.chamak.refund_debit", AsyncMock()) as exact, \
         patch("app.services.chamak.refund_credits",
               AsyncMock(return_value={"ok": True, "granted": 10})) as by_generation:
        asyncio.run(_refund_failed_generation(
            GEN_ID, "Generation failed", {"ok": True, "charged": 10, "balance": 90}))
    exact.assert_not_awaited()
    by_generation.assert_awaited_once_with("chamak_generation", GEN_ID, "Generation failed")


def test_a_failed_reanalysis_does_not_refund_the_generation_before_it():
    """Re-running (free) analysis on an already-generated row, and making it
    fail, used to refund that row's paid generation — a free output on demand."""
    from app.services.chamak import run_stage1_vision_analysis

    with patch("app.services.chamak.fetch_chamak_generation",
               AsyncMock(return_value=gen_row(source_image_1_url="https://bad/1"))), \
         patch("app.services.chamak.update_chamak_generation", AsyncMock()) as update, \
         patch("app.services.chamak.fetch_image_bytes_and_content_type",
               AsyncMock(side_effect=RuntimeError("unreachable image"))), \
         patch("app.services.chamak.refund_debit", AsyncMock()) as exact, \
         patch("app.services.chamak.refund_credits", AsyncMock()) as by_generation:
        asyncio.run(run_stage1_vision_analysis(
            GEN_ID, {"ok": True, "charged": 0, "free": True}))
    update.assert_any_await(GEN_ID, {"status": "failed"})
    exact.assert_not_awaited()
    by_generation.assert_not_awaited()


def test_the_analyze_endpoint_hands_its_free_charge_to_the_job():
    tasks = BackgroundTasks()
    free = {"ok": True, "charged": 0, "balance": 90, "free": True}
    m = mocks(spend=free)
    with m[0], m[1], m[2], m[3]:
        call_analyze(tasks=tasks)
    assert tasks.tasks[0].args == (GEN_ID, free)


# ── re-roll pricing: count_prior_debits ─────────────────────────────────────
# A refunded debit's work failed and its credits went back, so it must not
# turn the next attempt into a (cheaper) re-roll. Both refund shapes count as
# refunded: 006 ('refund:<debit id>', kind 'refund') and pre-006 (kind
# 'grant', per-reference key, but metadata.refund_of names the debit).

class _FakeLedgerQuery:
    def __init__(self, rows, error=None):
        self._rows, self._error = rows, error

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def execute(self):
        if self._error:
            raise self._error
        return type("Resp", (), {"data": self._rows})()


def _count_prior(rows, error=None):
    from app.db import repository

    client = type("Client", (), {"table": lambda self, _name: _FakeLedgerQuery(rows, error)})()
    with patch.object(repository, "get_supabase", return_value=client):
        return asyncio.run(repository.count_prior_debits("chamak_generation", GEN_ID))


def test_prior_debits_none_on_a_fresh_generation():
    assert _count_prior([]) == 0


def test_prior_debits_counts_a_kept_charge():
    assert _count_prior([{"id": "d1", "kind": "debit", "idempotency_key": "k1", "metadata": {}}]) == 1


def test_prior_debits_ignores_a_charge_refunded_after_006():
    rows = [
        {"id": "d1", "kind": "debit", "idempotency_key": "k1", "metadata": {}},
        {"id": "r1", "kind": "refund", "idempotency_key": "refund:d1", "metadata": {"refund_of": "d1"}},
    ]
    assert _count_prior(rows) == 0


def test_prior_debits_ignores_a_charge_refunded_before_006():
    rows = [
        {"id": "d1", "kind": "debit", "idempotency_key": "k1", "metadata": {}},
        {"id": "r1", "kind": "grant", "idempotency_key": f"refund:chamak_generation:{GEN_ID}",
         "metadata": {"refund_of": "d1"}},
    ]
    assert _count_prior(rows) == 0


def test_prior_debits_counts_only_the_charges_that_were_kept():
    rows = [
        {"id": "d1", "kind": "debit", "idempotency_key": "k1", "metadata": {}},
        {"id": "r1", "kind": "refund", "idempotency_key": "refund:d1", "metadata": {"refund_of": "d1"}},
        {"id": "d2", "kind": "debit", "idempotency_key": "k2", "metadata": {}},
    ]
    assert _count_prior(rows) == 1


def test_prior_debits_falls_back_to_the_first_generation_price_when_unreadable():
    assert _count_prior([], error=RuntimeError("ledger down")) == 0
