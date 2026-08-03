# Reviewed Decisions

The publication gate eliminates a row whenever the evidence is genuinely
ambiguous — the merchant text disagrees with the image, two rows share an
identity, or the image is missing. Those defaults are correct, but they leave
real products out of the catalog until a human adjudicates them.

A decision file records those adjudications so a run **reproduces** them instead
of re-litigating them.

## Running with decisions

```bash
scripts/run_fashion_catalog.sh
```

or directly:

```bash
PYTHONPATH=src python -m backend.fashion.batch \
  --input-csv shared/data/products_extended.csv \
  --images-dir shared/images \
  --output-dir data/catalog-recovery/run \
  --decisions shared/decisions/products_extended.jsonl
```

## File format

Line-delimited JSON. The first line binds the file to one exact CSV:

```json
{"kind": "decision_header", "decisions_version": "fashion-decisions/0.1", "input_sha256": "3b58651736…"}
```

Each subsequent line is one adjudication:

```json
{"source_row": 161, "resolves": ["UNRESOLVED_PRODUCT_CLASSIFICATION"],
 "classification": "apparel/jumpsuits", "reviewer": "someone@example.com",
 "rationale": "Name and image both say jumpsuit; only the subcategory column said dress."}
```

| Field | Required | Meaning |
|---|---|---|
| `source_row` | yes | CSV line number, header being line 1 |
| `resolves` | yes | Elimination reason codes this decision adjudicates |
| `reviewer` | yes | Who made the call |
| `rationale` | yes | Why — recorded in the run's decision ledger |
| `classification` | no | `category/subcategory` override for the published record |
| `record_id` | no | Stable id, used to distinguish otherwise-identical rows |

## What a reviewer may and may not resolve

Only `UNRESOLVED_PRODUCT_CLASSIFICATION` and `DUPLICATE_NAME_IMAGE` are
resolvable. Everything else stays a hard stop:

- `IMAGE_NOT_FOUND` / `IMAGE_UNREADABLE` — no visual evidence to adjudicate
- `MISSING_REQUIRED_FIELD` / `INVALID_PRICE` — the input row is invalid
- `MODEL_ENRICHMENT_FAILED` / `ENRICHMENT_NOT_AVAILABLE` — no record was
  produced, and a reviewer cannot supply enrichment that does not exist

A decision naming any other reason is rejected at load time rather than
silently never matching.

## Reproducibility guarantees

- **Decisions are bound to the CSV.** Row numbers only mean something for one
  exact input file. If the CSV changes, the run fails with a `DecisionError`
  telling you to re-review, instead of applying row 161's decision to whatever
  now sits on line 161.
- **The manifest pins everything.** `run_manifest.json` records `input_sha256`,
  `decisions_sha256`, `taxonomy_version`, `attribute_version`,
  `publication_policy_version`, `decisions_version`, and the `vlm_endpoint` and
  `vlm_model` that produced the run.
- **Every applied decision is logged.** `decision_ledger.jsonl` records, per
  row, the gate's reasons, which were resolved, which were not, the reviewer,
  the rationale, and whether the row ended up published.

### What is *not* guaranteed

Enrichment is a live model call. Identical inputs and identical decisions will
not necessarily produce byte-identical enriched text, because the VLM is not
deterministic. What the decision file makes reproducible is the **policy**:
which rows publish, under which classification, on whose authority. To
reproduce a catalog exactly, reuse the published artifacts rather than re-running
enrichment.
