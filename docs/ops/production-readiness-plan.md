# GraphRaggle production-readiness plan (2026-10-31)

Status: DRAFT for operator approval. Owner: lead. Executor of production steps: Hermes. Nothing here changes production until the operator
approves the plan and then gives a separate GO per production-changing step.
Tracking: Plane project GRAG, module "Production go-live", label `go-live`. Task GL-nn is GRAG-(55+nn), so GL-01 = GRAG-56 and GL-36 = GRAG-91.

## 0. Purpose and current state
Purpose (operator-confirmed): companies are NOT isolated. The point of GraphRAG here is the interconnections between companies, for example how one
company did something another company is now attempting. A user who names a company gets that company's information; an open question retrieves across
all companies. Every fact in an answer is attributed to its company and document.

Original functionality: documents go in `source_docs/<Company>/`; the worker ingests them; users ask in Open WebUI through the gateway.

State on 2026-10-31 (verified): the structure-ingested Solvay store is live (11 documents, 135 chunks, 1,483 entities, 1,867 relations), recall 86/100, p50 about 85 s,
p95 about 128 s. It works, but it has only ever held one company, so cross-company insight has never been tested. Gaps: no company attribution in answers, no cross-company
evaluation, no automatic ingest safety, unmeasured recall on new content, slow answers, a plaintext key, no restore drill, no soak under real use.

## 1. The go-live moment
GraphRaggle is READY TO GO LIVE at the instant all of these are true:
1. Every task GL-01 to GL-35 is Done and every gate G1 to G12 (section 4) is PASS, each with an evidence line in `docs/ops/runs/readiness-gates.md` (counts, ids, hashes, timings; no corpus text).
2. The lead has written `STATUS: READY FOR GO-LIVE <UTC timestamp>` as line 1 of that file, after re-running G2, G4, G5 and G6 within the previous 24 hours.
3. The last step, key rotation (GL-34), has completed and G12 passes after it.
The GO-LIVE MOMENT is the timestamp of the operator's written "go live" reply to that line (GL-36). Before it: pilot users only. After it: normal user admission and
normal ingestion of new companies. A later gate failure sets the status to NOT READY and pauses admission and ingestion.

## 2. Operator decisions (block the work)
- GL-01 scoping: how a user limits a question to one company. Recommendation: the gateway detects a company name in the question and narrows retrieval to it; no name means all companies.
- GL-02 attribution: every cited source reads [Company: document]; cross-company answers are grouped by company.
- GL-03 accuracy bar. Recommendation: ok% at least 88 on a NEW held-out set per company, 0 hallucinations (or at most 1 explained), refusal correctness at least 90%, plus gate G4c.
- GL-04 latency bar. Recommendation: p50 at most 60 s, p95 at most 120 s.
- GL-05 pilot users, and which second-company documents may be used for testing on stack2 (only Hermes reads them).
- GL-06 freeze: push tag `structure-cutover-2` (0047a69).

## 3. Tasks, in order
### A. Decisions and freeze: GL-01 to GL-06
See section 2.

### B. Cross-company correctness on stack2 (production untouched): GL-07 to GL-12
- GL-07 second-company test corpus on stack2 with duplicate files across companies and at least 10 shared-process topics. Done: ingested, counts-only report.
- GL-08 test `COMPANY_AWARE_DEDUPE=ON`. LightRAG dedupes by content hash across the whole store; with the flag OFF an identical file from a second company is dropped and that
  company can never be cited for it. Done: an identical file in two companies is ingested once and attributable to both, with no extra duplicate entities. The flag stays OFF in production until this passes.
- GL-09 company attribution from chunk to the Sources block (related: GRAG-23 chunk headers, GRAG-26 chunk metadata). Done: mis-attribution 0 on labelled probes; full suite passes.
- GL-10 scoped retrieval in the gateway (related: GRAG-31). Done: scoped probes return 0 sources from other companies; unscoped questions still search all companies; full suite passes.
- GL-11 cross-company retrieval. Done: on the cross-company probe set at least 80% of answers cite every company that holds evidence, with 0 invented links.
- GL-12 no regression: 100 Solvay probes at least 86/100, no new hallucination.

### C. Probes: GL-13, GL-14
Fresh held-out set per company (at least 30 each, unanswerables included); a scoped set and a cross-company set with expected companies labelled. Authored by dev-worker from source material, run by Hermes, counts only.

