"""Chamak 2.0 image prompt, version 3.

Assembled per generation from fixed blocks: every prompt gets the core rules,
plus only the modules that apply to it — the jewellery-type module for
Design 1's type, the cross-type block when Design 2 is a different kind of
piece, and the style chips the wholesaler tapped. The text is the reviewed
draft in plans/chamak/chamak-master-prompt-v3-draft.md, word for word.

Design 1 (Core design) is the foundation: the output is always its type and
keeps its identity. Design 2 (New direction) contributes ideas only.

The slider lines are passed in already built, so they stay byte-identical to
the previous prompt versions.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

from app.logging import logger

JEWELLERY_TYPES = (
    "necklace", "choker", "long_necklace", "pendant", "mangalsutra",
    "earrings", "jhumka", "ring", "bangle", "kada", "bracelet",
    "maang_tikka", "nose_ring", "anklet", "other",
)

# type -> (noun without article, type module)
_TYPES: dict[str, tuple[str, str]] = {
    "necklace": ("necklace", "necklace"),
    "choker": ("choker necklace", "necklace"),
    "long_necklace": ("long necklace", "necklace"),
    "pendant": ("pendant", "pendant"),
    "mangalsutra": ("mangalsutra", "mangalsutra"),
    "earrings": ("pair of earrings", "earrings"),
    "jhumka": ("pair of jhumka earrings", "earrings"),
    "ring": ("ring", "ring"),
    "bangle": ("bangle", "bangle"),
    "kada": ("kada", "bangle"),
    "bracelet": ("bracelet", "bracelet"),
    "maang_tikka": ("maang tikka", "maang_tikka"),
    "nose_ring": ("nose ring", "nose_ring"),
    "anklet": ("anklet", "anklet"),
}

_SYNONYMS = {
    "earring": "earrings", "studs": "earrings", "stud_earrings": "earrings",
    "jhumkas": "jhumka", "jhumki": "jhumka", "jhumka_earrings": "jhumka",
    "necklaces": "necklace", "necklace_set": "necklace", "collar": "necklace",
    "chokers": "choker",
    "haar": "long_necklace", "long_haar": "long_necklace", "rani_haar": "long_necklace",
    "pendants": "pendant", "pendant_set": "pendant",
    "rings": "ring", "bangles": "bangle", "kadas": "kada", "kadaa": "kada",
    "bracelets": "bracelet", "tikka": "maang_tikka", "maangtikka": "maang_tikka",
    "nath": "nose_ring", "nose_pin": "nose_ring", "payal": "anklet", "anklets": "anklet",
}


def normalize_jewellery_type(raw: Any) -> Optional[str]:
    """Map the analysis model's free text onto JEWELLERY_TYPES; None when blank."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    key = raw.strip().lower().replace("-", "_").replace(" ", "_")
    key = _SYNONYMS.get(key, key)
    return key if key in JEWELLERY_TYPES else "other"


def _with_article(noun: str) -> str:
    return f"{'an' if noun[:1] in 'aeiou' else 'a'} {noun}"


TASK = """TASK
Create one new, original piece of fine jewellery for a jewellery wholesaler's product catalogue. You are given two reference photographs of existing jewellery. Combine them as instructed below into ONE new design, and present it as a single clean, photorealistic catalogue product photograph that is ready to show to retail buyers. The piece must look like a real, finished piece of jewellery that a skilled goldsmith in India could actually manufacture and a customer could actually wear."""

REFERENCES = """REFERENCE IMAGES
- Image 1 (the first attached image) is DESIGN 1, the CORE DESIGN. It shows {{TYPE_1}}. Design 1 is the foundation of the new piece. The new piece is the same kind of jewellery as Design 1 and keeps Design 1's identity: its overall form and silhouette, its structure and construction, its proportions and size, its metal colour, and the way it is worn. All changes are made on top of Design 1.
- Image 2 (the second attached image) is DESIGN 2, the NEW DIRECTION. It shows {{TYPE_2}}. Design 2 is a source of new ideas only: motifs, patterns, stone choices, shapes, details and decorative language to bring into Design 1. Never copy Design 2 as a whole object, never place Design 2 next to Design 1, and never turn the new piece into Design 2's type of jewellery.{{DESIGN_SUMMARIES}}

USE ONLY THE JEWELLERY FROM THE PHOTOS
Both reference photos are shop or catalogue photos. Read only the jewellery in them. Everything else in those photos is NOT part of the design and must NOT appear in your result in any form:
- mannequins, busts, neck forms, display heads, faces, heads, hair, necks, shoulders, ears, hands, fingers, models, or any part of a person;
- display stands, trays, boxes, cushions, velvet or fabric, ribbons, flowers or any other prop;
- the photos' own backgrounds, shadows, reflections, colour casts, lighting and camera angle;
- any text, words, category names, labels, logos, watermarks, price tags, borders or frames printed on or around the photos.
If a piece is shown worn on a mannequin, a bust or a person, mentally lift the jewellery off and use only the jewellery. Do not reproduce the shape, colour, head, face or neck of the mannequin, bust or person it was resting on. If the new piece needs a display, use only the plain display described in WHAT TO MAKE. If a word such as "Necklace" or "Pendants" is printed on a photo, ignore it completely: it is a label, not part of the jewellery, and the result must contain no text at all."""

WHAT_TO_MAKE = """WHAT TO MAKE
Make exactly one new {{OUTPUT_TYPE}}. Nothing else: no second piece, no matching set, no extra jewellery, no copies or variations side by side."""

DISPLAY_NECK_FORM = """DISPLAY NECK FORM (the only display allowed, identical in every image)
A plain, headless jewellery display form of a neck and upper chest, covered in smooth matte velvet in the same colour as the background, just a little lighter so its outline is softly visible. The form ENDS AT THE NECK: it is cut off straight and flat just below where the jaw would be. There is NOTHING above the neck: no head, no chin, no face, no ears, no hair. The shoulders slope gently down and are cut off by the left and right edges of the frame. The form is smooth and simple: no seams, folds, creases, stitching, texture, stand or base. It is always the same shape, the same size and the same angle, facing the camera straight on. This plain form replaces any mannequin, bust, neck or person shown in the reference photos; never copy those."""

