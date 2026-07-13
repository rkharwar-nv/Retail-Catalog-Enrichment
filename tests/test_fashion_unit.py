"""Focused tests for the additive fashion batch workflow."""

import csv
import json
from pathlib import Path
from unittest.mock import patch

from backend.fashion.audit import audit_row
from backend.fashion.batch import run_batch
from backend.fashion.service import enrich_product
from backend.fashion.taxonomy import add_source_category_conflict, normalize_enrichment, validate_enrichment


def _valid_result():
    return {
        "product_type": {"value": "apparel.dresses", "confidence": 0.9, "status": "accepted", "sources": ["image"]},
        "attributes": {
            "pattern": {"value": "floral", "confidence": 0.9, "status": "accepted", "sources": ["image"]},
            "composition": {"value": "100% cotton", "confidence": 1.0, "status": "accepted", "sources": ["source_text"]},
        },
        "content": {"title": "Floral Dress", "enriched_description": "A floral dress."},
    }


def test_taxonomy_accepts_valid_enrichment():
    assert validate_enrichment(_valid_result()) == []


def test_taxonomy_rejects_non_applicable_attribute():
    result = _valid_result()
    result["attributes"]["shaft_height"] = {"value": "ankle", "status": "accepted", "sources": ["image"]}
    assert "not applicable" in validate_enrichment(result)[0]


def test_taxonomy_rejects_image_only_composition():
    result = _valid_result()
    result["attributes"]["composition"]["sources"] = ["image"]
    assert "image-only evidence" in validate_enrichment(result)[0]


def test_taxonomy_rejects_value_when_status_is_unknown():
    result = _valid_result()
    result["attributes"]["care"] = {"value": "Machine wash", "status": "unknown", "sources": []}
    assert "unknown value must be null" in validate_enrichment(result)[0]


def test_taxonomy_normalizes_unique_leaf_and_empty_status_value():
    result = _valid_result()
    result["product_type"]["value"] = "dresses"
    result["attributes"]["care"] = {"value": "other", "status": "not_visible", "sources": []}

    normalized = normalize_enrichment(result)

    assert normalized["product_type"]["value"] == "apparel.dresses"
    assert normalized["attributes"]["care"]["value"] is None


def test_taxonomy_normalizes_common_evidence_source_names():
    result = _valid_result()
    result["attributes"]["pattern"]["sources"] = ["visual"]

    normalized = normalize_enrichment(result)

    assert normalized["attributes"]["pattern"]["sources"] == ["image"]


def test_taxonomy_normalizes_direct_description_content():
    result = _valid_result()
    result["content"] = "A grounded floral dress description."

    normalized = normalize_enrichment(result)

    assert normalized["content"] == {"enriched_description": "A grounded floral dress description."}


def test_taxonomy_flattens_nested_evidence_sources():
    result = _valid_result()
    result["attributes"]["pattern"]["sources"] = [["image"], "text"]

    normalized = normalize_enrichment(result)

    assert normalized["attributes"]["pattern"]["sources"] == ["image", "source_text"]


def test_taxonomy_discards_over_specific_product_type_suffix():
    result = _valid_result()
    result["product_type"]["value"] = "apparel.dresses.maxi"

    normalized = normalize_enrichment(result)

    assert normalized["product_type"]["value"] == "apparel.dresses"


def test_taxonomy_normalizes_singular_product_type_label():
    result = _valid_result()
    result["product_type"]["value"] = "apparel.dress"

    normalized = normalize_enrichment(result)

    assert normalized["product_type"]["value"] == "apparel.dresses"


def test_source_category_conflict_is_added_for_different_product_type():
    result = _valid_result()
    result["product_type"]["value"] = "apparel.jumpsuits"
    result["conflicts"] = []

    add_source_category_conflict(result, {"subcategory": "dress"})

    assert result["conflicts"][0]["field"] == "product_type"


def test_audit_missing_image_is_review(tmp_path):
    row = {"name": "Dress", "description": "Description", "category": "apparel", "subcategory": "dress", "price": "10", "image": "/images/missing.jpg"}
    result = audit_row(row, tmp_path)
    assert result.disposition == "REVIEW"
    assert result.issues == ("IMAGE_NOT_FOUND",)


