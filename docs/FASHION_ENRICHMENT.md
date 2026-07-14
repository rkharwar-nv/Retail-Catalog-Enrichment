# Fashion Catalog Enrichment

## Purpose

This additive batch workflow turns a product CSV and matching image directory into a flat enriched fashion catalog. Nemotron Omni receives each complete CSV row and image together so visible evidence and supplied facts can be reconciled in one multimodal request.

The existing API and generic catalog-enrichment behavior are unchanged.

## Workflow

```mermaid
flowchart LR
    CSV[Product CSV] --> V[Validate and match]
    IMG[Image folder] --> V

    V -->|Valid row and image| O[Nemotron Omni<br/>image and text together]
    V -->|Missing or invalid input| R[enrichment_review.csv]

    O --> T[Enforce fashion taxonomy<br/>and evidence rules]
    T -->|Accepted and grounded corrections| C[enriched_products.jsonl<br/>production catalog]
    T -->|Failed or unresolved identity| E[eliminated_products.jsonl]
    T -->|All fields| R

    R --> H[Catalog review<br/>focus on review and failed rows]
```

Omni reasons over the complete source row and image in one request. Deterministic validation then controls which values enter the production catalog. Confidence, provenance, unknown values, conflicts, and failures are written to the separate review report.

## Inputs

Required CSV columns:

| Column | Meaning |
|---|---|
| `name` | Product name |
| `description` | Original supplier or merchant description |
| `image` | Image path whose basename exists under `--images-dir` |
| `price` | Non-negative numeric price |

The current sample also provides `category`, `subcategory`, and `url`. All source columns are preserved. Version 0.1 supports one local image per row and does not download remote images.

### Example input dataset

The customer dataset is not distributed with this repository. A compatible input CSV has this shape:

```csv
category,subcategory,name,description,url,price,image
apparel,dress,Navy Wrap Dress,"A navy wrap dress made from 100% cotton with machine-wash care instructions.",https://shop.example/products/navy-wrap-dress,149.99,/images/navy_wrap_dress.jpg
footwear,shoes,Tan Lace-Up Boots,"Tan ankle boots with a leather upper and lace fastening.",https://shop.example/products/tan-boots,179.99,/images/tan_lace_up_boots.jpg
```

The matching image directory is supplied separately:

```text
images/
├── navy_wrap_dress.jpg
└── tan_lace_up_boots.jpg
```

Only the basename from the CSV `image` value is resolved beneath `--images-dir`. For example, `/images/navy_wrap_dress.jpg` resolves to `/path/passed/to/images/navy_wrap_dress.jpg`. Files are matched exactly; the workflow does not fuzzy-match names or silently substitute a different image.

CSV row numbers in the outputs include the header as row 1. In this example, the dress has `source_row=2` and the boots have `source_row=3`.

Additional customer columns are carried through to `enriched_products.jsonl`, but the current version expects the required column names shown above and does not yet support a column-mapping configuration.

## Configuration

Set `NGC_API_KEY` in the repository `.env`. By default, the workflow uses the VLM URL and model in `shared/config/config.yaml`. An OpenAI-compatible remote endpoint can be selected without editing YAML:

```bash
VLM_API_BASE_URL=https://integrate.api.nvidia.com/v1
VLM_MODEL=nvidia/nemotron-3-nano-omni-30b-a3b-reasoning
```

## Run

Validate inputs without model calls:

```bash
python -m backend.fashion.batch \
  --input-csv /path/to/products.csv \
  --images-dir /path/to/images \
  --output-dir /path/to/output \
  --validate-only
```

Enrich the catalog:

```bash
python -m backend.fashion.batch \
  --input-csv /path/to/products.csv \
  --images-dir /path/to/images \
  --output-dir /path/to/output \
  --locale en-US \
  --currency USD
```

## Output files

```text
output/
├── enriched_products.jsonl
├── eliminated_products.jsonl
├── enrichment_review.csv
├── batch_summary.json
└── run_manifest.json
```

### `enriched_products.jsonl`

This is the production catalog to ingest. Each line is one flat JSON product that passed the publication gate. It preserves every original CSV field, normalizes `category` and `subcategory`, adds accepted or visually corrected fashion attributes, and adds one grounded `enriched_description`.

