# Packet 00: Working Agreement

## Status
accepted

## Objective

Establish the execution hygiene, ownership model, and review loop for all GraphRAG hardening work.

## Why This Packet Exists

The repo now acts as both implementation repo and OPS control plane. Before changes land, the team needs a shared process for scope control, evidence capture, review, and rollback awareness.

## In Scope

1. Confirm packet-driven execution.
2. Confirm branch naming and status flow.
3. Confirm required execution evidence.
4. Confirm review and acceptance gates.
5. Confirm rollback note requirement for each packet.

## Out Of Scope

1. Runtime behavior changes.
2. Validation logic.
3. Worker logic changes.
4. UI exposure changes.

## Dependencies

1. None.

## Required Decisions

1. One packet at a time by default.
2. Repo packet docs are the source of truth.
3. Runtime-affecting packets require live-stack verification.

## Implementation Guidance

1. Use `docs/packets/` for packet specs and status.
2. Use `docs/decisions/` for architecture and policy decisions.
3. Use `docs/ops/` for runbooks, verification, and rollback guidance.

## Expected Deliverables

1. OPS folder structure in repo.
2. Packet template.
3. Decision template.
4. Evidence and review templates.
5. Core OPS runbooks.

## Acceptance Criteria

1. The repo contains the OPS structure.
2. The status flow is documented.
3. The evidence requirements are documented.
4. The team can execute later packets against this structure.

## Verification Steps

1. Verify the new OPS files exist in the repo.
2. Verify packet and decision templates are usable.
3. Verify packet execution workflow is documented.

## Evidence Required

1. Changed files
2. Commands run
3. Observed outputs
4. Risks remaining
5. Rollback note

## Review Checklist

1. OPS structure exists and is coherent.
2. Packet status flow is documented.
3. Templates are consistent with the agreed workflow.

## Risks / Open Questions

1. Exact evidence storage location for noisy raw artifacts may need refinement.

## Execution Notes

This packet is satisfied by adding the OPS control-plane docs to the repo.

## Result

accepted
