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
    """
    from app.main import limiter

    monkeypatch.setattr(settings, "SUPABASE_JWT_SECRET", JWT_SECRET)
    monkeypatch.setattr(settings, "CREDITS_ENABLED", True)
    monkeypatch.setattr(limiter, "enabled", False)


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


def test_idempotency_key_header_is_passed_straight_through():
    m = mocks(spend={"ok": True, "charged": 10, "balance": 90})
    with m[0], m[1], m[2], m[3] as spend:
        call_generate(idempotency_key="user-action-abc")
    assert spend.await_args.kwargs["idempotency_key"] == "user-action-abc"


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
