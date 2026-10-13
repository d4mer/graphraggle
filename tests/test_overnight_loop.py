"""Offline tests for scripts/overnight_loop.py (packet-28).

No network, no docker, no sleeping: every side effect goes through a FakeOps
that answers scripted HTTP, records docker commands and advances a fake clock.
The filesystem is a real temp directory because the atomic-write and
byte-preservation behaviour is exactly what is under test.
"""

from __future__ import annotations

import ast
import builtins
import dis
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "scripts"))

import overnight_loop as ol  # noqa: E402

ENV_TEXT = (
    "RAG_API_KEY=gw-secret\n"
    "LIGHTRAG_API_KEY=lightrag-secret\n"
    "MAX_ASYNC=1\n"
    "# keep this comment line exactly\n"
    "MAX_PARALLEL_INSERT = 1\n"
    "export LLM_MODEL=qwen3.6-35b\n"
    "EMPTY_VAL=\n"
)

DEV_ITEMS = [
    {"id": "d1", "question": "Who is in Paris?", "expected_answer": "Marie", "evidence": "Marie is in Paris", "type": "single-fact"},
    {"id": "d2", "question": "What did the board decide?", "expected_answer": "fund phase 2", "evidence": "the board funded phase 2", "type": "multi-fact"},
    {"id": "d3", "question": "Is there anything about Mars?", "expected_answer": "not in the documents", "evidence": "", "type": "unanswerable"},
]
HELDOUT_ITEMS = [
    {"id": "h1", "question": "Where is the office?", "expected_answer": "Brussels", "evidence": "the office is in Brussels", "type": "single-fact"},
    {"id": "h2", "question": "Anything about the moon?", "expected_answer": "not in the documents", "evidence": "", "type": "unanswerable"},
    # "MISMATCH:" makes the fake judge say "wrong" (see FakeOps): without a wrong
    # answer here the set would score ok% 100 and every run would stop at SUCCESS.
    {"id": "h3", "question": "What is in the vault?", "expected_answer": "MISMATCH: gold", "evidence": "the vault holds nothing", "type": "single-fact"},
]


def _jsonl(items: list[dict]) -> str:
    return "".join(json.dumps(item) + "\n" for item in items)


class FakeOps(ol.Ops):
    """All side effects, scripted."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.clock = 0.0
        self.commands: list[list[str]] = []
        self.env_snapshots: list[str] = []
        self.http_calls: list[tuple[str, str]] = []
        self.headers_seen: list[tuple[str, dict]] = []
        self.delete_status = 200
        self.container_states: dict[str, str] = {}
        self.logs: dict[str, str] = {}
        self.worker_running = False
        self.fail_compose_calls: set[int] = set()
        self.raise_on_exec = False
        self.judge_garbage = 0
        self.chat_fails: set[str] = set()
        self.advance_per_call = 0.0
        self.compose_index = 0

    def now(self) -> float:
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.clock += max(0.0, seconds)

    def run(self, argv, *, cwd=None, timeout=180.0):
        argv = list(argv)
        self.commands.append(argv)
        if argv[:2] == ["docker", "compose"]:
            self.compose_index += 1
            self.env_snapshots.append((self.root / ".env").read_text(encoding="utf-8"))
            if self.compose_index in self.fail_compose_calls:
                return 1, "", "compose exploded"
            return 0, "", ""
        if argv[:2] == ["docker", "ps"]:
            if "-a" in argv:
                raw = ""
                for index, arg in enumerate(argv):
                    if arg.startswith("--filter"):
                        if arg.startswith("--filter="):
                            raw = arg.split("=", 1)[1]
                        elif index + 1 < len(argv):
                            raw = argv[index + 1]
                name = raw.removeprefix("name=")
                return 0, self.container_states.get(name or ol.CONTAINER, "running") + "\n", ""
            names = ["lightrag-server", "rag-gateway-api"]
            if self.worker_running:
                names.append("rag-ingest-worker")
            return 0, "".join(name + "\n" for name in names), ""
        if argv[:2] == ["docker", "exec"]:
            if self.raise_on_exec:
                raise RuntimeError("docker exec blew up")
            text = (self.root / ".env").read_text(encoding="utf-8")
            values = []
            for line in text.splitlines():
                match = ol._ENV_LINE.match(line.strip())
                if match:
                    values.append(f"{match.group(2)}={match.group(3).strip()}")
            return 0, "\n".join(values) + "\n", ""
        return 0, "", ""

    def http_json(self, method, url, *, headers=None, payload=None, timeout=30.0):
        self.http_calls.append((method, url))
        self.headers_seen.append((url, dict(headers or {})))
        self.clock += self.advance_per_call
        if url.endswith("/documents") and method == "DELETE" and self.delete_status != 200:
            return self.delete_status, {"error": "clear refused"}
        if url.endswith("/health"):
            return 200, {"status": "healthy"}
        if url.endswith("/v1/models"):
            return 200, {"data": [{"id": ol.JUDGE_MODEL}]}
        if url.endswith("/documents") and method == "DELETE":
            return 200, {"status": "ok"}
        if url.endswith("/status_counts"):
            return 200, {"status_counts": {
                "pending": 0, "parsing": 0, "analyzing": 0, "processing": 0,
                "preprocessed": 0, "processed": 0, "failed": 0, "all": 0,
            }}
        if url.endswith("/documents/text"):
            return 200, {"track_id": "track-1"}
        if "/documents/track_status/" in url:
            return 200, {"track_id": "track-1", "documents": [
                {"id": "doc-1", "status": "processed", "file_path": "doc1.txt",
                 "chunks_count": 1, "error_msg": None},
            ], "total_count": 1, "status_summary": {"processed": 1}}
        if url.endswith("/api/chat"):
            question = payload["messages"][-1]["content"]
            if question in self.chat_fails:
                return 500, {"error": "upstream failure"}
            return 200, {"message": {"content": f"answer for: {question}"}}
        if url.endswith("/v1/chat/completions"):
            if self.judge_garbage > 0:
                self.judge_garbage -= 1
                return 200, {"choices": [{"message": {"content": "I am unable to decide"}}]}
            prompt = payload["messages"][0]["content"]
            expected = prompt.split("Expected answer (ground truth):", 1)[1].split("Evidence quote", 1)[0].strip()
            if expected.startswith("MISMATCH:"):
                verdict = "wrong"
            else:
                verdict = "refused_ok" if "not in the documents" in expected else "correct"
            return 200, {"choices": [{"message": {"content": json.dumps({"verdict": verdict, "reason": "matches the evidence"})}}]}
        return 200, {}


class _Case(unittest.TestCase):
    """Temp root wired up like the Thinkpad layout, with a small experiment set."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        probe = self.root / "probe"
        probe.mkdir()
        (self.root / ".env").write_text(ENV_TEXT, encoding="utf-8")
        (probe / ol.DEV_PROBE_FILE.split("/", 1)[1]).write_text(_jsonl(DEV_ITEMS), encoding="utf-8")
        (probe / ol.HELDOUT_PROBE_FILE.split("/", 1)[1]).write_text(_jsonl(HELDOUT_ITEMS), encoding="utf-8")
        (probe / ol.CORPUS_FILE.split("/", 1)[1]).write_text(
            json.dumps([str(self.root / "doc1.txt")]), encoding="utf-8"
        )
        (self.root / "doc1.txt").write_text("transcript body\n", encoding="utf-8")
        self.out = self.root / "loop"
        self.experiments_path = self.root / "experiments.json"
        self.ops = FakeOps(self.root)

    def write_experiments(self, config: dict) -> dict:
        self.experiments_path.write_text(json.dumps(config), encoding="utf-8")
        return ol.load_experiments(self.experiments_path)

    def make_loop(self, config: dict, *, resume: bool = False, dry_run: bool = False, max_hours: float = 11.0) -> ol.Loop:
        return ol.Loop(
            root=self.root,
            out=self.out,
            config=config,
            ops=self.ops,
            max_hours=max_hours,
            dry_run=dry_run,
            resume=resume,
            handle_signals=False,
            print_fn=lambda line: None,
        )

    def env_now(self) -> str:
        return (self.root / ".env").read_text(encoding="utf-8")

    def compose_ups(self) -> list[list[str]]:
        return [
            command for command in self.ops.commands
            if command[:2] == ["docker", "compose"] and "up" in command
        ]

    def state(self) -> dict:
        return json.loads((self.out / "state.json").read_text(encoding="utf-8"))


# ── experiments.json: whitelist and refusal ────────────────────────────────

class TestExperimentsFile(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def _load(self, payload) -> dict:
        path = self.dir / "experiments.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return ol.load_experiments(path)

    def test_accepts_whitelisted_keys_and_keeps_file_order(self):
        config = self._load({
            "baseline_env": {"MAX_ASYNC": "1"},
            "experiments": [
                {"name": "E1", "env": {"MAX_ASYNC": "3", "LLM_MODEL_MAX_ASYNC": "3"}, "reingest": True, "probe_concurrency": 2},
                {"name": "E2", "env": {"CHUNK_SIZE": "2400"}},
                {"name": "E3", "env": {"RERANK_BY_DEFAULT": "false"}},
            ],
        })
        self.assertEqual([e["name"] for e in config["experiments"]], ["E1", "E2", "E3"])
        self.assertTrue(config["experiments"][0]["reingest"])
        self.assertEqual(config["experiments"][0]["probe_concurrency"], 2)
        self.assertFalse(config["experiments"][1]["reingest"])

    def test_refuses_key_outside_the_whitelist(self):
        with self.assertRaises(ol.LoopConfigError) as caught:
            self._load({"baseline_env": {}, "experiments": [{"name": "E1", "env": {"SOMETHING_ELSE": "1"}}]})
        self.assertIn("SOMETHING_ELSE", str(caught.exception))
        self.assertIn("not in the whitelist", str(caught.exception))

    def test_refuses_credentials_and_model_keys(self):
        for key in ("LIGHTRAG_API_KEY", "RAG_API_KEY", "OPENAI_API_KEY", "MY_SECRET", "DB_PASSWORD", "LLM_MODEL"):
            with self.subTest(key=key):
                with self.assertRaises(ol.LoopConfigError):
                    self._load({"baseline_env": {}, "experiments": [{"name": "E1", "env": {key: "x"}}]})

    def test_refuses_duplicate_names_missing_name_and_empty_list(self):
        with self.assertRaises(ol.LoopConfigError):
            self._load({"experiments": [{"name": "E1"}, {"name": "E1"}]})
        with self.assertRaises(ol.LoopConfigError):
            self._load({"experiments": [{"env": {"TOP_K": "1"}}]})
        with self.assertRaises(ol.LoopConfigError):
            self._load({"experiments": []})

    def test_refuses_missing_or_invalid_file(self):
        with self.assertRaises(ol.LoopConfigError):
            ol.load_experiments(self.dir / "nope.json")
        path = self.dir / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ol.LoopConfigError):
            ol.load_experiments(path)


# ── .env rewriting and restore ──────────────────────────────────────────────

class TestEnvRewrite(unittest.TestCase):
    def test_only_target_lines_change_everything_else_is_byte_identical(self):
        updated = ol.rewrite_env(ENV_TEXT, {"MAX_ASYNC": "3", "MAX_PARALLEL_INSERT": "2"})
        lines = updated.splitlines(keepends=True)
        self.assertEqual(lines[0], "RAG_API_KEY=gw-secret\n")
        self.assertEqual(lines[1], "LIGHTRAG_API_KEY=lightrag-secret\n")
        self.assertEqual(lines[2], "MAX_ASYNC=3\n")
        self.assertEqual(lines[3], "# keep this comment line exactly\n")
        self.assertEqual(lines[4], "MAX_PARALLEL_INSERT=2\n")
        self.assertEqual(lines[5], "export LLM_MODEL=qwen3.6-35b\n")
        self.assertEqual(lines[6], "EMPTY_VAL=\n")
        self.assertEqual(len(lines), len(ENV_TEXT.splitlines()))

    def test_missing_keys_are_appended_and_remove_keys_are_dropped(self):
        updated = ol.rewrite_env(ENV_TEXT, {"MAX_ASYNC": "3", "CHUNK_SIZE": "1200"})
        self.assertIn("CHUNK_SIZE=1200\n", updated)
        self.assertEqual(ol.env_value(updated, "CHUNK_SIZE"), "1200")
        removed = ol.rewrite_env(updated, {"MAX_ASYNC": "1"}, remove_keys={"CHUNK_SIZE"})
        self.assertIsNone(ol.env_value(removed, "CHUNK_SIZE"))
        self.assertEqual(ol.env_value(removed, "MAX_ASYNC"), "1")

    def test_values_with_spaces_are_quoted(self):
        updated = ol.rewrite_env(ENV_TEXT, {"ENTITY_TYPES": "Person, Organization"})
        self.assertIn('ENTITY_TYPES="Person, Organization"', updated)

    def test_env_value_handles_spaces_and_quotes(self):
        self.assertEqual(ol.env_value('A = "one two"\n', "A"), "one two")
        self.assertEqual(ol.env_value("B=3\n", "B"), "3")
        self.assertIsNone(ol.env_value("B=3\n", "C"))


# ── judge JSON parsing ──────────────────────────────────────────────────────

class TestJudgeParsing(unittest.TestCase):
    def test_clean_json(self):
        self.assertEqual(
            ol.parse_verdict('{"verdict": "correct", "reason": "matches"}'),
            {"verdict": "correct", "reason": "matches"},
        )

    def test_fenced_json(self):
        parsed = ol.parse_verdict('```json\n{"verdict": "partial", "reason": "incomplete"}\n```')
        self.assertEqual(parsed["verdict"], "partial")

    def test_prose_around_the_object(self):
        parsed = ol.parse_verdict('Judgement: {"verdict":"hallucinated","reason":"invented"} thanks')
        self.assertEqual(parsed["verdict"], "hallucinated")

    def test_garbage_returns_none(self):
        self.assertIsNone(ol.parse_verdict("I cannot decide"))
        self.assertIsNone(ol.parse_verdict(""))
        self.assertIsNone(ol.parse_verdict(None))

    def test_unknown_verdict_returns_none(self):
        self.assertIsNone(ol.parse_verdict('{"verdict": "maybe", "reason": "?"}'))

    def test_braces_inside_strings_do_not_break_the_scan(self):
        parsed = ol.parse_verdict('{"verdict": "wrong", "reason": "said {curly} things"} trailing }')
        self.assertEqual(parsed["verdict"], "wrong")


