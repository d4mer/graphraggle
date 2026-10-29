# Hermes brief — structure-cutover-2: prerequisites for phase 3 (2026-10-29)

Goal: make phase 3 ready to execute the moment the operator says go. Operator wants to go live ASAP.
Phase 3 itself is NOT approved by this brief; do not start it. This brief is prerequisites only.
Base: tag structure-cutover-2 (0047a69), plan = docs/ops/runs/structure-cutover-plan.md.

## Do, in order
1. **Baseline (queries only, no changes).** Run probe/run_prod_recall.py (same 100 probes, :9621) against CURRENT production
   before any change. Report score as counts only (correct/refused_ok/partial/halluc), so we can compare to the expected ~91/100
   after cutover. If production scores >= the stack2 expectation, say so plainly: the cutover is then a sidegrade.
2. **Stack2 quiescence.** Confirm nothing is querying or writing stack2 (:9622). Stop the stack2 LightRAG server (or document why
   not) before the step-5 copy, and re-verify the 12 store files against scratch/cutover/backups/stack2-store-20261027T195054Z/SHA256SUMS
   (exclude and note kv_store_llm_response_cache.json if its hash differs, with a reason). Confirm `stack2/lightrag_store` and
   `scratch/stack2-structure8` are the same tree, or fix the plan's paths.
3. **Scripts.** Create scratch/cutover/pre_cutover_snapshot.py exactly as in the plan; dry-test it into a throwaway dir (not backups/).
   Fill the step-3 smoke test env (no `***` placeholders; use env references, never print keys).
4. **Off-host backup.** Plan gains a step 1b: after the snapshot verifies, copy backups/pre-cutover-<UTC> to deltahome
   (~/backups/graphraggle-cutover-<UTC>) and verify sha256 there. Update the plan accordingly.
5. **Image build now (additive, no restart).** Do plan step 3 (build local/rag-gateway:structure-cutover-2, import smoke test,
   docker tag pre-cutover, write + `config -q` the rollback compose file). Do NOT retag `latest`.
6. **Dry-run the reconcile** on a fresh online-backup copy of production state/ingest.db: matched=10, unmatched=0, ambiguous=0.
7. **Prepare the .env and compose.yml edits as patch files** (not applied) so step 6 is a single reviewed apply.

## Report
Update docs/ops/runs/structure-cutover.md: STATUS line 1 = "PREREQS DONE, READY FOR PHASE 3 GO" or the precise blocker.
Include baseline counts, stack2 quiescence result, image id, deltahome copy verification. Production must be unchanged
(docker-inspect + .env-mtime snapshot start vs end, diff empty). Counts only, no corpus content in the report.
Lead cannot be messaged from your seat: write the summary file and say so in your pane; lead will read it.
