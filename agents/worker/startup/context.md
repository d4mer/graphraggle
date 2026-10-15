# Handoff — worker

Last updated: 2026-10-15 (rc-2 docs item **handed back uncommitted on master**;
nothing else owned)

## Current state

`qitem-20261015090325-ec23f24e` — record the rc-2 production release in the docs
(master, docs only). Uncommitted: `docs/05-known-good-config.md` +11/-4 (runtime table:
BOTH gateway and worker now `local/rag-gateway:rc2` / image `d0f31398fec5` / commit
`11b1833`, previous `4629dcab0bea` tagged `pre-rc2`; the stale "worker on pre-packet-18
`d9d9ce2307a2`" row corrected — it ran rc-1 since 2026-10-10; header now "verified
2026-10-15"; Open WebUI key drift recorded as an **accepted risk**, no rotation
scheduled, no key values written anywhere), `docs/08-change-log.md` +7/-2 (Production
Releases item 9 for rc-2 — image, tarball, backup
`~/rag-project/backups/pre-rc2-20261015T083456Z/`, what is live, rollback (gateway
retag; worker via `compose.rollback-rc2.yml`); Pending rewritten: old items 1 and 4
struck with the corrected premise, note that `COMPANY_AWARE_DEDUPE` stays off on
purpose), `docs/04-troubleshooting.md` +50 (two sections: 401 on `GET /ingest/status`
— bearer guard predates rc-2, verify the auth **and** the port (host 8020 → container
8000, docs examples use :8000), the unexplained unauthenticated 200 recorded as
unestablished; and "The ingest worker logs nothing on a clean scan" — `warn()` prints
only `WARNING:` to stderr, refusals come from the `gateway.state_store` logger,
liveness via `docker stats --no-stream`, `docker inspect -f '{{.State.Status}}
{{.State.StartedAt}}'`, `stat -c %y ~/rag-project/state/ingest.db`, or POST /reindex and
watch `summary.by_status` / rows' `updated_at`).
Suite: `env -i PATH=$PATH HOME=$HOME .venv/bin/python -m pytest tests/ -q` →
**`698 passed, 2 warnings, 6 subtests passed in 4.42s`** (master baseline unchanged; no
test reads docs files).
Reported-not-fixed: several curl examples in docs/03, 04, 09 use
`http://localhost:8000` while production publishes the gateway on host **8020** — a
docs inconsistency outside this brief.

Earlier today: the transcript-prep chunking item (`qitem-20261015074912-069626d5`) was
handed back uncommitted in the worktree `/home/maverick/work/graphraggle-chunking`
(branch `chunking`) and the lead committed it as **`b535792`**; worktree suite
**726 passed** (baseline 698 + 26 + 2). Design points, in case of rework: token
estimate `max(chars/4.0, words)` (tiktoken lives in LightRAG's image, not this venv;
200-token cadence vs 1200-token chunks = 6x margin); turns longer than 2 budgets split
at sentence terminators with the speaker label repeated; detection needs >=3 label
lines, >=2 distinct speakers with one recurring, labels >=30% of non-empty lines (or
>=3 subtitle cues); `Q:`/`A:` and Note/Step/Action-style labels are stoplisted;
caption-only output is stable but not re-detectable. Coverage script kept at
`/tmp/coverage_report.py` (synthetic ~5,000-token transcript: 30 headers, 15% payload
growth, spacing mean 158 / max 164 estimated tokens, 5/5 windows at 1200 tokens contain
a header, largest header-free window 0).

Earlier items (all committed by the lead on master): Open WebUI 0.12.0 pin,
company-aware dedupe behind `COMPANY_AWARE_DEDUPE` (do-not-enable per the live
probe), lifecycle cleanup, TODO #2/#3, conftest.

`qitem-20261014135220-06657d8a` — pin Open WebUI 0.12.0. **Uncommitted:**
`compose.yml` (image tag `0.11.4` → `0.12.0`), `docs/05-known-good-config.md`
(production runtime table row + changed-date note), `docs/08-change-log.md`
(forward pointer on the 2026-10-11 ports/images item + new dated Production
Releases entry). Suite: bare-env **`698 passed, 2 warnings, 6 subtests passed in
3.52s`**; `tests/test_compose_config.py` alone **`12 passed`**. Briefs under
`docs/briefs/` left alone (historical records). I annotated rather than rewrote
the dated 2026-10-11 item — flagged in the hand-back.

**Dedupe follow-up context (lead already documented it at `13d17ba`):** the live
probe answered the upstream question — LightRAG v1.5.7 dedupes by content hash
globally; an identical-bytes second submit returns 200 with a new track that
becomes a `dup-*` **FAILED** placeholder (0 chunks, never PROCESSED). So the
cross-company guard I built is effectively dead code (its PROCESSED-without-our-path
branch never fires); the real harm if `COMPANY_AWARE_DEDUPE` is ever enabled is
`lightrag_processing_failed` + junk `dup-*` rows upstream. Flag stays do-not-enable;
real fix = query-time attribution (the lead's own item).

Previous item (company-aware dedupe behind `COMPANY_AWARE_DEDUPE`, default off) is
committed by the lead; see `docs/05-known-good-config.md` § Settings Shipped Dark.

`qitem-20261014085257-ed1c3f7a` — company-aware duplicate selection, rulings
followed. **Uncommitted** in this checkout on `master` (base `7b664f1`):
`app/config.py` **+10**, `app/worker.py` **+157/−3**, `app/api.py` **+3/−1**, new
`tests/test_company_aware_dedupe.py` (**314 lines / 17 tests**).
- `COMPANY_AWARE_DEDUPE` (default **false** = byte-identical to today).
- `pick_duplicate_candidate(documents, current_path, *, company=None)`: when the
  flag is on, a candidate may suppress only if the new doc is unscoped or the
  candidate is the same company; `normalize_company(a) == normalize_company(b)`,
  **no case folding** (comment says both sides must change together).
  Call sites: `app/api.py` `/upload` passes `normalized_company`;
  `app/worker.py` passes the row's stored company.
- Marker + guard: `cross_company_guard_updates()` writes
  `cross_company_guard=verify_file_path_on_process` (+ matched path/company) only
  for rows the flag released past another company's SHA match; `poll_track`'
  PROCESSED branch calls `cross_company_guard_mismatch()` — marked rows need a
  `documents[].file_path` matching the submitted path (exact, or basename for
  `/documents/upload`) or they land on `error_code=cross_company_not_indexed`,
  `query_ready=False`. Unmarked rows are untouched.
- Tests: bare-env **698 passed, 2 warnings, 6 subtests passed** (also with
  `COMPANY_AWARE_DEDUPE=true` across the whole suite → no existing test encoded
  company-blind suppression). Behavioural BEFORE/AFTER control:
  `/tmp/control_company_dedupe.py`; 409 probe + self-heal probe run inline (see
  hand-back note). pyflakes: only the 3 pre-existing `app/api.py` findings.
- Backups while testing: `/tmp/p40-fix/{config,worker,api}.py`.

**409 finding (reported, not changed):** a 409 from `/documents/text` or
`/documents/upload` today → `failed` / `error_stage=submit` /
`error_code=submit_terminal_failed`, `retryable=False`, and because
`submit_terminal_failed` is in `NON_RETRYABLE_FAILURE_CODES` every later scan
returns before retry bookkeeping (`retry_count` stays 0) → the row is stuck at
`failed` until an operator reindex; `error_detail` is only `str(exc)`
(`Client error '409 Conflict' for url`) so the body that names the filename
conflict is lost.

**Undocumented setting (flag, don't fix unasked):** there is no `.env.example` or
settings reference doc; env vars live in per-packet briefs under `docs/briefs/`.
`COMPANY_AWARE_DEDUPE` is currently documented only in the code comment + the
lead's brief — ask whether an ops doc line is wanted.

Previous items all committed by the lead: `f3ca463` lifecycle cleanup,
`b7423b2` TODO #3, `224cea9` TODO #2, `6f523b7` conftest.

**Earlier round, superseded by the build above — `qitem-20261014083709-86837b07`
(company-aware duplicate selection), investigation-only, NO code changed.**
Findings (all
measured with `/tmp/probe_dedupe_company.py` — real `state_store` + real
`worker.handle_candidate`, network mocked to raise):
- Duplicate selection is company-blind by signature: `pick_duplicate_candidate(
  documents, current_path)` at `app/worker.py:269`, two call sites only
  (`app/api.py:1120` `/upload`, `app/worker.py:988` scan). `app/validation.py`
  takes `duplicate_of` from the caller — it decides nothing itself.
- B-upload of A's bytes → row stays `accepted` / `error_code=duplicate_content` /
  `query_ready=0`, **0 upstream submits after 3 scans**. Citation attribution is
  path-based (`enrich_citation` matches LightRAG `file_path` → gateway row →
  copies its company; `scope_query_citations` `app/api.py:851` keeps only
  `document.company == requested_company`), so B can never cite it.
- **Upstream same-content second submit = unknowable offline** (thin transport
  client, no `lightrag` in `.venv`, no recorded submit response in `docs/`, and
  **no repo doc says the doc id is content-derived** — the lead's premise is
  unverified). From our code: case-1 indexes B → fixed; case-2 no `track_id` →
  `failed`/`submit_missing_track_id` after `ingest_max_retries` (default 3,
  `worker.py:1059` stops — no infinite loop); **case-3 PROCESSED without B's
  `file_path` in chunks → we write `ingested`/`query_ready=True` while B-scoped
  queries return nothing = strictly worse than today**. Hence the stop.
- Recommendation sent: (a) company-aware picker **+** guard — when a track reports
  PROCESSED require a `track_status documents[].file_path` matching the submitted
  path (shape recorded in `docs/briefs/packet-32-loop-shapes.md:12`), else write a
  visible error (e.g. `upstream_indexed_elsewhere`) instead of `ingested`. Plus a
  2-call live probe (same tiny file twice via `/upload`, company A then company B,
  then GET both `track_status`).
- Stuck rows self-heal under (a), **no Hermes reindex**: `app/worker.py:995`
  `orphaned_duplicate` clears the flag, resets `retry_count=0`, submits with
  `retry_reason=duplicate_original_removed` (measured with only the picker
  monkeypatched).
- **Brief premise corrected:** `normalize_company` only `strip()`s — no case
  folding — and query scoping compares the RAW stored company with `==` against a
  stripped request (`api.py:851` vs `:1270`), so "Acme Corp" vs "acme corp" do NOT
  agree today. Ruling needed: mirror scoping exactly (my default) or fold case in
  BOTH the picker and `scope_query_citations` (query-time change → its own item).

**The three decisions the lead owed — all ruled (brief `7b664f1`):** ship picker +
guard **dark** behind `COMPANY_AWARE_DEDUPE` (default false); **no case folding**
(mirror `scope_query_citations`); the live probe is the operator/Hermes' job, not
mine.

Previous item, **committed by the lead at `f3ca463`**:
`qitem-20261014082513-5cb9b9e6` — lifecycle cleanup. Diff: `app/state_store.py`
**+58/−15**, `app/worker.py` **+28/−2**, `docs/decisions/0001-lifecycle-model.md`
**+77/−1**, `tests/test_lifecycle_cleanup.py` **new 221 lines / 7 tests**,
`tests/test_state_store_lifecycle.py` +5/−3, `tests/test_worker_dedupe.py` +5/−1.
What changed: (1) `increment_retry(path, *, expect_track_id=, expect_status=)` —
CAS'd retry counting, shared `_cas_clause()` helper (also used by
`update_document_state`); worker's two call sites in `handle_candidate` pass the
snapshot's track_id/status. (2) `submit_file`'s two post-submit **failure**
write-backs carry `expect_status="accepted"`; the **successful** upsert is
deliberately unguarded (comment in code: dropping it would strand a live track;
its stale `warnings_json` overwrite is the known residual). (3) `update_status()`
deleted (no app/ caller) — its two test uses moved to `update_document_state`.
(4) `READ_DERIVED_STATUSES = frozenset({"superseded"})` + comment: nothing in
app/ writes superseded, it is derived by `annotate_documents` /
`derive_readiness`; behaviour unchanged. (5) ADR 0001 now documents the enforced
table, the CAS rule, and what a refused write looks like in the log.
**Test edit the lead must know about:** `tests/test_worker_dedupe.py`
`_FakeStateStore.increment_retry(self, path, **kwargs)` — signature widened only;
the fake ignores the CAS (assertions untouched), the real CAS is covered in the
new file. Tests: bare env **`681 passed, 2 warnings, 6 subtests passed in 3.19s`**;
six vars exported **`681 passed … 3.01s`**; pre-change control (`git stash push --
app/worker.py app/state_store.py`) → **`4 failed, 677 passed`** — the 4 are in the
new file (2 REPRODUCTION + the new-signature + the READ_DERIVED marker test);
existing suite green pre-change too. pyflakes: only the 3 pre-existing `app/api.py`
findings. App backups while testing: `/tmp/p39-fix/{worker,state_store}.py`.

Earlier this session, both committed by the lead: TODO #3 (`b7423b2` — one
duplicate rule + one skip rule; verdicts: item 1 real bug, item 2 real bug, item 3
intended asymmetry, item 4 intended per ADR 0004; new finding = company-blind
duplicate selection, which the lead will brief separately — do not fix it), TODO #2
review follow-up and TODO #2 (`224cea9` lifecycle table + CAS), conftest env
defaults (`6f523b7`).
`qitem-20261014081856-2ec58157` — **TODO #3 upload vs source-doc ingestion
semantics**. Files: `app/api.py` **+10/−3**, `app/worker.py` **+18/−5**, new
`tests/test_upload_vs_scan_semantics.py` (287 lines, 7 tests). Fixes: (1) `/upload`
now uses the worker's `pick_duplicate_candidate(await get_documents_by_sha256
(sha), str(dest))` instead of `get_document_by_sha256` (newest row of ANY status,
including `duplicate_content` ones) — response shape untouched, the API keeps its
own 4-key `duplicate_of`; `app.api` now imports `app.worker` (same image for
gateway+worker, and `pptx` is a lazy import inside `extract_pptx_text`, so no new
dependency at API startup). (2) `should_skip_path` gained always-on rules for
BOTH doors — suffix in `INGEST_SKIP_SUFFIXES = {".part"}` and `is_metadata_file`
— and `scan_once` calls it **before** the unsupported-extension branch, so a scan
landing in `/upload`'s `.<document_id>.part` window no longer writes a `rejected`
row for a path that then vanishes. Verdicts: item 1 real bug (fixed), item 2 real
bug (fixed, reproduces), item 3 intended asymmetry (door re-validates with the
measured post-stream size; empty/large verdicts cannot differ; auto_split lands
the same both doors → readiness `split_required`), item 4 intended per ADR 0004
(path inference is defined for `source_docs/` only) — noted, not changed. New
finding reported, not fixed: duplicate selection is **company-blind in both
doors**, so a companyB upload can be marked `duplicate_content` of a companyA row
and never submitted. Tests: bare env **`674 passed, 2 warnings, 6 subtests passed
in 2.94s`** (baseline 667, +7); pre-change control (`git stash push -- app/api.py
app/worker.py`) → **`3 failed, 4 passed`**, exactly the 3 REPRODUCTION tests.
pyflakes: only 3 pre-existing `app/api.py` findings (`compute_adaptive_hops`
unused, `has_weak_answer_signal` redefinition, `weak_reason` unused) — present on
HEAD too, left alone. My app copies while testing: `/tmp/p38-fix/{api,worker}.py`.

Previous item (TODO #2 review follow-up, since committed as `224cea9`):
`qitem-20261014074257-239bc5fb` — TODO #2 review follow-up. `LEGAL_TRANSITIONS`
now allows **`ingested -> rejected`** (a file that changed on disk and no longer
validates really is rejected); `ingested -> processing` / `ingested -> failed`
stay denied, and the table comment now says why: the late-write protection is the
**poll CAS**, not the table. Evidence test
`test_submit_file_never_writes_onto_an_ingested_row` patches `worker.submit_file`
and shows the row is **`accepted`** (written moments earlier by the same scan's
`content_changed -> validating -> accepted` chain) when submit_file's reject/
failure writes land — so those are `accepted -> rejected/failed`, never
`ingested -> ...`. **The regression as described did not reproduce** on my
previous diff: the end-to-end test settles `rejected` with the new sha,
query_ready 0, unchanged on a second scan, on both versions. The two tests that
DO fail pre-fix are the table test and the store-level `ingested -> rejected`
write (which logged `write for /docs/a.txt ignored: status=rejected ... row now
(status=ingested ...)`). My CAS answer to the lead: not needed on submit_file's
post-HTTP writes now — same sequential scan owns the row, API reindex is the only
concurrent writer and lands on legal edges, and CAS without a pre-submit
reservation would strand live upstream tracks.
**Baseline is now 667 passed** (25 tests in
`tests/test_state_store_lifecycle.py`, 462 lines). Files: `app/state_store.py`
**+189/−11**, `app/worker.py` **+27/0**, new test file. Backups: `/tmp/p37-fix/
{state_store.py,state_store_v2.py,worker.py}`.

Earlier item, same work, reviewed OK by the lead: `qitem-20261014072738-31fd9e17`
— **SQLite status transitions + stale track_id CAS** (TODO near-term #2; base
`origin/master` `e387de4`; done **in the shared checkout
`/home/maverick/work/graphraggle` on branch `master`**, left uncommitted — three
files, do not lose them to a branch switch):
- `app/state_store.py` **+180/−11** — `STATUSES` (the 9 ADR-0001 statuses) +
  `LEGAL_TRANSITIONS` + `is_legal_transition()` / `legal_sources_for()` /
  `_status_transition_clause()`. Enforcement lives in the SQL `WHERE` (atomic,
  no read-then-write): `update_document_state` refuses an illegal status edge and
  any unknown status value; `upsert_document`'s `ON CONFLICT DO UPDATE` grew a
  `WHERE` guard. New optional kwargs `expect_track_id=` / `expect_status=` are a
  compare-and-set (`track_id IS ?` covers NULL). `upsert_document`,
  `update_document_state` and `update_status` now return **bool** (True =
  written); a refused write logs a `gateway.state_store` warning carrying the
  row's current status/track_id.
- `app/worker.py` **+27/0** — all **8** `poll_track` write-backs carry
  `expect_track_id=track_id, expect_status=doc.get("status")` (+ docstring).
- `tests/test_state_store_lifecycle.py` **new, 353 lines, 22 tests** — real
  state_store against a temp SQLite file, worker `client` mocked: late poll for a
  replaced track ignored; illegal edges refused (ingested→failed/rejected/
  processing, rejected|superseded→processing/ingested, never-upstream→ingested);
  unknown status refused; stale replay = logged no-op with `updated_at`
  unchanged; re-scan of an unchanged ingested file stays ingested and never
  submits.

Tests: **`667 passed, 2 warnings, 6 subtests passed in 2.88s`** bare env
(`env -i PATH=/usr/bin:/bin HOME=$HOME .venv/bin/python -m pytest tests/ -q`);
**`667 passed, … 2.94s`** with the six vars exported (master baseline 642, +25
new). New file alone `25 passed in 0.19s`. Pre-fix control (TODO #2: `git
checkout -- app/state_store.py app/worker.py`, new tests only) → **`20 failed,
2 passed`**; the 2 that also pass pre-fix are the deliberate no-regression guards
(`test_matching_poll_still_applies`,
`test_unchanged_ingested_file_stays_ingested_and_is_not_resubmitted`). pyflakes
clean. No existing test encoded an illegal transition — none edited. Pre-fix
backups of my change: `/tmp/p37-fix/{state_store,worker}.py`.

Previous item, now **merged** as `6f523b7`: `qitem-20261013201632-9e4c7ab1` —
**`tests/conftest.py` env defaults** (brief:
`agents/lead/briefs/conftest-env-defaults.md`). One new file, 58 lines, in the
**shared checkout** (`release-rc1-docs` @ `cf4a2e9`): module-scoped
`tempfile.TemporaryDirectory(prefix="graphraggle-pytest-")`, then
`os.environ.setdefault` for `RAG_API_KEY=test-rag-key`,
`LIGHTRAG_INTERNAL_API_KEY=test-internal-key`,
`LIGHTRAG_BASE_URL=http://example:9621`, and `SOURCE_DOCS_DIR` /
`UPLOADS_DIR` / `STATE_DB_PATH` under that temp dir (`source_docs/`, `uploads/`
subdirs pre-created). Results then: clean env **467 passed** on
`release-rc1-docs`; negative control (conftest removed) **4 collection errors** —
test_keywords/test_observability/test_ollama_bridge/test_rewrite; and on master
**642 passed** clean-env (**master's baseline is 642, not 467** — 467 was the
`release-rc1-docs` tree). pyflakes clean.

