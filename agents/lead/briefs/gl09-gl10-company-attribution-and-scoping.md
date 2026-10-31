# Worker brief — GL-09 + GL-10: company attribution and scoped retrieval (Plane GRAG-64, GRAG-65)

Base: branch from current `origin/master` (check `git fetch && git rev-list --left-right --count HEAD...origin/master` = 0 0 first). Own branch/worktree, deleted after merge.
You do NOT read corpus or LightRAG content (Hermes only). You may read repo code, tests, and docs. Nothing is deployed by you.

## Why (read this first)
The product's purpose is cross-company insight: "how did company A do something company B is now trying". Rules decided by the operator (2026-10-31):
- A question that NAMES a company is answered from that company's documents. No company named = retrieval spans ALL companies (this is the default and the point).
- Every cited source shows its company, as `[Company: document]`, and cross-company answers group their evidence by company.
- Accuracy matters more than speed. No timeout or rerank fallback may be introduced. The only user is the operator.
Gates that will judge this work: scoped probes return 0 sources from other companies; labelled probes show 0 mis-attributions; a file present in two companies is retrievable and attributed to both.

## What exists today (verify in code, do not trust this list)
- `documents.company` + `company_source` in `app/state_store.py`, inferred from the first directory under `source_docs/` (`build_company_attribution`).
- `app/api.py: scope_query_citations` filters gateway-returned citations by exact company match AFTER LightRAG answered, and its own warning says it is not isolation.
  Sources block (`format_sources_block`, ~l.185) prints `N. file — Company` (not `[Company: file]`, not grouped).
- `COMPANY_AWARE_DEDUPE` (default false) lets a company-scoped doc through when the only SHA match is another company's.
- Structure chunk header is `[Doc: title | date | section | Part k/n]`: it carries no company.
## The risk to solve
Filtering citations after generation does not stop the ANSWER from being written from the other company's context. Scoped questions must restrict what the model SEES, not only what is listed. Also, a document with the same content in two companies must stay retrievable under both.

## Do, in two stages (STOP after stage 1 and hand back the design; do not start stage 2 until the lead replies)
### Stage 1: design note (no app code)
Write `docs/decisions/gl09-gl10-company-scoping-design.md` (short). It must answer, with evidence from the code and from LightRAG 1.5.7 behaviour that is already documented in the repo/Prism notes (`docs/ops/` probes); where a fact needs a live probe, list it as a question for Hermes instead of guessing:
1. Where the company should live so it survives chunk -> entity/relation -> citation: header text, `file_path`, LightRAG doc metadata, or the gateway's state DB lookup. Compare at least these options on: effect on chunk hashes (a header change would force re-ingest of the 11 live documents), effect on entity extraction, and how a citation maps back to a company.
2. How scoped retrieval is enforced BEFORE generation (for example restricting to a document-id set via LightRAG's query params, or pre-filtering retrieved context, or a per-company query pass), and what happens to graph (entity/relation) context, which spans documents.
3. How the gateway detects a company name in a question: the list of known companies comes from `documents.company`; define matching (case, aliases, ambiguity such as two names in one question = cross-company comparison, treat as multi-company scope, not a refusal), and what it does when the name is not a known company.
4. Output format: `[Company: document]` per source, grouped by company for multi-company answers; how it interacts with the existing Sources block and the OpenWebUI citation path.
5. Duplicate files across companies with `COMPANY_AWARE_DEDUPE` ON: how both stay retrievable, and the entity-duplication risk.
6. A test plan: unit tests and the probe-set shapes you need Hermes to run (scoped, labelled-attribution, cross-company).
7. Migration cost for the live store: does the recommended option require re-ingesting the 11 documents? If yes, say what that costs and whether an option avoids it.
Recommend one option and say why. Counts only; no corpus text.

### Stage 2: implement (only after the lead approves the design)
Implementation with tests under `tests/`; flags default so that current production behaviour is unchanged until switched on; the full suite must pass bare:
`.venv/bin/python -m pytest tests/ -q`. No production deploy; Hermes tests on stack2.

## Hand-back (both stages)
Branch + patch path, files changed, test counts, any open question for Hermes. `rig queue create --destination orch-lead@graphraggle` with a summary.
