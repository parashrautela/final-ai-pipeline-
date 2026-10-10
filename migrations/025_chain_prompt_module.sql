-- Register Chains for prompt authoring. Keep generation disabled until the
-- category prompt and its four ordered presentation scenes are finalized.
-- Re-running this migration never overwrites an existing chain module.
INSERT INTO public.prompt_modules
    (module_type, jewellery_type, prompt_text, version, is_active)
SELECT 'category', 'chain', $chain_prompt$
CHAINS

Apply these rules specifically to standalone neck chains while preserving all universal base-prompt rules. The wholesaler's source photograph is the exact approved product. Presentation references control only the environment and photography, never the chain design.

1. COMMERCIAL MARKING REMOVAL
Remove price tags, stickers, labels, barcodes, SKU/inventory markings, seller markings, watermarks, promotional text, paper cards and their non-jewellery strings, clips and holders. Preserve actual jewellery clasps and connectors. Reconstruct only the minimum exposed background; never invent jewellery hidden by a removed tag.

2. EXACT PRODUCT FIDELITY
Preserve the source-supported chain construction, overall proportions and length, width, thickness, link shape, link pitch, repeated pattern, strand count, connectors, clasp, end fittings, surface finish, engravings, metal colour and handcrafted irregularities. Preserve any actual decorative elements visible in the source. Do not add, remove, duplicate, merge, redesign or simplify components. Do not convert one chain construction into another or turn fine links into a smooth metal strip. Never infer hidden links, clasp details, hallmarks or craftsmanship from generic jewellery knowledge.

3. STANDALONE CHAIN IDENTITY
This category is a standalone neck chain without a pendant. Never add a pendant, medallion, charm, central drop, gemstone, bead, extra strand or decorative focal piece to fill empty space. Do not convert the chain into a mangalsutra, necklace set or bracelet. If the supplied product contains a pendant or other unexpected component, preserve the source rather than removing real jewellery to force category conformity.

4. CONTINUITY AND PHYSICS
Maintain continuously connected, individually readable links and genuine negative space. Links must articulate according to their actual construction. Flexible segments may drape naturally without stretching or compressing their dimensions. Rigid segments remain rigid. No broken connections, fused links, impossible twists, sharp unsupported kinks, intersections or penetration. Preserve source-supported strand separation and connection relationships. Never force symmetry that changes the product.

5. COMPLETE PRODUCT READABILITY
Keep the complete source-supported chain visible with appropriate breathing room. Do not crop product-defining ends or the clasp when visible in the source. Do not create a closed loop or clasp when the source does not support it. Camera angle and presentation must preserve readable link structure and believable scale. Jewellery remains sharp and dominant; no extreme perspective or depth-of-field blur that obscures defining details.

6. MATERIALS AND EDGES
Preserve the actual source-supported metal tone, finish, relief and texture. Reflections follow individual link geometry under one coherent lighting environment. No plastic, liquid, neon or uniformly painted metal. Preserve thin links and internal openings during background removal. No halos, thickened edges or erased connectors. Sharpen only existing information; do not fabricate detail.

7. PRESENTATION AUTHORITY
Use only the selected approved chain scene. Scene instructions may control support, background, camera and lighting while all product-fidelity rules remain binding. Natural contact shadows occur only at actual contact points. Suspended sections respond to gravity and must not receive fake surface-contact shadows. No unsupported floating, additional jewellery, human body parts or props unless explicitly authorized by the selected scene.

8. FINAL VALIDATION
Verify exact source product and quantity, unchanged link construction and proportions, correct connectors and visible clasp, continuous strands, realistic gravity and contact, complete product readability, source-accurate metal, intact negative space, and removal of commercial attachments. Never add a pendant or invent hidden jewellery. Preserve the universal exact 1:1 output requirement. Each selected scene produces one separate photograph.

THE SOURCE CHAIN IS THE AUTHORITY. IMPROVE THE PHOTOGRAPH, NOT THE DESIGN. WHEN UNCERTAIN, PRESERVE THE SOURCE; DO NOT INVENT.
$chain_prompt$, 1, false
WHERE NOT EXISTS (
    SELECT 1 FROM public.prompt_modules
    WHERE module_type = 'category'
      AND lower(regexp_replace(trim(jewellery_type), '\s+', ' ', 'g'))
          IN ('chain', 'chains', 'neck chain', 'neck chains')
);
