# Hermes brief: second stack + chunker arms P1 and C2 (branch `chunking`)

**Status: APPROVED by the operator on 2026-10-15 for Part A ONLY (build the second
stack, verify it, dry run; no ingest, no sigma load beyond health checks). Part B (the
arms) is NOT approved: each arm needs its own written go.** Each arm is about 8 to 9
hours of sigma time. Approval prompts appear only in your tmux pane; the operator will be
at the terminal. Report to `~/rag-project/docs/ops/runs/chunker-second-stack.md` (files,
not `rig send`).

## Goal
Measure whether (P1) contextual headers + speaker-turn normalisation, and (C2) the
semantic-vector chunker, beat the current fixed-token chunker on the v2 probes
(40 dev / 40 held-out). **Production LightRAG, gateway, worker, `.env` and store are never
touched.** All re-ingests go to a second, throwaway stack.

## Hard rules
- The loop wipes the LightRAG store it points at. It must ONLY ever be pointed at the
  second stack (`--lightrag-url http://127.0.0.1:9622`). **Never pass
  `--allow-production`.** If the loop ever prints "this would wipe the production store",
  stop and report; do not work around it.
- Do not read or copy corpus content out; counts, scores and judgment only; file names
  masked in anything you report.
- Do not stop or restart the production worker, LightRAG, gateway or Open WebUI.
- One arm at a time (single oMLX/sigma endpoint is shared with production queries).
  Production answers will be slower while an arm ingests; say the observed production
  query latency before and during an arm.
- Stop at the first failed check; report; do not retry a denied approval.

## Part A: build the second stack (no ingest)
1. **Source**: the lead ships `~/rag-project/docs/chunkrun.tgz` (sha256
   `065d14d648571345108633897a338f60c94c7f1443db717e0da638b8abfca462`,
   branch `chunking` commit `7d74feb024d750654639c85d2e3319806e1cf4fc`) and
   `~/rag-project/docs/chunkrun-MANIFEST.txt`. Verify the tarball sha and every file sha,
   then extract to a NEW dir `~/rag-project/scratch/chunkrun`. It holds only
   `scripts/overnight_loop.py`, `app/__init__.py`, `app/transcript_prep.py` (stdlib only)
   and `docs/ops/loop/experiments-chunking.json`.
2. **Stack dir** `~/rag-project/stack2/` (compose project `rag-chunk2`), a compose file with
   exactly two services, no worker, no Open WebUI:
   - `lightrag-server2`: the same image tag as production LightRAG
     (`ghcr.io/hkuds/lightrag:v1.5.7`), published on `127.0.0.1:9622`, its OWN
     `./lightrag_store` and `./lightrag_inputs` (mode 777), `INPUT_DIR` and `--input-dir`
     equal, all other settings equal to production's LightRAG.
   - `gateway-api2`: `local/rag-gateway:rc2`, published on `127.0.0.1:8021`, its OWN
     `./state`, `./source_docs` (empty), `./uploads`, `./logs`, pointing at
     `lightrag-server2`.
   - `stack2/.env`: a copy of the production `.env` with only the LightRAG/gateway
     addresses and state paths changed. Keys are copied, never printed. The production
     `.env` is never edited.
   Also write `~/rag-project/stack2/down.sh` that does `docker compose -p rag-chunk2 ... down`
   (without `-v`; removing the volumes is a separate approved step).
3. **Check**: both containers healthy; `GET :9622/health`; `GET :8021/health`;
   `/documents/status_counts` on :9622 is all zero; production counts unchanged
   (10 processed upstream, 10 ingested at the gateway).
4. **Dry run**: run the loop with `--dry-run` using the full flag set from Part B,
   confirm it prints the plan and that no request goes to :9621/:8020.
5. **Probe files and corpus**: confirm `probe/loop_corpus.json` (3 to 6 transcripts) and
   the v2 dev/held-out probe files exist; report counts and sizes only. Tell me which
   `--dev-probes`/`--heldout-probes` names to use (v2 baseline run `loop4` used them).

## Part B: arms (each needs its own operator go)
For each arm create a COPY of the experiments file containing only that arm
(`experiments-<arm>.json`; edit the copy, never the shipped file) so one run is one arm.
Command (second stack, all flags explicit):

```
python3 ~/rag-project/scratch/chunkrun/scripts/overnight_loop.py \
  --root ~/rag-project \
  --experiments ~/rag-project/scratch/chunkrun/experiments-<arm>.json \
  --out ~/rag-project/probe/runs/chunker-<arm> \
  --dev-probes <v2 dev file> --heldout-probes <v2 held-out file> \
  --lightrag-url http://127.0.0.1:9622 --gateway-url http://127.0.0.1:8021 \
  --lightrag-container lightrag-server2 --gateway-container gateway-api2 \
  --lightrag-service lightrag-server2 --gateway-service gateway-api2 \
  --compose-file ~/rag-project/stack2/compose.yml --compose-project rag-chunk2 \
  --env-file ~/rag-project/stack2/.env --worker-container none \
  --max-hours 12
```

Order and gating:
1. **P1** (`transcript_prep`, fixed-token): go required.
2. **C2** (`semantic_vector`, percentile 95, buffer 1, size 1200): go required, only
   after P1 is reported.
3. **C0 baseline: do NOT re-run.** Reuse the existing v2 baseline (`loop4`, 80 rows);
   say which run and whether its judge rubric/probe files match the arms'.
4. C1 and P2 are optional and only on the lead's say-so.

Per arm report: ingest minutes per document, chunk counts per document (from
`/documents/paginated`, counts only), dev %, held-out %, hallucination %, refusal %,
median query seconds, answers that changed vs C0 by category (counts), and any
`transcript_prep` log lines (document index, headers, turns; no names). Production check
after each arm: counts and health unchanged, and the production worker still scanning.

## Decision rule (from the chunker brief)
Adopt an arm only if held-out accuracy beats C0 by at least 3 questions (1 question =
2.5 points) with hallucination not higher and refusals not lower. Otherwise keep
fixed-token and say so.

## Teardown (separate approval)
After the lead confirms results are captured: `down.sh`, then remove `stack2/` data
(`sudo rm -rf` may be needed for root-owned files: ask for that approval up front).
Production is never part of teardown.
