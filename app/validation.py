"""
Input validation and sanitization for the AI pipeline.

Provides Pydantic models and utility functions to:
1. Validate/sanitize user text inputs (title, jewellery_type)
2. Prevent prompt injection attacks
3. Validate UUIDs for product/image IDs
4. Block potentially dangerous characters and patterns
"""
from __future__ import annotations
import html
import re
from typing import Annotated, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# === SECURITY CONSTANTS ===

# Maximum lengths for text fields (prevents DoS via huge payloads)
MAX_TITLE_LENGTH = 200
MAX_JEWELLERY_TYPE_LENGTH = 100

# Patterns that could indicate prompt injection attempts
# These cover common LLM manipulation techniques
PROMPT_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|prompts?|context)",
    r"disregard\s+(all\s+)?(previous|above|prior)",
    r"forget\s+(everything|all|previous)",
    r"new\s+instructions?:",
    r"system\s*:",
    r"assistant\s*:",
    r"user\s*:",
    r"\[INST\]",
    r"\[/INST\]",
    r"<<SYS>>",
    r"<\|im_start\|>",
    r"<\|im_end\|>",
    r"<\|system\|>",
    r"<\|user\|>",
    r"<\|assistant\|>",
    r"</s>",
    r"<s>",
    r"###\s*(instruction|system|human|assistant)",
    r"you\s+are\s+now\s+(in\s+)?(a\s+)?new\s+mode",
    r"act\s+as\s+(if\s+)?you\s+(are|were)",
    r"pretend\s+(to\s+be|you\s+are)",
    r"roleplay\s+as",
    r"jailbreak",
    r"DAN\s*mode",
    r"developer\s*mode",
    r"ignore\s+safety",
    r"bypass\s+(filter|safety|restriction)",
]

# Characters that should be escaped or removed from user inputs
# to prevent injection into prompts or path traversal
DANGEROUS_CHARS_PATTERN = re.compile(r'[<>{}|\[\]\\`\x00-\x1f\x7f]')

# Path traversal patterns
PATH_TRAVERSAL_PATTERN = re.compile(r'\.{2,}[/\\]|[/\\]\.{2,}|^\.{2,}$')


def _compile_injection_patterns() -> re.Pattern:
    """Compile all injection patterns into a single regex for efficiency."""
    combined = "|".join(f"({p})" for p in PROMPT_INJECTION_PATTERNS)
    return re.compile(combined, re.IGNORECASE)


INJECTION_DETECTOR = _compile_injection_patterns()


class ValidationError(ValueError):
    """Raised when input validation fails."""
    pass


def sanitize_text(
    value: str,
    *,
    max_length: int,
    field_name: str = "input",
    allow_empty: bool = True,
) -> str:
    """
    Sanitize a text input string.

    1. Strip leading/trailing whitespace
    2. Collapse multiple spaces into one
    3. Remove dangerous characters
    4. Escape HTML entities
    5. Check for prompt injection patterns
    6. Enforce length limit

    Args:
        value: The raw input string
        max_length: Maximum allowed length after sanitization
        field_name: Name of the field (for error messages)
        allow_empty: Whether empty string is valid

    Returns:
        Sanitized string

    Raises:
        ValidationError: If validation fails
    """
    if value is None:
        if allow_empty:
            return ""
        raise ValidationError(f"{field_name} is required")

    # Step 1: Strip and normalize whitespace
    cleaned = value.strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)

    # Step 2: Check if empty is allowed
    if not cleaned:
        if allow_empty:
            return ""
        raise ValidationError(f"{field_name} cannot be empty")

    # Step 3: Check for path traversal attempts
    if PATH_TRAVERSAL_PATTERN.search(cleaned):
        raise ValidationError(f"{field_name} contains invalid path characters")

    # Step 4: Check for prompt injection patterns BEFORE HTML-escaping.
    # Patterns like <|im_start|> contain angle brackets that html.escape() turns
    # into &lt; and &gt;, which would no longer match the compiled regex.
    if INJECTION_DETECTOR.search(cleaned):
        raise ValidationError(
            f"{field_name} contains disallowed content patterns"
        )

    # Step 5: Remove dangerous characters
    cleaned = DANGEROUS_CHARS_PATTERN.sub('', cleaned)

    # Step 6: HTML-escape to prevent XSS if stored/displayed
    cleaned = html.escape(cleaned, quote=True)

    # Step 7: Enforce length limit (after processing)
    if len(cleaned) > max_length:
        raise ValidationError(
            f"{field_name} exceeds maximum length of {max_length} characters"
        )

    return cleaned


def validate_uuid(value: str, field_name: str = "id") -> str:
    """
    Validate that a string is a valid UUID.

    Args:
        value: The string to validate
        field_name: Name of the field (for error messages)

    Returns:
        The validated UUID string (lowercase, canonical format)

    Raises:
        ValidationError: If the value is not a valid UUID
    """
    if not value:
        raise ValidationError(f"{field_name} is required")

    try:
        # Parse first to validate the UUID is well-formed.
        # We then explicitly check .version because UUID(value, version=4) does NOT
        # reject other versions — it silently overwrites the version attribute.
        parsed = UUID(value)
        if parsed.version != 4:
            raise ValidationError(f"{field_name} must be a valid UUID v4")
        return str(parsed)
    except (ValueError, AttributeError):
        raise ValidationError(f"{field_name} must be a valid UUID")


