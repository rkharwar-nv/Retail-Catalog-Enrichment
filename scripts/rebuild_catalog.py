"""Rebuild the fashion catalog from frozen enrichment, deterministically.

Enrichment is a live model call, so re-running it cannot reproduce a catalog
exactly. This script instead replays enrichment that has already been produced
and applies the *current* publication rules to it: three-signal classification,
identity keyed on distinguishing fields, and reviewed decisions.

That makes the output a pure function of three pinned inputs -- the source CSV,
a frozen enrichment run, and the decision file -- so the same inputs always give
the same catalog, with no endpoint required.

Usage:
    PYTHONPATH=src python scripts/rebuild_catalog.py \
        --input-csv  shared/data/products_extended.csv \
        --enrichment shared/output/fashion-enriched-full-20260713 \
        --decisions  shared/decisions/products_extended.jsonl \
        --output-dir data/catalog-recovery/run
"""

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.fashion.batch import (
    PUBLICATION_POLICY_VERSION,
    _duplicate_key,
    _record_id,
)
from backend.fashion.decisions import DECISIONS_VERSION, load_decisions
from backend.fashion.taxonomy import (
    ATTRIBUTE_VERSION,
    PRODUCT_ATTRIBUTES,
    TAXONOMY_VERSION,
    resolve_product_type,
)

# Fields carried straight from the source row.
SOURCE_FIELDS = ("category", "subcategory", "name", "description", "url", "price", "image")
# Fields the enrichment run adds that are not attributes.
NON_ATTRIBUTE_FIELDS = SOURCE_FIELDS + (
    "record_id", "source_row", "enriched_description", "_enrichment_run",
)