Live git facts re-derived this wake (trust these over older notes): **master head
= `4d341d7`** (`docs: Hermes widen-corpus brief`) and it **contains `4bf61ea`
(packet-36 ok_pct)** — packet-36 is merged, so `../graphraggle-p36` is spent.
Above my packets the lead has been working solo through a `hermes-*` brief series
(`762680c` probe authoring v3 brief, `8a2fa39` v3 validate+run, `f4a8ed1` v3
outage diagnosis + rerun, `3a832bb` "handoff after V3 accuracy target met",
`4d341d7` widen-corpus) — none of those were delegated to me. The shared checkout
is on **`release-rc1-docs`** at **`cf4a2e9`** (`docs: Hermes failure-analysis
brief for V2 baseline`, +21 lines in `docs/briefs/hermes-failure-analysis-v2.md`)
with this context file as the only local modification.

Recent items, all handed off:

0. `qitem-20261013121639-55256fd3` — **probe authoring v3** (private; counts only
   here). In `/home/maverick/work/solvay-probes/` (dir 700, files 600):
   `solvay-heldout-v2.1.jsonl` (40 lines, exactly 2 lines replaced vs v2: ids
   **ho20** and **ho39**, both rewritten as genuinely unanswerable probes on the
   same topic rather than answerable — the answers were arguable, not plainly
   present; ho20's `source_doc` also moved to the transcript the solver discussion
   is in, so per-source 21/19; mix still 18/8/4/4/6; verify passed) and
   `solvay-heldout-v3.jsonl` (24 lines, ids hv01–hv24, never run or tuned on:
   9 single / 5 multi / 3 follow-up / 3 combine / 4 unanswerable; 11 + 13 per
   source against the 14 cap; verify passed). The other four v2 unanswerables
   (ho18/ho19/ho38/ho40) were audited by absent_terms + question nouns and left
   unchanged. `verify_probes.py` now accepts the `hv` prefix (cap 14) and takes
   `--forbid-overlap-with other.jsonl ...`, which fails any evidence quote sharing
   a ≥40-char normalised run with an earlier set's quote (negative control: v2 vs
   v2.1 fails with 34 overlaps). Unanswerable `absent_terms` are verified across
   all six transcripts (0 leaks) plus a word-boundary pass on the question's topic
   nouns. Read only the six transcripts and the probe files.
