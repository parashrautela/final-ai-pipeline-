-- Migration 003: Create prompt_modules table, seed category/base prompts, and extend ai_generation_logs table

-- 1. Create prompt_modules table
CREATE TABLE IF NOT EXISTS public.prompt_modules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    module_type TEXT NOT NULL CHECK (module_type IN ('base', 'category')),
    jewellery_type TEXT, -- NULL for base module, e.g. 'jhumka', 'mangalsutra', 'necklace', 'ring', 'bangle', 'other'
    prompt_text TEXT NOT NULL,
    version INT NOT NULL DEFAULT 1,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Unique index ensuring only one active row per (module_type, jewellery_type) combo
CREATE UNIQUE INDEX IF NOT EXISTS idx_prompt_modules_active
    ON public.prompt_modules (module_type, COALESCE(jewellery_type, ''))
    WHERE is_active = true;

CREATE INDEX IF NOT EXISTS idx_prompt_modules_type ON public.prompt_modules (module_type, is_active);

-- Enable RLS with no public/authenticated policies (service role backend access only)
ALTER TABLE public.prompt_modules ENABLE ROW LEVEL SECURITY;

-- 2. Seed Prompt Modules

-- Base Prompt Module (Version 1)
INSERT INTO public.prompt_modules (module_type, jewellery_type, prompt_text, version, is_active)
VALUES (
    'base',
    NULL,
    'STRICT HIGH-END JEWELLERY COMPOSITING TASK. '
    'Subject: {item_description}. '
    'Ensure immaculate background removal and clean edge separation with zero artifact halos. '
    'Studio lighting baseline: Balanced soft diffused 5600K daylight key light with subtle rim highlights to accentuate metallic luster (22k/18k gold, platinum, silver) and maximize gemstone dispersion, fire, and internal refraction. '
    'Color accuracy and fidelity: Strict true-to-life precious metal tone matching and vibrant, natural gemstone color saturation. '
    'Composition and Resolution: Crisp 8k macro photographic clarity, perfectly sharp focus on critical focal planes, pristine surface reflections, and hyper-realistic micro-shadows grounding the piece naturally. '
    'Preserve every structural detail, engraving, prong, pavé setting, and gemstone facet without distortion, hallucination, or design alterations.',
    1,
    true
)
ON CONFLICT DO NOTHING;

-- Category Prompt Module: Jhumka (Version 1)
INSERT INTO public.prompt_modules (module_type, jewellery_type, prompt_text, version, is_active)
VALUES (
    'category',
    'jhumka',
    'CATEGORY RULES — JHUMKA / HANGING EARRINGS: '
    '1. Gravity and Hanging Physics: The bell/dome (jhumki) must hang with realistic downward gravitational plumb alignment, suspended naturally from the stud/karnphool ear-post or hanging hoop. '
    '2. Dangles and Latkans: Dangling seed pearls, hanging gold beads, and delicate micro-bead fringe must hang vertically under natural gravity with realistic spacing, slight natural pendulum dispersion, and individual bead cast shadows. '
    '3. Dome Structure and Cavity Lighting: The interior hollow of the bell dome must exhibit natural soft ambient occlusion and graduated depth shadowing, while the exterior filigree/embossing catches directional rim highlights. '
    '4. Symmetry and Ear Connection: Maintain pristine structural symmetry across the dome circumference, crisp connection loops, and sharp ear-hook/post definition without flattening.',
    1,
    true
)
ON CONFLICT DO NOTHING;

-- Category Prompt Module: Mangalsutra (Version 1)
INSERT INTO public.prompt_modules (module_type, jewellery_type, prompt_text, version, is_active)
VALUES (
    'category',
    'mangalsutra',
    'CATEGORY RULES — MANGALSUTRA: '
    '1. Chain Drape and Bead Tension: The black-and-gold auspicious bead chain (karimani) must follow a natural sweeping fluid drape under authentic chain tension, avoiding rigid or artificial bends. '
    '2. Pendant Alignment and Resting Geometry: The central sacred pendant (such as twin gold vatis or diamond/tanmaniya centerpiece) must rest centered and perfectly balanced, laying flat against the presentation plane without tilting or floating. '
    '3. Black Bead Luster: Render individual black spinel/onyx/glass beads with crisp specular micro-highlights and realistic stringing knot definition. '
    '4. Centerpiece Focus: Maximize gemstone fire and intricate yellow gold craftsmanship on the central pendant while maintaining pristine continuity with the dual-strand or single-strand chain connections.',
    1,
    true
)
ON CONFLICT DO NOTHING;

