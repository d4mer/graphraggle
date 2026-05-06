#!/bin/bash
set -euo pipefail

ENDPOINT=""
TOKEN=""
COMPANY="GSK"
FORCE="false"
APPLY="false"

usage() {
  cat <<'EOF'
Usage: ./scripts/reindex_sct_docs.sh --endpoint <url> --token <bearer-token> [--company <name>] [--force] [--apply]

Defaults:
  --company GSK

Behavior:
  - Dry-run by default.
  - Uses /ingest/status and selects documents where:
      * source_type is filesystem
      * path contains source_docs/<company>
      * filename/path includes SCT or Workshop or Apr 30 or May
  - Calls POST /ingest/reindex for each selected document_id only with --apply.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --endpoint)
      ENDPOINT="$2"
      shift 2
      ;;
    --token)
      TOKEN="$2"
      shift 2
      ;;
    --company)
      COMPANY="$2"
      shift 2
      ;;
    --force)
      FORCE="true"
      shift
      ;;
    --apply)
      APPLY="true"
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$ENDPOINT" || -z "$TOKEN" ]]; then
  echo "ERROR: --endpoint and --token are required." >&2
  usage
  exit 1
fi

status_json=$(curl -fsS "$ENDPOINT/ingest/status" \
  -H "Authorization: Bearer $TOKEN")

selected_ids=$(python3 -c '
import json
import re
import sys

company = sys.argv[1]
payload = json.load(sys.stdin)
documents = payload.get("documents") or []
pattern = re.compile(r"(sct|workshop|apr\s*30|may)", re.IGNORECASE)
company_path = f"source_docs/{company}".lower()

for doc in documents:
    source_type = str(doc.get("source_type") or "")
    path = str(doc.get("path") or "")
    name = str(doc.get("original_filename") or doc.get("filename") or "")
    target = f"{name} {path}"
    if source_type != "filesystem":
        continue
    if company_path not in path.lower():
        continue
    if not pattern.search(target):
        continue
    doc_id = doc.get("document_id")
    if doc_id:
        print(doc_id)
' "$COMPANY" <<< "$status_json")

selected_count=0
applied_count=0
failed_count=0

if [[ -z "$selected_ids" ]]; then
  echo "No matching documents found."
  echo "Summary: selected=0 dry_run=$([[ "$APPLY" == "true" ]] && echo false || echo true) applied=0 failed=0"
  exit 0
fi

echo "Selected document_ids:"
while IFS= read -r doc_id; do
  [[ -z "$doc_id" ]] && continue
  selected_count=$((selected_count + 1))
  echo "- $doc_id"

  if [[ "$APPLY" != "true" ]]; then
    continue
  fi

  if curl -fsS -X POST "$ENDPOINT/ingest/reindex" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"document_id\":\"$doc_id\",\"force\":$FORCE}" >/dev/null; then
    applied_count=$((applied_count + 1))
  else
    failed_count=$((failed_count + 1))
  fi
done <<< "$selected_ids"

dry_run="true"
if [[ "$APPLY" == "true" ]]; then
  dry_run="false"
fi

echo "Summary: selected=$selected_count dry_run=$dry_run applied=$applied_count failed=$failed_count force=$FORCE company=$COMPANY"