# ── score maths ─────────────────────────────────────────────────────────────

class TestScoring(unittest.TestCase):
    def test_percentages_exclude_judge_errors(self):
        items = DEV_ITEMS + [{"id": "d4", "question": "q", "expected_answer": "a", "evidence": "e", "type": "single-fact"}]
        summary = ol.score_set(items, ["correct", "correct", "refused_ok", "hallucinated"], [1.0, 2.0, 3.0, 4.0])
        self.assertEqual(summary["judged"], 4)
        self.assertEqual(summary["correct_pct"], 50.0)
        self.assertEqual(summary["hallucination_pct"], 25.0)
        self.assertEqual(summary["refusal_accuracy_pct"], 100.0)
        self.assertEqual(summary["unanswerable_n"], 1)

    def test_partial_credit_and_judge_error_exclusion(self):
        items = [{"id": str(i), "type": "single-fact", "question": "q", "expected_answer": "a", "evidence": "e"} for i in range(5)]
        summary = ol.score_set(items, ["correct", "partial", "partial", "wrong", "judge_error"], [])
        self.assertEqual(summary["judged"], 4)
        self.assertEqual(summary["judge_errors"], 1)
        self.assertEqual(summary["correct_pct"], 25.0)
        self.assertEqual(summary["partial_credit_pct"], 50.0)  # (1 + 0.5*2) / 4
        self.assertIsNone(summary["refusal_accuracy_pct"])

    def test_by_type_and_latency_percentiles(self):
        items = [
            {"id": "a", "type": "single-fact", "question": "q", "expected_answer": "a", "evidence": "e"},
            {"id": "b", "type": "single-fact", "question": "q", "expected_answer": "a", "evidence": "e"},
            {"id": "c", "type": "cross-document", "question": "q", "expected_answer": "a", "evidence": "e"},
        ]
        summary = ol.score_set(items, ["correct", "partial", "wrong"], [1.0, 5.0, 9.0])
        self.assertEqual(summary["by_type"]["single-fact"]["partial_credit_pct"], 75.0)
        self.assertEqual(summary["by_type"]["cross-document"]["partial_credit_pct"], 0.0)
        self.assertEqual(summary["latency_p50"], 5.0)
        self.assertEqual(summary["latency_p95"], 9.0)

    def test_percentile_edges(self):
        self.assertEqual(ol.percentile([], 0.5), 0.0)
        self.assertEqual(ol.percentile([3.0], 0.95), 3.0)

    def test_success_thresholds(self):
        good = {"ok_pct": 90.0, "hallucination_pct": 4.9, "refusal_accuracy_pct": 80.0}
        self.assertTrue(ol.meets_success(good))
        self.assertFalse(ol.meets_success({**good, "ok_pct": 89.9}))
        self.assertFalse(ol.meets_success({**good, "hallucination_pct": 5.0}))
        self.assertFalse(ol.meets_success({**good, "refusal_accuracy_pct": 79.9}))
        self.assertFalse(ol.meets_success({**good, "refusal_accuracy_pct": None}))

    def _forty_items(self, unanswerable: int) -> list[dict]:
        items = []
        for index in range(40):
            if index < unanswerable:
                items.append({"id": f"h{index}", "type": "unanswerable", "question": f"q{index}",
                              "expected_answer": "not in the documents", "evidence": ""})
            else:
                items.append({"id": f"h{index}", "type": "single-fact", "question": f"q{index}",
                              "expected_answer": "a", "evidence": "e"})
        return items

    def test_ok_pct_counts_a_correct_refusal_as_answered_right(self):
        """loop4: 33 correct + 6 refused_ok + 1 wrong of 40 reported 82.5%."""
        summary = ol.score_set(self._forty_items(6), ["refused_ok"] * 6 + ["correct"] * 33 + ["wrong"], [])
        self.assertEqual(summary["judged"], 40)
        self.assertEqual(summary["correct_pct"], 82.5)           # unchanged, backward compatible
        self.assertEqual(summary["partial_credit_pct"], 82.5)    # unchanged
        self.assertEqual(summary["ok_pct"], 97.5)
        self.assertEqual(summary["ok_partial_credit_pct"], 97.5)
        self.assertEqual(summary["refusal_accuracy_pct"], 100.0)
        self.assertTrue(ol.meets_success(summary))               # 82.5% correct used to fail this

    def test_ok_pct_and_the_hallucination_boundary_on_40_probes(self):
        items = self._forty_items(4)
        summary = ol.score_set(items, ["refused_ok"] * 4 + ["correct"] * 34 + ["hallucinated"] * 2, [])
        self.assertEqual(summary["ok_pct"], 95.0)
        self.assertEqual(summary["hallucination_pct"], 5.0)
        self.assertFalse(ol.meets_success(summary))              # hallucination must stay < 5

        summary = ol.score_set(items, ["refused_ok"] * 4 + ["correct"] * 36, [])
        self.assertEqual(summary["ok_pct"], 100.0)
        self.assertEqual(summary["ok_partial_credit_pct"], 100.0)
        self.assertEqual(summary["hallucination_pct"], 0.0)
        self.assertTrue(ol.meets_success(summary))

    def test_ok_partial_credit_pct_adds_half_of_partial(self):
        summary = ol.score_set(self._forty_items(4),
                               ["refused_ok"] * 4 + ["correct"] * 20 + ["partial"] * 8 + ["wrong"] * 8, [])
        self.assertEqual(summary["ok_pct"], 60.0)
        self.assertEqual(summary["partial_credit_pct"], 60.0)
        self.assertEqual(summary["ok_partial_credit_pct"], 70.0)  # 60 + 0.5 * 20

    def test_results_row_renders_an_old_summary_without_the_new_keys(self):
        old = {"correct_pct": 80.0, "partial_credit_pct": 85.0, "hallucination_pct": 1.0,
               "refusal_accuracy_pct": 90.0, "judge_errors": 0}
        header_cells = ol.RESULTS_TABLE_HEADER.splitlines()[0].count("|")
        row = ol.results_row("E1", "done", old, old, 1.0, 2.0)
        self.assertEqual(row.count("|"), header_cells)
        self.assertIn("| n/a |", row)
        new = dict(old, ok_pct=90.0, ok_partial_credit_pct=95.0)
        row = ol.results_row("E1", "done", new, new, 1.0, 2.0)
        self.assertEqual(row.count("|"), header_cells)
        self.assertNotIn("n/a", row)
        self.assertIn("| 90.0 |", row)


# ── driver behaviour ────────────────────────────────────────────────────────

