# Packet Execution Workflow

## Purpose

This workflow defines how GraphRAG hardening work is planned, executed, reviewed, and accepted in this repo.

## Core Rules

1. Work is executed through packets tracked in `docs/packets/`.
2. Each packet has one clear objective and explicit acceptance criteria.
3. Only one packet should normally be `in_progress` at a time unless work is explicitly independent.
4. A packet is not complete until evidence is captured and review is done.
5. A packet must not silently expand scope.

## Packet Status Flow

1. `planned`
2. `ready`
3. `in_progress`
4. `review`
5. `accepted`

Optional states:

1. `blocked`
2. `needs_changes`

## Branching

Recommended branch names:

1. `packet-01-lifecycle-model`
2. `packet-02-validation-gate`
3. `packet-08-lightrag-webui`

## Execution Loop

1. Start from a stable base branch.
2. Move the packet file to `in_progress`.
3. Implement only the scoped changes.
4. Run packet verification steps.
5. Capture evidence using `docs/ops/templates/evidence-template.md`.
6. Move the packet file to `review`.
7. Review against the packet acceptance criteria.
8. If accepted, merge and mark the packet `accepted`.
9. If rejected, record findings and keep the packet in `needs_changes` until corrected.

## Required Evidence Per Packet

1. Changed files
2. Commands run
3. Observed outputs
4. API examples if behavior changed
5. Screenshots if UI-related
6. Risks remaining
7. Rollback note

## Review Gates

Before a packet can be accepted:

1. Scope stayed within packet boundaries.
2. Acceptance criteria were satisfied.
3. Runtime verification was completed.
4. Operator docs were updated if behavior changed.
5. No secrets were added to git.

## Notes

1. Architecture and policy decisions should be captured in `docs/decisions/`.
2. Raw logs and screenshots can be stored outside git or in a gitignored working area if they are too noisy.
