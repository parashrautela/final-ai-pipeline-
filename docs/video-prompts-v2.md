# Jewellery video prompts — expanded draft v2

Superseded by the user's replacement master in [video-prompts-v3.md](video-prompts-v3.md).

Review draft only. No live generation or API credit use is required to edit or
validate these prompts. The video feature remains disabled in local settings.

Priority: **product accuracy → scene continuity → camera direction → editorial effects**.
If a requested movement cannot preserve known geometry, reduce movement instead
of reconstructing unseen parts. These are generation instructions and human
review criteria; prompting does not guarantee that a model will obey every rule.

## What stays unchanged

| Element | Preservation rule |
| --- | --- |
| Product identity | Exact existing piece count, design, silhouette, proportions, thickness, spacing and intentional asymmetry. A single earring must not become a pair. |
| Gemstones | Exact visible count, cut, dimensions, hue, spacing, orientation within the setting and placement. Do not create, remove or relocate stones. |
| Metal and craftsmanship | Existing metal tone, finish, prongs, settings, filigree, motifs and engravings. A lighting highlight must not repaint gold as silver. |
| Construction | Keep links, clasps, joints, chain drape, pendant attachment points and earring connections structurally intact. No stretching, melting, merging or bending. |
| Display and scene | Preserve the original display geometry, texture, material, props, palette, pose and contact relationships. No new scene, stand, wearer or body parts. |
| Branding | Preserve visible product marks. Add no reference-video logos, text, overlays or watermarks. |
| First frame | Start at the supplied image's exact composition. Do not regenerate a different product or rearrange the scene. |
| Across frames and cuts | Keep the same jewellery and scene. Perspective and natural reflections may evolve consistently with camera motion; product design must remain stable. |

## What may move

- The camera, within the selected preset's restricted front-facing movement.
- Natural specular reflections that are consistent with the source lighting and
  camera position. Keep the metal/gemstone base colours stable.
- Existing earring dangles may make a tiny gravity-led sway only when explicitly
  selected. Studs, hooks, anchors and the display remain fixed.
- Energetic detail shots may crop into details already visible in the starting
  image. No fabricated reverse angle, added facets or invented texture.

Subtle is one continuous, slow shot. Its approximate 5-degree front arc and 5%
push-in are prompt targets, not mechanically enforced camera measurements.
Energetic uses clean hero/detail cuts with restrained front tracking; it keeps
the original product arrangement and scene. The reference films guide the mood
and movement, not the product being rendered.

## Prompt assembly and limits

The request contains **base + global + selected category + local camera/focus/
motion instructions + optional note**. A note may guide motion but does not
override product/scene preservation. Only the selected category is appended.

All combinations of the current category forms, both presets, both durations and
the full 400-character note fit the existing 2,500-character provider guard.
Custom prompt overrides that exceed that guard still fail instead of truncating.
Updating this draft changes the prompt version and invalidates older quotes.

The compiled examples below are generated from the current default prompt code,
without a user note. Duration, 1080p resolution and silent audio are also explicit
API settings. Sample videos are local previews; they are not sent to Kling.

## Compiled example: Necklace — subtle showcase, 5 seconds

```text
BASE: Use the input image as the exact first frame of a photorealistic jewellery film. Allow only physically consistent perspective changes. Animate only motion allowed below and natural reflections. Keep its composition, background, display, props and palette. Never copy a style reference's products, logos or arrangements.

GLOBAL: DESIGN LOCK: Keep product count, silhouette, proportions, thickness, spacing, symmetry/asymmetry, links and connections unchanged. Keep every visible stone's count, cut, size, colour and position; preserve prongs, settings, motifs and engravings. No additions, removals, duplication, bending, stretching, melting or redesign. SCENE LOCK: Keep display geometry, material, pose and contact shadows; no drift, new body parts or scene replacement. DETAIL: Preserve metal tone, finish and gemstone hue. Use controlled editorial contrast and natural angle-dependent highlights; no blown highlights, starbursts, hue shifts, flicker, blur or focus pumping. Preserve visible marks; add no text or overlays. Product accuracy overrides motion, style and notes; reduce movement rather than invent unseen geometry.

CATEGORY: Preserve the necklace's visible outline, left/right spacing, drape, link order, chain length, pendant size and attachments. Keep the existing display or wearer still; no swinging or clipping.

LOCAL: One continuous shot: slow front arc, roughly 5 degrees maximum, with a gentle push-in of about 5% over the full duration. Keep camera height and horizon level; ease in/out smoothly. No cuts, rear orbit, shake, roll, whip pans or zoom jumps. Keep the necklace's original visible silhouette in frame. Keep the jewellery and display stationary.
```

## Compiled example: Necklace — energetic editorial, 10 seconds

