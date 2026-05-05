#!/bin/bash
# Test validation gate behavior: file size limits, encoding fallback, duplicate rejection
set -euo pipefail

API_URL="${ENDPOINT:-http://localhost:8000}"
AUTH_TOKEN="${RAG_API_KEY:-}"
TEST_DIR="/tmp/validation_gate_test_$$"

if [[ -z "$AUTH_TOKEN" ]]; then
    echo "ERROR: RAG_API_KEY env var is not set" >&2
    exit 1
fi

cleanup() {
    rm -rf "$TEST_DIR"
}
trap cleanup EXIT

mkdir -p "$TEST_DIR"

small_file="$TEST_DIR/small.txt"
dd if=/dev/zero of="$small_file" bs=1024 count=50 2>/dev/null
echo "Small file created: $(stat -f%z "$small_file") bytes"

echo "=== Test 1: Upload small file (<100KB) → should be accepted ==="
response=$(curl -s -w "\n%{http_code}" -X POST "$API_URL/upload" \
    -H "Authorization: Bearer $AUTH_TOKEN" \
    -F "file=@$small_file")
http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')
echo "HTTP $http_code"
if [[ "$http_code" =~ ^2 ]] && echo "$body" | grep -qi "accept"; then
    echo "PASS: Small file accepted"
else
    echo "FAIL: Small file not accepted"
    exit 1
fi

echo ""
echo "=== Test 2: Upload large file (>500KB) → should be rejected/auto_split ==="
large_file="$TEST_DIR/large.bin"
dd if=/dev/zero of="$large_file" bs=1024 count=600 2>/dev/null
echo "Large file created: $(stat -f%z "$large_file") bytes"

response=$(curl -s -w "\n%{http_code}" -X POST "$API_URL/upload" \
    -H "Authorization: Bearer $AUTH_TOKEN" \
    -F "file=@$large_file")
http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')
echo "HTTP $http_code"
if echo "$body" | grep -qi "auto_split"; then
    echo "PASS: Large file flagged auto_split"
else
    echo "FAIL: Large file not auto_split"
    exit 1
fi

echo ""
echo "=== Test 3: Upload file with non-UTF8 content → encoding fallback ==="
non_utf8_file="$TEST_DIR/non_utf8.bin"
printf '\x80\x81\x82\xfe\xff' > "$non_utf8_file"

response=$(curl -s -w "\n%{http_code}" -X POST "$API_URL/upload" \
    -H "Authorization: Bearer $AUTH_TOKEN" \
    -F "file=@$non_utf8_file")
http_code=$(echo "$response" | tail -n1)
echo "HTTP $http_code"
if [[ "$http_code" =~ ^2 ]]; then
    echo "PASS: Non-UTF8 file accepted with encoding fallback"
else
    echo "INFO: Non-UTF8 file HTTP $http_code"
fi

echo ""
echo "=== Test 4: Upload same file twice → duplicate rejection ==="
unique_file="$TEST_DIR/dup_test_$$.txt"
echo "unique content $RANDOM" > "$unique_file"

response1=$(curl -s -X POST "$API_URL/upload" \
    -H "Authorization: Bearer $AUTH_TOKEN" \
    -F "file=@$unique_file")
echo "First upload: $(echo "$response1" | grep -o '"validation_state":"[^"]*"')"

sleep 1

response2=$(curl -s -X POST "$API_URL/upload" \
    -H "Authorization: Bearer $AUTH_TOKEN" \
    -F "file=@$unique_file")
echo "Second upload: $(echo "$response2" | grep -o '"validation_state":"[^"]*"')"
if echo "$response2" | grep -qi "warn.*duplicate\|duplicate"; then
    echo "PASS: Duplicate flagged"
else
    echo "FAIL: Duplicate not flagged"
    exit 1
fi

echo ""
echo "=== All validation gate tests passed ==="
exit 0