1. `qitem-20261013120520-b71696f0` — **packet-36 `ok_pct` (a correct refusal counts
   in the headline score)**. Branch **`packet-36-ok-pct`**, commit **`4bf61ea`**,
   base `5de3151`. **Merged into master** (verified: `git merge-base --is-ancestor
   4bf61ea master` → yes). Worktree **`/home/maverick/work/graphraggle-p36`,
   clean, not pushed**. `scripts/overnight_loop.py` +50/−18,
   `tests/test_overnight_loop.py` +132/−2. Tests: **`642 passed, 2 warnings, 6
   subtests passed in 2.79s`** (baseline 634; +8 new). pyflakes/undef/annscan clean.
   - `score_set` adds `ok_pct` = (correct + refused_ok)/judged and
     `ok_partial_credit_pct`; every old key untouched.
   - Tables grow `dev ok%` / `heldout ok%` (13 columns); `results_row` prints
     `n/a` for a `summary.json` saved before the change.
   - `meets_success`: heldout `ok_pct` >= 90 (constant renamed
     `SUCCESS_HELDOUT_OK_PCT`); halluc < 5 and refusal >= 80 unchanged.
   - Ranking: dev `ok_partial_credit_pct`, falling back to `dev_partial_credit_pct`
     for a resumed `state.json` that predates the key; recommendation line reads
     "best experiment by dev ok+0.5*partial%".
   - `state.json` results add `dev_ok_pct`, `heldout_ok_pct`,
     `dev_ok_partial_credit_pct`; `loop.log`'s per-experiment line now shows ok%.
   - `--rejudge` already re-scores from saved verdicts (synthetic out dir: zero
     judge calls, dev correct 50 vs ok 100). Real loop4 output lives on the
     Thinkpad, so it was not re-scored here.
   - **Fixture note**: `HELDOUT_ITEMS` needed a third probe the fake judge marks
     `wrong` (FakeOps now treats an expected answer starting with `MISMATCH:` as
     wrong). Under ok% the old 2-item set scored 100, so every default run stopped
     at SUCCESS after E1 — 3 TestDriver tests failed until the fixture stopped
     tripping the thresholds. The dedicated success test (9 + 1) still covers it.