TYPE_MODULES = {
    "necklace": """NECKLACE — COMPLETENESS AND DISPLAY
The necklace must be shown complete, as one continuous piece:
- the whole front section: the centre focal element and everything on both sides of it;
- both sides running up and away toward the back of the neck, equal in length and mirror images of each other where Design 1 is symmetric;
- the fastening at the ends, in the same style as Design 1: hook, S-hook, box clasp, lobster clasp, or a dori thread finished with tassels (latkans);
- every drop, hanging, fringe and pearl string fully visible; nothing cut off by the frame edge.
Keep Design 1's length and style category exactly: a choker stays a choker sitting high and close, a collar stays a collar, a short necklace stays short, a long haar or rani haar stays long. If Design 1 has several layers or strands, keep the same number of layers, each one complete and separately readable.
DISPLAY: Show the necklace worn on the DISPLAY NECK FORM described below, seen straight from the front. The necklace sits naturally on the form, at the height where Design 1's style is worn (a choker high around the neck, a long haar falling lower on the chest), with the centre focal element at the centre of the form and both sides rising evenly around the neck. Nothing of the necklace is hidden by the form except the very back of the neck.

{{DISPLAY_NECK_FORM}}""",
    "pendant": """PENDANT — COMPLETENESS AND DISPLAY
Show the pendant complete, with its bail (the loop that the chain passes through) clearly visible and properly joined to the pendant. If Design 1 shows the pendant on a chain, include the chain and show it worn on the DISPLAY NECK FORM: the chain falls around the neck in a smooth, symmetric V, both sides equal, with the pendant hanging straight at the centre of the chest. If Design 1 shows the pendant alone, show it alone, standing upright and facing the camera on a small, plain velvet display pad, with no neck form. Keep Design 1's pendant size relative to the chain.

{{DISPLAY_NECK_FORM}}""",
    "mangalsutra": """MANGALSUTRA — COMPLETENESS AND DISPLAY
Show the complete mangalsutra: the black bead strands (keep Design 1's number of strands and bead pattern, including any gold beads or links between the black beads), the centre pendant (vati or Design 1's centre element) hanging at the centre, and the clasp at the ends. Show it worn on the DISPLAY NECK FORM, falling around the neck in a smooth, even V, both sides equal. Black beads are small, round, glossy and uniform.

{{DISPLAY_NECK_FORM}}""",
    "earrings": """EARRINGS — ONE MATCHING PAIR
Make one pair: two earrings that are exact mirror images of each other, identical in size, design, stone count, stone colour and finish. Show both earrings side by side on a plain velvet earring display stand: a simple upright stand with a horizontal bar at the top. Hook, fish-hook and hoop earrings hang from the bar; stud earrings have their posts pushed through the bar so they sit upright. Both earrings are at exactly the same height, facing the camera, with a small even gap between them, centred in the frame. Each earring is complete: the ear fitting at the top in Design 1's style (stud post, fish-hook, hook with ear chain, hoop or clip), then every tier, dome, chain, bead and drop down to the very bottom, all fully inside the frame.
Keep Design 1's earring style: a stud stays a stud, a jhumka keeps its bell-shaped dome with the bead or pearl fringe hanging from the rim, a chandbali keeps its crescent-moon shape, a hoop stays a closed circle, a long dangler stays long. Never show an ear, an earlobe, a head or a face.""",
    "ring": """RING — COMPLETENESS AND DISPLAY
Make one ring. Show it sitting upright in the slot of a small, plain velvet ring cushion, turned at a slight three-quarter angle so that both the top (the head or face of the ring, with its main design and stones) and the band are clearly visible. The band forms a complete, even circle with Design 1's band width and profile. The head sits centred on the band. No finger, no hand, no ring box.""",
    "bangle": """BANGLE OR KADA — COMPLETENESS AND DISPLAY
Make one bangle (or one kada, if Design 1 is a kada; or one matching pair if Design 1 shows a pair of identical bangles). Show it standing upright, resting on a short, plain, horizontal velvet bangle roll (a smooth velvet cylinder), turned at a slight angle so the decorated outer face and the round form are both visible. A pair sits side by side on the same roll, slightly overlapping. The bangle is a complete, even circle (or Design 1's oval or openable shape, including its hinge and screw or clasp if it has them). Keep Design 1's width, thickness and profile. No wrist, no hand.""",
    "bracelet": """BRACELET — COMPLETENESS AND DISPLAY
Make one bracelet, shown wrapped around a plain, oval velvet bracelet cushion, front facing the camera, so its full decorated front and its round worn shape are visible. Every link, segment and element on the front is visible and evenly spaced; the clasp may sit at the back of the cushion. Keep Design 1's width, length and type (chain, tennis line, link, cuff, or flexible bangle-style). No wrist, no hand.""",
    "maang_tikka": """MAANG TIKKA — COMPLETENESS AND DISPLAY
Make one maang tikka, shown hanging vertically from a small, plain velvet display hook at the top of the frame: the hook of the tikka at the top, the chain running straight down the centre of the frame, and the pendant at the bottom with all its drops and fringe. Keep Design 1's chain length and pendant size. No forehead, no hair, no head.""",
    "nose_ring": """NOSE RING — COMPLETENESS AND DISPLAY
Make one nose ring, shown upright and facing the camera, sitting in the slot of a small, plain velvet ring cushion: the ring or hoop with its ornament, and, if Design 1 has one, the chain running to its hook. Keep Design 1's size and shape. No nose, no face.""",
    "anklet": """ANKLET — COMPLETENESS AND DISPLAY
Make one anklet (or one matching pair if Design 1 shows a pair), shown complete and laid flat on the velvet in an open, even curve, seen from directly above. Every link, motif, bell (ghungroo) and drop is visible and evenly spaced; the clasp is visible. No foot, no ankle.""",
    "other": """COMPLETENESS AND DISPLAY
Show the whole {{OUTPUT_TYPE}} complete, with every part, fastening and hanging element fully inside the frame, displayed on the simplest plain velvet display that a jewellery shop would normally use for this kind of piece. No person, no body part, no mannequin copied from the reference photos.""",
}

