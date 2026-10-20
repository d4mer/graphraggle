# Brief: pin Open WebUI 0.12.0 in the repo (production already cut over 2026-10-14)

Base on origin/master. Production open-webui was upgraded 0.11.4 -> 0.12.0 on the
Thinkpad (canary + cutover verified by Hermes, one-line compose.yml change). The repo
must match so the next deploy does not downgrade it.

Do:
1. compose.yml: open-webui image tag `0.11.4` -> `0.12.0`. Nothing else.
2. docs/05-known-good-config.md line ~25 and docs/08-change-log.md (the 0.11.4 mentions,
   including the 2026-10-11 ports/images item): update to 0.12.0 where it describes what
   runs now; add one dated change-log entry: "2026-10-14 Open WebUI 0.11.4 -> 0.12.0,
   canary on a data copy then cutover; DB migrated (6 revisions), downgrade unsupported,
   rollback = restore backup tarball; bridge contract unchanged (task short-circuit
   verified)". No corpus/chat content.
3. Run the bare-env full suite (tests/test_compose_config.py must still pass).

Hand back the diff; do not commit or push.