2. `qitem-20261013062452-16eefb5b` — **packet-35 probe files from the CLI**:
   **merged** (`34700a4` in master). Branch `packet-35-probe-paths`, worktree
   `../graphraggle-p35`. Details:
   Branch **`packet-35-probe-paths`**, commit **`34700a4`**, base `8c1984b`
   (= master head, already includes packet-34 `a6fecc7` and the lead's
   "loop run 4 config" docs). Worktree **`/home/maverick/work/graphraggle-p35`,
   clean, not pushed**. Files: `scripts/overnight_loop.py` +64/−8,
   `tests/test_overnight_loop.py` +144/−0 (diffstat 216 insertions / 8 deletions,
   only those two files). Tests: **`634 passed, 2 warnings, 6 subtests passed in
   2.78s`** (master baseline 627; +7 new). pyflakes/`/tmp/undef.py`/`/tmp/annscan.py`
   clean.
   - `resolve_probe_path(raw)`: absolute stays absolute; a value starting with
     `probe/` stays root-relative; anything else resolves under `<root>/probe`.
     Defaults are exactly `DEV_PROBE_FILE`/`HELDOUT_PROBE_FILE` (v1 names).
   - `--dev-probes` / `--heldout-probes` → `Loop(dev_probe_file=…,
     heldout_probe_file=…)` used in `run_experiment`, and
     `load_probe_maps(root, ops, dev_probe=…, heldout_probe=…)` +
     `rejudge(…, dev_probe=…, heldout_probe=…)` so `--rejudge` matches ids
     against the same files.
   - One log line at start, names only: `probe files: dev=… heldout=…` (goes to
     stdout + `loop.log`); rejudge emits the same line before loading.
   - Missing file → stderr `refusing to run: --dev-probes file not found:
     <full path>`, exit **3**, raised before `Loop` is built, so no docker, no
     HTTP, no `loop.log`. Smoke in `/tmp/p35root`: defaults asked the v1 ids;
     `solvay-dev-v2.jsonl` / `probe/solvay-heldout-v2.jsonl` / an absolute path
     each used the file given; a typo exited 3 and left no out dir.
   - Judgment calls told to the lead: the `probe/…` form is accepted so defaults
     round-trip; the existence check also guards the defaults (a missing v1 file
     now fails at startup instead of mid-experiment); `--rejudge` keeps its older
     "refusing to re-judge: need both <names>" → 3 path rather than the new check.