| Field | Meaning |
|---|---|
| Original CSV fields | Preserved source values; normalized classification replaces source `category` and `subcategory` after successful enrichment |
| `record_id` | Existing product ID/SKU when supplied; otherwise a deterministic hash of source name, image, and URL |
| `source_row` | Original CSV row number, including the header as row 1 |
| `category` | Canonical broad category, such as `apparel`, `footwear`, `bags`, `eyewear`, or `jewelry` |
| `subcategory` | Canonical product class, such as `dresses`, `skirts`, `boots`, or `shoulder_bags` |
| Fashion attributes | Accepted applicable fields such as `primary_color`, `pattern`, `neckline`, `garment_length`, or `composition` |
| `description` | Original source description, unchanged |
| `enriched_description` | Natural description grounded in the image and trustworthy supplied facts; use this for semantic embedding and optional display |
| `currency` | Added only when `--currency` is supplied |

Unknown or inapplicable attribute values are omitted. A clear conflict about a visible attribute does not eliminate the product: the visible value is published, the contradicted claim is excluded from `enriched_description`, and the complete decision is recorded in `enrichment_review.csv`. An unresolved product-identity conflict still eliminates the product. Confidence, provenance, statuses, conflicts, and processing details are intentionally excluded from this ingestion file.

Example:

```json
{
  "record_id": "generated:2b682a3db8471740",
  "source_row": 22,
  "category": "apparel",
  "subcategory": "dresses",
  "name": "Classic Wrap Dress",
  "description": "Original supplier description...",
  "price": "149.99",
  "image": "/images/wrap-dress.jpg",
  "primary_color": "navy",
  "neckline": "v_neck",
  "garment_length": "midi",
  "composition": "100% cotton",
  "enriched_description": "A navy midi dress with a V-neckline and wrap-style front. The supplied information identifies the fabric as 100% cotton."
}
```

The generated fallback ID is stable when the CSV is reordered, but a supplied `product_id`, `sku`, or `id` remains strongly preferred. If multiple rows share the same name and image without a stable supplied ID, all members of that ambiguous group are eliminated rather than assigned arbitrary identities.

### `eliminated_products.jsonl`

This is the quarantine file, not a production catalog. Each line preserves the original product and adds:

| Field | Meaning |
|---|---|
| `record_id` | Supplied stable ID or deterministic fallback ID |
| `source_row` | Original CSV row number |
| `elimination_reasons` | Stable machine-readable reason codes |
| `elimination_explanations` | Product-specific plain-language explanations, including the conflicting field and evidence reason when available |

For a blocking identity conflict, the explanation states:

1. which field caused elimination;
2. what the input text or structured row claimed;
3. what visual analysis found;
4. why the workflow refused to publish an unresolved choice.

Example:

```json
{
  "elimination_reasons": ["UNRESOLVED_PRODUCT_CLASSIFICATION"],
  "elimination_explanations": [
    "Cause: input-text-versus-image conflict for 'category/subcategory'. Input text/structured data says 'ballet flats'; visual analysis says 'high-heeled pumps'. Evidence detail: the source describes a low-profile flat, while the image shows a pointed stiletto heel. The product was not published because choosing either value without review could make its taxonomy, filters, or enriched description incorrect."
  ]
}
```

The opening `Cause:` label makes the evidence path explicit. It will say whether the failure is an unresolved identity conflict, missing or unusable visual evidence, incomplete or invalid input data, ambiguous product identity, model-output validation failure, or incomplete enrichment. Attribute corrections belong in the review report, not the eliminated file.

### When a product is eliminated

| Condition | Why it cannot enter the production JSONL |
|---|---|
| Required name or description is missing | The product cannot be identified or described reliably |
| Price is invalid or negative | The source record fails the required input contract |
| Image is missing or unreadable | Joint image/text enrichment cannot be completed |
| Model output remains invalid after three attempts | No schema-valid, taxonomy-valid enrichment is available |
| Source and image disagree on product classification | Publishing either classification would silently resolve an unresolved identity conflict |
| Multiple rows share name and image without stable IDs | The workflow cannot safely determine whether they are duplicates, variants, or distinct products |

Unknown optional attributes, corrected visible attributes, and unsupported source claims do **not** eliminate a product when the workflow can produce internally consistent grounded content. A product is eliminated only when its identity is unresolved, required evidence or input is unavailable, or no valid enrichment can be produced.

Machine-readable elimination codes include:

- `MISSING_REQUIRED_FIELD`
- `INVALID_PRICE`
- `IMAGE_NOT_FOUND`
- `IMAGE_UNREADABLE`
- `MODEL_ENRICHMENT_FAILED`
- `UNRESOLVED_PRODUCT_CLASSIFICATION`
- `DUPLICATE_NAME_IMAGE`

Fix or review these records, then rerun them before adding them to the production catalog.

### `enrichment_review.csv`

This is the human-readable audit and attention report. It contains one row per returned classification or attribute.

