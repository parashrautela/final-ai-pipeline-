#!/usr/bin/env python3
"""Run the image-variant catch-up by hand.

The server already does this on its own shortly after starting (see
`app/services/backfill.py`), so this exists for when you want to watch it, or
to run it somewhere other than the API container. It needs the pipeline's
environment, because it uses the same service-role key the uploader does.

    python scripts/backfill_image_variants.py --list      # what's outstanding
    python scripts/backfill_image_variants.py --limit 5   # convert a few
    python scripts/backfill_image_variants.py             # convert everything

Safe to stop and re-run: each file's name is a hash of its own bytes, so a
second run writes the same paths instead of piling up copies, and rows that
already have every size are skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.backfill import (  # noqa: E402
    CONCURRENCY,
    _chamak_rows_needing_variants,
    _product_rows_needing_variants,
    run_backfill,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--limit", type=int, help="stop after this many rows")
    parser.add_argument("--jobs", type=int, default=CONCURRENCY, help="images at once")
    parser.add_argument("--list", action="store_true", help="report what's outstanding, change nothing")
    args = parser.parse_args()

    if args.list:
        products = _product_rows_needing_variants(args.limit)
        chamak = _chamak_rows_needing_variants(args.limit)
        images = sum(len(job["urls"]) for job in products) + len(chamak)
        print(f"{len(products)} product row(s), {len(chamak)} chamak generation(s): {images} image(s) to convert")
        return 0

    done = asyncio.run(run_backfill(limit=args.limit, concurrency=args.jobs))
    print(
        f"Converted {done['images']} image(s) across {done['products']} product row(s) "
        f"and {done['chamak']} generation(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
