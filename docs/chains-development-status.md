# Chains category development

Implemented in the AI backend, web client and wholesaler iOS client:
- Canonical jewellery type `chain`, display name Chains; aliases chain/chains/neck chain/neck chains.
- Plain standalone neck chains without pendants; references describe category scope only.
- Product creation/edit choices and web save normalization.
- Catalogue and marketplace alias filtering, combined marketplace counts, and image-search normalization.
- Chamak picker and canonical category recognition; backend/iOS set uniqueness checks recognize chain aliases.
- Product-generation validation and prompt composition require an active chain prompt module instead of silently using `other`.

Deferred deliberately:
- Product image-generation prompt and presentation scenes.
- Chain-specific Chamak analysis/Fusion prompts and transformation behavior.
- Chain-specific Set Creation staging and any chain/pendant attachment behavior.
- Reclassification of existing products, deployment and paid generation evaluation.

The category is available for drafts/editing and browsing. Product image generation remains unavailable until an agreed chain category prompt is activated in `prompt_modules`. An inactive shared-rules chain draft is now registered in the live database; the four chain scenes still need to be finalized before activation. Existing generic Chamak generation behavior has not been tuned for chains.

Validation commands:
- Backend: `.venv/bin/python -m unittest test_chains -v`
- Web: `node --experimental-vm-modules --test tests/chains.test.mjs tests/catalogue-search.test.mjs tests/marketplace-pagination.test.mjs`
- iOS taxonomy: `bash tests/chains/run.sh`
- iOS marketplace: `CLANG_MODULE_CACHE_PATH=/private/tmp/jewel-chains-clang-cache SWIFT_MODULE_CACHE_PATH=/private/tmp/jewel-chains-swift-cache bash tests/marketplace-contract/run.sh`

Broader backend Chamak suite: endpoint tests now use a locally signed owner JWT and mocked credit operations, verify anonymous requests return 401 before lookup, and verify missing/foreign-owned generations return 404. The failed-analysis test mocks the refund helper to prevent network calls. All 11 tests in `test_chamak` and `test_chains` pass.

## Prompt registration — 10 October 2026

- Registered live `prompt_modules` category `chain`, version 1, `is_active=false`; verified by a subsequent read. Row ID: `3c6c201b-501e-43c7-9306-520cfaba1047`.
- Added migration `025_chain_prompt_module.sql` with the same draft rules. It preserves any existing module under chain aliases and can be re-run without overwriting authored prompts.
- Shared rules follow the live global/category prompt conventions: commercial-tag removal, exact source fidelity, link continuity, standalone-chain identity, physical realism and square single-image output.
- No live global prompt or existing category was changed. Chain generation remains gated pending four approved scenes.
- Blank category prompt rows now cannot enable generation (local backend change; not deployed).
- Backend validation: 13 tests pass in `test_chains` and `test_chamak`. Added coverage for blank chain prompts and the ordered first-N scene selection for 1–4 images. No paid AI generation was run.

## Supabase prompt v2 update — 10 October 2026

Saved and read back the active global base v2 and inactive chain category v2. Previous rows are backed up in `docs/prompts/prompt-modules-before-v2-update.json`; verified current rows are in `prompt-modules-after-v2-update.json`. The chain module contains shared rules and four `SCENE N —` sections in reference order.

Local backend now separates the four sections and appends only the selected scene per request. First-N selection is preserved. Oversized scene-aware prompts fail before paid generation rather than losing category rules to truncation. Maximum-title compositions measured 4,864–4,943 characters.

The compact global removes the model's independent three-output default, adds Chains, and preserves universal fidelity, commercial cleanup, source-supported details, material realism, physical support and square output rules. Other category rows were not rewritten and some still exceed the client limit when combined with the new base; this update guarantees the measured budget for Chains only.

Chains remains inactive because the scene-aware backend is not deployed. Deploy these backend changes before activation; the old backend does not split the stored scene sections. No paid generation or visual evaluation has been performed. Migration 026 preserves already-versioned modules.

## Chains live activation — 10 October 2026

Deployed backend commit `03364b1` to Railway; GitHub reports deployment success and production `/health` returns `category_variant_scenes=true`. Activated chain prompt v2 in Supabase and verified `is_active=true` by a fresh read. Global and category versions are both 2; four maximum-title composed prompts measure 4,864–4,943 characters.

A live `/process` request for `chains` with a deliberately unsupported text file passed category validation and returned 415 for the file type, proving the earlier inactive-prompt 422 is gone. It created no product and ran no paid generation. Evidence: `docs/prompts/chains-live-verification.json`. The 16 local chain/Chamak tests pass. Real-image visual evaluation remains pending.
