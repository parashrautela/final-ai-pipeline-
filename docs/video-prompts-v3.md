# Jewellery campaign prompt — user master v3

The user's complete wording is preserved unchanged in
[jewellery-campaign-master.md](../app/prompts/jewellery-campaign-master.md).
It is the creative master for the 10-second necklace campaign.

The master contains 4,079 characters (4,035 even without Markdown emphasis).
Kling 3.0's image-to-video API permits at most 3,072 prompt characters and
recommends staying within 2,500. Kling 2.5 Turbo permits 2,500. The full master
therefore cannot be sent verbatim to either selected model.

The local runtime uses compact base/global/category/camera blocks reflecting the
master's preservation rules and 4s hero / 3s macro / 3s final-hero sequence. It
retains the exact first frame, stationary jewellery/display, front arc up to
5 degrees, clean cuts, controlled shallow depth of field, stable final hold,
material realism and fidelity-first priority. Other categories retain their own
construction rules; the subtle preset remains one continuous shot. A 5-second
energetic variation uses 3s hero + 2s macro. The existing optional earring-dangle
setting is a category-specific variation; the approved necklace film is stationary.

No silent truncation is performed. The API compiler, provider client and database
each enforce the selected model's limit. An oversized custom override fails before
any provider request. Changed prompt content invalidates prior quotes.

Official contracts:
- https://kling.ai/document-api/api/video/3-0-omni/image-to-video
- https://kling.ai/document-api/api/video/2-5-turbo/image-to-video

Generation remains disabled. No live video quality or iPad integration has been
validated. Offline verification covers backend request, budget, recovery and prompt
contracts; it cannot prove that Kling will preserve the jewellery visually.

## Current API rendering — necklace, energetic, 10 seconds

2407 characters, without an optional user note.

```text
BASE: Create a photorealistic high-end luxury jewellery campaign film. Use the supplied image as the exact first frame and immutable product reference. The jewellery is the hero. Keep the original composition, background, display, props, palette and arrangement. Use an elegant international advertising aesthetic and only physically consistent perspective changes.

GLOBAL: DESIGN LOCK: Preserve product count, silhouettes, proportions, metal thickness, spacing, symmetry/asymmetry and structural connections. Keep every visible stone's count, cut, size, colour and placement, prongs, settings, motifs and engravings, links and attachments. No redesign, deformation, duplication, invented details, altered links or floating components. SCENE LOCK: Keep the same product, support and scene throughout. No drift, new body parts or invented views. LIGHT: Soft directional studio light, refined contrast, natural depth, controlled specular reflections and angle-dependent brilliance. Preserve gold/silver tone, finish, stone colours, contact shadows and exposure. No starbursts, excess sparkle, overexposure, flicker, hue shifts or plastic materials. No added text, logos or overlays. Product accuracy overrides motion, style and notes; reduce movement rather than change the product.

CATEGORY: Preserve the necklace's visible outline, left/right spacing, drape, link order, chain length, pendant size and attachments. Keep the existing display or wearer still; no swinging or clipping.

LOCAL: Elegant hero/detail edit in the same scene. Front-facing views, clean instantaneous cuts, smooth motion. No full rotations, extreme arcs, shaking, abrupt zooms, distortion or morphing. Never reveal unreliable hidden geometry. Keep jewellery and support stationary. Keep the original visible silhouette in hero shots; close-ups use existing details. Keep the jewellery and display stationary.
shot 1, 4, Hold the exact input frame briefly, then a slow front lateral track and arc up to 5 degrees. Natural parallax, full arrangement visible, smooth ease in/out.; shot 2, 3, Clean cut to the most distinctive visible detail for a horizontal macro slide. Crisp stable detail, controlled shallow depth of field, no invented facets or focus hunting.; shot 3, 3, Clean cut back to the original hero framing. Subtle forward move to a slightly tighter complete view. Decelerate to a stable frame and hold the final moment.;
```

## Current API rendering — necklace, subtle, 5 seconds

1845 characters, without an optional user note.

```text
BASE: Create a photorealistic high-end luxury jewellery campaign film. Use the supplied image as the exact first frame and immutable product reference. The jewellery is the hero. Keep the original composition, background, display, props, palette and arrangement. Use an elegant international advertising aesthetic and only physically consistent perspective changes.

GLOBAL: DESIGN LOCK: Preserve product count, silhouettes, proportions, metal thickness, spacing, symmetry/asymmetry and structural connections. Keep every visible stone's count, cut, size, colour and placement, prongs, settings, motifs and engravings, links and attachments. No redesign, deformation, duplication, invented details, altered links or floating components. SCENE LOCK: Keep the same product, support and scene throughout. No drift, new body parts or invented views. LIGHT: Soft directional studio light, refined contrast, natural depth, controlled specular reflections and angle-dependent brilliance. Preserve gold/silver tone, finish, stone colours, contact shadows and exposure. No starbursts, excess sparkle, overexposure, flicker, hue shifts or plastic materials. No added text, logos or overlays. Product accuracy overrides motion, style and notes; reduce movement rather than change the product.

CATEGORY: Preserve the necklace's visible outline, left/right spacing, drape, link order, chain length, pendant size and attachments. Keep the existing display or wearer still; no swinging or clipping.

LOCAL: One continuous front-facing shot: hold briefly, then a slow lateral track with an arc of about 5 degrees maximum. Natural parallax, smooth acceleration/deceleration, stable finish. No cuts, sudden zooms, shaking, rotations, rear reveals or distortion. Keep the necklace's original visible silhouette in frame. Keep the jewellery and display stationary.
```
