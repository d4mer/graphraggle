#!/bin/bash
set -euo pipefail

ENDPOINT="${ENDPOINT:-http://localhost:8000}"
TOKEN="${RAG_API_KEY:-}"
TEST_FILE="/tmp/smoke_test_doc_$$.txt"

cleanup() {
    rm -f "$TEST_FILE"
}
trap cleanup EXIT

if [[ -z "$TOKEN" ]]; then
    echo "ERROR: RAG_API_KEY env var is not set" >&2
    exit 1
fi

echo "Creating test file..."
echo "GraphRAG smoke test document $(date)" > "$TEST_FILE"
echo "This document contains multiple sentences." >> "$TEST_FILE"
echo "It discusses various topics including technology and science." >> "$TEST_FILE"
echo "Expected to be indexed and queryable." >> "$TEST_FILE"

echo "Uploading test file..."
UPLOAD_RESP=$(curl -s -X POST "${ENDPOINT}/upload" \
    -H "Authorization: Bearer ${TOKEN}" \
    -F "file=@${TEST_FILE}")

DOC_ID=$(echo "$UPLOAD_RESP" | grep -o '"document_id":"[^"]*"' | cut -d'"' -f4)

if [[ -z "$DOC_ID" ]]; then
    echo "ERROR: Failed to upload file. Response: $UPLOAD_RESP" >&2
    exit 1
fi
echo "Uploaded with document_id: $DOC_ID"

echo "Polling /ingest/status..."
STATUS_RESP=$(curl -s -X GET "${ENDPOINT}/ingest/status?document_id=${DOC_ID}" \
    -H "Authorization: Bearer ${TOKEN}")

STATE=$(echo "$STATUS_RESP" | grep -o '"state":"[^"]*"' | cut -d'"' -f4)
echo "Current state: $STATE"

MAX_ATTEMPTS=30
ATTEMPT=0
while [[ "$STATE" != "completed" && "$STATE" != "failed" && $ATTEMPT -lt $MAX_ATTEMPTS ]]; do
    sleep 2
    STATUS_RESP=$(curl -s -X GET "${ENDPOINT}/ingest/status?document_id=${DOC_ID}" \
        -H "Authorization: Bearer ${TOKEN}")
    STATE=$(echo "$STATUS_RESP" | grep -o '"state":"[^"]*"' | cut -d'"' -f4)
    echo "State: $STATE (attempt $((++ATTEMPT)))"
done

if [[ "$STATE" == "failed" ]]; then
    echo "ERROR: Ingestion failed. Response: $STATUS_RESP" >&2
    exit 1
fi

if [[ "$STATE" != "completed" ]]; then
    echo "ERROR: Ingestion did not complete within expected time" >&2
    exit 1
fi

echo "Ingestion completed. Querying document..."
QUERY_RESP=$(curl -s -X POST "${ENDPOINT}/query" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"query":"What topics are discussed in this document?","document_ids":["'"$DOC_ID"'"]}')

echo "Query response: $QUERY_RESP"

if echo "$QUERY_RESP" | grep -qi "citation"; then
    echo "PASS: Response contains citations"
    exit 0
else
    if echo "$QUERY_RESP" | grep -qi "content\|result\|answer\|response"; then
        echo "PASS: Received valid response"
        exit 0
    fi
    echo "ERROR: Response does not contain expected content. Response: $QUERY_RESP" >&2
    exit 1
fi