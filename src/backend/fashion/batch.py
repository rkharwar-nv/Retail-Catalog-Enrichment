"""Command-line batch enrichment for fashion catalogs."""

import argparse
import csv
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.fashion.audit import audit_row
from backend.fashion.service import enrich_product
from backend.fashion.taxonomy import ATTRIBUTE_VERSION, TAXONOMY_VERSION

logger = logging.getLogger("catalog_enrichment.fashion.batch")


def _record_id(csv_path: Path, row_number: int, row: dict[str, Any]) -> str:
    for key in ("product_id", "sku", "id"):
        if str(row.get(key) or "").strip():
            return str(row[key]).strip()
    return f"{csv_path.stem}:{row_number}"


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
    conflict_reasons = {item.get("field"): item.get("reason", "") for item in conflicts if isinstance(item, dict)}
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
        "status": "review" if "product_type" in conflict_reasons else _review_status(product_type),
        "attention_reason": conflict_reasons.get("product_type", ""),
    }]
    for field, value in (result.get("attributes") or {}).items():
        if not isinstance(value, dict):
            continue
        rows.append({
            "record_id": record_id,
            "source_row": row_number,
            "product_name": source.get("name", ""),
            "field": field,
            "original_value": "",
            "enriched_value": value.get("value") if value.get("value") is not None else "",
            "confidence": value.get("confidence", ""),
            "provenance": _sources(value),
            "status": "review" if field in conflict_reasons else _review_status(value),
            "attention_reason": conflict_reasons.get(field, ""),
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
    for field, value in (result.get("attributes") or {}).items():
        if isinstance(value, dict) and value.get("status") == "accepted" and value.get("value") is not None:
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

    disposition_rows: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    catalog_records: list[dict[str, Any]] = []

    for row_number, source in enumerate(source_rows, start=2):
        record_id = _record_id(input_csv, row_number, source)
        audit = audit_row(source, images_dir)
        disposition = audit.disposition

        result = None
        if not validate_only and audit.disposition != "FAIL" and audit.image_path and audit.content_type:
            try:
                result = enrich_product(source, audit.image_path.read_bytes(), audit.content_type, locale)
                if result.get("conflicts") or result.get("unsupported_claims"):
                    disposition = "REVIEW"
            except Exception as exc:
                logger.exception("Fashion enrichment failed for %s", record_id)
                disposition = "FAIL"
                review_rows.append({
                    "record_id": record_id, "source_row": row_number, "product_name": source.get("name", ""),
                    "field": "processing", "original_value": "", "enriched_value": "", "confidence": "",
                    "provenance": "", "status": "failed", "attention_reason": str(exc),
                })

        if result is not None:
            catalog_records.append(_catalog_record(record_id, row_number, source, result, currency))
            review_rows.extend(_review_rows(record_id, row_number, source, result))
        elif not validate_only:
            catalog_records.append({**source, "record_id": record_id, "source_row": row_number})
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
    review_fields = ["record_id", "source_row", "product_name", "field", "original_value", "enriched_value", "confidence", "provenance", "status", "attention_reason"]
    _write_csv(output_dir / "enrichment_review.csv", review_rows, review_fields)

    counts = {status: sum(row["disposition"] == status for row in disposition_rows) for status in ("PASS", "REVIEW", "FAIL", "SKIPPED")}
    summary = {"total": len(source_rows), **{key.lower(): value for key, value in counts.items()}, "validate_only": validate_only}
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