### D. Ingest safety: GL-15 to GL-18
Pre-ingest snapshot script with off-host copy; post-ingest verifier with numeric pass criteria; failure drills (corrupt PDF, empty DOCX, 0-byte file, duplicate, oversize); ingest runbook including onboarding a new company.

### E. Quality and latency (each change needs its own operator GO and is A/B-tested on stack2 first): GL-19 to GL-22
Rerank HTTP timeout patch; EMBEDDING_TIMEOUT raise and reranker throughput re-test; choose final settings (RELATED_CHUNK_NUMBER, timeouts) against GL-03/GL-04 on the Solvay, scoped
and cross-company sets (R=3 gave 29/34 held-out at p50 about 244 s, too slow); validate or drop the heading fix `f7051e6`.

### F. Recovery and operations: GL-23 to GL-27
Restore drill from deltahome; rollback drill of `compose.rollback-cutover.yml` on stack2; monitoring and alerts that reach the operator; backup schedule (nightly local, weekly off-host, OpenWebUI data included); rule for who may use the shared Sigma model during user hours.

### G. Hygiene: GL-28, GL-29
GSK leftovers (quarantine of 56 files, sealed deltahome `graphraggle-20261012`, OpenWebUI 0.11.4 tar, chat history); permissions `.env` 600 and `compose.yml` 640.

### H. Production window and soak: GL-30 to GL-33
Apply final code and settings in one reviewed window; full regression on production; second company's first batch through the runbook; 72-hour soak with pilot users and no changes.

### I. Go-live: GL-34 to GL-36
GL-34 key rotation (operator's last step); GL-35 readiness check and the READY line (lead); GL-36 the operator's written "go live".

## 4. Gates (evidence in readiness-gates.md)
| Gate | Requirement | Measured by |
|---|---|---|
| G1 Scope | Operator decisions GL-01 to GL-05 recorded | operator reply |
| G2 Health | :9621, :8020, open-webui 200; restart counts 0; all documents processed, 0 failed | Hermes snapshot |
| G3 Recoverability | Restore and rollback drills passed; off-host copies under 7 days old, sha256 clean | drill logs |
| G4a Scoping | Scoped probes: 0 sources from other companies | probe counts |
| G4b Attribution | Labelled probes: every cited source carries the right company; mis-attribution 0; a file in two companies is retrievable and attributed to both | probe counts |
| G4c Cross-company | At least 80% of cross-company answers cite every company holding evidence, grouped by company, 0 invented links | probe counts and Hermes judgment |
| G5 Accuracy | Fresh held-out set per company at the GL-03 bar | probe counts |
| G6 Latency | p50/p95 within GL-04 on Solvay, scoped and cross-company sets under normal Sigma load; 0 of 100 ending in rerank fallback | timings, log scan |
| G7 Ingest safety | Snapshot and verifier drilled; all five failure cases end clean | drill logs |
| G8 Live ingest | A real new-company batch ingested through the runbook; verifier clean; regression unchanged | GL-32 report |
| G9 Monitoring | An alert fires in a test and reaches the operator | alert test |
| G10 Soak | 72 h with pilot users: 0 outages, 0 failed documents, error rate under 1%, no open high-severity defect | daily counts |
| G11 Hygiene | GSK items decided and executed; no plaintext secrets outside .env; permissions correct | file audit (names only) |
| G12 Key | Rotation done; old key rejected; a query works through Open WebUI | test after GL-34 |

## 5. Rules while this plan runs
- No production change without a per-step operator GO, a snapshot and a rollback. The lead does not deploy.
- Evidence is counts, ids, hashes and timings; no corpus text. Only Hermes reads corpus content.
- If a gate cannot pass, the lead reports the failing number and the cheapest options. Only the operator changes a target.
- The lead keeps `master` clean, commits briefs the same day, and updates Prism and the Plane tasks at the end of each group.

## 6. Timeline and risks
Best case about 17 days elapsed, set by the 72-hour soak and Sigma contention. Large PDFs and the second company's ingest can lengthen it.
Main risks: (1) attribution and scoping need real gateway and chunk-header code (days, not hours), and entity merging across companies may blur company-specific facts;
(2) latency may not reach the GL-04 bar on this hardware, since cross-company queries retrieve more; (3) the shared Sigma model can invalidate timing runs;
(4) `COMPANY_AWARE_DEDUPE` was kept off for a reason, so GL-08 must prove it before it is enabled.
