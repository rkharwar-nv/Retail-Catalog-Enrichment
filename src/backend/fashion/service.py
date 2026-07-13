"""Fashion enrichment orchestration for one product."""

from typing import Any

from backend.fashion.models import enrich_with_omni
from backend.fashion.taxonomy import add_source_category_conflict, normalize_enrichment, validate_enrichment


def enrich_product(source: dict[str, Any], image_bytes: bytes, content_type: str, locale: str = "en-US") -> dict[str, Any]:
    """Run multimodal fashion enrichment and enforce its taxonomy."""
    errors: list[str] = []
    last_error: ValueError | None = None
    for _ in range(3):
        try:
            result = normalize_enrichment(enrich_with_omni(source, image_bytes, content_type, locale))
        except ValueError as exc:
            last_error = exc
            continue
        errors = validate_enrichment(result)
        if not errors:
            break
    else:
        if last_error and not errors:
            raise last_error
        raise ValueError("Invalid fashion enrichment: " + "; ".join(errors))
    add_source_category_conflict(result, source)
    return result
