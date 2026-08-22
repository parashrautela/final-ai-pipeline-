from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from app.db.repository import fetch_active_prompt_modules
from app.logging import logger

# Cache duration in seconds (1 minute TTL)
CACHE_TTL_SECONDS = 60.0

# ---------------------------------------------------------------------------
# Fallback Default Prompt Modules (used if DB is unreachable or empty)
# ---------------------------------------------------------------------------

DEFAULT_BASE_PROMPT = (
    "STRICT HIGH-END JEWELLERY COMPOSITING TASK. "
    "Subject: {item_description}. "
    "Ensure immaculate background removal and clean edge separation with zero artifact halos. "
    "Studio lighting baseline: Balanced soft diffused 5600K daylight key light with subtle rim highlights to accentuate metallic luster (22k/18k gold, platinum, silver) and maximize gemstone dispersion, fire, and internal refraction. "
    "Color accuracy and fidelity: Strict true-to-life precious metal tone matching and vibrant, natural gemstone color saturation. "
    "Composition and Resolution: Crisp 8k macro photographic clarity, perfectly sharp focus on critical focal planes, pristine surface reflections, and hyper-realistic micro-shadows grounding the piece naturally. "
    "Preserve every structural detail, engraving, prong, pavé setting, and gemstone facet without distortion, hallucination, or design alterations."
)

DEFAULT_CATEGORY_PROMPTS = {
    "jhumka": (
        "CATEGORY RULES — JHUMKA / HANGING EARRINGS: "
        "1. Gravity and Hanging Physics: The bell/dome (jhumki) must hang with realistic downward gravitational plumb alignment, suspended naturally from the stud/karnphool ear-post or hanging hoop. "
        "2. Dangles and Latkans: Dangling seed pearls, hanging gold beads, and delicate micro-bead fringe must hang vertically under natural gravity with realistic spacing, slight natural pendulum dispersion, and individual bead cast shadows. "
        "3. Dome Structure and Cavity Lighting: The interior hollow of the bell dome must exhibit natural soft ambient occlusion and graduated depth shadowing, while the exterior filigree/embossing catches directional rim highlights. "
        "4. Symmetry and Ear Connection: Maintain pristine structural symmetry across the dome circumference, crisp connection loops, and sharp ear-hook/post definition without flattening."
    ),
    "mangalsutra": (
        "CATEGORY RULES — MANGALSUTRA: "
        "1. Chain Drape and Bead Tension: The black-and-gold auspicious bead chain (karimani) must follow a natural sweeping fluid drape under authentic chain tension, avoiding rigid or artificial bends. "
        "2. Pendant Alignment and Resting Geometry: The central sacred pendant (such as twin gold vatis or diamond/tanmaniya centerpiece) must rest centered and perfectly balanced, laying flat against the presentation plane without tilting or floating. "
        "3. Black Bead Luster: Render individual black spinel/onyx/glass beads with crisp specular micro-highlights and realistic stringing knot definition. "
        "4. Centerpiece Focus: Maximize gemstone fire and intricate yellow gold craftsmanship on the central pendant while maintaining pristine continuity with the dual-strand or single-strand chain connections."
    ),
    "necklace": (
        "CATEGORY RULES — NECKLACE: "
        "1. Collar Curvature and Drape: Maintain an anatomical sweeping curve mimicking a natural neckline/bust display or flat lay collar curvature without kinks or unnatural gaps. "
        "2. Symmetry and Segment Articulation: Individual linked elements, gemstones, and motifs must cascade evenly along the curve with natural hinge articulation and proportional spacing. "
        "3. Center Drop Balance: The central pendant or focal station must anchor the apex of the curve with balanced downward weight distribution. "
        "4. Clasp and Extender Realism: Preserve crisp chain link resolution and realistic metal clasp/back-chain finish."
    ),
    "ring": (
        "CATEGORY RULES — RING: "
        "1. Band Geometry and Roundness: Maintain circular shank geometry and continuous smooth metal curvature without oval warping or structural distortion. "
        "2. Gemstone Setting and Table Brilliance: Solitaire and accent stones must exhibit pristine table facets, crown angles, pavilion reflections, and crisp prong/bezel security. "
        "3. Internal Shank Reflections: Render realistic internal band reflection, soft ambient shading on the inner surface, and subtle hallmark legibility where visible. "
        "4. Grounding Shadow: Cast a defined, delicate contact shadow beneath the band base and head to firmly anchor the ring on the presentation surface."
    ),
    "bangle": (
        "CATEGORY RULES — BANGLE / BRACELET: "
        "1. Rigid Circular / Oval Symmetry: Maintain rigid structural roundness and uniform wall thickness across the perimeter. "
        "2. Radial Metallic Reflections: Highlights must follow the cylindrical metal contour with smooth sweeping specular gradients. "
        "3. Clasp / Screw Articulation: Render hinges, clasp mechanisms, safety catches, or screw motifs with mechanical precision. "
        "4. Stacking and Inner Cavity: The inner rim must exhibit soft ambient shading and realistic hollow-cavity lighting without distortion."
    ),
    "other": (
        "CATEGORY RULES — GENERAL JEWELLERY FALLBACK: "
        "1. Structural Integrity: Preserve all three-dimensional contours, structural junctions, and mechanical components with exact geometric fidelity. "
        "2. Metal Luster and Gem Fire: Enhance natural surface polish, micro-textures, and optical gemstone dispersion across all visible surfaces. "
        "3. Natural Contact Shadows: Anchor the piece firmly onto the display plane with soft, realistic contact ambient occlusion. "
        "4. Design Authenticity: Strict zero-hallucination policy—do not alter motifs, engravings, stones, or silhouette."
    ),
}


