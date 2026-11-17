# GL-10 stack2 runbook — R0 / R1 / R2 for Hermes

One page. Run it as written; the scorer refuses to score a run that was set up differently, and the
comparison only means anything if the three runs differ in nothing but the settings below. Plan and
acceptance numbers: `docs/ops/gl10-company-scoping-probe-plan.md`. Probe files and the scorer are in
this repo (`scripts/make_gl10_probes.py`, `scripts/score_gl10_run.py`); the probe set itself is
off-repo at `/home/maverick/work/synthetic-co-probes/gl10/<Company>/manifest.json` (mode 600).

## Image

`local/rag-gateway:structure-cutover-5`, cut by the lead from the merged GL-10 measurement-kit
commit. Before switching:

```bash
docker tag local/rag-gateway:latest local/rag-gateway:pre-structure-cutover-5   # rollback tag
# build/pull structure-cutover-5, then point :latest at it for gateway-api and ingest-worker
docker compose up -d --no-deps gateway-api ingest-worker
docker ps --format '{{.Names}}\t{{.Image}}' | grep rag-     # report both image ids
curl -s localhost:8020/health                               # expect ok
```

Rollback at any point: `docker tag local/rag-gateway:pre-structure-cutover-5
local/rag-gateway:latest && docker compose up -d --no-deps gateway-api ingest-worker`.

## Corpus (already ingested on stack2 — verify, do not re-ingest unless the counts differ)

`/home/maverick/work/synthetic-co/{Northwind Chemicals,Contoso Pharma}`, seed **20261101**, copied
into `source_docs/<Company>/…` (the **filesystem** door, so `documents.company` is set from the
path). Verify before any query:

```bash
sqlite3 state/state.db "SELECT company, status, COUNT(*) FROM documents GROUP BY company, status;"
```

Expect 14 files per company, one shared-file row pair (identical sha256 under both companies), and
`ingested` for the rows the probes cite. Report the table; if rows are missing or `skipped`
unexpectedly, stop and report instead of querying.

The **upload-door** probe group (`run_group: upload_door`, 3 rows per company) needs its own index:
same bytes through `POST /ingest/upload` with the `company` field, in a separate stack2 state/store
backup-and-restore. Run it once at R2 and once at R0. Say which state directory each run used.

## The three runs — only these lines change

Base env (all three runs, from the existing `.env`): unchanged. Add to `.env` per run:

```bash
# R0 — today, the regression floor
COMPANY_FROM_QUESTION=false

# R1 — detection + citation filtering (the shipped enforcement value)
COMPANY_FROM_QUESTION=true
COMPANY_SCOPE_ENFORCEMENT=citations

# R2 — the guarantee: retrieve, restrict, regenerate
COMPANY_FROM_QUESTION=true
COMPANY_SCOPE_ENFORCEMENT=retrieval
COMPANY_SCOPED_CONTEXT_MAX_CHARS=12000
COMPANY_ALIASES='Northwind Chemicals=northwind;Contoso Pharma=contoso'
```

`COMPANY_ALIASES` must be exactly that string: the alias probe rows were built against it
(`aliases` block in each probe manifest). Restart the gateway after every change
(`docker compose restart gateway-api`), and for each run set a distinct log capture in `.env`:

```bash
QUERY_LOG_PATH=/app/logs/gl10-R0.query-log.jsonl   # and -R1, -R2
```

(`./logs/` is bind-mounted, so the host path is `logs/gl10-R<run>.query-log.jsonl`.) Truncate or
remove the previous run's file before starting a run so one capture never mixes two runs.

## Probe rows

49 rows per company, ten shapes: `scoped` 12, `cross_company` 9, `unscoped` 6, `unanswerable` 6,
`shared_file` 2, `alias` 3, `near_miss_word` 3, `unknown_org` 2, `punctuation_case` 3,
`upload_door` 3. Per run, ask every row in the two manifests whose `run_group` is `main` (46 each)
against the filesystem-door index, in manifest order, one request at a time.

Ask through the gateway `/query` door (`POST localhost:8020/query`, `Authorization: Bearer
$RAG_API_KEY`, body `{"query": …}`) — it returns structured citations with the company the gateway
joined, and its `meta.request_id` is the join key the scorer needs. Then re-run the same scoped and
cross-company rows once through the Open WebUI bridge (`/api/chat`) at R2 only, and record whether
the Sources block lists only the named company's documents; report that as a separate count, not
mixed into the matrix.

The runner's `*.results.jsonl` must carry, per row: `id` (the probe id), **`request_id`** (from
`meta.request_id`), `question`, `answer` (full), `citations` (the `data.citations` array as
returned, with its `company` fields), and `latency_s`. Without `request_id` the scorer refuses when a
question repeats. `--map THEIRS=OURS` exists if your field names differ; prefer adding it to the
command over editing the export.

Judge the answers as usual (`<results>.verdicts.jsonl`: `id`, `verdict` in
`correct|partial|wrong|hallucinated|refused_ok|refused_wrong`) against the manifests'
`expected_fact_code` / `expected_document`, not against the system's own text. Without verdicts the
scorer still reports the scoping guards but prints `scoped_correct=not computable`.

## Score and report

```bash
.venv/bin/python scripts/score_gl10_run.py \
  --probe-manifest "/home/maverick/work/synthetic-co-probes/gl10/Northwind Chemicals/manifest.json" \
  --probe-manifest "/home/maverick/work/synthetic-co-probes/gl10/Contoso Pharma/manifest.json" \
  --run  R0=…/gl10-R0.results.jsonl --run R1=…/gl10-R1.results.jsonl --run R2=…/gl10-R2.results.jsonl \
  --log  R0=logs/gl10-R0.query-log.jsonl --log R1=logs/gl10-R1.query-log.jsonl \
  --log  R2=logs/gl10-R2.query-log.jsonl \
  --verdicts R0=…/gl10-R0.results.verdicts.jsonl --verdicts R1=… --verdicts R2=… \
  --baseline R0
```

Exit 2 means the run could not be scored (a join that would need guessing, a missing log capture, a
duplicated id). Report the exit line and the reason rather than retrying with edited data.

Send back: the acceptance block verbatim (it is counts only), the `other_company_citations` and
`other_company_codes_in_scoped_answers` numbers per run, the cross-company citation rate, the
`unscoped_rows_changed_vs_baseline` number, R2's `scoped_pass` counts (kept /
dropped_other_company / unattributable / dropped_by_context_cap / declined reasons), latency median
and p95 per run, and the bridge Sources count. No question text, no answers, no reference content.

## Teardown

```bash
docker compose down --no-temps           # or leave the stack up if the lead asks
# restore the pre-run state/store ONLY if the upload-door run touched them
```

Leave `logs/gl10-R*.query-log.jsonl` and the three `*.results.jsonl` + verdicts files in place and
list their paths and sha256s in the report, so the numbers can be re-scored without re-running the
stack.