CROSS_TYPE = """TRANSLATING IDEAS FROM A DIFFERENT TYPE OF JEWELLERY
Design 2 is {{TYPE_2}}, but the new piece must be {{TYPE_1}}. Do not attach, paste, shrink or repeat Design 2 itself onto Design 1. Instead, translate Design 2's design language into Design 1's form:
- Motifs: take Design 2's motifs (for example peacock, lotus, paisley or kairi, temple deity, floral, leaf, geometric or architectural forms) and redraw them at the scale and spacing that suit {{TYPE_1}}, in the places where {{TYPE_1}} naturally carries decoration.
- Stones: take Design 2's stone types, cuts, colours and setting style and use them in the natural stone positions of the {{OUTPUT_TYPE}}.
- Shapes: turn Design 2's shapes (domes, drops, crescents, tiers, beads, fringes, links) into elements that belong on {{TYPE_1}}. For example, an earring's drop can become a necklace's centre drop or a row of side drops; an earring's dome can become a pendant or a repeating bead; a necklace's motif can become the top of an earring.
The result must read instantly as {{TYPE_1}}, with Design 2's personality clearly visible inside it."""

RESTRAINT = """DESIGN RESTRAINT AND HIERARCHY
- Build the piece around one clear focal element (for example the centre pendant of a necklace, the main dome of a jhumka, the head of a ring). Every other element supports it and is visibly smaller or quieter.
- Use fewer, better details rather than more. When in doubt, leave it out. Do not try to include every feature of both designs; choose the strongest ones and give them room.
- Leave clean negative space between motifs, stones and elements, so each one reads clearly when the photo is viewed at phone size.
- Do not add motifs, stones, colours or ornament that appear in neither reference design, unless the wholesaler's note asks for them.
- Keep the piece only as symmetric as Design 1 is. Do not force symmetry onto an asymmetric design, and do not break the symmetry of a symmetric one.
- Keep the overall visual weight close to Design 1: a light, delicate Design 1 stays light and delicate; a heavy bridal Design 1 stays rich and heavy. Rich is allowed; cluttered is not. A rich piece is organised: clear zones, clear sizes, clear rows.

ELEMENT LIMITS (hard limits for the whole piece)
- Motif families: at most THREE kinds of motif in total (for example: one hero motif such as a deity coin, plus one supporting motif such as peacocks, plus one border pattern). Do not mix elephants, peacocks, deities, paisleys, flowers and lattice all in one piece.
- Stones: at most THREE stone colours, plus pearls if the references have pearls.
- Beads and drops: at most TWO kinds of hanging bead or drop (for example: one size of pearl and one kind of coloured bead).
- Sizes: use a clear size ladder of three steps: ONE large focal element, a few medium elements, and small repeating elements. Nothing in between competes with the focal element.
- Plain metal: at least one third of the visible metal is plain, smooth, polished gold (frames, borders, rims, backplates, the band or collar) that gives the eye a place to rest. Not every surface is embossed, engraved or set with stones.
- Spacing: every motif has its own clear outline with a small gap of plain metal or background around it. Motifs never overlap, collide or merge into each other."""

CONSTRUCTION = """CONSTRUCTION
- Every part is physically connected. Links, jump rings, hooks, joins and hinges are visible and believable. Nothing floats in the air or overlaps through another part.
- Metal has a realistic, solid thickness: proper walls, rims, borders and backing plates. No paper-thin sheets, no crumpled foil, no flimsy wire where Design 1 is solid.
- The piece obeys gravity: drops and fringes hang straight down, chains fall in smooth natural curves, nothing bends unnaturally.
- Repeated elements (links, beads, petals, side motifs, stones in a row) are identical in size and shape and evenly spaced. Where the design is symmetric, the left side mirrors the right side exactly.
- Proportions and size are realistic for {{TYPE_1}} worn by an adult."""

METAL_AND_SURFACE = """METAL COLOUR
Use exactly the metal colour of Design 1: {{DESIGN_1_METAL}}. Match its hue, warmth and depth precisely. Do not drift toward a brassy, orange, greenish, pale or washed-out colour, and do not mix in a second metal colour that Design 1 does not have. (A gold-tone choice below, if present, overrides this.)

SURFACE QUALITY
- Metal surfaces are solid, smooth and evenly finished, with crisp, clean, straight or evenly curved edges and consistent thickness, like finished cast and hand-polished jewellery.
- Flat and curved areas of metal stay clean and continuous: no dents, dings, ripples, wrinkles, crumpling, bumps, pits or wobble.
- Use texture (filigree, granulation, engraving, hammering, beading, milgrain) only where a reference design actually has it, and render it neatly, evenly and deliberately, never as random noise.
- The finish (polish level, matte or satin areas, antique or oxidised darkening in the recesses) follows Design 1, unless a finish choice below says otherwise.

SCULPTED AND EMBOSSED WORK (nakshi, repoussé, temple carving, animal and deity figures)
- Relief work is crisply sculpted: clear, smooth high points, clean defined outlines, and smooth, even backgrounds between the raised details. It looks like a master carver made it.
- Never lumpy, blobby, melted, crinkled, crumpled, bubbly or wrinkled. Never a "tree bark" or "crumpled foil" grain spread over the metal. Animal bodies (elephants, peacocks) and deity figures have smooth, well-formed volumes with a few clear, deliberate detail lines, not a surface covered in random bumps.
- Every figure and motif is readable: you can tell at a glance what it is (a deity, an elephant, a peacock, a lotus). A cluster of small figures never melts into one textured mass.
- Leaf, mango (kairi) and oval plaques are smooth, polished, gently domed surfaces with at most a clean border or a few deliberate engraved lines, not covered in texture.
- Surface detail is only as dense as it needs to be to read the motif. Large areas of a figure stay smooth."""

