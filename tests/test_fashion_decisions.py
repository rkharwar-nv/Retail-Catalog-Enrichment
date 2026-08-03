"""Tests for reviewed decisions and how the publication gate applies them."""

import hashlib
import json
from pathlib import Path

import pytest

from backend.fashion import batch as batch_module
from backend.fashion.audit import AuditResult
from backend.fashion.batch import ELIMINATION_EXPLANATIONS, run_batch
from backend.fashion.decisions import (
    DECISIONS_VERSION,
    RESOLVABLE_REASONS,
    DecisionError,
    load_decisions,
)

# A genuinely ambiguous row: the name says flats, the image says heels, and the
# 'shoes' column covers both, so no signal can break the tie. Rows where two of
# the three signals agree are resolved by the classifier and never reach the
# decision layer -- see test_fashion_unit.py.
CSV_TEXT = (
    "category,subcategory,name,description,price,image\n"
    "footwear,shoes,Velvet Ballet Flats,A plush velvet flat,159.99,item.jpg\n"
)


def _write_csv(tmp_path: Path, text: str = CSV_TEXT) -> Path:
    path = tmp_path / "products.csv"
    path.write_text(text, encoding="utf-8")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_decisions(tmp_path: Path, input_sha: str, *rows: dict) -> Path:
    path = tmp_path / "decisions.jsonl"
    lines = [json.dumps({
        "kind": "decision_header",
        "decisions_version": DECISIONS_VERSION,
        "input_sha256": input_sha,
    })]
    lines.extend(json.dumps(row) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


DECISION = {
    "source_row": 2,
    "resolves": ["UNRESOLVED_PRODUCT_CLASSIFICATION", "NAME_CONTRADICTS_CLASSIFICATION"],
    "classification": "footwear/heels",
    "name": "Velvet Stiletto Pumps",
    "reviewer": "reviewer@example.com",
    "rationale": "Image clearly shows a stiletto heel; the merchant name is wrong.",
}


def test_every_resolvable_reason_is_one_the_gate_emits():
    """A decision resolving a reason the gate never emits would silently never match."""
    assert RESOLVABLE_REASONS <= set(ELIMINATION_EXPLANATIONS)


def test_load_decisions_reads_rows(tmp_path):
    csv_path = _write_csv(tmp_path)
    path = _write_decisions(tmp_path, _sha(csv_path), DECISION)
    registry = load_decisions(path)

    assert len(registry) == 1
    decision = registry.for_row(2)
    assert decision.classification == "footwear/heels"
    assert decision.name == "Velvet Stiletto Pumps"
    assert decision.resolves_reason("UNRESOLVED_PRODUCT_CLASSIFICATION")
    assert not decision.resolves_reason("IMAGE_NOT_FOUND")
    assert registry.file_sha256 == _sha(path)


def test_decisions_bound_to_a_different_csv_are_rejected(tmp_path):
    csv_path = _write_csv(tmp_path)
    path = _write_decisions(tmp_path, "0" * 64, DECISION)
    registry = load_decisions(path)

    with pytest.raises(DecisionError, match="re-review"):
        registry.verify_input(_sha(csv_path))


def test_unresolvable_reason_is_rejected(tmp_path):
    csv_path = _write_csv(tmp_path)
    path = _write_decisions(
        tmp_path, _sha(csv_path), {**DECISION, "resolves": ["IMAGE_NOT_FOUND"]}
    )
    with pytest.raises(DecisionError, match="cannot override"):
        load_decisions(path)


def test_missing_reviewer_is_rejected(tmp_path):
    csv_path = _write_csv(tmp_path)
    decision = {key: value for key, value in DECISION.items() if key != "reviewer"}
    path = _write_decisions(tmp_path, _sha(csv_path), decision)
    with pytest.raises(DecisionError, match="reviewer"):
        load_decisions(path)


def test_malformed_classification_is_rejected(tmp_path):
    csv_path = _write_csv(tmp_path)
    path = _write_decisions(tmp_path, _sha(csv_path), {**DECISION, "classification": "apparel"})
    with pytest.raises(DecisionError, match="category/subcategory"):
        load_decisions(path)


def test_header_is_required(tmp_path):
    path = tmp_path / "decisions.jsonl"
    path.write_text(json.dumps(DECISION) + "\n", encoding="utf-8")
    with pytest.raises(DecisionError, match="decision_header"):
        load_decisions(path)


def test_duplicate_rows_are_rejected(tmp_path):
    csv_path = _write_csv(tmp_path)
    path = _write_decisions(tmp_path, _sha(csv_path), DECISION, DECISION)
    with pytest.raises(DecisionError, match="Duplicate decision"):
        load_decisions(path)


def _stub_enrichment(monkeypatch, conflict: bool):
    """Return an enrichment result whose product_type is accepted or contested."""
    result = {
        "product_type": {"value": "footwear.heels", "status": "review" if conflict else "accepted"},
        "attributes": {"primary_color": {"value": "red", "status": "accepted"}},
        "content": {"enriched_description": "A plush velvet shoe."},
        "conflicts": (
            [{"field": "product_type", "source_value": "ballet flats",
              "visual_value": "footwear.heels", "reason": "Image shows a stiletto heel."}]
            if conflict else []
        ),
    }
    monkeypatch.setattr(batch_module, "enrich_product", lambda *a, **k: result)


def _stub_audit(monkeypatch, tmp_path):
    image = tmp_path / "item.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0stub")
    monkeypatch.setattr(
        batch_module, "audit_row",
        lambda *a, **k: AuditResult(1.0, "PASS", (), image, "image/jpeg"),
    )