| Column | Meaning |
|---|---|
| `record_id` | Product identifier used in the enriched catalog |
| `source_row` | Original CSV row number |
| `product_name` | Source product name for convenient review |
| `field` | Classification, attribute, unsupported claim, image issue, or processing issue being reported |
| `original_value` | Relevant supplied value when available; blank when the field was newly extracted |
| `enriched_value` | Proposed normalized value; blank when unknown or failed |
| `confidence` | Model confidence that the value was extracted from the stated evidence; not scientific verification of a supplier claim |
| `provenance` | Evidence source or sources supporting the value |
| `status` | Whether the field is usable or needs attention |
| `attention_reason` | Plain-language reason for a conflict, unsupported claim, missing input, or processing failure |
| `decision` | Explicit publication choice, such as `accepted`, `published_with_visual_correction`, `claim_omitted`, or a value/correction not published because the product was eliminated |
| `decision_reason` | Why that choice was safe, including which evidence was authoritative |

Status definitions:

| Status | Meaning | Production behavior |
|---|---|---|
| `accepted` | Usable value supported by permitted evidence | Included in the JSONL when it is an attribute or classification |
| `corrected` | Source text clearly conflicts with a directly visible attribute | Visual value is included in the JSONL and the source claim is excluded from the enriched description |
| `review` | Unsupported claim, identity conflict, or material uncertainty requires attention | Behavior is stated explicitly in `decision` |
| `unknown` | Available evidence cannot establish the value; this is not an error | Omitted from the JSONL |
| `failed` | The row could not produce valid enrichment | Product is written to `eliminated_products.jsonl`, not the production JSONL |

Provenance definitions:

| Provenance | Meaning |
|---|---|
| `image` | Visible evidence from the product image |
| `source_text` | Evidence extracted from supplied name or description |
| `source_structured` | Evidence from a structured CSV field |
| `image_ocr` | Text visibly read from the image |
| Values joined with `+` | More than one source supports the value |

Accepted fields do not receive verbose generated reasoning. Their value, confidence, and provenance are the routine explanation. `attention_reason` is reserved for rows requiring action.

Example showing both the inconsistency and the publication decision:

```csv
source_row,field,original_value,enriched_value,confidence,provenance,status,attention_reason,decision,decision_reason
22,primary_color,,navy,0.96,image,accepted,,accepted,The value passed taxonomy and evidence validation.
22,composition,,100% cotton,1.0,source_text,accepted,,accepted,The value passed taxonomy and evidence validation.
22,toe_shape,closed_toe,open_toe,0.99,image,corrected,Source says closed-toe but the image clearly shows an open toe.,published_with_visual_correction,The attribute is directly visible so the visual value replaced the source value in the product and enriched description.
22,care,,,0.0,,unknown,,omitted_not_available,No sufficiently supported value was available so the attribute was omitted without blocking the product.
```

### Decision reference

`status` describes the field finding. `decision` states what the workflow did with that finding. The decision is the clearest column for determining whether a value or product was published.

| Decision | Example situation | Outcome | Relevant output |
|---|---|---|---|
| `accepted` | A visible color is supported by the image and passes the controlled vocabulary | Value is published | `enriched_products.jsonl`; evidence row in `enrichment_review.csv` |
| `published_with_visual_correction` | Supplied text says `closed_toe`; clear visual evidence says `open_toe` | Visual value and corrected description are published; inconsistency and rationale remain in review | `enriched_products.jsonl` and `enrichment_review.csv` |
| `omitted_not_available` | Care instructions are absent and cannot be established visually | Optional field is omitted; product remains publishable | `enrichment_review.csv` |
| `claim_omitted` | Supplied text makes an objective claim that available evidence cannot support | Claim is excluded from grounded enrichment; remaining product is published | `enriched_products.jsonl` and `enrichment_review.csv` |
| `eliminated_for_identity_review` | Supplied identity says flats while clear visual evidence identifies heels, or duplicate rows lack stable IDs | Entire product is withheld because its identity is unresolved | `eliminated_products.jsonl` and `enrichment_review.csv` |
| `correction_not_published` | A visual attribute correction exists, but the same product has a blocking identity conflict | Correction is recorded for review but is not published | `eliminated_products.jsonl` and `enrichment_review.csv` |
| `value_not_published` | An individual field is valid, but the product has a blocking identity conflict | Valid field is recorded but is not published independently | `eliminated_products.jsonl` and `enrichment_review.csv` |
| `claim_not_published` | An unsupported claim belongs to a product already blocked by an identity conflict | Claim and product both remain outside the production catalog | `eliminated_products.jsonl` and `enrichment_review.csv` |
| `eliminated_for_missing_visual_evidence` | Referenced image is missing or unusable | Product is withheld because multimodal enrichment cannot run | `eliminated_products.jsonl` and `enrichment_review.csv` |
| `eliminated_for_invalid_input` | Required name/description is missing or price is invalid | Product fails the input contract and is withheld | `eliminated_products.jsonl` and `enrichment_review.csv` |
| `eliminated_for_processing_failure` | Model output remains schema- or taxonomy-invalid after three attempts | Product is withheld with the exact final validation detail | `eliminated_products.jsonl` and `enrichment_review.csv` |