STONES = """STONES, PEARLS AND BEADS
- Use stone types and colours taken from the reference designs, as the blend directions say. Do not introduce stone colours that appear in neither design, unless the wholesaler's note or a style choice asks for them.
- Every stone sits level in a proper, believable setting (prong, bezel, kundan foil setting, pavé, channel or close setting). No stone floats, tilts randomly or sits on bare metal.
- Stones are clean and correctly shaped (round, oval, pear, marquise, emerald cut, flat-cut polki, cabochon). Facets are crisp but calm: no glitter noise, no rainbow sparkle, no starbursts.
- Pearls and beads are uniform, round or Design 1's shape, with a soft natural lustre.
- Stone count and arrangement are deliberate: evenly spaced, symmetric where the design is symmetric, never randomly scattered.
- Small accent stones (tiny white stones, micro-pavé) appear only in clearly defined places: a neat line along a border, a ring around a centre stone, or a filled shape. Never sprinkled loosely across the metal surface or between motifs.

HANGING DROPS, PEARLS AND BEADS
- Drops hang in clean, evenly spaced rows. In each row every drop is the same size, the same kind and the same length, and the row follows a smooth line along the edge of the piece.
- Use at most two kinds of drop in the whole piece (for example a pearl and one coloured bead), in a simple repeating pattern (for example pearl, bead, pearl, bead).
- No loose clusters of tiny seed pearls wedged between motifs. No tangled or overlapping drops. Each drop hangs straight down on its own, with a little space between it and its neighbours."""

PHOTOGRAPHY = """BACKGROUND AND DISPLAY MATERIAL
- The background is a seamless, deep dark-charcoal matte velvet, smooth and even across the whole frame, with a very gentle, soft brightening behind the piece. No folds, no creases, no visible fabric pattern, no brocade, no carvings, no walls, no trays, no objects, no horizon line, no gradient bands. The background is identical in every image.
- Every display form or stand is covered in the same dark-charcoal matte velvet, only slightly lighter than the background so its outline is softly visible. Display forms are plain and simple: no seams, stitching, logos, labels, metal parts or bases showing. The jewellery is always the brightest and most eye-catching thing in the image.

LIGHTING (always the same setup)
- One large, soft light from the front, slightly above and to the left (about 45 degrees), plus a gentle fill light from the right so no part of the piece falls into deep shadow.
- The light is neutral white daylight (about 5500 K). No warm, golden, orange, yellow, blue or coloured light; no coloured rim light; no spotlight beams; no lens flare; no glow; no bokeh.
- Highlights on metal are soft, clean and white. They show the shape of the metal without blowing out to pure white or hiding detail. Shadows are soft, short and fall naturally behind and below the piece.
- The lighting must never change the colour of the metal or the stones. The gold must look exactly the colour it is in Design 1 (or the chosen gold tone), as it would look under neutral daylight.

CAMERA AND FRAMING
- A standard front-facing product catalogue photograph, taken with a normal lens: not a macro close-up, not wide-angle, no distortion, no extreme perspective. Use the view described in WHAT TO MAKE.
- The whole piece is in the frame and centred, filling about 70 to 80 percent of the frame width, with an even margin on all sides. Nothing is cut off.
- The entire piece is in sharp focus from edge to edge. No blur, no shallow depth of field.
- Natural, true-to-life colour and contrast. No filters, no cinematic colour grading, no vignette, no HDR look, no added glow or sparkle effects."""

PRIORITY = """IF ANY INSTRUCTIONS CONFLICT, FOLLOW THIS ORDER
1. Use only the jewellery from the photos: no person, head, face, mannequin, stand, prop or text from them. The only display allowed is the plain one described in WHAT TO MAKE.
2. What to make: the type, completeness and pair rules.
3. The wholesaler's note.
4. The finish and style choices.
5. The blend directions.
6. Everything else in this prompt."""

AVOID = """AVOID
- People and props: any head, face, chin, hair, ear, hand, finger, skin or model; any mannequin or bust copied from the reference photos; any display other than the plain velvet one described in WHAT TO MAKE; any tray, box, flower or other prop.
- Text: any words, letters, numbers, labels, category names, logos, watermarks, signatures, price tags, borders or frames.
- Metal defects: dents, dings, crumpled, wrinkled or foil-like metal, ripples, bumps, pits, warped, bent or melted shapes, wobbly or uneven edges, paper-thin parts.
- Surface noise: grainy, noisy or speckled surfaces, random scratches, smudges, over-sharpened texture, glitter-like over-sparkle, rainbow or starburst effects, blown-out highlights.
- Texture errors: lumpy, blobby, melted or bubbly relief; "tree bark", crumpled-foil or granular grain spread over the metal; every surface covered in texture with no plain polished metal; figures that melt into an unreadable mass.
- Design errors: cluttered or overlapping ornament, crowded motifs, more than three motif families, tiny stones sprinkled loosely over the metal, seed-pearl clusters wedged between motifs, drops of random sizes at random heights, extra or floating stones, broken, missing or disconnected parts, mismatched earrings, uneven sides, duplicated or extra pieces, half-merged objects.
- Image errors: cropped or cut-off parts, blur, tilted or distorted perspective, colour casts, cinematic grading, vignettes, busy or patterned backgrounds."""

