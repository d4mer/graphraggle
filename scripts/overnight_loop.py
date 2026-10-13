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
        --max-hours 11 [--dry-run] [--resume] [--retry-errors]

    python3 scripts/overnight_loop.py --root ~/rag-project \
        --rejudge ~/rag-project/probe/loop

--rejudge is the offline mode: it re-grades answers already saved in
<out>/<name>/answers.jsonl using nothing but the judge endpoint, rewrites those
directories' verdicts and summaries, and writes <out>/results-rejudged.md. It
never touches .env, docker, LightRAG, the gateway, sigma, results.md or
state.json.

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

An experiment may also carry ``"gateway_env": {...}`` with the gateway knobs
(BRIDGE_*, GATEWAY_CHUNK_TOP_K, CITATION_TOP_K). They live in the same .env, are
verified against the rag-gateway-api container, and that service is recreated and
waited for too. ENTITY_TYPES is refused: LightRAG v1.5.7 will not start with it.

Outputs, all under --out:
    state.json     resumable run state, written atomically after every step
    heartbeat      ISO timestamp, rewritten at least every 60 s while waiting
    loop.log       human-readable log (appended)
    results.md     one row per experiment, dev + heldout columns
    env.original   the untouched copy of <root>/.env (chmod 600)
    <name>/answers.jsonl, verdicts.jsonl, summary.json

Stdlib only (urllib, json, subprocess, argparse, time, pathlib, re). Every side
effect goes through the Ops class so the tests can replace it with a fake: no
network, no docker, no sleeping in tests.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
3. Speaker attribution: do not mark an answer wrong merely because it attributes a
   statement to a different speaker label, or omits who said it, unless the
   question explicitly asks who said it.
4. Judge only against the evidence quote and the expected answer given, never
   against outside knowledge. Extra detail that is not in the evidence is not an
   error unless it contradicts the evidence.
5. The "reason" must be ONE sentence and must name which rule decided it, for
   example "rule1 clean refusal" or "rule2 all key facts present".

Verdicts: correct, partial, wrong, hallucinated, refused_ok, refused_wrong.

Reply with ONLY this JSON object and nothing else:
{{"verdict": "correct|partial|wrong|hallucinated|refused_ok|refused_wrong", "reason": "<one sentence naming the deciding rule>"}}
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


