#!/usr/bin/env python3
"""scripts/overnight_loop.py — unattended overnight accuracy-loop driver.

Runs an ordered list of experiments against a live LightRAG stack on the
Thinkpad with no agent and no approval prompts: change whitelisted LightRAG
env vars, optionally re-ingest the transcript corpus, run the probe sets, judge
every answer with an independent LLM, record scores, and always put the
production config back at the end.

Usage:
    python3 scripts/overnight_loop.py --root ~/rag-project \
        --experiments experiments.json --out ~/rag-project/probe/loop \
        --max-hours 11 [--dry-run] [--resume] [--retry-errors] \
        [--draws 3] [--compare ARM_A ARM_B] [--second-judge-model MODEL] \
        [--no-retrieval-recall] [--recall-mode mix] [--probes-only --arm-name NAME]

    python3 scripts/overnight_loop.py --root ~/rag-project \
        --rejudge ~/rag-project/probe/loop

    python3 scripts/overnight_loop.py --root ~/rag-project \
        --probes-only --arm-name production-1200 --out ~/rag-project/loop-prod

--rejudge is the offline mode: it re-grades answers already saved in
<out>/<name>/answers.jsonl using nothing but the judge endpoint, rewrites those
directories' verdicts and summaries, and writes <out>/results-rejudged.md. It
never touches .env, docker, LightRAG, the gateway, sigma, results.md or
state.json.

--probes-only is the read-only mode: it runs the dev and held-out probes, the judge and the
recall probe against whatever --gateway-url/--lightrag-url point at, as one pseudo-arm named by
``--arm-name`` (default ``probes-only``), and writes nothing to the stack. There is no .env write
or backup, no container restart or exec, no compose command, no store clear and no ingest - the
readiness checks use HTTP only, so ``docker ps`` is never called either. It reads the target
stack's .env for one field, the LightRAG API key, and never rewrites it. That is what makes it
safe to score production and a second stack on the same probes and the same judge: production
keeps serving while it is measured. It needs no ``--allow-production`` because it cannot wipe
anything, and it is refused, naming every offending arm, if the ``--experiments`` file carries
``"reingest": true``, non-empty ``env``/``gateway_env``/``baseline_env``, or a ``chunking`` block.
Scores land in the same place as any other arm: ``<out>/<arm-name>/`` (answers.jsonl,
verdicts.jsonl, summary.json), plus results.md and state.json under ``--out``.

Stopping a run: use ``kill -TERM <pid>``, NOT Ctrl-C, when the process is piped
(e.g. ``... | tee loop.log``). Ctrl-C reaches tee too, the pipe dies, and any
later stdout write raises BrokenPipeError - the driver swallows that so finalise
still restores .env and recreates lightrag-server, but a clean SIGTERM is the
intended way to stop. --retry-errors (with --resume) re-runs experiments whose
recorded status is "error"; "done" experiments stay skipped.

Failure isolation: after any experiment ends in error the driver rewrites .env to
the baseline, recreates every service the run has touched (lightrag-server, and
gateway-api if gateway_env was used) and waits for them to answer. If that does
not recover within HEALTH_TIMEOUT the whole run stops with status "fatal" and the
container's masked ERROR lines in the message. Readiness waits also poll
``docker ps -a --filter name=<container> --format {{.State}}``, so a container that
exited or keeps restarting fails in seconds instead of minutes.

An experiment may also carry ``"transcript_prep": true`` to run every corpus file
through ``app.transcript_prep`` (contextual chunk headers + speaker-turn
normalisation) before posting it to ``/documents/text``. The loop bypasses the
worker, so without this key an experiment cannot measure what the
``TRANSCRIPT_PREP_ENABLED`` worker would send. Default false: file text is posted
unchanged, exactly as before.

An experiment may also carry ``"structure_chunking": {"max_piece_tokens": 7500,
"min_piece_tokens": 150, "header_tokens": 25, "headers": true, "merge_until": "floor"}`` to chunk each corpus
file with (``budget_tokens`` is still accepted as an explicit ceiling, for size arms):
``app.structure_chunker`` - the shipped structure-aware pre-chunker behind the worker's
``STRUCTURE_CHUNKING_ENABLED`` - and post the result with the ``fixed_token`` +
``split_by_character_only`` params it produced. In other words: our code chooses every
cut, and LightRAG is told to cut only there. The key is validated at startup (types,
bounds, unknown keys) and cannot be combined with ``chunking``: an arm that sends two
instructions about where the cuts fall answers nothing. Absent, the request body is
byte-identical to what this driver posted before the key existed.

An experiment may also carry ``"chunking": {"strategy": ..., "params": {...}}``, the
per-request chunker LightRAG 1.5.7 accepts on ``/documents/text``. Strategies and
param keys are checked structurally at startup (LightRAG itself answers an unknown
key, a bool-as-int or a numeric string with a synchronous 422), and whatever passes
is forwarded untouched - this driver never invents a param name. Default absent: the
request body is byte-identical to what was posted before the key existed. Omitting
``chunking`` entirely keeps LightRAG's fixed-token default.

Where the run goes is now explicit: ``--lightrag-url``, ``--gateway-url``,
``--lightrag-container``, ``--gateway-container``, ``--gateway-service``,
``--env-file``, ``--worker-container``, ``--compose-file`` and ``--compose-project``.
The worker guard checks the container you name, and ``none`` skips it for a stack with
no ingest worker (refused when the target is production, because that guard is what
protects the production store). Every ``docker compose`` call uses ``--compose-file``
and ``-p --compose-project``, so a second stack in its own compose project is the one
that gets recreated. Leaving them out points at production exactly as before. Because a
reingest clears the LightRAG store it points at, a run on the production LightRAG
with any ``"reingest": true`` experiment now stops before the first request with
"this would wipe the production store" unless ``--allow-production`` is given. That
applies to the defaults too, so an existing runbook that reingests production must
pass ``--allow-production`` explicitly. ``--dry-run`` is exempt (it issues no
requests) and prints the same text as a warning.

Corpus files are logged by position (``doc 3/6``), never by name: transcript file
names are themselves sensitive.

Measuring (what the instrument can actually see)
------------------------------------------------
The adversarial review's central point was that this harness could not see most of
the effects it was testing, so the numbers below are what changed, each tied to the
finding it answers:

* ``--draws N`` asks every probe N times (default 1, unchanged). One draw over ~40
  questions resolves about one standard error, which is not enough to adopt an arm
  (F25). Per-probe means across draws go into ``<name>/summary.json`` under
  ``probe_scores``.
* ``--compare A B`` writes a paired per-probe difference A - B with a bootstrap 95%
  interval (cluster bootstrap over probes, fixed seed, so the same two arms always
  give the same numbers) into results.md and ``state.json``. An interval covering
  zero is printed as "not resolved", which is the honest reading of a small set.
* Retrieval recall: for every probe that carries a gold quote (``evidence``, or a
  turn id in ``evidence_turn``/``turn_id``), the driver posts ``/query`` with
  ``only_need_context: true`` and checks whether that quote is in what LightRAG
  returned. That splits retrieval failures from synthesis failures without any
  judge. It sends no top_k/chunk_top_k/token budgets: the container env the arm set
  is what is being measured. ``--no-retrieval-recall`` skips it; a probe with no
  gold quote is ``na``, never a miss. Reported as ``evidence in context%``, which is
  the binary success gate and is NOT changed by anything below.
* Graded retrieval quality (``scripts/ranking_metrics.py``): the same recall probe
  response is also scored per returned STRING (unit), so an arm can be told apart
  from one that buries the evidence. Per arm, over the probes that carry evidence
  only: ``evidence_recall`` (mean fraction of a probe's gold quotes found), ``mrr``,
  ``ndcg@5``, ``hit@1``, ``hit@5``, ``average_precision``, ``graded_recall``, plus
  ``any_hit_pct``/``all_hit_pct`` so this instrument and the Thinkpad recall helpers
  (which require EVERY quote) can be reconciled. A probe may grade its quotes with
  the optional ``evidence_grades`` list (ints 0-2, aligned with ``evidence``; 2 =
  contains the answer, 1 = partly relevant, 0 = irrelevant); missing or malformed
  means all 2, so every existing probe file scores exactly as before. Rank numbers
  carry ``"rank_order": "unverified-order"``: rank is position in the response walk,
  and nobody has confirmed that LightRAG lists chunks in retrieval order.
* Attribution (F4): the content rubric no longer folds speaker attribution in, and
  a separate judge call grades it right/wrong/na, reported as its own columns.
  Only probes with ``expected_speaker`` are graded, so an existing probe set is
  unaffected.
* Refusals and accuracy are separated (F7): ``ok%`` is kept for comparability with
  old summaries, and ``heldout answerable%`` (correct over answerable probes) plus
  ``refusal acc%`` (refused over unanswerable probes) are reported next to it. A
  system that refuses more cannot raise accuracy. The final recommendation ranks arms
  by ``dev answerable-correct%``, not by ok+0.5*partial%: the old headline counted a
  correct refusal as answered, so an arm that answered less could win it. The old
  number is still printed beside the new one, and a state.json written before the new
  key existed still ranks by the old one.
* ``--second-judge-model`` (off by default) re-grades a deterministic sample
  (``--second-judge-fraction``, default 0.2) with another model and reports
  agreement, because the answerer and the judge are the same family (F6).

An experiment may also carry ``"gateway_env": {...}`` with the gateway knobs
(BRIDGE_*, GATEWAY_CHUNK_TOP_K, CITATION_TOP_K). They live in the same .env, are
verified against the rag-gateway-api container, and that service is recreated and
waited for too. ENTITY_TYPES is refused: LightRAG v1.5.7 will not start with it.

Outputs, all under --out:
    state.json     resumable run state, written atomically after every step, plus
                   the comparison block when --compare was given
    heartbeat      ISO timestamp, rewritten at least every 60 s while waiting
    loop.log       human-readable log (appended)
    results.md     one row per experiment, dev + heldout columns, plus the paired
                   comparison section when --compare was given
    env.original   the untouched copy of <root>/.env (chmod 600)
    <name>/answers.jsonl, verdicts.jsonl, summary.json (verdict rows also carry
                   draw, attribution and recall; answer rows carry recall_metrics;
                   summaries carry probe_scores and probe_ranking_metrics)

Stdlib only (urllib, json, subprocess, argparse, time, pathlib, re) plus three
first-party imports: ``app.transcript_prep`` and ``app.structure_chunker``, the same
transcript preprocessor and chunker the ingest worker uses, so an experiment
measures exactly what production would send, and the sibling
``scripts/ranking_metrics.py``, which is pure arithmetic and imports nothing back.
Those modules are themselves stdlib-only and read no settings. Every side effect
goes through the Ops class so the tests can replace it with a fake: no network, no
docker, no sleeping in tests.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Make the repo's own package importable when this script is started by absolute
# path from anywhere (the loop is run as scripts/overnight_loop.py).
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.structure_chunker import (
    DEFAULT_HEADER_TOKENS,
    DEFAULT_MERGE_UNTIL,
    DEFAULT_MAX_PIECE_TOKENS,
    DEFAULT_MIN_PIECE_TOKENS,
    chunk_document as structure_chunk_document,
    render_for_lightrag,
)
from app.transcript_prep import DEFAULT_HEADER_EVERY_TOKENS, prepare_transcript

# The sibling script dir is sys.path[0] when this file is run as
# scripts/overnight_loop.py, but not when a test imports it from the repo root,
# so make the import of the ranking-metric module work either way.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# Pure arithmetic on graded evidence, no I/O. normalise_quote and the response walk
# live THERE and are re-exported here, so this file, chunk_embed_bench.py and the
# Thinkpad recall helpers cannot drift to different definitions of a match.
from ranking_metrics import (  # noqa: E402
    ECHO_KEYS as _RECALL_ECHO_KEYS,
    RANK_ORDER_MARKER,
    context_units as ranked_units,
    mean_metric,
    normalise_quote,
    score_probe,
)

# ── Configuration surface ───────────────────────────────────────────────────

# The only .env keys this script may ever change.
ENV_WHITELIST = frozenset({
    "MAX_ASYNC",
    "LLM_MODEL_MAX_ASYNC",
    "MAX_PARALLEL_INSERT",
    "MAX_GLEANING",
    "CHUNK_SIZE",
    "CHUNK_OVERLAP_SIZE",
    "TOP_K",
    "CHUNK_TOP_K",
    "MAX_ENTITY_TOKENS",
    "MAX_RELATION_TOKENS",
    "MAX_TOTAL_TOKENS",
    "SUMMARY_MAX_TOKENS",
    "FORCE_LLM_SUMMARY_ON_MERGE",
    "RELATED_CHUNK_NUMBER",
    "KG_CHUNK_PICK_METHOD",
    "RERANK_BY_DEFAULT",
})

# Never changeable, even if a future edit whitelists them. The whitelist above is
# the only allow-list, so LLM_* / EMBEDDING_* are already excluded except
# LLM_MODEL_MAX_ASYNC, which the brief whitelists on purpose: it is an
# async-concurrency knob, not model selection. Credentials are banned by name and
# suffix regardless.

# The gateway (rag-gateway-api) reads the same <root>/.env through env_file, so
# an experiment may tune these too. They are a separate allow-list because the
# two services read different knobs out of one file.
GATEWAY_ENV_WHITELIST = frozenset({
    "BRIDGE_TOP_K",
    "BRIDGE_CHUNK_TOP_K",
    "BRIDGE_MAX_ENTITY_TOKENS",
    "BRIDGE_MAX_RELATION_TOKENS",
    "BRIDGE_MAX_TOTAL_TOKENS",
    "BRIDGE_ENABLE_RERANK",
    "GATEWAY_CHUNK_TOP_K",
    "CITATION_TOP_K",
})

NEVER_CHANGE_EXACT = frozenset({
    "LIGHTRAG_API_KEY",
    "OPENAI_API_KEY",
    "LLM_MODEL",
    "LLM_BINDING",
    "LLM_BINDING_HOST",
    "EMBEDDING_MODEL",
    "EMBEDDING_BINDING",
    "EMBEDDING_BINDING_HOST",
    "ENTITY_TYPES",
})
# Keys that are not credentials but still cannot be set, with the reason that has
# to appear in the refusal.
NEVER_CHANGE_REASONS = {
    "ENTITY_TYPES": "ENTITY_TYPES is not supported by LightRAG v1.5.7 (server refuses to start)",
}
NEVER_CHANGE_SUFFIX = re.compile(r"(_API_KEY|_API_KEYS|_KEY|_SECRET|_PASSWORD)$")

# Local oMLX judge endpoint. The key is the placeholder the local server
# expects; it is never logged. Override with OVERNIGHT_JUDGE_API_KEY.
JUDGE_URL = "http://192.168.1.190:1234/v1/chat/completions"
JUDGE_MODEL = "qwen3.6-35b"
JUDGE_KEY_DEFAULT = "1234"
# qwen3.6-35b on the Thinkpad is a reasoning model: it spends hundreds of tokens
# on message.reasoning_content before answering, so a small budget returns an
# empty message.content every time. 4000 leaves room for the verdict.
JUDGE_MAX_TOKENS = 4000
JUDGE_TIMEOUT = 300

# Service liveness probe ("sigma" on the Thinkpad).
SIGMA_URL = "http://192.168.1.195:1234/v1/models"

GATEWAY_URL = "http://127.0.0.1:8020"
LIGHTRAG_URL = "http://localhost:9621"
CONTAINER = "lightrag-server"
GATEWAY_SERVICE = "gateway-api"
GATEWAY_CONTAINER = "rag-gateway-api"
WORKER_CONTAINER = "rag-ingest-worker"

# The constants above ARE the production target. They are kept as defaults so an
# untouched invocation behaves exactly as it always did; --lightrag-url and
# friends exist so a run can point at a second stack instead.
PRODUCTION_LIGHTRAG_URL = LIGHTRAG_URL
PRODUCTION_GATEWAY_URL = GATEWAY_URL

# LightRAG 1.5.7 accepts a per-request chunker on POST /documents/text. The
# strategy names and the per-strategy param keys below come from Hermes reading
# the 1.5.7 image: LightRAG is strict - an unknown key, a bool passed where an int
# is wanted, or a number passed as a string all give a synchronous HTTP 422.
# Checking the same shape here turns a typo into a message at startup instead of a
# lost 3-hour ingest arm.
CHUNKING_STRATEGIES = frozenset({
    "fixed_token", "recursive_character", "semantic_vector",
    "paragraph_semantic", "custom",
})

# Which param keys each strategy accepts. Keys are optional; a wrong type or an
# unknown key is refused. "custom" is LightRAG's escape hatch and its param names
# are not in Hermes' report, so they are not restricted here - a typo in a custom
# arm still gets LightRAG's 422.
CHUNKING_PARAM_KEYS: dict[str, frozenset[str]] = {
    "fixed_token": frozenset({
        "chunk_token_size", "chunk_overlap_token_size",
        "split_by_character", "split_by_character_only",
    }),
    "recursive_character": frozenset({
        "chunk_token_size", "chunk_overlap_token_size", "separators",
    }),
    "paragraph_semantic": frozenset({
        "chunk_token_size", "chunk_overlap_token_size", "drop_references",
    }),
    "semantic_vector": frozenset({
        "chunk_token_size", "breakpoint_threshold_type",
        "breakpoint_threshold_amount", "buffer_size",
    }),
    "custom": frozenset(),
}

# Env-only on 1.5.7: sending it in a request is a 422, so refuse it by name here.
CHUNKING_ENV_ONLY_KEYS = frozenset({"sentence_split_regex"})

BREAKPOINT_TYPES = frozenset({"percentile", "standard_deviation", "interquartile", "gradient"})
MAX_SEPARATORS = 64
MAX_SEPARATOR_CHARS = 256


def _is_int(value: Any) -> bool:
    # bool is a subclass of int in Python, and LightRAG rejects a bool where it
    # wants an int, so exclude it explicitly.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def chunking_param_problems(strategy: str, params: dict[str, Any]) -> list[str]:
    """Structural check of a chunking params object. Returns problems; never raises.

    Types are checked the way LightRAG checks them - a numeric string is a
    problem, not something to coerce - and nothing is rewritten: whatever passes
    is forwarded to the request untouched. Separator CONTENT (escaped, not a
    regex) stays LightRAG's check: a legitimate separator may contain brackets, so
    guessing at it here would refuse valid arms.
    """
    problems: list[str] = []
    allowed = CHUNKING_PARAM_KEYS.get(strategy)
    for key in params:
        if str(key) in CHUNKING_ENV_ONLY_KEYS:
            problems.append(f"{key} is env-only on LightRAG 1.5.7 and is rejected (422) in a request")
        elif allowed is not None and str(key) not in allowed:
            listed = ", ".join(sorted(allowed)) if allowed else "none documented for this strategy"
            problems.append(f"{key} is not a {strategy} param (accepted: {listed})")
    if "chunk_token_size" in params and not _is_int(params["chunk_token_size"]):
        problems.append("chunk_token_size must be an integer >= 1")
    if "chunk_overlap_token_size" in params:
        overlap = params["chunk_overlap_token_size"]
        if strategy == "semantic_vector":
            problems.append("chunk_overlap_token_size is not accepted by semantic_vector (422)")
        elif not _is_int(overlap) or overlap < 0:
            problems.append("chunk_overlap_token_size must be an integer >= 0")
        elif _is_int(params.get("chunk_token_size")) and overlap >= params["chunk_token_size"]:
            problems.append("chunk_overlap_token_size must be smaller than chunk_token_size")
    if "split_by_character" in params and not isinstance(params["split_by_character"], str):
        problems.append("split_by_character must be a string")
    for key in ("split_by_character_only", "drop_references"):
        if key in params and not isinstance(params[key], bool):
            problems.append(f"{key} must be a boolean, not an int or a string")
    if "separators" in params:
        separators = params["separators"]
        if not isinstance(separators, list):
            problems.append("separators must be a list of strings")
        else:
            if len(separators) > MAX_SEPARATORS:
                problems.append(f"separators accepts at most {MAX_SEPARATORS} entries")
            if any(not isinstance(item, str) for item in separators):
                problems.append("every separator must be a string")
            if any(isinstance(item, str) and len(item) > MAX_SEPARATOR_CHARS for item in separators):
                problems.append(f"every separator must be at most {MAX_SEPARATOR_CHARS} characters")
    if strategy == "semantic_vector":
        kind = params.get("breakpoint_threshold_type")
        if kind is not None and (not isinstance(kind, str) or kind not in BREAKPOINT_TYPES):
            problems.append("breakpoint_threshold_type must be one of " + ", ".join(sorted(BREAKPOINT_TYPES)))
        amount = params.get("breakpoint_threshold_amount")
        if amount is not None:
            if not _is_number(amount) or amount <= 0:
                problems.append("breakpoint_threshold_amount must be a number > 0")
            elif kind in ("percentile", "gradient") and amount > 100:
                problems.append(f"breakpoint_threshold_amount must be within (0, 100] when the type is {kind}")
        if "buffer_size" in params and (not _is_int(params["buffer_size"]) or params["buffer_size"] < 1):
            problems.append("buffer_size must be an integer >= 1")
    return problems


# --worker-container accepts this to mean "this stack has no ingest worker", which
# skips the guard. Never valid against the production store: the guard is what stops
# the loop clearing LightRAG while an ingest worker is writing into it.
WORKER_GUARD_NONE = "none"


def _normalise_url(raw: str) -> str:
    return str(raw).strip().rstrip("/").lower()


@dataclass(frozen=True)
class Target:
    """What a run points at: two URLs, two containers, one .env.

    Unset fields resolve to the module constants (LIGHTRAG_URL, GATEWAY_URL,
    CONTAINER, ...), the values this driver hardcoded before the flags existed, so
    an untouched invocation goes to exactly the same stack it always did. They
    resolve at construction rather than at class-definition time, because those
    globals are the seam tests use to point the driver at a local stub: a default
    frozen at import would silently keep the production URL and a test would then
    talk to a real stack.
    """

    lightrag_url: str | None = None
    gateway_url: str | None = None
    lightrag_container: str | None = None
    gateway_container: str | None = None
    gateway_service: str | None = None
    env_file: str | None = None  # None -> <root>/.env, as always
    lightrag_service: str | None = None  # compose service; None -> the container name
    worker_container: str | None = None  # or WORKER_GUARD_NONE to skip the guard
    compose_file: str | None = None  # None -> <root>/compose.yml, resolved per call
    compose_project: str | None = None  # -p NAME, for a stack in its own project

    def __post_init__(self) -> None:
        for name, constant in (
            ("lightrag_url", LIGHTRAG_URL),
            ("gateway_url", GATEWAY_URL),
            ("lightrag_container", CONTAINER),
            ("gateway_container", GATEWAY_CONTAINER),
            ("gateway_service", GATEWAY_SERVICE),
            ("worker_container", WORKER_CONTAINER),
        ):
            if getattr(self, name) is None:
                object.__setattr__(self, name, constant)

    def is_production_lightrag(self) -> bool:
        return _normalise_url(self.lightrag_url) == _normalise_url(PRODUCTION_LIGHTRAG_URL)

    def guard_skipped(self) -> bool:
        return str(self.worker_container).strip().lower() == WORKER_GUARD_NONE


def production_wipe_refusal(target: Target, config: dict[str, Any]) -> str | None:
    """Return a refusal message when this run would wipe the production store.

    The driver clears the LightRAG store it points at before re-ingesting, so a
    run that defaults to the production URL and contains any ``"reingest":
    true"` experiment is a wipe of production. It now needs --allow-production
    explicitly. --dry-run is checked by the caller: it issues no requests.
    """
    if not target.is_production_lightrag():
        return None
    if not any(bool(exp.get("reingest")) for exp in config.get("experiments", [])):
        return None
    return (
        f"this would wipe the production store: --lightrag-url is the production "
        f"LightRAG ({PRODUCTION_LIGHTRAG_URL}) and the experiments file requests "
        "reingest: true, which deletes every document before re-inserting. Point "
        "--lightrag-url at a second stack, or pass --allow-production if you mean it."
    )


def worker_guard_refusal(target: Target) -> str | None:
    """Refuse a run that switches off the worker guard while pointing at production."""
    if not target.guard_skipped():
        return None
    if not target.is_production_lightrag():
        return None
    return (
        f"--worker-container {WORKER_GUARD_NONE} switches off the guard that stops this driver "
        f"clearing a LightRAG store while {WORKER_CONTAINER} writes into it, and this run points "
        "at the production LightRAG. Use it only with --lightrag-url pointed at a second stack "
        "that has no ingest worker."
    )

DEV_PROBE_FILE = "probe/solvay-dev-v1.jsonl"
HELDOUT_PROBE_FILE = "probe/solvay-heldout-v1.jsonl"
CORPUS_FILE = "probe/loop_corpus.json"


def resolve_probe_path(raw: str) -> str:
    """Normalise a --dev-probes / --heldout-probes value.

    An absolute path stays absolute; a value that already starts with "probe/"
    stays root-relative (the shape of the built-in defaults); anything else is
    taken relative to <root>/probe. The result is always used as <root> / value,
    and Path drops the left side when the right one is absolute.
    """
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return str(candidate)
    if candidate.parts and candidate.parts[0] == "probe":
        return str(candidate)
    return str(Path("probe") / candidate)

# Timeouts, in seconds.
HEALTH_TIMEOUT = 300
CLEAR_TIMEOUT = 600
DOC_TIMEOUT = 90 * 60
PROBE_TIMEOUT = 900
SERVICE_DOWN_WAIT = 600
HARD_ABORT_GRACE = 3600
HEARTBEAT_INTERVAL = 60

SUCCESS_HELDOUT_OK_PCT = 90.0
SUCCESS_HALLUCINATION_PCT = 5.0
SUCCESS_REFUSAL_ACCURACY_PCT = 80.0

VERDICT_VALUES = ("correct", "partial", "wrong", "hallucinated", "refused_ok", "refused_wrong")
JUDGE_ERROR = "judge_error"
# Verdicts that mean "this question produced no grade" and are kept out of every
# percentage: judge_error (the judge failed) and no_answer (the stack never
# answered, so there was nothing to grade).
NO_ANSWER = "no_answer"
EXCLUDED_VERDICTS = (JUDGE_ERROR, NO_ANSWER)

# Judge rubric v2. v1 left refusals and paraphrase to the judge's taste and a
# failure analysis of 165 graded answers found a correct refusal graded "wrong"
# and a clean refusal graded "hallucinated" for naming a real-but-adjacent entity.
# The rules below are asserted as text in tests/test_overnight_loop.py: if they
# silently disappear the scores move for the wrong reason and nothing notices.
JUDGE_PROMPT = """You are grading one answer from a document question-answering system.