FINAL_CHECK = """FINAL CHECK BEFORE YOU FINISH
- Is there exactly one {{OUTPUT_TYPE}}, complete, with every part inside the frame?
- Is it clearly the same kind of piece as Design 1, keeping Design 1's identity, with Design 2's ideas clearly visible in it?
- Is there no head, face, person, prop, text or logo anywhere in the image, no mannequin copied from the reference photos, and no display other than the plain velvet one described in WHAT TO MAKE? If a neck form is shown, does it end at the neck with nothing above it?
- Is the metal the same colour as Design 1 (or the chosen gold tone), with smooth, clean, dent-free surfaces?
- Are all parts connected, evenly spaced and realistic to manufacture?
- Is the background and lighting exactly as specified, neutral and consistent?
If any answer is no, fix it before producing the image."""

# Chip id -> (group, prompt line). Groups other than "craft" are pick-one.
STYLE_CHIPS: dict[str, tuple[str, str]] = {
    "high_polish": ("polish", "Finish: high polish. All metal surfaces are smooth and mirror-bright, with clean, crisp reflections. No matte areas, no antique darkening."),
    "matte": ("polish", "Finish: matte satin. Metal surfaces have a soft, even, brushed satin sheen with no mirror reflections. Edges and raised details may be lightly polished for contrast."),
    "hammered": ("polish", "Finish: hand-hammered. Main metal surfaces carry an even, deliberate pattern of small, regular, polished hammer facets, all the same size and spacing. This is an intentional texture: neat and uniform, never dents or damage."),
    "antique": ("polish", "Finish: antique. Recesses and the background of motifs are darkened, while raised details stay brighter, giving a rich, aged, traditional look. The darkening is even and controlled, not dirty or patchy."),
    "gold_22k": ("gold", "Metal colour: rich, deep, warm 22 karat Indian yellow gold."),
    "gold_18k": ("gold", "Metal colour: 18 karat yellow gold — a bright, slightly lighter and less orange yellow than 22 karat."),
    "rose_gold": ("gold", "Metal colour: rose gold — a soft, warm pinkish gold, consistent across the whole piece."),
    "white_gold": ("gold", "Metal colour: white gold — a bright, cool, neutral white metal, consistent across the whole piece."),
    "two_tone": ("gold", "Metal colour: two-tone — Design 1's gold colour for the main body, with white gold for the stone settings and fine accents only. The two metals are cleanly separated, never blended."),
    "kundan": ("craft", "Style: kundan. Uncut, flat, glassy stones set flush in gold foil settings, with clean, even gold borders around each stone."),
    "meenakari": ("craft", "Style: meenakari. Add smooth, glossy enamel in clean, solid colour fields within crisp gold outlines, on the areas that suit it. Enamel colours are taken from the reference designs."),
    "filigree": ("craft", "Style: filigree. Fine, even, twisted gold wirework in clean, regular open patterns, with neat joins and consistent wire thickness."),
    "temple": ("craft", "Style: temple jewellery. Traditional South Indian temple character: crisply sculpted motifs in rich gold with an antique finish in the recesses. Choose the temple motifs (such as a deity, peacock, lotus, mango or coin form) from those already in the reference designs, and stay within the element limits above."),
    "polki": ("craft", "Style: polki. Uncut, flat-cut diamonds with a soft, natural, milky sparkle, set in gold, used as the main stones."),
    "diamond_cut": ("craft", "Style: diamond-cut. Metal surfaces carry small, precise, faceted cuts that catch the light in clean, even sparkles. Neat and regular, never noisy."),
    "lightweight": ("look", "Look: lightweight daily wear. Slimmer proportions, fewer and smaller stones, more open space, a delicate and wearable piece."),
    "heavy_bridal": ("look", "Look: heavy bridal. A rich, substantial, statement piece with fuller coverage and more layers or drops, still clean, organised and uncluttered."),
    "minimal": ("look", "Look: minimal modern. Clean lines, simple geometry, fewer motifs, generous negative space, contemporary and understated."),
}

# A note is free text from the wholesaler; capped so it can never push the
# prompt past the vendor limit and truncate the rules that follow it.
_MAX_NOTE_CHARS = 1500

# CHAMAK_WILDNESS, 0–10: how far the design may move away from the two
# references. Five bands, each a distinct instruction — image models read a
# bare number like "6/10" as vague, so the number only picks the band. Each
# band names the rules it loosens; the fixed rules below hold at every level.
_WILDNESS_BANDS = (
    (1, "FAITHFUL",
     "Stay as close to the references as possible. The new piece is clearly Design 1, with Design 2's "
     "chosen elements placed into it, and nothing else. Invent nothing: every motif, stone, shape and "
     "detail comes from one of the two designs. Keep Design 1's silhouette and layout exactly."),
    (3, "REFINED BLEND",
     "Combine the two designs and refine the result into one coherent piece. Keep Design 1's silhouette "
     "and overall layout. You may add small connecting details (borders, joins, transitions) so the parts "
     "from both designs flow together, but no new motifs or stones that appear in neither design."),
    (6, "REINTERPRET",
     "Use the two designs as strong inspiration rather than parts to assemble. Keep a clear family "
     "resemblance to Design 1, but reinterpret: redraw the motifs in a fresh way, rearrange the "
     "composition and refine the silhouette. You may introduce ONE new motif or design idea that appears "
     "in neither reference, as long as it suits their style. This replaces the rule against adding "
     "motifs that appear in neither design."),
    (8, "BOLD NEW DESIGN",
     "Create a bold, original design that a buyer has not seen before. The references set the mood, the "
     "craft style, the materials and the colour palette; the composition, silhouette and motif treatment "
     "are yours. Design 1 is recognisable only as the starting point and Design 2 only as an influence. "
     "You may introduce up to TWO new motifs or design ideas that appear in neither reference. This "
     "replaces the rule against adding motifs that appear in neither design, and loosens \"keep Design 1's "
     "identity\" to \"keep Design 1's type, scale and wearability\". Treat the blend directions as a guide "
     "to which design's character leads, not as exact proportions."),
    (10, "WILD",
     "Design something genuinely new and unexpected: a fresh, original piece that would stand out in any "
     "catalogue. Treat both references only as a loose mood board for craft style, materials and colour. "
     "Invent the silhouette, structure, composition and motifs freely; surprising forms, inventive "
     "construction and unusual but tasteful ideas are welcome. Nothing has to be copied from either "
     "design. This replaces the rule against adding new motifs and the rule to keep Design 1's identity: "
     "keep only Design 1's type, scale and wearability. The blend directions are only a hint of which "
     "design's mood leads."),
)

