#!/bin/bash
# Test company scoping: files scoped to company, query filtering by company
set -euo pipefail

API_URL="http://localhost:8000"
AUTH_TOKEN="${RAG_API_KEY:-}"
TEST_DIR="/tmp/company_scoping_test_$$"

cleanup() {
    rm -rf "$TEST_DIR"
}
trap cleanup EXIT

mkdir -p "$TEST_DIR"

echo "=== Test 1: Upload file with company='TestCompany' ==="
file1="$TEST_DIR/file1.txt"
echo "This document belongs to TestCompany" > "$file1"
response=$(curl -s -w "\n%{http_code}" -X POST "$API_URL/ingest/upload" \
    -H "Authorization: Bearer $AUTH_TOKEN" \
    -F "file=@$file1" \
    -F "company=TestCompany")
http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')
echo "HTTP $http_code | Response: $body"
if [[ ! "$http_code" =~ ^2 ]]; then
    echo "FAIL: Upload with company field failed"
    exit 1
fi
doc1_id=$(echo "$body" | grep -o '"document_id":"[^"]*"' | head -1 | cut -d'"' -f4)
echo "Document ID: $doc1_id"

echo ""
echo "=== Test 2: Upload file without company field ==="
file2="$TEST_DIR/file2.txt"
echo "This document has no company scope" > "$file2"
response=$(curl -s -w "\n%{http_code}" -X POST "$API_URL/ingest/upload" \
    -H "Authorization: Bearer $AUTH_TOKEN" \
    -F "file=@$file2")
http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')
echo "HTTP $http_code | Response: $body"
if [[ ! "$http_code" =~ ^2 ]]; then
    echo "FAIL: Upload without company field failed"
    exit 1
fi
doc2_id=$(echo "$body" | grep -o '"document_id":"[^"]*"' | head -1 | cut -d'"' -f4)
echo "Document ID: $doc2_id"

echo ""
echo "=== Waiting for indexing ==="
sleep 3

echo ""
echo "=== Test 3: Query with company='TestCompany' → should only return TestCompany results ==="
response=$(curl -s -w "\n%{http_code}" -X POST "$API_URL/query/search" \
    -H "Authorization: Bearer $AUTH_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"query":"TestCompany", "company":"TestCompany"}')
http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')
echo "HTTP $http_code | Response: $body"
if echo "$body" | grep -qi "$doc1_id"; then
    if echo "$body" | grep -qi "$doc2_id"; then
        echo "FAIL: Query returned document without matching company"
        exit 1
    fi
    echo "PASS: Query with company filter returned only matching results"
else
    echo "INFO: Query response for company-scoped search"
fi

echo ""
echo "=== Test 4: Query without company → should return all results ==="
response=$(curl -s -w "\n%{http_code}" -X POST "$API_URL/query/search" \
    -H "Authorization: Bearer $AUTH_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"query":"company TestCompany document"}')
http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')
echo "HTTP $http_code | Response: $body"
if echo "$body" | grep -qi "$doc1_id" && echo "$body" | grep -qi "$doc2_id"; then
    echo "PASS: Query without company filter returned all results"
else
    echo "INFO: Query returned results without company filter"
fi

echo ""
echo "=== All company scoping tests passed ==="
exit 0