"""Command-line batch enrichment for fashion catalogs."""

import argparse
import csv
import hashlib
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.fashion.audit import audit_row
from backend.fashion.service import enrich_product
from backend.fashion.taxonomy import ATTRIBUTE_VERSION, TAXONOMY_VERSION

logger = logging.getLogger("catalog_enrichment.fashion.batch")

ELIMINATION_EXPLANATIONS = {
    "DUPLICATE_NAME_IMAGE": "Cause: ambiguous product identity. Multiple input rows use the same product name and image but do not provide stable source IDs. The workflow cannot determine whether they are duplicates, variants, or separate products, so none of the ambiguous rows is published.",
    "IMAGE_NOT_FOUND": "Cause: missing visual evidence. The referenced image was not found, so visual enrichment could not verify the product classification or ground the enriched description.",
    "IMAGE_UNREADABLE": "Cause: unusable visual evidence. The referenced image file could not be decoded, so visual analysis could not be completed.",
    "MISSING_REQUIRED_FIELD": "Cause: incomplete input data. The input row is missing a required product name or description, so a usable enriched catalog record cannot be created.",
    "INVALID_PRICE": "Cause: invalid input data. The input price is missing, negative, or not numeric, so the record fails the publication contract.",
    "MODEL_ENRICHMENT_FAILED": "Cause: model-output validation failure. After three attempts, the model output still failed schema, taxonomy, value, applicability, or evidence validation. This does not by itself mean that the input text conflicts with the image.",
    "UNRESOLVED_PRODUCT_CLASSIFICATION": "Cause: input-text-versus-image conflict. The input text or structured data and visual analysis identify different product types. Publishing either classification without review would create an unverified catalog identity.",
    "UNRESOLVED_EVIDENCE_CONFLICT": "Cause: input-text-versus-image conflict. The input text or structured data and visual analysis disagree on at least one product attribute. The disputed fact may affect filters or the enriched description, so the product is not published.",
    "ENRICHMENT_NOT_AVAILABLE": "Cause: incomplete enrichment. The workflow could not produce a complete, internally consistent, publication-ready record.",
}


def _has_source_id(row: dict[str, Any]) -> bool:
    return any(str(row.get(key) or "").strip() for key in ("product_id", "sku", "id"))


def _record_id(csv_path: Path, row_number: int, row: dict[str, Any]) -> str:
    for key in ("product_id", "sku", "id"):
        if str(row.get(key) or "").strip():
            return str(row[key]).strip()
    identity = {key: str(row.get(key) or "").strip().lower() for key in ("name", "image", "url")}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    return f"generated:{digest}"


def _duplicate_key(row: dict[str, Any]) -> tuple[str, str]:
    name = str(row.get("name") or "").strip().casefold()
    image = Path(str(row.get("image") or "")).name.casefold()
    return name, image


def _elimination_explanations(reasons: list[str], result: dict[str, Any] | None = None, detail: str = "") -> list[str]:
    explanations: list[str] = []
    has_conflict_detail = False
    if result:
        for conflict in result.get("conflicts") or []:
            if isinstance(conflict, dict) and conflict.get("reason"):
                has_conflict_detail = True
                field = "category/subcategory" if conflict.get("field") == "product_type" else conflict.get("field")
                source_value = conflict.get("source_value")
                visual_value = conflict.get("visual_value")
                comparison = ""
                if source_value not in (None, "") and visual_value not in (None, ""):
                    comparison = f" Input text/structured data says {source_value!r}; visual analysis says {visual_value!r}."
                explanations.append(
                    f"Cause: input-text-versus-image conflict for {field!r}.{comparison} "
                    f"Evidence detail: {conflict['reason']} The product was not published because choosing either "
                    "value without review could make its taxonomy, filters, or enriched description incorrect."
                )
    for reason in reasons:
        if has_conflict_detail and reason in {"UNRESOLVED_PRODUCT_CLASSIFICATION", "UNRESOLVED_EVIDENCE_CONFLICT"}:
            continue
        explanation = ELIMINATION_EXPLANATIONS.get(reason, reason)
        if explanation not in explanations:
            explanations.append(explanation)
    if detail:
        explanations.append(f"Validation detail: {detail}")
    return explanations


def _input_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _classification(product_type: str) -> tuple[str, str]:
    parts = product_type.split(".")
    return parts[0], parts[-1]


def _sources(value: dict[str, Any]) -> str:
    return "+".join(value.get("sources") or [])


