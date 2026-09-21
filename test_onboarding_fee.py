"""The one-time onboarding fee: amount from settings, a Razorpay link per
payer, and confirmation read back from Razorpay. No network, no database.

Run:  PYTHONPATH=<pytest site-packages> .venv/bin/python -m pytest test_onboarding_fee.py -v
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from app import onboarding_fee as fee
from app.config import settings
from test_credits import OWNER, STRANGER, _settings, token_for  # noqa: F401  (fixture)


class FakeTable:
    """Just enough of supabase-py's query builder for these routes."""

    def __init__(self, rows):
        self.rows = rows
        self.inserted, self.updated = [], []
        self._filters = {}

    def select(self, *_a): self._filters = {}; return self
    def eq(self, k, v): self._filters[k] = v; return self
    def limit(self, _n): return self
    def insert(self, row): self.inserted.append(row); return self
    def update(self, row): self.updated.append(row); return self

    def execute(self):
        data = [r for r in self.rows if all(r.get(k) == v for k, v in self._filters.items())]
        return MagicMock(data=data)


def db(rows):
    table = FakeTable(rows)
    client = MagicMock()
    client.table.return_value = table
    return patch.object(fee, "get_supabase", lambda: client), table


def razorpay(status_code=200, payload=None):
    """Patch httpx.AsyncClient so post/get answer with `payload`."""
    response = httpx.Response(status_code, json=payload or {})

    class Client:
        def __init__(self, *a, **k): self.calls = []
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, auth=None):
            Client.last = ("post", url, json)
            return response
        async def get(self, url, auth=None):
            Client.last = ("get", url, None)
            return response

    return patch.object(fee.httpx, "AsyncClient", Client), Client


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_test")
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "secret")
    monkeypatch.setattr(settings, "ONBOARDING_FEE_INR", 9)


def bearer(user=OWNER):
    return f"Bearer {token_for(user)}"


def test_status_reports_the_amount_from_settings(monkeypatch):
    p, _ = db([])
    with p:
        out = asyncio.run(fee.fee_status(authorization=bearer()))
    assert out == {"amount_inr": 9, "required": True, "payable": True, "paid": False}

    monkeypatch.setattr(settings, "ONBOARDING_FEE_INR", 0)
    with p:
        assert asyncio.run(fee.fee_status(authorization=None))["required"] is False


def test_fee_without_razorpay_keys_is_not_payable_so_it_blocks_nobody(monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "")
    p, _ = db([])
    with p:
        assert asyncio.run(fee.fee_status(authorization=None))["payable"] is False


def test_pay_makes_a_link_for_the_caller_that_the_credit_webhook_cannot_read():
    p, table = db([])
    r, client = razorpay(payload={"id": "plink_1", "short_url": "https://rzp.io/x"})
    with p, r:
        out = asyncio.run(fee.create_fee_link(authorization=bearer()))
    assert out["url"] == "https://rzp.io/x" and out["link_id"] == "plink_1"
    _, _, body = client.last
    assert body["amount"] == 900
    assert body["notes"] == {"purpose": "onboarding_fee", "user_id": OWNER, "source": "app"}
    assert "wholesaler_id" not in body["notes"]
    assert table.inserted[0]["user_id"] == OWNER


def test_pay_when_already_paid_makes_no_link():
    p, table = db([{"user_id": OWNER, "status": "paid", "link_id": "plink_0", "amount_paise": 900, "id": "r"}])
    r, client = razorpay()
    with p, r:
        out = asyncio.run(fee.create_fee_link(authorization=bearer()))
    assert out == {"required": True, "paid": True}
    assert not table.inserted


def test_pay_needs_a_session():
    with pytest.raises(HTTPException) as e:
        asyncio.run(fee.create_fee_link(authorization=None))
    assert e.value.status_code == 401


def test_confirm_records_a_paid_link():
    row = {"id": "r1", "user_id": OWNER, "link_id": "plink_1", "amount_paise": 900, "status": "created"}
    p, table = db([row])
    r, _ = razorpay(payload={
        "status": "paid", "amount_paid": 900, "notes": {"user_id": OWNER},
        "payments": [{"payment_id": "pay_1", "status": "captured"}],
    })
    with p, r:
        out = asyncio.run(fee.confirm_fee(fee.ConfirmBody(link_id="plink_1"), authorization=bearer()))
    assert out == {"paid": True}
    assert table.updated[0]["status"] == "paid" and table.updated[0]["payment_id"] == "pay_1"


def test_confirm_refuses_an_unpaid_or_short_link():
    row = {"id": "r1", "user_id": OWNER, "link_id": "plink_1", "amount_paise": 900, "status": "created"}
    for payload in ({"status": "created", "amount_paid": 0, "notes": {"user_id": OWNER}},
                    {"status": "paid", "amount_paid": 100, "notes": {"user_id": OWNER}}):
        p, table = db([dict(row)])
        r, _ = razorpay(payload=payload)
        with p, r:
            out = asyncio.run(fee.confirm_fee(fee.ConfirmBody(link_id="plink_1"), authorization=bearer()))
        assert out["paid"] is False
        assert not table.updated


def test_confirm_on_someone_elses_link_says_unpaid():
    row = {"id": "r1", "user_id": OWNER, "link_id": "plink_1", "amount_paise": 900, "status": "created"}
    p, table = db([row])
    r, client = razorpay(payload={"status": "paid", "amount_paid": 900, "notes": {"user_id": OWNER}})
    with p, r:
        out = asyncio.run(fee.confirm_fee(fee.ConfirmBody(link_id="plink_1"), authorization=bearer(STRANGER)))
    assert out == {"paid": False}
    assert not table.updated