class TestDriver(_Case):
    def two_experiments(self) -> dict:
        return self.write_experiments({
            "baseline_env": {"MAX_ASYNC": "1"},
            "experiments": [
                {"name": "E1-async3", "env": {"MAX_ASYNC": "3", "CHUNK_SIZE": "1200"}, "reingest": True},
                {"name": "E2-topk", "env": {"TOP_K": "60"}},
            ],
        })

    def test_experiments_run_in_order_and_env_never_stacks(self):
        config = self.two_experiments()
        self.assertEqual(self.make_loop(config).run(), 0)

        # one compose up per experiment, plus one at finalise
        self.assertEqual(len(self.compose_ups()), 3)
        first, second = self.ops.env_snapshots[0], self.ops.env_snapshots[1]
        self.assertEqual(ol.env_value(first, "MAX_ASYNC"), "3")
        self.assertEqual(ol.env_value(first, "CHUNK_SIZE"), "1200")
        # E2 must see the baseline again: MAX_ASYNC back to 1, CHUNK_SIZE removed
        self.assertEqual(ol.env_value(second, "MAX_ASYNC"), "1")
        self.assertIsNone(ol.env_value(second, "CHUNK_SIZE"))
        self.assertEqual(ol.env_value(second, "TOP_K"), "60")

    def test_production_env_is_restored_byte_identically(self):
        config = self.two_experiments()
        self.make_loop(config).run()
        self.assertEqual(self.env_now(), ENV_TEXT)
        self.assertEqual((self.out / "env.original").read_text(encoding="utf-8"), ENV_TEXT)

    def test_env_original_is_backed_up_once_with_mode_600(self):
        config = self.two_experiments()
        self.make_loop(config).run()
        mode = (self.out / "env.original").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_reingest_clears_then_inserts_and_polls(self):
        config = self.two_experiments()
        self.make_loop(config).run()
        calls = self.ops.http_calls
        self.assertIn(("DELETE", "http://localhost:9621/documents"), calls)
        self.assertTrue(any(url.endswith("/documents/status_counts") for _, url in calls))
        self.assertTrue(any(url.endswith("/documents/text") for _, url in calls))
        self.assertTrue(any("/documents/track_status/" in url for _, url in calls))
        self.assertEqual(self.state()["corpus_built_by"], "E1-async3")

    def test_stop_on_success_skips_remaining_experiments(self):
        # heldout: 9 answerable + 1 correctly refused -> correct 90%, halluc 0%,
        # refusal 100% -> SUCCESS thresholds met
        heldout = [{"id": f"s{i}", "question": f"q{i}", "expected_answer": "a", "evidence": "e", "type": "single-fact"}
                   for i in range(9)]
        heldout.append({"id": "s9", "question": "qmoon", "expected_answer": "not in the documents",
                        "evidence": "", "type": "unanswerable"})
        (self.root / "probe" / "solvay-heldout-v1.jsonl").write_text(_jsonl(heldout), encoding="utf-8")
        config = self.write_experiments({
            "baseline_env": {},
            "experiments": [{"name": "E1", "env": {"MAX_ASYNC": "2"}}, {"name": "E2", "env": {"MAX_ASYNC": "3"}}],
        })
        self.make_loop(config).run()
        state = self.state()
        self.assertEqual(state["status"], "success")
        self.assertEqual(state["experiments"]["E1"]["status"], "done")
        self.assertNotIn("E2", state["experiments"])
        self.assertTrue(state["stop_reason"].startswith("success in E1"))

    def test_experiment_error_continues_with_the_next(self):
        config = self.write_experiments({
            "baseline_env": {},
            "experiments": [{"name": "E1", "env": {"MAX_ASYNC": "2"}}, {"name": "E2", "env": {"MAX_ASYNC": "3"}}],
        })
        self.ops.fail_compose_calls = {1}
        self.assertEqual(self.make_loop(config).run(), 0)
        state = self.state()
        self.assertEqual(state["experiments"]["E1"]["status"], "error")
        self.assertIn("compose exploded", state["experiments"]["E1"]["error"])
        self.assertEqual(state["experiments"]["E2"]["status"], "done")

    def test_three_consecutive_errors_stop_the_run(self):
        config = self.write_experiments({
            "baseline_env": {},
            "experiments": [{"name": f"E{i}", "env": {}} for i in range(1, 5)],
        })
        # every experiment's own compose fails; the compose that baseline recovery
        # issues in between succeeds, so the run reaches the 3-error stop
        self.ops.fail_compose_calls = {1, 3, 5}
        self.make_loop(config).run()
        state = self.state()
        self.assertEqual([state["experiments"][f"E{i}"]["status"] for i in range(1, 4)], ["error", "error", "error"])
        self.assertNotIn("E4", state["experiments"])
        self.assertEqual(state["stop_reason"], "three_consecutive_errors")

    def test_resume_skips_experiments_already_done_or_error(self):
        config = self.write_experiments({
            "baseline_env": {},
            "experiments": [{"name": "E1", "env": {}}, {"name": "E2", "env": {}}],
        })
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "state.json").write_text(
            json.dumps({"status": "running", "experiments": {"E1": {"status": "done"}, "E2": {"status": "pending"}},
                        "changed_keys": [], "results": {}}),
            encoding="utf-8",
        )
        self.make_loop(config, resume=True).run()
        state = self.state()
        self.assertEqual(state["experiments"]["E1"]["status"], "done")
        self.assertEqual(state["experiments"]["E2"]["status"], "done")
        # only E2 was started, so exactly one compose up for experiments plus none
        # at finalise (no keys were changed in this resumed run)
        self.assertEqual(len(self.compose_ups()), 1)

    def test_worker_running_is_fatal_and_the_script_never_stops_it(self):
        config = self.two_experiments()
        self.ops.worker_running = True
        self.make_loop(config).run()
        state = self.state()
        self.assertEqual(state["status"], "aborted")
        self.assertEqual(state["experiments"]["E1-async3"]["status"], "error")
        self.assertNotIn("E2-topk", state["experiments"])
        self.assertIn("rag-ingest-worker is running", state["experiments"]["E1-async3"]["error"])
        self.assertFalse([c for c in self.ops.commands if c[:2] == ["docker", "stop"]])
        self.assertEqual(self.env_now(), ENV_TEXT)

    def test_finalise_runs_after_an_unexpected_exception(self):
        config = self.two_experiments()
        self.ops.raise_on_exec = True
        self.assertEqual(self.make_loop(config).run(), 2)
        state = self.state()
        self.assertEqual(state["status"], "fatal")
        self.assertIn("RuntimeError", state["stop_reason"])
        # env restored, then the container recreated from the restored env
        self.assertEqual(self.env_now(), ENV_TEXT)
        self.assertGreaterEqual(len(self.compose_ups()), 2)

    def test_unparsable_judge_becomes_judge_error_and_is_counted(self):
        config = self.write_experiments({"baseline_env": {}, "experiments": [{"name": "E1", "env": {}}]})
        self.ops.judge_garbage = 2  # both attempts for the first question
        self.make_loop(config).run()
        verdicts = [
            json.loads(line)
            for line in (self.out / "E1" / "verdicts.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(verdicts[0]["verdict"], "judge_error")
        summary = json.loads((self.out / "E1" / "summary.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(summary["dev"]["judge_errors"], 1)
        self.assertEqual(summary["dev"]["judged"], len(DEV_ITEMS) - summary["dev"]["judge_errors"])

    def test_probe_error_is_recorded_not_fatal(self):
        config = self.write_experiments({"baseline_env": {}, "experiments": [{"name": "E1", "env": {}}]})
        self.ops.chat_fails = {"Who is in Paris?"}
        self.make_loop(config).run()
        answers = [
            json.loads(line)
            for line in (self.out / "E1" / "answers.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        failed = [row for row in answers if row.get("error")]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["id"], "d1")
        summary = json.loads((self.out / "E1" / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["probe_errors"], 1)

    def test_state_and_heartbeat_files_are_written(self):
        config = self.two_experiments()
        self.make_loop(config).run()
        state = self.state()
        self.assertIn("E1-async3", state["experiments"])
        self.assertTrue(state["changed_keys"])
        self.assertTrue((self.out / "heartbeat").read_text(encoding="utf-8").strip())
        self.assertTrue((self.out / "loop.log").read_text(encoding="utf-8"))

    def test_results_table_has_a_row_per_experiment_and_names_the_corpus(self):
        config = self.two_experiments()
        self.make_loop(config).run()
        results = (self.out / "results.md").read_text(encoding="utf-8")
        self.assertIn("| E1-async3 | done |", results)
        self.assertIn("| E2-topk | done |", results)
        self.assertIn("current corpus built by: **E1-async3**", results)
        self.assertIn("dev ok%", results)
        self.assertIn("heldout ok%", results)

    def test_ok_percentages_land_in_state_json(self):
        config = self.two_experiments()
        self.make_loop(config).run()
        stored = self.state()["results"]["E1-async3"]
        self.assertEqual(stored["dev_ok_pct"], 100.0)            # d1 + d2 correct, d3 refused_ok
        self.assertEqual(stored["dev_ok_partial_credit_pct"], 100.0)
        self.assertEqual(stored["dev_correct_pct"], 66.67)       # old keys unchanged
        self.assertEqual(stored["heldout_ok_pct"], 66.67)        # h1 + h2 of 3, h3 judged wrong
        self.assertEqual(stored["heldout_correct_pct"], 33.33)

    def test_best_experiment_ranks_by_dev_ok_partial_credit(self):
        config = self.two_experiments()
        loop = self.make_loop(config)
        loop.load_state()
        loop.state["results"] = {
            "A": {"dev_partial_credit_pct": 92.0, "dev_ok_partial_credit_pct": 91.5,
                  "latency_p95": 5.0, "env": {}},
            "B": {"dev_partial_credit_pct": 90.0, "dev_ok_partial_credit_pct": 93.0,
                  "latency_p95": 5.0, "env": {}},
        }
        self.assertEqual(loop._best_experiment()[0], "B")        # the old metric would pick A
        loop._write_recommendation()
        text = (self.out / "results.md").read_text(encoding="utf-8")
        self.assertIn("best experiment by dev ok+0.5*partial%: **B** (93.0%", text)

    def test_an_old_state_json_still_ranks_without_the_new_key(self):
        config = self.two_experiments()
        loop = self.make_loop(config)
        loop.load_state()
        loop.state["results"] = {
            "A": {"dev_partial_credit_pct": 90.0, "latency_p95": 5.0, "env": {}},
            "B": {"dev_partial_credit_pct": 88.0, "latency_p95": 5.0, "env": {}},
        }
        self.assertEqual(loop._best_experiment()[0], "A")

    def test_best_experiment_env_is_recommended_but_not_applied(self):
        config = self.write_experiments({
            "baseline_env": {"MAX_ASYNC": "1"},
            "experiments": [{"name": "E1", "env": {"MAX_ASYNC": "4"}}],
        })
        self.make_loop(config).run()
        results = (self.out / "results.md").read_text(encoding="utf-8")
        self.assertIn("recommended env diff (NOT applied", results)
        self.assertIn("`MAX_ASYNC=4`", results)
        self.assertEqual(ol.env_value(self.env_now(), "MAX_ASYNC"), "1")

    def test_time_budget_stops_starting_new_experiments(self):
        config = self.write_experiments({
            "baseline_env": {},
            "experiments": [{"name": "E1", "env": {}}, {"name": "E2", "env": {}}],
        })
        self.ops.advance_per_call = 600.0  # every HTTP call burns 10 minutes
        self.make_loop(config, max_hours=0.5).run()
        state = self.state()
        self.assertEqual(state["experiments"]["E1"]["status"], "done")
        self.assertNotIn("E2", state["experiments"])
        self.assertEqual(state["stop_reason"], "time_budget")


# ── CLI ─────────────────────────────────────────────────────────────────────

class TestCommandLine(_Case):
    def test_dry_run_prints_the_plan_and_leaves_production_alone(self):
        self.write_experiments({
            "baseline_env": {"MAX_ASYNC": "1"},
            "experiments": [{"name": "E1", "env": {"MAX_ASYNC": "3"}, "reingest": True}],
        })
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = ol.main([
                "--root", str(self.root),
                "--experiments", str(self.experiments_path),
                "--out", str(self.out),
                "--max-hours", "11",
                "--dry-run",
            ])
        printed = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("1. E1: env[MAX_ASYNC=3]", printed)
        self.assertEqual(self.env_now(), ENV_TEXT)
        self.assertTrue((self.out / "dryrun.env").exists())

    def test_refuses_an_invalid_experiments_file(self):
        self.experiments_path.write_text(
            json.dumps({"experiments": [{"name": "E1", "env": {"LLM_MODEL": "x"}}]}), encoding="utf-8"
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = ol.main([
                "--root", str(self.root),
                "--experiments", str(self.experiments_path),
                "--out", str(self.out),
            ])
        self.assertEqual(code, 3)


# ── probe file selection (packet-35) ───────────────────────────────────────

class TestProbeFileSelection(_Case):
    """--dev-probes / --heldout-probes: defaults unchanged, everything else explicit."""

    def _v2_files(self) -> None:
        probe = self.root / "probe"
        (probe / "solvay-dev-v2.jsonl").write_text(_jsonl([
            {"id": "dev90", "question": "V2 dev question?", "expected_answer": "Marie",
             "evidence": "Marie is in Paris", "type": "single-fact"},
        ]), encoding="utf-8")
        (probe / "solvay-heldout-v2.jsonl").write_text(_jsonl([
            {"id": "ho90", "question": "V2 heldout question?", "expected_answer": "Brussels",
             "evidence": "the office is in Brussels", "type": "single-fact"},
        ]), encoding="utf-8")

    def _one_experiment(self) -> None:
        self.write_experiments({"baseline_env": {}, "experiments": [{"name": "E1", "env": {"MAX_ASYNC": "2"}}]})

    def _answer_ids(self) -> list[str]:
        return [json.loads(line)["id"]
                for line in (self.out / "E1" / "answers.jsonl").read_text(encoding="utf-8").splitlines()]

    def _run_cli(self, extra: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = ol.main([
                "--root", str(self.root),
                "--experiments", str(self.experiments_path),
                "--out", str(self.out),
                "--dry-run", *extra,
            ])
        return code, buffer.getvalue()

    def test_resolution_rules(self):
        self.assertEqual(ol.resolve_probe_path(ol.DEV_PROBE_FILE), "probe/solvay-dev-v1.jsonl")
        self.assertEqual(ol.resolve_probe_path(ol.HELDOUT_PROBE_FILE), "probe/solvay-heldout-v1.jsonl")
        self.assertEqual(ol.resolve_probe_path("solvay-dev-v2.jsonl"), "probe/solvay-dev-v2.jsonl")
        self.assertEqual(ol.resolve_probe_path("probe/solvay-dev-v2.jsonl"), "probe/solvay-dev-v2.jsonl")
        self.assertEqual(ol.resolve_probe_path("/tmp/elsewhere/x.jsonl"), "/tmp/elsewhere/x.jsonl")

    def test_defaults_are_still_the_v1_files(self):
        self._v2_files()
        self._one_experiment()
        code, printed = self._run_cli([])
        self.assertEqual(code, 0)
        self.assertIn(
            "probe files: dev=probe/solvay-dev-v1.jsonl heldout=probe/solvay-heldout-v1.jsonl", printed
        )
        self.assertEqual(self._answer_ids(), ["d1", "d2", "d3", "h1", "h2", "h3"])

    def test_custom_relative_names_are_used_instead(self):
        self._v2_files()
        self._one_experiment()
        code, printed = self._run_cli([
            "--dev-probes", "solvay-dev-v2.jsonl", "--heldout-probes", "solvay-heldout-v2.jsonl",
        ])
        self.assertEqual(code, 0)
        self.assertIn(
            "probe files: dev=probe/solvay-dev-v2.jsonl heldout=probe/solvay-heldout-v2.jsonl", printed
        )
        self.assertEqual(self._answer_ids(), ["dev90", "ho90"])

    def test_probe_prefixed_and_absolute_forms_work_too(self):
        self._v2_files()
        elsewhere = self.root / "elsewhere" / "heldout-v3.jsonl"
        elsewhere.parent.mkdir()
        elsewhere.write_text(_jsonl([
            {"id": "ho77", "question": "Absolute path question?", "expected_answer": "Brussels",
             "evidence": "the office is in Brussels", "type": "single-fact"},
        ]), encoding="utf-8")
        self._one_experiment()
        code, printed = self._run_cli([
            "--dev-probes", "probe/solvay-dev-v2.jsonl", "--heldout-probes", str(elsewhere),
        ])
        self.assertEqual(code, 0)
        self.assertIn(f"heldout={elsewhere}", printed)
        self.assertEqual(self._answer_ids(), ["dev90", "ho77"])

    def test_a_missing_probe_file_refuses_before_any_docker_or_http(self):
        import contextlib

        self._one_experiment()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = ol.main([
                "--root", str(self.root),
                "--experiments", str(self.experiments_path),
                "--out", str(self.out),
                "--dry-run", "--dev-probes", "nope.jsonl",
            ])
        self.assertEqual(code, 3)
        self.assertIn("--dev-probes file not found", stderr.getvalue())
        self.assertIn(str(self.root / "probe" / "nope.jsonl"), stderr.getvalue())
        self.assertFalse(self.out.exists())


# ── LightRAG API key is optional (packet-29) ───────────────────────────────

class TestLightRagApiKey(_Case):
    KEY = "lightrag-secret"

    def _env_without_key(self) -> str:
        return ENV_TEXT.replace(f"LIGHTRAG_API_KEY={self.KEY}\n", "")

    def _set_env(self, text: str) -> None:
        (self.root / ".env").write_text(text, encoding="utf-8")

    def _reingest_config(self) -> dict:
        return self.write_experiments({
            "baseline_env": {},
            "experiments": [{"name": "E1", "env": {}, "reingest": True}],
        })

    def _document_call_headers(self) -> list[tuple[str, dict]]:
        return [(url, headers) for url, headers in self.ops.headers_seen if "/documents" in url]

    def test_absent_key_sends_no_auth_header_and_reingest_succeeds(self):
        self._set_env(self._env_without_key())
        config = self._reingest_config()
        self.assertEqual(self.make_loop(config).run(), 0)
        state = self.state()
        self.assertEqual(state["experiments"]["E1"]["status"], "done")
        self.assertEqual(state["corpus_built_by"], "E1")
        calls = self._document_call_headers()
        self.assertTrue(any(url.endswith("/documents") for url, _ in calls))
        self.assertTrue(any(url.endswith("/documents/text") for url, _ in calls))
        self.assertTrue(any("/documents/track_status/" in url for url, _ in calls))
        for url, headers in calls:
            self.assertNotIn("X-API-Key", headers)

    def test_empty_key_is_treated_as_absent(self):
        self._set_env(ENV_TEXT.replace(f"LIGHTRAG_API_KEY={self.KEY}", "LIGHTRAG_API_KEY="))
        config = self._reingest_config()
        self.assertEqual(self.make_loop(config).run(), 0)
        self.assertEqual(self.state()["experiments"]["E1"]["status"], "done")
        for _url, headers in self._document_call_headers():
            self.assertNotIn("X-API-Key", headers)

    def test_present_key_sends_the_header_on_every_call(self):
        config = self._reingest_config()
        self.assertEqual(self.make_loop(config).run(), 0)
        calls = self._document_call_headers()
        self.assertTrue(calls)
        for _url, headers in calls:
            self.assertEqual(headers.get("X-API-Key"), self.KEY)

    def test_key_status_is_logged_once_per_run(self):
        self._set_env(self._env_without_key())
        config = self._reingest_config()
        self.make_loop(config).run()
        log = (self.out / "loop.log").read_text(encoding="utf-8")
        self.assertEqual(log.count("LightRAG API key: absent (open access)"), 1)
        self.assertNotIn("LightRAG API key: present", log)

    def test_present_key_logs_present_once_without_the_value(self):
        config = self._reingest_config()
        self.make_loop(config).run()
        log = (self.out / "loop.log").read_text(encoding="utf-8")
        self.assertEqual(log.count("LightRAG API key: present"), 1)
        self.assertNotIn(self.KEY, log)

    def test_key_value_never_reaches_the_log_or_an_error_message(self):
        config = self._reingest_config()
        self.ops.delete_status = 500
        self.make_loop(config).run()
        state = self.state()
        self.assertEqual(state["experiments"]["E1"]["status"], "error")
        self.assertIn("DELETE /documents returned 500", state["experiments"]["E1"]["error"])
        self.assertNotIn(self.KEY, state["experiments"]["E1"]["error"])
        self.assertNotIn(self.KEY, (self.out / "loop.log").read_text(encoding="utf-8"))
        self.assertNotIn(self.KEY, json.dumps(state))


# ── static integrity: no undefined names (packet-30) ────────────────────────

class TestStaticIntegrity(unittest.TestCase):
    """The fake-Ops tests never execute Ops.run, so a missing import crashed the
    first real run. This catches undefined names statically."""

    MODULE_PATH = ROOT_DIR / "scripts" / "overnight_loop.py"
    ALLOWED = {"__name__", "__file__", "__module__", "__doc__", "__builtins__"}

    def _module_level_names(self, tree: ast.Module) -> set[str]:
        """Names bound at module level, including inside module-level if/try/for
        blocks, but not names local to a function or class body."""
        names: set[str] = set()

        def collect(node: ast.AST) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
                return
            if isinstance(node, ast.Lambda):
                return
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
            elif isinstance(node, ast.TypeAlias):
                names.add(node.name.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                names.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            for child in ast.iter_child_nodes(node):
                collect(child)

        for statement in tree.body:
            collect(statement)
        return names

    def test_no_undefined_names(self):
        source = self.MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(self.MODULE_PATH))
        defined = self._module_level_names(tree) | self.ALLOWED
        code = compile(tree, str(self.MODULE_PATH), "exec")
        undefined: dict[str, list[str]] = {}

        def walk(co, scope):
            for instr in dis.get_instructions(co):
                # Functions/classes load globals by name; the module body (and
                # class bodies) use LOAD_NAME for both.
                if instr.opname == "LOAD_GLOBAL" or (
                    co.co_name == "<module>" and instr.opname == "LOAD_NAME"
                ):
                    name = instr.argval
                    if name in defined or hasattr(builtins, name):
                        continue
                    undefined.setdefault(name, []).append(scope)
            for const in co.co_consts:
                if hasattr(const, "co_name"):
                    walk(const, f"{scope}.{const.co_name}")

        walk(code, "<module>")
        self.assertEqual(undefined, {}, f"undefined names: {undefined}")

    def test_names_used_in_annotations_are_defined(self):
        """`from __future__ import annotations` hides these from the bytecode."""
        source = self.MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(self.MODULE_PATH))
        defined = self._module_level_names(tree) | self.ALLOWED
        annotations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.arg) and node.annotation is not None:
                annotations.append(node.annotation)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns:
                annotations.append(node.returns)
            elif isinstance(node, ast.AnnAssign):
                annotations.append(node.annotation)
        used = {n.id for ann in annotations for n in ast.walk(ann) if isinstance(n, ast.Name)}
        missing = sorted(name for name in used if name not in defined and not hasattr(builtins, name))
        self.assertEqual(missing, [], f"annotation names not defined: {missing}")


# ── a stub HTTP service used by the real-path tests ────────────────────────

class _RawBody:
    """Force the stub to answer with a non-JSON body."""

    def __init__(self, text: str) -> None:
        self.text = text


class _StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the test output clean
        pass

    def _respond(self, method: str) -> None:
        stub = self.server.stub
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        record = {
            "method": method,
            "path": self.path,
            "headers": {key.lower(): value for key, value in self.headers.items()},
            "body": raw,
        }
        stub.records.append(record)
        route = stub.routes.get((method, self.path)) or stub.routes.get((method, "*"))
        if route is None:
            status, payload = 404, {"error": f"no stub route for {method} {self.path}"}
        elif callable(route):
            status, payload = route(record)
        else:
            status, payload = route
        if isinstance(payload, _RawBody):
            body = payload.text.encode("utf-8")
            content_type = "text/plain"
        else:
            body = json.dumps(payload).encode("utf-8")
            content_type = "application/json"
        self.send_response(status)
        self.send_header("Connection", "close")
        self.close_connection = True
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._respond("GET")

    def do_POST(self):
        self._respond("POST")

    def do_DELETE(self):
        self._respond("DELETE")


class _StubServer:
    """stdlib ThreadingHTTPServer on an ephemeral port, with recorded requests."""

    def __init__(self) -> None:
        self.records: list[dict] = []
        self.routes: dict[tuple[str, str], tuple[int, object]] = {}
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self._httpd.stub = self
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def calls(self, method: str, path: str | None = None) -> list[dict]:
        return [
            record
            for record in self.records
            if record["method"] == method and (path is None or record["path"] == path)
        ]


# ── real Ops, no fakes (packet-30) ──────────────────────────────────────

class TestRealOps(unittest.TestCase):
    def setUp(self):
        self.ops = ol.Ops()
        self.stub = _StubServer()
        self.stub.routes = {
            ("GET", "/thing"): (200, {"ok": True}),
            ("POST", "/thing"): (201, {"created": True}),
            ("DELETE", "/thing"): (200, {"deleted": True}),
            ("GET", "/missing"): (404, {"error": "nope"}),
            ("GET", "/broken"): (500, {"error": "boom"}),
            ("GET", "/plain"): (200, {"note": "see headers"}),
        }
        self.stub.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.stub.stop()

    # ── subprocess ────────────────────────────────────────────────────────

    def test_run_returns_stdout_and_empty_stderr(self):
        self.assertEqual(self.ops.run(["echo", "hi"]), (0, "hi\n", ""))

    def test_run_reports_failure_returncode(self):
        code, _out, err = self.ops.run(["sh", "-c", "echo bad >&2; exit 7"])
        self.assertEqual(code, 7)
        self.assertEqual(err.strip(), "bad")

    def test_run_nonexistent_command_returns_127_without_raising(self):
        code, _out, err = self.ops.run(["definitely-not-a-command-xyz", "arg"])
        self.assertEqual(code, 127)
        self.assertIn("command not found", err)

    def test_run_timeout_returns_124_without_raising(self):
        code, _out, err = self.ops.run(["sleep", "5"], timeout=0.2)
        self.assertEqual(code, 124)
        self.assertIn("timed out", err)

    # ── http ───────────────────────────────────────────────────────────────

    def test_http_json_get_sends_headers_and_parses_json(self):
        status, body = self.ops.http_json(
            "GET", f"{self.stub.base}/thing", headers={"X-API-Key": "k1"}, timeout=10
        )
        self.assertEqual((status, body), (200, {"ok": True}))
        record = self.stub.calls("GET", "/thing")[-1]
        self.assertEqual(record["headers"]["x-api-key"], "k1")

    def test_http_json_post_sends_json_body_and_content_type(self):
        status, body = self.ops.http_json(
            "POST", f"{self.stub.base}/thing", payload={"text": "hello", "n": 2}, timeout=10
        )
        self.assertEqual((status, body), (201, {"created": True}))
        record = self.stub.calls("POST", "/thing")[-1]
        self.assertEqual(record["headers"]["content-type"], "application/json")
        self.assertEqual(json.loads(record["body"]), {"text": "hello", "n": 2})

    def test_http_json_delete_uses_the_delete_method(self):
        status, body = self.ops.http_json("DELETE", f"{self.stub.base}/thing", timeout=10)
        self.assertEqual((status, body), (200, {"deleted": True}))
        self.assertEqual(len(self.stub.calls("DELETE", "/thing")), 1)

    def test_http_json_returns_non_200_codes_as_values(self):
        for path, expected in (("/missing", 404), ("/broken", 500)):
            status, body = self.ops.http_json("GET", f"{self.stub.base}{path}", timeout=10)
            self.assertEqual(status, expected)
            self.assertIn("error", body)

    def test_http_json_unparsable_body_comes_back_raw(self):
        self.stub.routes[("GET", "/notjson")] = (200, _RawBody("<html>gateway error</html>"))
        status, body = self.ops.http_json("GET", f"{self.stub.base}/notjson", timeout=10)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"raw": "<html>gateway error</html>"})

    def test_http_json_connection_refused_raises(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaises(urllib.error.URLError):
            self.ops.http_json("GET", f"http://127.0.0.1:{port}/health", timeout=5)

    # ── files, clock ───────────────────────────────────────────────────────

    def test_read_text_and_write_text_atomic_roundtrip(self):
        target = self.root / "nested" / "file.txt"
        self.ops.write_text_atomic(target, "first\n")
        self.assertEqual(self.ops.read_text(target), "first\n")
        self.ops.write_text_atomic(target, "second\n")
        self.assertEqual(self.ops.read_text(target), "second\n")
        self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_write_text_atomic_applies_mode(self):
        target = self.root / "secret.env"
        self.ops.write_text_atomic(target, "a=b\n", mode=0o600)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_concurrent_atomic_writes_to_one_path_do_not_race(self):
        """A shared '<name>.tmp' let one thread rename the file out from under
        another, which killed probe threads calling heartbeat()."""
        target = self.root / "heartbeat"
        errors: list[str] = []

        def writer() -> None:
            for _ in range(40):
                try:
                    self.ops.write_text_atomic(target, "tick\n")
                except Exception as exc:  # pragma: no cover - the bug under test
                    errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=writer) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.ops.read_text(target), "tick\n")

    def test_sleep_and_clock_are_callable_and_real(self):
        self.assertIsNone(self.ops.sleep(0))
        self.assertIsNone(self.ops.sleep(-1))  # clamped, never negative
        first = self.ops.now()
        self.ops.sleep(0.01)
        self.assertGreaterEqual(self.ops.now(), first)


# ── end-to-end with the REAL Ops: fake docker on PATH + stub services ──────

FAKE_DOCKER = """#!/usr/bin/env bash
# Records its arguments, then answers the subcommands overnight_loop uses.
# Behaviour is steered by files in $FAKE_DOCKER_DIR:
#   always-exit         every lightrag-server recreate leaves the container exited
#   always-restarting   every lightrag-server recreate leaves it restarting
#   state-<container>   the state `docker ps -a` reports (default: running)
#   logs-<container>    output of `docker logs --tail 40 <container>`
#   gateway-running     marker set once gateway-api has been recreated
#   exec-empty-gateway  `docker exec rag-gateway-api env` prints nothing
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  ps)
    shift
    format=""; filter=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --filter) filter="$2"; shift ;;
        --format) format="$2"; shift ;;
      esac
      shift
    done
    if [ "$format" = "{{.State}}" ]; then
      name="${filter#name=}"
      if [ -f "$FAKE_DOCKER_DIR/state-$name" ]; then cat "$FAKE_DOCKER_DIR/state-$name"; else echo running; fi
      exit 0
    fi
    echo lightrag-server
    if [ -f "$FAKE_DOCKER_DIR/gateway-running" ]; then echo rag-gateway-api; fi
    exit 0;;
  compose)
    case "$*" in
      *gateway-api*) touch "$FAKE_DOCKER_DIR/gateway-running" ;;
      *lightrag-server*)
        if [ -f "$FAKE_DOCKER_DIR/always-exit" ]; then
          echo exited > "$FAKE_DOCKER_DIR/state-lightrag-server"
        elif [ -f "$FAKE_DOCKER_DIR/always-restarting" ]; then
          echo restarting > "$FAKE_DOCKER_DIR/state-lightrag-server"
        elif grep -q '^CHUNK_SIZE=666' "$FAKE_DOCKER_ENV_FILE"; then
          echo exited > "$FAKE_DOCKER_DIR/state-lightrag-server"
        else
          rm -f "$FAKE_DOCKER_DIR/state-lightrag-server"
        fi ;;
    esac
    exit 0;;
  exec)
    if [ "$3" = "env" ]; then
      if [ -f "$FAKE_DOCKER_DIR/exec-empty-gateway" ] && [ "$2" = "rag-gateway-api" ]; then exit 0; fi
      grep -E '^[A-Z_][A-Z0-9_]*=' "$FAKE_DOCKER_ENV_FILE"
      exit 0
    fi
    exit 1;;
  logs)
    shift 3
    if [ -f "$FAKE_DOCKER_DIR/logs-$1" ]; then cat "$FAKE_DOCKER_DIR/logs-$1"; fi
    exit 0;;
esac
exit 0
"""

CONTAINER_LOG_WITH_ERROR = (
    "INFO: started\n"
    "ERROR: CHUNK_SIZE environment variable is no longer supported for this value\n"
    "ERROR: startup aborted with LIGHTRAG_API_KEY=super-secret-value-do-not-log\n"
    "Traceback (most recent call last):\n"
)

E2E_ENV = (
    "RAG_API_KEY=gw-secret-do-not-log\n"
    "LIGHTRAG_API_KEY=lightrag-secret-do-not-log\n"
    "MAX_ASYNC=1\n"
    "CHUNK_SIZE=1200\n"
    "# a comment line that must survive\n"
    "LLM_MODEL=qwen3.6-35b\n"
)


class _FastOps(ol.Ops):
    """Real Ops; only sleeping is shortened."""

    def sleep(self, seconds: float) -> None:
        time.sleep(0)


class _E2EFixture:
    """Fixture: real Ops, fake docker on PATH, stub services. Mixed into the
    end-to-end test classes; it defines no tests itself."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "rag-project"
        self.out = self.root / "probe" / "loop"
        self.root.mkdir(parents=True)

        (self.root / ".env").write_text(E2E_ENV, encoding="utf-8")
        (self.root / "compose.yml").write_text("services: {}\n", encoding="utf-8")

        probe = self.root / "probe"
        probe.mkdir(parents=True, exist_ok=True)
        dev = [
            {"id": "d1", "question": "Who is in Paris?", "expected_answer": "Marie", "evidence": "Marie is in Paris", "type": "single-fact",
             "history": [{"role": "user", "content": "Earlier context"}, {"role": "assistant", "content": "Noted."}]},
            {"id": "d2", "question": "Board decision?", "expected_answer": "fund phase 2", "evidence": "the board funded phase 2", "type": "multi-fact"},
            {"id": "d3", "question": "Anything about Mars?", "expected_answer": "not in the documents", "evidence": "", "type": "unanswerable"},
        ]
        heldout = [
            {"id": "h1", "question": "Where is the office?", "expected_answer": "Brussels", "evidence": "the office is in Brussels", "type": "single-fact"},
            {"id": "h2", "question": "Anything about the moon?", "expected_answer": "not in the documents", "evidence": "", "type": "unanswerable"},
        ]
        (probe / "solvay-dev-v1.jsonl").write_text(_jsonl(dev), encoding="utf-8")
        (probe / "solvay-heldout-v1.jsonl").write_text(_jsonl(heldout), encoding="utf-8")

        corpus_dir = self.root / "corpus"
        corpus_dir.mkdir()
        transcript = corpus_dir / "t1.txt"
        transcript.write_text("transcript one: Marie is in Paris\n", encoding="utf-8")
        (probe / "loop_corpus.json").write_text(json.dumps([str(transcript)]), encoding="utf-8")

        # fake docker, first on PATH; its behaviour is steered by files in bin_dir
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        self.docker_dir = bin_dir
        docker = bin_dir / "docker"
        docker.write_text(FAKE_DOCKER, encoding="utf-8")
        docker.chmod(0o755)
        self.docker_log = bin_dir / "docker.calls"
        self.environ_backup = dict(os.environ)
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        os.environ["FAKE_DOCKER_LOG"] = str(self.docker_log)
        os.environ["FAKE_DOCKER_DIR"] = str(bin_dir)
        os.environ["FAKE_DOCKER_ENV_FILE"] = str(self.root / ".env")
        self.addCleanup(self._restore_env)

        self.stub = _StubServer()
        self.stub.routes = {
            ("GET", "/health"): (200, {"status": "healthy"}),
            ("DELETE", "/documents"): (200, {"status": "cleared"}),
            ("GET", "/documents/status_counts"): (200, REAL_STATUS_COUNTS_ZERO),
            ("POST", "/documents/text"): (200, {"track_id": "tr1"}),
            ("GET", "/documents/track_status/tr1"): (200, REAL_TRACK_PROCESSED),
            ("POST", "/api/chat"): (200, {"message": {"content": "stub answer from the stack"}}),
            ("GET", "/v1/models"): (200, {"data": [{"id": "qwen3.6-35b"}]}),
            ("POST", "/v1/chat/completions"): (
                200,
                {"choices": [{"message": {"content": '{"verdict": "correct", "reason": "stub judge"}'}}]},
            ),
        }
        self.stub.start()
        self.addCleanup(self.stub.stop)

        self.url_backup = {name: getattr(ol, name) for name in ("LIGHTRAG_URL", "GATEWAY_URL", "JUDGE_URL", "SIGMA_URL")}
        ol.LIGHTRAG_URL = self.stub.base
        ol.GATEWAY_URL = self.stub.base
        ol.JUDGE_URL = f"{self.stub.base}/v1/chat/completions"
        ol.SIGMA_URL = f"{self.stub.base}/v1/models"
        self.addCleanup(self._restore_urls)

        self.dev_items, self.heldout_items = dev, heldout

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self.environ_backup)

    def _restore_urls(self):
        for name, value in self.url_backup.items():
            setattr(ol, name, value)

    def _config_path(self) -> Path:
        path = Path(self.tmp.name) / "experiments.json"
        path.write_text(
            json.dumps(
                {
                    "baseline_env": {"MAX_ASYNC": "1"},
                    "experiments": [
                        {"name": "E1-reingest", "env": {"CHUNK_SIZE": "1200"}, "reingest": True, "probe_concurrency": 2},
                        {"name": "E2-probes", "env": {"CHUNK_SIZE": "800", "MAX_ASYNC": "4"}, "reingest": False, "probe_concurrency": 1},
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

class TestEndToEndRealOps(_E2EFixture, unittest.TestCase):
    """Real subprocess calls to a fake docker on PATH, real HTTP to the stub
    server, real files: the whole driver end to end, no fake Ops anywhere.

    The fake docker prints the current .env, so container-env verification is
    exercised for real too."""

    def test_full_run_with_real_ops(self):
        config = ol.load_experiments(self._config_path())
        loop = ol.Loop(
            root=self.root,
            out=self.out,
            config=config,
            ops=_FastOps(),
            max_hours=1.0,
            handle_signals=False,
            print_fn=lambda line: None,
        )
        self.assertEqual(loop.run(), 0)

        state = json.loads((self.out / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["experiments"]["E1-reingest"]["status"], "done")
        self.assertEqual(state["experiments"]["E2-probes"]["status"], "done")
        self.assertEqual(state["corpus_built_by"], "E1-reingest")

        results = (self.out / "results.md").read_text(encoding="utf-8")
        self.assertEqual(len([line for line in results.splitlines() if line.startswith("| E")]), 2)
        self.assertIn("## Final state", results)

        # production .env is back exactly as it was, and the backup is private
        self.assertEqual((self.root / ".env").read_text(encoding="utf-8"), E2E_ENV)
        original = self.out / "env.original"
        self.assertEqual(original.read_text(encoding="utf-8"), E2E_ENV)
        self.assertEqual(original.stat().st_mode & 0o777, 0o600)

        log = (self.out / "loop.log").read_text(encoding="utf-8")
        self.assertNotIn("gw-secret-do-not-log", log)
        self.assertNotIn("lightrag-secret-do-not-log", log)

        # real subprocess path: compose recreations (E1, E2, finalise) and env reads
        calls = self.docker_log.read_text(encoding="utf-8").splitlines()
        ups = [line for line in calls if line.startswith("compose -f") and "up -d --no-deps lightrag-server" in line]
        self.assertEqual(len(ups), 3)
        self.assertEqual([line for line in calls if line == "ps --format {{.Names}}"], ["ps --format {{.Names}}"])
        self.assertEqual(len([line for line in calls if line == "exec lightrag-server env"]), 2)

        # reingest happened over real HTTP, with the key from .env
        cleared = self.stub.calls("DELETE", "/documents")
        self.assertEqual(len(cleared), 1)
        self.assertEqual(cleared[0]["headers"].get("x-api-key"), "lightrag-secret-do-not-log")
        inserted = self.stub.calls("POST", "/documents/text")
        self.assertEqual(len(inserted), 1)
        payload = json.loads(inserted[0]["body"])
        self.assertEqual(payload["file_source"], str(self.root / "corpus" / "t1.txt"))
        self.assertIn("Marie is in Paris", payload["text"])
        self.assertTrue(self.stub.calls("GET", "/documents/track_status/tr1"))

        # probes went through the gateway: history first, question last
        asks = self.stub.calls("POST", "/api/chat")
        self.assertEqual(len(asks), 10)  # 2 experiments x (3 dev + 2 heldout)
        bodies = [json.loads(record["body"]) for record in asks]
        self.assertEqual({body["model"] for body in bodies}, {"lightrag:latest"})
        self.assertEqual({body["stream"] for body in bodies}, {False})
        by_question = {body["messages"][-1]["content"]: body for body in bodies}
        self.assertEqual(
            set(by_question),
            {item["question"] for item in self.dev_items} | {item["question"] for item in self.heldout_items},
        )
        paris = by_question["Who is in Paris?"]
        self.assertEqual([turn["role"] for turn in paris["messages"]], ["user", "assistant", "user"])
        self.assertEqual(paris["messages"][0]["content"], "Earlier context")

        # every probe was judged, graded against the item's own evidence
        judged = self.stub.calls("POST", "/v1/chat/completions")
        self.assertEqual(len(judged), 10)
        prompts = [json.loads(record["body"])["messages"][0]["content"] for record in judged]
        self.assertTrue(any("Marie is in Paris" in prompt for prompt in prompts))
        self.assertTrue(all(json.loads(record["body"])["model"] == ol.JUDGE_MODEL for record in judged))

        for name in ("E1-reingest", "E2-probes"):
            summary = json.loads((self.out / name / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["dev"]["correct_pct"], 100.0)
            self.assertEqual(summary["heldout"]["correct_pct"], 100.0)
            self.assertEqual(summary["probe_errors"], 0)
            self.assertEqual(len((self.out / name / "answers.jsonl").read_text(encoding="utf-8").splitlines()), 5)
            self.assertEqual(len((self.out / name / "verdicts.jsonl").read_text(encoding="utf-8").splitlines()), 5)

    def test_resume_after_a_first_run_skips_finished_experiments(self):
        config = ol.load_experiments(self._config_path())
        first = ol.Loop(root=self.root, out=self.out, config=config, ops=_FastOps(), max_hours=1.0,
                        handle_signals=False, print_fn=lambda line: None)
        self.assertEqual(first.run(), 0)
        docker_calls_before = len(self.docker_log.read_text(encoding="utf-8").splitlines())
        asks_before = len(self.stub.calls("POST", "/api/chat"))

        resumed = ol.Loop(root=self.root, out=self.out, config=ol.load_experiments(self._config_path()),
                          ops=_FastOps(), max_hours=1.0, resume=True, handle_signals=False,
                          print_fn=lambda line: None)
        self.assertEqual(resumed.run(), 0)
        self.assertEqual(len(self.stub.calls("POST", "/api/chat")), asks_before)
        log = (self.out / "loop.log").read_text(encoding="utf-8")
        self.assertIn("E1-reingest: already done in state.json; skipping (--resume)", log)
        self.assertIn("E2-probes: already done in state.json; skipping (--resume)", log)
        self.assertLess(len(self.docker_log.read_text(encoding="utf-8").splitlines()), docker_calls_before + 3)


# ── judge token budget for a reasoning model (packet-31) ────────────────

class TestJudgeTokenBudget(unittest.TestCase):
    """qwen3.6-35b answers with message.reasoning_content plus message.content;
    a 400-token budget bought only thinking, content came back empty, and every
    verdict was judge_error. Real Ops over the stub judge endpoint."""

    def setUp(self):
        self.stub = _StubServer()
        self.stub.start()
        self.addCleanup(self.stub.stop)
        self.url_backup = ol.JUDGE_URL
        ol.JUDGE_URL = f"{self.stub.base}/v1/chat/completions"
        self.addCleanup(setattr, ol, "JUDGE_URL", self.url_backup)
        self.ops = ol.Ops()
        self.item = {
            "id": "d1", "question": "Who is in Paris?", "expected_answer": "Marie",
            "evidence": "Marie is in Paris", "type": "single-fact",
        }

    def reply(self, content, reasoning="", finish="stop"):
        return {"choices": [{"message": {"content": content, "reasoning_content": reasoning},
                             "finish_reason": finish}]}

    def test_budget_and_timeout_constants_are_large_enough(self):
        self.assertGreaterEqual(ol.JUDGE_MAX_TOKENS, 1000)
        self.assertEqual(ol.JUDGE_TIMEOUT, 300)

    def test_larger_budget_makes_the_old_empty_content_case_work(self):
        def route(record):
            sent = json.loads(record["body"])
            if sent["max_tokens"] < 1000:
                return 200, self.reply("", reasoning="thinking hard", finish="length")
            return 200, self.reply('{"verdict": "correct", "reason": "matches the evidence"}')

        self.stub.routes[("POST", "/v1/chat/completions")] = route
        verdict = ol.judge_answer(self.ops, self.item, "Marie is in Paris.", "key")
        self.assertEqual(verdict["verdict"], "correct")
        sent = json.loads(self.stub.calls("POST", "/v1/chat/completions")[-1]["body"])
        self.assertEqual(sent["max_tokens"], ol.JUDGE_MAX_TOKENS)
        self.assertGreaterEqual(sent["max_tokens"], 1000)

    def test_verdict_is_taken_from_the_last_object_in_reasoning_content(self):
        reasoning = (
            'Start with {"verdict": "wrong", "reason": "stale"} then reconsider; '
            'on balance {"verdict": "partial", "reason": "missing the date"}'
        )
        self.stub.routes[("POST", "/v1/chat/completions")] = (200, self.reply("", reasoning=reasoning))
        verdict = ol.judge_answer(self.ops, self.item, "Marie.", "key")
        self.assertEqual(verdict["verdict"], "partial")
        self.assertEqual(verdict["reason"], "missing the date")

    def test_truncated_reply_raises_judge_truncated(self):
        self.stub.routes[("POST", "/v1/chat/completions")] = (
            200, self.reply("", reasoning="a very long chain of thought", finish="length")
        )
        with self.assertRaises(ol.JudgeTruncated):
            ol.judge_answer(self.ops, self.item, "Marie.", "key")

    def test_reply_with_no_verdict_anywhere_stays_unparsable(self):
        self.stub.routes[("POST", "/v1/chat/completions")] = (
            200, self.reply("", reasoning="prose with no json at all")
        )
        with self.assertRaises(RuntimeError):
            ol.judge_answer(self.ops, self.item, "Marie.", "key")


class TestJudgeTruncationEndToEnd(_E2EFixture, unittest.TestCase):
    """Truncation must be reported once per experiment, not once per question, and
    must not be retried (each call is worth 300 s)."""

    def setUp(self):
        super().setUp()
        self.stub.routes[("POST", "/v1/chat/completions")] = (
            200,
            {"choices": [{"message": {"content": "", "reasoning_content": "thinking " * 300},
                          "finish_reason": "length"}]},
        )

    def test_truncation_logged_once_per_experiment_and_scored_judge_error(self):
        config = ol.load_experiments(self._config_path())
        loop = ol.Loop(root=self.root, out=self.out, config=config, ops=_FastOps(), max_hours=1.0,
                       handle_signals=False, print_fn=lambda line: None)
        self.assertEqual(loop.run(), 0)

        log = (self.out / "loop.log").read_text(encoding="utf-8")
        self.assertEqual(log.count("judge truncated (raise JUDGE_MAX_TOKENS)"), 2)

        # 5 questions x 2 experiments, one call each: no retry on truncation
        self.assertEqual(len(self.stub.calls("POST", "/v1/chat/completions")), 10)

        for name in ("E1-reingest", "E2-probes"):
            verdicts = [
                json.loads(line)["verdict"]
                for line in (self.out / name / "verdicts.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(verdicts, [ol.JUDGE_ERROR] * 5)
            summary = json.loads((self.out / name / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["dev"]["judge_errors"], 3)
            self.assertEqual(summary["heldout"]["judge_errors"], 2)
            self.assertEqual(summary["dev"]["correct_pct"], 0.0)
            self.assertEqual(summary["heldout"]["refusal_accuracy_pct"], None)

        self.assertEqual((self.root / ".env").read_text(encoding="utf-8"), E2E_ENV)


# ── offline --rejudge mode (packet-31) ───────────────────────────────

class TestRejudge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "rag"
        self.out = self.root / "probe" / "loop"
        (self.root / "probe").mkdir(parents=True)
        self.out.mkdir(parents=True)

        dev = [
            {"id": "dev1", "question": "Question one?", "expected_answer": "Alpha", "evidence": "evidence alpha", "type": "single-fact"},
            {"id": "dev2", "question": "Question two?", "expected_answer": "Beta", "evidence": "evidence beta", "type": "multi-fact"},
            {"id": "dev3", "question": "Question three?", "expected_answer": "Gamma", "evidence": "evidence gamma", "type": "single-fact"},
        ]
        heldout = [
            {"id": "held1", "question": "Question four?", "expected_answer": "Delta", "evidence": "evidence delta", "type": "single-fact"},
            {"id": "held2", "question": "Question five?", "expected_answer": "not in the documents", "evidence": "", "type": "unanswerable"},
        ]
        (self.root / "probe" / "solvay-dev-v1.jsonl").write_text(_jsonl(dev), encoding="utf-8")
        (self.root / "probe" / "solvay-heldout-v1.jsonl").write_text(_jsonl(heldout), encoding="utf-8")

        # files that --rejudge must never touch
        (self.root / ".env").write_text("MAX_ASYNC=1\nLIGHTRAG_API_KEY=secret-do-not-log\n", encoding="utf-8")
        (self.out / "results.md").write_text("SENTINEL results table\n", encoding="utf-8")
        (self.out / "state.json").write_text('{"status": "running"}\n', encoding="utf-8")
        self.untouched = {
            path: path.read_bytes()
            for path in (self.root / ".env", self.out / "results.md", self.out / "state.json")
        }

        e1 = self.out / "E1"
        e2 = self.out / "E2"
        e1.mkdir()
        e2.mkdir()
        (e1 / "answers.jsonl").write_text(
            "".join([
                json.dumps({"set": "dev", "id": "dev1", "type": "single-fact", "question": "Question one?",
                            "answer": "saved answer one", "latency": 1.5, "error": None}) + "\n",
                json.dumps({"set": "dev", "id": "dev2", "type": "multi-fact", "question": "Question two?",
                            "answer": "saved answer two", "latency": 2.0, "error": None}) + "\n",
                json.dumps({"set": "dev", "id": "dev3", "type": "single-fact", "question": "Question three?",
                            "answer": "", "latency": 0.2, "error": "/api/chat returned HTTP 502"}) + "\n",
                '{"set": "dev", "id": "dev4", "answer": "a line cut off mid-wri',  # half-written
            ]),
            encoding="utf-8",
        )
        (e1 / "verdicts.jsonl").write_text(
            "".join([
                json.dumps({"set": "dev", "id": "dev1", "type": "single-fact", "verdict": "judge_error"}) + "\n",
                json.dumps({"set": "dev", "id": "dev2", "type": "multi-fact", "verdict": "correct"}) + "\n",
            ]),
            encoding="utf-8",
        )
        # no explicit "set" field: the id has to identify the set
        (e2 / "answers.jsonl").write_text(
            "".join([
                json.dumps({"id": "held1", "answer": "saved answer four", "latency": 3.0, "error": None}) + "\n",
                json.dumps({"id": "held2", "answer": "saved answer five", "latency": 4.0, "error": None}) + "\n",
                json.dumps({"id": "zz9", "answer": "answer with no probe item", "latency": 5.0, "error": None}) + "\n",
            ]),
            encoding="utf-8",
        )
        (e1 / "summary.json").write_text(json.dumps({"reingest": True, "env": {"CHUNK_SIZE": "1200"}}), encoding="utf-8")

        self.stub = _StubServer()
        self.stub.routes[("POST", "/v1/chat/completions")] = (
            200,
            {"choices": [{"message": {"content": '{"verdict": "wrong", "reason": "invented detail"}'},
                          "finish_reason": "stop"}]},
        )
        self.stub.start()
        self.addCleanup(self.stub.stop)
        self.url_backup = ol.JUDGE_URL
        ol.JUDGE_URL = f"{self.stub.base}/v1/chat/completions"
        self.addCleanup(setattr, ol, "JUDGE_URL", self.url_backup)
        self.ops = ol.Ops()

    def _read_verdicts(self, name):
        text = (self.out / name / "verdicts.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def test_rejudge_scores_keeps_good_verdicts_and_survives_a_torn_line(self):
        printed: list[str] = []
        code = ol.rejudge(self.out, self.root, ops=self.ops, print_fn=printed.append)
        self.assertEqual(code, 0)

        e1 = self._read_verdicts("E1")
        self.assertEqual([row["verdict"] for row in e1], ["wrong", "correct", ol.NO_ANSWER])
        self.assertEqual([row["id"] for row in e1], ["dev1", "dev2", "dev3"])
        e2 = self._read_verdicts("E2")
        self.assertEqual([row["verdict"] for row in e2], ["wrong", "wrong", ol.JUDGE_ERROR])
        # the unmatched id keeps an empty set label so the gap stays visible
        self.assertEqual([row["set"] for row in e2], ["heldout", "heldout", ""])

        summary = json.loads((self.out / "E1" / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["dev"]["correct_pct"], 50.0)   # dev1 wrong, dev2 correct
        self.assertEqual(summary["dev"]["judged"], 2)
        self.assertEqual(summary["dev"]["no_answers"], 1)
        self.assertEqual(summary["dev"]["judge_errors"], 0)
        self.assertEqual(summary["reingest"], True)             # unrelated keys preserved
        self.assertEqual(summary["env"], {"CHUNK_SIZE": "1200"})
        summary2 = json.loads((self.out / "E2" / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary2["heldout"]["n"], 2)          # the unmatched id is not scored
        self.assertEqual(summary2["heldout"]["judge_errors"], 0)
        self.assertEqual(summary2["unmatched"], 1)
        self.assertEqual(summary2["rejudged_counts"]["judged"], 2)

        # one judge call per re-graded answer: kept and no_answer cost nothing
        self.assertEqual(len(self.stub.calls("POST", "/v1/chat/completions")), 3)
        self.assertEqual({record["path"] for record in self.stub.records}, {"/v1/chat/completions"})

        table = (self.out / "results-rejudged.md").read_text(encoding="utf-8")
        self.assertIn(ol.RESULTS_TABLE_HEADER, table)
        rows = [line for line in table.splitlines() if line.startswith("| E")]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("rejudged" in line for line in rows))
        self.assertIn("1 answer(s) had no matching probe id", table)
        self.assertIn("1 unparsable line(s) in answers.jsonl", table)

        # counts only on stdout: no questions, answers or judge reasons
        joined = "\n".join(printed)
        for forbidden in ("Question one?", "saved answer", "invented detail", "secret-do-not-log"):
            self.assertNotIn(forbidden, joined)
        self.assertIn("judged 1 kept 1 no_answer 1 errors 0 unmatched 0 skipped_lines 1", joined)
        self.assertIn("judged 2 kept 0 no_answer 0 errors 0 unmatched 1 skipped_lines 0", joined)

        for path, before in self.untouched.items():
            self.assertEqual(path.read_bytes(), before, f"{path.name} was modified")

    def test_rejudge_is_idempotent_and_cheap_the_second_time(self):
        self.assertEqual(ol.rejudge(self.out, self.root, ops=self.ops, print_fn=lambda line: None), 0)
        calls_after_first = len(self.stub.records)
        printed: list[str] = []
        self.assertEqual(ol.rejudge(self.out, self.root, ops=self.ops, print_fn=printed.append), 0)
        self.assertEqual(len(self.stub.records), calls_after_first, "re-judging made new judge calls")
        joined = "\n".join(printed)
        self.assertIn("judged 0 kept 2 no_answer 1", joined)  # E1: wrong + correct kept
        self.assertIn("judged 0 kept 2 no_answer 0", joined)  # E2: both wrong kept
        self.assertEqual([row["verdict"] for row in self._read_verdicts("E1")],
                         ["wrong", "correct", ol.NO_ANSWER])

    def test_rejudge_refuses_without_the_probe_files(self):
        empty_root = Path(self.tmp.name) / "no-probes"
        empty_root.mkdir()
        printed: list[str] = []
        self.assertEqual(ol.rejudge(self.out, empty_root, ops=self.ops, print_fn=printed.append), 3)
        self.assertIn("refusing to re-judge", "\n".join(printed))
        self.assertFalse((self.out / "results-rejudged.md").exists())

    def test_rejudge_recomputes_ok_pct_from_saved_verdicts(self):
        """loop4 can be re-scored without re-running anything: ok% comes from the
        verdicts already on disk."""
        out = Path(self.tmp.name) / "ok-out"
        exp = out / "E9"
        exp.mkdir(parents=True)
        (exp / "answers.jsonl").write_text("".join([
            json.dumps({"set": "heldout", "id": "held1", "answer": "Delta",
                        "latency": 1.0, "error": None}) + "\n",
            json.dumps({"set": "heldout", "id": "held2", "answer": "nothing in the documents",
                        "latency": 1.0, "error": None}) + "\n",
        ]), encoding="utf-8")
        self.stub.routes[("POST", "/v1/chat/completions")] = (
            200,
            {"choices": [{"message": {"content": '{"verdict": "refused_ok", "reason": "rule 1"}'},
                          "finish_reason": "stop"}]},
        )
        printed: list[str] = []
        self.assertEqual(ol.rejudge(out, self.root, ops=self.ops, print_fn=printed.append), 0)
        summary = json.loads((exp / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["heldout"]["correct_pct"], 0.0)
        self.assertEqual(summary["heldout"]["ok_pct"], 100.0)
        self.assertEqual(summary["heldout"]["counts"]["refused_ok"], 2)
        self.assertEqual(summary["heldout"]["refusal_accuracy_pct"], 100.0)
        self.assertIn("rejudged", (out / "results-rejudged.md").read_text(encoding="utf-8"))

    def test_rejudge_on_an_empty_out_dir_is_not_an_error(self):
        empty_out = Path(self.tmp.name) / "empty-out"
        empty_out.mkdir()
        printed: list[str] = []
        self.assertEqual(ol.rejudge(empty_out, self.root, ops=self.ops, print_fn=printed.append), 0)
        self.assertIn("nothing to do", "\n".join(printed))

    def test_unreachable_judge_is_recorded_as_error_and_never_crashes(self):
        """The whole point of --rejudge is retrying saved judge_error rows, so a
        dead judge must degrade to counts, not a traceback."""
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        ol.JUDGE_URL = f"http://127.0.0.1:{port}/v1/chat/completions"
        printed: list[str] = []
        self.assertEqual(ol.rejudge(self.out, self.root, ops=self.ops, print_fn=printed.append), 0)
        self.assertEqual(
            [row["verdict"] for row in self._read_verdicts("E1")],
            [ol.JUDGE_ERROR, "correct", ol.NO_ANSWER],
        )
        self.assertIn("judged 0 kept 1 no_answer 1 errors 1 unmatched 0", "\n".join(printed))
        self.assertTrue((self.out / "results-rejudged.md").exists())

    def test_cli_wires_rejudge_without_experiments_or_out(self):
        with redirect_stdout(io.StringIO()) as captured:
            code = ol.main(["--root", str(self.root), "--rejudge", str(self.out)])
        self.assertEqual(code, 0)
        self.assertTrue((self.out / "results-rejudged.md").exists())
        self.assertNotIn("Question one?", captured.getvalue())

    def test_cli_still_demands_experiments_and_out_for_a_normal_run(self):
        with self.assertRaises(SystemExit) as raised:
            ol.main(["--root", str(self.root)])
        self.assertEqual(raised.exception.code, 2)

    def _v2_probe_files(self) -> None:
        probe = self.root / "probe"
        (probe / "solvay-dev-v2.jsonl").write_text(_jsonl([
            {"id": "dev1", "question": "Question one?", "expected_answer": "Alpha",
             "evidence": "evidence alpha", "type": "single-fact"},
        ]), encoding="utf-8")
        (probe / "solvay-heldout-v2.jsonl").write_text(_jsonl([
            {"id": "held1", "question": "Question four?", "expected_answer": "Delta",
             "evidence": "evidence delta", "type": "single-fact"},
            {"id": "held2", "question": "Question five?", "expected_answer": "not in the documents",
             "evidence": "", "type": "unanswerable"},
        ]), encoding="utf-8")

    def test_rejudge_grades_against_the_probe_files_it_is_handed(self):
        """dev2/dev3 exist only in the v1 file, so with v2 they are unmatched."""
        self._v2_probe_files()
        printed: list[str] = []
        code = ol.rejudge(
            self.out, self.root,
            dev_probe="probe/solvay-dev-v2.jsonl", heldout_probe="probe/solvay-heldout-v2.jsonl",
            ops=self.ops, print_fn=printed.append,
        )
        self.assertEqual(code, 0)
        joined = "\n".join(printed)
        self.assertIn(
            "probe files: dev=probe/solvay-dev-v2.jsonl heldout=probe/solvay-heldout-v2.jsonl", joined
        )
        self.assertIn("unmatched 2", joined)
        self.assertEqual([row["verdict"] for row in self._read_verdicts("E1")],
                         ["wrong", ol.JUDGE_ERROR, ol.JUDGE_ERROR])
        self.assertEqual([row["verdict"] for row in self._read_verdicts("E2")],
                         ["wrong", "wrong", ol.JUDGE_ERROR])

    def test_cli_passes_probe_names_to_rejudge(self):
        self._v2_probe_files()
        with redirect_stdout(io.StringIO()) as captured:
            code = ol.main([
                "--root", str(self.root), "--rejudge", str(self.out),
                "--dev-probes", "solvay-dev-v2.jsonl", "--heldout-probes", "solvay-heldout-v2.jsonl",
            ])
        self.assertEqual(code, 0)
        printed = captured.getvalue()
        self.assertIn("dev=probe/solvay-dev-v2.jsonl", printed)
        self.assertIn("unmatched 2", printed)
        self.assertNotIn("Question one?", printed)  # counts only, never question text
        self.assertTrue((self.out / "results-rejudged.md").exists())


# ── real LightRAG response shapes (packet-32) ────────────────────────────

REAL_STATUS_COUNTS_ZERO = {"status_counts": {
    "pending": 0, "parsing": 0, "analyzing": 0, "processing": 0,
    "preprocessed": 0, "processed": 0, "failed": 0, "all": 0,
}}
REAL_STATUS_COUNTS_BUSY = {"status_counts": {
    "pending": 0, "parsing": 0, "analyzing": 0, "processing": 0,
    "preprocessed": 0, "processed": 3, "failed": 0, "all": 3,
}}
REAL_TRACK_PROCESSED = {
    "track_id": "tr1",
    "documents": [{"id": "doc-1", "status": "processed", "file_path": "/data/x.txt",
                   "chunks_count": 2, "error_msg": None}],
    "total_count": 1,
    "status_summary": {"processed": 1},
}


class TestStatusCountsShape(unittest.TestCase):
    """v1.5.7 nests the counters under status_counts; the invented flat shape made
    the driver decide the store was never empty and time the clear out."""

    def test_real_shape_with_everything_zero_is_zero(self):
        self.assertTrue(ol.status_counts_are_zero(REAL_STATUS_COUNTS_ZERO))

    def test_real_shape_with_documents_is_not_zero(self):
        self.assertFalse(ol.status_counts_are_zero(REAL_STATUS_COUNTS_BUSY))

    def test_all_counter_wins_over_the_other_fields(self):
        self.assertTrue(ol.status_counts_are_zero({"status_counts": {"all": 0, "failed": 5}}))
        self.assertFalse(ol.status_counts_are_zero({"status_counts": {"all": 2, "failed": 0}}))

    def test_older_shapes_are_still_read(self):
        self.assertTrue(ol.status_counts_are_zero({"counts": {"pending": 0, "processed": 0}}))
        self.assertTrue(ol.status_counts_are_zero({"pending": 0, "processing": 0, "processed": 0}))

    def test_no_numbers_at_all_is_not_zero(self):
        for payload in ({}, {"status_counts": {}}, {"error": "nope"}, None, [], "counts"):
            self.assertFalse(ol.status_counts_are_zero(payload), msg=str(payload))

    def test_booleans_are_not_counters(self):
        self.assertFalse(ol.status_counts_are_zero({"status_counts": {"ok": True, "ready": True}}))

    def test_status_counts_of_prefers_the_nested_key(self):
        self.assertEqual(
            ol.status_counts_of({"status_counts": {"all": 1}, "counts": {"all": 9}}), {"all": 1}
        )

    def test_emit_ignores_a_dead_pipe(self):
        def dead_pipe(line):
            raise BrokenPipeError(32, "Broken pipe")

        def dead_io(line):
            raise OSError(5, "I/O error")

        for print_fn in (dead_pipe, dead_io, None):
            ol.emit(print_fn, "a line that must not raise")


class TestClearAndTrackAgainstRealShapes(_E2EFixture, unittest.TestCase):
    def test_nonzero_counts_keep_waiting_and_time_the_clear_out(self):
        self.stub.routes[("GET", "/documents/status_counts")] = (200, REAL_STATUS_COUNTS_BUSY)
        timeout_backup = ol.CLEAR_TIMEOUT
        ol.CLEAR_TIMEOUT = 0.05
        self.addCleanup(setattr, ol, "CLEAR_TIMEOUT", timeout_backup)

        config = ol.load_experiments(self._config_path())
        loop = ol.Loop(root=self.root, out=self.out, config=config, ops=_FastOps(), max_hours=1.0,
                       handle_signals=False, print_fn=lambda line: None)
        self.assertEqual(loop.run(), 0)

        state = json.loads((self.out / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["experiments"]["E1-reingest"]["status"], "error")
        self.assertIn("timeout waiting for status_counts to reach 0", state["experiments"]["E1-reingest"]["error"])
        self.assertIsNone(state["corpus_built_by"])  # nothing was inserted on top of a busy store
        # the clear itself was still issued, and the probe sets were never asked
        self.assertTrue(self.stub.calls("DELETE", "/documents"))
        self.assertEqual(len(self.stub.calls("POST", "/api/chat")), 5)  # E2-probes only

    def test_empty_documents_list_is_not_yet_processed(self):
        seen = {"calls": 0}

        def route(_record):
            seen["calls"] += 1
            if seen["calls"] < 3:
                return 200, {"track_id": "tr1", "documents": [], "total_count": 0, "status_summary": {}}
            return 200, REAL_TRACK_PROCESSED

        self.stub.routes[("GET", "/documents/track_status/tr1")] = route
        config = ol.load_experiments(self._config_path())
        loop = ol.Loop(root=self.root, out=self.out, config=config, ops=_FastOps(), max_hours=1.0,
                       handle_signals=False, print_fn=lambda line: None)
        self.assertEqual(loop.run(), 0)

        state = json.loads((self.out / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["experiments"]["E1-reingest"]["status"], "done")
        self.assertEqual(state["corpus_built_by"], "E1-reingest")
        self.assertGreaterEqual(seen["calls"], 3)


class TestRetryErrors(_Case):
    """--retry-errors (with --resume) re-runs experiments that ended in error;
    done experiments stay skipped."""

    def _config(self):
        return self.write_experiments({
            "baseline_env": {},
            "experiments": [
                {"name": "E1-bad", "env": {"CHUNK_SIZE": "1000"}, "reingest": True},
                {"name": "E2-good", "env": {"CHUNK_SIZE": "1200"}, "reingest": False},
            ],
        })

    def test_failed_experiment_is_rerun_and_the_good_one_is_skipped(self):
        config = self._config()
        self.ops.delete_status = 500
        self.make_loop(config).run()
        state = self.state()
        self.assertEqual(state["experiments"]["E1-bad"]["status"], "error")
        self.assertEqual(state["experiments"]["E2-good"]["status"], "done")

        self.make_loop(config, resume=True).run()
        log = (self.out / "loop.log").read_text(encoding="utf-8")
        self.assertIn("E1-bad: already error in state.json; skipping (--resume)", log)

        self.ops.delete_status = 200
        loop = ol.Loop(root=self.root, out=self.out, config=config, ops=self.ops, max_hours=11.0,
                       resume=True, retry_errors=True, handle_signals=False, print_fn=lambda line: None)
        loop.run()

        state = self.state()
        self.assertEqual(state["experiments"]["E1-bad"]["status"], "done")
        self.assertEqual(state["experiments"]["E2-good"]["status"], "done")
        log = (self.out / "loop.log").read_text(encoding="utf-8")
        self.assertIn("E1-bad: previous status was error; running it again (--retry-errors)", log)
        self.assertIn("E2-good: already done in state.json; skipping (--resume)", log)

    def test_without_the_flag_an_error_stays_skipped(self):
        config = self._config()
        self.ops.delete_status = 500
        self.make_loop(config).run()
        clears_before = len(self.ops.http_calls)
        self.make_loop(config, resume=True).run()
        self.assertEqual(len([call for call in self.ops.http_calls[clears_before:] if call[0] == "DELETE"]), 0)


class TestBrokenPipeFinalise(_E2EFixture, unittest.TestCase):
    """Ctrl-C in a `| tee` pipeline kills tee; the next stdout write raises
    BrokenPipeError. That must never abort finalise."""

    def test_finalise_still_restores_env_and_recreates_the_container(self):
        def dead_pipe(line):
            raise BrokenPipeError(32, "Broken pipe")

        config = ol.load_experiments(self._config_path())
        loop = ol.Loop(root=self.root, out=self.out, config=config, ops=_FastOps(), max_hours=1.0,
                       handle_signals=False, print_fn=dead_pipe)
        self.assertEqual(loop.run(), 0)

        self.assertEqual((self.root / ".env").read_text(encoding="utf-8"), E2E_ENV)
        calls = self.docker_log.read_text(encoding="utf-8").splitlines()
        ups = [line for line in calls if line.startswith("compose -f") and "up -d --no-deps lightrag-server" in line]
        self.assertEqual(len(ups), 3)  # E1, E2 and the finalise recreate
        self.assertTrue(any(line == "ps --format {{.Names}}" for line in calls))

        # the file log is unaffected: losing stdout must not lose the record
        lines = (self.out / "loop.log").read_text(encoding="utf-8").splitlines()
        self.assertGreater(len(lines), 10)
        self.assertIn("env restored", "\n".join(lines))
        state = json.loads((self.out / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "complete")


# ── packet-33: failure isolation, fail-fast start, gateway env ────────


class TestEntityTypesRefused(unittest.TestCase):
    """LightRAG v1.5.7 refuses to start with ENTITY_TYPES, so the driver must
    refuse the config instead of paying 5 minutes per experiment for it."""

    def test_entity_types_is_out_of_the_whitelist_and_forbidden(self):
        self.assertNotIn("ENTITY_TYPES", ol.ENV_WHITELIST)
        self.assertIn("ENTITY_TYPES", ol.NEVER_CHANGE_EXACT)

    def test_check_env_keys_names_the_real_reason(self):
        with self.assertRaises(ol.LoopConfigError) as caught:
            ol.check_env_keys({"ENTITY_TYPES": "Person, Organization"}, "experiment E3")
        self.assertEqual(
            str(caught.exception),
            "experiment E3: ENTITY_TYPES is not supported by LightRAG v1.5.7 (server refuses to start)",
        )

    def test_experiments_file_using_it_is_refused_at_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "experiments.json"
            path.write_text(
                json.dumps({
                    "baseline_env": {},
                    "experiments": [{"name": "E3", "env": {"ENTITY_TYPES": "Person, Organization"}}],
                }),
                encoding="utf-8",
            )
            with self.assertRaises(ol.LoopConfigError) as caught:
                ol.load_experiments(path)
            self.assertIn(
                "ENTITY_TYPES is not supported by LightRAG v1.5.7 (server refuses to start)",
                str(caught.exception),
            )

    def test_cli_refuses_such_a_config_before_touching_anything(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "experiments.json"
            path.write_text(
                json.dumps({"baseline_env": {}, "experiments": [{"name": "E3", "env": {"ENTITY_TYPES": "x"}}]}),
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = ol.main(["--root", tmp, "--experiments", str(path), "--out", f"{tmp}/loop", "--dry-run"])
            self.assertEqual(code, 3)
            self.assertIn("server refuses to start", stderr.getvalue())
            self.assertFalse((Path(tmp) / "loop").exists())


class TestGatewayEnvAllowList(unittest.TestCase):
    def test_only_the_gateway_allow_list_is_accepted(self):
        for key in sorted(ol.GATEWAY_ENV_WHITELIST):
            ol.check_env_keys({key: "1"}, "experiment G1", allow=ol.GATEWAY_ENV_WHITELIST)
        self.assertEqual(
            set(ol.GATEWAY_ENV_WHITELIST),
            {
                "BRIDGE_TOP_K", "BRIDGE_CHUNK_TOP_K", "BRIDGE_MAX_ENTITY_TOKENS",
                "BRIDGE_MAX_RELATION_TOKENS", "BRIDGE_MAX_TOTAL_TOKENS", "BRIDGE_ENABLE_RERANK",
                "GATEWAY_CHUNK_TOP_K", "CITATION_TOP_K",
            },
        )
        self.assertFalse(set(ol.GATEWAY_ENV_WHITELIST) & set(ol.ENV_WHITELIST))

    def test_a_lightrag_key_in_gateway_env_is_refused(self):
        with self.assertRaises(ol.LoopConfigError) as caught:
            ol.check_env_keys({"CHUNK_SIZE": "1"}, "experiment G1", allow=ol.GATEWAY_ENV_WHITELIST)
        self.assertIn("not in the whitelist", str(caught.exception))

    def test_load_refuses_a_gateway_key_that_is_not_allow_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "experiments.json"
            path.write_text(
                json.dumps({"baseline_env": {}, "experiments": [{"name": "G1", "env": {}, "gateway_env": {"MAX_ASYNC": "4"}}]}),
                encoding="utf-8",
            )
            with self.assertRaises(ol.LoopConfigError) as caught:
                ol.load_experiments(path)
            self.assertIn("experiment G1: key MAX_ASYNC is not in the whitelist", str(caught.exception))

    def test_load_refuses_a_credential_in_gateway_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "experiments.json"
            path.write_text(
                json.dumps({"baseline_env": {}, "experiments": [{"name": "G1", "gateway_env": {"BRIDGE_API_KEY": "x"}}]}),
                encoding="utf-8",
            )
            with self.assertRaises(ol.LoopConfigError):
                ol.load_experiments(path)

    def test_plan_lines_show_gateway_env(self):
        config = {
            "experiments": [{
                "name": "G1", "env": {"CHUNK_SIZE": "800"}, "gateway_env": {"BRIDGE_TOP_K": "8"},
                "reingest": False, "probe_concurrency": 1,
            }]
        }
        line = "\n".join(ol.build_plan_lines(config))
        self.assertIn("env[CHUNK_SIZE=800]", line)
        self.assertIn("gateway_env[BRIDGE_TOP_K=8]", line)


class TestSecretMasking(unittest.TestCase):
    def test_key_token_secret_and_password_values_are_masked(self):
        masked = ol.mask_secrets(
            "ERROR LIGHTRAG_API_KEY=abc123 OPENAI_TOKEN=tok CLIENT_SECRET=s33t DB_PASSWORD=pw"
        )
        for secret in ("abc123", "tok", "s33t", "pw"):
            self.assertNotIn(secret, masked)
        self.assertIn("LIGHTRAG_API_KEY=<masked>", masked)
        self.assertIn("DB_PASSWORD=<masked>", masked)

    def test_an_ordinary_log_line_is_left_alone(self):
        text = "ERROR: ENTITY_TYPES environment variable is no longer supported"
        self.assertEqual(ol.mask_secrets(text), text)


class TestFailureIsolation(_E2EFixture, unittest.TestCase):
    """Real Ops against the fake docker on PATH: a container that refuses to start
    must cost seconds, and the next experiment must start on a restored baseline."""

    def _run(self, experiments: list[dict], baseline_env: dict | None = None) -> tuple[int, dict, float, str]:
        path = Path(self.tmp.name) / "experiments.json"
        path.write_text(
            json.dumps({"baseline_env": baseline_env or {}, "experiments": experiments}), encoding="utf-8"
        )
        config = ol.load_experiments(path)
        loop = ol.Loop(
            root=self.root, out=self.out, config=config, ops=_FastOps(), max_hours=1.0,
            handle_signals=False, print_fn=lambda line: None,
        )
        started = time.monotonic()
        code = loop.run()
        elapsed = time.monotonic() - started
        log = (self.out / "loop.log").read_text(encoding="utf-8")
        state = json.loads((self.out / "state.json").read_text(encoding="utf-8"))
        return code, state, elapsed, log

    def docker_calls(self) -> list[str]:
        return self.docker_log.read_text(encoding="utf-8").splitlines() if self.docker_log.exists() else []

    def test_exited_container_fails_in_seconds_and_the_next_experiment_gets_a_healthy_baseline(self):
        (self.docker_dir / "logs-lightrag-server").write_text(CONTAINER_LOG_WITH_ERROR, encoding="utf-8")
        code, state, elapsed, log = self._run([
            {"name": "E1-poison", "env": {"CHUNK_SIZE": "666"}, "reingest": False},
            {"name": "E2-after", "env": {"CHUNK_SIZE": "900"}, "reingest": False},
        ])
        # HEALTH_TIMEOUT is 300 s; a run that noticed the exit should never come close
        self.assertLess(elapsed, 30.0)
        self.assertEqual(state["experiments"]["E1-poison"]["status"], "error")
        first_error = state["experiments"]["E1-poison"]["error"]
        self.assertIn("lightrag-server is exited", first_error)
        self.assertIn("no longer supported", first_error)
        self.assertIn("Traceback", first_error)
        self.assertNotIn("timeout waiting for", log)
        self.assertTrue(any("ps -a --filter name=lightrag-server --format {{.State}}" in call for call in self.docker_calls()))

        # the recovery step ran and put the original CHUNK_SIZE back before E2
        self.assertIn("baseline recovery before the next experiment", log)
        self.assertIn("baseline: env set to CHUNK_SIZE=1200", log)
        self.assertIn("baseline recovery done", log)
        self.assertEqual(state["experiments"]["E2-after"]["status"], "done")
        self.assertEqual(state["status"], "complete")
        # finalise put production .env back exactly as it was
        self.assertEqual((self.root / ".env").read_text(encoding="utf-8"), E2E_ENV)
        # the container log leaked an API key; neither artifact may repeat it
        state_text = (self.out / "state.json").read_text(encoding="utf-8")
        for blob in (state_text, log):
            self.assertNotIn("super-secret-value-do-not-log", blob)
        self.assertIn("LIGHTRAG_API_KEY=<masked>", log)
        self.assertIn("LIGHTRAG_API_KEY=<masked>", state_text)

    def test_a_container_stuck_restarting_fails_after_two_polls(self):
        (self.docker_dir / "always-restarting").write_text("", encoding="utf-8")
        (self.docker_dir / "logs-lightrag-server").write_text(
            "ERROR: ENTITY_TYPES environment variable is no longer supported\n", encoding="utf-8"
        )
        # /health answers only while the container is not restarting, like a real
        # crash-loop: the guard must see `restarting` twice in a row
        def health(_record):
            state_file = self.docker_dir / "state-lightrag-server"
            if state_file.exists() and "restarting" in state_file.read_text(encoding="utf-8"):
                return 503, {"status": "not ready"}
            return 200, {"status": "healthy"}

        self.stub.routes[("GET", "/health")] = health
        code, state, elapsed, log = self._run([
            {"name": "E1", "env": {"MAX_ASYNC": "2"}, "reingest": False},
            {"name": "E2", "env": {}, "reingest": False},
        ])
        self.assertLess(elapsed, 30.0)
        self.assertEqual(code, 1)
        self.assertEqual(state["experiments"]["E1"]["status"], "error")
        self.assertIn("kept restarting", state["experiments"]["E1"]["error"])
        self.assertIn("no longer supported", state["experiments"]["E1"]["error"])
        # recovery cannot bring it back either, so the run stops as fatal
        self.assertEqual(state["status"], "fatal")
        self.assertIn("cannot restore the baseline lightrag-server", state["stop_reason"])
        self.assertNotIn("E2", state["experiments"])

    def test_when_the_baseline_cannot_be_recovered_the_run_is_fatal_but_finalise_still_runs(self):
        (self.docker_dir / "always-exit").write_text("", encoding="utf-8")
        (self.docker_dir / "logs-lightrag-server").write_text(CONTAINER_LOG_WITH_ERROR, encoding="utf-8")
        code, state, elapsed, log = self._run([
            {"name": "E1", "env": {"MAX_ASYNC": "2"}, "reingest": False},
            {"name": "E2", "env": {}, "reingest": False},
        ])
        self.assertEqual(code, 1)
        self.assertEqual(state["status"], "fatal")
        self.assertIn("cannot restore the baseline lightrag-server", state["stop_reason"])
        self.assertIn("no longer supported", state["stop_reason"])
        self.assertNotIn("E2", state["experiments"])  # never started on a dead server
        self.assertNotIn("super-secret-value-do-not-log", (self.out / "state.json").read_text(encoding="utf-8"))
        self.assertIn("LIGHTRAG_API_KEY=<masked>", state["stop_reason"])
        # finalise still restored .env even though restoring the server failed
        self.assertEqual((self.root / ".env").read_text(encoding="utf-8"), E2E_ENV)
        self.assertIn("FINALISE FAILED", log)


class TestGatewayEnvExperiments(_E2EFixture, unittest.TestCase):
    def _run(self, experiments: list[dict]) -> tuple[dict, str]:
        path = Path(self.tmp.name) / "experiments.json"
        path.write_text(json.dumps({"baseline_env": {}, "experiments": experiments}), encoding="utf-8")
        config = ol.load_experiments(path)
        loop = ol.Loop(
            root=self.root, out=self.out, config=config, ops=_FastOps(), max_hours=1.0,
            handle_signals=False, print_fn=lambda line: None,
        )
        self.assertEqual(loop.run(), 0)
        state = json.loads((self.out / "state.json").read_text(encoding="utf-8"))
        return state, (self.out / "loop.log").read_text(encoding="utf-8")

    def test_gateway_keys_are_written_recreated_verified_and_restored(self):
        state, log = self._run([{
            "name": "G1", "env": {"MAX_ASYNC": "2"},
            "gateway_env": {"BRIDGE_TOP_K": "8", "CITATION_TOP_K": "20"}, "reingest": False,
        }])
        self.assertEqual(state["experiments"]["G1"]["status"], "done")
        self.assertIn("G1: env set to BRIDGE_TOP_K=8, CITATION_TOP_K=20, MAX_ASYNC=2", log)
        calls = self.docker_log.read_text(encoding="utf-8").splitlines()
        self.assertTrue(
            any("compose" in call and "up -d --no-deps gateway-api" in call for call in calls),
            f"gateway-api was never recreated: {calls}",
        )
        # the keys are proven against the gateway container, not the LightRAG one
        self.assertIn("rag-gateway-api: env verified for 2 key(s)", log)
        self.assertIn("rag-gateway-api: up", log)
        # finalise recreated the gateway too, and restored both env files
        self.assertEqual(len([call for call in calls if "up -d --no-deps gateway-api" in call]), 2)
        self.assertEqual((self.root / ".env").read_text(encoding="utf-8"), E2E_ENV)
        self.assertIn("rag-gateway-api: recreated", log)

    def test_gateway_keys_are_verified_against_the_gateway_container(self):
        (self.docker_dir / "exec-empty-gateway").write_text("", encoding="utf-8")
        path = Path(self.tmp.name) / "experiments.json"
        path.write_text(
            json.dumps({"baseline_env": {}, "experiments": [
                {"name": "G1", "env": {}, "gateway_env": {"BRIDGE_TOP_K": "8"}, "reingest": False},
            ]}),
            encoding="utf-8",
        )
        loop = ol.Loop(
            root=self.root, out=self.out, config=ol.load_experiments(path), ops=_FastOps(), max_hours=1.0,
            handle_signals=False, print_fn=lambda line: None,
        )
        loop.run()
        state = json.loads((self.out / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["experiments"]["G1"]["status"], "error")
        self.assertIn("rag-gateway-api env not applied", state["experiments"]["G1"]["error"])
        self.assertIn("BRIDGE_TOP_K: wanted 8 got None", state["experiments"]["G1"]["error"])

    def test_a_gateway_failure_recovers_both_containers_before_the_next_experiment(self):
        (self.docker_dir / "exec-empty-gateway").write_text("", encoding="utf-8")
        state, log = self._run([
            {"name": "G1-bad", "env": {}, "gateway_env": {"BRIDGE_TOP_K": "8"}, "reingest": False},
            {"name": "E2-good", "env": {"MAX_ASYNC": "4"}, "reingest": False},
        ])
        self.assertEqual(state["experiments"]["G1-bad"]["status"], "error")
        self.assertEqual(state["experiments"]["E2-good"]["status"], "done")
        self.assertIn("baseline recovery done", log)
        self.assertEqual((self.root / ".env").read_text(encoding="utf-8"), E2E_ENV)


# ── judge rubric v2: refusals, paraphrase, attribution (packet-34) ─────


class TestJudgeRubricV2(unittest.TestCase):
    """The rubric text itself is the contract.

    A failure analysis of 165 graded answers found 2 of 9 "failures" were the
    judge being wrong: a correct refusal graded wrong, and a clean refusal graded
    hallucinated because it named a real-but-adjacent entity. Those rules are
    asserted here as text, because if they quietly leave the prompt the scores
    move for the wrong reason and no other test notices.
    """

    def setUp(self):
        self.item = {
            "id": "dev01",
            "question": "Which retrieval system was chosen?",
            "expected_answer": "LightRAG",
            "evidence": "we settled on LightRAG for retrieval",
        }
        self.flat = " ".join(ol.JUDGE_PROMPT.split())

    def test_rule_1_clean_refusals_and_the_hallucination_boundary(self):
        for phrase in (
            'the expected answer is "not in the documents"',
            "a clear statement that the information is not available or not found in the provided context is refused_ok",
            "EVEN IF the answer names real entities or mentions nearby facts",
            "as long as it does not assert the requested fact itself",
            "hallucinated ONLY if the answer asserts a specific value, name, date or decision",
            "is not supported by the evidence or the expected answer",
        ):
            self.assertIn(phrase, self.flat, f"rule 1 lost: {phrase!r}")

    def test_rule_2_paraphrase_tolerance_and_the_answerable_ladder(self):
        for phrase in (
            "accept paraphrase, different wording, extra correct detail and a different order of the facts",
            "correct = every key fact in the expected answer is present and nothing contradicts the evidence",
            "partial = some key facts are present and none contradict",
            "wrong = key facts are missing or contradicted",
            'refusal ("not in the documents") on an answerable question is refused_wrong',
            "unless the question is itself unanswerable from the evidence (then refused_ok)",
        ):
            self.assertIn(phrase, self.flat, f"rule 2 lost: {phrase!r}")

    def test_rule_3_speaker_attribution_is_not_a_failure_by_itself(self):
        for phrase in (
            "do not mark an answer wrong merely because it attributes a statement to a different speaker label",
            "or omits who said it",
            "unless the question explicitly asks who said it",
        ):
            self.assertIn(phrase, self.flat, f"rule 3 lost: {phrase!r}")

    def test_rule_4_evidence_only_and_harmless_extra_detail(self):
        for phrase in (
            "Judge only against the evidence quote and the expected answer given, never against outside knowledge",
            "Extra detail that is not in the evidence is not an error unless it contradicts the evidence",
        ):
            self.assertIn(phrase, self.flat, f"rule 4 lost: {phrase!r}")

    def test_rule_5_reason_is_one_sentence_and_names_the_rule(self):
        for phrase in (
            '"reason" must be ONE sentence and must name which rule decided it',
            "name which rule decided it",
            '"rule1 clean refusal"',
        ):
            self.assertIn(phrase, self.flat, f"rule 5 lost: {phrase!r}")

    def test_output_contract_and_six_verdict_words_survive(self):
        formatted = ol.judge_prompt_for(self.item, "they picked LightRAG")
        self.assertIn(
            '{"verdict": "correct|partial|wrong|hallucinated|refused_ok|refused_wrong", '
            '"reason": "<one sentence naming the deciding rule>"}',
            formatted,
        )
        self.assertIn("Reply with ONLY this JSON object and nothing else:", formatted)
        for verdict in ol.VERDICT_VALUES:
            self.assertIn(verdict, ol.JUDGE_PROMPT)
        self.assertEqual(len(ol.VERDICT_VALUES), 6)
        # the five rules are numbered in order, so the reason can cite one
        numbered = [line[:2] for line in formatted.splitlines() if line[:2] in ("1.", "2.", "3.", "4.", "5.")]
        self.assertEqual(numbered, ["1.", "2.", "3.", "4.", "5."])

    def test_prompt_stays_small_enough_for_a_reasoning_judge(self):
        # 318 words of rubric; a bloated prompt eats the same 4000-token budget the
        # chain of thought needs
        self.assertLess(len(ol.JUDGE_PROMPT), 3000)

    def test_only_the_four_known_placeholders_are_used(self):
        import string

        names = {name for _text, name, _spec, _conv in string.Formatter().parse(ol.JUDGE_PROMPT) if name}
        self.assertEqual(names, {"question", "expected_answer", "evidence", "answer"})

    def test_placeholders_are_filled_and_content_braces_are_inert(self):
        item = {
            "id": "dev02",
            "question": "What did they say about {the budget} and {it}?",
            "expected_answer": "{2} million",
            "evidence": "budget was {2} million",
        }
        prompt = ol.judge_prompt_for(item, "It was {2} million, per {the deck}.")
        for text in ("{the budget}", "{it}", "{2} million", "{the deck}"):
            self.assertIn(text, prompt)
        for placeholder in ("{question}", "{expected_answer}", "{evidence}", "{answer}"):
            self.assertNotIn(placeholder, prompt)

    def test_verdict_parses_with_braces_in_the_reply(self):
        reply = "thinking about {several} candidates ... {\"verdict\": \"refused_ok\", \"reason\": \"rule1 clean refusal\"}"
        parsed = ol.parse_verdict(reply, prefer_last=True)
        self.assertEqual(parsed, {"verdict": "refused_ok", "reason": "rule1 clean refusal"})

    def test_the_rubric_is_what_actually_reaches_the_judge(self):
        class _RecordingOps(ol.Ops):
            def http_json(self, method, url, *, headers=None, payload=None, timeout=30.0):
                self.sent = payload
                return 200, {"choices": [{
                    "message": {"content": '{"verdict": "refused_ok", "reason": "rule1 clean refusal"}'},
                    "finish_reason": "stop",
                }]}

        ops = _RecordingOps()
        out = ol.judge_answer(
            ops,
            {"id": "dev03", "question": "When did the board meet?", "expected_answer": "not in the documents", "evidence": ""},
            "The provided context does not say when the board met; it does mention the pilot.",
            "key",
        )
        self.assertEqual(out["verdict"], "refused_ok")
        self.assertEqual(out["reason"], "rule1 clean refusal")
        sent = " ".join(ops.sent["messages"][0]["content"].split())
        self.assertIn("EVEN IF the answer names real entities or mentions nearby facts", sent)
        self.assertIn("hallucinated ONLY if the answer asserts a specific value", sent)
        self.assertEqual(ops.sent["max_tokens"], ol.JUDGE_MAX_TOKENS)


if __name__ == "__main__":
    unittest.main()