2. `qitem-20261013055434-0c1b2d47` — **packet-34 judge rubric v2**: merged by the
   lead (`a6fecc7` is in master). Branch `packet-34-judge`, worktree
   `../graphraggle-p34`, clean. `JUDGE_PROMPT` is a 5-numbered-rule rubric
   (2130 chars, asserted <3000): 1 refusals (`refused_ok` vs `refused_wrong`),
   2 paraphrase/unit tolerance, 3 attribution (a wrong source named is not a
   hallucination), 4 evidence-only, 5 the reason must name the rule breached.
   Live judge check on 4 **synthetic** cases: refusal → `refused_ok`; paraphrase +
   wrong attribution → `correct`; invented value → `hallucinated`; refusal on an
   answerable question → `refused_wrong`.
3. `qitem-20261013055450-364c65fd` — **probe authoring v2** (delivered to the
   lead; private content, so counts only here and in the handoff note, never
   question/answer/evidence text).
   Deliverables live **outside the repo**, dir `chmod 700`, files `600`:
   `/home/maverick/work/solvay-probes/solvay-dev-v2.jsonl` (40 probes, ids
   dev01–dev40; 18 single-fact / 8 multi-fact / 4 follow-up / 4 combine / 6
   unanswerable; per source 14/14/6/6 against the 14-per-file cap),
   `solvay-heldout-v2.jsonl` (40 probes, ids ho01–ho40; same 18/8/4/4/6 mix; 20
   and 20 against the 25-per-file cap) and `verify_probes.py` (stdlib validator:
   field/id/type checks, evidence ≤300 chars verified verbatim after
   whitespace-normalise+casefold against the named source in that source dir,
   follow-up must carry non-empty `history`, unanswerable must have empty
   evidence + "not in the documents" + 3–6 `absent_terms` found in **no** source,
   per-source caps). Both sets: **`result: passed`** (34 quotes + 6 unanswerables
   each). Negative control run: corrupting one quote ⇒ `FAILED`, so the verbatim
   check is live. Beyond the brief: the 12 unanswerables' `absent_terms` were also
   tested against **all six** transcripts across both dirs — zero leaks.
   Sources read: 4 dev + 2 heldout ASR transcripts. Judgment calls: garbled ASR
   passages skipped where the garbled term *is* the answer, kept where the answer
   is clean; no questions about who said what, no yes/no-shaped questions, no
   meeting-logistics questions. No repo files touched, no commit, no pytest needed.
   Generator scratch (throwaway): `/tmp/probes/{dev1,dev2,dev3,build,ho1a,ho1b,ho2a,ho2b,ho3a,ho3b}.py`.

