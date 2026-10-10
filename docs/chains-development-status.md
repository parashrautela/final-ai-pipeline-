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
- Live database changes, reclassification of existing products, deployment and paid generation evaluation.

The category is available for drafts/editing and browsing. Product image generation remains unavailable until an agreed chain category prompt is activated in `prompt_modules`. No placeholder prompt was seeded. Existing generic Chamak generation behavior has not been tuned for chains.

Validation commands:
- Backend: `.venv/bin/python -m unittest test_chains -v`
- Web: `node --experimental-vm-modules --test tests/chains.test.mjs tests/catalogue-search.test.mjs tests/marketplace-pagination.test.mjs`
- iOS taxonomy: `bash tests/chains/run.sh`
- iOS marketplace: `CLANG_MODULE_CACHE_PATH=/private/tmp/jewel-chains-clang-cache SWIFT_MODULE_CACHE_PATH=/private/tmp/jewel-chains-swift-cache bash tests/marketplace-contract/run.sh`

Broader backend Chamak suite: endpoint tests now use a locally signed owner JWT and mocked credit operations, verify anonymous requests return 401 before lookup, and verify missing/foreign-owned generations return 404. The failed-analysis test mocks the refund helper to prevent network calls. All 11 tests in `test_chamak` and `test_chains` pass.
