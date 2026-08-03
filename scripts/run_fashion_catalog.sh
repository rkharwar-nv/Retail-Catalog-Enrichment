#!/usr/bin/env bash
# Reproduce the fashion catalog from a pinned CSV and a reviewed decision file.
#
# The decision file is bound to the CSV's SHA-256, so this run fails loudly if
# the catalog input changed after the decisions were reviewed.
set -euo pipefail

INPUT_CSV="${INPUT_CSV:-$HOME/retail-shopping-assistant/shared/data/products_extended.csv}"
IMAGES_DIR="${IMAGES_DIR:-$HOME/retail-shopping-assistant/shared/images}"
DECISIONS="${DECISIONS:-shared/decisions/products_extended.jsonl}"
OUTPUT_DIR="${OUTPUT_DIR:-data/catalog-recovery/run}"
LOCALE="${LOCALE:-en-US}"

for path in "$INPUT_CSV" "$IMAGES_DIR" "$DECISIONS"; do
  if [ ! -e "$path" ]; then
    echo "Missing required input: $path" >&2
    exit 1
  fi
done

echo "input_csv     $INPUT_CSV"
echo "input_sha256  $(sha256sum "$INPUT_CSV" | cut -d' ' -f1)"
echo "decisions     $DECISIONS"
echo "output_dir    $OUTPUT_DIR"
echo

PYTHONPATH=src python -m backend.fashion.batch \
  --input-csv "$INPUT_CSV" \
  --images-dir "$IMAGES_DIR" \
  --output-dir "$OUTPUT_DIR" \
  --decisions "$DECISIONS" \
  --locale "$LOCALE"
