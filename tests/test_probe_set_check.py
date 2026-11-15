"""Tests for scripts/probe_set_check.py (GL-13 probe-set validator).

All fixtures are invented; nothing here knows about the real corpus.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_set_check.py"
SPEC = importlib.util.spec_from_file_location("probe_set_check", SCRIPT)
assert SPEC and SPEC.loader
psc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(psc)

SOURCE_TEXT = (
    "(Speaker 0) The plant went live in March after three months of hypercare.\n"
    "(You) And the interface queue is monitored by two people.\n"
    "(Speaker 0) Budget approval came from the transformation office.\n"
)
SOURCE_NAME = "Alpha meeting transcript.txt"


def _question(ident="aa01", *, shape="fact", q_type="single-fact", question="When did the plant go live?",
              history=None, absent_terms=None):
    return {
        "id": ident, "question": question, "source_doc": SOURCE_NAME, "type": q_type,
        "shape": shape, "history": history or [], "evidence": "", "expected_answer": "",
        "absent_terms": absent_terms if absent_terms is not None else [],
    }


def _key(ident="aa01", *, shape="fact", evidence="The plant went live in March after three months of hypercare.",
         expected="March, with three months of hypercare.", phrases=("March", "hypercare"),
         location=None, difficulty="easy", q_type="single-fact"):
    return {
        "id": ident, "question": "", "source_doc": SOURCE_NAME, "type": q_type, "shape": shape,
        "evidence": evidence, "expected_answer": expected, "key_phrases": list(phrases),
        "location": location or {"char_start": SOURCE_TEXT.index("The plant went live"),
                                 "char_end": SOURCE_TEXT.index("hypercare.") + len("hypercare."),
                                 "turn": 1},
        "difficulty": difficulty, "history": [], "absent_terms": [],
    }


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _fixtures(tmp_path: Path, questions: list[dict], key: list[dict]) -> tuple[Path, Path, Path]:
    source_dir = tmp_path / "src"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / SOURCE_NAME).write_text(SOURCE_TEXT, encoding="utf-8")
    qpath = _write(tmp_path / "questions.jsonl", questions)
    kpath = _write(tmp_path / "answer-key.jsonl", key)
    return qpath, kpath, source_dir


def _run(qpath: Path, kpath: Path, source_dir: Path, capsys, *extra: str) -> tuple[int, dict]:
    code = psc.main([str(qpath), "--answer-key", str(kpath), "--source-dir", str(source_dir), *extra])
    return code, json.loads(capsys.readouterr().out)


def test_good_set_passes(tmp_path, capsys):
    qpath, kpath, source_dir = _fixtures(tmp_path, [_question()], [_key()])
    code, report = _run(qpath, kpath, source_dir, capsys)
    assert code == 0, report
    assert report["failures"] == 0
    assert report["questions"] == 1
    assert report["by_shape"] == {"fact": 1}
    assert report["sha256_questions"]


def test_non_verbatim_evidence_fails(tmp_path, capsys):
    qpath, kpath, source_dir = _fixtures(
        tmp_path, [_question()], [_key(evidence="The plant went live in April after three months of hypercare.")])
    code, report = _run(qpath, kpath, source_dir, capsys)
    assert code == 1
    assert any("not a verbatim substring" in p for p in report["problems"])


def test_answer_text_in_question_file_fails(tmp_path, capsys):
    row = _question()
    row["expected_answer"] = "March"
    qpath, kpath, source_dir = _fixtures(tmp_path, [row], [_key()])
    code, report = _run(qpath, kpath, source_dir, capsys)
    assert code == 1
    assert any("must be empty in the question file" in p for p in report["problems"])


def test_unanswerable_phrase_present_in_source_fails(tmp_path, capsys):
    row = _question("aa02", shape="unanswerable", q_type="unanswerable",
                    question="Who signs off the hypercare budget?",
                    absent_terms=["zephyr ledger", "quorum gate", "velar signoff"])
    key = _key("aa02", shape="unanswerable", evidence="",
               expected="not in the documents", phrases=(),
               location={"char_start": 0, "char_end": 0, "turn": 0})
    key["distinctive_phrase"] = "three months of hypercare"
    qpath, kpath, source_dir = _fixtures(tmp_path, [row], [key])
    code, report = _run(qpath, kpath, source_dir, capsys)
    assert code == 1
    assert any("distinctive_phrase occurs in a source" in p for p in report["problems"])


def test_unanswerable_absent_terms_enforced(tmp_path, capsys):
    row = _question("aa02", shape="near-miss", q_type="unanswerable",
                    question="What did the CEO say about the March hypercare?",
                    absent_terms=["apricot rota"])
    key = _key("aa02", shape="near-miss", evidence="", expected="not in the documents", phrases=(),
               location={"char_start": 0, "char_end": 0, "turn": 0})
    key["distinctive_phrase"] = "apricot rota schedule"
    qpath, kpath, source_dir = _fixtures(tmp_path, [row], [key])
    code, report = _run(qpath, kpath, source_dir, capsys)
    assert code == 1
    assert any("3-6 absent_terms" in p for p in report["problems"])


def test_key_phrase_must_exist_in_source(tmp_path, capsys):
    qpath, kpath, source_dir = _fixtures(tmp_path, [_question()], [_key(phrases=("March", "unicorne"))])
    code, report = _run(qpath, kpath, source_dir, capsys)
    assert code == 1
    assert any("not inside the evidence span" in p for p in report["problems"])


def test_location_span_must_match_evidence(tmp_path, capsys):
    qpath, kpath, source_dir = _fixtures(
        tmp_path, [_question()],
        [_key(location={"char_start": 0, "char_end": 20, "turn": 1})])
    code, report = _run(qpath, kpath, source_dir, capsys)
    assert code == 1
    assert any("does not match the located span" in p for p in report["problems"])


def test_history_rules(tmp_path, capsys):
    flat = _question("aa03", q_type="follow-up", question="And for how long?")
    qpath, kpath, source_dir = _fixtures(tmp_path, [flat], [_key("aa03", q_type="follow-up")])
    code, report = _run(qpath, kpath, source_dir, capsys)
    assert code == 1 and any("non-empty history" in p for p in report["problems"])

    carried = _question("aa04", q_type="single-fact", question="And for how long?",
                        history=[{"role": "user", "content": "When did it go live?"}])
    qpath, kpath, source_dir = _fixtures(tmp_path / "b", [carried], [_key("aa04", q_type="single-fact")])
    code, report = _run(qpath, kpath, source_dir, capsys)
    assert code == 1 and any("only follow-up rows may carry history" in p for p in report["problems"])


def test_overlap_against_earlier_set_is_counted(tmp_path, capsys):
    qpath, kpath, source_dir = _fixtures(tmp_path, [_question()], [_key()])
    earlier = _write(tmp_path / "earlier.jsonl", [
        {"id": "zz99", "question": "When did the plant go live and for how long?",
         "source_doc": SOURCE_NAME, "type": "single-fact", "evidence": "The plant went live in March"}])
    code, report = _run(qpath, kpath, source_dir, capsys, "--forbid-overlap-with", str(earlier),
                        "--overlap-limit", "20")
    assert code == 1
    assert report["overlap"]["max_question_shared_normalised_chars"] == len("when did the plant go live")
    assert any("shares a" in p for p in report["problems"])


def test_overlap_under_limit_passes(tmp_path, capsys):
    qpath, kpath, source_dir = _fixtures(tmp_path, [_question(question="Which month did the plant switch live?")],
                                         [_key()])
    earlier = _write(tmp_path / "earlier.jsonl", [
        {"id": "zz99", "question": "Who monitors the queue?", "source_doc": SOURCE_NAME,
         "type": "single-fact", "evidence": ""}])
    code, report = _run(qpath, kpath, source_dir, capsys, "--forbid-overlap-with", str(earlier),
                        "--overlap-limit", "120")
    assert code == 0, report
    assert report["overlap"]["max_question_shared_normalised_chars"] > 0


def test_id_collision_between_sets_fails_quietly_via_overlap(tmp_path, capsys):
    qpath, kpath, source_dir = _fixtures(tmp_path, [_question()], [_key()])
    code, report = _run(qpath, kpath, source_dir, capsys)
    assert code == 0
    # same id twice inside the new set is a hard failure
    dup_q, dup_k, src = _fixtures(tmp_path / "dup", [_question(), _question()], [_key(), _key()])
    code, report = _run(dup_q, dup_k, src, capsys)
    assert code == 1
    assert any("duplicate id" in p for p in report["problems"])


def test_missing_answer_key_row_fails(tmp_path, capsys):
    qpath, kpath, source_dir = _fixtures(tmp_path, [_question("aa01"), _question("aa02")], [_key("aa01")])
    code, report = _run(qpath, kpath, source_dir, capsys)
    assert code == 1
    assert any("absent from the answer key" in p for p in report["problems"])


def test_run_set_merge_fills_answers(tmp_path, capsys):
    questions = [_question(), _question("aa02", shape="near-miss", q_type="unanswerable",
                                        question="What did the CEO say about hypercare budget?",
                                        absent_terms=["orchid quota", "basalt rota", "kiln ledger"])]
    key = [_key(), _key("aa02", shape="near-miss", evidence="", expected="not in the documents",
                        phrases=(), location={"char_start": 0, "char_end": 0, "turn": 0})]
    key[1]["distinctive_phrase"] = "the CEO quoted a quarterly dividend"
    qpath, kpath, source_dir = _fixtures(tmp_path, questions, key)
    run_set = tmp_path / "run.jsonl"
    code, report = _run(qpath, kpath, source_dir, capsys, "--run-set", str(run_set))
    assert code == 0, report
    rows = psc.read_jsonl(run_set)
    assert rows[0]["evidence"].startswith("The plant went live")
    assert rows[1]["expected_answer"] == "not in the documents"
    assert report["sha256_run_set"]


def test_max_shared_run_helper():
    assert psc.max_shared_run("the plant went live in March", "PLANT WENT LIVE") == len("plant went live")
    assert psc.max_shared_run("abcdef", "xyz") == 0


def test_gate_constants_are_env_overridable(monkeypatch):
    """GRAGGLE_PROBE_* retune the gates without editing the file."""
    monkeypatch.setenv("GRAGGLE_PROBE_MAX_EVIDENCE_CHARS", "420")
    monkeypatch.setenv("GRAGGLE_PROBE_OVERLAP_LIMIT", "17")
    spec = importlib.util.spec_from_file_location("probe_set_check_env", SCRIPT)
    assert spec and spec.loader
    tuned = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tuned)
    assert tuned.MAX_EVIDENCE_CHARS == 420
    assert tuned.OVERLAP_LIMIT == 17
    assert psc.MAX_EVIDENCE_CHARS == 300 and psc.OVERLAP_LIMIT == 40


def test_id_collision_with_earlier_set_fails(tmp_path, capsys):
    qpath, kpath, source_dir = _fixtures(tmp_path, [_question()], [_key()])
    earlier = _write(tmp_path / "earlier.jsonl", [
        {"id": "aa01", "question": "A completely different question about budgets",
         "source_doc": SOURCE_NAME, "type": "single-fact", "evidence": ""}])
    code, report = _run(qpath, kpath, source_dir, capsys, "--forbid-overlap-with", str(earlier),
                        "--overlap-limit", "200")
    assert code == 1
    assert any("already used in" in p for p in report["problems"])
    assert report["overlap"]["existing_question_counts"] == {"earlier.jsonl": 1}


def test_sources_are_found_in_subdirectories(tmp_path, capsys):
    source_dir = tmp_path / "src" / "heldout"
    source_dir.mkdir(parents=True)
    (source_dir / SOURCE_NAME).write_text(SOURCE_TEXT, encoding="utf-8")
    qpath = _write(tmp_path / "questions.jsonl", [_question()])
    kpath = _write(tmp_path / "answer-key.jsonl", [_key()])
    code, report = _run(qpath, kpath, tmp_path / "src", capsys)
    assert code == 0, report


def test_key_phrase_outside_the_evidence_span_fails(tmp_path, capsys):
    """A phrase may exist in the document yet be ungradeable if it is not in the quote."""
    qpath, kpath, source_dir = _fixtures(tmp_path, [_question()],
                                          [_key(phrases=("March", "transformation office"))])
    code, report = _run(qpath, kpath, source_dir, capsys)
    assert code == 1
    assert any("not inside the evidence span" in p for p in report["problems"])


def test_evidence_is_verbatim_after_whitespace(tmp_path, capsys):
    """ASR transcripts wrap oddly; the quote must still match after whitespace folding."""
    qpath, kpath, source_dir = _fixtures(
        tmp_path, [_question()],
        [_key(evidence="The plant went live in March after\n     three months of hypercare.")])
    code, report = _run(qpath, kpath, source_dir, capsys)
    assert code == 0, report


# ── GL-13b: office sources arrive as pre-extracted text, never as bytes ──────

OFFICE_NAME = "Beta deck.pptx"
OFFICE_TEXT = ("Slide 1\n\nTitle: Cutover readiness\n\n"
               "Q3 interface backlog reached 1,240 items before the cutover.\n\n"
               "[Table]\n| Region | Volume |\n| APAC | 310 |\n")


def _office_question(ident="pp01", source=OFFICE_NAME,
                     question="How many interface backlog items were recorded before the cutover?",
                     *, shape="pptx", q_type="single-fact", absent_terms=None):
    return {"id": ident, "question": question, "source_doc": source, "type": q_type,
            "shape": shape, "history": [], "evidence": "", "expected_answer": "",
            "absent_terms": absent_terms or []}


def _office_key(ident="pp01", source=OFFICE_NAME,
                evidence="Q3 interface backlog reached 1,240 items before the cutover.",
                phrases=("1,240 items",), shape="pptx", location=None):
    start = OFFICE_TEXT.index("Q3 interface backlog")
    return {"id": ident, "source_doc": source, "type": "single-fact", "shape": shape,
            "evidence": evidence, "expected_answer": "1,240 items.",
            "key_phrases": list(phrases),
            "location": location or {"char_start": start, "char_end": start + len(evidence),
                                     "slide": 1},
            "difficulty": "medium", "history": [], "absent_terms": []}


def _office_args(extracted_dir: Path) -> list[str]:
    extracted_dir.mkdir(parents=True, exist_ok=True)
    text_path = extracted_dir / "Beta-deck.txt"
    text_path.write_text(OFFICE_TEXT, encoding="utf-8")
    return ["--extracted-text", f"{OFFICE_NAME}={text_path}"]


def _office_fixtures(tmp_path: Path, questions=None, key=None):
    source_dir = tmp_path / "src"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / SOURCE_NAME).write_text(SOURCE_TEXT, encoding="utf-8")
    (source_dir / OFFICE_NAME).write_bytes(b"PK\x03\x04 not really a deck, and never read as text")
    qpath = _write(tmp_path / "questions.jsonl", questions or [_office_question()])
    kpath = _write(tmp_path / "answer-key.jsonl", key or [_office_key()])
    return qpath, kpath, source_dir


def test_office_source_without_extracted_text_fails_with_the_hint(tmp_path, capsys):
    qpath, kpath, source_dir = _office_fixtures(tmp_path)
    code, report = _run(qpath, kpath, source_dir, capsys)
    assert code == 1
    assert any(f"--extracted-text {OFFICE_NAME}=" in p for p in report["problems"]), report


def test_extracted_text_makes_an_office_source_checkable(tmp_path, capsys):
    qpath, kpath, source_dir = _office_fixtures(tmp_path)
    code, report = _run(qpath, kpath, source_dir, capsys, *_office_args(tmp_path / "extracted"))
    assert code == 0, report
    assert report["extracted_text_sources"] == [OFFICE_NAME]
    assert report["by_shape"] == {"pptx": 1}


def test_binary_sources_are_skipped_not_read_as_text(tmp_path, capsys):
    """Reading a .pptx as text gives junk in which every quote fails and every absent term
    passes by accident. It must be reported as skipped, and the extracted text used instead."""
    qpath, kpath, source_dir = _office_fixtures(tmp_path)
    code, report = _run(qpath, kpath, source_dir, capsys, *_office_args(tmp_path / "extracted"))
    assert code == 0, report
    assert report["binary_sources_skipped"] == [OFFICE_NAME]
    assert report["source_files_on_disk"] == 2


def test_evidence_only_in_the_binary_is_not_verbatim(tmp_path, capsys):
    """The junk inside the bytes must not rescue a quote that the extractor never produced."""
    qpath, kpath, source_dir = _office_fixtures(
        tmp_path, key=[_office_key(evidence="interface backlog reached 9,999 items")])
    code, report = _run(qpath, kpath, source_dir, capsys, *_office_args(tmp_path / "extracted"))
    assert code == 1
    assert any("not a verbatim substring" in p for p in report["problems"])


def test_unanswerable_phrase_present_in_office_text_fails(tmp_path, capsys):
    row = _office_question(ident="un01", question="What did the interface backlog cost per item?",
                           shape="unanswerable", q_type="unanswerable", absent_terms=["cost per item", "unit price", "licence fee"])
    key_row = {"id": "un01", "source_doc": OFFICE_NAME, "type": "unanswerable",
               "shape": "unanswerable", "evidence": "", "key_phrases": [],
               "distinctive_phrase": "interface backlog reached",
               "expected_answer": "not in the documents", "location": {"char_start": 0,
                                                                       "char_end": 0},
               "difficulty": "medium", "history": [], "absent_terms": ["cost per item",
                                                                       "unit price", "licence fee"]}
    qpath, kpath, source_dir = _office_fixtures(tmp_path, [row], [key_row])
    code, report = _run(qpath, kpath, source_dir, capsys, *_office_args(tmp_path / "extracted"))
    assert code == 1
    assert any("distinctive_phrase occurs in a source document" in p for p in report["problems"])


def test_absent_terms_are_checked_against_office_text_too(tmp_path, capsys):
    row = _office_question(ident="un02", question="Which vendor quoted the licence fee?",
                           shape="unanswerable", q_type="unanswerable", absent_terms=["cutover", "licence fee", "vendor quote"])
    key_row = {"id": "un02", "source_doc": OFFICE_NAME, "type": "unanswerable",
               "shape": "unanswerable", "evidence": "", "key_phrases": [],
               "distinctive_phrase": "licence fee per seat",
               "expected_answer": "not in the documents", "location": {"char_start": 0,
                                                                       "char_end": 0},
               "difficulty": "medium", "history": [], "absent_terms": ["cutover", "licence fee",
                                                                       "vendor quote"]}
    qpath, kpath, source_dir = _office_fixtures(tmp_path, [row], [key_row])
    code, report = _run(qpath, kpath, source_dir, capsys, *_office_args(tmp_path / "extracted"))
    assert code == 1
    assert any("absent_term #1 (sha256" in p for p in report["problems"]), report
    # the report is the one artefact allowed into a ticket: it names the slot, never the wording
    assert not any("cutover" in p for p in report["problems"]), report


def test_location_offsets_are_against_the_extracted_text(tmp_path, capsys):
    qpath, kpath, source_dir = _office_fixtures(
        tmp_path, key=[_office_key(location={"char_start": 5, "char_end": 40, "slide": 1})])
    code, report = _run(qpath, kpath, source_dir, capsys, *_office_args(tmp_path / "extracted"))
    assert code == 1
    assert any("does not match the located span" in p for p in report["problems"])


def test_extracted_text_argument_failures(tmp_path):
    for bad in ("no equals sign", "Ghost.txt=", "=no-name"):
        try:
            psc.parse_extracted_text([bad])
        except SystemExit:
            pass
        else:
            raise AssertionError(f"{bad!r} should not parse")
    missing = tmp_path / "absent.txt"
    try:
        psc.parse_extracted_text([f"Ghost.pdf={missing}"])
    except SystemExit as exc:
        assert "file not found" in str(exc)
    else:
        raise AssertionError("a missing extracted-text file must fail loudly")
