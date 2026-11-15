"""app/services/catalogue_import/synonyms.py

General synonym dictionary and normalization rules for jewellery catalogue import.
Covers English shorthand, Hindi, and Hinglish (romanized) vocabulary.
"""

from __future__ import annotations

import re
from typing import Optional

# Canonical options
CANONICAL_JEWELLERY_TYPES = [
    "necklace", "chain", "rings", "earrings", "haram",
    "pendant", "bangles", "nosepins", "mangalsutra"
]

CANONICAL_CATEGORIES = [
    "gold", "silver", "diamond", "platinum", "gemstone", "pearl"
]

CANONICAL_STYLES = [
    "traditional", "modern", "fusion", "antique", "minimalist", "bridal"
]

CANONICAL_PURITIES = [
    "24k", "22k", "18k", "14k", "925", "950pt"
]

CANONICAL_SIZES = [
    "xs", "s", "m", "l", "xl", "freesize"
]

# Header normalization: normalize header text (strip punctuation, lower, collapse spaces)
def normalize_header_text(header: str) -> str:
    h = header.strip().lower()
    # Replace dots and underscores with spaces
    h = re.sub(r"[\._\-\(\)\[\]/]", " ", h)
    h = re.sub(r"\s+", " ", h).strip()
    return h


# Header synonyms dictionary: target_field -> list of synonym phrases
HEADER_SYNONYMS: dict[str, list[str]] = {
    "product_code": [
        "design no", "design no.", "design code", "item code", "code", "item no",
        "design", "model no", "model", "style no", "product code", "designcode",
        "itemcode", "sku", "tag no", "barcode"
    ],
    "title": [
        "item name", "product name", "title", "name", "description", "desc",
        "vivaran", "विवरण", "design name", "item description"
    ],
    "jewellery_type": [
        "item", "item type", "jewellery type", "jewelry type", "type", "prakar",
        "प्रकार", "jewel type", "category type", "product type"
    ],
    "category": [
        "metal", "material", "dhatu", "धातु", "metal type", "dharu", "material type",
        "gold silver", "metal category"
    ],
    "metal_purity": [
        "kt", "purity", "karat", "carat", "shuddhata", "शुद्धता", "touch",
        "metal purity", "gold purity", "kdm"
    ],
    "gross_weight": [
        "gross", "gross wt", "gross weight", "g wt", "g.wt", "g wt gm", "g.wt gm",
        "gr wt", "gr.wt", "gwt", "kul vajan", "kul vajan gm", "kul vajan gms",
        "कुल वजन", "kul wajan", "gross weight gm", "gross weight gms", "gross wt gm",
        "gr wt gm", "g wt (gm)", "g.wt (gm)"
    ],
    "stone_weight": [
        "stone", "stone wt", "stone weight", "st wt", "st.wt", "st wt gm", "st.wt gm",
        "nag vajan", "nag", "नग", "stwt", "stone wt gm", "stone weight gm",
        "stone wajan", "st wt (gm)", "st.wt (gm)", "diamond wt", "dia wt"
    ],
    "net_weight": [
        "net", "net wt", "net weight", "n wt", "n.wt", "n wt gm", "n.wt gm",
        "shuddh vajan", "shuddh", "शुद्ध वजन", "nwt", "net wt gm", "net weight gm",
        "net wajan", "n wt (gm)", "n.wt (gm)"
    ],
    "size": [
        "size", "sizes", "map", "naap", "maat", "dimensions", "ring size", "bangle size"
    ],
    "style": [
        "style", "style aesthetic", "design style", "look", "aesthetic"
    ],
    "stock_available": [
        "ready order", "ready/order", "stock", "ready stock", "availability",
        "stock available", "status", "in stock", "ready or order"
    ],
    "make_to_order_days": [
        "days", "banwai din", "lead time", "mto days", "making days", "delivery days",
        "banwaai din", "making time", "order days"
    ],
    "photo": [
        "photo", "photo link", "image", "image link", "photo url", "pic", "picture",
        "photo ref", "tasveer", "फोटो", "image url", "photos"
    ],
    "ignore": [
        "sr no", "sr no.", "serial no", "serial number", "s no", "s.no", "index",
        "sr.", "s.no."
    ]
}

