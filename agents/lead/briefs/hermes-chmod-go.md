# Hermes — operator GO: tighten permissions on .env and compose.yml (GL-29, Plane GRAG-84), 2026-10-31

APPROVED, exactly this: in ~/rag-project run `chmod 600 .env` and `chmod 640 compose.yml`. Nothing else: no restart, no edit, no recreate, no key rotation.
Before: record `stat -c '%a %U:%G %n'` for both files and the owner of the docker compose process. After: the same stat output (no key values anywhere), then
`docker compose config -q` (rc 0, output not printed) and `curl` /health on :9621 and :8020 = 200. Production containers must be unchanged (docker ps ids, restart counts).
Rollback if config -q fails: `chmod 644 .env; chmod 755 compose.yml` (the original was 777; restore the previous mode you recorded).
Do it between your current tasks (it takes a minute); it does not touch stack2. Append "CHMOD 2026-10-31" to structure-cutover.md (before/after modes, rc codes).
