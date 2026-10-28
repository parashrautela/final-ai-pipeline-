"""Background removal for Chamak "Remove background" renders.

gpt-image-2 can't render a transparent background, and the model that can
(gpt-image-1.5) gives visibly softer, 1024px results. So a cut-out is made in
two steps: gpt-image-2 renders the piece at full size on plain white, then
BiRefNet — a model built only for separating a subject from its background —
turns the white into transparency here on the server.

Measured on 2026-10-28 for one 1024px pass on CPU: ~4.8 GB peak memory and
~16 s on two threads. The model loads in a few seconds, so it is loaded per
cut-out and released afterwards instead of holding ~2 GB all day, and only
one cut-out runs at a time. Before each one the container's free memory is
checked; without enough, the caller falls back to OpenAI's own cut-out
rather than risk the whole service being killed for memory.

`python -m app.services.cutout` downloads and verifies the model; the
Dockerfile runs it so the model ships inside the image.
"""
from __future__ import annotations

import gc
import hashlib
import io
import os
import threading
from pathlib import Path
from typing import Optional

import httpx
import numpy as np
import onnxruntime as ort
from PIL import Image

MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/BiRefNet-general-epoch_244.onnx"
MODEL_SHA256 = "58f621f00f5d756097615970a88a791584600dcf7c45b18a0a6267535a1ebd3c"
_MODEL_MAX_BYTES = 1_100_000_000
_INPUT_SIZE = (1024, 1024)
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# Peak memory one cut-out needs on top of what the service already uses,
# with some margin over the measured ~4.8 GB.
CUTOUT_PEAK_BYTES = 5_300_000_000

_lock = threading.Lock()


class NotEnoughMemory(RuntimeError):
    pass


def model_path() -> Path:
    default = Path.home() / ".cache" / "chamak-cutout" / "birefnet-general.onnx"
    return Path(os.getenv("CHAMAK_CUTOUT_MODEL_PATH", str(default)))


def prepare() -> Path:
    """Download and verify the model if it isn't already in place."""
    path = model_path()
    if path.exists() and _sha256(path) == MODEL_SHA256:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".download")
    try:
        digest, size = hashlib.sha256(), 0
        with httpx.stream("GET", MODEL_URL, follow_redirects=True, timeout=600) as response:
            response.raise_for_status()
            with temporary.open("wb") as file:
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > _MODEL_MAX_BYTES:
                        raise RuntimeError("Cut-out model larger than expected")
                    digest.update(chunk)
                    file.write(chunk)
        if digest.hexdigest() != MODEL_SHA256:
            raise RuntimeError("Cut-out model checksum mismatch")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def memory_headroom() -> Optional[int]:
    """Bytes the container can still use, or None when there's no readable limit.

    Reads the cgroup (v2, then v1) limit and current usage — what Railway
    enforces — rather than the host's free memory.
    """
    for limit_file, usage_file in (
        ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current"),
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes", "/sys/fs/cgroup/memory/memory.usage_in_bytes"),
    ):
        try:
            limit = Path(limit_file).read_text().strip()
            usage = int(Path(usage_file).read_text().strip())
        except (OSError, ValueError):
            continue
        if limit == "max" or int(limit) >= 1 << 60:
            return None
        return int(limit) - usage
    return None


def has_room() -> bool:
    headroom = memory_headroom()
    return headroom is None or headroom >= CUTOUT_PEAK_BYTES


def remove_background(image_bytes: bytes) -> bytes:
    """A transparent PNG of `image_bytes` with its background removed.

    Blocking and memory-heavy: call it through asyncio.to_thread. Raises
    NotEnoughMemory when the container can't fit a pass right now.
    """
    with Image.open(io.BytesIO(image_bytes)) as opened:
        image = opened.convert("RGB")

    pixels = np.asarray(image.resize(_INPUT_SIZE, Image.Resampling.LANCZOS), dtype=np.float32)
    pixels /= max(float(pixels.max()), 1e-6)
    pixels = ((pixels - _MEAN) / _STD).transpose(2, 0, 1)[None].astype(np.float32)

    with _lock:
        if not has_room():
            raise NotEnoughMemory(f"Less than {CUTOUT_PEAK_BYTES / 1e9:.1f} GB free for a cut-out")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        # The arena keeps freed memory reserved; without it the peak is lower
        # and the memory goes back once the pass is done.
        options.enable_cpu_mem_arena = False
        session = ort.InferenceSession(str(prepare()), sess_options=options, providers=["CPUExecutionProvider"])
        try:
            prediction = session.run(None, {session.get_inputs()[0].name: pixels})[0][0, 0]
        finally:
            del session
            gc.collect()

    prediction = 1 / (1 + np.exp(-prediction))
    spread = float(prediction.max() - prediction.min())
    if not np.isfinite(prediction).all() or spread < 1e-6:
        raise RuntimeError("Cut-out model returned an empty mask")
    prediction = (prediction - prediction.min()) / spread
    mask = Image.fromarray((prediction * 255).astype("uint8")).resize(image.size, Image.Resampling.LANCZOS)

    cut = image.convert("RGBA")
    cut.putalpha(mask)
    output = io.BytesIO()
    cut.save(output, "PNG")
    return output.getvalue()


if __name__ == "__main__":
    print(f"Cut-out model ready at {prepare()}")
