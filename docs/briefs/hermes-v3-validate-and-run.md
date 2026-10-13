# Brief - validate the new probes, then run the V3 scoring pass (Thinkpad ~/rag-project)

Files (already on the Thinkpad in ~/rag-project/probe/, chmod 600): solvay-heldout-v2.1.jsonl (40; ho20/ho39 were replaced), solvay-heldout-v3.jsonl (24 fresh, ids hv01..hv24), solvay-dev-v2.jsonl.
Source transcripts: ~/rag-project/staging/Solvay (held-out ones are the two "Recording" transcripts).

## Part 1 - validate (files only, no stack calls; you are the only reader of probes+transcripts for this purpose)
a) ho20 and ho39 in v2.1: the dev-worker made each a NEW unanswerable question. You earlier said the originals were answerable. Check that each new question is truly unanswerable from the transcripts (search the key nouns), and tell me per id: valid / ambiguous / answerable.
b) v3: read all 4 unanswerable probes plus 8 randomly chosen answerable ones against the transcripts: valid (one defensible answer, quote supports it) / ambiguous / wrong. Report counts per category and ids of any non-valid (ids only, no content).
If a probe is non-valid, do NOT edit the files; list ids so the lead can have them fixed before scoring. If >2 of the 12 v3 probes are non-valid, stop after Part 1 and report; do not run Part 2.

## Part 2 - V3 scoring pass (only if Part 1 passes)
1. Write ~/rag-project/probe/experiments-v3.json by heredoc, same as experiments-v2.json but with experiment name "V3-fresh" and "reingest": false (the store already holds the 6 transcripts from loop4; confirm via /documents/status_counts that processed==6 first, and ask nothing else of LightRAG).
2. In tmux session loop5: `cd ~/rag-project/probe && python3 overnight_loop.py --root ~/rag-project --experiments experiments-v3.json --out ~/rag-project/probe/loop5 --dev-probes solvay-heldout-v2.1.jsonl --heldout-probes solvay-heldout-v3.jsonl --max-hours 2`
   (v2.1 sits in the dev slot as a re-check; the SCORED set is v3 in the held-out slot.) Do not stop it with Ctrl-C; wait for "run status: complete" in loop5/results.md.
3. Then read answers/verdicts and write ~/rag-project/docs/ops/runs/v3-results-summary.md (heredoc, counts only): per-set verdict counts, failing ids, cause per failing id (judge_wrong / probe_bad / not_in_graph / retrieval_miss / synthesis_wrong / other), judge agreement on 3 random correct and all refused_ok.
4. Message lead: file paths + the counts. No transcript/question/answer text, no names or quotes.
Constraints: no docker, rm/mv/chmod/chown; the driver restarts lightrag-server itself; unlisted approval dialogs: do not accept, report.
