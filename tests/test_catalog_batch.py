"""Tests for the HTTP-based catalog batch client."""

import csv
import inspect
import json
from unittest.mock import patch

import httpx

from backend import catalog_batch


def _write_input(tmp_path, sample_image_bytes, **updates):
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "product.png").write_bytes(sample_image_bytes)
    row = {
        "sku": "SKU-1",
        "name": "Source product",
        "description": "Merchant description",
        "price": "29.99",
        "image": "/images/product.png",
        "merchant_field": "preserve me",
    }
    row.update(updates)
    input_csv = tmp_path / "products.csv"
    with input_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=row.keys())
        writer.writeheader()
        writer.writerow(row)
    return input_csv, images_dir, row


def _responses(request, calls):
    body = request.read()
    calls.append((request.url.path, request.headers, body))
    if request.url.path == "/vlm/analyze":
        return httpx.Response(
            200,
            json={
                "title": "Enriched product",
                "description": "Endpoint-enriched description.",
                "categories": ["clothing"],
                "tags": ["floral"],
                "colors": ["blue"],
                "locale": "en-US",
                "enhanced_product": {"sku": "SKU-1", "merchant_field": "preserve me"},
            },
        )
    return httpx.Response(
        200,
        json={
            "visible_product": True,
            "product_identity": {"product_type": "dress"},
            "visual_summary": {"short_description": "A blue floral dress."},
            "appearance": {
                "colors": ["blue"],
                "pattern": "floral",
                "materials_visible": ["woven fabric"],
                "style_or_design": ["fitted"],
            },
        },
    )


def test_valid_row_calls_both_endpoints_with_complete_source_row(tmp_path, sample_image_bytes):
    input_csv, images_dir, source = _write_input(tmp_path, sample_image_bytes)
    calls = []
    client = httpx.Client(
        base_url="http://catalog-api.test",
        transport=httpx.MockTransport(lambda request: _responses(request, calls)),
    )

    summary = catalog_batch.run_batch(input_csv, images_dir, tmp_path / "output", client=client)

    assert summary == {"total": 1, "succeeded": 1, "failed": 0}
    assert [call[0] for call in calls] == ["/vlm/analyze", "/vlm/rich-product"]
    analyze_body = calls[0][2].decode("utf-8", errors="replace")
    rich_body = calls[1][2].decode("utf-8", errors="replace")
    assert 'name="image"; filename="product.png"' in analyze_body
    assert 'name="locale"' in analyze_body
    assert "en-US" in analyze_body
    assert 'name="product_data"' in analyze_body
    assert json.dumps(source, ensure_ascii=False) in analyze_body
    assert 'name="image"; filename="product.png"' in rich_body
    assert 'name="locale"' in rich_body
    assert 'name="product_data"' not in rich_body


def test_output_is_shaped_only_from_source_and_endpoint_responses(tmp_path, sample_image_bytes):
    input_csv, images_dir, _ = _write_input(tmp_path, sample_image_bytes)
    client = httpx.Client(
        base_url="http://catalog-api.test",
        transport=httpx.MockTransport(lambda request: _responses(request, [])),
    )
    output_dir = tmp_path / "output"

    catalog_batch.run_batch(input_csv, images_dir, output_dir, client=client)

    result = json.loads((output_dir / "enriched_products.jsonl").read_text())
    assert result["source"]["merchant_field"] == "preserve me"
    assert result["catalog_product"]["merchant_field"] == "preserve me"
    assert result["catalog_product"]["title"] == "Enriched product"
    assert result["catalog_product"]["description"] == "Endpoint-enriched description."
    assert result["enriched_response"]["categories"] == ["clothing"]
    assert result["raw_product_response"]["visible_product"] is True
    assert result["visual_fields"] == {
        "product_type": "dress",
        "short_description": "A blue floral dress.",
        "colors": ["blue"],
        "pattern": "floral",
        "materials_visible": ["woven fabric"],
        "style_or_design": ["fitted"],
    }
    csv_result = next(csv.DictReader((output_dir / "enriched_products.csv").open()))
    assert json.loads(csv_result["source"])["sku"] == "SKU-1"
    assert json.loads(csv_result["raw_product_response"])["visible_product"] is True


def test_endpoint_failure_is_reported_and_other_endpoint_is_still_called(tmp_path, sample_image_bytes):
    input_csv, images_dir, _ = _write_input(tmp_path, sample_image_bytes)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/vlm/analyze":
            return httpx.Response(400, json={"detail": "Invalid locale. Supported locales: ['en-US']"})
        return httpx.Response(200, json={"visible_product": True})

    client = httpx.Client(base_url="http://catalog-api.test", transport=httpx.MockTransport(handler))
    output_dir = tmp_path / "output"

    summary = catalog_batch.run_batch(
        input_csv,
        images_dir,
        output_dir,
        client=client,
        locale="invalid-locale",
        retries=3,
    )

    assert summary == {"total": 1, "succeeded": 0, "failed": 1}
    assert calls == ["/vlm/analyze", "/vlm/rich-product"]
    error = json.loads((output_dir / "batch_errors.jsonl").read_text())
    assert error["endpoint"] == "/vlm/analyze"
    assert error["status_code"] == 400
    assert error["attempts"] == 1
    assert "Invalid locale" in error["message"]
    assert error["raw_product_response"] == {"visible_product": True}


def test_retryable_http_failure_is_retried(tmp_path, sample_image_bytes):
    input_csv, images_dir, _ = _write_input(tmp_path, sample_image_bytes)
    analyze_calls = 0

    def handler(request):
        nonlocal analyze_calls
        if request.url.path == "/vlm/analyze":
            analyze_calls += 1
            if analyze_calls == 1:
                return httpx.Response(503, json={"detail": "Temporarily unavailable"})
        return _responses(request, [])

    client = httpx.Client(base_url="http://catalog-api.test", transport=httpx.MockTransport(handler))

    summary = catalog_batch.run_batch(input_csv, images_dir, tmp_path / "output", client=client, retries=1)

    assert summary["succeeded"] == 1
    assert analyze_calls == 2
    result = json.loads((tmp_path / "output" / "enriched_products.jsonl").read_text())
    assert result["request_attempts"]["/vlm/analyze"] == 2


def test_invalid_local_image_is_reported_without_http_calls(tmp_path):
    input_csv = tmp_path / "products.csv"
    input_csv.write_text("sku,image\nSKU-1,/images/missing.png\n", encoding="utf-8")
    calls = []
    client = httpx.Client(
        base_url="http://catalog-api.test",
        transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200, json={})),
    )

    summary = catalog_batch.run_batch(input_csv, tmp_path / "images", tmp_path / "output", client=client)

    assert summary == {"total": 1, "succeeded": 0, "failed": 1}
    assert calls == []
    error = json.loads((tmp_path / "output" / "batch_errors.jsonl").read_text())
    assert error["stage"] == "input_validation"
    assert "Image not found" in error["message"]


@patch("openai.OpenAI")
def test_batch_has_no_direct_model_client(mock_openai, tmp_path, sample_image_bytes):
    input_csv, images_dir, _ = _write_input(tmp_path, sample_image_bytes)
    client = httpx.Client(
        base_url="http://catalog-api.test",
        transport=httpx.MockTransport(lambda request: _responses(request, [])),
    )

    catalog_batch.run_batch(input_csv, images_dir, tmp_path / "output", client=client)

    mock_openai.assert_not_called()
    source = inspect.getsource(catalog_batch)
    assert "from openai" not in source
    assert "NGC_API_KEY" not in source