Example of a published correction:

```text
field: toe_shape
original_value: closed_toe
enriched_value: open_toe
status: corrected
decision: published_with_visual_correction
attention_reason: Supplied text says closed-toe while the product is visibly open-toe.
decision_reason: The directly visible value replaced the conflicting source value in the product and enriched description.
```

Example of an identity-blocked product with otherwise valid field findings:

```text
category/subcategory decision: eliminated_for_identity_review
heel_type decision: correction_not_published
primary_color decision: value_not_published
result: the complete product is written only to eliminated_products.jsonl
```

## Validation and disposition rules

Validation occurs in three stages:

1. Input validation checks required text, price, image presence, and image readability.
2. Omni analyzes the complete CSV row and image together.
3. Deterministic validation enforces allowed classifications, applicable attributes, controlled values, evidence sources, and the required grounded description.

| Condition | Batch disposition | Review status and behavior |
|---|---|---|
| Valid input and enrichment with no material conflict | `PASS` | Accepted fields enter the JSONL |
| Optional attribute cannot be established | May remain `PASS` | Field is `unknown` and omitted from JSONL |
| Missing image | `REVIEW` | Product is eliminated because visual enrichment cannot run |
| Clear visible-attribute disagreement | `REVIEW` | Product is published with the visual correction; source value, visual value, inconsistency, decision, and rationale are recorded |
| Product-classification disagreement | `REVIEW` | Entire product is eliminated pending resolution |
| Unsupported objective claim | `REVIEW` | Claim is reported and excluded from grounded content; the remaining product is published |
| Duplicate name and image without stable source ID | `REVIEW` | All ambiguous rows are eliminated without model calls |
| Missing name or description | `FAIL` | `input_validation` is marked failed and product is eliminated |
| Invalid or negative price | `FAIL` | `input_validation` is marked failed and product is eliminated |
| Image exists but is unreadable | `FAIL` | `input_validation` is marked failed and product is eliminated |
| Model response remains invalid after three attempts | `FAIL` | `processing` is marked failed and product is eliminated |

Model output is rejected when it contains an unknown classification, an attribute that does not apply to the classification, a value outside a controlled vocabulary, invalid provenance, an image-only composition or care claim, an incomplete conflict record, a visual correction that does not match the selected attribute, or no grounded enriched description. Harmless structural variations are normalized before validation; invalid results receive at most three total attempts.

`PASS`, `REVIEW`, and `FAIL` apply to the complete product row in `batch_summary.json`. `accepted`, `review`, `unknown`, and `failed` apply to individual rows in `enrichment_review.csv`.

### `batch_summary.json`

Reports total, ready, eliminated, pass, review, fail, and skipped record counts. These are operational dispositions, not a composite truth or quality score.

### `run_manifest.json`

Records the input path and hash, image directory, taxonomy versions, locale, currency, validation mode, and execution time. It contains no credentials.

## Evidence rules

- Image evidence supports visible product form, color, pattern, neckline, sleeves, silhouette, and closure.
- Source text supports supplied nonvisual facts such as composition, care, and measurements.
- Source text also supports hidden or internal functional features that one exterior image cannot confirm or refute.
- Structured fields support price, URL, identifiers, and other explicit source values.
- Appearance alone cannot prove composition, care, waterproofing, sustainability, measurements, availability, or performance.
- A conflict requires clear, mutually exclusive evidence about the same attribute and product component.
- Layered product components may coexist; seeing one exterior closure does not disprove a supplied hidden or internal closure.
- A product may support multiple carrying methods; one photographed presentation does not disprove a supplied detachable or out-of-frame strap.
- Lack of visibility or visual uncertainty is not a conflict.
- Clear visible-attribute conflicts use the visual correction in structured data and grounded description while retaining the complete audit decision in the review report.
- Product-identity conflicts are reported and eliminated rather than silently resolved.
- Unknown is preferred to guessing.

## Current limitations

- One local image per CSV row
- Default column names only
- Exact duplicate name/image ambiguity is quarantined; broader entity resolution and fuzzy duplicate detection are not included
- No style/variant reconstruction without stable identifiers
- No inventory or availability inference
- No Elasticsearch indexing or embedding generation
- No complete Google Merchant feed generation
- No large occasion, mood, aesthetic, or trend taxonomy