# (category, subcategory) -> canonical product type. The forward mapping is
# product_type.split("."), taking the first and last parts, and each product
# type yields a unique pair, so it inverts cleanly.
CLASSIFICATION_TO_TYPE = {
    (product_type.split(".")[0], product_type.split(".")[-1]): product_type
    for product_type in PRODUCT_ATTRIBUTES
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def rebuild(
    input_csv: Path,
    enrichment_dirs: list[Path],
    gate_run: Path,
    decisions_path: Path | None,
    output_dir: Path,
) -> dict[str, Any]:
    input_sha = _sha256(input_csv)

    manifests = []
    for directory in enrichment_dirs:
        manifest = json.loads((directory / "run_manifest.json").read_text())
        if manifest.get("input_sha256") != input_sha:
            raise SystemExit(
                f"Frozen enrichment in {directory.name} was produced from a different CSV "
                f"({manifest.get('input_sha256', '?')[:12]}… vs {input_sha[:12]}…). Row numbers "
                "would not line up."
            )
        manifests.append(manifest)

    registry = None
    if decisions_path:
        registry = load_decisions(decisions_path)
        registry.verify_input(input_sha)

    with input_csv.open(newline="", encoding="utf-8-sig") as handle:
        source_rows = {i + 2: row for i, row in enumerate(csv.DictReader(handle))}

    # Enrichment sources in priority order; no single run covers every row.
    enriched: dict[int, dict[str, Any]] = {}
    for directory in enrichment_dirs:
        for record in _read_jsonl(directory / "enriched_products.jsonl"):
            if record.get("enriched_description") and record["source_row"] not in enriched:
                enriched[record["source_row"]] = {**record, "_enrichment_run": directory.name}

    # Rows the gate contested. Everything else it already published, so the
    # classification tie-breaker does not apply -- it resolves disputes, it is
    # not a second filter over rows that were never in dispute.
    contested = {
        record["source_row"]
        for record in _read_jsonl(gate_run / "eliminated_products.jsonl")
    }

    # Identity is ambiguous only when nothing distinguishes two rows.
    duplicate_counts: dict[tuple[str, ...], int] = {}
    for row in source_rows.values():
        key = _duplicate_key(row)
        duplicate_counts[key] = duplicate_counts.get(key, 0) + 1

    catalog: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []

    for source_row in sorted(source_rows):
        source = source_rows[source_row]
        record = enriched.get(source_row) or {}
        decision = registry.for_row(source_row) if registry else None
        record_id = decision.record_id if decision and decision.record_id else _record_id(
            input_csv, source_row, source
        )
        entry = {
            "source_row": source_row,
            "name": source.get("name", ""),
            "record_id": record_id,
            "published": False,
            "reason": None,
            "classification": None,
            "resolved_by": None,
        }

        if duplicate_counts[_duplicate_key(source)] > 1:
            entry["reason"] = "DUPLICATE_NAME_IMAGE"
            ledger.append(entry)
            continue

        if not record.get("enriched_description"):
            # No usable enrichment exists for this row in the frozen run.
            entry["reason"] = "NO_FROZEN_ENRICHMENT"
            ledger.append(entry)
            continue

        product_type = CLASSIFICATION_TO_TYPE.get((record.get("category"), record.get("subcategory")))
        if not product_type:
            entry["reason"] = "UNMAPPABLE_CLASSIFICATION"
            ledger.append(entry)
            continue

        classification = f"{record['category']}/{record['subcategory']}"
        resolution = resolve_product_type(source, product_type)
        if source_row not in contested:
            entry["resolved_by"] = "uncontested"
        elif resolution["publish"]:
            entry["resolved_by"] = "signal_majority"
            if resolution["outlier"]:
                entry["outlier"] = resolution["outlier"]
        elif decision and decision.resolves_reason("UNRESOLVED_PRODUCT_CLASSIFICATION"):
            entry["resolved_by"] = "reviewed_decision"
            entry["reviewer"] = decision.reviewer
            if decision.classification:
                classification = decision.classification
        else:
            entry["reason"] = "UNRESOLVED_PRODUCT_CLASSIFICATION"
            ledger.append(entry)
            continue

        category, subcategory = classification.split("/", 1)
        published = {key: source.get(key, "") for key in SOURCE_FIELDS}
        published["category"] = category
        published["subcategory"] = subcategory
        published["record_id"] = record_id
        published["source_row"] = source_row
        for field, value in record.items():
            if field not in NON_ATTRIBUTE_FIELDS and value not in (None, "", []):
                published[field] = value
        published["enriched_description"] = record["enriched_description"]

        catalog.append(published)
        entry.update(
            published=True,
            classification=classification,
            enrichment_run=record.get("_enrichment_run"),
        )
        ledger.append(entry)

    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "enriched_products.jsonl").open("w", encoding="utf-8") as handle:
        for record in catalog:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    with (output_dir / "rebuild_ledger.jsonl").open("w", encoding="utf-8") as handle:
        for entry in ledger:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    excluded: dict[str, int] = {}
    for entry in ledger:
        if not entry["published"]:
            excluded[entry["reason"]] = excluded.get(entry["reason"], 0) + 1
    summary = {
        "source_rows": len(source_rows),
        "published": len(catalog),
        "excluded": len(ledger) - len(catalog),
        "excluded_by_reason": excluded,
        "contested_rows": len(contested),
        "resolved_by_uncontested": sum(e.get("resolved_by") == "uncontested" for e in ledger),
        "resolved_by_signal_majority": sum(e.get("resolved_by") == "signal_majority" for e in ledger),
        "resolved_by_reviewed_decision": sum(e.get("resolved_by") == "reviewed_decision" for e in ledger),
        "published_with_outlier_signal": sum(bool(e.get("outlier")) for e in ledger),
    }
    (output_dir / "rebuild_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output_dir / "rebuild_manifest.json").write_text(json.dumps({
        "input_csv": str(input_csv),
        "input_sha256": input_sha,
        "enrichment_runs": [str(directory) for directory in enrichment_dirs],
        "gate_run": str(gate_run),
        "decisions_path": str(decisions_path) if decisions_path else None,
        "decisions_sha256": registry.file_sha256 if registry else None,
        "decisions_version": DECISIONS_VERSION if registry else None,
        "taxonomy_version": TAXONOMY_VERSION,
        "attribute_version": ATTRIBUTE_VERSION,
        "publication_policy_version": PUBLICATION_POLICY_VERSION,
        "enrichment_source": "replayed from a frozen run; no model calls were made",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument(
        "--enrichment", type=Path, required=True, action="append",
        help="Frozen enrichment run; repeat in priority order, as no single run covers every row",
    )
    parser.add_argument(
        "--gate-run", type=Path, required=True,
        help="Run whose eliminated_products.jsonl defines which rows were contested",
    )
    parser.add_argument("--decisions", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(
        rebuild(args.input_csv, args.enrichment, args.gate_run, args.decisions, args.output_dir),
        indent=2,
    ))


if __name__ == "__main__":
    main()
