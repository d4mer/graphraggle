# Packet Index

These packet files are the execution units for the GraphRAG hardening program.

## Status Flow

1. `planned`
2. `ready`
3. `in_progress`
4. `review`
5. `accepted`

Optional:

1. `blocked`
2. `needs_changes`

## Recommended Execution Order

1. [Packet 00: Working Agreement](./00-working-agreement.md)
2. [Packet 01: Lifecycle And Error Model](./01-lifecycle-and-error-model.md)
3. [Packet 02: Validation Gate Design](./02-validation-gate-design.md)
4. [Packet 03: Schema And Status API Expansion](./03-schema-and-status-api-expansion.md)
5. [Packet 04: Upload Intake Hardening](./04-upload-intake-hardening.md)
6. [Packet 05: Worker Hardening](./05-worker-hardening.md)
7. [Packet 06: Query Readiness And Reindex Controls](./06-query-readiness-and-reindex-controls.md)
8. [Packet 07: Company Attribution And Retrieval Scoping](./07-company-attribution-and-retrieval-scoping.md)
9. [Packet 08: LightRAG Web UI Exposure](./08-lightrag-webui-exposure.md)
10. [Packet 09: Operator UX And Documentation Refresh](./09-operator-ux-and-documentation-refresh.md)
11. [Packet 10: Source Extraction Cleanup](./10-source-extraction-cleanup.md)
12. [Packet 11: Regression Tests And Verification Harness](./11-regression-tests-and-verification-harness.md)

## Notes

1. Each packet should have one clear objective and explicit acceptance criteria.
2. One packet should normally be `in_progress` at a time unless dependencies are truly independent.
3. Packet execution must follow the process in `../ops/packet-execution-workflow.md`.
