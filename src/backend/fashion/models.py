"""Fashion enrichment using the repository's configured Omni endpoint."""

import base64
import json
import os
from typing import Any

from openai import OpenAI

from backend.config import get_config
from backend.fashion.taxonomy import ATTRIBUTE_VALUES, PRODUCT_ATTRIBUTES
from backend.utils import parse_llm_json
from backend.vlm import LOCALE_CONFIG, NGC_API_KEY_NOT_SET_ERROR


def _api_key() -> str:
    key = os.getenv("NGC_API_KEY")
    if not key:
        raise RuntimeError(NGC_API_KEY_NOT_SET_ERROR)
    return key


def enrich_with_omni(
    source: dict[str, Any],
    image_bytes: bytes,
    content_type: str,
    locale: str,
) -> dict[str, Any]:
    """Reason over source text and image together, returning canonical JSON."""
    config = get_config().get_vlm_config()
    info = LOCALE_CONFIG.get(locale, LOCALE_CONFIG["en-US"])
    client = OpenAI(base_url=config["url"], api_key=_api_key())
    prompt = f"""Analyze the sold fashion product using the image and source row together.

SOURCE ROW:
{json.dumps(source, ensure_ascii=False)}

ALLOWED PRODUCT TYPES AND ATTRIBUTES:
{json.dumps({key: sorted(value) for key, value in PRODUCT_ATTRIBUTES.items()})}

CONTROLLED ATTRIBUTE VALUES:
{json.dumps({key: sorted(value) for key, value in ATTRIBUTE_VALUES.items()})}

RULES:
- Return exactly one allowed product_type, or status needs_review when identity is unresolved.
- The image is authoritative for visible product type, color, pattern, shape, construction, and closures. Visible functional form/components determine product type; do not let a broad supplied subcategory override them.
- Source text is authoritative for exact composition, care, dimensions, and other nonvisual supplied facts.
- Absence from the image is not a contradiction for a nonvisual supplied fact.
- Report source/image disagreements in conflicts; do not silently choose source marketing copy over clear image evidence.
- Report an attribute conflict whenever source text and visible evidence disagree, even after choosing the visually supported value.
- Garment length is visible only when the hem and enough body context are shown; otherwise return null with status not_visible.
- Use only applicable attributes and controlled values. composition and care may be free text.
- Sources are source_structured, source_text, image, or image_ocr.
- Status is accepted, unknown, not_visible, not_applicable, conflicting, or needs_review.
- An unknown/not_visible value must be null and must not contain invented source evidence.
- Use unknown, not not_visible, when a nonvisual fact such as care is absent from source text.
- Never infer composition, care, dimensions, size, price, availability, audience, performance, or genuine precious materials from appearance.
- Visible color or finish does not contradict supplied material composition; a coated or colored material may look different.
- For genuine material claims, an explicit composition statement in the description outranks promotional material words in the product name. Flag the unsupported name claim and omit it from grounded content.
- unsupported_claims is only for unsupported objective claims such as composition, care, dimensions, performance, or genuine precious materials. Do not flag subjective styling, occasion, versatility, or mood language.
- Do not create occasion, formality, aesthetic, mood, or trend fields.
- Write one natural, standalone enriched_description in {info['language']} for {info['region']}. Combine useful visible details with trustworthy source facts so this single field is suitable for semantic search. Do not write a keyword list.

Return one JSON object only with:
- product_type: value, confidence (0-1), status, sources
- attributes: a JSON OBJECT keyed by exact allowed attribute names; each value is an object with value, confidence (0-1), status, sources. Never return attributes as an array.
- conflicts: array of field, source_value, visual_value, reason
- unsupported_claims: array of strings
- content: enriched_description
No markdown or additional keys."""
    response = client.chat.completions.create(
        model=config["model"],
        messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{base64.b64encode(image_bytes).decode()}"}},
            {"type": "text", "text": prompt},
        ]}],
        temperature=0.0,
        top_p=1,
        max_tokens=8192,
        stream=False,
        response_format={"type": "json_object"},
    )
    text = response.choices[0].message.content or ""
    parsed = parse_llm_json(text, extract_braces=True, strip_comments=True)
    if not isinstance(parsed, dict):
        raise ValueError("Fashion Omni enrichment returned invalid JSON")
    return parsed
