"""Versioned image-to-video prompts and category-specific form definitions."""
from __future__ import annotations

import hashlib
import json

from app.config import settings
from app.jewellery_types import normalize_chain_type
from app.services.chamak_prompt_v3 import normalize_jewellery_type
from app.services.kling import PROMPT_LIMITS

BASE = (
    "Create a photorealistic high-end luxury jewellery campaign film. Use the supplied image as the exact first frame and immutable product reference. "
    "The jewellery is the hero. Keep the original composition, background, display, props, palette and arrangement. "
    "Use an elegant international advertising aesthetic and only physically consistent perspective changes."
)
GLOBAL = (
    "DESIGN LOCK: Preserve product count, silhouettes, proportions, metal thickness, spacing, symmetry/asymmetry and structural connections. "
    "Keep every visible stone's count, cut, size, colour and placement, prongs, settings, motifs and engravings, links and attachments. "
    "No redesign, deformation, duplication, invented details, altered links or floating components. "
    "SCENE LOCK: Keep the same product, support and scene throughout. No drift, new body parts or invented views. "
    "LIGHT: Soft directional studio light, refined contrast, natural depth, controlled specular reflections and angle-dependent brilliance. "
    "Preserve gold/silver tone, finish, stone colours, contact shadows and exposure. No starbursts, excess sparkle, overexposure, flicker, hue shifts or plastic materials. "
    "No added text, logos or overlays. Product accuracy overrides motion, style and notes; reduce movement rather than change the product."
)
CATEGORIES = {
    "earrings": "Preserve the visible earring count, relative size, spacing, orientation, hooks, posts, stud-to-drop links, dome and fringe. Do not invent a matching partner, ears or a wearer.",
    "ring": "Preserve band geometry as seen in perspective, shank thickness, stone-to-band scale, prong count, setting height and inner contours. Keep the ring fixed; do not introduce a hand.",
    "necklace": "Preserve the necklace's visible outline, left/right spacing, drape, link order, chain length, pendant size and attachments. Keep the existing display or wearer still; no swinging or clipping.",
    "bangle": "Preserve the rigid contour as seen in perspective, thickness, inner opening, stone rows, pattern, hinges and clasp. No ovalising, opening, closing or rotation of the bangle.",
    "chain": "Preserve visible link order, bead count and spacing, clasp, connections, thickness, length and drape. Keep every strand distinct; no merging, tangling or changing link patterns.",
    "other": "Preserve the exact visible piece count, outline, scale, structural joints, motifs and fine details. Retain intentional asymmetry and every original contact point with the display.",
}
FOCUS = {
    "earrings": {"whole_piece": "Keep the original visible earring outlines in frame.", "details": "Emphasize existing decorative details; keep the original earring outlines visible."},
    "ring": {"whole_piece": "Keep the ring's original visible outline in frame.", "setting": "Emphasize the existing setting; retain the originally visible band in frame."},
    "necklace": {"whole_piece": "Keep the necklace's original visible silhouette in frame.", "pendant": "Emphasize the existing centrepiece; retain the originally visible chain in frame."},
    "bangle": {"whole_piece": "Keep the bangle's original visible contour in frame.", "pattern": "Emphasize the visible surface pattern without revealing unseen surfaces."},
    "chain": {"whole_piece": "Keep the chain's original visible layout in frame.", "links": "Emphasize existing link craftsmanship; retain the originally visible silhouette."},
    "other": {"whole_piece": "Keep the jewellery's original visible silhouette in frame."},
}


def category(raw: str) -> str:
    if normalize_chain_type(raw or "") == "chain":
        return "chain"
    value = normalize_jewellery_type(raw) or "other"
    if value in {"earrings", "jhumka"}:
        return "earrings"
    if value in {"necklace", "choker", "long_necklace", "pendant", "mangalsutra"}:
        return "necklace"
    if value in {"bangle", "kada", "bracelet"}:
        return "bangle"
    return value if value in CATEGORIES else "other"


