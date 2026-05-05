#!/bin/bash
# Run all test scripts and report PASS/FAIL
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOTAL=0
PASSED=0
FAILED=0

run_test() {
    local script="$1"
    local name="$(basename "$script")"
    TOTAL=$((TOTAL + 1))
    echo ""
    echo "========================================"
    echo "Running: $name"
    echo "========================================"
    if "$script"; then
        echo "✓ PASS: $name"
        PASSED=$((PASSED + 1))
    else
        echo "✗ FAIL: $name (exit code $?)"
        FAILED=$((FAILED + 1))
    fi
}

run_test "$SCRIPT_DIR/validation_gate_test.sh"
run_test "$SCRIPT_DIR/company_scoping_test.sh"
run_test "$SCRIPT_DIR/reindex_test.sh"
run_test "$SCRIPT_DIR/lightrag_webui_check.sh"

echo ""
echo "========================================"
echo "Summary: $PASSED/$TOTAL passed, $FAILED failed"
echo "========================================"

if [[ $FAILED -eq 0 ]]; then
    echo "All tests passed!"
    exit 0
else
    echo "Some tests failed."
    exit 1
fi