Question:
{question}

Expected answer (ground truth):
{expected_answer}

Evidence quote from the documents:
{evidence}

System answer:
{answer}

Rules (apply in this order, and name the rule that decided the verdict):
1. Unanswerable question - the expected answer is "not in the documents": a clear
   statement that the information is not available or not found in the provided
   context is refused_ok, EVEN IF the answer names real entities or mentions
   nearby facts, as long as it does not assert the requested fact itself. Verdict
   hallucinated ONLY if the answer asserts a specific value, name, date or
   decision that answers the question and is not supported by the evidence or the
   expected answer.
2. Answerable question - the expected answer is a real fact: accept paraphrase,
   different wording, extra correct detail and a different order of the facts.
   correct = every key fact in the expected answer is present and nothing
   contradicts the evidence. partial = some key facts are present and none
   contradict. wrong = key facts are missing or contradicted. A refusal ("not in
   the documents") on an answerable question is refused_wrong, unless the question
   is itself unanswerable from the evidence (then refused_ok).
3. Speaker attribution: do not mark an answer wrong merely because it attributes a statement to a different speaker label, or omits who said it. Attribution is NOT part of this verdict at all - it is graded by a separate call with its own rubric, so never fold it in here.
4. Judge only against the evidence quote and the expected answer given, never
   against outside knowledge. Extra detail that is not in the evidence is not an
   error unless it contradicts the evidence.
5. The "reason" must be ONE sentence and must name which rule decided it, for
   example "rule1 clean refusal" or "rule2 all key facts present".

Verdicts: correct, partial, wrong, hallucinated, refused_ok, refused_wrong.

Reply with ONLY this JSON object and nothing else:
{{"verdict": "correct|partial|wrong|hallucinated|refused_ok|refused_wrong", "reason": "<one sentence naming the deciding rule>"}}
"""

# Finding F4: rule 3 above deliberately refuses to fold attribution into the content
# verdict, which is also exactly what speaker-aware chunking is supposed to improve.
# So attribution gets its own call, its own verdict and its own column. "na" when
# the probe pins no speaker, or when there was no answer to attribute anything to.
ATTRIBUTION_VALUES = ("right", "wrong", "na")
ATTRIBUTION_ERROR = "attribution_error"

ATTRIBUTION_PROMPT = """You are checking ONE thing about an answer from a document
question-answering system: did it attribute the statement to the right speaker?

Question:
{question}

Expected answer (ground truth):
{expected_answer}

Speaker the expected answer attributes this to:
{expected_speaker}

Evidence quote from the documents:
{evidence}

System answer:
{answer}

Rules (apply in this order):
1. right - the answer names the expected speaker, or an unambiguous form of that
   name (an initial, a title, a label from the transcript) for the statement the
   question asks about.
2. wrong - the answer attributes the statement to a DIFFERENT named speaker than
   the expected one. Contradicting the expected speaker is wrong.
3. na    - the answer neither states nor implies who spoke, or the expected speaker
   is not identified, or the answer refused to answer. Never guess: absence is na,
   not wrong.
4. Judge the attribution only. Content errors, extra detail and wording are
   irrelevant here and must not change this verdict.

Reply with ONLY this JSON object and nothing else:
{{"attribution": "right|wrong|na", "reason": "<one sentence>"}}
"""


class LoopConfigError(Exception):
    """The experiments file is invalid or tries to change a forbidden key."""


class StepError(Exception):
    """A step failed after its retries; the experiment is marked error."""


class FatalRun(Exception):
    """Unsafe to continue the run at all (for example the worker is running)."""


class UnrecoverableRun(FatalRun):
    """The stack cannot be put back into its baseline shape, so no further
    experiment can be trusted. finalise still runs."""


# ── experiments.json ────────────────────────────────────────────────────────

def is_forbidden_key(key: str) -> bool:
    """Credential and model-selection keys: never touchable, whitelist or not."""
    if key in NEVER_CHANGE_EXACT or NEVER_CHANGE_SUFFIX.search(key):
        return True
    return False


def forbidden_reason(key: str) -> str:
    return NEVER_CHANGE_REASONS.get(key) or f"key {key} may never be changed"


def check_env_keys(
    mapping: dict[str, Any],
    where: str,
    *,
    allow: frozenset[str] = ENV_WHITELIST,
) -> None:
    for key in mapping:
        key = str(key)
        if is_forbidden_key(key):
            raise LoopConfigError(f"{where}: {forbidden_reason(key)}")
        if key not in allow:
            raise LoopConfigError(f"{where}: key {key} is not in the whitelist")


def load_experiments(path: Path, allow_empty_experiments: bool = False
                     ) -> dict[str, Any]:
    """Parse and validate experiments.json, preserving file order.

    ``allow_empty_experiments`` is for --probes-only, which runs one synthetic arm and so
    has no use for the list; every other entry in the file is still validated.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LoopConfigError(f"experiments file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LoopConfigError(f"experiments file is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise LoopConfigError("experiments file must be a JSON object")
    baseline = raw.get("baseline_env") or {}
    if not isinstance(baseline, dict):
        raise LoopConfigError("baseline_env must be an object")
    check_env_keys(baseline, "baseline_env")

    entries = raw.get("experiments")
    if not isinstance(entries, list):
        raise LoopConfigError("experiments must be a list")
    if not entries and not allow_empty_experiments:
        raise LoopConfigError("experiments must be a non-empty list")

    experiments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise LoopConfigError(f"experiments[{index}] must be an object")
        name = str(entry.get("name") or "").strip()
        if not name:
            raise LoopConfigError(f"experiments[{index}]: name is required")
        if name in seen:
            raise LoopConfigError(f"duplicate experiment name: {name}")
        seen.add(name)
        env = entry.get("env") or {}
        if not isinstance(env, dict):
            raise LoopConfigError(f"experiment {name}: env must be an object")
        check_env_keys(env, f"experiment {name}")
        gateway_env = entry.get("gateway_env") or {}
        if not isinstance(gateway_env, dict):
            raise LoopConfigError(f"experiment {name}: gateway_env must be an object")
        check_env_keys(gateway_env, f"experiment {name}", allow=GATEWAY_ENV_WHITELIST)
        concurrency = int(entry.get("probe_concurrency", 1) or 1)
        if concurrency < 1:
            raise LoopConfigError(f"experiment {name}: probe_concurrency must be >= 1")
        # Experiment-level per-request chunker (LightRAG 1.5.7). Validated only
        # structurally: strategy must be a known name, params must be an object.
        # Key names inside params belong to LightRAG and are never touched here.
        chunking: dict[str, Any] | None = None
        if entry.get("chunking") is not None:
            raw_chunking = entry["chunking"]
            if not isinstance(raw_chunking, dict):
                raise LoopConfigError(f"experiment {name}: chunking must be an object")
            strategy = raw_chunking.get("strategy")
            if not isinstance(strategy, str) or strategy not in CHUNKING_STRATEGIES:
                raise LoopConfigError(
                    f"experiment {name}: chunking.strategy must be one of "
                    + ", ".join(sorted(CHUNKING_STRATEGIES))
                )
            params = raw_chunking.get("params", {})
            if not isinstance(params, dict):
                raise LoopConfigError(f"experiment {name}: chunking.params must be an object")
            problems = chunking_param_problems(strategy, params)
            if problems:
                raise LoopConfigError(
                    f"experiment {name}: chunking.params for {strategy}: " + "; ".join(problems)
                )
            chunking = {"strategy": strategy, "params": params}
        structure_chunking = structure_chunking_spec(entry, name)
        if entry.get("recall_mode") is not None and not isinstance(entry["recall_mode"], str):
            raise LoopConfigError(f"experiment {name}: recall_mode must be a string")
        experiments.append({
            "name": name,
            "env": {str(k): str(v) for k, v in env.items()},
            "gateway_env": {str(k): str(v) for k, v in gateway_env.items()},
            "reingest": bool(entry.get("reingest", False)),
            # Experiment-level transcript preparation (see app/transcript_prep.py).
            # Default false, so an experiments.json written before this key exists
            # behaves exactly as before.
            "transcript_prep": bool(entry.get("transcript_prep", False)),
            # Experiment-level chunker override; None means the request body is
            # byte-identical to what this driver posted before the key existed.
            "chunking": chunking,
            # Experiment-level structure-aware pre-chunking (app/structure_chunker.py):
            # the loop chunks the text itself and posts the delimiter markers plus the
            # fixed_token/split_by_character_only params. None (the default) leaves the
            # request body exactly as it was before the key existed.
            "structure_chunking": structure_chunking,
            "probe_concurrency": concurrency,
            # Which LightRAG mode the retrieval-recall probe asks for the context in.
            # The metric itself is on unless --no-retrieval-recall is given; this only
            # says which retrieval mode "the context" means for this arm.
            "recall_mode": str(entry.get("recall_mode") or ""),
        })
    return {
        "baseline_env": {str(k): str(v) for k, v in baseline.items()},
        "experiments": experiments,
    }


# ── .env rewriting (every untouched line stays byte-identical) ──────────────

_ENV_LINE = re.compile(r"^(\s*)(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def _quote_value(value: str) -> str:
    if value == "":
        return '""'
    if re.search(r'[\s#"\']', value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def rewrite_env(text: str, set_values: dict[str, str], remove_keys: set[str] | None = None) -> str:
    """Set/remove whitelisted keys in place; leave every other line identical.

    A set key that already exists is replaced in place (indentation and line
    ending preserved). A set key that does not exist is appended at the end.
    Keys in ``remove_keys`` — keys a previous experiment of this run added and
    this experiment no longer wants — are deleted.
    """
    remove_keys = remove_keys or set()
    lines = text.splitlines(keepends=True)
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.rstrip("\r\n")
        ending = line[len(stripped):] or "\n"
        match = _ENV_LINE.match(stripped)
        if match:
            key = match.group(2)
            if key in set_values:
                seen.add(key)
                output.append(f"{match.group(1)}{key}={_quote_value(set_values[key])}{ending}")
                continue
            if key in remove_keys:
                continue
        output.append(line)

    additions = [f"{key}={_quote_value(value)}" for key, value in set_values.items() if key not in seen]
    if additions:
        if output and not output[-1].endswith(("\n", "\r")):
            output[-1] = output[-1] + "\n"
        output.extend(addition + "\n" for addition in additions)
    return "".join(output)


def env_value(text: str, key: str) -> str | None:
    for line in text.splitlines():
        match = _ENV_LINE.match(line.strip())
        if match and match.group(2) == key:
            return match.group(3).strip().strip('"').strip("'")
    return None


# ── Judge output parsing ────────────────────────────────────────────────────

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def first_json_object(text: str) -> dict[str, Any] | None:
    """Return the first balanced JSON object found anywhere in ``text``."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start:index + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = text.find("{", start + 1)
    return None


def last_json_object(text: str) -> dict[str, Any] | None:
    """Return the LAST balanced JSON object found anywhere in ``text``."""
    found: dict[str, Any] | None = None
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start:index + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        found = parsed
                    break
        start = text.find("{", start + 1)
    return found


class JudgeTruncated(Exception):
    """The judge ran out of tokens before it could answer."""


def parse_verdict(text: str | None, *, prefer_last: bool = False) -> dict[str, str] | None:
    """Extract ``{"verdict": ..., "reason": ...}`` from a judge reply.

    Handles clean JSON, markdown-fenced JSON and prose wrapped around the
    object. ``prefer_last`` takes the final object instead of the first, which is
    what a reasoning model's chain of thought needs (the verdict comes last).
    Returns None when nothing usable is present.
    """
    if not text:
        return None
    candidate = text.strip()
    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    obj = last_json_object(candidate) if prefer_last else first_json_object(candidate)
    if obj is None:
        return None
    verdict = str(obj.get("verdict", "")).strip().lower()
    if verdict not in VERDICT_VALUES:
        return None
    return {"verdict": verdict, "reason": str(obj.get("reason", "")).strip()}


def judge_prompt_for(item: dict[str, Any], answer: str) -> str:
    return JUDGE_PROMPT.format(
        question=item.get("question") or "",
        expected_answer=item.get("expected_answer") or "",
        evidence=item.get("evidence") or "",
        answer=answer,
    )


def expected_speaker_of(item: dict[str, Any]) -> str:
    """The speaker a probe pins its answer to. Optional - never invented.

    Probe files may carry "expected_speaker" or "expected_speaker_label" (the label
    as written in the transcript). A probe with neither simply never gets an
    attribution grade, so adding this field cannot change an existing set's numbers.
    """
    for key in ("expected_speaker", "expected_speaker_label", "speaker"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def attribution_applicable(item: dict[str, Any]) -> bool:
    return bool(expected_speaker_of(item)) and not is_unanswerable(item)


def attribution_prompt_for(item: dict[str, Any], answer: str) -> str:
    return ATTRIBUTION_PROMPT.format(
        question=item.get("question") or "",
        expected_answer=item.get("expected_answer") or "",
        expected_speaker=expected_speaker_of(item) or "(not identified)",
        evidence=item.get("evidence") or "",
        answer=answer,
    )


def parse_attribution(text: str | None, *, prefer_last: bool = False) -> str | None:
    """Pull ``{"attribution": "right|wrong|na"}`` out of a judge reply."""
    if not text:
        return None
    candidate = text.strip()
    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    obj = last_json_object(candidate) if prefer_last else first_json_object(candidate)
    if obj is None:
        return None
    value = str(obj.get("attribution", "")).strip().lower()
    return value if value in ATTRIBUTION_VALUES else None


def judge_attribution(ops: Ops, item: dict[str, Any], answer: str, judge_key: str) -> str:
    """One attribution call. Raises JudgeTruncated on a truncated reply so the
    caller records attribution_error instead of a silent "na"."""
    content, reasoning, finish_reason = judge_reply(
        ops, attribution_prompt_for(item, answer), judge_key
    )
    verdict = parse_attribution(content)
    if verdict is not None:
        return verdict
    if finish_reason == "length":
        raise JudgeTruncated(f"attribution reply truncated at max_tokens={JUDGE_MAX_TOKENS}")
    verdict = parse_attribution(reasoning, prefer_last=True)
    if verdict is not None:
        return verdict
    raise RuntimeError("attribution judge reply unparsable")


def judge_reply(
    ops: Ops, prompt: str, judge_key: str, model: str | None = None
) -> tuple[str, str, str]:
    """One judge call. Returns (message.content, message.reasoning_content,
    choices[0].finish_reason). Transport and HTTP problems raise.

    ``model`` overrides the judge for a cross-check (finding F6: the answerer and
    the judge are the same model family). The payload is otherwise identical, so a
    second-judge number is comparable to the primary one.
    """
    payload = {
        "model": model or JUDGE_MODEL,
        "temperature": 0,
        "max_tokens": JUDGE_MAX_TOKENS,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
    }
    status, body = ops.http_json(
        "POST", JUDGE_URL,
        headers={"Authorization": f"Bearer {judge_key}"},
        payload=payload,
        timeout=JUDGE_TIMEOUT,
    )
    if status >= 400:
        raise RuntimeError(f"judge returned HTTP {status}")
    choices = body.get("choices") or [{}]
    choice = choices[0] or {}
    message = choice.get("message") or {}
    return (
        str(message.get("content") or ""),
        str(message.get("reasoning_content") or ""),
        str(choice.get("finish_reason") or ""),
    )


def judge_answer(
    ops: Ops, item: dict[str, Any], answer: str, judge_key: str,
    model: str | None = None,
) -> dict[str, str]:
    """Grade one answer against the item's evidence.

    The verdict normally sits in message.content. If it is missing and the call
    was not cut short, look for the last JSON object in reasoning_content, which
    is where a reasoning model puts it. Raises JudgeTruncated when the token
    budget was the problem, RuntimeError when the reply is simply unusable.
    """
    content, reasoning, finish_reason = judge_reply(
        ops, judge_prompt_for(item, answer), judge_key, model
    )
    verdict = parse_verdict(content)
    if verdict is not None:
        return verdict
    if finish_reason == "length":
        raise JudgeTruncated(
            f"finish_reason=length with no usable content at max_tokens={JUDGE_MAX_TOKENS}"
        )
    verdict = parse_verdict(reasoning, prefer_last=True)
    if verdict is not None:
        return verdict
    raise RuntimeError("judge reply unparsable")


# ── Scoring ─────────────────────────────────────────────────────────────────

# Ranking order: the honest column first, older summaries fall back to the ones they have.
RANKING_KEYS = ("dev_answerable_correct_pct", "dev_ok_partial_credit_pct",
                "dev_partial_credit_pct")

UNANSWERABLE = re.compile(
    r"(not in the documents|not available|not mentioned|unanswerable|no answer)",
    re.IGNORECASE,
)


def is_unanswerable(item: dict[str, Any]) -> bool:
    expected = str(item.get("expected_answer") or "").strip()
    return expected == "" or bool(UNANSWERABLE.search(expected))


# ── retrieval recall ───────────────────────────────────────────────────────
# The harness could only see the ANSWER, so a retrieval failure and a synthesis
# failure looked identical (adversarial review, "build an instrument that can see
# retrieval"). This asks LightRAG for the context it WOULD pass, and checks
# whether the probe's gold evidence is in it - no judge involved.

RECALL_VALUES = ("hit", "miss", "na")

# Every field below is copied from a call this repo already makes against LightRAG
# 1.5.7 (app/api.py build_ollama_bridge_query_payload and the /query path near
# line 1290): query, mode, response_type, only_need_context, only_need_prompt,
# stream, include_references, include_chunk_content. Nothing is invented.
# No top_k / chunk_top_k / max_*_tokens here on purpose: those come from the
# container env the arm itself set, so the metric measures the stack as
# configured rather than numbers this script made up. enable_rerank is likewise
# left out so LightRAG's own RERANK_BY_DEFAULT applies.
def recall_probe_payload(question: str, mode: str = "mix") -> dict[str, Any]:
    return {
        "query": question,
        "mode": mode,
        "response_type": "Multiple Paragraphs",
        "only_need_context": True,
        "only_need_prompt": False,
        "stream": False,
        "include_references": True,
        "include_chunk_content": True,
    }


# Keys whose values are the request echoed back, not retrieved material. The set
# itself lives in ranking_metrics.ECHO_KEYS (imported above as _RECALL_ECHO_KEYS)
# so the ordered extractor and the binary check share one definition.


def context_strings(payload: Any, *, skip: str = "") -> list[str]:
    """Every string the query response carries, at any depth, minus request echoes.

    What only_need_context returns is not documented in this repo, so the
    extractor refuses to guess an envelope: it walks the JSON and returns every
    string. That is a superset of the context and the chunk content, so a "hit"
    always means the gold text really was somewhere in what the stack returned.

    It is the ordered walk in ``ranking_metrics.context_units``, unchanged: the
    binary check below joins it, the rank metrics treat each entry as one unit.
    """
    return ranked_units(payload, skip=skip, echo_keys=_RECALL_ECHO_KEYS)


def gold_evidence_of(item: dict[str, Any]) -> list[str]:
    """Strings that would prove the gold evidence was retrieved. Optional in probes.

    Accepts "evidence" (the quote the judge already uses) and turn identifiers
    ("evidence_turn", "turn_id", "evidence_ids"), as a string or a list. A probe
    with none of them is not counted, rather than scored as a miss.
    """
    values: list[str] = []
    for key in ("evidence", "evidence_turn", "turn_id", "evidence_ids", "gold_span"):
        raw = item.get(key)
        if isinstance(raw, str):
            values.append(raw)
        elif isinstance(raw, (list, tuple)):
            values.extend(str(entry) for entry in raw)
    return [value for value in (normalise_quote(value) for value in values) if len(value) >= 4]


# "evidence_grades": optional, one int per entry of the probe's "evidence" list.
# 2 = the quote contains the answer, 1 = partly relevant, 0 = irrelevant. Absent,
# short, or out of range means 2, which is what an ungraded probe already asserted,
# so probe files written before this key exist are scored unchanged.
GRADE_VALUES = (2, 1, 0)


def evidence_grades_of(item: dict[str, Any]) -> list[int] | None:
    """The probe's own grade list, cleaned; None when the probe does not carry one."""
    raw = item.get("evidence_grades")
    if not isinstance(raw, (list, tuple)):
        return None
    grades: list[int] = []
    for value in raw:
        grades.append(value if isinstance(value, int) and not isinstance(value, bool)
                     and value in GRADE_VALUES else 2)
    return grades


def gold_evidence_with_grades(item: dict[str, Any]) -> list[tuple[str, int]]:
    """(normalised quote, grade) pairs, aligned the way ``gold_evidence_of`` collects them.

    Grades index the probe's own ``evidence`` list positionally, so a probe listing
    three quotes grades them in that order; turn-identifier keys carry no grades and
    default to 2. A quote too short to be evidence is dropped, exactly as in
    ``gold_evidence_of``, and the grade slot it occupied is dropped with it.
    """
    grades = evidence_grades_of(item)
    pairs: list[tuple[str, int]] = []
    for key in ("evidence", "evidence_turn", "turn_id", "evidence_ids", "gold_span"):
        raw = item.get(key)
        values = [raw] if isinstance(raw, str) else (
            [str(entry) for entry in raw] if isinstance(raw, (list, tuple)) else []
        )
        for index, value in enumerate(values):
            grade = (grades[index] if key == "evidence" and grades is not None
                     and index < len(grades) else 2)
            normalised = normalise_quote(value)
            if len(normalised) >= 4:
                pairs.append((normalised, grade))
    return pairs


def evidence_in_context(item: dict[str, Any], payload: Any, *, question: str = "") -> str:
    """hit / miss / na for one probe: is its gold evidence in the returned context?"""
    gold = gold_evidence_of(item)
    if not gold:
        return "na"
    haystack = normalise_quote(" \n ".join(context_strings(payload, skip=question)))
    if not haystack:
        return "miss"
    return "hit" if any(needle in haystack for needle in gold) else "miss"


# ── paired statistics ──────────────────────────────────────────────────────
# One draw per probe on 40 questions cannot resolve the differences being chased
# (finding F25). Draws give a per-probe mean, and the comparison between two named
# arms is paired per probe with a cluster bootstrap over probes.

def mean_of(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def paired_bootstrap_difference(
    a: dict[str, float],
    b: dict[str, float],
    *,
    samples: int = 2000,
    seed: int = 0,
    confidence: float = 0.95,
) -> dict[str, Any]:
    """Paired mean difference (a - b) with a bootstrap CI, resampled by PROBE.

    Probes are the cluster: each probe contributes one paired value (its mean
    across draws), and the bootstrap resamples probes with replacement. The seed is
    fixed so the same two arms always produce the same interval - an interval that
    moves between readings of the same data is not evidence.
    """
    shared = sorted(set(a) & set(b))
    if not shared:
        return {"n": 0, "mean_a": 0.0, "mean_b": 0.0, "difference": 0.0,
                "ci_low": 0.0, "ci_high": 0.0, "excludes_zero": False, "only_in_a": 0,
                "only_in_b": 0}
    diffs = [float(a[key]) - float(b[key]) for key in shared]
    observed = mean_of(diffs)
    generator = random.Random(seed)
    means: list[float] = []
    for _ in range(max(1, int(samples))):
        means.append(mean_of([diffs[generator.randrange(len(diffs))] for _ in diffs]))
    means.sort()
    tail = (1.0 - confidence) / 2.0
    low = means[max(0, int(tail * len(means)) - (1 if tail * len(means) < 1 else 0))]
    high = means[min(len(means) - 1, int((1.0 - tail) * len(means)))]
    return {
        "n": len(shared),
        "mean_a": mean_of([float(a[key]) for key in shared]),
        "mean_b": mean_of([float(b[key]) for key in shared]),
        "difference": round(observed, 4),
        "ci_low": round(low, 4),
        "ci_high": round(high, 4),
        "excludes_zero": not (low <= 0.0 <= high),
        "only_in_a": len(set(a) - set(b)),
        "only_in_b": len(set(b) - set(a)),
    }


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = max(1, int(round(fraction * len(ordered))))
    return float(ordered[min(rank - 1, len(ordered) - 1)])


def score_set(
    items: list[dict[str, Any]],
    verdicts: list[str],
    latencies: list[float],
    extra: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Percentage maths for one probe set. judge_error, no_answer and any
    unrecognised verdict are excluded from the rates and reported separately.

    ``extra`` carries the per-row non-verdict measurements that came out of the same
    pass - ``attribution`` (finding F4) and ``recall`` (the retrieval metric). It is
    optional so a summary written before those keys existed still scores, and a
    caller can re-score an old verdicts file unchanged.
    """
    judge_errors = sum(1 for verdict in verdicts if verdict == JUDGE_ERROR)
    no_answers = sum(1 for verdict in verdicts if verdict == NO_ANSWER)
    unknown_verdicts = sum(
        1 for verdict in verdicts
        if verdict not in EXCLUDED_VERDICTS and verdict not in VERDICT_VALUES
    )
    pairs = [
        (item, verdict) for item, verdict in zip(items, verdicts) if verdict in VERDICT_VALUES
    ]
    rows_extra = list(extra) if extra is not None else [{} for _ in items]
    total = len(pairs)

    counts: dict[str, int] = {value: 0 for value in VERDICT_VALUES}
    for _, verdict in pairs:
        counts[verdict] += 1

    def pct(numerator: int) -> float:
        return round(100.0 * numerator / total, 2) if total else 0.0

    by_type: dict[str, dict[str, Any]] = {}
    for item, verdict in pairs:
        bucket = by_type.setdefault(
            str(item.get("type") or "unknown"),
            {"n": 0, "correct": 0, "partial": 0, "hallucinated": 0},
        )
        bucket["n"] += 1
        if verdict in bucket:
            bucket[verdict] += 1
    for bucket in by_type.values():
        bucket["partial_credit_pct"] = round(
            100.0 * (bucket["correct"] + 0.5 * bucket["partial"]) / bucket["n"], 2
        ) if bucket["n"] else 0.0

    unanswerable = [(item, verdict) for item, verdict in pairs if is_unanswerable(item)]
    answerable = [(item, verdict) for item, verdict in pairs if not is_unanswerable(item)]
    refusal_accuracy = round(
        100.0 * sum(1 for _, verdict in unanswerable if verdict == "refused_ok") / len(unanswerable), 2
    ) if unanswerable else None

    def rate(numerator: int, denominator: int) -> float:
        return round(100.0 * numerator / denominator, 2) if denominator else 0.0

    # Finding F7: ok% counted a correct refusal as accuracy, so a system that
    # refuses more looked better. Both halves are now reported on their own terms:
    # accuracy over answerable probes, refusal rate over unanswerable ones.
    answerable_correct = sum(1 for _, verdict in answerable if verdict == "correct")
    answerable_partial = sum(1 for _, verdict in answerable if verdict == "partial")

    attribution_counts = {value: 0 for value in ATTRIBUTION_VALUES}
    attribution_errors = 0
    for index, (item, verdict) in enumerate(pairs):
        row = rows_extra[index] if index < len(rows_extra) else {}
        value = row.get("attribution")
        if value in attribution_counts:
            attribution_counts[value] += 1
        elif value == ATTRIBUTION_ERROR:
            attribution_errors += 1
    attribution_n = sum(attribution_counts[value] for value in ("right", "wrong"))

    recall_hits = sum(1 for index in range(len(pairs))
                      if (rows_extra[index] if index < len(rows_extra) else {}).get("recall") == "hit")
    recall_misses = sum(1 for index in range(len(pairs))
                        if (rows_extra[index] if index < len(rows_extra) else {}).get("recall") == "miss")
    recall_n = recall_hits + recall_misses

    # Graded retrieval quality on the SAME recall probe response. Averaged only over
    # the rows that carry gold evidence and got an answer: a gold-less probe is na
    # and a missing stack is na, and neither may become a 0.0 that drags an arm down.
    # rank-based values stay marked unverified-order (see ranking_metrics): nobody
    # has confirmed that LightRAG lists chunks in the order it retrieved them.
    metric_rows = [
        rows_extra[index].get("recall_metrics")
        for index in range(len(pairs))
        if isinstance(rows_extra[index].get("recall_metrics"), dict)
    ] if rows_extra else []
    ranking = {
        "ranking_measured_n": len(metric_rows),
        "evidence_recall": mean_metric([row.get("evidence_recall") for row in metric_rows]),
        "graded_recall": mean_metric([row.get("graded_recall") for row in metric_rows]),
        "mrr": mean_metric([row.get("mrr") for row in metric_rows]),
        "average_precision": mean_metric([row.get("average_precision") for row in metric_rows]),
        "hit@1": mean_metric([row.get("hit@1") for row in metric_rows]),
        "hit@5": mean_metric([row.get("hit@5") for row in metric_rows]),
        "ndcg@5": mean_metric([row.get("ndcg@k") for row in metric_rows]),
        # Reported side by side so this instrument and the Thinkpad helpers, which
        # require EVERY gold quote, can be reconciled instead of argued about. None
        # when nothing was measured: 0.0 would read as a measured failure.
        "any_hit_pct": (
            rate(sum(1 for row in metric_rows if row.get("any_hit")), len(metric_rows))
            if metric_rows else None),
        "all_hit_pct": (
            rate(sum(1 for row in metric_rows if row.get("all_hit")), len(metric_rows))
            if metric_rows else None),
        "rank_order": RANK_ORDER_MARKER if metric_rows else None,
    }

    # Second judge (finding F6): the same model family grades its own answers.
    # Agreement is measured only where both judges graded the same row.
    second_judge_n = 0
    second_judge_agree = 0
    for index, (item, verdict) in enumerate(pairs):
        row = rows_extra[index] if index < len(rows_extra) else {}
        other = row.get("second_verdict")
        if other in VERDICT_VALUES:
            second_judge_n += 1
            if other == verdict:
                second_judge_agree += 1

    return {
        "n": len(verdicts),
        "judged": total,
        "judge_errors": judge_errors,
        "no_answers": no_answers,
        "unknown_verdicts": unknown_verdicts,
        "counts": counts,
        "correct_pct": pct(counts["correct"]),
        # ok% also counts a correct refusal, so refusing an unanswerable probe
        # correctly is no longer scored below answering it wrongly. Kept for
        # comparability with older run summaries; accuracy_pct is the honest one.
        "ok_pct": pct(counts["correct"] + counts["refused_ok"]),
        "answerable_n": len(answerable),
        "answerable_correct_pct": rate(answerable_correct, len(answerable)),
        "answerable_partial_credit_pct": (
            round(100.0 * (answerable_correct + 0.5 * answerable_partial) / len(answerable), 2)
            if answerable else 0.0
        ),
        "partial_credit_pct": round(100.0 * (counts["correct"] + 0.5 * counts["partial"]) / total, 2) if total else 0.0,
        "ok_partial_credit_pct": round(
            100.0 * (counts["correct"] + counts["refused_ok"] + 0.5 * counts["partial"]) / total, 2
        ) if total else 0.0,
        "partial_credit_raw": round(counts["correct"] + 0.5 * counts["partial"], 2),
        "hallucination_pct": pct(counts["hallucinated"]),
        "refused_wrong": counts["refused_wrong"],
        "refusal_accuracy_pct": refusal_accuracy,
        "unanswerable_n": len(unanswerable),
        # Same number as refusal_accuracy_pct, under a name that says what it is:
        # how often an unanswerable probe was refused. Kept separate so a table
        # reader never mistakes it for accuracy on answerable questions (F7).
        "unanswerable_refused_pct": refusal_accuracy,
        "attribution_n": attribution_n,
        "attribution_right": attribution_counts["right"],
        "attribution_wrong": attribution_counts["wrong"],
        "attribution_na": attribution_counts["na"],
        "attribution_errors": attribution_errors,
        "attribution_right_pct": rate(attribution_counts["right"], attribution_n),
        "attribution_wrong_pct": rate(attribution_counts["wrong"], attribution_n),
        "evidence_n": recall_n,
        "evidence_in_context_pct": rate(recall_hits, recall_n),
        **ranking,
        "second_judge_n": second_judge_n,
        "second_judge_agreement_pct": rate(second_judge_agree, second_judge_n),
        "by_type": by_type,
        "latency_p50": round(percentile(latencies, 0.50), 2),
        "latency_p95": round(percentile(latencies, 0.95), 2),
    }


RESULTS_TABLE_HEADER = (
    "| experiment | status | dev correct% | dev ok% | dev correct+0.5*partial% | heldout correct% "
    "| heldout ok% | heldout answerable% | heldout correct+0.5*partial% | heldout halluc% "
    "| refusal acc% | evidence in context% | attrib right% | attrib wrong% | p50 s | p95 s "
    "| judge errors |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
)


def results_row(
    name: str,
    status: str,
    dev: dict[str, Any],
    heldout: dict[str, Any],
    p50: float,
    p95: float,
) -> str:
    """One markdown row. Shared by results.md and results-rejudged.md so the two
    tables stay comparable.

    The columns separate what used to be mixed together: "heldout answerable%" is
    accuracy over answerable probes only, "refusal acc%" is the refusal rate over
    unanswerable ones (so refusing more cannot raise accuracy - finding F7), and
    "evidence in context%" is retrieval rather than answer quality. A summary
    written before those keys existed renders as n/a rather than 0.
    """
    judge_errors = int(dev.get("judge_errors", 0)) + int(heldout.get("judge_errors", 0))
    refusal = heldout.get("refusal_accuracy_pct")

    def cell(mapping: dict[str, Any], key: str) -> Any:
        """A summary saved before a key existed renders as n/a instead of crashing."""
        value = mapping.get(key)
        return value if isinstance(value, (int, float)) else "n/a"

    return (
        "| {name} | {status} | {dc} | {dok} | {dp} | {hc} | {hok} | {hacc} | {hp} | {hh} "
        "| {ra} | {recall} | {ar} | {aw} | {p50} | {p95} | {je} |\n"
    ).format(
        name=name,
        status=status,
        dc=dev.get("correct_pct", 0.0),
        dok=cell(dev, "ok_pct"),
        dp=dev.get("partial_credit_pct", 0.0),
        hc=heldout.get("correct_pct", 0.0),
        hok=cell(heldout, "ok_pct"),
        hacc=cell(heldout, "answerable_correct_pct"),
        hp=heldout.get("partial_credit_pct", 0.0),
        hh=heldout.get("hallucination_pct", 0.0),
        ra=refusal if refusal is not None else "n/a",
        recall=cell(heldout, "evidence_in_context_pct"),
        ar=cell(heldout, "attribution_right_pct"),
        aw=cell(heldout, "attribution_wrong_pct"),
        p50=p50,
        p95=p95,
        je=judge_errors,
    )


def status_counts_of(payload: Any) -> dict[str, Any]:
    """The counts mapping inside a /documents/status_counts response.

    LightRAG v1.5.7 nests it: {"status_counts": {"pending": 0, "parsing": 0,
    "analyzing": 0, "processing": 0, "preprocessed": 0, "processed": 0,
    "failed": 0, "all": 0}}. Older/alternate shapes put it under "counts" or at
    the top level, so both are still read.
    """
    if not isinstance(payload, dict):
        return {}
    for key in ("status_counts", "counts"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return payload


def status_counts_are_zero(payload: Any) -> bool:
    """True when the store reports nothing left pending or processing.

    The "all" counter wins when present. Otherwise every numeric value must be 0.
    A response with no numbers at all is NOT zero - treating a malformed or error
    payload as "empty store" would let a reingest start on top of documents that
    are still there.
    """
    counts = status_counts_of(payload)
    total = counts.get("all")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        return total == 0
    numeric = [
        value for value in counts.values()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    return bool(numeric) and all(value == 0 for value in numeric)


def emit(print_fn: Callable[[str], None] | None, line: str) -> None:
    """Print if there is a stdout to print to.

    A piped run (``| tee``) dies on Ctrl-C by killing tee, after which every
    stdout write raises BrokenPipeError. That must never abort finalise, which is
    what puts production back the way it was.
    """
    if print_fn is None:
        return
    try:
        print_fn(line)
    except (BrokenPipeError, OSError):
        pass


def meets_success(heldout: dict[str, Any]) -> bool:
    """SUCCESS: heldout ok% >= 90 AND hallucination% < 5 AND refusal accuracy >= 80.

    ok% counts "correct" plus "refused_ok", i.e. how often the system answered
    right, including a correct refusal of an unanswerable probe.
    """
    refusal = heldout.get("refusal_accuracy_pct")
    return (
        heldout.get("ok_pct", 0.0) >= SUCCESS_HELDOUT_OK_PCT
        and heldout.get("hallucination_pct", 100.0) < SUCCESS_HALLUCINATION_PCT
        and refusal is not None
        and refusal >= SUCCESS_REFUSAL_ACCURACY_PCT
    )


_SECRET_ASSIGN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Za-z0-9_]*)\s*=\s*\S+")
_LOG_ERROR_LINE = re.compile(r"ERROR|Traceback|no longer supported", re.IGNORECASE)


def mask_secrets(text: str) -> str:
    """Blank out any KEY=/TOKEN=/SECRET=/PASSWORD= value in container output.

    Container logs can echo the environment; the driver must never write those
    values into loop.log or state.json.
    """
    return _SECRET_ASSIGN.sub(r"\1=<masked>", text)


# ── Side effects ────────────────────────────────────────────────────────────

class Ops:
    """Every side effect the driver performs. Tests replace this with a fake."""

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))

    def read_text(self, path: Path) -> str:
        return Path(path).read_text(encoding="utf-8")

    def write_text_atomic(self, path: Path, text: str, *, mode: int | None = None) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # The temp name must be unique per process AND thread: heartbeat() and
        # state writes happen on probe threads, and a shared "<name>.tmp" lets one
        # thread rename the file out from under another (FileNotFoundError in
        # os.replace, which killed a probe thread).
        temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temp.write_text(text, encoding="utf-8")
        if mode is not None:
            os.chmod(temp, mode)
        os.replace(temp, path)

    def append_text(self, path: Path, text: str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    def read_json(self, path: Path) -> Any:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def write_json_atomic(self, path: Path, payload: Any) -> None:
        self.write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def http_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        payload: Any = None,
        timeout: float = 30.0,
    ) -> tuple[int, Any]:
        """Return (status_code, parsed_body). Raises on transport errors."""
        data = None
        request_headers = dict(headers or {})
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status = response.getcode()
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            status = exc.code
        try:
            body = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            body = {"raw": raw}
        return status, body

    def run(self, argv: list[str], *, cwd: Path | None = None, timeout: float = 180.0) -> tuple[int, str, str]:
        """Run a command and return (returncode, stdout, stderr).

        A missing executable and a timeout come back as non-zero return codes
        (127 and 124) rather than exceptions, so callers that already branch on
        rc=0 treat them as failures instead of aborting the whole run.
        """
        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            return 127, "", f"command not found: {argv[0]}"
        except subprocess.TimeoutExpired:
            return 124, "", f"timed out after {timeout}s: {' '.join(argv)}"
        except OSError as exc:
            return 126, "", f"failed to run {' '.join(argv)}: {exc}"
        return completed.returncode, completed.stdout, completed.stderr


class DryRunOps(Ops):
    """A fake that lets --dry-run execute the whole plan harmlessly.

    No docker, no HTTP, no sleeping; the only files written are under --out.
    """

    def __init__(self) -> None:
        self.clock = 0.0
        self.commands: list[list[str]] = []
        self.requests: list[tuple[str, str]] = []

    def now(self) -> float:
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.clock += max(0.0, seconds)

    def http_json(self, method, url, *, headers=None, payload=None, timeout=30.0):
        self.requests.append((method, url))
        if url.endswith("/health"):
            return 200, {"status": "healthy"}
        if url.endswith("/status_counts"):
            return 200, {"status_counts": {
                "pending": 0, "parsing": 0, "analyzing": 0, "processing": 0,
                "preprocessed": 0, "processed": 0, "failed": 0, "all": 0,
            }}
        if "/documents/text" in url:
            return 200, {"track_id": "dryrun-track"}
        if "/documents/track_status/" in url:
            return 200, {"track_id": "dryrun-track", "documents": [
                {"id": "doc-1", "status": "processed", "file_path": "dryrun.txt", "chunks_count": 1},
            ], "total_count": 1, "status_summary": {"processed": 1}}
        if url.endswith("/query"):
            return 200, {"status": "success", "result": "[dry-run] simulated retrieved context"}
        if url.endswith("/v1/models"):
            return 200, {"data": [{"id": JUDGE_MODEL}]}
        if url.endswith("/api/chat"):
            return 200, {"message": {"content": "dry-run answer"}}
        if url.endswith("/v1/chat/completions"):
            return 200, {"choices": [{"message": {"content": '{"verdict": "correct", "reason": "dry run"}'}}]}
        return 200, {}

    def run(self, argv, *, cwd=None, timeout=180.0):
        self.commands.append(list(argv))
        if argv[:2] == ["docker", "ps"]:
            return 0, "rag-gateway-api\nlightrag-server\n", ""      # no worker
        if argv[:2] == ["docker", "exec"]:
            return 0, "", ""
        return 0, "", ""


# ── Driver ──────────────────────────────────────────────────────────────────

class Loop:
    def __init__(
        self,
        *,
        root: Path,
        out: Path,
        config: dict[str, Any],
        ops: Ops,
        max_hours: float = 11.0,
        dry_run: bool = False,
        resume: bool = False,
        retry_errors: bool = False,
        dev_probe_file: str = DEV_PROBE_FILE,
        heldout_probe_file: str = HELDOUT_PROBE_FILE,
        target: Target | None = None,
        handle_signals: bool = True,
        print_fn: Callable[[str], None] | None = None,
        draws: int = 1,
        second_judge_model: str | None = None,
        second_judge_fraction: float = 0.2,
        retrieval_recall: bool = True,
        recall_mode: str = "mix",
        compare_arms: tuple[str, str] | None = None,
        probes_only: bool = False,
        arm_name: str = "probes-only",
    ) -> None:
        self.root = Path(root)
        self.out = Path(out)
        self.config = config
        self.ops = ops
        self.max_hours = float(max_hours)
        self.dry_run = dry_run
        self.resume = resume
        self.retry_errors = retry_errors
        self.dev_probe_file = dev_probe_file
        self.heldout_probe_file = heldout_probe_file
        # Every URL and container name used below comes from this object; the
        # defaults are the constants this driver hardcoded before the flags.
        self.t = target or Target()
        self.handle_signals = handle_signals
        self.print_fn = print_fn or (lambda line: print(line, flush=True))

        self.real_env_path = Path(self.t.env_file).expanduser() if self.t.env_file else self.root / ".env"
        # --dry-run never rewrites the production .env: it writes a shadow copy.
        self.env_path = (self.out / "dryrun.env") if dry_run else self.real_env_path
        self.env_original_path = self.out / "env.original"
        self.state_path = self.out / "state.json"
        self.heartbeat_path = self.out / "heartbeat"
        self.log_path = self.out / "loop.log"
        self.results_path = self.out / "results.md"

        self.state: dict[str, Any] = {}
        self.original_env_text = ""
        self.changed_keys: set[str] = set()
        self.start_time = 0.0
        self.deadline = 0.0
        self.hard_deadline = 0.0
        self.abort_requested = False
        self.judge_key = os.environ.get("OVERNIGHT_JUDGE_API_KEY", JUDGE_KEY_DEFAULT)
        self.judge_truncated = 0
        self.gateway_touched = False
        self.plan: list[str] = []
        # One draw per probe cannot resolve the differences being chased (finding
        # F25): draws give a per-probe mean, and --compare turns two arms into a
        # paired difference with a bootstrap interval.
        self.draws = max(1, int(draws))
        self.second_judge_model = second_judge_model or None
        self.second_judge_fraction = min(1.0, max(0.0, float(second_judge_fraction)))
        self.retrieval_recall = bool(retrieval_recall)
        self.recall_mode = str(recall_mode or "mix")
        # Per-arm override from the experiments file ("recall_mode": "naive").
        self.recall_mode_override: str | None = None
        self.compare_arms = compare_arms
        # --probes-only: measure one named arm without writing anything to the stack.
        self.probes_only = bool(probes_only)
        self.arm_name = str(arm_name or "probes-only")

    # ── logging, heartbeat, state ──────────────────────────────────────────

    def log(self, message: str) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        line = f"{stamp} {message}"
        try:
            self.ops.append_text(self.log_path, line + "\n")
        except OSError as exc:  # losing the log must not stop the run
            try:
                print(f"could not append to {self.log_path}: {exc}", flush=True)
            except (BrokenPipeError, OSError):
                pass
        emit(self.print_fn, line)

    def heartbeat(self) -> None:
        self.ops.write_text_atomic(self.heartbeat_path, time.strftime("%Y-%m-%dT%H:%M:%S") + "\n")

    def load_state(self) -> None:
        if self.resume and self.state_path.exists():
            try:
                self.state = self.ops.read_json(self.state_path)
            except (OSError, json.JSONDecodeError):
                self.log("state.json unreadable; starting a fresh state")
        if not self.state:
            self.state = {
                "status": "running",
                "dry_run": self.dry_run,
                "experiments": {},
                "changed_keys": [],
                "corpus_built_by": None,
                "results": {},
            }

    def save_state(self) -> None:
        self.state["changed_keys"] = sorted(self.changed_keys)
        self.state["heartbeat"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.ops.write_json_atomic(self.state_path, self.state)

    def _install_signals(self) -> None:
        if not self.handle_signals:
            return
        if threading.current_thread() is not threading.main_thread():
            return

        def handler(signum, _frame):
            self.abort_requested = True
            self.log(f"signal {signum} received; finishing the current step then finalising")

        for name in ("SIGINT", "SIGTERM"):
            number = getattr(signal, name, None)
            if number is not None:
                try:
                    signal.signal(number, handler)
                except (ValueError, OSError):
                    pass  # not the main thread (tests)

    # ── waiting helpers ────────────────────────────────────────────────────

    def _check_abort(self) -> None:
        if self.abort_requested:
            raise FatalRun("abort requested")
        if self.ops.now() > self.hard_deadline:
            raise FatalRun(f"hard abort at max-hours + {HARD_ABORT_GRACE}s")

    def _wait_until(
        self,
        probe: Callable[[], tuple[bool, Any]],
        *,
        timeout: float,
        interval: float,
        what: str,
    ) -> Any:
        deadline = self.ops.now() + timeout
        while True:
            self._check_abort()
            ok, value = probe()
            if ok:
                return value
            self.heartbeat()
            remaining = deadline - self.ops.now()
            if remaining <= 0:
                raise StepError(f"timeout waiting for {what}")
            self.ops.sleep(min(interval, remaining, HEARTBEAT_INTERVAL))

    def _attempt(self, action: Callable[[], Any], *, what: str, attempts: int = 2) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return action()
            except FatalRun:
                raise
            except JudgeTruncated:
                raise  # deterministic: retrying just burns another 300 s
            except Exception as exc:
                last_error = exc
                self.log(f"{what}: attempt {attempt} failed: {exc}")
                self.heartbeat()
        raise StepError(f"{what}: {last_error}")

    # ── steps 1 and 2: env, restart, verify ────────────────────────────────

    def _target_env(self, experiment: dict[str, Any]) -> tuple[dict[str, str], set[str]]:
        """Env for one experiment: original values, then baseline, then the
        experiment's own env. Keys an earlier experiment added but this one does
        not want are removed, so experiments never stack."""
        baseline = self.config.get("baseline_env") or {}
        env = experiment.get("env") or {}
        gateway_env = experiment.get("gateway_env") or {}
        wanted = {**env, **gateway_env}
        set_values: dict[str, str] = {}
        for key in sorted(self.changed_keys):
            original = env_value(self.original_env_text, key)
            if original is not None:
                set_values[key] = original
        remove_keys = {
            key for key in self.changed_keys
            if key not in set_values and key not in baseline and key not in wanted
        }
        set_values.update(baseline)
        set_values.update(wanted)
        return set_values, remove_keys

    def _apply_env(self, experiment: dict[str, Any]) -> dict[str, str]:
        set_values, remove_keys = self._target_env(experiment)
        check_env_keys(
            set_values,
            f"experiment {experiment['name']}",
            allow=ENV_WHITELIST | GATEWAY_ENV_WHITELIST,
        )
        text = self.ops.read_text(self.env_path) if Path(self.env_path).exists() else self.original_env_text
        self.ops.write_text_atomic(self.env_path, rewrite_env(text, set_values, remove_keys))
        self.changed_keys |= set(set_values) | remove_keys
        if experiment.get("gateway_env"):
            self.gateway_touched = True
        self.log(f"{experiment['name']}: env set to " + ", ".join(f"{k}={set_values[k]}" for k in sorted(set_values)))
        return set_values

    def _compose(self, *args: str) -> tuple[int, str, str]:
        """Every docker compose call honours --compose-file and --compose-project.

        A second stack lives in its own compose project; without -p these calls would
        recreate the production services by name.
        """
        argv = ["docker", "compose", "-f", self.t.compose_file or str(self.root / "compose.yml")]
        if self.t.compose_project:
            argv += ["-p", self.t.compose_project]
        argv.extend(args)
        return self.ops.run(argv, cwd=self.root, timeout=300)

    def _restart_service(self, service: str, container: str) -> None:
        code, out, err = self._compose("up", "-d", "--no-deps", service)
        if code != 0:
            raise StepError(f"compose up {service} failed rc={code}: {err.strip() or out.strip()}")
        self.log(f"{container}: recreated")

    def _restart_server(self) -> None:
        # Today the compose service and the container share a name; a second stack in
        # its own project may keep the service name and rename only the container, so
        # the service is its own knob and falls back to the container name.
        service = self.t.lightrag_service or self.t.lightrag_container
        self._restart_service(service, self.t.lightrag_container)

    def _restart_gateway(self) -> None:
        self._restart_service(self.t.gateway_service, self.t.gateway_container)

    def _container_state(self, container: str) -> str:
        """docker ps -a state for one container: running, exited, restarting, ..."""
        code, out, _err = self.ops.run(
            ["docker", "ps", "-a", "--filter", f"name={container}", "--format", "{{.State}}"],
            cwd=self.root,
            timeout=60,
        )
        if code != 0:
            return ""
        for line in out.splitlines():
            if line.strip():
                return line.strip().lower()
        return ""

    def _container_error_lines(self, container: str) -> str:
        """Up to 5 ERROR/Traceback lines from the container log, secrets masked."""
        code, out, err = self.ops.run(["docker", "logs", "--tail", "40", container], cwd=self.root, timeout=60)
        blob = f"{out}\n{err}"
        if code != 0 and not blob.strip():
            return f"docker logs {container} failed rc={code}"
        lines = [mask_secrets(line).strip()[:300] for line in blob.splitlines() if _LOG_ERROR_LINE.search(line)]
        if not lines:
            return f"no ERROR lines in the last 40 log lines of {container}"
        return " | ".join(lines[-5:])

    def _liveness_guard(self, container: str, http_probe: Callable[[], tuple[bool, Any]]) -> Callable[[], tuple[bool, Any]]:
        """Wrap an HTTP readiness probe so a dead container fails immediately.

        Without this, a server that refuses to start (ENTITY_TYPES in v1.5.7) sat
        out the whole health timeout and the next experiment inherited the corpse.
        """
        restarting = {"streak": 0}

        def check() -> tuple[bool, Any]:
            state = self._container_state(container)
            if state in ("exited", "dead"):
                raise StepError(f"{container} is {state}: {self._container_error_lines(container)}")
            if state == "restarting":
                restarting["streak"] += 1
                if restarting["streak"] >= 2:
                    raise StepError(
                        f"{container} kept restarting ({restarting['streak']} polls in a row): "
                        f"{self._container_error_lines(container)}"
                    )
            else:
                restarting["streak"] = 0
            return http_probe()

        return check

    def _health(self) -> tuple[bool, Any]:
        try:
            status, body = self.ops.http_json("GET", f"{self.t.lightrag_url}/health", timeout=15)
        except Exception as exc:
            return False, str(exc)
        return status == 200 and str(body.get("status", "")).lower() == "healthy", body

    def _wait_healthy(self) -> None:
        self._wait_until(
            self._liveness_guard(self.t.lightrag_container, self._health),
            timeout=HEALTH_TIMEOUT,
            interval=10,
            what=f"{self.t.lightrag_container} health",
        )
        self.log(f"{self.t.lightrag_container}: healthy")

    def _gateway_running(self) -> bool:
        code, out, _err = self.ops.run(["docker", "ps", "--format", "{{.Names}}"], cwd=self.root, timeout=60)
        if code != 0:
            return False
        return any(self.t.gateway_container in line for line in out.splitlines())

    def _gateway_up(self) -> tuple[bool, Any]:
        """The gateway is up once it answers on any path, even a 404."""
        if not self._gateway_running():
            return False, f"{self.t.gateway_container} is not in docker ps"
        try:
            status, body = self.ops.http_json("GET", f"{self.t.gateway_url}/", timeout=15)
        except Exception as exc:
            return False, str(exc)
        return True, {"status": status}

    def _wait_gateway_up(self) -> None:
        self._wait_until(
            self._liveness_guard(self.t.gateway_container, self._gateway_up),
            timeout=HEALTH_TIMEOUT,
            interval=5,
            what=f"{self.t.gateway_container} readiness",
        )
        self.log(f"{self.t.gateway_container}: up")

    def _sigma_up(self) -> tuple[bool, Any]:
        try:
            status, body = self.ops.http_json(
                "GET", SIGMA_URL, headers={"Authorization": f"Bearer {self.judge_key}"}, timeout=15
            )
        except Exception as exc:
            return False, str(exc)
        return status == 200, body

    def _ensure_services(self) -> None:
        """Wait out a down LightRAG, gateway or sigma judge for up to 10 minutes."""
        self._wait_until(self._health, timeout=SERVICE_DOWN_WAIT, interval=15, what="LightRAG /health")
        if self.gateway_touched:
            self._wait_until(self._gateway_up, timeout=SERVICE_DOWN_WAIT, interval=15, what=f"{self.t.gateway_container}")
        self._wait_until(self._sigma_up, timeout=SERVICE_DOWN_WAIT, interval=15, what="judge service (sigma)")

    def _container_env(self, container: str) -> dict[str, str]:
        code, out, err = self.ops.run(["docker", "exec", container, "env"], timeout=60)
        if code != 0:
            raise StepError(f"docker exec {container} env failed rc={code}: {err.strip()}")
        values: dict[str, str] = {}
        for line in out.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        return values

    def _verify_env(self, set_values: dict[str, str]) -> None:
        if self.dry_run:
            self.log(f"dry run: skipping 'docker exec env' verification of {len(set_values)} key(s)")
            return
        # Which service reads a key decides which container proves it: the gateway
        # allow-list is disjoint from LightRAG's, and both read the same .env.
        groups = {self.t.lightrag_container: {}, self.t.gateway_container: {}}
        for key, value in set_values.items():
            container = self.t.gateway_container if key in GATEWAY_ENV_WHITELIST else self.t.lightrag_container
            groups[container][key] = value
        checked = 0
        for container, values in groups.items():
            if not values:
                continue
            actual = self._container_env(container)
            mismatched = {key: (value, actual.get(key)) for key, value in values.items() if actual.get(key) != value}
            if mismatched:
                detail = ", ".join(f"{k}: wanted {wanted!s} got {got!s}" for k, (wanted, got) in mismatched.items())
                raise StepError(f"{container} env not applied: {detail}")
            checked += len(values)
            self.log(f"{container}: env verified for {len(values)} key(s)")
        if checked == 0:
            self.log("no env keys to verify")

    # ── step 3: reingest ───────────────────────────────────────────────────

    def _worker_running(self, container: str) -> bool:
        code, out, _err = self.ops.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            cwd=self.root,
            timeout=60,
        )
        if code != 0:
            raise StepError(f"docker ps failed rc={code}")
        return any(container in line for line in out.splitlines())

    def _guard_worker(self) -> None:
        # The guard protects the store this run clears, so it has to watch the worker
        # of THAT stack. "none" skips it - allowed only for a stack with no worker,
        # and refused before the run starts when the target is production.
        if self.t.guard_skipped():
            self.log("worker guard skipped (--worker-container none): no ingest worker on this stack")
            return
        container = self.t.worker_container
        if self._worker_running(container):
            raise FatalRun(
                f"{container} is running; refusing to clear LightRAG under it. Stop the worker "
                "yourself and rerun, or pass --worker-container with this stack's worker "
                "(or none, if this stack has no worker)."
            )
        self.log(f"{container} is stopped; safe to clear LightRAG")

    def _api_key(self) -> str:
        """LightRAG's API key, or "" when the stack runs open access.

        Production runs LightRAG without LIGHTRAG_API_KEY (the /documents/*
        routes are whitelisted), so an absent key is normal, not an error. The
        value is never logged.
        """
        return env_value(self.original_env_text, "LIGHTRAG_API_KEY") or ""

    def _lightrag_headers(self) -> dict[str, str]:
        """Auth header for LightRAG calls: only when a key is configured."""
        key = self._api_key()
        return {"X-API-Key": key} if key else {}

    def _clear_lightrag(self) -> None:
        headers = self._lightrag_headers()
        status, body = self.ops.http_json("DELETE", f"{self.t.lightrag_url}/documents", headers=headers, timeout=120)
        if status >= 400:
            raise StepError(f"DELETE /documents returned {status}")
        self.log("LightRAG clear requested")

        def counts_zero() -> tuple[bool, Any]:
            code, payload = self.ops.http_json("GET", f"{self.t.lightrag_url}/documents/status_counts", headers=headers, timeout=30)
            if code != 200:
                return False, payload
            return status_counts_are_zero(payload), payload

        self._wait_until(counts_zero, timeout=CLEAR_TIMEOUT, interval=15, what="status_counts to reach 0")
        self.log("LightRAG is empty")

    def _corpus_paths(self) -> list[Path]:
        try:
            raw = self.ops.read_json(self.root / CORPUS_FILE)
        except json.JSONDecodeError as exc:
            raise StepError(f"{CORPUS_FILE} is not valid JSON: {exc}")
        except OSError as exc:
            raise StepError(f"cannot read {CORPUS_FILE}: {exc}")
        if not isinstance(raw, list) or not raw:
            raise StepError(f"{CORPUS_FILE} must be a non-empty JSON list of file paths")
        return [Path(str(entry)) for entry in raw]

    def _insert_one(
        self,
        path: Path,
        *,
        transcript_prep: bool = False,
        chunking: dict[str, Any] | None = None,
        structure_chunking: dict[str, Any] | None = None,
        doc_label: str = "doc",
    ) -> float:
        started = self.ops.now()
        text = self.ops.read_text(path)
        if transcript_prep:
            # Experiment-level key "transcript_prep": true. Same function the
            # worker calls; the default cadence is the shipped default, and text
            # that is not transcript-shaped comes back untouched.
            prepared = prepare_transcript(
                text, filename=path.name, header_every_tokens=DEFAULT_HEADER_EVERY_TOKENS
            )
            if prepared.applied:
                self.log(
                    f"transcript_prep {doc_label}: {prepared.headers_inserted} headers, "
                    f"{prepared.turns} turns, {len(prepared.speakers)} speakers, "
                    f"labels parsed {prepared.labels_parsed}/{prepared.lines_nonempty} lines, "
                    f"rejected {prepared.labels_rejected} (unparseable "
                    f"{prepared.labels_unparseable})"
                )
                text = prepared.text
            else:
                self.log(
                    f"transcript_prep {doc_label}: skipped ({prepared.reason}); "
                    f"labels parsed {prepared.labels_parsed}/{prepared.lines_nonempty} lines, "
                    f"rejected {prepared.labels_rejected} (unparseable "
                    f"{prepared.labels_unparseable})"
                )
        if structure_chunking:
            # Experiment-level key "structure_chunking": the SAME function the worker
            # calls behind STRUCTURE_CHUNKING_ENABLED, so an arm measures the shipped
            # path and not a driver's imitation of it.
            plan = structure_chunk_document(
                text,
                filename=path.name,
                max_piece_tokens=structure_chunking["max_piece_tokens"],
                min_piece_tokens=structure_chunking["min_piece_tokens"],
                header_tokens=structure_chunking["header_tokens"],
                headers=structure_chunking["headers"],
                until_merge=structure_chunking["merge_until"],
            )
            # Same rule as the worker: tell LightRAG a size that our largest piece clears,
            # or it is free to re-cut the pieces this plan just chose.
            text, params = render_for_lightrag(
                plan,
                chunk_token_size=max(64, int(plan.stats.get("tokens_max") or 0) + 64))
            chunking = params
            self.log(
                f"structure_chunking {doc_label}: kind={plan.kind_detected} "
                f"pieces={len(plan.pieces)} tokens p50/p95/max="
                f"{plan.stats.get('tokens_p50')}/{plan.stats.get('tokens_p95')}/"
                f"{plan.stats.get('tokens_max')} dropped_lines="
                f"{plan.stats.get('dropped_running_lines', 0)} repeated_words="
                f"{plan.stats.get('repeated_words', 0)} tokenizer={plan.stats.get('tokenizer')}"
                + (f" warnings={','.join(plan.warnings)}" if plan.warnings else "")
            )
        payload: dict[str, Any] = {"text": text, "file_source": str(path)}
        if chunking:
            # Experiment-level key "chunking": a per-request chunker for LightRAG
            # 1.5.7. params is forwarded exactly as the experiments file wrote it;
            # its key names belong to LightRAG and are never invented here. With
            # no key, this body is byte-identical to what was posted before.
            payload["chunking"] = chunking
        status, body = self.ops.http_json(
            "POST",
            f"{self.t.lightrag_url}/documents/text",
            headers=self._lightrag_headers(),
            payload=payload,
            timeout=120,
        )
        if status >= 400:
            raise RuntimeError(f"POST /documents/text returned {status}")
        track_id = body.get("track_id")
        if not track_id:
            raise RuntimeError("POST /documents/text returned no track_id")

        def processed() -> tuple[bool, Any]:
            code, payload_status = self.ops.http_json(
                "GET", f"{self.t.lightrag_url}/documents/track_status/{track_id}",
                headers=self._lightrag_headers(), timeout=30,
            )
            if code != 200:
                return False, payload_status
            documents = payload_status.get("documents") or []
            statuses = [str(doc.get("status", "")).lower() for doc in documents]
            if any(state == "failed" for state in statuses):
                raise RuntimeError(f"track {track_id} reported failed")
            return bool(statuses) and all(state == "processed" for state in statuses), payload_status

        self._wait_until(processed, timeout=DOC_TIMEOUT, interval=30, what=f"{doc_label} to process")
        return round(self.ops.now() - started, 1)

    def _reingest(self, experiment: dict[str, Any]) -> None:
        self._guard_worker()
        self._clear_lightrag()
        seconds_per_doc: list[float] = []
        prep = bool(experiment.get("transcript_prep"))
        chunking = experiment.get("chunking")
        structure = experiment.get("structure_chunking")
        paths = self._corpus_paths()
        total = len(paths)
        for index, path in enumerate(paths, 1):
            # Positions, never file names: the loop log must not carry corpus
            # paths (the operator's transcript names are themselves sensitive).
            doc_label = f"doc {index}/{total}"
            seconds = self._attempt(
                lambda p=path, q=prep, c=chunking, r=structure, d=doc_label:
                self._insert_one(p, transcript_prep=q, chunking=c, structure_chunking=r,
                                 doc_label=d),
                what=f"insert {doc_label}",
                attempts=2,
            )
            seconds_per_doc.append(seconds)
            self.log(f"{experiment['name']}: inserted {doc_label} in {seconds}s")
        self.state["corpus_built_by"] = experiment["name"]
        self.state.setdefault("results", {}).setdefault(experiment["name"], {})["seconds_per_doc"] = seconds_per_doc
        self.save_state()

    # ── step 4: probes ─────────────────────────────────────────────────────

    def _load_probe_items(self, relative: str) -> list[dict[str, Any]]:
        path = self.root / relative
        text = self.ops.read_text(path)
        items: list[dict[str, Any]] = []
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                raise StepError(f"{path}:{lineno}: invalid JSONL: {exc}")
            if not isinstance(parsed, dict) or "question" not in parsed:
                raise StepError(f"{path}:{lineno}: probe line needs at least a question")
            items.append(parsed)
        if not items:
            raise StepError(f"{path}: no probe items")
        return items

    def _ask(self, item: dict[str, Any]) -> tuple[str, float]:
        messages = [
            {"role": str(turn.get("role") or "user"), "content": str(turn.get("content") or "")}
            for turn in (item.get("history") or [])
        ]
        messages.append({"role": "user", "content": str(item.get("question") or "")})
        payload = {"model": "lightrag:latest", "stream": False, "messages": messages}
        started = self.ops.now()
        status, body = self.ops.http_json("POST", f"{self.t.gateway_url}/api/chat", payload=payload, timeout=PROBE_TIMEOUT)
        latency = round(self.ops.now() - started, 2)
        if status >= 400:
            raise RuntimeError(f"/api/chat returned HTTP {status}")
        answer = (body.get("message") or {}).get("content")
        if answer is None:
            raise RuntimeError("/api/chat response has no message.content")
        return str(answer), latency

    def _ask_for_recall(self, item: dict[str, Any]) -> Any:
        """Ask LightRAG for the context it WOULD pass, and return the raw response.

        Sent straight to the LightRAG this run points at, with only_need_context set,
        so the answer step cannot colour it. top_k, chunk_top_k and the token budgets
        are deliberately absent: the container env the arm itself set decides them, so
        the metric measures the stack as configured (see recall_probe_payload).
        """
        question = str(item.get("question") or "")
        mode = str(item.get("_recall_mode") or self.recall_mode)
        status, body = self.ops.http_json(
            "POST", f"{self.t.lightrag_url}/query",
            headers=self._lightrag_headers(),
            payload=recall_probe_payload(question, mode),
            timeout=PROBE_TIMEOUT,
        )
        if status >= 400:
            raise RuntimeError(f"LightRAG /query returned HTTP {status}")
        return body

    def _recall_for(self, item: dict[str, Any]) -> str:
        """hit/miss/na for one probe. A stack that will not answer is na, not miss.

        The success gate still reads only this. ``_recall_measurement`` is what the
        driver actually calls: the same response, graded further.
        """
        return self._recall_measurement(item)[0]

    def _recall_measurement(self, item: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        """(hit/miss/na, graded measurement for the same response) for one probe.

        One /query pays for both: the binary joined check that gates the run, and
        the per-unit rank numbers that say WHERE the evidence sat. The two are not
        identical by construction - the binary check joins the strings, so a quote
        spanning two units is a hit there and a miss per unit - and both numbers are
        reported so the gap is visible. ``None`` metrics means na: no gold quote, or
        no answer from the stack.
        """
        question = str(item.get("question") or "")
        if not gold_evidence_of(item):
            return "na", None
        try:
            body = self._attempt(
                lambda: self._ask_for_recall(item), what=f"recall {item.get('id')}", attempts=2
            )
        except (StepError, RuntimeError, OSError) as exc:
            self.log(f"recall probe {item.get('id')}: unavailable ({exc})")
            return "na", None
        verdict = evidence_in_context(item, body, question=question)
        gold = gold_evidence_with_grades(item)
        metrics = score_probe(
            [quote for quote, _grade in gold],
            context_strings(body, skip=question),
            [grade for _quote, grade in gold],
        )
        return verdict, metrics

    def _run_set(self, items: list[dict[str, Any]], concurrency: int) -> list[dict[str, Any]]:
        """Ask every probe ``draws`` times, tagging each row with its draw.

        One draw per probe is what made the old comparisons unable to resolve a
        10-point difference (finding F25): a repeat is what makes a per-probe mean
        and a paired test possible. Recall is measured next to the answer, in the
        same thread, against the same stack state.
        """
        jobs = [(index, draw) for index in range(len(items)) for draw in range(1, self.draws + 1)]
        results: list[dict[str, Any] | None] = [None] * len(jobs)
        cursor = [0]
        lock = threading.Lock()

        def worker_thread() -> None:
            while True:
                with lock:
                    slot = cursor[0]
                    if slot >= len(jobs):
                        return
                    cursor[0] += 1
                index, draw = jobs[slot]
                item = items[index]
                row = {
                    "id": item.get("id"),
                    "type": item.get("type"),
                    "question": item.get("question"),
                    "item_index": index,
                    "draw": draw,
                    "answer": "",
                    "latency": 0.0,
                    "error": None,
                    "recall": "na",
                    "recall_metrics": None,
                }
                try:
                    answer, latency = self._attempt(
                        lambda: self._ask(item), what=f"probe {item.get('id')} draw {draw}",
                        attempts=2,
                    )
                    row["answer"], row["latency"] = answer, latency
                except StepError as exc:
                    row["error"] = str(exc)
                if self.retrieval_recall and not self.dry_run:
                    row["recall"], row["recall_metrics"] = self._recall_measurement(item)
                results[slot] = row
                self.heartbeat()

        threads = [threading.Thread(target=worker_thread) for _ in range(max(1, int(concurrency)))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return [row for row in results if row is not None]

    def _probe_scores(self, items: list[dict[str, Any]], rows: list[dict[str, Any]],
                      verdicts: list[str]) -> dict[str, float]:
        """Per-probe mean correctness across draws - the unit a paired test uses.

        1.0 correct, 0.5 partial, 0 for anything else. judge_error and no_answer
        draws are skipped rather than scored 0, so a flaky judge cannot manufacture
        a difference between arms.
        """
        totals: dict[str, list[float]] = {}
        for row, verdict in zip(rows, verdicts):
            if verdict not in VERDICT_VALUES:
                continue
            score = {"correct": 1.0, "partial": 0.5}.get(verdict, 0.0)
            totals.setdefault(str(row.get("id")), []).append(score)
        return {key: mean_of(values) for key, values in totals.items()}

    # The two rank measures worth a paired interval: one says whether the evidence
    # was there at all (fraction of gold quotes), the other says how high it sat.
    PAIRED_RANKING_METRICS = ("evidence_recall", "mrr")

    def _probe_ranking_metrics(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
        """Per-probe mean of each rank measure across draws, {measure: {probe_id: value}}.

        Only rows that carry a gold quote AND got a recall answer are included, so a
        probe nobody can answer stays na in a paired comparison rather than becoming
        a zero for the arm that happened to refuse it.
        """
        collected: dict[str, dict[str, list[float]]] = {
            metric: {} for metric in self.PAIRED_RANKING_METRICS
        }
        for row in rows:
            metrics = row.get("recall_metrics")
            if not isinstance(metrics, dict):
                continue
            ident = str(row.get("id"))
            for metric in self.PAIRED_RANKING_METRICS:
                value = metrics.get(metric)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    collected[metric].setdefault(ident, []).append(float(value))
        return {
            metric: {key: mean_of(values) for key, values in mapping.items()}
            for metric, mapping in collected.items()
        }

    # ── step 5: judge ──────────────────────────────────────────────────────

    def _judge_one(self, item: dict[str, Any], answer: str) -> dict[str, str]:
        return judge_answer(self.ops, item, answer, self.judge_key)

    def _sampled_for_second_judge(self, item: dict[str, Any], draw: int) -> bool:
        """Deterministic sample of rows for the cross-check judge (F6).

        Seeded by probe id and draw, so re-running the same run picks the same rows
        and the agreement number means the same thing twice.
        """
        if not self.second_judge_model or self.second_judge_fraction <= 0.0:
            return False
        if self.second_judge_fraction >= 1.0:
            return True
        seed = f"{item.get('id')}|{draw}"
        return random.Random(seed).random() < self.second_judge_fraction

    def _attribution_for(self, item: dict[str, Any], answer: str, verdict: str) -> str:
        """The separate attribution grade (F4). Never changes the content verdict."""
        if verdict in (NO_ANSWER, JUDGE_ERROR) or not attribution_applicable(item):
            return "na"
        try:
            return self._attempt(
                lambda: judge_attribution(self.ops, item, answer, self.judge_key),
                what=f"attribution {item.get('id')}", attempts=2,
            )
        except JudgeTruncated:
            self.judge_truncated += 1
            return ATTRIBUTION_ERROR
        except (StepError, RuntimeError, OSError):
            return ATTRIBUTION_ERROR

    def _second_judge_for(self, item: dict[str, Any], answer: str) -> str:
        try:
            return self._attempt(
                lambda: judge_answer(self.ops, item, answer, self.judge_key,
                                     self.second_judge_model)["verdict"],
                what=f"second judge {item.get('id')}", attempts=2,
            )
        except JudgeTruncated:
            self.judge_truncated += 1
            return JUDGE_ERROR
        except (StepError, RuntimeError, OSError):
            return JUDGE_ERROR

    def _judge_set(
        self, items: list[dict[str, Any]], results: list[dict[str, Any]]
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Grade every row. Returns (verdicts, extras) aligned with ``results``.

        ``extras`` carries the measurements that are not the content verdict:
        the attribution grade (F4), the retrieval check recorded while answering,
        and the second judge's verdict where a cross-check was sampled (F6).
        """
        verdicts: list[str] = []
        extras: list[dict[str, Any]] = []
        for row in results:
            item = items[int(row.get("item_index", 0))]
            extra: dict[str, Any] = {
                "draw": row.get("draw", 1), "recall": row.get("recall", "na"),
                "recall_metrics": row.get("recall_metrics"),
            }
            if row.get("error"):
                verdicts.append(JUDGE_ERROR)
                extra["attribution"] = "na"
                extras.append(extra)
                continue
            try:
                verdict = self._attempt(
                    lambda i=item, r=row: self._judge_one(i, r["answer"]),
                    what=f"judge {item.get('id')}",
                    attempts=2,
                )
                verdicts.append(verdict["verdict"])
            except JudgeTruncated:
                self.judge_truncated += 1
                verdicts.append(JUDGE_ERROR)
            except StepError:
                verdicts.append(JUDGE_ERROR)
            extra["attribution"] = self._attribution_for(
                item, str(row.get("answer") or ""), verdicts[-1]
            )
            if self._sampled_for_second_judge(item, int(row.get("draw", 1))):
                extra["second_verdict"] = self._second_judge_for(
                    item, str(row.get("answer") or "")
                )
            extras.append(extra)
        return verdicts, extras

    def _repeated_items(self, items: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The probe list expanded to one entry per row, so score_set() stays aligned."""
        return [items[int(row.get("item_index", 0))] for row in rows]

    # ── step 6: record ─────────────────────────────────────────────────────

    RESULTS_HEADER = RESULTS_TABLE_HEADER

    def _ensure_results_header(self) -> None:
        if not Path(self.results_path).exists():
            self.ops.write_text_atomic(
                self.results_path,
                "# Overnight loop results\n\n" + self.RESULTS_HEADER,
            )

    def _record(
        self,
        experiment: dict[str, Any],
        set_name: str,
        items: list[dict[str, Any]],
        rows: list[dict[str, Any]],
        verdicts: list[str],
        extras: list[dict[str, Any]] | None = None,
    ) -> None:
        exp_dir = self.out / experiment["name"]
        answers = [
            {"set": set_name,
             **{key: row.get(key) for key in ("id", "type", "question", "answer", "latency",
                                             "error", "draw", "recall", "recall_metrics")}}
            for row in rows
        ]
        judged = []
        for index, (row, verdict) in enumerate(zip(rows, verdicts)):
            extra = extras[index] if index < len(extras) else {}
            entry = {"set": set_name, "id": row.get("id"), "type": row.get("type"),
                     "draw": row.get("draw", 1), "verdict": verdict,
                     "attribution": extra.get("attribution", "na"),
                     "recall": extra.get("recall", "na")}
            if "second_verdict" in extra:
                entry["second_verdict"] = extra["second_verdict"]
            judged.append(entry)
        self.ops.append_text(
            exp_dir / "answers.jsonl", "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in answers)
        )
        self.ops.append_text(
            exp_dir / "verdicts.jsonl", "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in judged)
        )

    def _run_experiment(self, experiment: dict[str, Any]) -> dict[str, Any]:
        name = experiment["name"]
        self.recall_mode_override = experiment.get("recall_mode") or None
        self.log(f"{name}: start (reingest={experiment['reingest']}, concurrency={experiment['probe_concurrency']})")
        set_values = self._apply_env(experiment)
        self.save_state()
        self._restart_server()
        self._wait_healthy()
        if experiment.get("gateway_env"):
            self._restart_gateway()
            self._wait_gateway_up()
        self._verify_env(set_values)
        if experiment["reingest"]:
            self._reingest(experiment)
        return self._measure(experiment, set_values=set_values)

    def _measure(self, experiment: dict[str, Any], *, set_values: dict[str, str] | None = None
                 ) -> dict[str, Any]:
        """Ask the probe sets, judge every answer, measure recall, write the arm's summary.

        The measuring half, shared by a normal experiment (which has already rewritten
        .env, restarted services and maybe re-ingested) and --probes-only (which has done
        none of that). Everything it sends is a read: GET /health, POST /query for the
        recall probe, the gateway query for the answer, and the judge endpoint.
        """
        set_values = dict(set_values or {})
        name = experiment["name"]
        dev_items = self._load_probe_items(self.dev_probe_file)
        heldout_items = self._load_probe_items(self.heldout_probe_file)
        if self.recall_mode_override:
            for item in dev_items + heldout_items:
                item["_recall_mode"] = self.recall_mode_override
        dev_rows = self._run_set(dev_items, experiment["probe_concurrency"])
        self.judge_truncated = 0
        dev_verdicts, dev_extras = self._judge_set(dev_items, dev_rows)
        self._record(experiment, "dev", dev_items, dev_rows, dev_verdicts, dev_extras)
        heldout_rows = self._run_set(heldout_items, experiment["probe_concurrency"])
        heldout_verdicts, heldout_extras = self._judge_set(heldout_items, heldout_rows)
        self._record(experiment, "heldout", heldout_items, heldout_rows, heldout_verdicts,
                     heldout_extras)

        latencies = [row["latency"] for row in dev_rows + heldout_rows if not row.get("error")]
        summary = {
            "dev": score_set(self._repeated_items(dev_items, dev_rows), dev_verdicts,
                             [row["latency"] for row in dev_rows], dev_extras),
            "heldout": score_set(self._repeated_items(heldout_items, heldout_rows), heldout_verdicts,
                                 [row["latency"] for row in heldout_rows], heldout_extras),
            "all_latencies_p50": percentile(latencies, 0.5),
            "all_latencies_p95": percentile(latencies, 0.95),
            "probe_errors": sum(1 for row in dev_rows + heldout_rows if row.get("error")),
            "reingest": experiment["reingest"],
            "env": set_values,
            # What produced these numbers, so a table row can be compared honestly:
            # draws per probe, whether retrieval was measured, and which judge
            # cross-checked what.
            "draws": self.draws,
            "retrieval_recall": self.retrieval_recall,
            "recall_mode": self.recall_mode if self.retrieval_recall else None,
            "second_judge_model": self.second_judge_model,
            "second_judge_fraction": self.second_judge_fraction if self.second_judge_model else None,
            # Per-probe means across draws: the paired unit --compare consumes. Saved
            # per arm so a comparison can be run after the fact, or on a resumed run.
            "probe_scores": {
                "dev": self._probe_scores(dev_items, dev_rows, dev_verdicts),
                "heldout": self._probe_scores(heldout_items, heldout_rows, heldout_verdicts),
            },
            "probe_ranking_metrics": {
                "dev": self._probe_ranking_metrics(dev_rows),
                "heldout": self._probe_ranking_metrics(heldout_rows),
            },
        }
        self.ops.write_json_atomic(self.out / name / "summary.json", summary)
        if self.judge_truncated:
            self.log(
                f"judge truncated (raise JUDGE_MAX_TOKENS): {self.judge_truncated} call(s) in {name}"
            )

        stored = self.state.setdefault("results", {}).setdefault(name, {})
        stored.update({
            "dev_correct_pct": summary["dev"]["correct_pct"],
            "dev_ok_pct": summary["dev"]["ok_pct"],
            "dev_partial_credit_pct": summary["dev"]["partial_credit_pct"],
            "dev_ok_partial_credit_pct": summary["dev"]["ok_partial_credit_pct"],
            "dev_answerable_correct_pct": summary["dev"]["answerable_correct_pct"],
            "heldout_correct_pct": summary["heldout"]["correct_pct"],
            "heldout_ok_pct": summary["heldout"]["ok_pct"],
            "heldout_partial_credit_pct": summary["heldout"]["partial_credit_pct"],
            "heldout_hallucination_pct": summary["heldout"]["hallucination_pct"],
            "refusal_accuracy_pct": summary["heldout"]["refusal_accuracy_pct"],
            "heldout_answerable_correct_pct": summary["heldout"]["answerable_correct_pct"],
            "heldout_evidence_in_context_pct": summary["heldout"]["evidence_in_context_pct"],
            # Graded retrieval, over gold-evidence probes only. null means na (no
            # gold quote or no answer), never a measured zero.
            "heldout_evidence_recall": summary["heldout"]["evidence_recall"],
            "heldout_mrr": summary["heldout"]["mrr"],
            "heldout_ndcg_at_5": summary["heldout"]["ndcg@5"],
            "heldout_attribution_wrong_pct": summary["heldout"]["attribution_wrong_pct"],
            "draws": self.draws,
            "latency_p50": summary["all_latencies_p50"],
            "latency_p95": summary["all_latencies_p95"],
            "judge_errors": summary["dev"]["judge_errors"] + summary["heldout"]["judge_errors"],
            "env": set_values,
        })
        self._ensure_results_header()
        heldout, dev = summary["heldout"], summary["dev"]
        self.ops.append_text(
            self.results_path,
            results_row(name, "done", dev, heldout, summary["all_latencies_p50"], summary["all_latencies_p95"]),
        )
        self.log(
            f"{name}: dev correct {dev['correct_pct']}% (+0.5p {dev['partial_credit_pct']}%, "
            f"ok {dev['ok_pct']}%), heldout correct {heldout['correct_pct']}% "
            f"(ok {heldout['ok_pct']}%), "
            f"halluc {heldout['hallucination_pct']}%, refusal {heldout['refusal_accuracy_pct']}"
        )
        if heldout["ranking_measured_n"]:
            self.log(
                f"{name}: heldout retrieval over {heldout['ranking_measured_n']} measured "
                f"probe rows: evidence_recall {heldout['evidence_recall']}, mrr "
                f"{heldout['mrr']}, ndcg@5 {heldout['ndcg@5']}, hit@1 {heldout['hit@1']}, "
                f"hit@5 {heldout['hit@5']}, any_hit {heldout['any_hit_pct']}%, all_hit "
                f"{heldout['all_hit_pct']}% [{heldout['rank_order']}]"
            )
        self.save_state()
        return summary

    def _run_probe_only_arm(self) -> None:
        """--probes-only: one arm, named by --arm-name, with zero writes to the stack.

        No .env write or backup, no docker call of any kind, no store clear, no ingest, no
        compose command. Read-only by construction, which is what makes it safe to point at
        production: it scores the stack exactly as configured, so production and a second
        stack can be compared on the same probes and the same judge.
        """
        experiment = {
            "name": self.arm_name, "env": {}, "gateway_env": {}, "reingest": False,
            "transcript_prep": False, "chunking": None, "probe_concurrency": 1,
            "recall_mode": "", "probes_only": True, "structure_chunking": None,
        }
        name = self.arm_name
        self.recall_mode_override = None
        record = self.state["experiments"].setdefault(name, {"status": "pending"})
        self.state["current"] = name
        record["status"] = "running"
        self.save_state()
        self.heartbeat()
        self.log(f"{name}: probes-only start against lightrag={self.t.lightrag_url} "
                 f"gateway={self.t.gateway_url} (read-only: no env write, no restart, "
                 "no clear, no ingest)")
        self._wait_until(self._health, timeout=SERVICE_DOWN_WAIT, interval=15,
                         what="LightRAG /health")
        self._wait_until(self._gateway_answers, timeout=SERVICE_DOWN_WAIT, interval=15,
                         what=f"gateway {self.t.gateway_url}")
        self._wait_until(self._sigma_up, timeout=SERVICE_DOWN_WAIT, interval=15,
                         what="judge service (sigma)")
        try:
            summary = self._measure(experiment, set_values={})
            record["status"] = "done"
            record.pop("error", None)
            if meets_success(summary["heldout"]):
                self.state["status"] = "success"
                self.state["stop_reason"] = f"success in {name}"
        except StepError as exc:
            record["status"] = "error"
            record["error"] = str(exc)
            self.log(f"{name}: probes-only run error: {exc}")
        self.save_state()

    def _gateway_answers(self) -> tuple[bool, Any]:
        """Gateway readiness without docker: any HTTP answer means it is up.

        ``_gateway_up`` consults docker ps, which --probes-only must never call.
        """
        try:
            status, body = self.ops.http_json("GET", f"{self.t.gateway_url}/", timeout=15)
        except Exception as exc:
            return False, str(exc)
        return True, {"status": status}

    # ── run control ────────────────────────────────────────────────────────

    def _prepare(self) -> None:
        self.ops.write_text_atomic(self.heartbeat_path, "")  # creates out/
        if self.probes_only:
            # Read-only run: .env is opened only to find the LightRAG API key. No copy,
            # no backup, no rewrite — that is what makes it safe to point at production.
            try:
                self.original_env_text = self.ops.read_text(self.real_env_path)
            except OSError:
                self.original_env_text = ""
            self.log("probes-only: .env read for the API key only; no env.original backup, "
                     "no rewrite, no restart")
            key = self._api_key()
            self.log("LightRAG API key: present" if key else "LightRAG API key: absent (open access)")
            return
        current = self.ops.read_text(self.real_env_path)
        if self.env_original_path.exists():
            # --resume: .env may currently hold experiment values; env.original
            # is the authority for what production looked like before the run.
            self.original_env_text = self.ops.read_text(self.env_original_path)
            self.changed_keys = set(self.state.get("changed_keys") or [])
        else:
            self.original_env_text = current
            self.ops.write_text_atomic(self.env_original_path, current, mode=0o600)
            self.log(f"backed up .env to {self.env_original_path} (chmod 600)")
        if self.dry_run:
            self.ops.write_text_atomic(self.env_path, self.original_env_text)
        key = self._api_key()
        self.log(
            "LightRAG API key: present" if key else "LightRAG API key: absent (open access)"
        )

    def _recover_to_baseline(self, *, why: str) -> None:
        """Put the stack back into its baseline config before the next experiment.

        Without this, an experiment that killed the server left the corpse for its
        successor, which then burned the full health timeout and failed for the
        wrong reason.
        """
        self.log(f"baseline recovery before the next experiment: {why}")
        self._apply_env({"name": "baseline", "env": {}, "gateway_env": {}, "reingest": False})
        try:
            self._restart_server()
            self._wait_healthy()
        except StepError as exc:
            raise UnrecoverableRun(
                f"cannot restore the baseline {self.t.lightrag_container}: {exc}; "
                f"{self._container_error_lines(self.t.lightrag_container)}"
            ) from exc
        if self.gateway_touched:
            try:
                self._restart_gateway()
                self._wait_gateway_up()
            except StepError as exc:
                raise UnrecoverableRun(
                    f"cannot restore the baseline {self.t.gateway_container}: {exc}; "
                    f"{self._container_error_lines(self.t.gateway_container)}"
                ) from exc
        self.log("baseline recovery done; the next experiment starts from the baseline config")

    def _run_experiments(self) -> None:
        if self.probes_only:
            self._run_probe_only_arm()
            return
        consecutive_errors = 0
        needs_recovery = False
        for experiment in self.config["experiments"]:
            name = experiment["name"]
            record = self.state["experiments"].get(name)
            skipped_statuses = ("done",) if self.retry_errors else ("done", "error")
            if self.resume and record and record.get("status") in skipped_statuses:
                self.log(f"{name}: already {record['status']} in state.json; skipping (--resume)")
                continue
            if self.resume and self.retry_errors and record and record.get("status") == "error":
                self.log(f"{name}: previous status was error; running it again (--retry-errors)")
            if needs_recovery:
                self._recover_to_baseline(why="the previous experiment ended in error")
                needs_recovery = False
            if self.ops.now() > self.deadline:
                self.state["stop_reason"] = "time_budget"
                self.log(f"max-hours {self.max_hours} reached; not starting {name}")
                return
            self._check_abort()
            record = self.state["experiments"].setdefault(name, {"status": "pending"})
            self.state["current"] = name
            record["status"] = "running"
            self.save_state()
            self.heartbeat()
            try:
                self._ensure_services()
                summary = self._run_experiment(experiment)
                record["status"] = "done"
                record.pop("error", None)
                consecutive_errors = 0
                if meets_success(summary["heldout"]):
                    self.state["status"] = "success"
                    self.state["stop_reason"] = f"success in {name}"
                    self.log(f"{name}: SUCCESS thresholds met; skipping the remaining experiments")
                    self.save_state()
                    return
            except FatalRun as exc:
                record["status"] = "error"
                record["error"] = str(exc)
                self.state["status"] = "fatal" if isinstance(exc, UnrecoverableRun) else "aborted"
                self.state["stop_reason"] = "fatal" if isinstance(exc, UnrecoverableRun) else str(exc)
                self.log(f"{name}: FATAL, stopping the run: {exc}")
                self.save_state()
                return
            except StepError as exc:
                record["status"] = "error"
                record["error"] = str(exc)
                consecutive_errors += 1
                needs_recovery = True
                self.log(f"{name}: experiment error ({consecutive_errors} consecutive): {exc}")
                if consecutive_errors >= 3:
                    self.state["stop_reason"] = "three_consecutive_errors"
                    self.log("3 consecutive experiment errors; stopping the run")
                    self.save_state()
                    return
            self.save_state()

    def run(self) -> int:
        self._install_signals()
        self.load_state()
        self.start_time = self.ops.now()
        self.deadline = self.start_time + self.max_hours * 3600
        self.hard_deadline = self.deadline + HARD_ABORT_GRACE
        exit_code = 0
        try:
            self._prepare()
            self._run_experiments()
            if self.state.get("status") == "running":
                self.state["status"] = "complete"
        except FatalRun as exc:
            self.log(f"run aborted: {exc}")
            self.state["status"] = "fatal" if isinstance(exc, UnrecoverableRun) else "aborted"
            self.state["stop_reason"] = str(exc)
            exit_code = 1
        except Exception as exc:  # never leave the stack in an experiment config
            self.log(f"unexpected error: {type(exc).__name__}: {exc}")
            self.state["status"] = "fatal"
            self.state["stop_reason"] = f"{type(exc).__name__}: {exc}"
            exit_code = 2
        finally:
            self._finalise()
            self._write_recommendation()
            if self.compare_arms:
                try:
                    self._report_comparison()
                except Exception as exc:  # a statistic must never break the run
                    self.log(f"comparison skipped: {type(exc).__name__}: {exc}")
            self.heartbeat()
        return exit_code

    def _arm_probe_scores(self, name: str) -> dict[str, dict[str, float]] | None:
        """Read one arm's per-probe scores back out of its summary.json."""
        try:
            summary = self.ops.read_json(self.out / name / "summary.json")
        except (OSError, json.JSONDecodeError):
            return None
        scores = summary.get("probe_scores") if isinstance(summary, dict) else None
        if not isinstance(scores, dict):
            return None
        cleaned: dict[str, dict[str, float]] = {}
        for set_name, mapping in scores.items():
            if isinstance(mapping, dict):
                cleaned[str(set_name)] = {str(key): float(value) for key, value in mapping.items()}
        return cleaned

    def _arm_ranking_metrics(self, name: str) -> dict[str, dict[str, dict[str, float]]] | None:
        """Read one arm's per-probe rank measures back out of its summary.json.

        Absent in a summary written before these measures existed: the caller skips
        the ranking rows rather than comparing against nothing.
        """
        try:
            summary = self.ops.read_json(self.out / name / "summary.json")
        except (OSError, json.JSONDecodeError):
            return None
        block = summary.get("probe_ranking_metrics") if isinstance(summary, dict) else None
        if not isinstance(block, dict):
            return None
        cleaned: dict[str, dict[str, dict[str, float]]] = {}
        for set_name, measures in block.items():
            if not isinstance(measures, dict):
                continue
            cleaned[str(set_name)] = {
                str(metric): {str(key): float(value) for key, value in mapping.items()
                              if isinstance(value, (int, float)) and not isinstance(value, bool)}
                for metric, mapping in measures.items() if isinstance(mapping, dict)
            }
        return cleaned

    def _report_comparison(self) -> None:
        """Paired difference between the two named arms, appended to results.md.

        One draw of 40 probes cannot resolve the effects being chased (finding F25),
        so the comparison is paired per probe and carries a bootstrap interval over
        probes. An interval that covers zero is reported as "not resolved", which is
        the honest reading of a small probe set.
        """
        left, right = self.compare_arms
        a_scores = self._arm_probe_scores(left)
        b_scores = self._arm_probe_scores(right)
        if not a_scores or not b_scores:
            missing = left if not a_scores else right
            self.log(f"comparison {left} vs {right}: no probe_scores in {missing}/summary.json")
            return
        lines = [
            "",
            f"## Paired comparison: {left} vs {right}",
            "",
            "| set | probes paired | mean A | mean B | difference | 95% CI | resolved |",
            "|---|---|---|---|---|---|---|",
        ]
        summary: dict[str, Any] = {}
        for set_name in ("dev", "heldout"):
            a = a_scores.get(set_name) or {}
            b = b_scores.get(set_name) or {}
            stats = paired_bootstrap_difference(a, b)
            summary[set_name] = stats
            lines.append(
                f"| {set_name} | {stats['n']} | {stats['mean_a']} | {stats['mean_b']} "
                f"| {stats['difference']} | [{stats['ci_low']}, {stats['ci_high']}] "
                f"| {'yes' if stats['excludes_zero'] else 'not resolved'} |"
            )
            if stats["only_in_a"] or stats["only_in_b"]:
                lines.append(
                    f"| unpaired | {stats['only_in_a']} only in {left}, "
                    f"{stats['only_in_b']} only in {right} | | | | | |"
                )
        lines += [
            "",
            f"Scores are per-probe means across {self.draws} draw(s) "
            "(correct 1.0, partial 0.5, anything else 0), paired by probe id; the "
            "interval is a cluster bootstrap over probes with a fixed seed, so the "
            "same two arms always give the same numbers.",
            "",
        ]
        measures = self._ranking_comparison_rows(left, right, lines)
        lines += [
            "evidence_recall is the fraction of a probe's gold quotes that came back; "
            "mrr is 1 over the position of the first returned string carrying any of "
            "them. Both are paired over the probes that carry gold evidence, so their "
            "n can be smaller than the score table's. mrr's position is UNVERIFIED "
            "order: position in the response walk, not a confirmed retrieval ranking.",
            "",
        ]
        self.ops.append_text(self.results_path, "\n".join(lines) + "\n")
        self.state["comparison"] = {"a": left, "b": right, "draws": self.draws,
                                    **summary, "measures": measures}
        self.save_state()
        for set_name, stats in summary.items():
            self.log(
                f"comparison {left} vs {right} ({set_name}): difference "
                f"{stats['difference']:+.4f} over {stats['n']} probes, "
                f"95% CI [{stats['ci_low']}, {stats['ci_high']}], "
                f"{'resolved' if stats['excludes_zero'] else 'not resolved'}"
            )
        for set_name, by_measure in measures.items():
            for metric, stats in by_measure.items():
                self.log(
                    f"comparison {left} vs {right} ({set_name} {metric}): difference "
                    f"{stats['difference']:+.4f} over {stats['n']} probes, "
                    f"95% CI [{stats['ci_low']}, {stats['ci_high']}], "
                    f"{'resolved' if stats['excludes_zero'] else 'not resolved'}"
                )

    def _ranking_comparison_rows(
        self, left: str, right: str, lines: list[str]
    ) -> dict[str, dict[str, dict[str, Any]]]:
        """Append the paired rank-metric rows to ``lines``; return them for state.json.

        The binary hit could not tell an arm that found the evidence from one that
        found it and buried it, so the same paired bootstrap is run on
        ``evidence_recall`` and ``mrr``. An arm whose summary predates these measures
        contributes nothing, and no row is invented for it.
        """
        a_metrics = self._arm_ranking_metrics(left)
        b_metrics = self._arm_ranking_metrics(right)
        measures: dict[str, dict[str, dict[str, Any]]] = {}
        lines += [
            "| set | measure | probes paired | mean A | mean B | difference | 95% CI | resolved |",
            "|---|---|---|---|---|---|---|---|",
        ]
        if not a_metrics or not b_metrics:
            missing = left if not a_metrics else right
            lines.append(
                f"| n/a | rank metrics | 0 | | | | | skipped: no probe_ranking_metrics "
                f"in {missing}/summary.json |"
            )
            return measures
        for set_name in ("dev", "heldout"):
            by_measure: dict[str, dict[str, Any]] = {}
            for metric in ("evidence_recall", "mrr"):
                a = (a_metrics.get(set_name) or {}).get(metric) or {}
                b = (b_metrics.get(set_name) or {}).get(metric) or {}
                stats = paired_bootstrap_difference(a, b)
                by_measure[metric] = stats
                lines.append(
                    f"| {set_name} | {metric} | {stats['n']} | {stats['mean_a']} "
                    f"| {stats['mean_b']} | {stats['difference']} "
                    f"| [{stats['ci_low']}, {stats['ci_high']}] "
                    f"| {'yes' if stats['excludes_zero'] else 'not resolved'} |"
                )
            measures[set_name] = by_measure
        lines.append("")
        return measures

    # ── finalise ───────────────────────────────────────────────────────────

    def _finalise(self) -> None:
        try:
            if self.changed_keys:
                self.ops.write_text_atomic(self.env_path, self.original_env_text)
                self.log(f"env restored from {self.env_original_path.name} ({len(self.changed_keys)} key(s) reverted)")
                self._restart_server()
                self._wait_healthy()
                if self.gateway_touched:
                    self._restart_gateway()
                    self._wait_gateway_up()
            else:
                self.log("no env keys were changed; nothing to restore")
        except Exception as exc:  # finalise must never raise
            self.log(f"FINALISE FAILED (restore by hand from {self.env_original_path}): {exc}")
        self.save_state()

    def _best_experiment(self) -> tuple[str | None, dict[str, Any]]:
        """Rank experiments by dev answerable-correct% — accuracy on questions that HAVE answers.

        The previous ranking (ok+0.5*partial%) counted a correct refusal as answered, so an
        arm that refused more could win without knowing more (finding F7). That column is
        still printed and still stored, and a state.json written before
        ``dev_answerable_correct_pct`` existed falls back to it, so --resume across the
        change and older runs still rank.
        """
        results = self.state.get("results") or {}

        def score(values: dict[str, Any]) -> float:
            for key in RANKING_KEYS:
                value = values.get(key)
                if isinstance(value, (int, float)):
                    return float(value)
            return 0.0

        ranked = sorted(
            (
                (name, values) for name, values in results.items()
                if any(isinstance(values.get(key), (int, float)) for key in RANKING_KEYS)
            ),
            key=lambda entry: (score(entry[1]), -entry[1].get("latency_p95", 0.0)),
            reverse=True,
        )
        return ranked[0] if ranked else (None, {})

    def _write_recommendation(self) -> None:
        baseline = self.config.get("baseline_env") or {}
        best_name, best = self._best_experiment()

        def score_of(values: dict[str, Any]) -> Any:
            for key in RANKING_KEYS:
                if isinstance(values.get(key), (int, float)):
                    return values[key]
            return None

        self._ensure_results_header()
        lines = [
            "\n## Final state\n",
            f"- run status: **{self.state.get('status')}**"
            + (f" ({self.state.get('stop_reason')})" if self.state.get("stop_reason") else ""),
            f"- current corpus built by: **{self.state.get('corpus_built_by') or 'no reingest ran'}**",
        ]
        if best_name:
            diff = {
                key: value
                for key, value in (best.get("env") or {}).items()
                if str(baseline.get(key)) != str(value)
            }
            lines.append(f"- best experiment by dev answerable-correct%: **{best_name}** "
                         f"({score_of(best)}%, p95 {best.get('latency_p95')} s)")
            # The old headline is kept next to it, because every older run's numbers are
            # written that way (finding F7: it counted a correct refusal as answered, so a
            # system that refuses more looked more accurate).
            old_headline = best.get("dev_ok_partial_credit_pct", best.get("dev_partial_credit_pct"))
            if isinstance(old_headline, (int, float)):
                lines.append(f"- for comparability with earlier runs: dev ok+0.5*partial% was "
                             f"{old_headline}% — that figure counts a correct refusal as "
                             f"answered, which is why the ranking no longer uses it")
            refusals = best.get("refusal_accuracy_pct")
            if isinstance(refusals, (int, float)):
                lines.append(f"- unanswerable probes were refused {refusals}% of the time")
            if diff:
                lines.append("- recommended env diff (NOT applied — apply by hand if you agree):")
                lines.extend(f"  - `{key}={value}`" for key, value in sorted(diff.items()))
            else:
                lines.append("- best experiment used the baseline env; no change recommended")
        else:
            lines.append("- no experiment produced a score; nothing to recommend")
        self.ops.append_text(self.results_path, "\n".join(lines) + "\n")
        emit(self.print_fn, "\n".join(lines))


# ── offline re-judge mode ────────────────────────────────────────────

def read_jsonl_tolerant(ops: Ops, path: Path) -> tuple[list[dict[str, Any]], int]:
    """Read a JSONL file, skipping anything unparsable.

    The loop writes answers.jsonl from several threads while --rejudge may be
    running, so a half-written final line is expected and must never crash it.
    """
    try:
        text = ops.read_text(path)
    except OSError:
        return [], 0
    rows: list[dict[str, Any]] = []
    skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
        else:
            skipped += 1
    return rows, skipped


def load_probe_maps(
    root: Path,
    ops: Ops,
    *,
    dev_probe: str = DEV_PROBE_FILE,
    heldout_probe: str = HELDOUT_PROBE_FILE,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """id -> probe item for the dev file and the heldout file."""
    maps = []
    for relative in (dev_probe, heldout_probe):
        rows, _skipped = read_jsonl_tolerant(ops, Path(root) / relative)
        maps.append({str(row["id"]): row for row in rows if row.get("id") is not None})
    return maps[0], maps[1]


def resolve_set(row: dict[str, Any], dev_map: dict[str, Any], heldout_map: dict[str, Any]) -> str | None:
    """Which probe set an answer row belongs to.

    The driver writes an explicit "set" field; older or hand-edited files may not,
    so fall back to which probe file holds the id, then to the id prefix (probe
    ids are shaped like dev35 / held12).
    """
    explicit = str(row.get("set") or "").strip().lower()
    if explicit in ("dev", "heldout"):
        return explicit
    ident = str(row.get("id") or "")
    in_dev, in_heldout = ident in dev_map, ident in heldout_map
    if in_dev and not in_heldout:
        return "dev"
    if in_heldout and not in_dev:
        return "heldout"
    lowered = ident.lower()
    if lowered.startswith("dev"):
        return "dev"
    if lowered.startswith("held") or lowered.startswith("ho"):
        return "heldout"
    return None


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def rejudge_experiment(
    directory: Path,
    *,
    ops: Ops,
    judge_key: str,
    dev_map: dict[str, Any],
    heldout_map: dict[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Re-grade one experiment directory. Returns (name, counts, summary)."""
    answers, skipped_lines = read_jsonl_tolerant(ops, directory / "answers.jsonl")
    existing_rows, _skipped = read_jsonl_tolerant(ops, directory / "verdicts.jsonl")
    previous = {str(row.get("id") or ""): str(row.get("verdict") or "") for row in existing_rows}

    counts = {"answers": len(answers), "judged": 0, "kept": 0, "no_answer": 0,
              "errors": 0, "unmatched": 0, "skipped_lines": skipped_lines}
    verdict_rows: list[dict[str, Any]] = []
    per_set: dict[str, tuple[list[dict[str, Any]], list[str], list[float]]] = {
        "dev": ([], [], []), "heldout": ([], [], []),
    }
    all_latencies: list[float] = []
    probe_errors = 0

    for row in answers:
        ident = str(row.get("id") or "")
        set_name = resolve_set(row, dev_map, heldout_map)
        item = (dev_map if set_name == "dev" else heldout_map).get(ident) if set_name else None
        if item is None:
            verdict = JUDGE_ERROR
            counts["unmatched"] += 1
        elif row.get("error") or not str(row.get("answer") or "").strip():
            verdict = NO_ANSWER
            counts["no_answer"] += 1
            probe_errors += 1
        elif previous.get(ident) in VERDICT_VALUES:
            verdict = previous[ident]  # a good verdict is kept, never re-paid for
            counts["kept"] += 1
        else:
            try:
                verdict = judge_answer(ops, item, str(row.get("answer") or ""), judge_key)["verdict"]
                counts["judged"] += 1
            except Exception:
                verdict = JUDGE_ERROR
                counts["errors"] += 1

        verdict_rows.append({
            "set": set_name or str(row.get("set") or ""),
            "id": ident,
            "type": row.get("type") if item is None else (row.get("type") or item.get("type")),
            "verdict": verdict,
        })
        if set_name in per_set and item is not None:
            latency = _as_float(row.get("latency"))
            per_set[set_name][0].append(item)
            per_set[set_name][1].append(verdict)
            per_set[set_name][2].append(latency)
            all_latencies.append(latency)

    ops.write_text_atomic(
        directory / "verdicts.jsonl",
        "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in verdict_rows),
    )

    try:
        summary = ops.read_json(directory / "summary.json")
        if not isinstance(summary, dict):
            summary = {}
    except (OSError, json.JSONDecodeError):
        summary = {}

    dev_items, dev_verdicts, dev_latencies = per_set["dev"]
    heldout_items, heldout_verdicts, heldout_latencies = per_set["heldout"]
    summary["dev"] = score_set(dev_items, dev_verdicts, dev_latencies)
    summary["heldout"] = score_set(heldout_items, heldout_verdicts, heldout_latencies)
    summary["all_latencies_p50"] = percentile(all_latencies, 0.5)
    summary["all_latencies_p95"] = percentile(all_latencies, 0.95)
    summary["probe_errors"] = probe_errors
    summary["unmatched"] = counts["unmatched"]
    summary["rejudged_counts"] = counts
    summary["rejudged_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    ops.write_json_atomic(directory / "summary.json", summary)
    return directory.name, counts, summary


def rejudge(
    out_dir: Path,
    root: Path,
    *,
    dev_probe: str = DEV_PROBE_FILE,
    heldout_probe: str = HELDOUT_PROBE_FILE,
    ops: Ops | None = None,
    print_fn: Callable[[str], None] | None = None,
) -> int:
    """Re-grade saved answers offline, calling only the oMLX judge.

    Touches nothing else: no .env, no docker, no LightRAG, no gateway, no sigma,
    and never results.md or state.json. Safe to run repeatedly and while the loop
    is still running.
    """
    ops = ops or Ops()
    out_dir, root = Path(out_dir), Path(root)
    print_fn = print_fn or (lambda line: print(line, flush=True))
    judge_key = os.environ.get("OVERNIGHT_JUDGE_API_KEY", JUDGE_KEY_DEFAULT)

    emit(print_fn, f"probe files: dev={dev_probe} heldout={heldout_probe}")
    dev_map, heldout_map = load_probe_maps(root, ops, dev_probe=dev_probe, heldout_probe=heldout_probe)
    if not dev_map or not heldout_map:
        emit(print_fn,
             f"refusing to re-judge: need both {dev_probe} and {heldout_probe} "
             f"with ids under {root}")
        return 3

    directories = sorted(
        path for path in out_dir.iterdir() if path.is_dir() and (path / "answers.jsonl").exists()
    ) if out_dir.exists() else []
    if not directories:
        emit(print_fn, f"nothing to do: no <name>/answers.jsonl under {out_dir}")
        return 0

    rows: list[str] = []
    notes: list[str] = []
    for directory in directories:
        name, counts, summary = rejudge_experiment(
            directory, ops=ops, judge_key=judge_key, dev_map=dev_map, heldout_map=heldout_map
        )
        rows.append(
            results_row(
                name, "rejudged", summary["dev"], summary["heldout"],
                summary["all_latencies_p50"], summary["all_latencies_p95"],
            )
        )
        # counts only: never questions, answers or judge reasons
        emit(print_fn,
             f"{name}: judged {counts['judged']} kept {counts['kept']} "
             f"no_answer {counts['no_answer']} errors {counts['errors']} "
             f"unmatched {counts['unmatched']} skipped_lines {counts['skipped_lines']}")
        if counts["unmatched"]:
            notes.append(
                f"- {name}: {counts['unmatched']} answer(s) had no matching probe id, "
                "excluded from the percentages"
            )
        if counts["skipped_lines"]:
            notes.append(
                f"- {name}: {counts['skipped_lines']} unparsable line(s) in answers.jsonl "
                "(the loop may still be writing)"
            )

    table = "# Overnight loop results (re-judged)\n\n" + RESULTS_TABLE_HEADER + "".join(rows)
    if notes:
        table += "\nNotes\n\n" + "\n".join(notes) + "\n"
    ops.write_text_atomic(out_dir / "results-rejudged.md", table)
    emit(print_fn, f"wrote {out_dir / 'results-rejudged.md'} ({len(rows)} experiment(s))")
    return 0


def probes_only_refusal(config: dict[str, Any]) -> str | None:
    """Why --probes-only cannot be combined with this experiments file, or None if it can.

    --probes-only promises the stack will not be touched. An experiments file that carries
    env keys or reingest:true is a promise to touch it, so the combination is refused rather
    than silently reduced to the read-only parts — the operator may believe they are
    measuring an arm that was never applied.
    """
    problems: list[str] = []
    if config.get("baseline_env"):
        problems.append("baseline_env is not empty (that would rewrite .env)")
    for experiment in config.get("experiments") or []:
        name = str(experiment.get("name") or "?")
        if experiment.get("reingest"):
            problems.append(f"{name}: reingest:true would clear and refill the store")
        for key in ("env", "gateway_env"):
            if experiment.get(key):
                problems.append(f"{name}: {key} would rewrite .env")
        if experiment.get("chunking"):
            problems.append(f"{name}: chunking only applies to an ingest this run will not do")
    if not problems:
        return None
    return ("--probes-only is read-only, but the experiments file asks for writes: "
            + "; ".join(problems) + ". Point --experiments at a file with no env/reingest, "
            "or drop it entirely")


def structure_chunking_spec(entry: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Validate an experiment's ``structure_chunking`` key, or None when absent.

    Shape: ``{"max_piece_tokens": 7500, "min_piece_tokens": 150, "header_tokens": 25,
    "headers": true, "merge_until": "floor"}`` - every field optional, all types checked here rather than in the
    middle of an ingest. ``max_piece_tokens`` is a ceiling (the embedder window), not a
    target: an arm that wants a chunk-size comparison sets it deliberately low and says so.
    ``budget_tokens`` is accepted as the same ceiling under its old name.
    It cannot be combined with ``chunking``: both choose the cut list, and an arm that
    sends two instructions says nothing about which one the result came from.
    """
    raw = entry.get("structure_chunking")
    if raw is None or raw is False:
        return None
    if raw is True:
        raw = {}
    if not isinstance(raw, dict):
        raise LoopConfigError(f"experiment {name}: structure_chunking must be an object, "
                              "true or false")
    spec: dict[str, Any] = {"max_piece_tokens": DEFAULT_MAX_PIECE_TOKENS,
                            "min_piece_tokens": DEFAULT_MIN_PIECE_TOKENS,
                            "header_tokens": DEFAULT_HEADER_TOKENS, "headers": True,
                            "merge_until": DEFAULT_MERGE_UNTIL}
    for key, floor in (("max_piece_tokens", 32), ("min_piece_tokens", 0),
                       ("header_tokens", 0)):
        if key in raw:
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < floor:
                raise LoopConfigError(f"experiment {name}: structure_chunking.{key} must be "
                                      f"an int >= {floor}")
            spec[key] = int(value)
    if "budget_tokens" in raw:
        if "max_piece_tokens" in raw:
            raise LoopConfigError(f"experiment {name}: structure_chunking.budget_tokens and "
                                  "max_piece_tokens are the same ceiling under two names")
        value = raw["budget_tokens"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 32:
            raise LoopConfigError(f"experiment {name}: structure_chunking.budget_tokens must "
                                  "be an int >= 32")
        spec["max_piece_tokens"] = int(value)
    if "merge_until" in raw:
        merge_until = str(raw["merge_until"] or "")
        if merge_until not in ("floor", "window"):
            raise LoopConfigError(f"experiment {name}: structure_chunking.merge_until must "
                                  "be \"floor\" or \"window\"")
        spec["merge_until"] = merge_until
    if "headers" in raw:
        headers = raw["headers"]
        if not isinstance(headers, bool):
            raise LoopConfigError(f"experiment {name}: structure_chunking.headers must be "
                                  "true or false")
        spec["headers"] = bool(headers)
    unknown = sorted(set(raw) - set(spec) - {"budget_tokens"})
    if unknown:
        raise LoopConfigError(f"experiment {name}: structure_chunking has unknown keys: "
                              + ", ".join(unknown))
    if entry.get("chunking"):
        raise LoopConfigError(f"experiment {name}: structure_chunking and chunking cannot be "
                              "combined - both choose where the cuts fall")
    return spec


def build_plan_lines(config: dict[str, Any]) -> list[str]:
    """Human-readable plan, used by --dry-run and at the start of a real run."""
    lines = ["plan:"]
    for index, experiment in enumerate(config["experiments"], 1):
        env = ", ".join(f"{k}={v}" for k, v in sorted(experiment["env"].items())) or "(baseline only)"
        gateway_env = experiment.get("gateway_env") or {}
        gateway_part = (
            " gateway_env[" + ", ".join(f"{k}={v}" for k, v in sorted(gateway_env.items())) + "]"
            if gateway_env else ""
        )
        lines.append(
            f"  {index}. {experiment['name']}: env[{env}]{gateway_part} "
            f"reingest={experiment['reingest']} probe_concurrency={experiment['probe_concurrency']}"
            + (f" chunking={experiment['chunking']['strategy']}" if experiment.get("chunking") else "")
            + (" transcript_prep" if experiment.get("transcript_prep") else "")
            + (" structure_chunking" if experiment.get("structure_chunking") else "")
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Unattended overnight accuracy-loop driver")
    parser.add_argument("--root", required=True, help="project root on the Thinkpad (~/rag-project)")
    parser.add_argument("--experiments", help="path to experiments.json (not needed with --rejudge)")
    parser.add_argument("--out", help="output/state directory (not needed with --rejudge)")
    parser.add_argument("--max-hours", type=float, default=11.0, help="stop starting experiments after this many hours")
    parser.add_argument("--dry-run", action="store_true", help="run the plan against a fake Ops; touches nothing real")
    parser.add_argument("--resume", action="store_true", help="skip experiments already done/error in state.json")
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="with --resume: run experiments whose recorded status is \"error\" again from step 1 "
             "(\"done\" experiments stay skipped)",
    )
    parser.add_argument(
        "--rejudge",
        metavar="OUT_DIR",
        help="offline mode: re-grade saved answers in OUT_DIR/<name>/answers.jsonl using only "
             "the judge endpoint, then write OUT_DIR/results-rejudged.md",
    )
    parser.add_argument(
        "--dev-probes",
        metavar="PATH",
        default=DEV_PROBE_FILE,
        help=f"dev probe JSONL: absolute, or relative to <root>/probe (default: {DEV_PROBE_FILE})",
    )
    parser.add_argument(
        "--heldout-probes",
        metavar="PATH",
        default=HELDOUT_PROBE_FILE,
        help=f"held-out probe JSONL, resolved the same way (default: {HELDOUT_PROBE_FILE})",
    )
    # Target selection. Unset means the module constant, i.e. the same stack this
    # driver pointed at before these flags existed.
    parser.add_argument("--lightrag-url", default=None,
                       help=f"LightRAG base URL (default: the production {LIGHTRAG_URL})")
    parser.add_argument("--gateway-url", default=None,
                       help=f"gateway base URL (default: the production {GATEWAY_URL})")
    parser.add_argument("--lightrag-container", default=None,
                       help=f"LightRAG container name for docker ps/logs/exec (default: {CONTAINER})")
    parser.add_argument("--gateway-container", default=None,
                       help=f"gateway container name (default: {GATEWAY_CONTAINER})")
    parser.add_argument("--gateway-service", default=None,
                       help=f"compose service name used to recreate the gateway (default: {GATEWAY_SERVICE})")
    parser.add_argument("--env-file", default=None,
                       help=".env file to rewrite (default: <root>/.env, as always)")
    parser.add_argument("--lightrag-service", default=None,
                       help="compose service name used to recreate LightRAG (default: the "
                            "--lightrag-container name, as today)")
    parser.add_argument("--worker-container", default=None,
                       help=f"container the worker guard checks (default: {WORKER_CONTAINER}; "
                            f"{WORKER_GUARD_NONE} skips the guard, only for a stack with no worker)")
    parser.add_argument("--compose-file", default=None,
                       help="compose file for every docker compose call (default: <root>/compose.yml)")
    parser.add_argument("--compose-project", default=None,
                       help="compose project name (-p) when the stack lives in its own project")
    parser.add_argument(
        "--allow-production",
        action="store_true",
        help="required when the run points at the production LightRAG AND the experiments file "
             "requests reingest: true, because that deletes every document in that store before "
             "re-inserting it",
    )
    parser.add_argument(
        "--draws", type=int, default=1, metavar="N",
        help="ask every probe N times per experiment (default 1). One draw over ~40 "
             "questions resolves about one standard error, which is not enough to adopt "
             "an arm; draws give a per-probe mean a paired test can use",
    )
    parser.add_argument(
        "--compare", nargs=2, metavar=("ARM_A", "ARM_B"), default=None,
        help="after the run, report the paired per-probe difference ARM_A - ARM_B with a "
             "bootstrap 95%% CI in results.md (names must match experiments)",
    )
    parser.add_argument(
        "--second-judge-model", default=None, metavar="MODEL",
        help="optional cross-check: grade a sample of rows with this model too and report "
             "agreement with the primary judge (off by default)",
    )
    parser.add_argument(
        "--second-judge-fraction", type=float, default=0.2, metavar="FRACTION",
        help="share of graded rows the second judge re-grades (default 0.2)",
    )
    parser.add_argument(
        "--no-retrieval-recall", dest="retrieval_recall", action="store_false",
        help="skip the per-probe check that the gold evidence is in the context LightRAG "
             "returned (it costs one extra LightRAG query per probe per draw)",
    )
    parser.add_argument(
        "--probes-only", action="store_true",
        help="read-only mode: run the dev and held-out probes, the judge and the recall probe "
             "against the current --gateway-url/--lightrag-url as one pseudo-arm named by "
             "--arm-name, and write NOTHING to the stack - no .env write or backup, no "
             "container restart or exec, no store clear, no ingest, no compose command. "
             "Safe against production because it cannot change it; refused if the "
             "experiments file carries env keys or reingest:true",
    )
    parser.add_argument("--arm-name", default="probes-only", metavar="NAME",
                       help="the arm name --probes-only writes into results.md "
                            "(default: probes-only)")
    parser.add_argument(
        "--recall-mode", default="mix",
        help="LightRAG query mode for the retrieval-recall probe (default mix, the mode the "
             "production bridge sends)",
    )
    arguments = parser.parse_args(argv)

    root_path = Path(arguments.root).expanduser()
    dev_probe = resolve_probe_path(arguments.dev_probes)
    heldout_probe = resolve_probe_path(arguments.heldout_probes)
    target = Target(
        lightrag_url=arguments.lightrag_url,
        gateway_url=arguments.gateway_url,
        lightrag_container=arguments.lightrag_container,
        gateway_container=arguments.gateway_container,
        gateway_service=arguments.gateway_service,
        env_file=arguments.env_file,
        lightrag_service=arguments.lightrag_service,
        worker_container=arguments.worker_container,
        compose_file=arguments.compose_file,
        compose_project=arguments.compose_project,
    )

    if arguments.rejudge:
        return rejudge(
            Path(arguments.rejudge).expanduser(), root_path,
            dev_probe=dev_probe, heldout_probe=heldout_probe,
        )

    if arguments.probes_only and not arguments.out:
        parser.error("--probes-only needs --out")
    if not arguments.probes_only and (not arguments.experiments or not arguments.out):
        parser.error("--experiments and --out are required unless --rejudge or "
                     "--probes-only is given")

    config: dict[str, Any] = {"baseline_env": {}, "experiments": []}
    if arguments.experiments:
        try:
            config = load_experiments(Path(arguments.experiments),
                                      allow_empty_experiments=arguments.probes_only)
        except LoopConfigError as exc:
            print(f"refusing to run: {exc}", file=sys.stderr)
            return 3
    if arguments.probes_only:
        refusal = probes_only_refusal(config)
        if refusal:
            print(f"refusing to run: {refusal}", file=sys.stderr)
            return 3

    # Names are checked before docker or HTTP is touched, so a typo costs nothing.
    for label, spec in (("--dev-probes", dev_probe), ("--heldout-probes", heldout_probe)):
        if not (root_path / spec).is_file():
            print(f"refusing to run: {label} file not found: {root_path / spec}", file=sys.stderr)
            return 3

    # The store this run clears is the store it points at. A run that lands on the
    # production LightRAG and requests reingest wipes production, so it now needs
    # an explicit --allow-production. Checked before any request or docker call.
    if arguments.probes_only:
        # Nothing to guard: this run cannot reingest, cannot restart and cannot rewrite
        # .env, so the production-wipe and worker guards do not apply. It is still read
        # from the production stack, so the target is printed rather than assumed.
        print(f"probes-only: read-only against lightrag={target.lightrag_url} "
              f"gateway={target.gateway_url}; arm name={arguments.arm_name!r}; no .env write, "
              "no docker, no clear, no ingest", file=sys.stderr)
    else:
        guard_refusal = worker_guard_refusal(target)
        if guard_refusal:
            print(f"refusing to run: {guard_refusal}", file=sys.stderr)
            return 3
        refusal = production_wipe_refusal(target, config)
    if not arguments.probes_only and refusal:
        if arguments.allow_production:
            print(f"WARNING: production reingest approved by --allow-production ({refusal})", file=sys.stderr)
        elif arguments.dry_run:
            print(f"WARNING (dry run only, no request is made): {refusal}", file=sys.stderr)
        else:
            print(f"refusing to run: {refusal}", file=sys.stderr)
            return 3

    ops: Ops = DryRunOps() if arguments.dry_run else Ops()
    loop = Loop(
        root=Path(arguments.root).expanduser(),
        out=Path(arguments.out).expanduser(),
        config=config,
        ops=ops,
        max_hours=arguments.max_hours,
        dry_run=arguments.dry_run,
        resume=arguments.resume,
        retry_errors=arguments.retry_errors,
        dev_probe_file=dev_probe,
        heldout_probe_file=heldout_probe,
        target=target,
        draws=arguments.draws,
        second_judge_model=arguments.second_judge_model,
        second_judge_fraction=arguments.second_judge_fraction,
        retrieval_recall=arguments.retrieval_recall,
        recall_mode=arguments.recall_mode,
        compare_arms=tuple(arguments.compare) if arguments.compare else None,
        probes_only=arguments.probes_only,
        arm_name=arguments.arm_name,
    )
    # file names only, never their contents
    loop.log(f"probe files: dev={dev_probe} heldout={heldout_probe}")
    compose_shown = target.compose_file or str(root_path / "compose.yml")
    if target.compose_project:
        compose_shown += f" -p {target.compose_project}"
    loop.log(f"target: lightrag={target.lightrag_url} gateway={target.gateway_url} "
             f"containers={target.lightrag_container},{target.gateway_container} "
             f"worker={target.worker_container} compose={compose_shown} "
             f"env={target.env_file or (root_path / '.env')}")
    loop.log(
        f"measurement: draws={loop.draws} retrieval_recall={loop.retrieval_recall} "
        f"(mode={loop.recall_mode}) second_judge="
        f"{loop.second_judge_model or 'off'}"
        + (f"({loop.second_judge_fraction:g})" if loop.second_judge_model else "")
        + (f" compare={loop.compare_arms[0]} vs {loop.compare_arms[1]}" if loop.compare_arms else "")
    )
    for line in build_plan_lines(config):
        loop.log(line)
    if arguments.dry_run:
        loop.log(f"dry run: max_hours={arguments.max_hours}")
    return loop.run()


if __name__ == "__main__":
    exit_code = main()
    # A closed downstream pipe (| head, a killed tee) otherwise makes CPython exit
    # 120 while flushing stdout at shutdown, which looks like a failed run.
    try:
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
    sys.exit(exit_code)