def shot_options() -> list[dict]:
    # These are provisional camera directions, not reference-video API inputs.
    return [
        {"id": "option_1", "label": "Subtle showcase", "prompt": settings.VIDEO_SHOT_1_PROMPT or "One continuous front-facing shot: hold briefly, then a slow lateral track with an arc of about 5 degrees maximum. Natural parallax, smooth acceleration/deceleration, stable finish. No cuts, sudden zooms, shaking, rotations, rear reveals or distortion.", "preview_url": settings.VIDEO_SHOT_1_PREVIEW_URL or None,"multi_shot":False},
        {"id": "option_2", "label": "Energetic editorial", "prompt": settings.VIDEO_SHOT_2_PROMPT or "Elegant hero/detail edit in the same scene. Front-facing views, clean instantaneous cuts, smooth motion. No full rotations, extreme arcs, shaking, abrupt zooms, distortion or morphing. Never reveal unreliable hidden geometry. Keep jewellery and support stationary.", "preview_url": settings.VIDEO_SHOT_2_PREVIEW_URL or None,"multi_shot":True},
    ]


def form_fields(kind: str) -> list[dict]:
    fields = [{"id": "focus", "label": "What should the camera emphasize?", "default": "whole_piece", "choices": list(FOCUS[kind])}]
    if kind == "earrings":
        fields.append({"id": "subject_motion", "label": "Earring movement", "default": "still", "choices": ["still", "subtle_sway"]})
    return fields


def compose(raw_category: str, shot_id: str, inputs: dict[str, str], note: str = "", duration: int = 5, model: str = "kling-3.0") -> dict:
    kind = category(raw_category)
    shots = {shot["id"]: shot for shot in shot_options()}
    if shot_id not in shots:
        raise ValueError("Choose option 1 or option 2.")
    if type(duration) is not int or duration not in (5,10):
        raise ValueError("Choose a duration of 5 or 10 seconds.")
    if model not in PROMPT_LIMITS:
        raise ValueError("Choose a supported video model.")
    fields = {field["id"]: field for field in form_fields(kind)}
    if set(inputs) - set(fields):
        raise ValueError("These form fields do not apply to this jewellery category.")
    values = {key: inputs.get(key, field["default"]) for key, field in fields.items()}
    if any(value not in fields[key]["choices"] for key, value in values.items()):
        raise ValueError("Choose one of the available category options.")
    if values.get("subject_motion") == "subtle_sway":
        motion = "Allow only tiny natural sway in existing dangles; keep hooks, studs, anchors and display fixed. No rubbery bending or stretched links."
    else:
        motion = "Keep the jewellery and display stationary."
    multi_shot = shots[shot_id]["multi_shot"]
    focus = FOCUS[kind][values["focus"]]
    if multi_shot:
        focus = "Keep the original visible silhouette in hero shots; close-ups use existing details." + (" Prioritize " + values["focus"].replace("_"," ") + "." if values["focus"] != "whole_piece" else "")
    timeline = ""
    if multi_shot:
        segments = [(3,"Hold the exact input frame briefly, then track slowly across the hero composition with a front arc up to 5 degrees."),
                    (2,"Clean cut to the most distinctive visible detail for a short horizontal macro slide. No invented facets or focus hunting.")] if duration == 5 else [
            (4,"Hold the exact input frame briefly, then a slow front lateral track and arc up to 5 degrees. Natural parallax, full arrangement visible, smooth ease in/out."),
            (3,"Clean cut to the most distinctive visible detail for a horizontal macro slide. Crisp stable detail, controlled shallow depth of field, no invented facets or focus hunting."),
            (3,"Clean cut back to the original hero framing. Subtle forward move to a slightly tighter complete view. Decelerate to a stable frame and hold the final moment.")]
        timeline = "\n" + "; ".join(f"shot {i}, {seconds}, {text}" for i,(seconds,text) in enumerate(segments,1)) + ";"
    parts = {
        "base": settings.VIDEO_BASE_PROMPT.strip() or BASE,
        "global": settings.VIDEO_GLOBAL_PROMPT.strip() or GLOBAL,
        "category": CATEGORIES[kind],
        "local": " ".join([shots[shot_id]["prompt"], focus, motion]) + timeline,
        "user_note": note.strip(),
    }
    prompt = "\n\n".join(f"{key.upper()}: {value}" for key, value in parts.items() if value)
    if len(prompt) > PROMPT_LIMITS[model]:
        raise ValueError(f"The combined video prompt exceeds {PROMPT_LIMITS[model]:,} characters for {model}. Shorten the instructions.")
    version = "video-v3-" + hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:12]
    return {"category": kind, "inputs": values, "parts": parts, "prompt": prompt, "version": version,"multi_shot":multi_shot}


