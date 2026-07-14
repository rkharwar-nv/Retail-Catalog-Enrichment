"""Batch CSV products through the catalog-enrichment HTTP API."""

import argparse
import csv
import hashlib
import json
import logging
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from PIL import Image


logger = logging.getLogger("catalog_enrichment.catalog_batch")
RETRYABLE_STATUS_CODES = {408, 429}


class EndpointError(Exception):
    """An API endpoint failed after its configured attempts."""

    def __init__(self, endpoint: str, message: str, attempts: int, status_code: int | None = None):
        super().__init__(message)
        self.endpoint = endpoint
        self.message = message
        self.attempts = attempts
        self.status_code = status_code

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "status_code": self.status_code,
            "attempts": self.attempts,
            "message": self.message,
        }


def _record_id(row: dict[str, str]) -> str:
    for key in ("product_id", "sku", "id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    normalized = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"generated:{hashlib.sha256(normalized.encode()).hexdigest()[:16]}"


def _resolve_image(
    row: dict[str, str], images_dir: Path, image_column: str
) -> tuple[Path | None, str | None, str | None]:
    image_value = str(row.get(image_column) or "").strip()
    if not image_value:
        return None, None, f"Missing image reference in column {image_column!r}."

    image_path = images_dir / Path(image_value).name
    if not image_path.is_file():
        return None, None, f"Image not found: {image_path}"

    try:
        with Image.open(image_path) as image:
            image.verify()
            image_format = image.format
    except Exception as exc:
        return None, None, f"Image is unreadable: {image_path} ({exc})"

    content_type = Image.MIME.get(image_format) or mimetypes.guess_type(image_path.name)[0]
    if not content_type or not content_type.startswith("image/"):
        return None, None, f"Unsupported image type: {image_path}"
    return image_path, content_type, None


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip() or response.reason_phrase
    if isinstance(payload, dict) and payload.get("detail"):
        return str(payload["detail"])
    return json.dumps(payload, ensure_ascii=False)


def _post_json(
    client: httpx.Client,
    endpoint: str,
    *,
    image_path: Path,
    content_type: str,
    data: dict[str, str],
    retries: int,
) -> tuple[dict[str, Any], int]:
    attempts = retries + 1
    image_bytes = image_path.read_bytes()
    for attempt in range(1, attempts + 1):
        try:
            response = client.post(
                endpoint,
                data=data,
                files={"image": (image_path.name, image_bytes, content_type)},
            )
        except httpx.RequestError as exc:
            if attempt < attempts:
                logger.warning("%s request failed on attempt %d: %s", endpoint, attempt, exc)
                continue
            raise EndpointError(endpoint, str(exc), attempt) from exc

        if response.is_success:
            try:
                payload = response.json()
            except ValueError as exc:
                raise EndpointError(endpoint, "Endpoint returned invalid JSON.", attempt, response.status_code) from exc
            if not isinstance(payload, dict):
                raise EndpointError(endpoint, "Endpoint response must be a JSON object.", attempt, response.status_code)
            return payload, attempt

        retryable = response.status_code in RETRYABLE_STATUS_CODES or response.status_code >= 500
        if retryable and attempt < attempts:
            logger.warning("%s returned HTTP %d on attempt %d", endpoint, response.status_code, attempt)
            continue
        raise EndpointError(endpoint, _error_message(response), attempt, response.status_code)

    raise AssertionError("unreachable")


def _catalog_product(source: dict[str, str], enriched: dict[str, Any]) -> dict[str, Any]:
    """Shape the authoritative /vlm/analyze response into an ingestible product."""
    product = dict(source)
    enhanced_product = enriched.get("enhanced_product")
    if isinstance(enhanced_product, dict):
        product.update(enhanced_product)
    for field in ("title", "description", "categories", "tags", "colors", "locale", "policy_decision"):
        if field in enriched:
            product[field] = enriched[field]
    return product


def _visual_fields(raw_product: dict[str, Any]) -> dict[str, Any]:
    """Copy selected fields from /vlm/rich-product without reclassifying them."""
    identity = raw_product.get("product_identity")
    summary = raw_product.get("visual_summary")
    appearance = raw_product.get("appearance")
    identity = identity if isinstance(identity, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    appearance = appearance if isinstance(appearance, dict) else {}
    fields = {
        "product_type": identity.get("product_type"),
        "short_description": summary.get("short_description"),
        "colors": appearance.get("colors"),
        "pattern": appearance.get("pattern"),
        "materials_visible": appearance.get("materials_visible"),
        "style_or_design": appearance.get("style_or_design"),
    }
    return {key: value for key, value in fields.items() if value not in (None, "", [])}


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _result_csv_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "record_id": result["record_id"],
            "source_row": result["source_row"],
            "source": json.dumps(result["source"], ensure_ascii=False),
            "catalog_product": json.dumps(result["catalog_product"], ensure_ascii=False),
            "enriched_response": json.dumps(result["enriched_response"], ensure_ascii=False),
            "raw_product_response": json.dumps(result["raw_product_response"], ensure_ascii=False),
            "visual_fields": json.dumps(result["visual_fields"], ensure_ascii=False),
        }
        for result in results
    ]


