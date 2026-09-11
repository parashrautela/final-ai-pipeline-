"""Tests for the catch-up that converts images stored before variants existed.

It runs unattended in production against real rows, so what matters is that
it converts only what is missing, records exactly what it wrote, and never
loses variants that were already there.
"""

import asyncio

import pytest

from app.services import backfill


# ─────────────────────────────────────────────────────────────────────────────
# A Supabase stand-in: just the chain the module actually uses.
# ─────────────────────────────────────────────────────────────────────────────

class FakeQuery:
    def __init__(self, rows, updates, table):
        self._rows, self._updates, self._table = rows, updates, table
        self._payload = None
        self._limit = None
        self.not_ = self

    def select(self, *_args, **_kw):
        return self

    def order(self, *_args, **_kw):
        return self

    def eq(self, column, value):
        if self._payload is not None:
            self._updates.append((self._table, value, self._payload))
        return self

    def is_(self, *_args):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def update(self, payload):
        self._payload = payload
        return self

    def execute(self):
        rows = self._rows if self._payload is None else []
        return type("Result", (), {"data": rows[: self._limit] if self._limit else rows})()


class FakeSupabase:
    def __init__(self, tables):
        self.tables = tables
        self.updates: list[tuple[str, str, dict]] = []

    def table(self, name):
        return FakeQuery(self.tables.get(name, []), self.updates, name)


URL = "https://p.supabase.co/storage/v1/object/public/plant-images/products/a.png"
URL2 = "https://p.supabase.co/storage/v1/object/public/plant-images/products/b.png"
FULL_SET = {"card": "c.webp", "detail": "d.webp", "full": "f.webp"}


@pytest.fixture
def wired(monkeypatch):
    """Patch out the network; hand back the fake client and what was uploaded."""
    uploaded: list[tuple[str, str]] = []

    def fake_upload(_content, bucket, path):
        uploaded.append((bucket, path))
        return dict(FULL_SET)

    monkeypatch.setattr(backfill, "upload_variants", fake_upload)
    monkeypatch.setattr(backfill, "_download", lambda bucket, path: b"bytes")

    def install(tables):
        client = FakeSupabase(tables)
        monkeypatch.setattr(backfill, "get_supabase", lambda: client)
        return client, uploaded

    return install


def run(coro):
    return asyncio.run(coro)


def test_a_row_with_no_variants_is_converted_and_recorded(wired):
    client, uploaded = wired({"products": [{"id": "p1", "image_url": URL, "image_variants": None}]})

    result = run(backfill.run_backfill())

    assert uploaded == [("plant-images", "products/a.png")]
    assert result["images"] == 1
    table, row_id, payload = client.updates[0]
    assert (table, row_id) == ("products", "p1")
    assert payload == {"image_variants": {URL: FULL_SET}}


def test_a_row_that_already_has_every_size_is_left_alone(wired):
    client, uploaded = wired(
        {"products": [{"id": "p1", "image_url": URL, "image_variants": {URL: FULL_SET}}]}
    )

    result = run(backfill.run_backfill())

    assert uploaded == [], "nothing is re-uploaded"
    assert client.updates == [], "nothing is rewritten"
    assert result == {"products": 0, "chamak": 0, "images": 0}


def test_a_half_converted_row_is_finished(wired):
    client, uploaded = wired(
        {"products": [{"id": "p1", "image_url": URL, "image_variants": {URL: {"card": "c.webp"}}}]}
    )

    run(backfill.run_backfill())

    assert uploaded == [("plant-images", "products/a.png")]
    assert client.updates[0][2]["image_variants"][URL] == FULL_SET


def test_variants_already_recorded_for_other_images_survive(wired):
    client, _ = wired(
        {
            "products": [
                {
                    "id": "p1",
                    "image_url": URL,
                    "generated_image_urls": [URL2],
                    "image_variants": {URL2: FULL_SET},
                }
            ]
        }
    )

    run(backfill.run_backfill())

    recorded = client.updates[0][2]["image_variants"]
    assert set(recorded) == {URL, URL2}, "the existing entry is merged, not replaced"


