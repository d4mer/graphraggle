# Gate 4 — go / no-go (release rc-1 to first use)

Prepared by lead, 2026-10-12. Decision owner: operator. Plan: `docs/ops/release-plan-first-use.md`.

## Recommendation
**GO for supervised first use (Gate 5 hypercare, one week)**, with the three known quality limits below accepted explicitly and two small
pre-use actions. Not a go for unsupervised reliance on answers to questions the documents may not answer.

## What is true in production today
- Gateway and ingest worker run the same rc-1 build (p24, image 4629dcab0bea). LightRAG v1.5.7. Corpus: 68 documents (63 processed).
- Office/PDF ingestion works again (fixed 2026-10-11: LightRAG gets its own input dir and `INPUT_DIR` matches `--input-dir`). A `.docx` and a `.pdf` canary both
  reached `ingested`, were queryable, and were removed. Before the fix every `.docx`/`.pdf` dropped in `source_docs` failed.
- Repo `master` matches production configuration (compose reconciled, guard test), except the OpenWebUI API key, which stays only on the host.

## Gate evidence
| Gate | Result |
|---|---|
| 0 Freeze / merge | master = rc-1 line, tag `rc-1`, all work pushed. |
| 1 Probe set | 36 probes with provenance, closed by operator. |
| 2 Quality baseline | 3 draws x 36 probes, production. 0 empty answers, 0 non-2xx, 0 keyword-fallback cells. |
| 3 Resilience | Backup/restore drill done. Reboot drill done (stack back in about 1 minute). Worker update to rc-1 done with rollback prepared. |
| Post-gate fixes | Task-prompt cap shipped OFF by default (packet-24); worker update; LightRAG input-dir fix; compose reconcile (packet-26). 479 tests pass. |

## Quality baseline (automatic scoring, substring key-fact checks; no judge model)
| Question type | Pass rate (mean of 3 draws) |
|---|---|
| single_fact | 1.00 |
| follow_up | 0.89 |
| multi_fact | 0.83 |
| unanswerable | 0.67 (correct refusal in all 3 draws: 1 of 3 probes) |
| cross_document | 0.56 (range 0.50-0.67) |

Latency: median 178 s, p95 237 s per answer. Bridge decode runs about 58 tok/s; first token about 46 s (32k-token prompts).

## Remaining risks you are accepting
1. **Invented answers when the documents do not contain the answer.** Probe p35 answered instead of refusing in 2 of 3 draws, p34 once.
   Users must verify claims that matter against the source document. Mitigation exists only as a prompt/tuning item, not yet built.
2. **Weak cross-document questions (0.56).** Questions that need facts from several documents are right about half the time. Single-document questions are reliable.
3. **Slow answers (about 3 minutes).** The measured decode and first-token times explain about 80-90 s; the rest is unexplained (no persistent query log;
   the operator declined enabling `QUERY_LOG_PATH`). Not a correctness issue, but it affects feel.
4. **Memory pressure on the oMLX box.** Embeddings, reranker and the LLM share one machine with swap about 89% full. Concurrent heavy requests slow decode.
   No policy decided (GRAG-42).
5. **Small items:** the empty-answer retry path was exercised once, not extensively; `pipeline_status` hangs on the Thinkpad (do not use as a health check);
   Thinkpad agent approval prompts are invisible to the operator and time out; 11 old rows remain `failed` in the ingest DB and 5 failed stubs in LightRAG
   (all triaged as duplicates or test artifacts, nothing missing, left as is by decision); the sigma-to-Thinkpad VPN dropped for about 70 minutes after the reboot drill, cause unknown.

## Do before first use
1. **Rotate the OpenWebUI API key** (`OPENAI_API_KEYS` in the Thinkpad compose). It was printed in a terminal diff once. Never committed.
2. **One real-document check:** drop one real `.docx` in `source_docs`, confirm it shows `ingested` and appears in the LightRAG web UI under the current date.

## Rollback (nothing after the freeze deploys without a new go)
- Worker: `docker compose -f compose.yml -f compose.rollback-worker.yml up -d --no-deps ingest-worker` (image `local/rag-gateway:pre-packet-18`).
- LightRAG config: restore `backups/compose.yml.pre-inputdir2-20261011-110558Z` over `compose.yml`, then `docker compose up -d --no-deps lightrag-server`.
  This returns to the state where Office/PDF files fail.
- State DB: backup `backups/pre-worker-update-20261010-075725Z/ingest.db`.
- Corpus/index: backup `backups/rc1-20261009-082525` (restore drilled).
- General: capture evidence first, rollback before diagnosing (`docs/ops/rollback-and-containment.md`).

## Hypercare plan (Gate 5)
Operator logs each bad answer (question, expected, got). Lead triages weekly into bug, tuning flag, or accepted limitation. First candidates to tackle from the
log: refusal behaviour on unanswerable questions, cross-document retrieval, and the latency gap.

## Decision
**GO**, given by the operator on 2026-10-12: proceed with first use of rc-1 (Gate 5 hypercare begins). Pre-use actions (key rotation, real-document check) remain open.
