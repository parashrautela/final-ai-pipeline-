"""Small copies of an image, in the sizes the apps actually display.

A catalogue card is about 540px across on the densest screen we ship on, but
the stored image is 2048px and ~3 MB — so a grid of twelve cards downloads
~36 MB to draw about 0.5 MB worth of pixels, every single time it appears.
These variants are written next to the original when it is uploaded, and the
original is kept untouched as the archive copy.

Sizes were measured against a real catalogue photo (2048px, 2,936 KB):

    card    540px  WebP q80  ~41 KB    72x smaller
    detail 1200px  WebP q80  ~176 KB   17x smaller
    full   2048px  WebP q82  ~474 KB    6x smaller

Each file's name carries a hash of its own bytes, so a path always means one
exact image and the CDN can be told to cache it forever. Re-processing a
product writes new names rather than overwriting, which is what makes
`immutable` caching safe.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO
from typing import NamedTuple

from PIL import Image, ImageOps

WEBP_CONTENT_TYPE = "image/webp"

#: A year. Safe because the file name changes whenever the bytes do.
CACHE_SECONDS = "31536000"


@dataclass(frozen=True)
class VariantSpec:
    """One size the apps ask for."""

    name: str
    #: Longest edge, in pixels. Smaller originals are never blown up.
    max_px: int
    quality: int


VARIANTS: tuple[VariantSpec, ...] = (
    VariantSpec("card", 540, 80),
    VariantSpec("detail", 1200, 80),
    VariantSpec("full", 2048, 82),
)


class Derivative(NamedTuple):
    name: str
    path: str
    content: bytes


def variant_path(original_path: str, name: str, digest: str) -> str:
    """`products/processed/x_v1.png` → `products/processed/x_v1.card.ab12cd34.webp`."""
    stem = original_path.rsplit(".", 1)[0] if "." in original_path.rsplit("/", 1)[-1] else original_path
    return f"{stem}.{name}.{digest}.webp"


def build_variants(
    original: bytes,
    original_path: str,
    specs: tuple[VariantSpec, ...] = VARIANTS,
) -> list[Derivative]:
    """Every variant of `original`, ready to upload.

    Raises on an image Pillow cannot read; callers treat that as "no
    variants" and keep serving the original rather than failing the upload.
    """
    with Image.open(BytesIO(original)) as opened:
        # Phone photos carry their rotation in EXIF. Bake it in, or the
        # thumbnails come out sideways while the original looks right.
        image = ImageOps.exif_transpose(opened)
        image.load()
        # WebP keeps transparency, so only flatten what it cannot store.
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "A" in image.mode else "RGB")

        out: list[Derivative] = []
        for spec in specs:
            copy = image.copy()
            # thumbnail() only ever shrinks, so a small original stays as it is.
            copy.thumbnail((spec.max_px, spec.max_px), Image.LANCZOS)
            buffer = BytesIO()
            copy.save(buffer, "WEBP", quality=spec.quality, method=4)
            content = buffer.getvalue()
            digest = hashlib.sha256(content).hexdigest()[:8]
            out.append(Derivative(spec.name, variant_path(original_path, spec.name, digest), content))
        return out