## Worktrees / branches

- `packet-36-ok-pct` (**`4bf61ea`**, `../graphraggle-p36`) — **merged into master**
  (`4d341d7` contains it). Worktree clean, spendable/removable at the lead's call.
- `packet-35-probe-paths` (`34700a4`, `../graphraggle-p35`) — **merged**.
- `packet-34-judge` (`a6fecc7`, `../graphraggle-p34`) — **merged** into master.
- Master head: **`4d341d7`** (`docs: Hermes widen-corpus brief`; ⊃ `4bf61ea`
  packet-36 ⊃ `34700a4` packet-35 ⊃ `a6fecc7` packet-34). Test baseline on master
  is **642 passed** (634 was pre-packet-36) — I have not re-run the suite on
  master this wake, only confirmed ancestry.
- `packet-32-shapes` (`e06eabe`), `packet-31-rejudge` (`378bc21`),
  `packet-30-realpath` (`ef2219c`), `packet-29-nokey` (`ea17a88`),
  `packet-28-loop` (`eb6ab0a`), `packet-27-impl` (`1f90c0e`), `packet-26-impl`
  (`fca1b3f`) — in master.
- `packet-25-impl` (`c6d8b96`) — pushed; merge into master still unconfirmed by me.
- Older: `release-rc1-docs` (`e436e98`), `packet-24-task-prompt-cap`
  (`632d43a`/`c89b293`, deployed as `local/rag-gateway:p24`), `packet-23-empty-retry`
  (`9a7bb9a`), `packet-22-ll-seed`.
