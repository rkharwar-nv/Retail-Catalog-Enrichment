# Reviewed Decisions

For input shape, configuration, running and output, see the
[Runbook](FASHION_RUNBOOK.md).

Most conflicts are resolved from the data itself — see
[Conflict Resolution and Poor Product Information](FASHION_CONFLICT_RESOLUTION.md)
for how classification, attribute, and identity conflicts are settled without a
human.

What is left is genuine ambiguity: a specific signal contradicts the image and
no other signal can break the tie, or two rows are indistinguishable. Those need
a person. A decision file records those adjudications so a run **reproduces**
them instead of re-litigating them.

For the 218-row reference catalog this is two rows.

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
| `name` | conditional | Corrected product name; **required** when resolving `NAME_CONTRADICTS_CLASSIFICATION` |

When a decision supplies a `name`, the corrected name is published, the original
is recorded here and in the run ledger rather than republished, and the merchant
`description` is dropped because it describes the contradicted product type.

## What a reviewer may and may not resolve

Three reasons are resolvable:

- `UNRESOLVED_PRODUCT_CLASSIFICATION` — signals disagree with no majority
- `DUPLICATE_NAME_IMAGE` — rows indistinguishable, none canonical
- `NAME_CONTRADICTS_CLASSIFICATION` — the name states a different product type
  than the category. Resolving this **without** a corrected `name` is rejected
  when the file loads, since publishing incoherent copy is what the rule exists
  to prevent.

Everything else stays a hard stop:

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

## When a decision is needed at all

Most eliminations are not genuine ambiguity — they are merchant metadata that
disagrees with itself. The gate weighs three independent signals before asking
for a human:

| Signal | Source |
|---|---|
| product name | the merchant `name` column |
| subcategory | the merchant `subcategory` column |
| image | the visual product type |

A signal only counts when it is specific. `subcategory: shoes` covers heels,
flats, boots and sandals, so it cannot settle a dispute between them, and a name
mentioning two types ("Woven Lace Blouse Sweater") abstains rather than guessing.

The product publishes when a specific signal corroborates the visual type, or
when nothing contradicts it. The disagreeing signal is recorded in
`enrichment_review.csv` as `published_with_outlier_signal` so the source catalog
can be corrected. Only a genuine tie — one specific signal against the image,
with no third signal able to break it — needs a decision entry.

A decision is also needed when a published product's **name** states a different
product type than its category, since that is incoherent to a shopper whatever
the taxonomy says. See
[Incoherent product copy](FASHION_CONFLICT_RESOLUTION.md#incoherent-product-copy).

For the 218-row reference catalog this reduced the decision file from seven
entries to two, both of which were adjudicated against the product image.
