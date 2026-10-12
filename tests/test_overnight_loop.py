"""Offline tests for scripts/overnight_loop.py (packet-28).

No network, no docker, no sleeping: every side effect goes through a FakeOps
that answers scripted HTTP, records docker commands and advances a fake clock.
The filesystem is a real temp directory because the atomic-write and
byte-preservation behaviour is exactly what is under test.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
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


if __name__ == "__main__":
    unittest.main()
