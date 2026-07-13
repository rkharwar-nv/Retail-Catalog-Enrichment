"""Small, versioned taxonomy used by fashion enrichment."""

from typing import Any

TAXONOMY_VERSION = "fashion-product-types/0.1"
ATTRIBUTE_VERSION = "fashion-attributes/0.1"

COMMON_ATTRIBUTES = {"primary_color", "pattern", "composition", "care"}

PRODUCT_ATTRIBUTES = {
    "apparel.dresses": COMMON_ATTRIBUTES | {"neckline", "sleeve_length", "garment_length", "silhouette", "closure"},
    "apparel.skirts": COMMON_ATTRIBUTES | {"garment_length", "silhouette", "closure"},
    "apparel.tops.blouses": COMMON_ATTRIBUTES | {"neckline", "sleeve_length", "silhouette", "closure"},
    "apparel.tops.camisoles": COMMON_ATTRIBUTES | {"neckline", "sleeve_length"},
    "apparel.knitwear.sweaters": COMMON_ATTRIBUTES | {"neckline", "sleeve_length", "garment_length", "silhouette", "closure"},
    "apparel.jumpsuits": COMMON_ATTRIBUTES | {"neckline", "sleeve_length", "garment_length", "silhouette", "closure"},
    "footwear.boots": COMMON_ATTRIBUTES | {"toe_shape", "heel_type", "fastening", "shaft_height"},
    "footwear.sandals": COMMON_ATTRIBUTES | {"toe_shape", "heel_type", "fastening"},
    "footwear.flats": COMMON_ATTRIBUTES | {"toe_shape", "heel_type", "fastening"},
    "footwear.heels": COMMON_ATTRIBUTES | {"toe_shape", "heel_type", "fastening"},
    "footwear.other_shoes": COMMON_ATTRIBUTES | {"toe_shape", "heel_type", "fastening"},
    "bags.tote_bags": COMMON_ATTRIBUTES | {"carry_method", "bag_closure", "structure"},
    "bags.shoulder_bags": COMMON_ATTRIBUTES | {"carry_method", "bag_closure", "structure"},
    "bags.crossbody_bags": COMMON_ATTRIBUTES | {"carry_method", "bag_closure", "structure"},
    "bags.clutches": COMMON_ATTRIBUTES | {"carry_method", "bag_closure", "structure"},
    "bags.satchels": COMMON_ATTRIBUTES | {"carry_method", "bag_closure", "structure"},
    "bags.travel_bags": COMMON_ATTRIBUTES | {"carry_method", "bag_closure", "structure"},
    "bags.other_bags": COMMON_ATTRIBUTES | {"carry_method", "bag_closure", "structure"},
    "eyewear.sunglasses": COMMON_ATTRIBUTES | {"frame_shape", "lens_appearance"},
    "jewelry.bracelets": COMMON_ATTRIBUTES | {"jewelry_form", "metal_color"},
    "jewelry.earrings": COMMON_ATTRIBUTES | {"jewelry_form", "metal_color"},
    "jewelry.necklaces": COMMON_ATTRIBUTES | {"jewelry_form", "metal_color"},
    "jewelry.watches": COMMON_ATTRIBUTES | {"metal_color"},
}

ATTRIBUTE_VALUES = {
    "primary_color": {"black", "white", "gray", "silver", "gold", "brown", "beige", "red", "orange", "yellow", "green", "blue", "navy", "purple", "pink", "multicolor", "other"},
    "pattern": {"solid", "floral", "striped", "checked", "plaid", "polka_dot", "geometric", "abstract", "animal", "paisley", "graphic", "color_block", "other"},
    "neckline": {"crew", "v_neck", "scoop", "square", "boat", "halter", "high_neck", "turtleneck", "cowl", "sweetheart", "off_shoulder", "one_shoulder", "collared", "strapless", "other"},
    "sleeve_length": {"sleeveless", "short", "elbow", "three_quarter", "long", "other"},
    "garment_length": {"cropped", "mini", "knee_length", "midi", "maxi", "full_length", "other"},
    "silhouette": {"a_line", "straight", "fitted", "bodycon", "flared", "column", "fit_and_flare", "boxy", "other"},
    "closure": {"button", "zip", "hook_and_eye", "snap", "tie", "pull_on", "wrap", "open_front", "other"},
    "toe_shape": {"round", "pointed", "almond", "square", "open_toe", "other"},
    "heel_type": {"flat", "block", "stiletto", "kitten", "wedge", "platform", "other"},
    "fastening": {"slip_on", "buckle", "lace_up", "zip", "ankle_strap", "other"},
    "shaft_height": {"ankle", "mid_calf", "knee", "over_knee", "other"},
    "carry_method": {"handheld", "shoulder", "crossbody", "multiple", "other"},
    "bag_closure": {"zip", "magnetic", "snap", "buckle", "drawstring", "flap", "open", "other"},
    "structure": {"structured", "semi_structured", "soft", "basket", "other"},
    "frame_shape": {"aviator", "round", "square", "rectangular", "cat_eye", "oval", "geometric", "shield", "other"},
    "lens_appearance": {"clear", "dark", "gradient", "mirrored", "colored", "other"},
    "jewelry_form": {"chain", "beaded", "cuff", "bangle", "charm", "drop", "hoop", "stud", "pendant", "choker", "strand", "other"},
    "metal_color": {"gold_tone", "silver_tone", "rose_gold_tone", "mixed", "other"},
}

FREE_TEXT_ATTRIBUTES = {"composition", "care"}
STATUSES = {"accepted", "unknown", "not_visible", "not_applicable", "conflicting", "needs_review"}
SOURCES = {"source_structured", "source_text", "image", "image_ocr"}
SOURCE_ALIASES = {
    "visual": "image",
    "vision": "image",
    "text": "source_text",
    "description": "source_text",
    "source": "source_text",
    "structured": "source_structured",
    "ocr": "image_ocr",
}