def test_contested_row_is_eliminated_without_a_decision(tmp_path, monkeypatch):
    csv_path = _write_csv(tmp_path)
    _stub_audit(monkeypatch, tmp_path)
    _stub_enrichment(monkeypatch, conflict=True)
    out = tmp_path / "out"

    summary = run_batch(csv_path, tmp_path, out)

    assert summary["ready"] == 0
    assert summary["eliminated"] == 1
    assert not (out / "decision_ledger.jsonl").exists()


def test_decision_publishes_a_contested_row(tmp_path, monkeypatch):
    csv_path = _write_csv(tmp_path)
    _stub_audit(monkeypatch, tmp_path)
    _stub_enrichment(monkeypatch, conflict=True)
    decisions = _write_decisions(tmp_path, _sha(csv_path), DECISION)
    out = tmp_path / "out"

    summary = run_batch(csv_path, tmp_path, out, decisions_path=decisions)

    assert summary["ready"] == 1
    assert summary["eliminated"] == 0
    assert summary["decisions_applied"] == 1

    record = json.loads((out / "enriched_products.jsonl").read_text().strip())
    assert record["category"] == "footwear"
    assert record["subcategory"] == "heels"

    # The corrected name is published; the original lives in the ledger only.
    assert record["name"] == "Velvet Stiletto Pumps"
    assert "merchant_name" not in record
    # The merchant description described flats, so it is not published either.
    assert record["description"] == ""

    entry = json.loads((out / "decision_ledger.jsonl").read_text().strip())
    assert entry["outcome"] == "published"
    assert entry["resolved"] == [
        "UNRESOLVED_PRODUCT_CLASSIFICATION",
    ]
    assert entry["reviewer"] == "reviewer@example.com"

    manifest = json.loads((out / "run_manifest.json").read_text())
    assert manifest["decisions_sha256"] == _sha(decisions)
    assert manifest["decisions_version"] == DECISIONS_VERSION


def test_decision_does_not_publish_an_uncontested_row_differently(tmp_path, monkeypatch):
    """A decision is inert when the gate would have published the row anyway."""
    csv_path = _write_csv(tmp_path)
    _stub_audit(monkeypatch, tmp_path)
    _stub_enrichment(monkeypatch, conflict=False)
    decisions = _write_decisions(tmp_path, _sha(csv_path), DECISION)
    out = tmp_path / "out"

    summary = run_batch(csv_path, tmp_path, out, decisions_path=decisions)

    assert summary["ready"] == 1
    record = json.loads((out / "enriched_products.jsonl").read_text().strip())
    assert record["subcategory"] == "heels"


def test_run_is_reproducible_across_identical_runs(tmp_path, monkeypatch):
    csv_path = _write_csv(tmp_path)
    _stub_audit(monkeypatch, tmp_path)
    _stub_enrichment(monkeypatch, conflict=True)
    decisions = _write_decisions(tmp_path, _sha(csv_path), DECISION)

    outputs = []
    for name in ("run_a", "run_b"):
        out = tmp_path / name
        run_batch(csv_path, tmp_path, out, decisions_path=decisions)
        outputs.append((out / "enriched_products.jsonl").read_text())

    assert outputs[0] == outputs[1]


def test_stale_decision_file_fails_the_run(tmp_path, monkeypatch):
    """Editing the CSV after review must fail loudly, not silently misapply rows."""
    csv_path = _write_csv(tmp_path)
    _stub_audit(monkeypatch, tmp_path)
    _stub_enrichment(monkeypatch, conflict=True)
    decisions = _write_decisions(tmp_path, _sha(csv_path), DECISION)
    csv_path.write_text(CSV_TEXT.replace("159.99", "189.99"), encoding="utf-8")

    with pytest.raises(DecisionError):
        run_batch(csv_path, tmp_path, tmp_path / "out", decisions_path=decisions)


def test_name_contradicting_its_category_is_held_without_corrected_copy(tmp_path, monkeypatch):
    """A product named 'Ballet Flats' filed under heels is incoherent to a shopper."""
    csv_path = _write_csv(tmp_path)
    _stub_audit(monkeypatch, tmp_path)
    _stub_enrichment(monkeypatch, conflict=False)
    out = tmp_path / "out"

    summary = run_batch(csv_path, tmp_path, out)

    assert summary["ready"] == 0
    assert summary["eliminated"] == 1
    eliminated = json.loads((out / "eliminated_products.jsonl").read_text().strip())
    assert eliminated["elimination_reasons"] == ["NAME_CONTRADICTS_CLASSIFICATION"]


def test_decision_resolving_name_conflict_must_supply_a_name(tmp_path):
    csv_path = _write_csv(tmp_path)
    decision = {key: value for key, value in DECISION.items() if key != "name"}
    path = _write_decisions(tmp_path, _sha(csv_path), decision)
    with pytest.raises(DecisionError, match="corrected 'name'"):
        load_decisions(path)


def test_compatible_types_do_not_count_as_contradiction(tmp_path, monkeypatch):
    """A heeled sandal is an ordinary product, not incoherent copy."""
    csv_path = _write_csv(
        tmp_path,
        "category,subcategory,name,description,price,image\n"
        "footwear,shoes,Bow Trim Heeled Shoes,An open-toe bow shoe,129.99,item.jpg\n",
    )
    _stub_audit(monkeypatch, tmp_path)
    monkeypatch.setattr(batch_module, "enrich_product", lambda *a, **k: {
        "product_type": {"value": "footwear.sandals", "status": "accepted"},
        "attributes": {"primary_color": {"value": "white", "status": "accepted"}},
        "content": {"enriched_description": "An open-toe bow sandal on a block heel."},
        "conflicts": [],
    })
    out = tmp_path / "out"

    summary = run_batch(csv_path, tmp_path, out)

    assert summary["ready"] == 1
