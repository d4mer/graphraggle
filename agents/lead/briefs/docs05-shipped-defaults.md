# Worker brief: docs/05 "defaults as shipped" must match app/config.py

Intent: `docs/05-known-good-config.md` (the "Structure chunking ... defaults as shipped" block, ~line 73-85)
says `STRUCTURE_CHUNKING_ENABLED=true`, but `app/config.py` defaults it to **false**. An operator reading the
doc would assume the worker structure-chunks out of the box. Make the doc true, and check the rest of that block.

Work only in a new worktree: `git worktree add ../graphraggle-docs05 -b worker/docs05 master`.
No corpus, no network, no ssh. Do not commit (the lead commits). Do not touch `agents/` or `rig.yaml`.

1. For every variable in that block (and the PDF/DOCX/transcript-prep flags if the doc lists them anywhere), compare
   the documented value to the default in `app/config.py` (read the `Field(default=...)`, do not trust docs or
   comments). Fix every mismatch **in the doc**, not in the code. Where the doc means "recommended for production",
   say so explicitly: label the block as defaults, and add a separate short "set these to turn it on" list
   (`STRUCTURE_CHUNKING_ENABLED=true`, `WORKER_PDF_EXTRACT_ENABLED=true`, `WORKER_DOCX_EXTRACT_ENABLED=true`,
   which now default false and are only reached when structure chunking is on).
2. `grep -rn "STRUCTURE_CHUNKING_ENABLED\|WORKER_PDF_EXTRACT\|WORKER_DOCX_EXTRACT\|TRANSCRIPT_PREP_ENABLED" docs README.md
   scripts/README.md` and fix any other place that states a default wrongly. Report each file and line touched.
3. Add a small test only if one is cheap: a test that reads the documented defaults from the docs/05 block and asserts
   they equal `Settings` defaults would stop this recurring. If it needs a fragile parser, skip it and say so.
4. Full suite must pass (use the main checkout's `.venv`).

Hand back with `rig queue handoff ... --body-file <your report>` (the body must hold the report): the list of
mismatches found (doc value vs code default), the diff summary, suite count, and anything you could not verify.