def _error_csv_rows(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "record_id": error["record_id"],
            "source_row": error["source_row"],
            "source": json.dumps(error["source"], ensure_ascii=False),
            "stage": error["stage"],
            "endpoint": error.get("endpoint", ""),
            "status_code": error.get("status_code", ""),
            "attempts": error.get("attempts", ""),
            "message": error["message"],
            "enriched_response": json.dumps(error.get("enriched_response"), ensure_ascii=False),
            "raw_product_response": json.dumps(error.get("raw_product_response"), ensure_ascii=False),
        }
        for error in errors
    ]


def run_batch(
    input_csv: Path,
    images_dir: Path,
    output_dir: Path,
    *,
    api_base_url: str = "http://localhost:8000",
    locale: str = "en-US",
    image_column: str = "image",
    retries: int = 2,
    timeout: float = 120.0,
    client: httpx.Client | None = None,
) -> dict[str, int]:
    """Call both catalog endpoints for every locally valid CSV row."""
    if retries < 0:
        raise ValueError("retries must be zero or greater")
    output_dir.mkdir(parents=True, exist_ok=True)
    with input_csv.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("Input CSV must contain a header row.")
        source_rows = list(reader)

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    owns_client = client is None
    api_client = client or httpx.Client(base_url=api_base_url.rstrip("/"), timeout=timeout)
    try:
        for source_row, source in enumerate(source_rows, start=2):
            record_id = _record_id(source)
            image_path, content_type, validation_error = _resolve_image(source, images_dir, image_column)
            if validation_error or image_path is None or content_type is None:
                errors.append({
                    "record_id": record_id,
                    "source_row": source_row,
                    "source": source,
                    "stage": "input_validation",
                    "message": validation_error,
                })
                continue

            enriched_response = None
            raw_product_response = None
            endpoint_errors: list[EndpointError] = []
            try:
                enriched_response, analyze_attempts = _post_json(
                    api_client,
                    "/vlm/analyze",
                    image_path=image_path,
                    content_type=content_type,
                    data={"locale": locale, "product_data": json.dumps(source, ensure_ascii=False)},
                    retries=retries,
                )
            except EndpointError as exc:
                endpoint_errors.append(exc)

            try:
                raw_product_response, rich_attempts = _post_json(
                    api_client,
                    "/vlm/rich-product",
                    image_path=image_path,
                    content_type=content_type,
                    data={"locale": locale},
                    retries=retries,
                )
            except EndpointError as exc:
                endpoint_errors.append(exc)

            if endpoint_errors:
                for error in endpoint_errors:
                    errors.append({
                        "record_id": record_id,
                        "source_row": source_row,
                        "source": source,
                        "stage": "endpoint_request",
                        **error.as_dict(),
                        "enriched_response": enriched_response,
                        "raw_product_response": raw_product_response,
                    })
                continue

            results.append({
                "record_id": record_id,
                "source_row": source_row,
                "source": source,
                "catalog_product": _catalog_product(source, enriched_response),
                "enriched_response": enriched_response,
                "raw_product_response": raw_product_response,
                "visual_fields": _visual_fields(raw_product_response),
                "request_attempts": {
                    "/vlm/analyze": analyze_attempts,
                    "/vlm/rich-product": rich_attempts,
                },
            })
    finally:
        if owns_client:
            api_client.close()

    _write_jsonl(output_dir / "enriched_products.jsonl", results)
    _write_csv(
        output_dir / "enriched_products.csv",
        _result_csv_rows(results),
        ["record_id", "source_row", "source", "catalog_product", "enriched_response", "raw_product_response", "visual_fields"],
    )
    _write_jsonl(output_dir / "batch_errors.jsonl", errors)
    _write_csv(
        output_dir / "batch_errors.csv",
        _error_csv_rows(errors),
        ["record_id", "source_row", "source", "stage", "endpoint", "status_code", "attempts", "message", "enriched_response", "raw_product_response"],
    )

    summary = {"total": len(source_rows), "succeeded": len(results), "failed": len({error["source_row"] for error in errors})}
    (output_dir / "batch_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    manifest = {
        "input_csv": str(input_csv),
        "images_dir": str(images_dir),
        "api_base_url": api_base_url,
        "locale": locale,
        "image_column": image_column,
        "retries": retries,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich a product CSV through the catalog-enrichment HTTP API.")
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--images-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--api-base-url", default="http://localhost:8000")
    parser.add_argument("--locale", default="en-US")
    parser.add_argument("--image-column", default="image")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    summary = run_batch(
        args.input_csv,
        args.images_dir,
        args.output_dir,
        api_base_url=args.api_base_url,
        locale=args.locale,
        image_column=args.image_column,
        retries=args.retries,
        timeout=args.timeout,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
