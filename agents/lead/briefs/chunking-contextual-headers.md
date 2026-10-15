# Brief: contextual chunk headers + speaker-turn normalisation (branch `chunking`)

**Branch and checkout.** Work ONLY in the worktree `/home/maverick/work/graphraggle-chunking`
(branch `chunking`, base `origin/chunking` = `rc-2` = `11b1833`). Do not touch
`/home/maverick/work/graphraggle` (master = frozen release line). Run tests from the
worktree with the shared venv, bare env:
`cd /home/maverick/work/graphraggle-chunking && env -i PATH=$PATH HOME=$HOME /home/maverick/work/graphraggle/.venv/bin/python -m pytest tests/ -q`
and confirm `app` is imported from the worktree, not the master checkout (print `app.__file__`
once in your report). Baseline expectation: 698 passed. Do not commit or push; hand back the
diff, I commit to `chunking`.

## Intent
LightRAG 1.5.7 cuts text into fixed-size token chunks. For transcripts that (a) cuts
mid-sentence, (b) separates a decision from its reasoning, and (c) leaves each chunk with
no context: only chunk 0 can know the meeting title or date, so "the plan" is ambiguous at
retrieval. Earlier tuning (chunk size 600/2000, TOP_K, rerank, token budget) did not move
accuracy, so settings are not the lever; the *text we hand LightRAG* is. Goal: make every
chunk LightRAG produces self-describing and aligned to speaker turns, without changing
LightRAG itself.

## What to build
A pure, deterministic pre-processor, one module (suggested `app/transcript_prep.py`):

1. **Detect** a transcript-shaped text document conservatively (speaker-label lines such as
   `Name  0:00`, `Name: text`, `[00:01:02] Name:`, WebVTT/SRT cues). When unsure, return the
   input unchanged. Never alter non-transcripts (specs, policies, notes).
2. **Normalise turns**: one turn per block, `Speaker: text`, merge consecutive lines of the
   same speaker, strip pure timestamps/noise lines, keep original word order and wording
   exactly. No summarising, no spelling "fixes", no rewriting of words. Garbled
   speech-to-text words must pass through unchanged.
3. **Contextual header**: re-insert a short one-line context header before every block of
   about N tokens (N configurable, default well under the chunk size so every chunk
   LightRAG cuts contains at least one header), e.g.
   `[Meeting: <title> | Date: <date> | Part k/n]`. Title and date come only from the file
   name stem and an ISO-ish date in the name if present; if absent, omit the field. Never
   invent participants, never read file content for metadata beyond the turns themselves.
   A list of speaker names observed in the document is allowed as a header field.
4. **Idempotent and reversible in meaning**: running it twice gives the same output; the
   original words, in order, are all still present (test this with a token-level subsequence
   check against the input minus headers).
5. **Flag-gated, default OFF**: `TRANSCRIPT_PREP_ENABLED=false` in `app/config.py`, plus
   `TRANSCRIPT_PREP_HEADER_EVERY_TOKENS` (default 200). Wire it into the worker's
   text-submit path (`submit_file`, `/documents/text`) behind the flag, applied only to
   `.txt`/`.md`/`.vtt`-like text, after the byte guard and the existing normalisation.
6. **One shared entry point**: `scripts/overnight_loop.py` posts text straight to
   `/documents/text` and bypasses the worker (about line 1331). Expose the function so the
   loop can apply the same pre-processing from an experiment-level key (e.g.
   `"transcript_prep": true`). Add that key and its test; do not change defaults.
7. **Provenance**: record on the document row (via the existing warnings/`warnings_json`
   path, no schema change) that prep ran and how many headers were inserted, so a reindex
   can tell prepared from unprepared documents.

## Hard constraints
- **Data rule:** you must NOT read any real corpus or transcript content. Build and test on
  synthetic fixtures you write (several transcript dialects, a non-transcript, an empty
  file, a file with no speakers, very long turns, unicode, CRLF). Hermes will provide a
  content-free *shape report* of the real transcripts (label pattern, timestamp pattern,
  turn-length distribution); if it arrives before you finish, add the dialects it names.
- No change to LightRAG settings, no schema change, no change to chunk size, no change to
  dedupe/lifecycle code. The flag off must be byte-for-byte today's behaviour: add a test
  that the submitted payload is identical with the flag off.
- The content hash used for dedupe must stay computed on the ORIGINAL file bytes, not the
  prepared text (otherwise re-saving a file changes its identity). Test it.
- Dedupe note: prepared text goes upstream under the existing `file_source`; the 409/dup
  behaviour documented in `docs/05-known-good-config.md` is unchanged.

## Hand-back
Diff, new tests, bare-env suite result, a short table of what each fixture dialect looks
like before/after (synthetic text only), and your honest list of cases where detection is
uncertain. Also report, for a synthetic 5,000-token transcript, how many LightRAG-sized
chunks (use the same tokenizer LightRAG uses if available in requirements; otherwise say
which approximation you used) contain a header and the largest header-free gap.
