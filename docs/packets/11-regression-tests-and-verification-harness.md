# Packet 11: Regression Tests And Verification Harness

## Status
accepted

## Objective

Add repeatable tests and smoke verification so future changes do not regress validation, ingestion, readiness, scoping, and admin workflows.

## Why This Packet Exists

Without tests and a repeatable verification baseline, the hardening work will decay quickly and later changes will be risky.

## In Scope

1. Add tests for validation decisions.
2. Add tests for lifecycle transitions.
3. Add tests for upload collision handling.
4. Add tests for duplicate-content behavior.
5. Add tests for company attribution behavior.
6. Add tests for readiness logic.
7. Define live-stack smoke test runbook.
8. Define minimum regression pack for future changes.

## Out Of Scope

1. Product redesign
2. Unrelated feature work
3. Large new testing infrastructure if lightweight coverage is enough initially

## Dependencies

1. Packet 10 preferred
2. Packet 05 minimum for meaningful runtime behavior coverage

## Required Decisions

1. Minimum required regression suite for future ingest changes
2. What is unit-testable vs smoke-test-only
3. Which sample files become canonical fixtures
4. Whether smoke tests run against live local stack, isolated environment, or both

## Implementation Guidance

At minimum cover:

1. validation matrix
2. lifecycle transitions
3. upload behavior
4. worker duplicate and stale-track behavior
5. query readiness
6. company attribution

Recommended verification harness:

1. automated tests for policy and state logic
2. scripted smoke tests for end-to-end runtime behavior

## Expected Deliverables

1. Regression coverage map
2. Test list
3. Smoke test runbook
4. Canonical fixtures list
5. Verification outputs if implemented

## Acceptance Criteria

1. Critical lifecycle and validation logic are test-covered.
2. At least one repeatable end-to-end smoke test exists.
3. Future changes have a known verification baseline.
4. Operator-facing behavior has regression protection.

## Verification Steps

1. Run automated tests.
2. Run smoke-test workflow.
3. Verify failure cases are intentionally covered.
4. Verify docs reference the verification process.

## Evidence Required

1. Packet summary
2. Coverage map
3. Automated test outputs
4. Smoke test outputs
5. Canonical fixtures
6. Remaining risks

## Review Checklist

1. Most fragile paths are covered.
2. Smoke test is realistic.
3. Future contributors can use this as a release gate.
4. Coverage matches actual risk areas.

## Risks / Open Questions

1. Fixture management may need cleanup once real source extraction is done.

## Execution Notes

Prefer lightweight but meaningful coverage over elaborate infrastructure that the team will not maintain.

## Result

Pending execution and review.