_WILDNESS_FIXED = (
    "At every level these stay fixed: the new piece is {{TYPE_1}}, complete and wearable; only jewellery "
    "appears, with no person, mannequin or text; the element limits, construction, surface-quality and "
    "metal-colour rules apply in full, so a wilder design is still clean and uncluttered; the background, "
    "lighting and camera do not change. The wholesaler's note still comes first."
)


def parse_level(raw: Any) -> Optional[int]:
    """0–10 from a dashboard or request value; None when blank or not a number."""
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None
    if value != value:  # NaN
        return None
    return max(0, min(10, round(value)))


def _wildness_block(level: int) -> str:
    name, text = next((n, t) for top, n, t in _WILDNESS_BANDS if level <= top)
    return f"CREATIVE FREEDOM — LEVEL {level} OF 10 ({name})\n{text}\n{_WILDNESS_FIXED}"


# CHAMAK_STRUCTURE, 0–10: how much the shape and build of Design 1 may
# change. Wildness decides how inventive the ideas are; this decides the
# outline, layers and layout, and wins where the two overlap.
_STRUCTURE_BANDS = (
    (1, "KEEP THE SHAPE",
     "Keep Design 1's structure exactly: the same outline and silhouette, the same length and size, the "
     "same number of layers, strands, tiers and drop rows, the same layout of the main parts and the same "
     "way they are joined. Changes happen only in motifs, stones, surface and details."),
    (3, "SMALL SHAPE CHANGES",
     "Keep Design 1's outline, layout and number of layers. Small proportion changes are allowed: the "
     "focal element a little larger or smaller, slightly different spacing, slightly longer or shorter "
     "drops."),
    (6, "SOME SHAPE CHANGES",
     "Keep the piece recognisably Design 1's form, but its structure may change: different proportions, "
     "a new shape for the focal element, a new arrangement of the side elements, and one layer, tier or "
     "row of drops added or removed. Structural ideas from Design 2, such as its tiers or its outline, may "
     "be brought in."),
    (8, "BIG SHAPE CHANGES",
     "Build a new structure for the piece: a new outline and layout, a different number of layers or "
     "tiers, and new ways the parts connect. Design 2's structure may lead, translated into Design 1's "
     "type."),
    (10, "COMPLETELY NEW SHAPE",
     "Design a completely new structure for this type of jewellery: a new outline, layering, layout and "
     "construction. Only the type and wearability carry over from Design 1."),
)

_STRUCTURE_FIXED = (
    "At every level the new piece stays {{TYPE_1}}, complete, wearable and realistic to manufacture, and "
    "the construction rules apply in full. This setting decides the shape and structure of the piece: "
    "where the creative-freedom level says something different about silhouette, layout or structure, "
    "follow this setting."
)


def _structure_block(level: int) -> str:
    name, text = next((n, t) for top, n, t in _STRUCTURE_BANDS if level <= top)
    return f"STRUCTURE — LEVEL {level} OF 10 ({name})\n{text}\n{_STRUCTURE_FIXED}"


# In a cut-out every display form would stay in the PNG, so for "Remove
# background" each type module's display sentences are swapped for the piece
# shown on its own. Swapping beats overriding them later: gpt-image-1.5, the
# cut-out model, kept the neck form when the prompt only overrode it.
_CUT_OUT_DISPLAY = {
    "necklace": [(
        "DISPLAY: Show the necklace worn on the DISPLAY NECK FORM described below, seen straight from the front. The necklace sits naturally on the form, at the height where Design 1's style is worn (a choker high around the neck, a long haar falling lower on the chest), with the centre focal element at the centre of the form and both sides rising evenly around the neck. Nothing of the necklace is hidden by the form except the very back of the neck.\n\n{{DISPLAY_NECK_FORM}}",
        "DISPLAY: No display form. Show the necklace on its own, seen straight from the front, in its natural worn U or V shape as if on an invisible neck, with the centre focal element at the bottom centre and both sides rising evenly.",
    )],
    "pendant": [(
        "If Design 1 shows the pendant on a chain, include the chain and show it worn on the DISPLAY NECK FORM: the chain falls around the neck in a smooth, symmetric V, both sides equal, with the pendant hanging straight at the centre of the chest. If Design 1 shows the pendant alone, show it alone, standing upright and facing the camera on a small, plain velvet display pad, with no neck form.",
        "If Design 1 shows the pendant on a chain, include the chain, falling in a smooth, symmetric V, both sides equal, with the pendant hanging straight at the centre, as if on an invisible neck. If Design 1 shows the pendant alone, show it alone, upright and facing the camera.",
    ), ("\n\n{{DISPLAY_NECK_FORM}}", "")],
    "mangalsutra": [(
        "Show it worn on the DISPLAY NECK FORM, falling around the neck in a smooth, even V, both sides equal.",
        "Show it on its own in a smooth, even V, both sides equal, as if on an invisible neck.",
    ), ("\n\n{{DISPLAY_NECK_FORM}}", "")],
    "earrings": [(
        "Show both earrings side by side on a plain velvet earring display stand: a simple upright stand with a horizontal bar at the top. Hook, fish-hook and hoop earrings hang from the bar; stud earrings have their posts pushed through the bar so they sit upright.",
        "Show both earrings side by side, upright, as if hanging, with nothing holding them.",
    )],
    "ring": [(
        "Show it sitting upright in the slot of a small, plain velvet ring cushion, turned at",
        "Show it standing upright on its own, turned at",
    )],
    "bangle": [(
        "Show it standing upright, resting on a short, plain, horizontal velvet bangle roll (a smooth velvet cylinder), turned at",
        "Show it standing upright on its own, turned at",
    ), (
        "A pair sits side by side on the same roll, slightly overlapping.",
        "A pair stands side by side, slightly overlapping.",
    )],
    "bracelet": [(
        "shown wrapped around a plain, oval velvet bracelet cushion, front facing the camera, so its full decorated front and its round worn shape are visible.",
        "shown upright in its round worn shape on its own, front facing the camera, so its full decorated front is visible.",
    ), ("the clasp may sit at the back of the cushion.", "the clasp may sit at the back.")],
    "maang_tikka": [(
        "shown hanging vertically from a small, plain velvet display hook at the top of the frame:",
        "shown hanging vertically on its own:",
    )],
    "nose_ring": [(", sitting in the slot of a small, plain velvet ring cushion:", ":")],
    "anklet": [(
        "laid flat on the velvet in an open, even curve, seen from directly above.",
        "laid out in an open, even curve, seen from directly above.",
    )],
    "other": [(
        "displayed on the simplest plain velvet display that a jewellery shop would normally use for this kind of piece.",
        "shown on its own, with no display.",
    )],
}