-- Category Prompt Module: Necklace (Version 1)
INSERT INTO public.prompt_modules (module_type, jewellery_type, prompt_text, version, is_active)
VALUES (
    'category',
    'necklace',
    'CATEGORY RULES — NECKLACE: '
    '1. Collar Curvature and Drape: Maintain an anatomical sweeping curve mimicking a natural neckline/bust display or flat lay collar curvature without kinks or unnatural gaps. '
    '2. Symmetry and Segment Articulation: Individual linked elements, gemstones, and motifs must cascade evenly along the curve with natural hinge articulation and proportional spacing. '
    '3. Center Drop Balance: The central pendant or focal station must anchor the apex of the curve with balanced downward weight distribution. '
    '4. Clasp and Extender Realism: Preserve crisp chain link resolution and realistic metal clasp/back-chain finish.',
    1,
    true
)
ON CONFLICT DO NOTHING;

-- Category Prompt Module: Ring (Version 1)
INSERT INTO public.prompt_modules (module_type, jewellery_type, prompt_text, version, is_active)
VALUES (
    'category',
    'ring',
    'CATEGORY RULES — RING: '
    '1. Band Geometry and Roundness: Maintain circular shank geometry and continuous smooth metal curvature without oval warping or structural distortion. '
    '2. Gemstone Setting and Table Brilliance: Solitaire and accent stones must exhibit pristine table facets, crown angles, pavilion reflections, and crisp prong/bezel security. '
    '3. Internal Shank Reflections: Render realistic internal band reflection, soft ambient shading on the inner surface, and subtle hallmark legibility where visible. '
    '4. Grounding Shadow: Cast a defined, delicate contact shadow beneath the band base and head to firmly anchor the ring on the presentation surface.',
    1,
    true
)
ON CONFLICT DO NOTHING;

-- Category Prompt Module: Bangle (Version 1)
INSERT INTO public.prompt_modules (module_type, jewellery_type, prompt_text, version, is_active)
VALUES (
    'category',
    'bangle',
    'CATEGORY RULES — BANGLE / BRACELET: '
    '1. Rigid Circular / Oval Symmetry: Maintain rigid structural roundness and uniform wall thickness across the perimeter. '
    '2. Radial Metallic Reflections: Highlights must follow the cylindrical metal contour with smooth sweeping specular gradients. '
    '3. Clasp / Screw Articulation: Render hinges, clasp mechanisms, safety catches, or screw motifs with mechanical precision. '
    '4. Stacking and Inner Cavity: The inner rim must exhibit soft ambient shading and realistic hollow-cavity lighting without distortion.',
    1,
    true
)
ON CONFLICT DO NOTHING;

-- Category Prompt Module: Other (Version 1 - Fallback)
INSERT INTO public.prompt_modules (module_type, jewellery_type, prompt_text, version, is_active)
VALUES (
    'category',
    'other',
    'CATEGORY RULES — GENERAL JEWELLERY FALLBACK: '
    '1. Structural Integrity: Preserve all three-dimensional contours, structural junctions, and mechanical components with exact geometric fidelity. '
    '2. Metal Luster and Gem Fire: Enhance natural surface polish, micro-textures, and optical gemstone dispersion across all visible surfaces. '
    '3. Natural Contact Shadows: Anchor the piece firmly onto the display plane with soft, realistic contact ambient occlusion. '
    '4. Design Authenticity: Strict zero-hallucination policy—do not alter motifs, engravings, stones, or silhouette.',
    1,
    true
)
ON CONFLICT DO NOTHING;

-- 3. ai_generation_logs table (create if not exists, then alter with required columns)
CREATE TABLE IF NOT EXISTS public.ai_generation_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id UUID REFERENCES public.images(id) ON DELETE CASCADE,
    wholesaler_id UUID,
    triggered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'started',
    trigger_source TEXT DEFAULT 'api'
);

-- Add required audit columns
ALTER TABLE public.ai_generation_logs ADD COLUMN IF NOT EXISTS composed_prompt TEXT;
ALTER TABLE public.ai_generation_logs ADD COLUMN IF NOT EXISTS base_module_version INT;
ALTER TABLE public.ai_generation_logs ADD COLUMN IF NOT EXISTS category_module_version INT;
ALTER TABLE public.ai_generation_logs ADD COLUMN IF NOT EXISTS jewellery_type TEXT;

-- Indexes for log querying & auditing
CREATE INDEX IF NOT EXISTS idx_ai_generation_logs_product ON public.ai_generation_logs(product_id);
CREATE INDEX IF NOT EXISTS idx_ai_generation_logs_jewellery_type ON public.ai_generation_logs(jewellery_type);
CREATE INDEX IF NOT EXISTS idx_ai_generation_logs_triggered_at ON public.ai_generation_logs(triggered_at DESC);
