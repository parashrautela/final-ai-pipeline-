import logging

from pydantic_settings import BaseSettings

_log = logging.getLogger(__name__)

# jewellery design if the instruction isn't specific enough.
# Use {item_description} as a placeholder — it is replaced at runtime with the
# actual product title and jewellery type (e.g. "Diamond Tennis Bracelet (bracelet)").
VARIANT_PROMPTS = [
    (
        "STRICT IMAGE COMPOSITING TASK. "
        "Subject: {item_description}. "
        "Place the {item_description} on a dark navy-blue sculpted stone surface. "
        "Classic front-facing display angle — {item_description} perfectly centred. "
        "Soft directional studio lighting from the upper-left. "
        "Deep, moody, luxurious atmosphere. No background distractions. "
        "Preserve every detail, texture, reflection, and gemstone of the {item_description} exactly. "
        "Do NOT modify the {item_description} design, colour, or proportions. "
        "Professional luxury product photography."
    ),
    (
        "STRICT IMAGE COMPOSITING TASK. "
        "Subject: {item_description}. "
        "Place the {item_description} on a deep burgundy-red velvet cushion surface. "
        "Present at a gentle 45-degree angle — front-left perspective. "
        "Warm golden studio lighting from the upper-right. "
        "Shallow depth of field; soft bokeh background haze. "
        "Luxury jewellery boutique aesthetic. "
        "Preserve every detail, texture, reflection, and gemstone of the {item_description} exactly. "
        "Do NOT modify the {item_description} design, colour, or proportions. "
        "Professional luxury product photography."
    ),
    (
        "STRICT IMAGE COMPOSITING TASK. "
        "Subject: {item_description}. "
        "Place the {item_description} on a pristine white Carrara marble surface with subtle grey veining. "
        "Slight overhead / elevated perspective — camera angled 30 degrees above horizontal. "
        "Bright diffused natural daylight; clean, airy, editorial feel. "
        "Minimal composition — {item_description} as the sole subject. "
        "High-fashion editorial campaign style. "
        "Preserve every detail, texture, reflection, and gemstone of the {item_description} exactly. "
        "Do NOT modify the {item_description} design, colour, or proportions. "
        "Professional luxury product photography."
    ),
    (
        "STRICT IMAGE COMPOSITING TASK. "
        "Subject: {item_description}. "
        "Place the {item_description} floating and centred against a deep charcoal-black gradient background. "
        "Subtle warm amber and gold light accents rim the edges. "
        "Dramatic side-profile angle — {item_description} rotated approximately 60 degrees. "
        "Single hard spotlight from directly above creating a defined shadow below. "
        "Premium luxury advertisement style — bold and dramatic. "
        "Preserve every detail, texture, reflection, and gemstone of the {item_description} exactly. "
        "Do NOT modify the {item_description} design, colour, or proportions. "
        "Professional luxury product photography."
    ),
]