def _type_module(module: str, cut_out: bool) -> str:
    text = TYPE_MODULES[module]
    if cut_out:
        for old, new in _CUT_OUT_DISPLAY[module]:
            text = text.replace(old, new)
    return text


# The wholesaler's "Remove background" choice. Sent to OpenAI as
# background="transparent"; the prompt drops every display form, because a
# neck form or stand would stay in the cut-out.
TRANSPARENT_BACKGROUND = """TRANSPARENT BACKGROUND (this overrides BACKGROUND AND DISPLAY MATERIAL and every DISPLAY instruction above)
- The background is fully transparent: no colour, no velvet, no surface, no floor, no shadow and no reflection beneath the piece.
- Show only the jewellery itself, with no mannequin, bust, neck form, stand, cushion, hook or any other display, not even a dark or shadowy one. Arrange it as it is naturally worn and seen from the front: a necklace, mangalsutra or chain in its natural worn U or V shape as if on an invisible neck; a pair of earrings side by side, upright, at the same height; a ring upright at a slight three-quarter angle; a bangle or bracelet upright showing its decorated face.
- Edges are crisp and clean all around the piece, including fine chains, drops and the gaps inside open work, so the cut-out can be placed on any background.
- The lighting stays as described: neutral white, never changing the colour of the metal."""

# The same cut-out instructions for the two-step cut-out (app.services.cutout):
# gpt-image-2 renders on plain white, then the background is removed here.
WHITE_CUT_OUT_BACKGROUND = TRANSPARENT_BACKGROUND.replace(
    "TRANSPARENT BACKGROUND (", "PLAIN WHITE BACKGROUND FOR CUT-OUT ("
).replace(
    "- The background is fully transparent: no colour, no velvet, no surface, no floor, no shadow and no reflection beneath the piece.",
    "- The background is one perfectly even, plain, pure white (#FFFFFF) from edge to edge: no velvet, no surface, no floor, no gradient, no shadow and no reflection beneath the piece. The piece will be cut out of this background, so it is clearly separated from it on every side.",
)
assert WHITE_CUT_OUT_BACKGROUND.count("pure white (#FFFFFF)") == 1


