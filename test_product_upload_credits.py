"""Credits for product uploads: the uploader picks how many studio images
(1–4) and pays `product.images_<n>` before any work starts.

Same approach as test_credits.py: endpoint coroutines called directly, the
repository mocked, and Supabase cut off so nothing can reach production.

Run:  PYTHONPATH=<pytest site-packages> .venv/bin/python -m pytest test_product_upload_credits.py -v
"""

from __future__ import annotations

import asyncio
import io
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, UploadFile
from starlette.background import BackgroundTasks
from starlette.datastructures import Headers

from app.config import settings
from test_credits import JWT_SECRET, OWNER, STRANGER, make_request, token_for  # noqa: F401

PRODUCT_ID = str(uuid.uuid4())
LEDGER_ID = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
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


def upload_file() -> UploadFile:
    return UploadFile(
        file=io.BytesIO(b"\xff\xd8\xff" + b"0" * 64),
        filename="ring.jpg",
        headers=Headers({"content-type": "image/jpeg"}),
    )


def call_process(*, token=None, image_count=None, key="submit-1", wholesaler_id=None, tasks=None):
    from app.main import process_upload

    return asyncio.run(process_upload(
        request=make_request(),
        background_tasks=tasks if tasks is not None else BackgroundTasks(),
        file=upload_file(),
        title="Temple Ring",
        jewellery_type="rings",
        jewelry_type=None,
        wholesaler_id=wholesaler_id,
        image_count=image_count,
        authorization=f"Bearer {token}" if token else None,
        idempotency_key=key,
    ))


def mocks(*, spend=None, spend_raises=None, create_raises=None):
    return (
        patch("app.main.validate_jewellery_type_dynamic", AsyncMock(return_value="rings")),
        patch("app.main.create_product",
              AsyncMock(return_value={"id": PRODUCT_ID, "title": "Temple Ring"},
                        side_effect=create_raises)),
        patch("app.main.upload_raw_image", lambda *_a, **_k: "https://x/raw.jpg"),
        patch("app.main.update_product_image_url", AsyncMock()),
        patch("app.main.spend_credits", AsyncMock(return_value=spend, side_effect=spend_raises)),
        patch("app.main.refund_debit", AsyncMock(return_value={"ok": True})),
    )


def run(ms, fn):
    with ms[0], ms[1] as create, ms[2], ms[3], ms[4] as spend, ms[5] as refund:
        try:
            return fn(), create, spend, refund
        except Exception as e:  # noqa: BLE001 — the tests assert on which one
            return e, create, spend, refund


PAID = {"ok": True, "charged": 600, "balance": 900, "ledger_id": LEDGER_ID}


def test_signed_in_upload_is_charged_for_the_count_chosen():
    tasks = BackgroundTasks()
    result, create, spend, refund = run(mocks(spend=PAID), lambda: call_process(
        token=token_for(OWNER), image_count=3, tasks=tasks))
    assert spend.await_args.kwargs["feature_key"] == "product.images_3"
    assert spend.await_args.kwargs["user_id"] == OWNER
    assert result["charged"] == 600 and result["image_count"] == 3
    # The pipeline is told the count and handed the charge, for a refund on failure.
    assert tasks.tasks[0].args[1:] == (3, PAID)
    refund.assert_not_awaited()


def test_product_belongs_to_the_caller_not_the_form():
    _, create, _, _ = run(mocks(spend=PAID), lambda: call_process(
        token=token_for(OWNER), image_count=2, wholesaler_id=STRANGER))
    assert create.await_args.kwargs["wholesaler_id"] == OWNER


def test_out_of_credits_is_402_and_nothing_is_created():
    short = {"ok": False, "error": "INSUFFICIENT_CREDITS", "required": 400, "balance": 100, "short_by": 300}
    err, create, _, _ = run(mocks(spend=short), lambda: call_process(token=token_for(OWNER), image_count=2))
    assert isinstance(err, HTTPException) and err.status_code == 402
    assert err.detail["short_by"] == 300
    create.assert_not_awaited()


