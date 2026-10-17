# Chains prompt authoring checklist

Based on the live `public.prompt_modules` snapshot fetched on 10 October 2026: one active global base, nine active category prompts, and the inactive Chains draft. Full text is saved in [all-prompt-modules.md](all-prompt-modules.md), with structured records in [prompt-modules-snapshot.json](prompt-modules-snapshot.json).

The task is to author a Chains category module and four ordered scene prompts that work with the universal base. A stronger Chains prompt does not require a separate competing global prompt.

## 1. Resolve current conflicts before authoring

- [ ] Fit every complete outgoing prompt within the current client's 5,000-character budget: global rules + Chains rules + exactly one selected scene + substituted product description + separators. The fetched global alone is 16,739 characters; the Chains draft is 4,181. Current truncation retains only the beginning and final 400 characters, dropping most category instructions. Adding more prose cannot fix this. Author compact runtime rules or change how full modules are delivered, then measure the actual outgoing text.
- [ ] Separate output-count selection from image instructions. The current global says three images by default; the application requests 1–4 separate generations. Each request must instruct the model to produce one square photograph for its selected scene, never a collage or all four scenes.
- [ ] Use four chain-specific scenes instead of automatically appending the current generic navy stone, burgundy cushion, white marble and floating charcoal scenes. These are existing implementation defaults, not approved chain inspirations.
- [ ] Remove the unsupported-floating instruction from the chain path. The existing fourth generic scene requests floating jewellery and a contact shadow; this conflicts with the global physical-realism and shadow rules.
- [ ] Add Chains to the universal category list and the Other module's known-category exclusions when revising those modules. The current lists omit Chains. This is a consistency update, not a substitute for activating the chain category row.
- [ ] State background colour and material in each approved scene. Some existing category prompts defer fabric colour to the base, but the fetched base provides a broad visual philosophy rather than four explicit palettes.
- [ ] Decide which source pose properties stay fixed and which may change through natural articulation. Preserve construction, actual length/proportions and connections; do not simultaneously require an identical photographic silhouette and a materially different drape.

## 2. Preserve universal rules

- [ ] Treat the wholesaler photograph as the exact product authority; inspiration controls photography and environment only.
- [ ] Preserve exact quantity, components, construction, proportions, material appearance, craftsmanship and intentional asymmetry across all four outputs.
- [ ] Remove commercial price tags, labels, barcodes, SKU text, seller markings, watermarks, paper and their non-jewellery attachments completely. Preserve actual clasps, connectors and manufactured product markings.
- [ ] Reconstruct exposed background only; never invent jewellery behind a removed tag or recover hidden details by guessing.
- [ ] Improve exposure, colour casts, noise, compression and sharpness without changing the product or creating details absent from the source.
- [ ] Keep each output exactly square, the product dominant, and its defining structure readable.
- [ ] Use coherent lighting, reflections and shadows; no plastic metal, neon gold, fake sparkle, halos or erased edges.
- [ ] Preserve ambiguous source areas without guessed link patterns, hidden clasps, hallmarks or engravings.

Sources: universal base and the recurring fidelity, commercial-removal and final-validation rules in every active category.

## 3. Define Chains-specific identity and structure

- [ ] Define the category as standalone neck chains without added pendants. Make the chain itself the hero product.
- [ ] Never add charms, central drops, medallions, decorative stones, beads or extra strands to fill visual space.
- [ ] Preserve real unexpected source components instead of removing them to force category conformity; flag a mismatched product category for review.
- [ ] Preserve the exact source-supported link shape, pitch, spacing, width, thickness, pattern, surface finish and strand count. Do not normalize all products into one generic chain style.
- [ ] Keep links individually legible and continuously connected; no fused links, smooth metal ribbons, gaps, impossible intersections or erased negative space.
- [ ] Preserve visible clasp, end fittings and connector relationships without inventing a hidden back side or closing an unsupported loop.
- [ ] Preserve distinct strands and actual construction; never duplicate one chain into a layered set.
- [ ] Specify natural gravity, weight, tension and articulation. Flexible sections drape; rigid sections retain their form; do not stretch, compress or kink links.
- [ ] Match support and shadows to the scene. Suspended sections have believable support and gravity; resting sections contact their surface naturally.
- [ ] Keep the complete source-supported product visible by default. Permit any detail crop only explicitly in the chosen scene, with complete-product images prioritized first.

Adapt link continuity and gravity from Necklace, Haram and Mangalsutra; do not inherit their pendant, sacred-bead or mandatory U-shape assumptions. Adapt fabric contact from Rings/Bangles/Pendant only for approved resting scenes. Nosepins demonstrate that human presentation requires explicit category authorization; they do not authorize a human chain scene automatically. Earrings provide suspension principles, not a requirement that every chain scene must hang.

## 4. Specify four ordered scenes from the inspirations

