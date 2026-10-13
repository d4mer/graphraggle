# Brief - root-cause the loop5 outage window, then re-run the V3 pass (Thinkpad ~/rag-project)

loop5 is not scorable: between ~15:28 and the end of hv12 (UTC) answers came back as the fast "no relevant context" boilerplate (22-27 s) and the judge returned HTTP 500 for 13 items. Valid signal was 27/27 (v2.1 slot) and 12/13 (v3). Nothing else was supposed to be using the stack. We need the cause before re-running.

## Part 1 - diagnose (read-only; classes and counts only in messages, no query/answer/transcript text)
Look at 15:15-16:00 UTC for: `docker logs --since/--until` of lightrag-server and gateway-api (error classes + counts per minute; do not paste log lines that contain query text), the sigma Strata endpoint (is http://192.168.1.195:1234/v1/models reachable now, any timeouts/connection errors in the lightrag log to 192.168.1.195), the oMLX judge (192.168.1.190:1234: what did the 500 body say - model unloaded/memory/timeout?; `curl` /v1/models now), embedding errors to 192.168.1.190, any restarts of containers in that window (`docker ps`/`docker inspect` StartedAt), and who else was calling sigma/oMLX then (the lead's dev-worker seat on sigma may have been running a model job: you may only report that load existed, via the sigma/oMLX server's own status if it exposes one).
Classify: sigma_unavailable / sigma_timeout / omlx_judge_memory / omlx_embedding / lightrag_internal / container_restart / unknown. Write ~/rag-project/docs/ops/runs/v3-outage-diagnosis.md (heredoc, short).

## Part 2 - re-run (only if Part 1 finds no ongoing fault: /health healthy, sigma /models reachable, judge answers a trivial prompt)
Same command as before into a NEW dir, nothing else changed:
`cd ~/rag-project/probe && python3 overnight_loop.py --root ~/rag-project --experiments experiments-v3.json --out ~/rag-project/probe/loop6 --dev-probes solvay-heldout-v2.1.jsonl --heldout-probes solvay-heldout-v3.jsonl --max-hours 2` in tmux session loop6.
Gate during the run: if after 20 probes more than 3 answers are the fast boilerplate (<30 s "no relevant context"), stop the run with `kill -TERM` of the driver python pid (never kill the tmux wrapper), re-diagnose, and message me instead of continuing.
Afterwards write ~/rag-project/docs/ops/runs/v3-rerun-summary.md (heredoc, counts only: verdict counts per set, failing ids with cause, judge errors, boilerplate-answer count, judge agreement on 3 random correct + all refused_ok) and message me the paths and counts.
Constraints: no rm/mv/chmod/chown; no docker restart except what the driver does; unlisted approval dialogs: do not accept, report.