# Ambiguous headers: must NOT be silently auto-mapped
AMBIGUOUS_HEADERS = {
    "wt": "ambiguous_weight",     # could be gross or net
    "weight": "ambiguous_weight", # could be gross or net (unless duplicate disambiguated)
    "rate": "ambiguous_rate",     # could be price or ignore
    "qty": "ambiguous_qty",       # quantity vs stock status
    "quantity": "ambiguous_qty",
    "price": "ambiguous_rate",
}

# Values synonym dictionary: field -> canonical_value -> list of synonyms
VALUE_SYNONYMS: dict[str, dict[str, list[str]]] = {
    "jewellery_type": {
        "rings": [
            "rg", "ring", "rings", "anguthi", "अंगूठी", "anguthiyan", "band",
            "finger ring", "gents ring", "ladies ring", "ring ladies", "ring gents",
            "couple ring", "cocktail ring"
        ],
        "necklace": [
            "nkl", "necklace", "necklaces", "neckless", "neck", "haar", "हार", "choker",
            "collar", "kanthi", "short necklace", "guluband", "necklace set"
        ],
        "chain": [
            "chn", "chain", "chains", "neck chain", "neck chains", "zanjeer", "जंजीर",
            "zanjir", "kadi chain", "gold chain", "silver chain", "gents chain", "ladies chain"
        ],
        "earrings": [
            "er", "earrings", "earring", "jhumka", "jhumki", "bali", "tops", "studs",
            "drop", "झुमका", "बाली", "kaan ki bali", "ear ring", "ear rings",
            "sui dhaga", "chaandbali", "hoops"
        ],
        "haram": [
            "hrm", "haram", "rani haar", "ranihaar", "long haram", "रानी हार",
            "bridal haram", "sita haar", "long necklace", "laxmi haram", "temple haram"
        ],
        "pendant": [
            "pdt", "pendant", "pendants", "locket", "pend", "लॉकेट", "pendant set",
            "lockets", "om locket", "god pendant", "heart pendant"
        ],
        "bangles": [
            "bng", "bangles", "bangle", "kangan", "chudi", "choodi", "kada", "कंगन",
            "चूड़ी", "कड़ा", "bracelet", "bracelets", "patla", "chuda", "pair bangles"
        ],
        "nosepins": [
            "np", "nosepins", "nosepin", "nose pin", "nath", "laung", "नथ", "लौंग",
            "nose ring", "nose stud", "mookuthi"
        ],
        "mangalsutra": [
            "ms", "mangalsutra", "mangal sutra", "tanmaniya", "मंगलसूत्र", "mangalsutra chain",
            "black beads chain", "short mangalsutra", "long mangalsutra"
        ],
    },
    "category": {
        "gold": [
            "g", "gold", "sona", "सोना", "yellow gold", "yg", "rose gold",
            "white gold", "wg", "swarna", "kundan gold"
        ],
        "silver": [
            "slv", "silver", "chandi", "चांदी", "sil", "chandi 925", "rupya"
        ],
        "diamond": [
            "dia", "diamond", "dmd", "heera", "हीरा", "diamonds", "real diamond",
            "cvd", "lab grown", "uncut diamond", "polki"
        ],
        "platinum": [
            "platinum", "pt", "plat", "pt950", "pt 950", "pltn"
        ],
        "gemstone": [
            "gemstone", "gem", "stone", "ratna", "navratna", "emerald", "ruby",
            "sapphire", "panna", "manik", "pukhraj", "neelam"
        ],
        "pearl": [
            "pearl", "moti", "मोती", "pearls", "basra pearl", "culture pearl"
        ],
    },
    "metal_purity": {
        "24k": [
            "24k", "24kt", "24", "999", "99.9", "24 karat", "24 carat", "pure gold"
        ],
        "22k": [
            "22k", "22kt", "22", "916", "91.6", "22 karat", "22 carat",
            "916 kdm", "hallmarked 916", "22k 916"
        ],
        "18k": [
            "18k", "18kt", "18", "750", "75.0", "18 karat", "18 carat", "18k 750"
        ],
        "14k": [
            "14k", "14kt", "14", "585", "58.5", "14 karat", "14 carat"
        ],
        "925": [
            "925", "92.5", "sterling", "925 silver", "sterling silver",
            "silver 925", "925 slv"
        ],
        "950pt": [
            "950pt", "pt950", "pt 950", "950 platinum", "950", "platinum 950"
        ],
    },
    "stock_available": {
        "true": [
            "ready", "r", "stock", "in stock", "haan", "ha", "yes", "y", "1",
            "✓", "available", "ready stock", "true", "instock"
        ],
        "false": [
            "order", "o", "mto", "nahi", "no", "n", "0", "make to order",
            "false", "order only", "out of stock", "on order"
        ],
    },
    "style": {
        "traditional": [
            "traditional", "paramparik", "riwayati", "heritage", "classic", "temple"
        ],
        "modern": [
            "modern", "contemporary", "adhunik", "western", "trendy"
        ],
        "fusion": [
            "fusion", "indo-western", "indowestern"
        ],
        "antique": [
            "antique", "vintage", "prachin", "oxidized", "jadau"
        ],
        "minimalist": [
            "minimalist", "minimal", "simple", "daily", "daily wear", "sleek"
        ],
        "bridal": [
            "bridal", "dulhan", "shaadi", "wedding", "heavy bridal"
        ],
    },
    "size": {
        "freesize": [
            "free", "free size", "fs", "freesize", "adjustable", "open size", "standard"
        ],
        "xs": ["xs", "extra small"],
        "s": ["s", "small"],
        "m": ["m", "medium", "med"],
        "l": ["l", "large"],
        "xl": ["xl", "extra large"],
    }
}


