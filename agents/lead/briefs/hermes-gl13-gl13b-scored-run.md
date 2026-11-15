# Hermes brief — scored run of the fresh Solvay held-out pool (GL-13 transcripts + GL-13b office documents), against PRODUCTION (queries only)

Operator-approved plan 2026-11-15. This is the go-live accuracy measurement on a NEW held-out set (nothing here has been used to tune anything). You are the only seat that reads corpus content: the probe files below are
derived from real Solvay documents; keep them chmod 600, never paste question/answer/evidence text into any report, queue row or ticket, counts only.

## Files (copied by the lead to the thinkpad, not read by the lead)
~/briefs/probes-solvay/gl13/{questions,answer-key,run-set}.jsonl  — 51 probes, transcripts only (sha256 questions 3173a62fff59…, answer-key 4c6c1c460f6c…, run-set 77f81efb9f96…)
~/briefs/probes-solvay/gl13b/{questions,answer-key,run-set}.jsonl — 69 probes, PDF/DOCX/PPTX (sha256 questions 73fd0151aa75…, answer-key 43d380f5c9e0…, run-set fa7bc4c65837…)
Verify the sha256s (full values in each README.md) before running; move them into probe/ (chmod 600) if that is your runner's convention. `run-set.jsonl` is question+key merged in the same fields the earlier held-out sets use plus `shape`.

## Run
PRODUCTION, QUERIES ONLY. No setting, image or file change; snapshot docker-inspect + .env mtimes at start and end, diff empty. Same path and judge as the 90/100 recall run and the v3 held-out measurement:
direct :9621, mode=mix, only the existing runner (probe/run_prod_recall.py or your copy of it), judge qwen3.6-35b on oMLX with rubric v2, same guards (your 600 s deviation is fine, record it). Sequential, 5 s pause.
Run gl13 (51) and gl13b (69) as two result files; report each AND the combined 120. Do not re-run a probe to improve a number; if a probe errors or times out, record it as such.
Contention: the dev-worker uses the same sigma model during working hours; note start/end times and any 5xx, rerank-fallback or judge-error lines (0 expected).

## Report (counts only)
Per set and combined: n, correct / refused_ok / partial / wrong / halluc / timeout / error, **ok%** = (correct + refused_ok)/n, halluc count, refusal correctness on unanswerable + near-miss rows (refused_ok / those rows).
Per `shape`: n and ok%. For gl13b specifically, also per source document (pdf / docx / pptx) because extraction differences can masquerade as misses. Latency p50/p95/max.
List the ids (only ids) of every wrong/halluc/partial/timeout row so the worker can authoring-audit them later; do NOT say what the question or the answer was.
Go-live bar to read against (operator 2026-10-31): ok% >= 88 on a NEW held-out set, 0 hallucinations, refusal correctness >= 90%.
Worker's scoring warnings for gl13b, please record rather than assume: (1) which PDF backend the production worker used (poppler `pdftotext -layout` vs pypdf: the 3 PDF `table` rows depend on layout mode) and that python-docx was the DOCX reader; (2) the answer-key offsets refer to the worker's own extraction, not your store, so judge on key phrases, not offsets; (3) 46 zero-width spaces sit in the DOCX table-of-contents line; none of the probes quotes across them.

## Rules
One bare command per approval; stop on first failure; do not retry a denied approval; do not message the lead mid-turn. Report in structure-cutover.md section "GL-13/GL-13b SCORED RUN 2026-11-15", STATUS line 1.
