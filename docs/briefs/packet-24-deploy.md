# Brief: deploy packet-24 to the Thinkpad (operator authorised 2026-10-08; cap ships OFF)

To: ops-hermes@thinkpad-ops. Do this ONLY after Part A (all 3 draws), the lone-prompt oMLX test and the addendum reports are finished.
A gateway restart during Part A would corrupt it. Parts B/C of gate2-gate3 may be done before or after; do not interleave.
Follow the procedure you used for packet-23 (report docs/ops/runs/stage0-empty-retry-deploy.md).

Source: branch `packet-24-task-prompt-cap`, commit c89b293 (off packet-23-empty-retry 9a7bb9a), pushed to origin.
Change: new setting BRIDGE_TASK_MAX_CHARS (default 0 = OFF) caps forwarded OpenWebUI `### Task:` prompts; bridge log lines gain
`prompt_chars` (always) and `forwarded_chars` (task path only). Behaviour is otherwise identical with the default.

Steps:
1. Record the current image id and tag it: `docker tag local/rag-gateway:latest local/rag-gateway:pre-packet-24` (should equal p23 68c1631d7dd3; report it).
2. Build the packet-24 image from c89b293 as `local/rag-gateway:p24`, point `latest` at it, `docker compose up -d --no-deps gateway-api`.
   Do NOT add BRIDGE_TASK_MAX_CHARS to .env (default 0 must apply). No other env changes.
3. Acceptance (as in packet-23; no pipeline_status): (a) 2 normal bridge queries return non-empty answers, their log lines have
   `prompt_chars` and NO `forwarded_chars`; (b) one short synthetic `### Task:` request (made up, no corpus text) logs
   `task_prompt=true`, `prompt_chars == forwarded_chars`, non-empty answer; (c) `empty_retries_used` still present;
   (d) no log value contains prompt text.
4. Write docs/ops/runs/stage0-task-cap-deploy.md on the Thinkpad (image ids, acceptance results, rollback command:
   `docker tag local/rag-gateway:pre-packet-24 local/rag-gateway:latest && docker compose up -d --no-deps gateway-api`) and reply to orch-lead@graphraggle with the headline.
5. If any acceptance step fails: roll back immediately, say so, and report.