def _review_status(value: dict[str, Any]) -> str:
    status = value.get("status")
    if status == "accepted":
        return "accepted"
    if status in {"conflicting", "needs_review"}:
        return "review"
    return "unknown"


def _review_rows(record_id: str, row_number: int, source: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    conflicts = result.get("conflicts") or []
    conflict_details = {item.get("field"): item for item in conflicts if isinstance(item, dict)}
    product_type = result["product_type"]
    category, subcategory = _classification(product_type["value"])
    rows = [{
        "record_id": record_id,
        "source_row": row_number,
        "product_name": source.get("name", ""),
        "field": "category/subcategory",
        "original_value": f"{source.get('category', '')} / {source.get('subcategory', '')}",
        "enriched_value": f"{category} / {subcategory}",
        "confidence": product_type.get("confidence", ""),
        "provenance": _sources(product_type),
        "status": "review" if "product_type" in conflict_details else _review_status(product_type),
        "attention_reason": (conflict_details.get("product_type") or {}).get("reason", ""),
    }]
    for field, value in (result.get("attributes") or {}).items():
        if not isinstance(value, dict):
            continue
        rows.append({
            "record_id": record_id,
            "source_row": row_number,
            "product_name": source.get("name", ""),
            "field": field,
            "original_value": (conflict_details.get(field) or {}).get("source_value", ""),
            "enriched_value": (conflict_details.get(field) or {}).get("visual_value", value.get("value") if value.get("value") is not None else ""),
            "confidence": value.get("confidence", ""),
            "provenance": _sources(value),
            "status": "review" if field in conflict_details else _review_status(value),
            "attention_reason": (conflict_details.get(field) or {}).get("reason", ""),
        })
    for claim in result.get("unsupported_claims") or []:
        rows.append({
            "record_id": record_id, "source_row": row_number, "product_name": source.get("name", ""),
            "field": "unsupported_claim", "original_value": claim, "enriched_value": "", "confidence": "",
            "provenance": "source_text", "status": "review", "attention_reason": "Claim was not supported by the available evidence.",
        })
    return rows


def _catalog_record(record_id: str, row_number: int, source: dict[str, Any], result: dict[str, Any], currency: str | None) -> dict[str, Any]:
    product_type = result["product_type"]["value"]
    category, subcategory = _classification(product_type)
    record: dict[str, Any] = {
        **source,
        "record_id": record_id,
        "source_row": row_number,
        "category": category,
        "subcategory": subcategory,
    }
    if currency:
        record["currency"] = currency
    conflicted_fields = {item.get("field") for item in result.get("conflicts") or [] if isinstance(item, dict)}
    for field, value in (result.get("attributes") or {}).items():
        if field not in conflicted_fields and isinstance(value, dict) and value.get("status") == "accepted" and value.get("value") is not None:
            record[field] = value["value"]
    enriched_description = (result.get("content") or {}).get("enriched_description")
    if enriched_description:
        record["enriched_description"] = enriched_description
    return record


def run_batch(
    input_csv: Path,
    images_dir: Path,
    output_dir: Path,
    *,
    locale: str = "en-US",
    currency: str | None = None,
    validate_only: bool = False,
) -> dict[str, Any]:
    """Validate and optionally enrich every CSV row."""
    output_dir.mkdir(parents=True, exist_ok=True)
    with input_csv.open(newline="", encoding="utf-8-sig") as handle:
        source_rows = list(csv.DictReader(handle))
    duplicate_counts = Counter(_duplicate_key(row) for row in source_rows if not _has_source_id(row))

    disposition_rows: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    catalog_records: list[dict[str, Any]] = []
    eliminated_records: list[dict[str, Any]] = []

    for row_number, source in enumerate(source_rows, start=2):
        record_id = _record_id(input_csv, row_number, source)
        audit = audit_row(source, images_dir)
        disposition = audit.disposition
        duplicate = not _has_source_id(source) and duplicate_counts[_duplicate_key(source)] > 1

        result = None
        failure_reason = ""
        if duplicate:
            disposition = "REVIEW"
            review_rows.append({
                "record_id": record_id, "source_row": row_number, "product_name": source.get("name", ""),
                "field": "identity", "original_value": source.get("image", ""), "enriched_value": "", "confidence": "",
                "provenance": "source_structured", "status": "review",
                "attention_reason": "DUPLICATE_NAME_IMAGE: multiple rows share the same product name and image without a stable source identifier.",
            })
        elif not validate_only and audit.disposition != "FAIL" and audit.image_path and audit.content_type:
            try:
                result = enrich_product(source, audit.image_path.read_bytes(), audit.content_type, locale)
                if result.get("conflicts") or result.get("unsupported_claims"):
                    disposition = "REVIEW"
            except Exception as exc:
                logger.exception("Fashion enrichment failed for %s", record_id)
                disposition = "FAIL"
                failure_reason = str(exc)
                review_rows.append({
                    "record_id": record_id, "source_row": row_number, "product_name": source.get("name", ""),
                    "field": "processing", "original_value": "", "enriched_value": "", "confidence": "",
                    "provenance": "", "status": "failed", "attention_reason": str(exc),
                })

        has_conflict = bool(result and result.get("conflicts"))
        product_type_conflict = bool(result and any(
            isinstance(item, dict) and item.get("field") == "product_type"
            for item in result.get("conflicts") or []
        ))
        product_type_unresolved = bool(result and (result.get("product_type") or {}).get("status") != "accepted")

        if result is not None:
            review_rows.extend(_review_rows(record_id, row_number, source, result))
            if not validate_only and not has_conflict and not product_type_unresolved:
                catalog_records.append(_catalog_record(record_id, row_number, source, result, currency))
            elif not validate_only:
                reasons = ["UNRESOLVED_PRODUCT_CLASSIFICATION" if product_type_conflict or product_type_unresolved else "UNRESOLVED_EVIDENCE_CONFLICT"]
                eliminated_records.append({
                    **source, "record_id": record_id, "source_row": row_number,
                    "elimination_reasons": reasons,
                    "elimination_explanations": _elimination_explanations(reasons, result),
                })
        elif not validate_only:
            reasons = []
            if duplicate:
                reasons.append("DUPLICATE_NAME_IMAGE")
            reasons.extend(audit.issues)
            if failure_reason:
                reasons.append("MODEL_ENRICHMENT_FAILED")
            reasons = reasons or ["ENRICHMENT_NOT_AVAILABLE"]
            eliminated_records.append({
                **source, "record_id": record_id, "source_row": row_number,
                "elimination_reasons": reasons,
                "elimination_explanations": _elimination_explanations(reasons, detail=failure_reason),
            })
            if audit.issues:
                input_failed = audit.disposition == "FAIL"
                review_rows.append({
                    "record_id": record_id, "source_row": row_number, "product_name": source.get("name", ""),
                    "field": "input_validation" if input_failed else "image",
                    "original_value": "" if input_failed else source.get("image", ""),
                    "enriched_value": "", "confidence": "", "provenance": "",
                    "status": "failed" if input_failed else "review",
                    "attention_reason": ", ".join(audit.issues),
                })
        disposition_rows.append({"disposition": disposition})

    if catalog_records:
        with (output_dir / "enriched_products.jsonl").open("w", encoding="utf-8") as handle:
            for record in catalog_records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    if eliminated_records:
        with (output_dir / "eliminated_products.jsonl").open("w", encoding="utf-8") as handle:
            for record in eliminated_records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    review_fields = ["record_id", "source_row", "product_name", "field", "original_value", "enriched_value", "confidence", "provenance", "status", "attention_reason"]
    _write_csv(output_dir / "enrichment_review.csv", review_rows, review_fields)

    counts = {status: sum(row["disposition"] == status for row in disposition_rows) for status in ("PASS", "REVIEW", "FAIL", "SKIPPED")}
    summary = {
        "total": len(source_rows),
        "ready": len(catalog_records),
        "eliminated": len(eliminated_records),
        **{key.lower(): value for key, value in counts.items()},
        "validate_only": validate_only,
    }
    (output_dir / "batch_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    manifest = {
        "input_csv": str(input_csv),
        "input_sha256": _input_hash(input_csv),
        "images_dir": str(images_dir),
        "taxonomy_version": TAXONOMY_VERSION,
        "attribute_version": ATTRIBUTE_VERSION,
        "locale": locale,
        "currency": currency,
        "validate_only": validate_only,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate and enrich a fashion catalog using the configured NIM models.")
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--images-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--locale", default="en-US")
    parser.add_argument("--currency")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    summary = run_batch(args.input_csv, args.images_dir, args.output_dir, locale=args.locale, currency=args.currency, validate_only=args.validate_only)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