@dataclass
class PromptModuleItem:
    id: str
    module_type: str
    jewellery_type: Optional[str]
    prompt_text: str
    version: int


@dataclass
class ComposedPromptResult:
    composed_prompt: str
    base_module_version: int
    category_module_version: int
    jewellery_type_requested: str
    jewellery_type_matched: str


class PromptComposer:
    def __init__(self, ttl: float = CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._cached_at: float = 0.0
        self._cached_base: Optional[PromptModuleItem] = None
        self._cached_categories: dict[str, PromptModuleItem] = {}

    def invalidate_cache(self) -> None:
        """Manually clear the in-memory cache to force a reload on the next call."""
        self._cached_at = 0.0
        self._cached_base = None
        self._cached_categories = {}
        logger.info("PromptComposer cache invalidated.")

    async def _refresh_cache_if_expired(self) -> None:
        now = time.time()
        if (now - self._cached_at) < self._ttl and self._cached_base is not None:
            return

        try:
            modules = await fetch_active_prompt_modules()
            if modules:
                base_item = None
                categories: dict[str, PromptModuleItem] = {}

                for m in modules:
                    m_type = (m.get("module_type") or "").strip().lower()
                    j_type = (m.get("jewellery_type") or "").strip().lower() if m.get("jewellery_type") else None
                    item = PromptModuleItem(
                        id=str(m.get("id", "")),
                        module_type=m_type,
                        jewellery_type=j_type,
                        prompt_text=m.get("prompt_text", ""),
                        version=int(m.get("version", 1)),
                    )

                    if m_type == "base":
                        base_item = item
                    elif m_type == "category" and j_type:
                        categories[j_type] = item

                if base_item:
                    self._cached_base = base_item
                self._cached_categories = categories
                self._cached_at = now
                logger.info(
                    f"PromptComposer cache refreshed from DB — Base v{self._cached_base.version if self._cached_base else 1}, "
                    f"{len(self._cached_categories)} active category modules: {list(self._cached_categories.keys())}"
                )
                return

        except Exception as exc:
            logger.warning(f"Failed to refresh prompt modules from DB: {exc}. Using existing cache or default fallback.")

        # If cache is still empty after DB failure or empty response, set defaults
        if self._cached_base is None:
            self._cached_base = PromptModuleItem(
                id="default-base",
                module_type="base",
                jewellery_type=None,
                prompt_text=DEFAULT_BASE_PROMPT,
                version=1,
            )
        if not self._cached_categories:
            self._cached_categories = {
                j_type: PromptModuleItem(
                    id=f"default-{j_type}",
                    module_type="category",
                    jewellery_type=j_type,
                    prompt_text=text,
                    version=1,
                )
                for j_type, text in DEFAULT_CATEGORY_PROMPTS.items()
            }
        self._cached_at = now

    async def get_valid_jewellery_types(self) -> set[str]:
        """Return the set of valid jewellery_type keys currently active in the database/cache."""
        await self._refresh_cache_if_expired()
        valid = set(self._cached_categories.keys())
        # Always ensure 'other' is recognized as a valid fallback
        valid.add("other")
        return valid

    async def get_composed_prompt(
        self,
        jewellery_type: str,
        item_description: str = "",
    ) -> ComposedPromptResult:
        """
        Compose final prompt = base_module + category_module[jewellery_type].

        If the requested category is not found, falls back to 'other'.
        Substitutes {item_description} in templates when provided.
        """
        await self._refresh_cache_if_expired()

        base_item = self._cached_base or PromptModuleItem(
            id="default-base",
            module_type="base",
            jewellery_type=None,
            prompt_text=DEFAULT_BASE_PROMPT,
            version=1,
        )

        norm_type = (jewellery_type or "other").strip().lower()
        category_item = self._cached_categories.get(norm_type)
        matched_type = norm_type

        if not category_item:
            category_item = self._cached_categories.get("other")
            matched_type = "other"

        if not category_item:
            # Absolute fallback
            category_item = PromptModuleItem(
                id="default-other",
                module_type="category",
                jewellery_type="other",
                prompt_text=DEFAULT_CATEGORY_PROMPTS["other"],
                version=1,
            )
            matched_type = "other"

        base_text = base_item.prompt_text
        cat_text = category_item.prompt_text

        desc = item_description.strip() or (norm_type if norm_type != "other" else "jewellery")
        if "{item_description}" in base_text:
            base_text = base_text.replace("{item_description}", desc)
        if "{item_description}" in cat_text:
            cat_text = cat_text.replace("{item_description}", desc)

        composed_text = f"{base_text}\n\n{cat_text}"

        return ComposedPromptResult(
            composed_prompt=composed_text,
            base_module_version=base_item.version,
            category_module_version=category_item.version,
            jewellery_type_requested=norm_type,
            jewellery_type_matched=matched_type,
        )


# Singleton prompt composer instance
prompt_composer = PromptComposer()
