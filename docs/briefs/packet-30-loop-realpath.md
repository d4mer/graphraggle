# Packet 30 - overnight_loop: missing imports + real-path (non-fake) tests   URGENT (blocks tonight's run)

Bug: the first real start on the Thinkpad crashed at once: `NameError: name 'subprocess' is not defined` (the real Ops.run uses subprocess but the module never imports it;
the 555 tests only use a fake Ops so they never hit the real code). An AST scan also flags `Callable` as undefined. There may be more latent bugs in the real paths.

Do, in scripts/overnight_loop.py and tests/test_overnight_loop.py only (worktree: `git worktree add ../graphraggle-p30 -b packet-30-realpath master`):
1. Add `import subprocess` and import `Callable` (from collections.abc). Re-run an undefined-name scan (ast-based or `python -m pyflakes` if you can install it into a temp venv) and fix everything it finds.
2. Add a test `test_no_undefined_names` that parses the module with ast and fails on any undefined global name (builtins, imports, defs, args, assigned names count as defined).
3. Real-path tests (these must NOT use the fake Ops):
   a. The real Ops class: `run(["echo","hi"])` returns (0,"hi\n",""); a nonexistent command and a timeout are handled as the code intends; `http_json` GET/POST/DELETE against a stdlib
      `http.server` started on 127.0.0.1:0 in a thread (check headers, JSON bodies, non-200 codes, connection refused); `read_text` / `write_text_atomic` incl. mode 0o600; the real sleep/clock functions exist.
   b. One END-TO-END run of Loop with the REAL Ops: tmp root dir with a .env and compose.yml; a fake `docker` executable (shell script in a tmp dir put first on PATH) that handles `docker ps --format ...`
      (prints "lightrag-server"), `docker compose ... up -d ...` (exit 0) and `docker exec lightrag-server env` (prints the .env lines); a stub HTTP server emulating LightRAG
      (/health, DELETE /documents, /documents/status_counts, POST /documents/text, /documents/track_status/<id>), the gateway (POST /api/chat -> {"message":{"content":"stub"}}), sigma /v1/models and the judge
      (/v1/chat/completions -> JSON verdict). Module-level URL constants may be patched to the stub's port. 2 experiments (one reingest, one probes-only), tiny probe files (3 questions) and a 1-transcript corpus,
      sleeps patched to ~0. Assert: state.json statuses, results.md written, env restored to byte-identical original, no key values in loop.log.
4. Run the whole suite; all existing tests must pass.
Report back: commit hash, test count, every bug the new tests found and fixed (list them).