def test_validation_only_writes_reports(tmp_path, sample_image_bytes):
    images = tmp_path / "images"
    output = tmp_path / "output"
    images.mkdir()
    (images / "dress.png").write_bytes(sample_image_bytes)
    csv_path = tmp_path / "products.csv"
    fields = ["category", "subcategory", "name", "description", "url", "price", "image"]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({"category": "apparel", "subcategory": "dress", "name": "Dress", "description": "A dress", "url": "/images/dress.png", "price": "20", "image": "/images/dress.png"})

    summary = run_batch(csv_path, images, output, validate_only=True)

    assert summary == {"total": 1, "pass": 1, "review": 0, "fail": 0, "skipped": 0, "validate_only": True}
    assert (output / "enrichment_review.csv").exists()
    assert json.loads((output / "batch_summary.json").read_text())["pass"] == 1


def test_invalid_input_is_reported_as_failed(tmp_path, sample_image_bytes):
    images = tmp_path / "images"
    output = tmp_path / "output"
    images.mkdir()
    (images / "dress.png").write_bytes(sample_image_bytes)
    csv_path = tmp_path / "products.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["name", "description", "price", "image"])
        writer.writeheader()
        writer.writerow({"name": "", "description": "Description", "price": "invalid", "image": "/images/dress.png"})

    summary = run_batch(csv_path, images, output)
    review = next(csv.DictReader((output / "enrichment_review.csv").open()))

    assert summary["fail"] == 1
    assert review["field"] == "input_validation"
    assert review["status"] == "failed"
    assert "MISSING_REQUIRED_FIELD" in review["attention_reason"]
    assert "INVALID_PRICE" in review["attention_reason"]


def test_missing_image_is_reported_for_review(tmp_path):
    output = tmp_path / "output"
    csv_path = tmp_path / "products.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["name", "description", "price", "image"])
        writer.writeheader()
        writer.writerow({"name": "Dress", "description": "Description", "price": "20", "image": "/images/missing.png"})

    summary = run_batch(csv_path, tmp_path / "images", output)
    review = next(csv.DictReader((output / "enrichment_review.csv").open()))

    assert summary["review"] == 1
    assert review["field"] == "image"
    assert review["status"] == "review"
    assert review["attention_reason"] == "IMAGE_NOT_FOUND"


@patch("backend.fashion.batch.enrich_product", return_value=_valid_result())
def test_enriched_output_is_flat_and_review_is_explanatory(mock_enrich, tmp_path, sample_image_bytes):
    images = tmp_path / "images"
    output = tmp_path / "output"
    images.mkdir()
    (images / "dress.png").write_bytes(sample_image_bytes)
    csv_path = tmp_path / "products.csv"
    fields = ["category", "subcategory", "name", "description", "price", "image"]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({"category": "apparel", "subcategory": "dress", "name": "Dress", "description": "Original", "price": "20", "image": "/images/dress.png"})

    run_batch(csv_path, images, output)

    record = json.loads((output / "enriched_products.jsonl").read_text())
    assert record["category"] == "apparel"
    assert record["subcategory"] == "dresses"
    assert record["description"] == "Original"
    assert record["enriched_description"] == "A floral dress."
    assert record["pattern"] == "floral"
    assert "product_type" not in record
    assert "semantic_search_text" not in record

    review = list(csv.DictReader((output / "enrichment_review.csv").open()))
    assert review[0]["field"] == "category/subcategory"
    assert review[0]["confidence"] == "0.9"
    assert review[0]["provenance"] == "image"


@patch("backend.fashion.service.enrich_with_omni")
def test_enrichment_retries_once_after_invalid_schema(mock_omni):
    invalid = _valid_result()
    invalid["attributes"] = []
    mock_omni.side_effect = [invalid, _valid_result()]

    result = enrich_product({"name": "Dress"}, b"image", "image/jpeg")

    assert result["product_type"]["value"] == "apparel.dresses"
    assert mock_omni.call_count == 2


@patch("backend.fashion.service.enrich_with_omni")
def test_enrichment_retries_once_after_parse_failure(mock_omni):
    mock_omni.side_effect = [ValueError("invalid JSON"), _valid_result()]

    result = enrich_product({"name": "Dress"}, b"image", "image/jpeg")

    assert result["product_type"]["value"] == "apparel.dresses"
    assert mock_omni.call_count == 2


@patch("backend.fashion.service.enrich_with_omni")
def test_enrichment_allows_a_final_bounded_retry(mock_omni):
    invalid = _valid_result()
    invalid["product_type"]["value"] = "not-in-taxonomy"
    mock_omni.side_effect = [invalid, invalid, _valid_result()]

    result = enrich_product({"name": "Dress"}, b"image", "image/jpeg")

    assert result["product_type"]["value"] == "apparel.dresses"
    assert mock_omni.call_count == 3
