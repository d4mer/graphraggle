# GL-10 stack2 probe plan — does the company named in the question really scope the search and answer

Written 2026-11-17 on `worker/gl10-company-from-question` for Hermes to run on stack2. Nothing here
was run by the worker: the offline part is the unit suite plus `scripts/gl10_design_measure.py` on
invented documents. Every reported number is a count — no question text, no answer text, no
reference content.

## Corpora

The GL-07 synthetic corpora only, generated and never read from `source_docs/` in this repo:

```bash
.venv/bin/python scripts/make_synthetic_company.py --company "Northwind Chemicals" --seed 7 --out /tmp/gl10-synth
.venv/bin/python scripts/make_synthetic_company.py --company "Contoso Pharma"      --seed 7 --out /tmp/gl10-synth
```

Ingest through the **filesystem door** (`source_docs/<Company>/…`) so `documents.company` is set by
path, and record before querying: rows per company, `ingested`/`skipped` counts, and the shared-file
row pair (`shared-between-companies/cutover-freeze-checklist.txt`, identical bytes in both
companies — measured: 1 of 28 documents). Then the same corpus through the **upload door** as a
second, separate run: that is where attribution is expected to fail, and the run should prove the
failure is visible rather than silent.

## Runs to compare

Same probe rows, three settings, each with its own `QUERY_LOG_PATH` capture:

| run | settings | what it measures |
|---|---|---|
| R0 | `COMPANY_FROM_QUESTION=false` | today, unchanged: the regression floor |
| R1 | `COMPANY_FROM_QUESTION=true`, `COMPANY_SCOPE_ENFORCEMENT=citations` | detection + citation filtering only |
| R2 | `COMPANY_FROM_QUESTION=true`, `COMPANY_SCOPE_ENFORCEMENT=retrieval`, `COMPANY_SCOPED_CONTEXT_MAX_CHARS=12000` | the guarantee, at its stated cost |

Log the same fields for every run; they are already emitted: `companies_detected`,
`company_detected_from`, `company_candidates`, `scoped_pass`,
`scoped_pass_references_kept`, `..._dropped_other_company`, `..._unattributable`,
`..._dropped_by_context_cap`, `..._second_model_call`, `..._declined_reason`, plus
`citations_raw/merged/scoped/final` and `duration_ms`.

## Probe rows

Take the probe shapes the manifests already emit (`manifest.json` → probes: scoped, cross-company,
unanswerable) and add the detection rows the offline rules need proving on real text:

1. **scoped** — question names exactly one company. Expect `company_mode=single`,
   `detected_from=question`.
2. **cross-company** — names both. Expect `company_mode=multi`, both cited.
3. **unscoped** — names none. Expect `companies_detected=[]` and R1/R2 envelopes equal to R0.
4. **unanswerable** — names a company, asks a fact in no document. Expect a refusal, not a code.
5. **shared-file scoped** — one company asks about the shared file. Expect its citations to include
   the shared document even if only the other company's path was indexed upstream.
6. **alias** — the short form only, with `COMPANY_ALIASES` set. Expect
   `company_matched_by_alias`.
7. **near-miss name** — a word that is also a common English word, lowercase in the question
   (`COMPANY_ALIASES` empty). Expect **no** scope, and the token in `company_candidates`.
8. **unknown organisation** — a company not in the store. Expect unscoped, never a refusal.
9. **punctuation/case** — the company name upper-cased and hyphenated. Expect a match.
10. **upload-door scoped** — the upload-ingested corpus, company named. Expect
    `scoped_pass_references_unattributable > 0` and, if nothing else supports the answer, the honest
    refusal. A wrong-company answer here is a defect; a refusal is the designed behaviour.

## Acceptance

- **0 citations from a company the question did not name** (scoped and cross-company rows) —
  computed from citation `company` fields the gateway already enriches.
- **0 other-company planted fact codes in a scoped answer.** Use the manifests' company-unique fact
  codes: for each scoped row, test membership of that company's codes (present/absent) and of every
  other company's codes; report counts only.
- **Scoped correct ≥ the 2026-11-12 baseline** (27 of 28 scoped probes correct at R0; read the same
  judge rubric at R1/R2 so the comparison is like for like).
- **Cross-company rows cite every named company ≥ 80%.**
- **Unscoped rows byte-identical between R0 and R1/R2** in citations and `query_scope`, apart from
  the new `company_scope` keys.
- **Refusals on unanswerable rows do not regress** (compare with the M1/GM1 numbers for the same
  corpus).
- R2 latency reported per run as a median and a p95, with `..._second_model_call` counted, so the
  trade-off is priced rather than asserted.

## What a scorer must compute (counts only)

Input: the three log captures, the two manifests, and the runner's `*.results.jsonl` rows. Join a log
line to a results row by `request_id` (the runner must record it); refuse to score rather than guess
when a row cannot be joined, when a probe id repeats without a `request_id`, or when a run has no log
capture. Per run print: rows by probe shape; rows by `company_mode` and `detected_from`;
citations-by-company matrix (asked companies × cited companies); planted-code membership per asked
company; `scoped_pass` counts summed and the top `declined_reason` values; refusal counts on
unanswerable rows; latency medians. Never print a question, an answer, or reference content.

The existing `scripts/score_grounding_run.py` is the model for the join and the refusal-to-guess
behaviour; a GL-10 scorer is a sibling, not a modification of it.

## Not verifiable offline (what this run is actually deciding)

- Whether LightRAG 1.5.7 `/query` accepts a document/path filter or supports per-company workspaces
  (design ranking in `docs/decisions/gl10-scoped-retrieval-design.md`); if it does, the second model
  call in R2 can go away.
- Whether the retrieval pool is large enough that a company's chunks survive the filter — R2's
  `references_dropped_other_company` and `references_kept` are the first real numbers for that.
- Real-corpus attribution: the upload-door basename gap and any path drift between saved extractions
  and stored `documents.path`.
- What the second model call costs on the deployment's model, and whether losing the token stream on
  the bridge is acceptable to the operator. That is a product decision, not a measurement.
