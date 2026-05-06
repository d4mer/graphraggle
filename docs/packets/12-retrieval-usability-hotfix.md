# Packet 12: Retrieval Usability Hotfix

## Status: accepted

## Description
This packet addresses usability issues in the retrieval layer by improving default query behavior, implementing a bounded fallback mechanism for weak retrieval, optimizing depth for transcript-like queries, and improving transcript text preprocessing.

## Scope
- Query mode defaults: `query_mode` defaults to `hybrid` if omitted.
- Fallback: One-time fallback to `naive` mode if retrieval strength is below threshold.
- Depth Optimization: Increased retrieval depth for queries detected as transcript-like.
- Preprocessing: Enhanced transcript text normalization in the worker.
- Tooling: Helper script `scripts/reindex_sct_docs.sh`.

## Implementation Plan
1. Create packet file: `docs/packets/12-retrieval-usability-hotfix.md`
2. Implement retrieval usability hotfix:
    a) query default mode -> hybrid when omitted
    b) bounded one-time fallback to naive when weak retrieval is detected
    c) higher retrieval depth for transcript-like queries
    d) transcript text preprocessing normalization in worker before /documents/text
    e) create `scripts/reindex_sct_docs.sh` helper
3. Update documentation:
    - `docs/06-api-reference.md`
    - `docs/03-admin-operations.md`
    - `docs/07-openwebui-guide.md`
    - `docs/09-operator-cheat-sheet.md`
    - `docs/quick-reference.md`
    - `scripts/README.md`
    - `CHANGELOG.md`
4. Write evidence file: `docs/ops/runs/packet-12-evidence.md`
5. Update packet status to `accepted`.

## Result: accepted with commit hashes pending finalization

## Checklist
- [x] Create packet file
- [x] Implement retrieval usability hotfix
- [x] Update documentation
- [x] Write evidence file
- [x] Update packet status
