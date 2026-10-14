# Packet 35 - overnight_loop: probe file paths settable from the CLI (small; code only, no data)

The driver reads fixed names `<root>/probe/solvay-dev-v1.jsonl` and `solvay-heldout-v1.jsonl` (DEV_PROBE_FILE / HELDOUT_PROBE_FILE). We now have v2 probe sets and must keep v1 intact.
Worktree: `git worktree add ../graphraggle-p35 -b packet-35-probe-paths master`. Only scripts/overnight_loop.py and tests/test_overnight_loop.py.
1. New optional CLI args `--dev-probes PATH` and `--heldout-probes PATH` (absolute or relative to <root>/probe). Defaults stay the current v1 file names, so existing behaviour and tests are unchanged.
2. They apply everywhere the probe files are read: normal runs, `--dry-run`, and `--rejudge` (the answers are matched to items by id from those two files). Print/log the chosen file NAMES (not contents) once at start.
3. Tests: custom paths used in a run and in --rejudge; defaults unchanged; relative path resolution; missing file -> clear error before any docker/HTTP step (exit code 3 like other config errors).
4. Full suite passes. Report: commit hash, test count.
