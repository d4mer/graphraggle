# Approval request: structure-store cutover (phases 0 to 2 only)

To: operator. From: lead. Date: 2026-10-26. Brief: `docs/briefs/hermes-structure-cutover.md` (master, `f28615b`).

## Decision
Should production switch to the structure-ingested store (stack2) and cut new documents the same way? I am asking
approval for **phases 0 to 2 only**. They change nothing in production (read-only checks, scratch builds, rehearsal on
copies and on stack2). Phase 3, the real cutover, needs a separate "go" from you, with you at the pane.

## Evidence (same 100 probes; judge-free: does the evidence reach the returned context)
| arm | chunking | embedder | result |
|---|---|---|---|
| A. production as it runs | legacy 1200/100, 110 chunks | mxbai-embed-large-v1 | 85 |
| B. scratch | legacy 1200/100, 110 chunks | BGE-M3 | 83 |
| C. stack2 | structure path, 129 chunks | BGE-M3 | 91 |

- Embedder alone (B minus A): -2, noise. Switching to BGE-M3 is neutral; it is only required because the swap carries
  BGE-M3 vectors.
- Chunker (C minus B): +8. 12 probes only C finds, 4 only B finds; two-sided sign test p = 0.077, bootstrap interval 0 to 16.
- All of the gain is on the structure probe sets (structure-dev 9/16 to 14/16, structure-heldout 10/16 to 14/16). On the
  Solvay sets there is no gain (the 4 probes B finds and C misses are all Solvay).
- Limits: the probes test whether evidence reaches the context, not whether answers are right; each arm is one
  non-deterministic extraction draw.

In short: likely better on structure-heavy documents (specs, handbooks), no benefit on transcripts, not proven
statistically. If your real usage is mostly transcripts, the cutover buys little.

## Choices I need you to confirm or change
1. **Code:** tag `structure-cutover-1` on `f7051e6` (last runtime-code commit; later commits touch nothing under `app/`).
   Delivered to Hermes by git bundle with sha256; nothing is pushed to origin for this.
2. **Data path A:** swap stack2's store into production and reconcile the state DB (about 5 minutes of retrieval downtime
   in phase 3). Option B (re-ingest, about 3.2 h degraded retrieval, a new extraction draw) only if phase 2 shows the
   state DB cannot be reconciled safely.
3. **Embedder:** the swap forces production onto BGE-M3 in the same window (production is on mxbai today, confirmed).

## What phases 0 to 2 do (no production change)
- Phase 0: record what runs now, env, state DB schema and counts, compose diff, check the build inputs resolve.
- Phase 1: prove the tag cuts the 10 documents into the same 129 pieces stack2 stored (pass = 129/129), scratch only.
- Phase 2: rehearse the swap on copies; a scratch worker must submit nothing over 5 scan cycles. Output: the exact
  phase 3 commands for me to review before I ask you for phase 3.

Rollback exists for phase 3 (snapshots with sha256 manifests, prior images tagged, rollback compose file).

## Reply needed
"Approved: phases 0 to 2" (with any changes to choices 1 to 3), or "hold".
