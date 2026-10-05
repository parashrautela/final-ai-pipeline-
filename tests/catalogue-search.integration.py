"""Real CLIP retrieval and latency against a read-only catalogue fixture.
Usage: IMAGE_SEARCH_MODEL_PATH=... python tests/catalogue-search.integration.py fixture.json
"""
import asyncio
import io
import json
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image, ImageOps
from app.services.catalogue_search import CatalogueIndex, ImageEncoder, InvalidPhoto, cache_key, source_url

async def main():
    fixture = json.loads(Path(sys.argv[1]).read_text())
    rows = [dict(row, is_published=True) for row in fixture["products"]]
    reference = Path(fixture["reference"]).read_bytes()
    encoder = ImageEncoder()
    with tempfile.TemporaryDirectory() as cache:
        hosts = {urlparse(source_url(row)).hostname for row in rows}
        index = CatalogueIndex(encoder, lambda: rows, hosts, Path(cache)/"index.json")
        started = time.perf_counter()
        await index.refresh(rows)
        assert len(index.vectors) == len(rows), "Some real catalogue images failed to index"
        print(f"Pre-indexed {len(rows)} images in {time.perf_counter()-started:.2f}s")
        started = time.perf_counter()
        result = await index.search(reference, rows, fixture["category"])
        elapsed = time.perf_counter()-started
        assert result["matches"] and result["matches"][0]["id"] == rows[0]["id"], "Identical image not ranked first"
        assert result["matches"][0]["similarity"] > 0.99
        assert all(match["similarity"] >= 0.90 for match in result["matches"])
        print(f"Exact query: {len(result['matches'])} qualifying matches, top={result['matches'][0]['similarity']:.4f}, {elapsed:.3f}s")
        # The app sends an oriented, resized JPEG, not the original file bytes.
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(reference))).convert("RGB")
        image.thumbnail((1024,1024))
        output = io.BytesIO(); image.save(output,"JPEG",quality=92)
        converted = await index.search(output.getvalue(), rows, fixture["category"])
        assert converted["matches"] and converted["matches"][0]["id"] == rows[0]["id"], "App-sized JPEG not found"
        print(f"App-sized query: top={converted['matches'][0]['similarity']:.4f}")
        # An unrelated patterned image must not be jewellery just because the
        # category was selected; this catches generic-category false positives.
        random = np.random.default_rng(7).integers(0,256,(300,300,3),dtype=np.uint8)
        output=io.BytesIO(); Image.fromarray(random).save(output,"PNG")
        unrelated=await index.search(output.getvalue(),rows,fixture["category"])
        assert not unrelated["matches"], "Unrelated image returned a jewellery match"
        # Only the current published category is eligible, even when an older
        # embedding exists in the shared cache.
        hidden=[dict(row,is_published=False) if row['id']==rows[0]['id'] else row for row in rows]
        removed=await index.search(reference,hidden,fixture["category"])
        assert rows[0]['id'] not in {match['id'] for match in removed['matches']}
        other=await index.search(reference,rows,"nonexistent")
        assert other['total']==0 and not other['matches']
        cache_index=CatalogueIndex(encoder,lambda:rows,hosts,Path(cache)/"index.json")
        warm=await cache_index.search(reference,rows,fixture["category"])
        assert warm['matches']==result['matches'], "Persisted index changed ranking"
        partial=rows+[dict(id="missing-photo",jewellery_type=fixture["category"],is_published=True)]
        index.failed.add(cache_key(partial[-1]))
        partial_result=await index.search(reference,partial,fixture["category"])
        assert partial_result['skipped']==1 and partial_result['matches'][0]['id']==rows[0]['id']
        for bad in [b"corrupt",b"x"*(10*1024*1024+1)]:
            try: encoder.embed(bad); raise AssertionError("Bad photo accepted")
            except InvalidPhoto: pass
        print("PASS: resized JPEG, unrelated rejection, unpublished/category isolation, cache reuse, partial failure, bad photos")

asyncio.run(main())
