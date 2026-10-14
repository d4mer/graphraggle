# Handoff — worker

Last updated: 2026-10-13 (after **conftest env-defaults** handoff; queue empty;
**standby**)

## Current state

Queue: `rig queue list --owned` → `[]`, `rig queue inbox-pending
dev-worker@graphraggle` → `[]`. Nothing claimed, nothing owed. Standby.

Latest item, **handed off uncommitted** (lead commits):
`qitem-20261013201632-9e4c7ab1` — **`tests/conftest.py` env defaults** (brief:
`agents/lead/briefs/conftest-env-defaults.md`). One new file, 58 lines, in the
**shared checkout** (`release-rc1-docs` @ `cf4a2e9`), **left uncommitted on
purpose** — if it ever goes missing, recreate it: module-scoped
`tempfile.TemporaryDirectory(prefix="graphraggle-pytest-")`, then
`os.environ.setdefault` for `RAG_API_KEY=test-rag-key`,
`LIGHTRAG_INTERNAL_API_KEY=test-internal-key`,
`LIGHTRAG_BASE_URL=http://example:9621`, and `SOURCE_DOCS_DIR` /
`UPLOADS_DIR` / `STATE_DB_PATH` under that temp dir (`source_docs/`, `uploads/`
subdirs pre-created). Results: clean env **467 passed, 2 warnings in 0.99s**;
six vars exported **467 passed, 2 warnings in 0.58s**; `env -i` **467 passed**;
negative control (conftest removed) **4 collection errors** —
test_keywords/test_observability/test_ollama_bridge/test_rewrite. Also checked on
a **detached** `/tmp` worktree at master `4d341d7` (now removed): **642 passed, 2
warnings, 6 subtests passed** clean-env, same 4 errors without it — so it lands
safely on either branch, and **master's baseline is 642, not 467** (467 = the
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

Idle/standby; nothing in the queue as of this wake. If the lead asks for a tweak
to the conftest, it lives uncommitted in the **shared checkout**
(`tests/conftest.py`), not in a worktree — don't lose it to a branch switch. On wake: `rig queue list
--owned` and `rig queue inbox-pending dev-worker@graphraggle` (the second needs
the destination session as an argument). Likely follow-ups: a new packet off the
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
