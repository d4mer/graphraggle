# Scripts

## `install_rag_stack.sh`

Bootstrap script for the Mac mini GraphRAG stack.

## `upload_batch.sh`

Uploads all supported files from a directory to the gateway upload endpoint.

Usage:

```bash
export RAG_API_KEY='your-gateway-key'
./scripts/upload_batch.sh "/path/to/folder"
```

Optional second argument:

```bash
./scripts/upload_batch.sh "/path/to/folder" http://192.168.1.180:8000
```

Supported file types:

1. `.txt`
2. `.md`
3. `.html`
4. `.htm`
5. `.json`
6. `.csv`
7. `.pdf`
8. `.docx`
9. `.pptx`
10. `.xlsx`

## Notes

1. This script reflects the currently validated stack architecture.
2. Keep changes to this script tracked in git.
3. When changing model names, service images, or compose behavior, also update:
   - `CHANGELOG.md`
   - `docs/05-known-good-config.md`
   - `docs/04-troubleshooting.md`

## Current Validated Assumptions

1. LightRAG image tag is `ghcr.io/hkuds/lightrag:v1.4.15`
2. Embedding model is `bge-m3-mlx-fp16`
3. Reranker model is `jina-reranker-v3-mlx`
4. Text-like files should be ingested via LightRAG `POST /documents/text`
5. Binary files should be ingested via LightRAG `POST /documents/upload`

## `reindex_sct_docs.sh`

Helper script to batch-mark SCT/workshop transcript-like GSK docs for worker reindex.

Usage:

```bash
./scripts/reindex_sct_docs.sh \
  --endpoint http://localhost:8000 \
  --token "$RAG_API_KEY"
```

Apply mode:

```bash
./scripts/reindex_sct_docs.sh \
  --endpoint http://localhost:8000 \
  --token "$RAG_API_KEY" \
  --apply
```

Flags:

1. `--company` defaults to `GSK`
2. `--force` sets `force=true` on each `/ingest/reindex` call
3. dry-run is default; no API write calls are made without `--apply`

## `probe_set_check.py`

Validates a probe set against its source documents before it is used to score a run. It
prints COUNTS ONLY - no question text, no quotes - so the report is safe to paste into a
ticket. Probe files themselves are never committed.

```bash
.venv/bin/python scripts/probe_set_check.py <questions.jsonl> \
  --answer-key <answer-key.jsonl> --source-dir <dir of source documents> \
  --forbid-overlap-with <already-consumed-set.jsonl> [...] \
  --run-set <merged-for-the-runner.jsonl>
```

What it checks:

1. question rows carry no answers, ids are unique and are not reused by an earlier set
2. every evidence quote is a verbatim substring of its cited source (whitespace-folded)
   and every key phrase sits inside that quote, so the answer is gradeable from it
3. `unanswerable` / `near-miss` rows carry 3-6 absent terms and a distinctive phrase that
   occurs in NO source, so a refusal cannot be scored as a miss
4. overlap against already-consumed sets: the longest normalised run shared with an
   earlier question or evidence quote, reported as a number (default ceiling 40)
5. the answer key and the question file agree row by row, and `--run-set` writes the
   merged question+answer file the scoring runner consumes

Gate constants `GRAGGLE_PROBE_MAX_EVIDENCE_CHARS` (300) and `GRAGGLE_PROBE_OVERLAP_LIMIT`
(40) are overridable by environment variable or flag without editing the file.
Exit code 0 = clean, 1 = failure list printed in the report.

## Versioning Practice

When the installer changes materially:

1. update `CHANGELOG.md`
2. document the reason for the change
3. verify the smoke test still passes
4. commit the installer change separately when possible

## `make_synthetic_company.py`

GL-07: generates an invented company's document set so cross-company retrieval,
attribution, scoping and duplicate handling can be tested before a real second
company exists. Everything it writes is invented; it never reads `source_docs/`,
`lightrag_store/` or `state/`, and it refuses to write inside the repo.

```bash
# default company (Northwind Chemicals) into /home/maverick/work/synthetic-co/
.venv/bin/python scripts/make_synthetic_company.py

# the second company, same seed: the cross-company shared file is then identical bytes
.venv/bin/python scripts/make_synthetic_company.py --company "Contoso Pharma"

# a scratch corpus, and the shared file on its own for a duplicate test
.venv/bin/python scripts/make_synthetic_company.py --seed 7 --out /tmp/synth
.venv/bin/python scripts/make_synthetic_company.py --seed 7 --emit-shared-file /tmp/shared.txt

# force two companies apart on unique topics
.venv/bin/python scripts/make_synthetic_company.py --company "Contoso Pharma" \
    --forbid-unique-topic "unique:tanker fleet telematics"
```

Same `--seed` plus same `--company` plus same flags gives byte-identical files. Each
company directory carries a `manifest.json`: the topic map, every planted fact with
the document that holds it, the unanswerable facts, the duplicate map, and generated
scoped / cross-company / unanswerable probe shapes. Counts and invented facts only -
it is a mechanics test set, not evidence about any real company.
