"""Batch the pilot's local pair measurements for actual Jev decisions."""
import asyncio
import math
import os
import time

import httpx
import numpy as np
from app.services.catalogue_search import cache_key, visual_fingerprint

MODEL = "jev-1.13.0"
INSTRUCTIONS = "Do these isolated jewellery subjects have closely matching designs, silhouettes and visible ornamentation? Compare the jewellery, not the photograph. Background, lighting, photo dimensions and placement are irrelevant; color alone cannot establish gemstone or material identity. Evaluate only the supplied measurements; you cannot see the photos. Do not claim piracy, ownership, material, exact product identity or guaranteed accuracy. Identical decoded pixels are strong direct evidence. Near-zero normalized resized-pixel error, together with high CLIP and low dHash difference, strongly supports a resized or JPEG-compressed copy even when dimensions and exact hashes differ. Do not require byte or pixel identity for compressed copies. Shared backgrounds can also reduce error, so consider all measurements together. High CLIP alone may indicate only the same broad category. Use uncertain when the evidence cannot distinguish matching designs from different jewellery of the same category."
CRITERIA = {
    "similar": "Strong evidence of identical or closely matching visual appearance beyond merely the same category.",
    "different": "Strong evidence of substantially different visual appearance.",
    "uncertain": "Ambiguous or insufficient evidence; visual review or richer image descriptions are needed.",
}
LIMITATIONS = "CLIP can score distinct jewellery designs highly. Background removal is approximate and can miss thin chains or keep props. Subject pixel errors and dHash compare silhouette/layout, not exact product identity or hidden geometry. Viewpoint changes are not fully normalized. No captions or jewellery-specific validation are available. These numbers are not calibrated percentages."


def configured():
    return bool(os.getenv("TYPESAFE_API_KEY", "").strip())


def pair_evidence(query, candidate, similarity):
    a = np.frombuffer(bytes.fromhex(query["rgb_sample"]), dtype=np.uint8).astype(np.float32) / 255
    b = np.frombuffer(bytes.fromhex(candidate["rgb_sample"]), dtype=np.uint8).astype(np.float32) / 255
    delta = a - b
    return {"byte_identical": query["bytes_sha256"] == candidate["bytes_sha256"],
            "decoded_rgb_pixels_identical": query["pixels_sha256"] == candidate["pixels_sha256"],
            "clip_cosine_similarity": round(similarity, 6),
            "normalized_resized_pixel_mae": round(float(np.abs(delta).mean()), 6),
            "normalized_resized_pixel_rmse": round(float(np.sqrt(np.mean(delta ** 2))), 6),
            "pixel_error_scale": "0 means equal after RGB resize to 64x64; 1 is maximum channel difference. These are image errors, not probabilities.",
            "dhash_different_bits_out_of_64": sum(a != b for a, b in zip(query["dhash"], candidate["dhash"])),
            "comparison_target": "foreground jewellery, background removed and subject centered on identical neutral canvases",
            "visual_encoder": "CLIP ViT-B/32 on isolated foregrounds, run locally", "limitations": LIMITATIONS}


def parse_answer(answer):
    if not isinstance(answer, dict) or answer.get("type") != "choice" or answer.get("choice") not in CRITERIA:
        raise ValueError("Invalid Jev choice")
    probabilities = answer.get("probabilities", {})
    if set(probabilities) != set(CRITERIA) or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in probabilities.values()):
        raise ValueError("Invalid Jev probabilities")
    if abs(sum(probabilities.values()) - 1) > .02:
        raise ValueError("Invalid Jev probability total")
    confidence = answer.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("Invalid Jev confidence")
    return answer


async def decide_matches(index, photo, rows, result):
    key = os.getenv("TYPESAFE_API_KEY", "").strip()
    if not key:
        raise LookupError("Jev image decisions are not configured on the server yet.")
    if not result["matches"]:
        return dict(result, decision_source="jev", decisions=[])
    row_by_id = {row["id"]: row for row in rows}
    query = await asyncio.to_thread(visual_fingerprint, photo)
    pairs, questions = {}, {}
    for i, match in enumerate(result["matches"]):
        fingerprint = index.fingerprints.get(cache_key(row_by_id[match["id"]]))
        if not fingerprint or "rgb_sample" not in fingerprint:
            index.schedule_refresh()
            raise LookupError("Catalogue comparison evidence is updating. Please retry shortly.")
        name = f"pair_{i}"
        pairs[name] = pair_evidence(query, fingerprint, match["similarity"])
        questions[name] = {"type": "choice", "instructions": {"question": INSTRUCTIONS, "evaluate_only": f"state.pairs.{name}"}, "criteria": CRITERIA}
    payload = {"model": MODEL, "state": {"pairs": pairs}, "questions": questions}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post("https://api.typesafe.ai/v1/systemone", headers={"Authorization": f"Bearer {key}"}, json=payload)
        if response.status_code != 200:
            raise LookupError("Jev image decisions are temporarily unavailable. Please retry shortly.")
        output = response.json()
        if not isinstance(output.get("model"), str) or set(output.get("answers", {})) != set(questions):
            raise ValueError("Incomplete Jev response")
        accepted, decisions = [], []
        for i, match in enumerate(result["matches"]):
            answer = parse_answer(output["answers"][f"pair_{i}"])
            decision = {"id": match["id"], "decision": answer["choice"],
                        "jev_probability": answer["probabilities"][answer["choice"]], "jev_confidence": answer["confidence"]}
            decisions.append(decision)
            if answer["choice"] == "similar":
                accepted.append(dict(match, **decision))
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise LookupError("Jev image decisions could not be completed. Please retry shortly.") from exc
    accepted.sort(key=lambda match: (-match["jev_probability"], -match["similarity"], match["id"]))
    return dict(result, matches=accepted, decisions=decisions, decision_source="jev", decision_model=output["model"],
                candidates_checked=len(decisions), jev_ms=round((time.perf_counter() - started) * 1000))
