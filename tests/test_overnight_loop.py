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
            return 200, {"counts": {"pending": 0, "processing": 0, "preprocessed": 0, "processed": 0, "failed": 0}}
        if url.endswith("/documents/text"):
            return 200, {"track_id": "track-1"}
        if "/documents/track_status/" in url:
            return 200, {"documents": [{"status": "processed"}]}
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
        good = {"correct_pct": 90.0, "hallucination_pct": 4.9, "refusal_accuracy_pct": 80.0}
        self.assertTrue(ol.meets_success(good))
        self.assertFalse(ol.meets_success({**good, "correct_pct": 89.9}))
        self.assertFalse(ol.meets_success({**good, "hallucination_pct": 5.0}))
        self.assertFalse(ol.meets_success({**good, "refusal_accuracy_pct": 79.9}))
        self.assertFalse(ol.meets_success({**good, "refusal_accuracy_pct": None}))


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
        self.ops.fail_compose_calls = {1, 2, 3, 4, 5}
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
        names: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
            elif isinstance(node, ast.TypeAlias):
                names.add(node.name.id)
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
# Records its arguments, then answers the three subcommands overnight_loop uses.
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  ps)   echo "lightrag-server"; echo "rag-gateway-api"; exit 0;;
  compose) exit 0;;
  exec)
    if [ "$2" = "lightrag-server" ] && [ "$3" = "env" ]; then
      grep -E '^[A-Z_][A-Z0-9_]*=' "$FAKE_DOCKER_ENV_FILE"
      exit 0
    fi
    exit 1;;
esac
exit 0
"""

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

        # fake docker, first on PATH
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(FAKE_DOCKER, encoding="utf-8")
        docker.chmod(0o755)
        self.docker_log = bin_dir / "docker.calls"
        self.environ_backup = dict(os.environ)
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        os.environ["FAKE_DOCKER_LOG"] = str(self.docker_log)
        os.environ["FAKE_DOCKER_ENV_FILE"] = str(self.root / ".env")
        self.addCleanup(self._restore_env)

        self.stub = _StubServer()
        self.stub.routes = {
            ("GET", "/health"): (200, {"status": "healthy"}),
            ("DELETE", "/documents"): (200, {"status": "cleared"}),
            ("GET", "/documents/status_counts"): (200, {"counts": {"processing": 0, "pending": 0, "processed": 0}}),
            ("POST", "/documents/text"): (200, {"track_id": "tr1"}),
            ("GET", "/documents/track_status/tr1"): (200, {"documents": [{"status": "processed"}]}),
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


if __name__ == "__main__":
    unittest.main()
