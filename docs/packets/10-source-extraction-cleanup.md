# Packet 10: Source Extraction Cleanup

## Status
accepted

## Objective

Move generated app code out of `scripts/install_rag_stack.sh` into first-class source files after ingestion behavior and operator semantics are stable.

## Why This Packet Exists

The installer currently embeds the primary gateway, worker, and state-store logic. That makes review, testing, and future change management harder than necessary.

## In Scope

1. Extract gateway app code into real source files.
2. Extract worker code into real source files.
3. Extract state store code into real source files.
4. Keep installer focused on bootstrap and packaging behavior.
5. Preserve runtime behavior.

## Out Of Scope

1. Product redesign
2. New ingestion features unless required for safe extraction
3. New UI work
4. New auth systems

## Dependencies

1. Packets 01 through 09 stable or accepted

## Required Decisions

1. Single shared package or separate gateway/worker packages
2. Whether compose and build flow stays the same
3. Whether installer should generate any app code after extraction

## Implementation Guidance

Recommended approach:

1. extract with minimal behavioral change first
2. avoid mixing structure refactor with new runtime semantics
3. keep installer functional while removing embedded source-of-truth logic

## Expected Deliverables

1. Target source layout
2. Extraction plan
3. Installer compatibility note
4. Verification outputs if implemented

## Acceptance Criteria

1. App logic is no longer primarily embedded in installer.
2. Installer remains usable.
3. Behavior remains consistent with prior accepted packets.
4. Future changes can target real source files directly.

## Verification Steps

1. Fresh install or bootstrap still works.
2. Gateway starts.
3. Worker starts.
4. Status APIs still work.
5. Known-good smoke test still passes.

## Evidence Required

1. Packet summary
2. New structure
3. Installer compatibility
4. Verification commands
5. Observed outputs
6. Remaining cleanup items

## Review Checklist

1. Logic was actually extracted.
2. Behavior was preserved.
3. New structure is materially easier to maintain.
4. Packet did not bundle unrelated functionality.

## Risks / Open Questions

1. Extraction may surface hidden coupling in the installer-generated layout.

## Execution Notes

Treat this as a behavior-preserving structural packet as much as possible.

## Result

Accepted. Extracted 5 files from install script HEREDOCs into `app/`: `__init__.py`, `config.py`, `auth.py`, `lightrag_client.py`, `generation.py`. Install script now copies these from canonical source. All 10 app files now exist as real source files.
