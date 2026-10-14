# Packet 31 - overnight_loop: judge token budget + offline re-judge mode   URGENT (the running loop's judge fails)

Bug (my spec's fault): the judge model qwen3.6-35b on http://192.168.1.190:1234 is a REASONING model: the reply has `message.reasoning_content` (thinking) and `message.content`
(final answer). With max_tokens=400 the thinking uses the whole budget (a trivial prompt already used 371 tokens), `content` comes back empty, and every call is logged
"judge replyunparsable" -> verdict judge_error. The tonight run (python process already running, cannot be hot-patched) has saved all probe ANSWERS, so we re-judge them offline.

Do, in scripts/overnight_loop.py and tests/test_overnight_loop.py only (worktree: `git worktree add ../graphraggle-p31 -b packet-31-rejudge master`):
1. Judge call: max_tokens 4000 (constant JUDGE_MAX_TOKENS), request timeout 300 s. If `content` is empty/unparsable but `finish_reason` != "length", also try the LAST {...} JSON object found in
   `reasoning_content` as a fallback. If finish_reason == "length" and content empty -> log "judge truncated (raise JUDGE_MAX_TOKENS)" once per experiment, then judge_error as before.
2. New mode: `overnight_loop.py --rejudge <out_dir> --root <root>` (no --experiments needed in this mode; --root only supplies <root>/probe/solvay-dev-v1.jsonl and solvay-heldout-v1.jsonl).
   For every subdirectory <out_dir>/<name>/ containing answers.jsonl: load the probe items (id -> question, expected_answer, evidence, type) from the two probe files; judge each answer whose
   verdict is missing or judge_error in <name>/verdicts.jsonl (keep good verdicts; sequential, 1 call at a time; skip answers that errored/empty -> verdict "no_answer");
   rewrite <name>/verdicts.jsonl and <name>/summary.json using the existing scoring functions; then write <out_dir>/results-rejudged.md (same table format as results.md, one row per experiment, dev and heldout
   columns). It must NEVER touch .env, docker, LightRAG, the gateway, sigma, or the existing results.md/state.json; only the oMLX judge endpoint is called. Safe to run repeatedly and while the loop is running
   (write files atomically; a half-written answers.jsonl line must be skipped, not crash). It prints per-experiment counts only (judged / kept / errors), never questions, answers or reasons.
   Answers are matched to probe items by id; which set an answer belongs to is determined by the id prefix used in answers.jsonl (look at how the driver writes it: ids like dev35 in the log).
3. Tests (real code paths, stub HTTP server for the judge as in packet 30): stub returns empty content + long reasoning when max_tokens < 1000 and a JSON verdict when >= 1000 -> proves the fix;
   fallback-from-reasoning case; length-truncation case; --rejudge over a tmp out dir with 2 experiments (one with a judge_error row and one good row kept) incl. a truncated last line in answers.jsonl;
   assert results-rejudged.md written, results.md untouched, no questions/answers in stdout.
4. Full suite must pass. Report: commit hash, test count, anything you resolved ambiguously.