Four chain inspiration photographs are now supplied and saved unmodified in `chain-references/`. The draft uses their order: white steps, charcoal curved support, emerald velvet rolls, and burgundy blocks. Full specifications are in `chains-master-v2.md`; complete provider-sized prompts are in `chains-runtime-v2.json`.

For each slot, complete the following specification:

| Property | Scene 1 | Scene 2 | Scene 3 | Scene 4 |
|---|---|---|---|---|
| Inspiration and visual intent | White architecture | Charcoal sculpture | Emerald textile | Burgundy geometry |
| Support / resting / worn presentation | Stepped plinths | Rounded support | Velvet rolls | Block top with hanging runs |
| Background material and colour | Matte neutral white | Matte charcoal | Emerald/teal short-pile velvet | Matte/satin wine-magenta |
| Chain pose and allowed articulation | Supported step drape | Elongated supported loop/curve | Contour-following drape | Source-compatible front curve |
| Camera angle and framing | Elevated three-quarter | Elevated diagonal | Near-front, slight elevation | Front, slight elevation |
| Lighting direction, softness and fill | Upper-left, moderate softness/fill | Neutral upper-left key/fill | Soft upper-left, neutral fill | Directional upper-left, neutral fill |
| Contact points and shadow behaviour | Landings/edge; physically projected face shadows | Crest/contour/foreground contacts | Subtle pile compression/contact shadows | Top support; offset projected shadows below |
| Complete product or explicit detail crop | Complete supported product | Complete supported product | Complete supported product | Complete supported product |

- [ ] Make Scene 1 a useful standalone catalogue image that clearly identifies the full product.
- [ ] Make Scene 2 add a distinct, useful view so the two-image option is a complete product presentation. These first-two priorities are authoring recommendations, not requirements found in the existing modules.
- [ ] Make Scenes 3 and 4 add intentional variety while preserving product identity and quantity.
- [ ] Define each scene independently; it must not depend on seeing a previous generated image.
- [ ] Author each final prompt as compact global rules + shared chain rules + its single scene. Do not send all four scene descriptions in each image request.
- [ ] Select the first N scenes deterministically: 1 → [1], 2 → [1,2], 3 → [1,2,3], 4 → [1,2,3,4]. Preserve this order when provider jobs finish at different times.
- [ ] Use the same raw source photo for every selected scene; do not feed previous AI outputs into later scenes.

## 5. Validate and activate

- [ ] Review all four compiled prompts for contradictions, clear support physics, complete fidelity rules and the measured character budget. Essential rules must survive intact.
- [ ] Test chain/chains/neck chain/neck chains aliases against the canonical `chain` module.
- [ ] Verify first-N selection and saved output order for all requested counts. Existing mocked backend checks cover 1–4 selection; repeat after adding chain-specific routing.
- [ ] Evaluate representative source photographs: fine and broad chains, different visible link constructions, open ends and visible clasps, long chains, obstructing commercial tags, and poor-quality input. Include genuine multi-strand construction only if supported by the source.
- [ ] Compare each output directly with the source for link pattern, thickness, clasp, quantity, metal tone, complete structure and commercial removal. A beautiful altered chain fails.
- [ ] Reject invented links/pendants, fused or broken strands, missing real components, floating support, fake contact shadows, excessive blur, cropping of required structures and commercial markings.
- [ ] Version the final module and scene definitions, then activate the completed chain module. Keep a recoverable previous version and verify the active row after the change.
- [ ] Verify the deployed backend picks up the final category and scene definitions, including prompt-cache refresh and actual outgoing prompt contents. Local test success alone does not confirm deployment.

Current status: all source prompts fetched; Chains has an inactive draft; source fidelity and ordered image selection have local backend coverage. Four reference-based scenes and compact runtime prompts are now drafted locally. Runtime prompts measure 4,694–4,773 characters each. Production delivery still uses the old long global and generic scenes: chain-specific routing, visual evaluation and activation remain pending. This checklist does not change any live prompt.

## Supabase v2 save

The compact global v2 is saved and active; chain shared rules and four scenes are saved as inactive v2. Local routing now extracts one selected scene from the database module and rejects oversized prompts before generation. All 16 chain/Chamak checks pass. Maximum-title prompts measure 4,864–4,943 characters. Deployment, visual evaluation and chain activation remain pending. Previous modules are backed up locally.

## Chains live activation — 10 October 2026

Deployed backend commit `03364b1` to Railway; GitHub reports deployment success and production `/health` returns `category_variant_scenes=true`. Activated chain prompt v2 in Supabase and verified `is_active=true` by a fresh read. Global and category versions are both 2; four maximum-title composed prompts measure 4,864–4,943 characters.

A live `/process` request for `chains` with a deliberately unsupported text file passed category validation and returned 415 for the file type, proving the earlier inactive-prompt 422 is gone. It created no product and ran no paid generation. Evidence: `docs/prompts/chains-live-verification.json`. The 16 local chain/Chamak tests pass. Real-image visual evaluation remains pending.