def _chip_lines(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    taken: set[str] = set()
    lines: list[str] = []
    for chip in raw:
        entry = STYLE_CHIPS.get(chip) if isinstance(chip, str) else None
        if not entry or chip in taken:
            continue
        group, line = entry
        if group != "craft" and group in taken:
            continue
        taken.update({chip, group})
        lines.append(line)
    return lines


def _resolve_types(analysis: dict[str, Any]) -> tuple[str, Optional[str]]:
    """Design 1's and Design 2's types; Design 2 is None when only "different" is known.

    Rows analysed before v3 carry one shared `jewelry_type` and a
    `type_mismatch` flag instead of a type per image.
    """
    legacy = normalize_jewellery_type(analysis.get("jewelry_type"))
    type_1 = normalize_jewellery_type(analysis.get("image1_type")) or legacy or "other"
    type_2 = normalize_jewellery_type(analysis.get("image2_type"))
    if type_2 is None and not analysis.get("type_mismatch"):
        type_2 = type_1
    return type_1, type_2


def _noun(jewellery_type: str, analysis: dict[str, Any]) -> str:
    if jewellery_type in _TYPES:
        return _TYPES[jewellery_type][0]
    raw = str(analysis.get("jewelry_type") or "").strip().lower()
    return raw if raw and normalize_jewellery_type(raw) == "other" else "piece of jewellery"


# The whole prompt as one template. CHAMAK_BASE_PROMPT_TEMPLATE on Railway
# replaces it; the {{TOKENS}} are where each generation's parts go. A token
# whose part doesn't apply (no sliders, no chips, same type) becomes empty.
DEFAULT_TEMPLATE = "\n\n".join([
    TASK, REFERENCES, WHAT_TO_MAKE, "{{TYPE_MODULE}}", "{{CROSS_TYPE}}", "{{WILDNESS}}", "{{STRUCTURE}}",
    "{{BLEND_DIRECTIONS}}", RESTRAINT, CONSTRUCTION, METAL_AND_SURFACE, STONES, "{{STYLE_CHOICES}}",
    "{{WHOLESALER_NOTE}}", PHOTOGRAPHY, "{{BACKGROUND_MODE}}", PRIORITY, AVOID, FINAL_CHECK,
])

TOKENS = (
    "TYPE_1", "TYPE_2", "OUTPUT_TYPE", "DESIGN_SUMMARIES", "DESIGN_1_METAL", "DISPLAY_NECK_FORM",
    "TYPE_MODULE", "CROSS_TYPE", "WILDNESS", "STRUCTURE", "BLEND_DIRECTIONS", "STYLE_CHOICES",
    "WHOLESALER_NOTE", "BACKGROUND_MODE",
)
# Without these, sliders, chips, notes or the type rules would silently stop
# reaching the model, so a template missing any of them is not used.
REQUIRED_TOKENS = ("TYPE_MODULE", "BLEND_DIRECTIONS", "STYLE_CHOICES", "WHOLESALER_NOTE")

_TOKEN = re.compile(r"\{\{\s*([A-Z0-9_]+)\s*\}\}")


def resolve_template(raw: Optional[str]) -> tuple[str, Optional[str]]:
    """The template to use and a short label for it (None for the built-in one).

    A dashboard edit that is blank or missing a required token falls back to
    the built-in template rather than shipping a broken prompt.
    """
    text = (raw or "").strip()
    if not text:
        return DEFAULT_TEMPLATE, None
    # A value pasted as one line with literal "\n" sequences.
    if "\n" not in text and "\\n" in text:
        text = text.replace("\\n", "\n")
    found = set(_TOKEN.findall(text))
    missing = [t for t in REQUIRED_TOKENS if t not in found]
    if missing:
        logger.error(
            "CHAMAK_BASE_PROMPT_TEMPLATE ignored — missing " + ", ".join("{{%s}}" % t for t in missing)
            + "; using the built-in v3 prompt."
        )
        return DEFAULT_TEMPLATE, None
    unknown = sorted(found - set(TOKENS))
    if unknown:
        logger.warning(
            "CHAMAK_BASE_PROMPT_TEMPLATE has unknown tokens, left as written: "
            + ", ".join("{{%s}}" % t for t in unknown)
        )
    label = "railway-" + hashlib.sha256(text.encode()).hexdigest()[:8]
    # Templates pasted before these settings existed have no slot for them:
    # wildness and structure go just ahead of the blend directions, the
    # background override at the end (it names the blocks it overrides).
    missing_slots = [t for t in ("WILDNESS", "STRUCTURE") if t not in found]
    if missing_slots:
        slot = next(m for m in _TOKEN.finditer(text) if m.group(1) == "BLEND_DIRECTIONS")
        text = text[:slot.start()] + "".join("{{%s}}\n\n" % t for t in missing_slots) + text[slot.start():]
    if "BACKGROUND_MODE" not in found:
        text += "\n\n{{BACKGROUND_MODE}}"
    return text, label


def _fill(text: str, values: dict[str, str]) -> str:
    # One pass: text inserted for a token is never scanned again, so a note
    # that happens to contain "{{TYPE_1}}" reaches the model as written.
    return _TOKEN.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def compile_chamak_prompt_v3(
    analysis: Optional[dict[str, Any]],
    form_input: Optional[dict[str, Any]],
    note: Optional[str],
    slider_lines: list[str],
    template: str = DEFAULT_TEMPLATE,
    wildness: Optional[int] = None,
    structure: Optional[int] = None,
    transparent: bool = False,
    white_cut_out: bool = False,
) -> str:
    analysis = analysis or {}
    form_input = form_input or {}

    type_1, type_2 = _resolve_types(analysis)
    output_type = _noun(type_1, analysis)
    module_1 = _TYPES.get(type_1, ("", "other"))[1]
    module_2 = _TYPES.get(type_2, ("", "other"))[1] if type_2 else None

    summaries = ""
    for label, key in (("Design 1", "image1_summary"), ("Design 2", "image2_summary")):
        text = str(analysis.get(key) or "").strip()
        if text:
            summaries += f"\n{label} in words: {text}"
    if summaries:
        summaries = "\n" + summaries

    values = {
        "TYPE_1": _with_article(output_type),
        "TYPE_2": _with_article(_noun(type_2, analysis)) if type_2 else "a different kind of jewellery",
        "OUTPUT_TYPE": output_type,
        "DESIGN_SUMMARIES": summaries,
        "DESIGN_1_METAL": str(analysis.get("image1_metal") or "").strip() or "the exact colour it has in Image 1",
        "DISPLAY_NECK_FORM": DISPLAY_NECK_FORM,
    }
    values["TYPE_MODULE"] = _fill(_type_module(module_1, transparent), values)
    values["CROSS_TYPE"] = _fill(CROSS_TYPE, values) if module_2 != module_1 else ""
    values["WILDNESS"] = _fill(_wildness_block(wildness), values) if wildness is not None else ""
    values["STRUCTURE"] = _fill(_structure_block(structure), values) if structure is not None else ""
    values["BACKGROUND_MODE"] = (
        (WHITE_CUT_OUT_BACKGROUND if white_cut_out else TRANSPARENT_BACKGROUND) if transparent else ""
    )

    img1 = analysis.get("image1_strengths") or []
    img2 = analysis.get("image2_strengths") or []
    features = [
        f"Design 1 observed features: {', '.join(img1)}." if img1 else "",
        f"Design 2 observed features: {', '.join(img2)}." if img2 else "",
    ]
    features = [line for line in features if line]
    values["BLEND_DIRECTIONS"] = (
        "\n".join([
            "BLEND DIRECTIONS",
            "Follow the per-attribute design proportions below; neither reference automatically takes priority.",
            *features,
            *slider_lines,
        ]) if slider_lines
        else "\n".join(["REFERENCE FEATURES", *features]) if features
        else ""
    )

    chips = _chip_lines(form_input.get("style_chips"))
    values["STYLE_CHOICES"] = "\n".join([
        "FINISH AND STYLE CHOICES (chosen by the wholesaler — follow these over the reference designs)",
        *chips,
    ]) if chips else ""

    values["WHOLESALER_NOTE"] = (
        "WHOLESALER'S NOTE (follow this over the blend directions and style choices, unless it asks for "
        "something physically impossible or asks you to include a person, mannequin or text)\n"
        + note.strip()[:_MAX_NOTE_CHARS]
    ) if note and note.strip() else ""

    # Empty parts leave runs of blank lines behind; fold them to one.
    return re.sub(r"\n{3,}", "\n\n", _fill(template, values)).strip()
