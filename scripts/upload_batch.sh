#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  printf 'Usage: %s <directory> [gateway-base-url]\n' "$0" >&2
  printf 'Example: %s "/Users/imac/Desktop/GSK/Logistics" http://localhost:8000\n' "$0" >&2
  exit 1
fi

SOURCE_DIR="$1"
BASE_URL="${2:-http://localhost:8000}"

if [[ -z "${RAG_API_KEY:-}" ]]; then
  printf 'RAG_API_KEY is not set. Export it first.\n' >&2
  exit 1
fi

if [[ ! -d "$SOURCE_DIR" ]]; then
  printf 'Directory not found: %s\n' "$SOURCE_DIR" >&2
  exit 1
fi

uploaded=0
failed=0

while IFS= read -r -d '' file; do
  printf 'Uploading: %s\n' "$file"
  if curl -fsS -X POST "$BASE_URL/upload" \
    -H "Authorization: Bearer $RAG_API_KEY" \
    -F "file=@${file}" >/dev/null; then
    uploaded=$((uploaded + 1))
  else
    printf 'Failed: %s\n' "$file" >&2
    failed=$((failed + 1))
  fi
done < <(find "$SOURCE_DIR" -type f \( \
  -iname '*.txt' -o \
  -iname '*.md' -o \
  -iname '*.html' -o \
  -iname '*.htm' -o \
  -iname '*.json' -o \
  -iname '*.csv' -o \
  -iname '*.pdf' -o \
  -iname '*.docx' -o \
  -iname '*.pptx' -o \
  -iname '*.xlsx' \
\) -print0)

printf '\nUpload complete. Success: %d Failed: %d\n' "$uploaded" "$failed"
printf '\nCheck ingestion status with:\n'
printf 'curl -i %s/ingest/status -H "Authorization: Bearer $RAG_API_KEY"\n' "$BASE_URL"