SOURCE_SUBCATEGORY_PREFIXES = {
    "dress": ("apparel.dresses",),
    "skirt": ("apparel.skirts",),
    "top blouse sweater": ("apparel.tops.", "apparel.knitwear."),
    "shoes": ("footwear.",),
    "bag": ("bags.",),
    "sunglasses": ("eyewear.sunglasses",),
    "bracelet": ("jewelry.bracelets",),
    "earrings": ("jewelry.earrings",),
    "necklace": ("jewelry.necklaces",),
}


def _normalize_sources(sources: Any) -> Any:
    if not isinstance(sources, list):
        return sources
    flattened = [item for source in sources for item in (source if isinstance(source, list) else [source])]
    return [SOURCE_ALIASES.get(str(source).strip().lower(), source) for source in flattened]


def _singular(value: str) -> str:
    if value.endswith("ies"):
        return f"{value[:-3]}y"
    if value.endswith(("sses", "shes", "ches", "xes", "zes")):
        return value[:-2]
    if value.endswith("s") and not value.endswith("ss"):
        return value[:-1]
    return value


def normalize_enrichment(value: dict[str, Any]) -> dict[str, Any]:
    """Apply safe structural normalization before strict validation."""
    if isinstance(value.get("content"), str):
        value["content"] = {"enriched_description": value["content"]}

    product = value.get("product_type")
    if isinstance(product, dict):
        product["sources"] = _normalize_sources(product.get("sources"))
    if isinstance(product, dict) and product.get("value") not in PRODUCT_ATTRIBUTES:
        raw_value = str(product.get("value") or "").strip().lower()
        prefix_matches = [code for code in PRODUCT_ATTRIBUTES if raw_value.startswith(f"{code}.")]
        if prefix_matches:
            product["value"] = max(prefix_matches, key=len)
        leaf = raw_value.rsplit(".", 1)[-1]
        matches = [code for code in PRODUCT_ATTRIBUTES if code.rsplit(".", 1)[-1] == leaf]
        if not matches:
            matches = [code for code in PRODUCT_ATTRIBUTES if _singular(code.rsplit(".", 1)[-1]) == _singular(leaf)]
        if product.get("value") not in PRODUCT_ATTRIBUTES and len(matches) == 1:
            product["value"] = matches[0]

    attributes = value.get("attributes")
    if isinstance(attributes, dict):
        for attribute in attributes.values():
            if isinstance(attribute, dict):
                if attribute.get("status") in {"unknown", "not_visible", "not_applicable"}:
                    attribute["value"] = None
                sources = attribute.get("sources")
                attribute["sources"] = _normalize_sources(sources)
    return value


def validate_enrichment(value: dict[str, Any]) -> list[str]:
    """Return validation errors without mutating model output."""
    errors: list[str] = []
    content = value.get("content")
    if not isinstance(content, dict) or not str(content.get("enriched_description") or "").strip():
        errors.append("content: enriched_description is required")
    product = value.get("product_type")
    product_type = product.get("value") if isinstance(product, dict) else None
    if product_type not in PRODUCT_ATTRIBUTES:
        return ["invalid product_type"]
    if product.get("status") not in STATUSES:
        errors.append("product_type: invalid status")
    if product.get("status") == "accepted" and not product.get("sources"):
        errors.append("product_type: accepted value requires a source")

    attributes = value.get("attributes")
    if attributes is None:
        attributes = {}
    if not isinstance(attributes, dict):
        return errors + ["attributes: must be an object"]
    for name, attribute in attributes.items():
        if name not in PRODUCT_ATTRIBUTES[product_type]:
            errors.append(f"{name}: not applicable to {product_type}")
            continue
        if not isinstance(attribute, dict):
            errors.append(f"{name}: must be an object")
            continue
        status = attribute.get("status")
        if status not in STATUSES:
            errors.append(f"{name}: invalid status")
        sources = attribute.get("sources") or []
        if not isinstance(sources, list) or any(not isinstance(source, str) or source not in SOURCES for source in sources):
            errors.append(f"{name}: invalid source")
        attribute_value = attribute.get("value")
        if status in {"unknown", "not_visible", "not_applicable"} and attribute_value is not None:
            errors.append(f"{name}: {status} value must be null")
        if status == "accepted" and not sources:
            errors.append(f"{name}: accepted value requires a source")
        if name in ATTRIBUTE_VALUES and attribute_value not in ATTRIBUTE_VALUES[name] and attribute_value is not None:
            errors.append(f"{name}: invalid value")
        if name in FREE_TEXT_ATTRIBUTES and sources == ["image"] and attribute_value:
            errors.append(f"{name}: image-only evidence is not allowed")
    return errors


def add_source_category_conflict(value: dict[str, Any], source: dict[str, Any]) -> None:
    """Record a deterministic disagreement with the supplied subcategory."""
    subcategory = str(source.get("subcategory") or "").strip().lower()
    prefixes = SOURCE_SUBCATEGORY_PREFIXES.get(subcategory)
    product = value.get("product_type") or {}
    product_type = str(product.get("value") or "")
    if not prefixes or any(product_type == prefix or product_type.startswith(prefix) for prefix in prefixes):
        return
    conflicts = value.setdefault("conflicts", [])
    if any(item.get("field") == "product_type" for item in conflicts if isinstance(item, dict)):
        return
    conflicts.append({
        "field": "product_type",
        "source_value": subcategory,
        "visual_value": product_type,
        "reason": "Canonical product type differs from the supplied subcategory.",
    })