def load_experiments(path: Path) -> dict[str, Any]:
    """Parse and validate experiments.json, preserving file order."""
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
    if not isinstance(entries, list) or not entries:
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
        experiments.append({
            "name": name,
            "env": {str(k): str(v) for k, v in env.items()},
            "gateway_env": {str(k): str(v) for k, v in gateway_env.items()},
            "reingest": bool(entry.get("reingest", False)),
            "probe_concurrency": concurrency,
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


def judge_reply(ops: Ops, prompt: str, judge_key: str) -> tuple[str, str, str]:
    """One judge call. Returns (message.content, message.reasoning_content,
    choices[0].finish_reason). Transport and HTTP problems raise."""
    payload = {
        "model": JUDGE_MODEL,
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


def judge_answer(ops: Ops, item: dict[str, Any], answer: str, judge_key: str) -> dict[str, str]:
    """Grade one answer against the item's evidence.

    The verdict normally sits in message.content. If it is missing and the call
    was not cut short, look for the last JSON object in reasoning_content, which
    is where a reasoning model puts it. Raises JudgeTruncated when the token
    budget was the problem, RuntimeError when the reply is simply unusable.
    """
    content, reasoning, finish_reason = judge_reply(ops, judge_prompt_for(item, answer), judge_key)
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

UNANSWERABLE = re.compile(
    r"(not in the documents|not available|not mentioned|unanswerable|no answer)",
    re.IGNORECASE,
)


def is_unanswerable(item: dict[str, Any]) -> bool:
    expected = str(item.get("expected_answer") or "").strip()
    return expected == "" or bool(UNANSWERABLE.search(expected))


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = max(1, int(round(fraction * len(ordered))))
    return float(ordered[min(rank - 1, len(ordered) - 1)])


def score_set(items: list[dict[str, Any]], verdicts: list[str], latencies: list[float]) -> dict[str, Any]:
    """Percentage maths for one probe set. judge_error, no_answer and any
    unrecognised verdict are excluded from the rates and reported separately."""
    judge_errors = sum(1 for verdict in verdicts if verdict == JUDGE_ERROR)
    no_answers = sum(1 for verdict in verdicts if verdict == NO_ANSWER)
    unknown_verdicts = sum(
        1 for verdict in verdicts
        if verdict not in EXCLUDED_VERDICTS and verdict not in VERDICT_VALUES
    )
    pairs = [
        (item, verdict) for item, verdict in zip(items, verdicts) if verdict in VERDICT_VALUES
    ]
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
    refusal_accuracy = round(
        100.0 * sum(1 for _, verdict in unanswerable if verdict == "refused_ok") / len(unanswerable), 2
    ) if unanswerable else None

    return {
        "n": len(verdicts),
        "judged": total,
        "judge_errors": judge_errors,
        "no_answers": no_answers,
        "unknown_verdicts": unknown_verdicts,
        "counts": counts,
        "correct_pct": pct(counts["correct"]),
        # ok% also counts a correct refusal, so refusing an unanswerable probe
        # correctly is no longer scored below answering it wrongly.
        "ok_pct": pct(counts["correct"] + counts["refused_ok"]),
        "partial_credit_pct": round(100.0 * (counts["correct"] + 0.5 * counts["partial"]) / total, 2) if total else 0.0,
        "ok_partial_credit_pct": round(
            100.0 * (counts["correct"] + counts["refused_ok"] + 0.5 * counts["partial"]) / total, 2
        ) if total else 0.0,
        "partial_credit_raw": round(counts["correct"] + 0.5 * counts["partial"], 2),
        "hallucination_pct": pct(counts["hallucinated"]),
        "refused_wrong": counts["refused_wrong"],
        "refusal_accuracy_pct": refusal_accuracy,
        "unanswerable_n": len(unanswerable),
        "by_type": by_type,
        "latency_p50": round(percentile(latencies, 0.50), 2),
        "latency_p95": round(percentile(latencies, 0.95), 2),
    }


RESULTS_TABLE_HEADER = (
    "| experiment | status | dev correct% | dev ok% | dev correct+0.5*partial% | heldout correct% "
    "| heldout ok% | heldout correct+0.5*partial% | heldout halluc% | refusal acc% | p50 s | p95 s "
    "| judge errors |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
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
    tables stay comparable."""
    judge_errors = int(dev.get("judge_errors", 0)) + int(heldout.get("judge_errors", 0))
    refusal = heldout.get("refusal_accuracy_pct")

    def cell(mapping: dict[str, Any], key: str) -> Any:
        """A summary saved before a key existed renders as n/a instead of crashing."""
        value = mapping.get(key)
        return value if isinstance(value, (int, float)) else "n/a"

    return "| {name} | {status} | {dc} | {dok} | {dp} | {hc} | {hok} | {hp} | {hh} | {ra} | {p50} | {p95} | {je} |\n".format(
        name=name,
        status=status,
        dc=dev.get("correct_pct", 0.0),
        dok=cell(dev, "ok_pct"),
        dp=dev.get("partial_credit_pct", 0.0),
        hc=heldout.get("correct_pct", 0.0),
        hok=cell(heldout, "ok_pct"),
        hp=heldout.get("partial_credit_pct", 0.0),
        hh=heldout.get("hallucination_pct", 0.0),
        ra=refusal if refusal is not None else "n/a",
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
        handle_signals: bool = True,
        print_fn: Callable[[str], None] | None = None,
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
        self.handle_signals = handle_signals
        self.print_fn = print_fn or (lambda line: print(line, flush=True))

        self.real_env_path = self.root / ".env"
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
        return self.ops.run(
            ["docker", "compose", "-f", str(self.root / "compose.yml"), *args],
            cwd=self.root,
            timeout=300,
        )

    def _restart_service(self, service: str, container: str) -> None:
        code, out, err = self._compose("up", "-d", "--no-deps", service)
        if code != 0:
            raise StepError(f"compose up {service} failed rc={code}: {err.strip() or out.strip()}")
        self.log(f"{container}: recreated")

    def _restart_server(self) -> None:
        self._restart_service(CONTAINER, CONTAINER)

    def _restart_gateway(self) -> None:
        self._restart_service(GATEWAY_SERVICE, GATEWAY_CONTAINER)

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
            status, body = self.ops.http_json("GET", f"{LIGHTRAG_URL}/health", timeout=15)
        except Exception as exc:
            return False, str(exc)
        return status == 200 and str(body.get("status", "")).lower() == "healthy", body

    def _wait_healthy(self) -> None:
        self._wait_until(
            self._liveness_guard(CONTAINER, self._health),
            timeout=HEALTH_TIMEOUT,
            interval=10,
            what=f"{CONTAINER} health",
        )
        self.log(f"{CONTAINER}: healthy")

    def _gateway_running(self) -> bool:
        code, out, _err = self.ops.run(["docker", "ps", "--format", "{{.Names}}"], cwd=self.root, timeout=60)
        if code != 0:
            return False
        return any(GATEWAY_CONTAINER in line for line in out.splitlines())

    def _gateway_up(self) -> tuple[bool, Any]:
        """The gateway is up once it answers on any path, even a 404."""
        if not self._gateway_running():
            return False, f"{GATEWAY_CONTAINER} is not in docker ps"
        try:
            status, body = self.ops.http_json("GET", f"{GATEWAY_URL}/", timeout=15)
        except Exception as exc:
            return False, str(exc)
        return True, {"status": status}

    def _wait_gateway_up(self) -> None:
        self._wait_until(
            self._liveness_guard(GATEWAY_CONTAINER, self._gateway_up),
            timeout=HEALTH_TIMEOUT,
            interval=5,
            what=f"{GATEWAY_CONTAINER} readiness",
        )
        self.log(f"{GATEWAY_CONTAINER}: up")

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
            self._wait_until(self._gateway_up, timeout=SERVICE_DOWN_WAIT, interval=15, what=f"{GATEWAY_CONTAINER}")
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
        groups = {CONTAINER: {}, GATEWAY_CONTAINER: {}}
        for key, value in set_values.items():
            container = GATEWAY_CONTAINER if key in GATEWAY_ENV_WHITELIST else CONTAINER
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

    def _worker_running(self) -> bool:
        code, out, _err = self.ops.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            cwd=self.root,
            timeout=60,
        )
        if code != 0:
            raise StepError(f"docker ps failed rc={code}")
        return any(WORKER_CONTAINER in line for line in out.splitlines())

    def _guard_worker(self) -> None:
        if self._worker_running():
            raise FatalRun(
                f"{WORKER_CONTAINER} is running; refusing to clear LightRAG under it. "
                "Stop the worker yourself and rerun with --resume."
            )
        self.log("worker is stopped; safe to clear LightRAG")

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
        status, body = self.ops.http_json("DELETE", f"{LIGHTRAG_URL}/documents", headers=headers, timeout=120)
        if status >= 400:
            raise StepError(f"DELETE /documents returned {status}")
        self.log("LightRAG clear requested")

        def counts_zero() -> tuple[bool, Any]:
            code, payload = self.ops.http_json("GET", f"{LIGHTRAG_URL}/documents/status_counts", headers=headers, timeout=30)
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

    def _insert_one(self, path: Path) -> float:
        started = self.ops.now()
        text = self.ops.read_text(path)
        status, body = self.ops.http_json(
            "POST",
            f"{LIGHTRAG_URL}/documents/text",
            headers=self._lightrag_headers(),
            payload={"text": text, "file_source": str(path)},
            timeout=120,
        )
        if status >= 400:
            raise RuntimeError(f"POST /documents/text returned {status}")
        track_id = body.get("track_id")
        if not track_id:
            raise RuntimeError("POST /documents/text returned no track_id")

        def processed() -> tuple[bool, Any]:
            code, payload = self.ops.http_json(
                "GET", f"{LIGHTRAG_URL}/documents/track_status/{track_id}",
                headers=self._lightrag_headers(), timeout=30,
            )
            if code != 200:
                return False, payload
            documents = payload.get("documents") or []
            statuses = [str(doc.get("status", "")).lower() for doc in documents]
            if any(state == "failed" for state in statuses):
                raise RuntimeError(f"track {track_id} reported failed")
            return bool(statuses) and all(state == "processed" for state in statuses), payload

        self._wait_until(processed, timeout=DOC_TIMEOUT, interval=30, what=f"{path.name} to process")
        return round(self.ops.now() - started, 1)

    def _reingest(self, experiment: dict[str, Any]) -> None:
        self._guard_worker()
        self._clear_lightrag()
        seconds_per_doc: list[float] = []
        for path in self._corpus_paths():
            seconds = self._attempt(lambda p=path: self._insert_one(p), what=f"insert {path.name}", attempts=2)
            seconds_per_doc.append(seconds)
            self.log(f"{experiment['name']}: inserted {path.name} in {seconds}s")
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
        status, body = self.ops.http_json("POST", f"{GATEWAY_URL}/api/chat", payload=payload, timeout=PROBE_TIMEOUT)
        latency = round(self.ops.now() - started, 2)
        if status >= 400:
            raise RuntimeError(f"/api/chat returned HTTP {status}")
        answer = (body.get("message") or {}).get("content")
        if answer is None:
            raise RuntimeError("/api/chat response has no message.content")
        return str(answer), latency

    def _run_set(self, items: list[dict[str, Any]], concurrency: int) -> list[dict[str, Any]]:
        results: list[dict[str, Any] | None] = [None] * len(items)
        cursor = [0]
        lock = threading.Lock()

        def worker_thread() -> None:
            while True:
                with lock:
                    index = cursor[0]
                    if index >= len(items):
                        return
                    cursor[0] += 1
                item = items[index]
                row = {
                    "id": item.get("id"),
                    "type": item.get("type"),
                    "question": item.get("question"),
                    "answer": "",
                    "latency": 0.0,
                    "error": None,
                }
                try:
                    answer, latency = self._attempt(
                        lambda: self._ask(item), what=f"probe {item.get('id')}", attempts=2
                    )
                    row["answer"], row["latency"] = answer, latency
                except StepError as exc:
                    row["error"] = str(exc)
                results[index] = row
                self.heartbeat()

        threads = [threading.Thread(target=worker_thread) for _ in range(max(1, int(concurrency)))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return [row for row in results if row is not None]

    # ── step 5: judge ──────────────────────────────────────────────────────

    def _judge_one(self, item: dict[str, Any], answer: str) -> dict[str, str]:
        return judge_answer(self.ops, item, answer, self.judge_key)

    def _judge_set(self, items: list[dict[str, Any]], results: list[dict[str, Any]]) -> list[str]:
        verdicts: list[str] = []
        for item, row in zip(items, results):
            if row.get("error"):
                verdicts.append(JUDGE_ERROR)
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
        return verdicts

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
    ) -> None:
        exp_dir = self.out / experiment["name"]
        answers = [
            {"set": set_name, **{key: row.get(key) for key in ("id", "type", "question", "answer", "latency", "error")}}
            for row in rows
        ]
        judged = [
            {"set": set_name, "id": item.get("id"), "type": item.get("type"), "verdict": verdict}
            for item, verdict in zip(items, verdicts)
        ]
        self.ops.append_text(
            exp_dir / "answers.jsonl", "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in answers)
        )
        self.ops.append_text(
            exp_dir / "verdicts.jsonl", "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in judged)
        )

    def _run_experiment(self, experiment: dict[str, Any]) -> dict[str, Any]:
        name = experiment["name"]
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

        dev_items = self._load_probe_items(self.dev_probe_file)
        heldout_items = self._load_probe_items(self.heldout_probe_file)
        dev_rows = self._run_set(dev_items, experiment["probe_concurrency"])
        self.judge_truncated = 0
        dev_verdicts = self._judge_set(dev_items, dev_rows)
        self._record(experiment, "dev", dev_items, dev_rows, dev_verdicts)
        heldout_rows = self._run_set(heldout_items, experiment["probe_concurrency"])
        heldout_verdicts = self._judge_set(heldout_items, heldout_rows)
        self._record(experiment, "heldout", heldout_items, heldout_rows, heldout_verdicts)

        latencies = [row["latency"] for row in dev_rows + heldout_rows if not row.get("error")]
        summary = {
            "dev": score_set(dev_items, dev_verdicts, [row["latency"] for row in dev_rows]),
            "heldout": score_set(heldout_items, heldout_verdicts, [row["latency"] for row in heldout_rows]),
            "all_latencies_p50": percentile(latencies, 0.5),
            "all_latencies_p95": percentile(latencies, 0.95),
            "probe_errors": sum(1 for row in dev_rows + heldout_rows if row.get("error")),
            "reingest": experiment["reingest"],
            "env": set_values,
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
            "heldout_correct_pct": summary["heldout"]["correct_pct"],
            "heldout_ok_pct": summary["heldout"]["ok_pct"],
            "heldout_partial_credit_pct": summary["heldout"]["partial_credit_pct"],
            "heldout_hallucination_pct": summary["heldout"]["hallucination_pct"],
            "refusal_accuracy_pct": summary["heldout"]["refusal_accuracy_pct"],
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
        self.save_state()
        return summary

    # ── run control ────────────────────────────────────────────────────────

    def _prepare(self) -> None:
        self.ops.write_text_atomic(self.heartbeat_path, "")  # creates out/
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
                f"cannot restore the baseline {CONTAINER}: {exc}; "
                f"{self._container_error_lines(CONTAINER)}"
            ) from exc
        if self.gateway_touched:
            try:
                self._restart_gateway()
                self._wait_gateway_up()
            except StepError as exc:
                raise UnrecoverableRun(
                    f"cannot restore the baseline {GATEWAY_CONTAINER}: {exc}; "
                    f"{self._container_error_lines(GATEWAY_CONTAINER)}"
                ) from exc
        self.log("baseline recovery done; the next experiment starts from the baseline config")

    def _run_experiments(self) -> None:
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
            self.heartbeat()
        return exit_code

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
        """Rank experiments by dev ok+0.5*partial% (a correct refusal counts).

        A state.json written before that key existed still ranks by the old
        correct+0.5*partial% number, so --resume across the change works.
        """
        results = self.state.get("results") or {}

        def score(values: dict[str, Any]) -> float:
            for key in ("dev_ok_partial_credit_pct", "dev_partial_credit_pct"):
                value = values.get(key)
                if isinstance(value, (int, float)):
                    return float(value)
            return 0.0

        ranked = sorted(
            (
                (name, values) for name, values in results.items()
                if "dev_ok_partial_credit_pct" in values or "dev_partial_credit_pct" in values
            ),
            key=lambda entry: (score(entry[1]), -entry[1].get("latency_p95", 0.0)),
            reverse=True,
        )
        return ranked[0] if ranked else (None, {})

    def _write_recommendation(self) -> None:
        baseline = self.config.get("baseline_env") or {}
        best_name, best = self._best_experiment()
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
            lines.append(f"- best experiment by dev ok+0.5*partial%: **{best_name}** "
                         f"({best.get('dev_ok_partial_credit_pct', best.get('dev_partial_credit_pct'))}%, "
                         f"p95 {best.get('latency_p95')} s)")
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
    arguments = parser.parse_args(argv)

    root_path = Path(arguments.root).expanduser()
    dev_probe = resolve_probe_path(arguments.dev_probes)
    heldout_probe = resolve_probe_path(arguments.heldout_probes)

    if arguments.rejudge:
        return rejudge(
            Path(arguments.rejudge).expanduser(), root_path,
            dev_probe=dev_probe, heldout_probe=heldout_probe,
        )

    if not arguments.experiments or not arguments.out:
        parser.error("--experiments and --out are required unless --rejudge is given")

    try:
        config = load_experiments(Path(arguments.experiments))
    except LoopConfigError as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 3

    # Names are checked before docker or HTTP is touched, so a typo costs nothing.
    for label, spec in (("--dev-probes", dev_probe), ("--heldout-probes", heldout_probe)):
        if not (root_path / spec).is_file():
            print(f"refusing to run: {label} file not found: {root_path / spec}", file=sys.stderr)
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
    )
    # file names only, never their contents
    loop.log(f"probe files: dev={dev_probe} heldout={heldout_probe}")
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
