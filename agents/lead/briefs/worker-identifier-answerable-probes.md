# Worker brief — identifier-bearing ANSWERABLE probes for the M1 gate guard test

Base: `git fetch && git rev-list --left-right --count HEAD...origin/master`; branch from the shared repo's local `master` (82062d3 or later; origin may be 2 behind, expected). Own branch/worktree, no push, one commit.
Synthetic data only (the GL-07 corpora and their manifests are invented; you may read them). Bare full suite must pass: `.venv/bin/python -m pytest tests/ -q`.
Hand back via `rig queue create --destination orch-lead@graphraggle --summary ...` with branch/SHA, test counts, output paths.

## Why
The M1 gate (`app/grounding.py`, flag `GROUNDING_CHECK_ASKED_IDENTIFIER`) refuses/flags when an identifier named in the question is absent from the evidence. The 55-probe stack2 run showed
`would_newly_refuse_answerable = 0`, but all 43 answerable questions came back `no_identifier`: the generated questions name no code, so the gate was inert on them and the 0 is structural.
Enforcement can only wrongly refuse a good answer on an answerable question that DOES name an identifier whose evidence arrives without content (production export had 509
references_without_content). We need answerable, identifier-bearing probes to measure that.

## Task
A deterministic generator `scripts/make_identifier_probes.py` (stdlib + repo deps) that reads each company's `manifest.json` (default `/home/maverick/work/synthetic-co/<Company>/`, the same
two companies and seed as the corpora Hermes ingested on stack2: Northwind Chemicals, Contoso Pharma) and writes, per company, a probe manifest in the SAME SHAPE the Hermes runner and
`scripts/score_grounding_run.py` already consume (read the scorer's manifest loader and `make_synthetic_company.py`; the scorer rejects unknown probe keys, so reuse the existing buckets
`scoped` / `cross_company` / `unanswerable`, each row with `question`, `shape`, the fact code and expected answer fields the manifest already uses).
Output dir (not committed): `/home/maverick/work/synthetic-co-probes/<Company>/manifest.json`.
Rows:
1. One answerable SCOPED probe per planted fact code (the manifest lists ~78 across both companies): "What outcome was recorded under <CODE>?"-style questions whose code occurs verbatim in the
   document, with the expected answer string from the manifest. Vary phrasing deterministically over several templates (code at start / middle / end, quoted, followed by `?` or `.`,
   with a company mention), so punctuation and position edge cases are exercised.
2. Edge variants on a deterministic subset (say 20%): lowercase code, code split by a space instead of a hyphen, code with a trailing comma. For each variant record whether the gate SHOULD
   treat it as supported (the evidence holds the canonical code) or is a known limitation; do not hide the ones that will fire, label them `edge_expected_fire` in a field the scorer ignores or
   in a side file, so false refusals can be separated from by-design ones.
3. A small number of TWO-CODE questions (both present) and ONE-PRESENT/ONE-ABSENT questions, labelled as such.
4. The existing 12 unanswerable lookalike rows per company, unchanged, as controls (so one run reports both the catch rate and the guard).
Validation in the script (fail loudly): every code used exists in the manifest as planted; its document file in the company folder contains it verbatim; run the repo structure chunker over
that document and assert the code survives byte-identical in at least one piece (the gate checks the evidence pieces, not the raw file); no question contains the expected answer string;
ids unique and distinct from the GL-07 probe ids.

## Tests
Determinism (same seed = same bytes), code present in doc and in a chunk, answers absent from questions, the scorer loads the generated manifest without error (call its loader), edge variant
labelling, validation failing on a tampered code. Invented fixtures where you can; the real GL-07 manifests only if present, skipped otherwise.

## Hand-back
Output paths and counts per bucket, the exact command lines, the list of edge-variant ids that are expected to fire, and anything in the gate (`app/grounding.py`) you found that the new probes
would trip for a bad reason. Do not change the gate. Hermes will run this set next on stack2; the lead briefs that.