class Settings(BaseSettings):
    # Supabase
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""  # service role bypasses RLS, keep this secret

    # AI Models
    REVE_API_KEY: str = ""
    REVE_PROMPT: str = ""
    NANOBANA_API_KEY: str = ""
    NANOBANA_PROMPT: str = ""  # only used in single-variant mode; ignored in 4-variant flow

    # Variant prompts (can be overridden via env vars)
    NANOBANA_VARIANT_PROMPT_1: str = VARIANT_PROMPTS[0]
    NANOBANA_VARIANT_PROMPT_2: str = VARIANT_PROMPTS[1]
    NANOBANA_VARIANT_PROMPT_3: str = VARIANT_PROMPTS[2]
    NANOBANA_VARIANT_PROMPT_4: str = VARIANT_PROMPTS[3]

    @property
    def NANOBANA_VARIANT_PROMPTS(self) -> list[str]:
        # Collected as a list so pipeline.py can just zip() over it.
        return [
            self.NANOBANA_VARIANT_PROMPT_1,
            self.NANOBANA_VARIANT_PROMPT_2,
            self.NANOBANA_VARIANT_PROMPT_3,
            self.NANOBANA_VARIANT_PROMPT_4,
        ]

    # App
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    # Set TEST_MODE=true in .env to generate only 1 variant (saves API credits during testing)
    TEST_MODE: bool = False

    # Number of Nanobana variants to generate per product, one per entry in
    # VARIANT_SCENE_SETTINGS (pipeline.py). Named to match the
    # IMAGE_GENERATION_COUNT env var already set on Railway — that var existed
    # before any code read it, so it silently did nothing; this wires it up.
    # Only 4 scenes are defined today, so this is clamped to that range
    # regardless of what the env var says — see the variant_count property
    # below. TEST_MODE still forces this to 1.
    IMAGE_GENERATION_COUNT: int = 4

    @property
    def variant_count(self) -> int:
        if self.TEST_MODE:
            return 1
        return max(1, min(self.IMAGE_GENERATION_COUNT, 4))

    # Nanobana generate-pro "resolution" value. Named to match the
    # NANOBANA_IMAGE_SIZE env var already set on Railway (to "2k") — that var
    # existed before any code read it either; ai.py had "2K" hardcoded.
    NANOBANA_IMAGE_SIZE: str = "2K"

    @property
    def nanobana_resolution(self) -> str:
        value = (self.NANOBANA_IMAGE_SIZE or "").strip().upper()
        if value not in ("1K", "2K", "4K"):
            _log.warning(
                f"NANOBANA_IMAGE_SIZE={self.NANOBANA_IMAGE_SIZE!r} is not one of "
                f"1K/2K/4K — falling back to 2K"
            )
            return "2K"
        return value

    # Observability
    # Sentry DSN for error reporting. Left blank by default so the app still
    # starts fine locally / in CI without it — sentry_sdk.init() is only
    # called (in app/main.py) when this is actually set. Set the SENTRY_DSN
    # env var in the deployment environment to enable error tracking.
    SENTRY_DSN: str = ""

    # Worker
    POLL_INTERVAL_SECONDS: int = 2
    MAX_CONCURRENT_JOBS: int = 5
    MAX_RETRIES: int = 3
    PROCESSING_TIMEOUT_SECONDS: int = 300  # 5 minutes before a stuck job is reset

    # Database
    DB_TABLE_NAME: str = "images"

    # ── Auth ─────────────────────────────────────────────────────────────────
    # Supabase project JWT secret (Dashboard → Settings → API → JWT Secret).
    # When set, end-user tokens are verified locally with HS256 — no network
    # hop, and credit-bearing routes keep working if Supabase Auth blips.
    # When blank, app/auth.py falls back to asking Supabase to resolve the
    # token, which is correct for projects using asymmetric signing keys.
    # Either way an unverifiable token is rejected; there is no anonymous path.
    SUPABASE_JWT_SECRET: str = ""

    # ── Credits (Treasure Chest) ─────────────────────────────────────────────
    # Master switch. Ships OFF so the rollout can be staged safely:
    #   1. deploy this code with CREDITS_ENABLED=false — auth is enforced,
    #      nothing is charged
    #   2. run migrations/004_credits_treasure_chest.sql
    #   3. verify wallets are populating, then flip this to true
    # With it off, credit-bearing routes behave exactly as they did before.
    CREDITS_ENABLED: bool = False

    # What a single AI call actually costs us, in paise, recorded into
    # credit_ledger.metadata on every debit. We bill a FLAT price — these are
    # only so real margin per generation is measurable rather than guessed.
    # Update them when the vendor bill changes; nothing in the pricing logic
    # reads these.
    COST_PAISE_VISION_ANALYSIS: int = 150     # OpenAI vision, Chamak stage 1
    COST_PAISE_IMAGE_GENERATION: int = 950    # one Nano Banana generation
    COST_PAISE_BACKGROUND_REMOVAL: int = 200  # one Reve call

    @property
    def chamak_generation_cost_paise(self) -> int:
        """Rough COGS of one Chamak fusion: one vision call + one image."""
        return self.COST_PAISE_VISION_ANALYSIS + self.COST_PAISE_IMAGE_GENERATION

    @property
    def product_upload_cost_paise(self) -> int:
        """Rough COGS of one product upload: a background removal + N variants.

        Worth reading twice: at the default variant_count of 4 this is several
        times the cost of a Chamak fusion, which is why `product.upload` is
        seeded at zero credits rather than at a guessed price.
        """
        return (
            self.COST_PAISE_BACKGROUND_REMOVAL
            + self.COST_PAISE_IMAGE_GENERATION * self.variant_count
        )

    # Storage
    RAW_BUCKET_NAME: str = "plant-images"
    RAW_STORAGE_FOLDER: str = "products"
    PROCESSED_BUCKET_NAME: str = "plant-images"  # same bucket, different folder
    PROCESSED_STORAGE_FOLDER: str = "products/processed"
    ALLOWED_MIME_TYPES: list[str] = ["image/jpeg", "image/png", "image/webp"]
    MAX_FILE_SIZE_BYTES: int = 10 * 1024 * 1024  # 10 MB

    # Chamak & OpenAI
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o"
    CHAMAK_TABLE_NAME: str = "chamak_generations"
    CHAMAK_OUTPUT_BUCKET: str = "chamak-outputs"
    CHAMAK_PROMPT_VERSION: str = "v1.0-chamak"

    class Config:
        env_file = ".env"
        extra = "ignore"  # silently drop any unknown keys from .env


settings = Settings()


def build_variant_prompts(title: str = "", jewellery_type: str = "") -> list[str]:
    """Return the 4 Nanobana variant prompts with product data injected.

    Combines ``title`` and ``jewellery_type`` (both already sanitised by
    validation.py) into a single ``item_description`` string that is
    substituted into every ``{item_description}`` placeholder in each prompt
    template.  This gives the AI precise context about the specific product
    so it doesn't drift from the original design.

    Examples
    --------
    title="Diamond Tennis Bracelet", jewellery_type="bracelet"
        → item_description = "Diamond Tennis Bracelet (bracelet)"

    title="Gold Ring", jewellery_type=""
        → item_description = "Gold Ring"

    title="", jewellery_type="necklace"
        → item_description = "necklace"

    title="", jewellery_type=""
        → item_description = "jewellery"
    """
    title = (title or "").strip()
    jewellery_type = (jewellery_type or "").strip()

    if title and jewellery_type:
        item_description = f"{title} ({jewellery_type})"
    elif title:
        item_description = title
    elif jewellery_type:
        item_description = jewellery_type
    else:
        item_description = "jewellery"

    # .format() only substitutes named placeholders that exist in the string —
    # templates without {item_description} are returned unchanged, so custom
    # env-var overrides that omit the placeholder continue to work safely.
    return [p.format(item_description=item_description) for p in settings.NANOBANA_VARIANT_PROMPTS]