def test_every_image_on_a_row_is_converted(wired):
    _, uploaded = wired(
        {
            "products": [
                {
                    "id": "p1",
                    "image_url": URL,
                    "processed_image_url": URL,  # the same image twice
                    "generated_image_urls": [URL2],
                    "image_variants": None,
                }
            ]
        }
    )

    run(backfill.run_backfill())

    assert uploaded == [
        ("plant-images", "products/a.png"),
        ("plant-images", "products/b.png"),
    ], "each distinct image once"


def test_rows_without_usable_images_are_skipped(wired):
    client, uploaded = wired(
        {
            "products": [
                {"id": "p1", "image_url": None, "image_variants": None},
                {"id": "p2", "image_url": "products/relative/path.png", "image_variants": None},
                {"id": "p3", "image_url": "https://example.com/elsewhere.png", "image_variants": None},
            ]
        }
    )

    result = run(backfill.run_backfill())

    assert uploaded == []
    assert client.updates == []
    assert result["images"] == 0


def test_one_bad_image_does_not_stop_the_others(wired, monkeypatch):
    client, uploaded = wired(
        {
            "products": [
                {"id": "p1", "image_url": URL, "image_variants": None},
                {"id": "p2", "image_url": URL2, "image_variants": None},
            ]
        }
    )

    def explode_on_a(bucket, path):
        if path.endswith("a.png"):
            raise RuntimeError("unreadable image")
        return b"bytes"

    monkeypatch.setattr(backfill, "_download", explode_on_a)

    result = run(backfill.run_backfill())

    assert uploaded == [("plant-images", "products/b.png")]
    assert [row_id for _, row_id, _ in client.updates] == ["p2"]
    assert result["images"] == 1


def test_a_row_is_not_marked_done_when_nothing_could_be_written(wired, monkeypatch):
    client, _ = wired({"products": [{"id": "p1", "image_url": URL, "image_variants": None}]})
    monkeypatch.setattr(backfill, "upload_variants", lambda *_: {})

    result = run(backfill.run_backfill())

    assert client.updates == [], "an empty result must not overwrite the column"
    assert result["images"] == 0


def test_chamak_outputs_are_stored_as_paths(wired):
    client, uploaded = wired(
        {"chamak_generations": [{"id": "g1", "output_image_url": "wholesaler/gen.png"}]}
    )

    result = run(backfill.run_backfill())

    assert uploaded == [("chamak-outputs", "wholesaler/gen.png")]
    assert client.updates[0] == ("chamak_generations", "g1", {"output_variants": FULL_SET})
    assert result["chamak"] == 1


def test_a_chamak_path_carrying_its_bucket_is_normalised(wired):
    _, uploaded = wired(
        {"chamak_generations": [{"id": "g1", "output_image_url": "chamak-outputs/w/gen.png"}]}
    )

    run(backfill.run_backfill())

    assert uploaded == [("chamak-outputs", "w/gen.png")]


def test_a_chamak_row_holding_a_url_is_skipped(wired):
    client, uploaded = wired(
        {"chamak_generations": [{"id": "g1", "output_image_url": "https://example.com/x.png"}]}
    )

    run(backfill.run_backfill())

    assert uploaded == []
    assert client.updates == []


def test_nothing_to_do_costs_nothing(wired):
    client, uploaded = wired({})

    result = run(backfill.run_backfill())

    assert result == {"products": 0, "chamak": 0, "images": 0}
    assert uploaded == []
    assert client.updates == []


def test_the_startup_task_can_be_switched_off(monkeypatch):
    called = False

    async def never(*_args, **_kw):
        nonlocal called
        called = True

    monkeypatch.setattr(backfill.settings, "IMAGE_VARIANT_BACKFILL", False)
    monkeypatch.setattr(backfill, "run_backfill", never)

    run(backfill.backfill_on_startup())

    assert called is False


def test_a_failing_backfill_never_escapes_the_startup_task(monkeypatch):
    async def explode(*_args, **_kw):
        raise RuntimeError("storage is down")

    monkeypatch.setattr(backfill.settings, "IMAGE_VARIANT_BACKFILL", True)
    monkeypatch.setattr(backfill, "STARTUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(backfill, "run_backfill", explode)

    run(backfill.backfill_on_startup())  # must not raise
