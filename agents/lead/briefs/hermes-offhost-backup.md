# Hermes brief — off-host copy of the pre-cutover backup (2026-10-30)

Context: phase 3 is live. Step 1b (copy to deltahome) was missed before the cutover (your own record says so). Close that gap now.
This is additive and read-only on production: no .env edit, no restart, no write to lightrag_store/, state/ or source_docs/.

## Do
1. Copy `backups/pre-cutover-20261030T075612Z` (the pre-cutover snapshot, manifest verify bad=0) to deltahome at
   `~/backups/graphraggle-cutover-20261030T075612Z`. Also copy `lightrag_store.pre-cutover-20261030T075612Z` only if it is not
   already identical to the snapshot's store files; say which.
2. Verify sha256 of every file on deltahome against the manifest. Pass = bad=0, file count equal.
3. Do not delete anything from the Thinkpad, and do not touch the stack2 A/B that is running (lightrag-server2).
4. One bare command per approval; background long copies with a log.

## Report
Add a short section "OFF-HOST BACKUP 2026-10-30" to docs/ops/runs/structure-cutover.md: path on deltahome, file count, bytes,
bad count, timestamp. Counts only. Write the summary file and say so in your pane; lead cannot be messaged from your seat.
