"""Tests for the image variants the apps download instead of the original."""

from io import BytesIO

import pytest
from PIL import Image

from app.services.derivatives import (
    VARIANTS,
    VariantSpec,
    build_variants,
    variant_path,
)


def photo(width: int, height: int, mode: str = "RGB") -> bytes:
    """A JPEG-ish test image with enough detail that it cannot compress to nothing."""
    image = Image.new(mode, (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            value = (x * 7 + y * 13) % 256, (x * 3) % 256, (y * 5) % 256
            pixels[x, y] = value + ((255,) if mode == "RGBA" else ())
    buffer = BytesIO()
    image.save(buffer, "PNG" if mode == "RGBA" else "JPEG", quality=95)
    return buffer.getvalue()


def opened(content: bytes) -> Image.Image:
    return Image.open(BytesIO(content))


def test_every_size_is_produced_as_webp():
    variants = build_variants(photo(2048, 2048), "products/processed/abc_v1.png")
    assert [v.name for v in variants] == ["card", "detail", "full"]
    for variant in variants:
        assert opened(variant.content).format == "WEBP"


def test_each_size_is_capped_on_its_longest_edge():
    variants = {v.name: opened(v.content).size for v in build_variants(photo(2048, 2048), "a.png")}
    assert variants == {"card": (540, 540), "detail": (1200, 1200), "full": (2048, 2048)}


def test_aspect_ratio_is_kept():
    variants = {v.name: opened(v.content).size for v in build_variants(photo(3024, 4032), "a.jpg")}
    assert variants["card"] == (405, 540), "portrait photo keeps its shape"
    assert variants["detail"] == (900, 1200)


def test_a_small_original_is_never_blown_up():
    variants = {v.name: opened(v.content).size for v in build_variants(photo(320, 240), "a.png")}
    assert variants == {"card": (320, 240), "detail": (320, 240), "full": (320, 240)}


def test_the_card_is_far_smaller_than_the_original():
    original = photo(2048, 2048)
    card = next(v for v in build_variants(original, "a.png") if v.name == "card")
    assert len(card.content) < len(original) / 10


def test_transparency_survives():
    """Cut-out product shots have a transparent background; WebP keeps it."""
    image = Image.new("RGBA", (64, 64), (255, 0, 0, 255))
    image.putalpha(Image.new("L", (64, 64), 0).resize((64, 64)))  # fully transparent
    buffer = BytesIO()
    image.save(buffer, "PNG")

    card = next(v for v in build_variants(buffer.getvalue(), "a.png") if v.name == "card")
    decoded = opened(card.content).convert("RGBA")
    assert decoded.getpixel((5, 5))[3] == 0, "a transparent pixel stays transparent"


def test_exif_rotation_is_baked_in():
    upright = Image.new("RGB", (100, 50), "white")
    buffer = BytesIO()
    exif = Image.Exif()
    exif[274] = 6  # "rotate 90° when displaying"
    upright.save(buffer, "JPEG", exif=exif)

    card = next(v for v in build_variants(buffer.getvalue(), "a.jpg") if v.name == "card")
    assert opened(card.content).size == (50, 100), "a sideways photo is uprighted, not left rotated"


def test_the_name_carries_the_size_and_a_hash_of_the_bytes():
    variants = build_variants(photo(600, 600), "products/processed/abc_v1.png")
    for variant in variants:
        assert variant.path.startswith("products/processed/abc_v1.")
        assert variant.path.endswith(".webp")
        assert f".{variant.name}." in variant.path

    # Same bytes in, same paths out: re-running the pipeline doesn't litter.
    again = build_variants(photo(600, 600), "products/processed/abc_v1.png")
    assert [v.path for v in variants] == [v.path for v in again]

    # Different image, different paths — which is what makes it safe to tell
    # the CDN to cache these forever.
    other = build_variants(photo(600, 601), "products/processed/abc_v1.png")
    assert [v.path for v in variants] != [v.path[: len(v.path)] for v in other]


def test_path_building_handles_folders_and_missing_extensions():
    assert variant_path("a/b/c.png", "card", "dead") == "a/b/c.card.dead.webp"
    assert variant_path("a/b/c", "full", "beef") == "a/b/c.full.beef.webp"
    assert variant_path("a.b/c", "card", "f00d") == "a.b/c.card.f00d.webp"


def test_a_file_that_is_not_an_image_raises():
    with pytest.raises(Exception):
        build_variants(b"this is not an image", "a.png")


def test_specs_can_be_narrowed_for_one_off_work():
    variants = build_variants(photo(900, 900), "a.png", specs=(VariantSpec("card", 540, 80),))
    assert [v.name for v in variants] == ["card"]
