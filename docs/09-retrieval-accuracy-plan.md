# Retrieval accuracy plan (2026-10-16)

Status: PLAN, proposed by the lead, not yet approved. Nothing here changes production.
Every production step needs the operator's written go; Hermes (Thinkpad) executes.

## Goal
Retrieval as accurate as possible over a growing corpus of meeting transcripts (speech to
text, `(Speaker) text`) plus docx/pdf/pptx, on LightRAG v1.5.7 with local models. Ingestion
must preserve whole-document context. Release line = `master` (tag `rc-2`); experiments
live on branch `chunking`.

## What we now know (evidence)
1. **Embedder truncates at 512 tokens, silently.** Hermes proved it with synthetic text
   (`ops/runs/embedder-check.md`): past token 512 the embedding does not change (cosine
   1.000000). LightRAG believes the limit is 8192 (openai-binding default), so its own
   protections never fire. Production chunks are 1200 tokens, so ~57% of every chunk is
   invisible to vector search, entity and relation description vectors, and the reranker's
   vector side. The limit is not settable by env in 1.5.7.
2. **Earlier "no gain" results are not trustworthy** (chunk size 600/2000, TOP_K, token
   budget, rerank, and the P1 pilot): the test corpora (2-6 docs, ~16 chunks) were smaller
   than `top_k=40`/`chunk_top_k=20`, so every arm retrieved nearly everything; the judge
   ignored wrong-speaker attribution; one draw on 40 questions (~6 pt standard error).
3. **The pilot's T1 file is not a transcript** (2 lines, one ~7.6k-token paragraph); the
   detector skipping it was correct. T2 is a true 2-speaker transcript, P1 applied there,
   zero verdict change. This only says P1 did nothing on one small transcript.
4. **Research** (`docs/research/chunking-context-preserving.md` and
   `chunking-adversarial-review.md`): boundaries alone are a contested, probably weak lever;
   restoring document context to chunks helps in vendor benchmarks but an independent
   placebo-controlled ablation (arXiv 2610.10170) found LLM headers no better than a
   shuffled header at a 512-token budget; larger chunks hurt LightRAG extraction (arXiv
   2605.21974); no study covers noisy multi-party transcripts. Drop late chunking,
   propositions, fine-tuned chunkers, full RAPTOR.
5. **Code review** (`docs/research/graphraggle-adversarial-review.md`) found ingestion data
   loss: `[hh:mm:ss]` deleted everywhere in text; text files >= 500 KB marked `auto_split`
   and never submitted; in-place edits likely stick on 409; Latin-1 fallback mangles
   UTF-16; `/api/*` has no auth; Sources lists retrieved, not used, references.

## Principles
- Fix the instrument before comparing methods. No more multi-hour arms on 2-6 documents.
- One variable per arm; placebo arm for any header; 3 draws; paired bootstrap CIs.
- Production changes only via a Hermes brief, a backup, a written go, a rollback.
- Neither lead nor worker reads corpus content; Hermes reports counts and shapes.

## Phases

### Phase 0: instrument (DONE on `chunking`, commit 267656f, 816 tests)
Label parser widened, unparseable labels no longer merged, attribution and refusal columns,
`evidence_in_context%` probe, `--draws`, paired bootstrap `--compare`, optional second
judge, retrieval sweep experiments file (query-side only, `reingest:false`).
Follow-up (worker, small): rank arms by the answerable-only column, not `ok+0.5*partial`.

### Phase 1: a fair test corpus on stack2 (Hermes, operator go)
All 6 loop transcripts plus the 4 other production documents (distractors), ingested at the
current chunker; gold evidence quotes per probe (a person or Hermes marks them, content
stays on the host); 60-100 questions, dev/held-out split frozen before any arm is run and
held-out read-only. Gate: corpus chunk count must exceed `top_k` clearly (>> 40).
Output: baseline C0 with evidence_in_context%, answerable accuracy, attribution,
refusal columns, 3 draws.

### Phase 2: retrieval sweep (stack2, no re-ingest, hours)
TOP_K, CHUNK_TOP_K 20/50/100, entity/relation budgets, rerank on/off, on the Phase 1 corpus.
Purpose: find whether candidate recall or chunk content is the bottleneck, and set the
query-side defaults. Adopt only when the paired CI excludes zero.

### Phase 3: embedder-sized, turn-aligned chunks (the main bet)
Arm A: chunks of ~300-450 tokens that respect speaker-turn boundaries, a ~20-30 token
deterministic header (meeting, date, speakers), via LightRAG's per-request `chunking`
(recursive/custom) or the `transcript_prep` path. Arm A0 (placebo): same chunks, shuffled
header from another document. Compare against C0 on Phase 1 corpus.
Requires a worker task: verify LightRAG 1.5.7 `custom` chunker contract, whether embedded
text can differ from stored text, and what happens to long single turns (T1-style paragraphs
need a sentence/paragraph fallback, not a token cut). Alternative if chunk size cannot be
reduced cleanly: an embedder with a real context >= chunk size (operator decision; changes
every vector, full reindex).
Expected: more searchable text per chunk, ~3x more chunks, longer ingest. Gate: evidence_in_
context% up with answerable accuracy not down.

### Phase 4: document-level context (only if Phase 3 shows a signal)
Per-document title/date/company header, one summary unit per document (RAPTOR-lite),
query-time neighbour-turn expansion. LLM-written per-chunk headers only with the placebo
arm, and only after the cheaper deterministic versions are measured. Topic segmentation
last. Whole-meeting-in-context reading for small corpora is a candidate alternative to test.

### Phase 5: release (Hermes, operator go)
Winning settings go to production through the normal release path: rc tag from master,
tarball, backup of `state/ingest.db` and `lightrag_store/` (manifest, proven restore),
reindex in a window the operator names (chunking change = full reindex of the growing
corpus), rollback = restore the backup. Never `--allow-production` for the loop.

## Parallel track: fixes independent of chunking (worker, master)
Priority order: (1) stop deleting `[hh:mm:ss]` globally and add a splitter or explicit
failure for >= 500 KB texts; (2) UTF-16/BOM-aware decoding; (3) bearer auth on `/api/*`
(operator decision: single-user LAN, key rotation was deferred); (4) Sources shows used,
not retrieved, references and not on refusals; (5) 409-on-edit handling; (6) SQLite WAL and
busy timeout; (7) docs that call unwired features "validated"; (8) repo copy of
`compose.rollback-rc2.yml`; (9) curl examples with :8000 vs 8020.
Each gets a brief, tests, bare-env suite, lead review.

## Decisions needed from the operator
1. Phase 1 corpus: OK to use all 6 transcripts + the 4 other documents on stack2, and who
   marks gold evidence (Hermes recommended).
2. Chunk-size route for Phase 3: smaller chunks (recommended) vs a different embedder.
3. Reindex window for Phase 5, accepted only after a Phase 3 signal.
4. Order of the parallel track relative to the experiments.

## Expectations and failure reading
Likely: Phase 2 gives small gains; Phase 3 gives a measurable evidence-in-context gain.
Unproven: any gain on noisy meeting transcripts. If evidence_in_context% does not move,
the bottleneck is generation (small local models, citation of evidence) and chunking is
not the lever.

## Open uncertainties
Placebo header vs real header at 512 tokens; whether small local models limit extraction;
whole-meeting reading vs chunked retrieval; LightRAG 1.5.7 `custom` chunker and
parent-expansion support (unchecked); several UNVERIFIED citations in the research file.
