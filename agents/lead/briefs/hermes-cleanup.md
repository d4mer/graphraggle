# Hermes brief — pre-launch cleanup: inventory now, destructive steps staged for GO (2026-10-31)

Goal: get production ready to open to users. Part A is read-only and starts now. Part B changes things: prepare it as
scripts/patch files and a dry-run report, but DO NOT RUN any of it until the operator gives an explicit GO per item.
Production (:9621, :8020, open-webui) must stay up and unchanged during Part A. Counts, names, sizes only; no corpus content.

## Part A: inventory (read-only)
1. **Stray containers:** for `lightrag-server2` (stack2) and `rag-gateway-api2` (old rc2): image, ports, uptime, what mounts they use,
   whether anything queries them (access log counts, last hour). Note stack2 is needed by the queued R=5 arm; say what stopping it costs.
2. **GSK-quoting leftovers (GSK data was wiped from the store on 2026-10-12):** list by path, size and mtime, never contents: files under
   probe/, docs/ops/runs/, scratch/, any probe results/jsonl that quote answers or questions. Mark each: GSK-derived / Solvay-derived / neither
   (judge by name and size/mtime only; if unsure say "unknown", do not open the file to find out).
3. **OpenWebUI:** chat history size (row counts of chats/messages tables only), user count, which API keys exist (names, created dates, NOT values),
   and where the gateway key is configured (compose var name `OPENWEBUI_OPENAI_API_KEYS`, .env, host compose literal). Note the host compose carries
   the key as a literal, per your phase 0 finding.
4. **Old store and backups on the Thinkpad:** sizes of `lightrag_store.pre-cutover-*`, `backups/*`, and `scratch/` trees; which have verified off-host copies on
   deltahome (pre-cutover, pre-pptx yes; timeout-pre: no).
5. **Deltahome cleanup candidates** already listed in your T6: re-list with sizes, mark which are safe by your record.
6. Check the `.env`/compose.yml permissions and whether any file contains a plain-text API key outside .env (names of files only).

## Part B: stage, do not run (each needs its own operator GO)
For each, write a script/patch under scratch/cleanup/, a dry-run output, and the exact rollback or "irreversible" note:
- B1 stop (not remove) `rag-gateway-api2`; B2 stop `lightrag-server2` only if the R=5 arm is dropped; say which.
- B3 OpenWebUI key rotation: new key generated into .env by reference only (never printed), gateway and open-webui updated together;
  the downtime estimate (restart of open-webui and gateway-api); rollback = restore .env/compose from snapshot.
- B4 wipe plan for GSK-quoting leftovers: move to a quarantine dir first (not delete), with a manifest; irreversible delete is a later separate GO.
- B5 OpenWebUI chat-history wipe: NOT requested by the operator; stage only a backup + a clearly separate wipe script, marked "do not run without explicit ask".

## Hard rules
- No change to lightrag_store/, state/ingest.db, source_docs/, uploads/, .env values, rerank/LLM/query parameters.
- No deletes anywhere in this brief. Snapshots before any later GO. One bare command per approval; background long jobs with a log.
- Do not read file contents to classify them; names/sizes/mtimes/counts only.

## Report
Section "CLEANUP INVENTORY 2026-10-31" in docs/ops/runs/structure-cutover.md (STATUS line: "CLEANUP INVENTORY DONE, STAGED B1-B5 AWAITING GO"),
with a table: item, size/count, risk, proposed action, reversible yes/no. Lead cannot be messaged from your seat: write the file and say so in your pane.
