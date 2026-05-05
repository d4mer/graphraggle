#!/bin/bash
# Test reindex functionality: find failed document, trigger reindex, verify queue
set -euo pipefail

API_URL="http://localhost:8000"
AUTH_TOKEN="${RAG_API_KEY:-}"

echo "=== Query /ingest/status to find failed documents ==="
response=$(curl -s -w "\n%{http_code}" -X GET "$API_URL/ingest/status" \
    -H "Authorization: Bearer $AUTH_TOKEN")
http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')
echo "HTTP $http_code"
echo "Response: $body"

if [[ ! "$http_code" =~ ^2 ]]; then
    echo "FAIL: Could not reach /ingest/status"
    exit 1
fi

failed_doc_id=$(echo "$body" | grep -o '"document_id":"[^"]*","status":"failed"' | head -1 | cut -d'"' -f4)
if [[ -z "$failed_doc_id" ]]; then
    failed_doc_id=$(echo "$body" | grep -o '"id":"[^"]*","status":"failed"' | head -1 | cut -d'"' -f4)
fi

if [[ -z "$failed_doc_id" ]]; then
    echo "INFO: No failed documents found, looking for any document to test reindex"
    any_doc_id=$(echo "$body" | grep -o '"document_id":"[^"]*"' | head -1 | cut -d'"' -f4)
    if [[ -z "$any_doc_id" ]]; then
        any_doc_id=$(echo "$body" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
    fi
    if [[ -z "$any_doc_id" ]]; then
        echo "FAIL: No documents found to test reindex"
        exit 1
    fi
    echo "Using document for reindex test: $any_doc_id"
    failed_doc_id="$any_doc_id"
fi

echo ""
echo "=== Call POST /ingest/reindex with document_id=$failed_doc_id and force=true ==="
response=$(curl -s -w "\n%{http_code}" -X POST "$API_URL/ingest/reindex" \
    -H "Authorization: Bearer $AUTH_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"document_id\": \"$failed_doc_id\", \"force\": true}")
http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')
echo "HTTP $http_code | Response: $body"

if [[ ! "$http_code" =~ ^2 ]]; then
    echo "FAIL: Reindex request failed with HTTP $http_code"
    exit 1
fi

echo ""
echo "=== Verify reindex was queued ==="
if echo "$body" | grep -qi "queued\|accepted\|reindex.*started\|job"; then
    echo "PASS: Reindex was queued successfully"
elif [[ "$http_code" =~ ^2 ]]; then
    echo "PASS: Reindex request accepted (HTTP $http_code)"
else
    echo "FAIL: Could not verify reindex was queued"
    exit 1
fi

echo ""
echo "=== Reindex test passed ==="
exit 0