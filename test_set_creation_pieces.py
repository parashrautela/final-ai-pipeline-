"""Set Creation with two to four pieces.

Run:  PYTHONPATH=<pytest site-packages> .venv/bin/python -m pytest test_set_creation_pieces.py -v
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from starlette.background import BackgroundTasks

from app.services.chamak import (
    SET_MAX_PIECES,
    build_set_creation_prompt,
    set_source_urls,
)
from app.validation import ChamakGenerationRequest
from test_credits import OWNER, _settings, make_request  # noqa: F401  (fixture)

GEN = str(uuid.uuid4())


def row(pieces: int) -> dict:
    r = {"id": GEN, "wholesaler_id": OWNER, "mode": "set_creation", "status": "queued"}
    for slot in range(1, pieces + 1):
        r[f"source_image_{slot}_url"] = f"https://x/{slot}.jpg"
    return r


# ── prompt ───────────────────────────────────────────────────────────────────

def test_two_piece_prompt_is_unchanged():
    text = build_set_creation_prompt("velvet_bust", None)
    assert "two photographs of two separate" in text
    assert "Image 1 and Image 2." in text
    assert build_set_creation_prompt("velvet_bust", None, pieces=2) == text


@pytest.mark.parametrize("pieces,word", [(3, "three"), (4, "four")])
def test_bigger_sets_name_every_piece(pieces, word):
    text = build_set_creation_prompt(None, "warm light", pieces=pieces)
    assert f"{word} photographs of {word} separate" in text
    assert f"Image {pieces}" in text
    assert f"ALL {word.upper()} pieces together" in text
    # Nothing still talks about a pair.
    for leftover in ("BOTH", "either piece", "two pieces", "third piece", "the other."):
        assert leftover not in text, leftover
    # The two-piece prompt is already over the image API's 5000-char cap and
    # is cut there (a known, separate problem); a bigger set must not make
    # that worse.
    assert len(text) <= len(build_set_creation_prompt(None, "warm light")) + 60


@pytest.mark.parametrize("pieces", [3, 4])
def test_bigger_sets_without_a_note(pieces):
    text = build_set_creation_prompt(None, None, pieces=pieces)
    assert "BOTH" not in text and "either piece" not in text


def test_out_of_range_piece_count_is_refused():
    with pytest.raises(ValueError):
        build_set_creation_prompt(None, None, pieces=5)
    with pytest.raises(ValueError):
        build_set_creation_prompt(None, None, pieces=1)


def test_source_urls_are_read_in_slot_order_and_stop_at_a_gap():
    assert set_source_urls(row(4)) == [f"https://x/{i}.jpg" for i in range(1, 5)]
    gap = row(2) | {"source_image_4_url": "https://x/4.jpg"}
    assert set_source_urls(gap) == ["https://x/1.jpg", "https://x/2.jpg"]
    assert SET_MAX_PIECES == 4


# ── pricing ──────────────────────────────────────────────────────────────────

def call(pieces: int, prior: int = 0):
    from app.main import set_creation_generate

    paid = {"ok": True, "charged": 1, "balance": 1, "ledger_id": str(uuid.uuid4())}
    with patch("app.main.fetch_chamak_generation", AsyncMock(return_value=row(pieces))), \
         patch("app.main.update_chamak_generation", AsyncMock()), \
         patch("app.main.count_prior_debits", AsyncMock(return_value=prior)), \
         patch("app.main.spend_credits", AsyncMock(return_value=paid)) as spend:
        try:
            asyncio.run(set_creation_generate(
                request=make_request(),
                body=ChamakGenerationRequest(generation_id=GEN),
                background_tasks=BackgroundTasks(),
                user_id=OWNER,
                idempotency_key="k",
            ))
        except HTTPException as e:
            return e, spend
    return None, spend


@pytest.mark.parametrize("pieces,key", [
    (2, "chamak.set_creation"),
    (3, "chamak.set_creation_3"),
    (4, "chamak.set_creation_4"),
])
def test_price_follows_the_piece_count(pieces, key):
    err, spend = call(pieces)
    assert err is None
    assert spend.await_args.kwargs["feature_key"] == key
    assert spend.await_args.kwargs["metadata"]["pieces"] == pieces


def test_one_piece_is_refused_before_charging():
    err, spend = call(1)
    assert isinstance(err, HTTPException) and err.status_code == 422
    spend.assert_not_awaited()


def test_reroll_keeps_the_reroll_price():
    _, spend = call(4, prior=1)
    assert spend.await_args.kwargs["feature_key"] == "chamak.reroll"


# ── the bug this change also fixes ───────────────────────────────────────────

def test_a_finished_set_stays_done_and_is_not_refunded():
    """It used to log a name that doesn't exist after saving the result, which
    threw, marked the set failed and refunded it."""
    from app.services import chamak

    updates = []
    stored = type("S", (), {"url": "https://x/out.png", "variants": {}})()
    with patch.object(chamak, "fetch_chamak_generation", AsyncMock(return_value=row(3))), \
         patch.object(chamak, "update_chamak_generation",
                      AsyncMock(side_effect=lambda _id, data: updates.append(data))), \
         patch.object(chamak.nanobana_client, "compose_set", AsyncMock(return_value=[b"img"])) as compose, \
         patch.object(chamak, "upload_chamak_output", lambda **_: stored), \
         patch.object(chamak, "_refund_failed_generation", AsyncMock()) as refund:
        asyncio.run(chamak.run_set_creation_generation(GEN, {"ledger_id": "L"}))

    assert len(compose.await_args.args[0]) == 3
    assert updates[-1]["status"] == "done"
    refund.assert_not_awaited()