def test_double_submit_is_refused_not_charged_twice():
    replay = {"ok": True, "replayed": True, "ledger_id": LEDGER_ID}
    err, create, _, _ = run(mocks(spend=replay), lambda: call_process(token=token_for(OWNER), image_count=2))
    assert isinstance(err, HTTPException) and err.status_code == 409
    create.assert_not_awaited()


def test_failure_after_payment_refunds_the_charge():
    err, _, _, refund = run(mocks(spend=PAID, create_raises=RuntimeError("db down")),
                            lambda: call_process(token=token_for(OWNER), image_count=2))
    assert isinstance(err, RuntimeError)
    refund.assert_awaited_once()
    assert refund.await_args.args[0] == LEDGER_ID


def test_ledger_unreachable_fails_closed():
    err, create, _, _ = run(mocks(spend_raises=RuntimeError("timeout")),
                            lambda: call_process(token=token_for(OWNER), image_count=2))
    assert isinstance(err, HTTPException) and err.status_code == 503
    create.assert_not_awaited()


def test_count_out_of_range_is_rejected():
    err, create, spend, _ = run(mocks(spend=PAID), lambda: call_process(token=token_for(OWNER), image_count=5))
    assert isinstance(err, HTTPException) and err.status_code == 422
    spend.assert_not_awaited()


def test_bad_token_is_401_never_anonymous():
    err, _, spend, _ = run(mocks(spend=PAID), lambda: call_process(token="garbage", image_count=2))
    assert isinstance(err, HTTPException) and err.status_code == 401
    spend.assert_not_awaited()


def test_no_token_is_the_old_free_path():
    """The web doesn't send a token yet; it keeps working and isn't charged."""
    result, create, spend, _ = run(mocks(spend=PAID), lambda: call_process(image_count=2, wholesaler_id=OWNER))
    spend.assert_not_awaited()
    assert result["charged"] == 0
    assert create.await_args.kwargs["wholesaler_id"] == OWNER


def test_pipeline_that_generates_nothing_refunds():
    from app.main import _run_product_pipeline

    with patch("app.main.process_product_image", AsyncMock(side_effect=RuntimeError("all failed"))), \
         patch("app.main.update_job_status", AsyncMock()), \
         patch("app.main.refund_debit", AsyncMock(return_value={"ok": True})) as refund:
        asyncio.run(_run_product_pipeline({"id": PRODUCT_ID}, 2, PAID))
    refund.assert_awaited_once()


def test_pipeline_uses_the_chosen_count(monkeypatch):
    from app.services import pipeline

    monkeypatch.setattr(settings, "TEST_MODE", False)
    seen = []

    async def fake_variant(**kw):
        seen.append(kw["variant_index"])
        return None

    composed = type("C", (), {"composed_prompt": "p", "base_module_version": 1, "category_module_version": 1})()
    with patch.object(pipeline, "_generate_variant", fake_variant), \
         patch.object(pipeline.prompt_composer, "get_composed_prompt", AsyncMock(return_value=composed)):
        with pytest.raises(RuntimeError):  # every fake variant "fails"
            asyncio.run(pipeline.process_product_image(
                {"id": PRODUCT_ID, "image_url": "https://x/raw.jpg", "title": "t"}, image_count=3))
    assert sorted(seen) == [1, 2, 3]


def test_onboarding_fee_is_published_from_settings(monkeypatch):
    from app.main import onboarding_fee
    from test_credits import make_request

    monkeypatch.setattr(settings, "ONBOARDING_FEE_INR", 9)
    assert asyncio.run(onboarding_fee(make_request())) == {"amount_inr": 9}
    monkeypatch.setattr(settings, "ONBOARDING_FEE_INR", 0)
    assert asyncio.run(onboarding_fee(make_request())) == {"amount_inr": 0}