def match_header_synonym(raw_header: str) -> tuple[Optional[str], float]:
    """
    Matches raw header to a canonical field.
    Returns (target_field, confidence).
    If ambiguous (e.g. 'wt', 'rate'), returns (ambiguous_key, 0.5).
    """
    h_clean = normalize_header_text(raw_header)
    if not h_clean:
        return None, 0.0

    # Check ambiguous list first
    if h_clean in AMBIGUOUS_HEADERS:
        return AMBIGUOUS_HEADERS[h_clean], 0.50

    # Exact matches in synonym lists
    for target, syns in HEADER_SYNONYMS.items():
        for syn in syns:
            if h_clean == normalize_header_text(syn):
                return target, 0.95

    # Substring / word boundary matches
    for target, syns in HEADER_SYNONYMS.items():
        for syn in syns:
            syn_norm = normalize_header_text(syn)
            # e.g. "g.wt (gm)" or "item code no"
            if syn_norm in h_clean or h_clean in syn_norm:
                # Avoid false match on single small words like "g" or "kt"
                if len(syn_norm) >= 3 or len(h_clean) >= 3:
                    return target, 0.88

    return None, 0.0


def match_value_synonym(field_name: str, raw_value: str) -> tuple[Optional[str], float]:
    """
    Matches a raw cell value to a canonical enum value for a specific field.
    Returns (canonical_value, confidence).
    """
    if not raw_value:
        return None, 0.0
    val_clean = raw_value.strip().lower()

    field_dict = VALUE_SYNONYMS.get(field_name)
    if not field_dict:
        return None, 0.0

    # Exact match in synonyms
    for canon, syns in field_dict.items():
        if val_clean == canon:
            return canon, 1.0
        for syn in syns:
            if val_clean == syn.lower():
                return canon, 0.95

    # Check numeric purities like '916' -> '22k'
    if field_name == "metal_purity":
        digits = re.sub(r"[^\d]", "", val_clean)
        if digits in ("916", "22"):
            return "22k", 0.95
        if digits in ("750", "18"):
            return "18k", 0.95
        if digits in ("999", "24"):
            return "24k", 0.95
        if digits in ("585", "14"):
            return "14k", 0.95
        if digits == "925":
            return "925", 0.95
        if digits == "950":
            return "950pt", 0.95

    # Check multi-word phrases and words within phrase with word boundaries
    # (e.g. "silver nose pin" -> jewellery_type: "nosepins", "22k gold ladies ring" -> "rings")
    for canon, syns in field_dict.items():
        for syn in sorted(syns, key=len, reverse=True):
            if len(syn) >= 2:
                pattern = r"\b" + re.escape(syn.lower()) + r"\b"
                if re.search(pattern, val_clean):
                    return canon, 0.88

    return None, 0.0
