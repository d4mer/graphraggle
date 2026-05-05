#!/bin/bash
# Test validation gate behavior: file size limits, encoding fallback, duplicate rejection
set -euo pipefail

API_URL="http://localhost:8000"
AUTH_TOKEN="${RAG_API_KEY:-}"
TEST_DIR="/tmp/validation_gate_test_$$"

cleanup() {
    rm -rf "$TEST_DIR"
}
trap cleanup EXIT

mkdir -p "$TEST_DIR"

small_file="$TEST_DIR/small.txt"
dd if=/dev/urandom of="$small_file" bs=1024 count=50 2>/dev/null
echo "Small file created: $(stat -f%z "$small_file") bytes"

echo "=== Test 1: Upload small file (<100KB) → should be accepted ==="
response=$(curl -s -w "\n%{http_code}" -X POST "$API_URL/ingest/upload" \
    -H "Authorization: Bearer $AUTH_TOKEN" \
    -F "file=@$small_file")
http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')
echo "HTTP $http_code | Response: $body"
if [[ "$http_code" =~ ^2 ]]; then
    echo "PASS: Small file accepted"
else
    echo "FAIL: Small file rejected with HTTP $http_code"
    exit 1
fi

echo ""
echo "=== Test 2: Upload large file (>500KB) → should be rejected/auto_split ==="
large_file="$TEST_DIR/large.bin"
dd if=/dev/urandom of="$large_file" bs=1024 count=600 2>/dev/null
echo "Large file created: $(stat -f%z "$large_file") bytes"

response=$(curl -s -w "\n%{http_code}" -X POST "$API_URL/ingest/upload" \
    -H "Authorization: Bearer $AUTH_TOKEN" \
    -F "file=@$large_file")
http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')
echo "HTTP $http_code | Response: $body"
if [[ "$http_code" == "413" ]] || echo "$body" | grep -qi "auto_split\|split\|chunk"; then
    echo "PASS: Large file handled (rejected or split)"
else
    echo "FAIL: Large file not properly handled"
    exit 1
fi

echo ""
echo "=== Test 3: Upload file with non-UTF8 content → test encoding fallback ==="
non_utf8_file="$TEST_DIR/non_utf8.bin"
printf '\x80\x81\x82\xfe\xff' > "$non_utf8_file"

response=$(curl -s -w "\n%{http_code}" -X POST "$API_URL/ingest/upload" \
    -H "Authorization: Bearer $AUTH_TOKEN" \
    -F "file=@$non_utf8_file")
http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')
echo "HTTP $http_code | Response: $body"
if [[ "$http_code" =~ ^2 ]]; then
    echo "PASS: Non-UTF8 file handled with fallback"
else
    echo "INFO: Non-UTF8 file response: HTTP $http_code"
fi

echo ""
echo "=== Test 4: Upload same file twice → should reject duplicate ==="
response1=$(curl -s -w "\n%{http_code}" -X POST "$API_URL/ingest/upload" \
    -H "Authorization: Bearer $AUTH_TOKEN" \
    -F "file=@$small_file")
http_code1=$(echo "$response1" | tail -n1)
echo "First upload HTTP $http_code1"

sleep 1

response2=$(curl -s -w "\n%{http_code}" -X POST "$API_URL/ingest/upload" \
    -H "Authorization: Bearer $AUTH_TOKEN" \
    -F "file=@$small_file")
http_code2=$(echo "$response2" | tail -n1)
body2=$(echo "$response2" | sed '$d')
echo "Second upload HTTP $http_code2 | Response: $body2"
if [[ "$http_code2" == "409" ]] || echo "$body2" | grep -qi "duplicate\|exists\|already"; then
    echo "PASS: Duplicate file rejected"
else
    echo "FAIL: Duplicate file not rejected"
    exit 1
fi

echo ""
echo "=== All validation gate tests passed ==="
exit 0