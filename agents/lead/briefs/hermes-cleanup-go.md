# Hermes — operator GO for part of the cleanup (2026-10-31)

Operator approved EXACTLY these. Everything else in your B list and extras stays staged and NOT approved.

APPROVED
1. B1: stop (not remove) rag-gateway-api2. Confirm /health on :9621, :8020 and open-webui unchanged afterwards.
2. B4: quarantine (mv, not delete) the 34 GSK-derived files to quarantine/gsk-20261031 with a sha256 manifest; verify the manifest after the move.
   Also quarantine the 4 probe/runs/omlx_*.log files (the 9.2 MB "unknown" logs, treated as GSK-bearing by the operator), same manifest.
   The other "unknown" files stay where they are and are NOT quarantined: list them by name in your report so the operator can decide.
3. Deltahome: delete ONLY graphraggle-cutover-dry-20261029 and graphraggle-cutover-dry-20261029b (your dry-run snapshots). Before deleting,
   confirm by name and size that they are those two and that graphraggle-cutover-20261030T075612Z and graphraggle-pptx-20261030T184549Z are untouched.
4. KEEP graphraggle-20261012 on deltahome sealed. Do not delete or move it.

NOT APPROVED (do not run): B3 key rotation (operator will do it last, himself or by a later GO), chmod of .env/compose.yml, B5 anything,
the openwebui tar, env.original/.envdump deletion, scratch trees, container removal.

Hard rules as before: no .env/store/state/source_docs change; one bare command per approval; counts only in the report.
Report: append "CLEANUP EXECUTED 2026-10-31" to structure-cutover.md with per-item result, the manifest path and verify result,
and the remaining unknown-file names. Update STATUS line 1.