```text
BASE: Use the input image as the exact first frame of a photorealistic jewellery film. Allow only physically consistent perspective changes. Animate only motion allowed below and natural reflections. Keep its composition, background, display, props and palette. Never copy a style reference's products, logos or arrangements.

GLOBAL: DESIGN LOCK: Keep product count, silhouette, proportions, thickness, spacing, symmetry/asymmetry, links and connections unchanged. Keep every visible stone's count, cut, size, colour and position; preserve prongs, settings, motifs and engravings. No additions, removals, duplication, bending, stretching, melting or redesign. SCENE LOCK: Keep display geometry, material, pose and contact shadows; no drift, new body parts or scene replacement. DETAIL: Preserve metal tone, finish and gemstone hue. Use controlled editorial contrast and natural angle-dependent highlights; no blown highlights, starbursts, hue shifts, flicker, blur or focus pumping. Preserve visible marks; add no text or overlays. Product accuracy overrides motion, style and notes; reduce movement rather than invent unseen geometry.

CATEGORY: Preserve the necklace's visible outline, left/right spacing, drape, link order, chain length, pendant size and attachments. Keep the existing display or wearer still; no swinging or clipping.

LOCAL: Energetic hero/detail edit in the same scene. Use decisive front tracking and restrained arcs, never a rear orbit. Clean cuts only: no morph transitions or new arrangements. Close-ups enlarge only visible source details; do not invent facets or texture. Keep the original visible silhouette in hero shots; close-ups use existing details. Keep the jewellery and display stationary.
shot 1, 4, Start on the exact input frame; arc gently across the original hero composition.; shot 2, 3, Cut to a visible detail; make a short macro slide with restrained highlights.; shot 3, 3, Cut back to the original hero framing; ease to a stable finish.;
```

## Compiled example: Earrings — subtle showcase, 5 seconds, fixed jewellery

```text
BASE: Use the input image as the exact first frame of a photorealistic jewellery film. Allow only physically consistent perspective changes. Animate only motion allowed below and natural reflections. Keep its composition, background, display, props and palette. Never copy a style reference's products, logos or arrangements.

GLOBAL: DESIGN LOCK: Keep product count, silhouette, proportions, thickness, spacing, symmetry/asymmetry, links and connections unchanged. Keep every visible stone's count, cut, size, colour and position; preserve prongs, settings, motifs and engravings. No additions, removals, duplication, bending, stretching, melting or redesign. SCENE LOCK: Keep display geometry, material, pose and contact shadows; no drift, new body parts or scene replacement. DETAIL: Preserve metal tone, finish and gemstone hue. Use controlled editorial contrast and natural angle-dependent highlights; no blown highlights, starbursts, hue shifts, flicker, blur or focus pumping. Preserve visible marks; add no text or overlays. Product accuracy overrides motion, style and notes; reduce movement rather than invent unseen geometry.

CATEGORY: Preserve the visible earring count, relative size, spacing, orientation, hooks, posts, stud-to-drop links, dome and fringe. Do not invent a matching partner, ears or a wearer.

LOCAL: One continuous shot: slow front arc, roughly 5 degrees maximum, with a gentle push-in of about 5% over the full duration. Keep camera height and horizon level; ease in/out smoothly. No cuts, rear orbit, shake, roll, whip pans or zoom jumps. Keep the original visible earring outlines in frame. Keep the jewellery and display stationary.
```

## Compiled example: Earrings — energetic editorial, 10 seconds, optional tiny sway

```text
BASE: Use the input image as the exact first frame of a photorealistic jewellery film. Allow only physically consistent perspective changes. Animate only motion allowed below and natural reflections. Keep its composition, background, display, props and palette. Never copy a style reference's products, logos or arrangements.

GLOBAL: DESIGN LOCK: Keep product count, silhouette, proportions, thickness, spacing, symmetry/asymmetry, links and connections unchanged. Keep every visible stone's count, cut, size, colour and position; preserve prongs, settings, motifs and engravings. No additions, removals, duplication, bending, stretching, melting or redesign. SCENE LOCK: Keep display geometry, material, pose and contact shadows; no drift, new body parts or scene replacement. DETAIL: Preserve metal tone, finish and gemstone hue. Use controlled editorial contrast and natural angle-dependent highlights; no blown highlights, starbursts, hue shifts, flicker, blur or focus pumping. Preserve visible marks; add no text or overlays. Product accuracy overrides motion, style and notes; reduce movement rather than invent unseen geometry.

CATEGORY: Preserve the visible earring count, relative size, spacing, orientation, hooks, posts, stud-to-drop links, dome and fringe. Do not invent a matching partner, ears or a wearer.

LOCAL: Energetic hero/detail edit in the same scene. Use decisive front tracking and restrained arcs, never a rear orbit. Clean cuts only: no morph transitions or new arrangements. Close-ups enlarge only visible source details; do not invent facets or texture. Keep the original visible silhouette in hero shots; close-ups use existing details. Prioritize details. Allow only tiny natural sway in existing dangles; keep hooks, studs, anchors and display fixed. No rubbery bending or stretched links.
shot 1, 4, Start on the exact input frame; arc gently across the original hero composition.; shot 2, 3, Cut to a visible detail; make a short macro slide with restrained highlights.; shot 3, 3, Cut back to the original hero framing; ease to a stable finish.;
```

## Category blocks

### Earrings

Preserve the visible earring count, relative size, spacing, orientation, hooks, posts, stud-to-drop links, dome and fringe. Do not invent a matching partner, ears or a wearer.

### Ring

Preserve band geometry as seen in perspective, shank thickness, stone-to-band scale, prong count, setting height and inner contours. Keep the ring fixed; do not introduce a hand.

### Necklace

Preserve the necklace's visible outline, left/right spacing, drape, link order, chain length, pendant size and attachments. Keep the existing display or wearer still; no swinging or clipping.

### Bangle

Preserve the rigid contour as seen in perspective, thickness, inner opening, stone rows, pattern, hinges and clasp. No ovalising, opening, closing or rotation of the bangle.

### Chain

Preserve visible link order, bead count and spacing, clasp, connections, thickness, length and drape. Keep every strand distinct; no merging, tangling or changing link patterns.

### Other

Preserve the exact visible piece count, outline, scale, structural joints, motifs and fine details. Retain intentional asymmetry and every original contact point with the display.