- Shared checkout `/home/maverick/work/graphraggle` NOT switched — still
  `release-rc1-docs`, only this file modified. Venv has `python-pptx==1.0.2`
  (authorized by packet-27).
- Test command (run from the worktree): `RAG_API_KEY=test
  LIGHTRAG_INTERNAL_API_KEY=test LIGHTRAG_BASE_URL=http://localhost:9621
  SOURCE_DOCS_DIR=/tmp/src UPLOADS_DIR=/tmp/up STATE_DB_PATH=/tmp/state.db
  /home/maverick/work/graphraggle/.venv/bin/python -m pytest tests/ -q`
  (**634** on master `5de3151`, **642** on `packet-36-ok-pct`).
- Tooling in /tmp (recreate from the test file if wiped): `/tmp/undef.py`,
  `/tmp/annscan.py`, `/tmp/pyflakes_lib` (`pip --target`, not in the venv). Smoke
  roots `/tmp/p28root` … `/tmp/p33root`.

## packet-33 facts still worth remembering (in master)

`ENV_WHITELIST` 16 keys (no `ENTITY_TYPES`); `NEVER_CHANGE_EXACT` +
`NEVER_CHANGE_REASONS`; forbidden keys checked **before** the allow-list.
`GATEWAY_ENV_WHITELIST` = BRIDGE_TOP_K, BRIDGE_CHUNK_TOP_K,
BRIDGE_MAX_ENTITY_TOKENS, BRIDGE_MAX_RELATION_TOKENS, BRIDGE_MAX_TOTAL_TOKENS,
BRIDGE_ENABLE_RERANK, GATEWAY_CHUNK_TOP_K, CITATION_TOP_K (same `.env`, verified
via `docker exec rag-gateway-api env`). `_liveness_guard()` polls
`docker ps -a --filter name=<c> --format {{.State}}`; exited/dead or `restarting`×2
⇒ `StepError` with ≤5 masked ERROR lines (`mask_secrets()` kills
`*KEY/TOKEN/SECRET/PASSWORD*` values). `UnrecoverableRun` ⇒ status `fatal`, exit 1,
finalise still restores; `_recover_to_baseline()` at the start of the next
experiment. Gateway readiness reuses `HEALTH_TIMEOUT` (300 s). Pre-flight
`_ensure_services` deliberately unguarded (packet-28's 10-min wait for an
operator-stopped stack).

## overnight_loop test fixture state

`_E2EFixture` + `_StubServer` + `_FastOps` run the real `Ops` against a fake
`docker` on `PATH`, steered by control files in `$FAKE_DOCKER_DIR`: `always-exit`,
`always-restarting`, `state-<container>`, `logs-<container>`, `gateway-running`,
`exec-empty-gateway`, `CHUNK_SIZE=666` poison. `FakeOps.container_states` drives
fail-fast for fake-Ops classes. `CONTAINER_LOG_WITH_ERROR` carries a masked secret
on an ERROR line.

## Next step

On wake: `rig queue list --owned` and `rig queue inbox-pending
dev-worker@graphraggle`. Queue at hand-back: `[]` (only `qitem-20261015090325-ec23f24e`
in progress, now handed off).

