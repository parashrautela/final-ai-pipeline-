"""Pre-indexed catalogue retrieval. CLIP runs here, never at an external AI API."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import threading
from pathlib import Path
from urllib.parse import urlparse

import httpx
import numpy as np
import onnxruntime as ort
from PIL import Image, ImageOps, UnidentifiedImageError

logger = logging.getLogger(__name__)
MODEL_REVISION = "d15189d7028b43f1d3e65039190477f6af591c2a"
MODEL_SHA256 = "583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299"
MODEL_URL = f"https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/{MODEL_REVISION}/onnx/vision_model_quantized.onnx"
MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 50_000_000
MIN_SIMILARITY = 0.90  # Retrieval score, not a measured accuracy percentage.
COLUMNS = "id,jewellery_type,is_published,raw_image_url,processed_image_url,image_url,generated_image_urls,showcase_image_urls"


def category(value: str | None) -> str:
    value = (value or "").strip().lower()
    return {"necklaces": "necklace", "earrings": "earring", "bangles": "bangle", "pendants": "pendant"}.get(value, value)


def source_url(row: dict) -> str | None:
    for key in ("showcase_image_urls", "generated_image_urls"):
        if isinstance(row.get(key), list):
            for value in row[key]:
                if isinstance(value, str) and value.strip():
                    return value.strip()
    for key in ("processed_image_url", "image_url", "raw_image_url"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def cache_key(row: dict) -> str:
    return f"{row['id']}:{source_url(row) or ''}"


def download_model(path: Path) -> None:
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == MODEL_SHA256:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".download")
    digest = hashlib.sha256()
    try:
        with httpx.stream("GET", MODEL_URL, follow_redirects=True, timeout=120) as response:
            response.raise_for_status()
            with temporary.open("wb") as output:
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > 100 * 1024 * 1024:
                        raise RuntimeError("Model download exceeded its expected size")
                    digest.update(chunk)
                    output.write(chunk)
        if digest.hexdigest() != MODEL_SHA256:
            raise RuntimeError("Image model checksum mismatch")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class InvalidPhoto(ValueError):
    pass


class ImageEncoder:
    def __init__(self, path: Path | None = None):
        self.path = path or Path(os.getenv("IMAGE_SEARCH_MODEL_PATH", str(Path.home() / ".cache/jewel-image-search/vision.onnx")))
        self.session = None
        self.lock = threading.Lock()

    @staticmethod
    def pixels(photo: bytes) -> np.ndarray:
        if not photo or len(photo) > MAX_BYTES:
            raise InvalidPhoto("Choose a photo smaller than 10 MB.")
        try:
            with Image.open(io.BytesIO(photo)) as original:
                if original.width * original.height > MAX_PIXELS or max(original.size) > 32 * min(original.size):
                    raise InvalidPhoto("Choose a smaller photo of the jewellery.")
                image = ImageOps.exif_transpose(original)
                if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                    rgba = image.convert("RGBA")
                    image = Image.new("RGB", rgba.size, "white")
                    image.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    image = image.convert("RGB")
                width, height = image.size
                # Exact pinned CLIP processor: shortest edge 224, bicubic,
                # centre crop 224, RGB rescale and CLIP mean/std normalization.
                scale = 224 / min(width, height)
                image = image.resize((int(width * scale), int(height * scale)), Image.Resampling.BICUBIC)
                x, y = (image.width - 224) // 2, (image.height - 224) // 2
                image = image.crop((x, y, x + 224, y + 224))
                pixels = np.asarray(image, dtype=np.float32) / 255.0
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError) as exc:
            if isinstance(exc, InvalidPhoto):
                raise
            raise InvalidPhoto("Choose a readable photo of the jewellery.") from exc
        mean = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
        std = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
        return np.ascontiguousarray(((pixels - mean) / std).transpose(2, 0, 1)[None])

    def embed(self, photo: bytes) -> np.ndarray:
        pixels = self.pixels(photo)
        with self.lock:
            if self.session is None:
                download_model(self.path)
                options = ort.SessionOptions()
                options.intra_op_num_threads = 2
                options.inter_op_num_threads = 1
                self.session = ort.InferenceSession(str(self.path), sess_options=options, providers=["CPUExecutionProvider"])
            vector = self.session.run(["image_embeds"], {"pixel_values": pixels})[0].reshape(-1).astype(np.float32)
        norm = float(np.linalg.norm(vector))
        if vector.shape != (512,) or not np.isfinite(vector).all() or not np.isfinite(norm) or norm < 1e-6:
            raise RuntimeError("Invalid image model output")
        return vector / norm


class CatalogueIndex:
    def __init__(self, encoder: ImageEncoder, fetch_rows, allowed_hosts: set[str], cache_path: Path | None = None):
        self.encoder, self.fetch_rows = encoder, fetch_rows
        self.allowed_hosts = allowed_hosts
        self.cache_path = cache_path or Path(os.getenv("IMAGE_SEARCH_INDEX_PATH", str(Path.home() / ".cache/jewel-image-search/index.json")))
        self.vectors: dict[str, np.ndarray] = {}
        self.failed: set[str] = set()
        self.refresh_lock = asyncio.Lock()
        self.refresh_task = None
        self.load_cache()

    def load_cache(self):
        try:
            saved = json.loads(self.cache_path.read_text())
            if saved.get("model_sha256") != MODEL_SHA256:
                return
            for key, values in saved.get("vectors", {}).items():
                vector = np.asarray(values, dtype=np.float32)
                norm = float(np.linalg.norm(vector))
                if vector.shape == (512,) and np.isfinite(vector).all() and 0.99 < norm < 1.01:
                    self.vectors[key] = vector
        except (OSError, ValueError, TypeError):
            pass

    def persist(self, vectors):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"model_sha256": MODEL_SHA256, "vectors": vectors}, separators=(",", ":")))
        temporary.replace(self.cache_path)

    async def download(self, client: httpx.AsyncClient, url: str | None) -> bytes:
        parsed = urlparse(url or "")
        if parsed.scheme != "https" or parsed.hostname not in self.allowed_hosts or parsed.port not in (None, 443) or parsed.username:
            raise InvalidPhoto("Unavailable catalogue image")
        data = bytearray()
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            if int(response.headers.get("content-length", "0")) > MAX_BYTES:
                raise InvalidPhoto("Oversized catalogue image")
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > MAX_BYTES:
                    raise InvalidPhoto("Oversized catalogue image")
                data.extend(chunk)
        return bytes(data)

    def schedule_refresh(self):
        if self.refresh_task is None or self.refresh_task.done():
            self.refresh_task = asyncio.create_task(self.refresh())

    async def refresh(self, rows: list[dict] | None = None):
        async with self.refresh_lock:
            try:
                if rows is None:
                    rows = await asyncio.to_thread(self.fetch_rows)
                rows = [r for r in rows if r.get("is_published") is True]
                keys = {cache_key(row) for row in rows}
                self.vectors = {key: vector for key, vector in self.vectors.items() if key in keys}
                self.failed.intersection_update(keys)
                concurrency = asyncio.Semaphore(4)
                async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                    async def index(row):
                        key = cache_key(row)
                        if key in self.vectors:
                            return
                        async with concurrency:
                            try:
                                photo = await self.download(client, source_url(row))
                                self.vectors[key] = await asyncio.to_thread(self.encoder.embed, photo)
                                self.failed.discard(key)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                self.failed.add(key)
                    await asyncio.gather(*(index(row) for row in rows))
                snapshot = {key: vector.tolist() for key, vector in self.vectors.items()}
                await asyncio.to_thread(self.persist, snapshot)
                logger.info("Catalogue index ready: %s indexed, %s unavailable", len(self.vectors), len(self.failed))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Catalogue index refresh failed; search will not invent results")

    async def maintain(self):
        while True:
            await self.refresh()
            await asyncio.sleep(60)

    async def search(self, photo: bytes, rows: list[dict], chosen_category: str) -> dict:
        rows = [r for r in rows if r.get("is_published") is True and category(r.get("jewellery_type")) == category(chosen_category)]
        if not rows:
            return {"matches": [], "checked": 0, "total": 0, "skipped": 0}
        missing = [row for row in rows if cache_key(row) not in self.vectors and cache_key(row) not in self.failed]
        if missing:
            self.schedule_refresh()
            raise LookupError("Catalogue image search is updating. Please retry shortly.")
        available = [row for row in rows if cache_key(row) in self.vectors]
        if not available:
            raise LookupError("Catalogue photos couldn’t be checked. Please try again.")
        query = await asyncio.to_thread(self.encoder.embed, photo)
        matrix = np.stack([self.vectors[cache_key(row)] for row in available])
        scores = np.einsum("ij,j->i", matrix, query, optimize=False)
        if not np.isfinite(scores).all():
            raise RuntimeError("Invalid catalogue similarity scores")
        matches = [{"id": row["id"], "similarity": float(np.clip(score, -1, 1))}
                   for row, score in zip(available, scores) if np.isfinite(score) and score >= MIN_SIMILARITY]
        matches.sort(key=lambda match: (-match["similarity"], match["id"]))
        return {"matches": matches[:20], "checked": len(available), "total": len(rows), "skipped": len(rows) - len(available)}


if __name__ == "__main__":
    download_model(ImageEncoder().path)
    print("Pinned CLIP image model downloaded and checksum verified.")
