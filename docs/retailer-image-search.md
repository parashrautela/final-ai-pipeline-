# Indexed retailer image search

`POST /api/retailer/image-search` accepts multipart `photo` and `jewellery_type`, plus a Supabase bearer session. It requires both retailer user metadata and a verified retailer row, even when credits are disabled. It returns ranked `{matches: [{id, similarity}], checked, total, skipped}` with private/no-store caching. No supplier identity or product-selection write is included.

The server uses the pinned, checksum-verified CLIP ViT-B/32 quantized ONNX encoder on its own CPU. Product vectors are computed in the background and saved in the server cache. Queries encode only the uploaded photo and compare it against already indexed products of the selected jewellery type. The active published-product list is fetched per query, so an unpublished product is excluded immediately. Changing a photo URL invalidates its old embedding. Refresh runs every minute; missing vectors return a retryable 503 rather than a false “no matches”. Unavailable catalogue images are counted separately.

The initial cutoff is cosine similarity 0.90 and results are capped at 20. This is a retrieval threshold, not a measured 90% accuracy claim. Labelled jewellery evaluation is still needed for different camera angles, screenshots, backgrounds, and similar-looking designs. Matching is not evidence of ownership or copying.

The app sends one oriented, resized JPEG to this service. Query photos are not saved to product storage or sent to Jev/Hugging Face/an external inference API. Multipart temporary files are closed and removed after reading. Downloads for catalogue indexing are restricted to this project's Supabase host and Cloudinary, with no redirects, a 10 MB limit, and timeouts. Uploaded photos also have a size/pixel/aspect-ratio limit. Search does not consume generation credits.

The Docker build downloads the fixed public model once and verifies its SHA-256. Runtime index path defaults to the app user's cache; set `IMAGE_SEARCH_INDEX_PATH` on a persistent volume to retain vectors across redeploys. `IMAGE_SEARCH_MODEL_PATH` can point to the baked model. Without a persistent volume the index is rebuilt after a new deployment. Model weights/indices/credentials are not committed to Git. Existing Supabase server credentials must be valid.

Run `python tests/catalogue-search-api.test.py` for role/session/upload/readiness checks, and `IMAGE_SEARCH_MODEL_PATH=/path/to/vision.onnx python tests/catalogue-search.integration.py /path/to/read-only-fixture.json` for actual retrieval and latency. Fixture: `{reference: "/absolute/photo.jpg", category: "necklace", products: [...]}`; first product is the reference. The real-image check covers exact and app-sized self-match, unrelated-image rejection, published/category isolation, persistent cache consistency, partial failure, and invalid uploads.

A 362-product published catalogue was indexed and the exact query ranked first; the app-sized JPEG also ranked first. Warm query computation on the development machine was about 45 ms. This excludes authentication, HTTP, production hardware and network latency; do not advertise it as end-to-end app timing. The initial complete indexing run took about 149 seconds and is background work, not repeated per search.

Release order: deploy this backend, allow indexing to finish, then distribute the native app update. The existing app generation routes continue independently. There is no new database migration; the Jev flow requires the server-only key described below.


## Jev pilot decision flow

The authenticated image-search endpoint now retrieves the top 12 category candidates without a CLIP acceptance cutoff, then batches one Jev Choice per candidate in a single request. Measurements match the isolated pilot: CLIP cosine, byte/pixel equality, dHash difference and dimensions. The persistent server index stores catalogue fingerprints alongside vectors; customer fingerprints stay in query memory. No customer or catalogue photos, product IDs, supplier identities or URLs go to Jev. Anonymous pair labels identify each question.

Only actual Jev `similar` choices are returned as matches. Uncertain/different choices are exposed in decision metadata but excluded from matches. There is no CLIP-only fallback on provider failure: a retryable 503 is returned. This follows the pilot and does not establish jewellery retrieval accuracy. Missing catalogue evidence triggers a refresh/readiness response.

Production requires server-only `TYPESAFE_API_KEY`. Jev model is pinned to `jev-1.13.0`. GET `/api/retailer/image-search/status` exposes configuration presence, encoder/evidence counts and model only, never the key. The app transport must understand `decision_source=jev` and each match’s decision/probability; older builds still impose the CLIP cutoff. The UI and multipart query contract are unchanged.

Checks: `python tests/jev-catalogue.test.py` (batched actual-choice parsing, below-cutoff acceptance, uncertainty rejection, provider/incomplete-response errors, missing key/evidence, candidate recall and cache migration), plus existing access and real-encoder tests.
