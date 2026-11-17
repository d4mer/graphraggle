# Hermes brief — GL-10 on stack2: does naming the company in the question scope the search AND the answer? (R0 / R1 / R2 / R3)

STACK2 ONLY. Production untouched (docker-inspect + .env-mtime snapshot start and end, diff empty). Synthetic data (Northwind Chemicals, Contoso Pharma), you may read it and print text where useful.
Why: the operator's core requirement is "the company named in the question is enough to scope the search and the answer". The worker built it behind flags (off by default) on tag structure-cutover-5 (commit 6f2f6d3); this run measures it.

Files (staged by the lead):
- Code: ~/briefs/structure-cutover-5.bundle, sha256 c1c67987760f974742a0457304c4bc67433f1ff809dda6d7502805d4e9603a7b (verify the sha and `git bundle verify`; build from the tag only).
- Runbook (exact env blocks, runner field requirements, scoring command, teardown): docs/ops/gl10-stack2-runbook.md inside that tag (also copied to ~/briefs/gl10-stack2-runbook.md).
- Probes: ~/briefs/probes-gl10/ (Northwind Chemicals/manifest.json, Contoso Pharma/manifest.json; 98 rows, 10 shapes; sha256 ab46a98c3e35... and aff2a0241062...). Scorer: scripts/score_gl10_run.py at the tag.
Follow the runbook for the details; the points below are what I need you to be careful about.

## Do
1. Build local/rag-gateway:structure-cutover-5 from the tag (additive; keep -2/-3/-4 and tag pre-structure-cutover-5 for rollback). Stack2 baseline pair unchanged (unpatched LightRAG, R=1, dedupe OFF as in your earlier runs, worker2 stopped, no ingest).
   Check stack2's state table first: documents per company, the shared-file row pair, and that the document paths in the state DB match the paths in the manifests (the shared-file rows depend on that join; report counts).
2. Four runs on the SAME 92 main-group probes, each with its own fresh QUERY_LOG_PATH, llm_response_cache cleared (backed up) before each run, gateway recreated with --force-recreate:
   R0 COMPANY_FROM_QUESTION=false (today); R1 =true with COMPANY_SCOPE_ENFORCEMENT=citations; R2 =true, =retrieval, COMPANY_SCOPED_CONTEXT_MAX_CHARS=12000, COMPANY_ALIASES as in the runbook; R3 = R2 + COMPANY_KEYWORD_HINT=true.
   Runner (your existing runner is fine) must record per row: request_id (from meta.request_id), probe id, question, answer, citations WITH their `company` fields, latency_s. Persist the references shape census from the query log, not text. 900 s guard, sequential, stop at first 5xx.
3. The upload_door group (6 rows) is a separate small run on its own index per the runbook (back up and restore stack2 state/store around it). The operator will use the filesystem door, so this is only to see the failure is visible, not silent; keep it short.
4. One Open WebUI bridge pass at R2 (the Sources block) as the runbook says, a handful of company-named questions; say whether the answer streams or arrives at once.
5. Judge: your usual judge (qwen3.6-35b, rubric v2, the 'Bearer ' header line intact, do not re-edit it with the patch tool) for correct/partial/refused on the answerable and unanswerable rows, as verdicts.jsonl per run for the scorer.
6. Score with scripts/score_gl10_run.py exactly as the runbook says, R0 as baseline. Report counts only from the scorer, plus: scoped_pass_model_calls must read 1 in R2 and R3 (if 2, report retrieval route, fallback reason and shape census and do not re-run blind); retrieval route counts; keys seen for chunk text and file_path.

## Acceptance (what I need to read)
0 citations from a company the question did not name; 0 other-company planted fact codes in scoped answers; scoped correct >= the 11-12 baseline (27/28); cross-company answers cite every named company >= 80%; unscoped rows unchanged vs R0; refusals on unanswerable rows not worse; latency median and p95 per run.
Also tell me per shape which rows failed (ids only), and for any detection row that failed whether it was over-scoped or under-scoped.

## Teardown and report
Restore stack2 .env/compose from backup, stop it, store/state unchanged (170/1735/2298, 32 state rows, or the same numbers you find at start), production diff empty. Keep logs/results/verdicts with sha256s under ~/briefs/out/ with prefix gl10-.
Append (>>, dated .bak first) the section "GL-10 STACK2 RUNS 2026-11-17" to structure-cutover.md; STATUS line only if you must. Rules: one bare command per approval, stop on first failure, do not retry a denied approval. The operator is waiting on this to add real company documents: please report as soon as R2 is scored, then R3.