If the rc-2 docs item comes back for tweaks it is uncommitted **on master**:
`docs/04-troubleshooting.md`, `docs/05-known-good-config.md`, `docs/08-change-log.md`;
verify with `env -i PATH=$PATH HOME=$HOME .venv/bin/python -m pytest tests/ -q` (expect
**698 passed**; no test reads docs files, so a docs edit can only fail the suite if it
touches `compose.yml`).

Chunking work is in the worktree `/home/maverick/work/graphraggle-chunking` (branch
`chunking`, now `b535792`, suite **726 passed** with the shared venv). If Hermes'
content-free shape report arrives (label pattern, timestamp pattern, turn lengths), add
the dialects it names to `tests/test_transcript_prep.py` and re-check the detection
thresholds — especially whether the real corpus uses `Q:`/`A:` labels (currently
stoplisted) and whether filenames carry usable meeting titles.

Likely follow-ups: a new packet off the
Hermes brief series in `docs/briefs/` (`hermes-widen-corpus.md`,
`hermes-failure-analysis-v2.md` on `release-rc1-docs`), packet-36 tweaks (work in
`../graphraggle-p36` — though it is already merged, so a tweak now wants a fresh
branch off master `4d341d7`), or a probe-set run (sets live at
`/home/maverick/work/solvay-probes/` — `solvay-dev-v2.jsonl`,
`solvay-heldout-v2.1.jsonl`, `solvay-heldout-v3.jsonl` — and a run points at them
with packet-35's `--dev-probes` / `--heldout-probes`, operator-side), or
restarting tonight's loop
(`--resume --retry-errors` re-runs an errored experiment from step 1; a reingest
experiment wipes LightRAG and rebuilds; `--rejudge <out>` re-grades saved answers
without touching the store). I have **not** touched the live stack, `.env`,
`state/`, `source_docs/`, `lightrag_store/`, or git remotes.

## History — packets 28–32 (all in master)

- **packet-32** `e06eabe`: `status_counts_of()`/`status_counts_are_zero()` (nested
  `status_counts`, numeric `all` wins, **no numbers ≠ zero**), fakes emit real
  v1.5.7 shapes, `--retry-errors`, `emit()`/`Loop.log` pipe-proofing + `__main__`
  stdout flush. Gateway `/api/chat` `message.content` verified against
  `app/api.py:1766`/`build_ollama_chat_response()` (`app/api.py:683`).
- **packet-31** `378bc21`: judge token budget for a reasoning model
  (`JUDGE_MAX_TOKENS=4000`, `JUDGE_TIMEOUT=300`, verdict fallback to the last JSON
  object in `reasoning_content`, `JudgeTruncated` never retried) + offline
  `--rejudge`; `score_set` excludes `no_answer`.
- **packet-30** `ef2219c`: missing `import subprocess` / `Callable`; `Ops.run` rc
  127/124/126; `write_text_atomic` temp name unique per process+thread. Real-path
  tests added — fake-only tests had missed the import crash.
- **packet-29** `ea17a88`: LightRAG API key optional (`_api_key()` → `""`,
  `X-API-Key` only when non-empty).
- **packet-28** `eb6ab0a`: `scripts/overnight_loop.py` driver + tests; 16-key
  whitelist is the sole allow-gate; credentials banned by name/suffix;
  byte-preserving `.env` rewrite + `env.original` (0600) restored in `finally`;
  worker-running aborts the run (never `docker stop`); SUCCESS = heldout correct
  ≥ 90 ∧ halluc < 5 ∧ refusal ≥ 80; 3 consecutive errors stop;
  `--dry-run`/`--resume`/heartbeat.

### packet-28 semantics still worth remembering

Non-stacking env = original-for-touched-keys + `baseline_env` + experiment env;
unanswered probes → `probe_errors`/`judge_error` (never judged wrong); `correct%`
excludes partial; refusal accuracy null when no unanswerables (→ SUCCESS can't
fire); judge key `1234` overridable by `OVERNIGHT_JUDGE_API_KEY`; recommendation
diff computed against `baseline_env` and never applied.

## Standing out-of-brief findings (lead's call)

1. Stale podman/ports facts in `docs/01,02,03,04,06,09,10,12`,
   `docs/quick-reference.md`, `docs/README.md`, `docs/lightrag-webui-guide.md`,
   `docs/ops/rollback-and-containment.md`, `scripts/install_rag_stack.sh`.
2. `docs/07-openwebui-guide.md:180` says query mode defaults to `hybrid`; the code
   default is `mix`.
3. Repo `compose.yml` has no chromadb service though production runs `rag-chromadb`.
4. HyperQwen endpoints use `192.168.1.195`; docs say the oMLX is `192.168.1.190:1234`.
5. Docs still say "binary docs ingest through `/documents/upload`" — stale for
   `.pptx` (packet-27) and for `source_docs` files (packet-25).
6. `run()`'s generic `except Exception` turns any internal bug into run status
   `fatal` with exit 2 and only a `self.log` line (invisible when `print_fn` is
   silenced). It hid a test-harness `NameError` for me twice.