# === PYDANTIC MODELS ===

class ProductCreate(BaseModel):
    """Validated input for creating a new product."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: Annotated[
        str,
        Field(
            default="Untitled",
            max_length=MAX_TITLE_LENGTH,
            description="Product title",
        ),
    ]
    jewellery_type: Annotated[
        str,
        Field(
            ...,
            min_length=1,
            max_length=MAX_JEWELLERY_TYPE_LENGTH,
            description="Type of jewellery (e.g. jhumka, mangalsutra, necklace, ring, bangle, other)",
        ),
    ]

    @field_validator("title", mode="before")
    @classmethod
    def validate_title(cls, v: Optional[str]) -> str:
        if v is None:
            return "Untitled"
        return sanitize_text(
            v,
            max_length=MAX_TITLE_LENGTH,
            field_name="title",
            allow_empty=True,
        ) or "Untitled"

    @field_validator("jewellery_type", mode="before")
    @classmethod
    def validate_jewellery_type_field(cls, v: Optional[str]) -> str:
        if not v or not str(v).strip():
            raise ValidationError("jewellery_type is required")
        sanitized = sanitize_text(
            str(v),
            max_length=MAX_JEWELLERY_TYPE_LENGTH,
            field_name="jewellery_type",
            allow_empty=False,
        )
        return sanitized.lower()


class ProductId(BaseModel):
    """Validated product/image ID (UUID format)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    id: Annotated[
        str,
        Field(
            description="Product UUID",
            pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$",
        ),
    ]

    @field_validator("id", mode="before")
    @classmethod
    def validate_id(cls, v: str) -> str:
        return validate_uuid(v, "product_id")


class ChamakGenerationRequest(BaseModel):
    """Validated Chamak generation request (UUID format)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    generation_id: Annotated[
        str,
        Field(
            description="Chamak generation UUID",
            pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$",
        ),
    ]

    @field_validator("generation_id", mode="before")
    @classmethod
    def validate_gen_id(cls, v: str) -> str:
        return validate_uuid(v, "generation_id")


async def validate_jewellery_type_dynamic(jewellery_type: str) -> str:
    """
    Validate jewellery_type against active prompt_modules in database/cache.

    This ensures categories can be added in DB without redeploying code.
    """
    from app.services.prompt_composer import prompt_composer

    if not jewellery_type or not str(jewellery_type).strip():
        raise ValidationError("jewellery_type is required")

    sanitized = sanitize_text(
        str(jewellery_type),
        max_length=MAX_JEWELLERY_TYPE_LENGTH,
        field_name="jewellery_type",
        allow_empty=False,
    ).lower()

    valid_types = await prompt_composer.get_valid_jewellery_types()
    if sanitized not in valid_types:
        formatted_list = ", ".join(sorted(valid_types))
        raise ValidationError(
            f"Invalid jewellery_type '{sanitized}'. Must be one of: {formatted_list}"
        )

    return sanitized


def validate_product_input(
    title: Optional[str] = None,
    jewellery_type: Optional[str] = None,
) -> ProductCreate:
    """
    Convenience function to validate product creation inputs.

    Args:
        title: Raw title string
        jewellery_type: Raw jewellery type string

    Returns:
        Validated ProductCreate model

    Raises:
        ValidationError: If validation fails
    """
    if not jewellery_type or not str(jewellery_type).strip():
        raise ValidationError("jewellery_type is required")

    return ProductCreate(
        title=title if title else "Untitled",
        jewellery_type=jewellery_type,
    )


def validate_product_id(product_id: str) -> str:
    """
    Validate and normalize a product ID.

    Args:
        product_id: Raw product ID string

    Returns:
        Normalized UUID string

    Raises:
        ValidationError: If not a valid UUID
    """
    return validate_uuid(product_id, "product_id")


def is_safe_for_prompt(text: str) -> bool:
    """
    Check if text is safe to include in an LLM prompt.

    This is a quick check for use in prompt construction.
    Returns False if the text contains injection patterns.

    Args:
        text: The text to check

    Returns:
        True if safe, False if potentially dangerous
    """
    if not text:
        return True
    return not bool(INJECTION_DETECTOR.search(text))


def sanitize_for_prompt(text: str, max_length: int = 500) -> str:
    """
    Sanitize text for safe inclusion in an LLM prompt.

    More aggressive than general sanitization:
    - Removes all non-alphanumeric except basic punctuation
    - Truncates to max_length

    Args:
        text: Raw text to sanitize
        max_length: Maximum length

    Returns:
        Sanitized text safe for prompt inclusion
    """
    if not text:
        return ""

    # Reject injection patterns before stripping special characters.
    # The character filter below would remove angle brackets and similar tokens,
    # which could silently swallow a payload instead of rejecting it.
    if INJECTION_DETECTOR.search(text):
        raise ValidationError("Text contains disallowed content patterns")

    # Keep only safe characters: letters, numbers, spaces, basic punctuation
    safe = re.sub(r'[^a-zA-Z0-9\s\.,!?\-\'\"():;]', '', text)

    # Normalize whitespace
    safe = re.sub(r'\s+', ' ', safe).strip()

    # Truncate
    if len(safe) > max_length:
        safe = safe[:max_length].rsplit(' ', 1)[0] + "..."

    return safe