def storyboard(raw_category: str, shots: list[dict], model: str, style: str = "simple", two_scenes: bool = False) -> dict:
    """An approved storyboard's nine panels become three Kling beats: panels 1-3, 4-5 and 6-9.

    `simple` is one continuous move (like reference option 1); `match_cut` is an
    editorial edit with aligned hard cuts (like option 2). With `two_scenes` the
    film opens on the first frame and closes on the supplied closing frame.
    """
    kind = category(raw_category)
    if model not in PROMPT_LIMITS:
        raise ValueError("Choose a supported video model.")
    if style == "match_cut" and model != "kling-3.0":
        raise ValueError("Fast cuts need Kling 3.0.")
    duration = round(shots[-1]["end_seconds"])
    if duration not in (5, 10):
        raise ValueError("Choose a duration of 5 or 10 seconds.")
    notes = [" ".join(item["direction"] for item in group) for group in (shots[:3], shots[3:5], shots[5:])]
    ending = ("the same piece in the closing frame's scene, ending exactly on the closing frame" if two_scenes
              else "the original full-piece framing")
    if style == "match_cut":
        segments = ([(3, f"Start exactly on the first frame; slow lateral track with an arc of at most 5 degrees. Storyboard: {notes[0]}"),
                     (2, f"Clean cut to a macro of the most distinctive visible motif and stones, then a clean cut to {ending}. Storyboard: {notes[1]} {notes[2]}")]
                    if duration == 5 else
                    [(4, f"Start exactly on the first frame; slow lateral track with an arc of at most 5 degrees. Storyboard: {notes[0]}"),
                     (3, f"Clean cut to a horizontal macro slide across the most distinctive visible motif and stones. Storyboard: {notes[1]}"),
                     (3, f"Clean cut to {ending}; decelerate and hold the final moment. Storyboard: {notes[2]}")])
        local = ("Editorial edit with clean instantaneous match cuts. At every cut the jewellery keeps the same position and size in frame. "
                 "Keep the jewellery and its display stationary.\n" + "; ".join(f"shot {i}, {s}, {t}" for i, (s, t) in enumerate(segments, 1)) + ";")
    else:
        a, b = (2, 3.5) if duration == 5 else (4, 7)
        local = ("One continuous camera move with no cuts. Keep the jewellery and its display stationary.\n"
                 f"0-{a:g}s: Start exactly on the first frame; slow push-in with an arc of at most 5 degrees; the whole piece stays in frame. Storyboard: {notes[0]}\n"
                 f"{a:g}-{b:g}s: Glide into a macro view of the most distinctive visible motif and stones"
                 + (" until the background is fully out of focus" if two_scenes else "") + f". Storyboard: {notes[1]}\n"
                 f"{b:g}-{duration}s: Pull back out of the macro to {ending}; hold the final moment. Storyboard: {notes[2]}")
        if two_scenes:
            local += "\nThe background changes only while it is out of focus; the jewellery never morphs, bends or changes."
    base, global_rules = settings.VIDEO_BASE_PROMPT.strip() or BASE, settings.VIDEO_GLOBAL_PROMPT.strip() or GLOBAL
    if two_scenes:
        base = base.replace("Keep the original composition, background, display, props, palette and arrangement.",
                            "Keep each supplied frame's composition, background, display, props, palette and arrangement.")
        global_rules = global_rules.replace("Keep the same product, support and scene throughout.",
                                            "Keep the same product throughout; the scene changes exactly once, as planned below.")
    parts = {"base": base, "global": global_rules, "category": CATEGORIES[kind], "local": local}
    prompt = "\n\n".join(f"{key.upper()}: {value}" for key, value in parts.items())
    if len(prompt) > PROMPT_LIMITS[model]:
        raise ValueError(f"The shot directions are too long for {model}. Shorten them and approve a new storyboard.")
    version = "video-storyboard-v2-" + hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:12]
    return {"category": kind, "parts": parts, "prompt": prompt, "version": version, "multi_shot": style == "match_cut"}
