# Brief: loop flags needed to run against a second stack (branch `chunking`)

Worktree `/home/maverick/work/graphraggle-chunking`, base `origin/chunking` = `b7fe10f`.
Same test command and rules as before; do not commit or push; hand back the diff.
Baseline 760 passed.

Your reported-not-fixed items (1) and (2) are blockers for the plan: production's
`rag-ingest-worker` is running and will keep running, and the second stack lives in its
own compose project. Without both flags a second-stack run aborts or touches the wrong
compose file.

1. `--worker-container NAME` (default `rag-ingest-worker`, today's behaviour). The
   worker guard must check THAT container. Also accept `none` (skip the guard) for a
   second stack that has no worker; refuse `none` when the resolved LightRAG URL is the
   production URL (the guard is what protects production there). Test both, and test that
   with the second stack the production worker running does not abort the run.
2. `--compose-file PATH` (default `<root>/compose.yml`) and `--compose-project NAME`
   (default none) passed to every `docker compose` call the loop makes (recreate of the
   gateway service, readiness waits). Test the exact argv in the fake Ops.
3. Everything the loop restarts or reads must come from the target: confirm no remaining
   use of `lightrag-server`/`rag-gateway-api`/production `.env` constants on a run with a
   non-default target (grep and a test that fails if a production constant is used).
4. While there: fix the pre-existing `Any` import in `tests/test_overnight_loop.py:72`.
5. Add to `docs/ops/loop/experiments-chunking.json` `_readme` the exact command line for
   the second-stack run (flags only, no secrets), and the sentence: "Never run this
   file against production without --allow-production; it wipes the store."

Hard rule you already learned: tests must never open a real socket. Add a conftest-level
guard (a fixture that fails any test that connects to localhost:9621/8020) if you can do
it without touching other tests; if not, say so.
