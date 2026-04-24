#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 || $(( ($# - 1) % 2 )) -ne 0 ]]; then
  printf 'Usage: %s <input.pdf> <output-dir> <label1> <range1> [<label2> <range2> ...]\n' "$0" >&2
  printf 'Example: %s "/Users/imac/Desktop/big.pdf" "/Users/imac/Desktop/big-split" intro 1-50 process 51-180 design 181-320 appendix 321-z\n' "$0" >&2
  exit 1
fi

if ! command -v qpdf >/dev/null 2>&1; then
  printf 'qpdf is required but not installed.\n' >&2
  printf 'If you do not want to install qpdf, use macOS Preview and the sectioning guidance in docs/12-large-pdf-ingestion.md.\n' >&2
  exit 1
fi

INPUT_PDF="$1"
OUTPUT_DIR="$2"
shift 2

if [[ ! -f "$INPUT_PDF" ]]; then
  printf 'Input PDF not found: %s\n' "$INPUT_PDF" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"

index=1
while [[ $# -gt 0 ]]; do
  label="$1"
  range="$2"
  shift 2

  safe_label="$(printf '%s' "$label" | tr ' ' '-' | tr '[:upper:]' '[:lower:]')"
  output_file="$OUTPUT_DIR/$(printf '%02d' "$index")-${safe_label}.pdf"

  printf 'Creating %s from pages %s\n' "$output_file" "$range"
  qpdf "$INPUT_PDF" --pages "$INPUT_PDF" "$range" -- "$output_file"

  index=$((index + 1))
done

printf '\nDone. Output directory: %s\n' "$OUTPUT_DIR"
