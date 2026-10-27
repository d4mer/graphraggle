#!/usr/bin/env python3
"""scripts/chunk_boundary_diff.py — A/B the structure chunker across two commits, in COUNTS ONLY.

Why this exists
---------------
A replay of the real corpus cut one PDF into 98 pieces where the stored build cut 106, with
ZERO sha overlap, while eight text transcripts and a DOCX came back byte-exact. That is a
boundary question about one document, and answering it means running the two builds over the
SAME text. This tool does that and reports only what is safe to paste into a report: counts,
lengths, sha256 digests, character offsets, line numbers and booleans.

**It never prints document text.** No piece, no line, no excerpt, no word list. The only input
that may hold real text is the file you hand it (`--text-file` / `--pdf`); it is read inside a
child process whose stdout is parsed for numbers and digests, and the parent only ever emits
what it derived from those.

What is compared
----------------
For each side, in its OWN checkout, with the worker's own call shape:

    chunk_document(text, filename=..., max_piece_tokens=..., min_piece_tokens=...,
                   until_merge="floor", topic_embed=None, header_tokens=..., headers=True)
    piece.render() for piece in plan.pieces          # header + body: the stored chunk text

so a piece's sha256 here is the sha256 of the text LightRAG stores after splitting the rendered
payload on the marker. The input sanitiser is not replayed (the worker's own survival check
covers that); a side whose plan cannot render reports an error rather than a zero.

Modes
-----
default            pieces per side, per-piece length + sha256, the first index where the sides
                   diverge, how many pieces are identical at the same index, sha OVERLAP
                   between the two sides (a shifted-but-identical piece still overlaps), and for
                   every differing position the character offsets and lengths on both sides
--only-function    no chunking at all: for each line the heading detector examined,
                   (line number, character length, left verdict, right verdict) for the lines
                   where the two sides DISAGREE, plus examined/true/flipped counts for both
                   `_is_heading` (composite) and `_is_all_caps_heading` (the suspect rule)

Usage
-----
    python3 scripts/chunk_boundary_diff.py --left 0047a69 --right f7051e6 \
        --text-file /tmp/doc.txt [--filename doc.pdf] [--only-function] \
        [--env KEY=VALUE]... [--max-piece-tokens 7500] [--min-piece-tokens 150] \
        [--merge-until floor] [--header-tokens 25] [--no-headers] \
        [--pdf-backend auto] [--max-bytes N] [--max-chars N] [--repo PATH] [--json]

`--left`/`--right` take a commit-ish (exported with `git archive` into a temp dir, so the
working tree is never the thing under test) or a path to a repo tree, which is used read-only
and labelled `tree` in the report because it is not a clean checkout.

Each side runs in its OWN interpreter rooted at its own checkout: the two `app` packages can
never see each other, which is the only way to compare a module that uses relative imports.

`--env KEY=VALUE` is repeatable and pins the child environment for both sides, so a run does not
inherit whatever the shell had: `--env WORKER_PDF_TEXT_BACKEND=pypdf`, `--env
STRUCTURE_CHUNKING_ENABLED=true`. Keys and values are echoed in the report (they are
configuration, not content). The chunker reads no settings at all, so a pin changes the INPUT
(only the PDF extraction step consults those names), never the cutting.

Standard library only. `app.structure_chunker` and `app.pdf_extract` are themselves
stdlib-only, so no third-party package is needed.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

CHILD_FLAG = "--__boundary_child__"
SIDES = ("left", "right")

# app/config.py shipped caps for extraction; overridable per run.
DEFAULT_PDF_MAX_BYTES = 104_857_600
DEFAULT_PDF_MAX_CHARS = 2_000_000

# Reported from plan.stats when present: counts and numbers, never content.
STATS_KEYS = ("natural_units", "units_whole", "pieces", "pieces_merged_up_to_floor",
              "stub_pieces_merged", "lines_in", "tokens_p50", "tokens_p95", "tokens_max",
              "budget_tokens", "tokenizer")


# ── side resolution (a ref never resolves against the working tree) ─────────

def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def resolve_repo(start: Path) -> Path:
    """The repository root refs are read from.

    Falls back to ``start`` when it is not a git working tree (an archive export, a vendored
    copy): tree-path mode still works there, and a commit-ish is refused with a message rather
    than a subprocess traceback.
    """
    done = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return Path(done.stdout.decode().strip()) if done.returncode == 0 else start


def commit_of(repo: Path, ref: str) -> str | None:
    """The commit a ref names, or None when it is not a ref (e.g. a directory path)."""
    done = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return done.stdout.decode().strip()[:12] if done.returncode == 0 else None


def export_checkout(repo: Path, ref: str, workdir: Path) -> Path:
    """`git archive <ref>` into a fresh temp dir, so the working tree is never under test."""
    target = workdir / ref.replace("/", "_")
    target.mkdir(parents=True, exist_ok=True)
    try:
        payload = git(repo, "archive", "--format=tar", ref)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"git archive {ref!r} in {repo} failed: "
                         f"{exc.stderr.decode('utf-8', 'replace').strip()}") from None
    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        tar.extractall(path=str(target))   # noqa: S202 - our own repo's git archive
    return target


# ── the child: one side, one interpreter, one JSON line ────────────────────

def child_env(checkout: Path, pins: dict[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    env.update(pins)
    root = str(checkout)
    existing = [part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part]
    env["PYTHONPATH"] = os.pathsep.join([root, *existing])
    return env


def run_child(checkout: Path, pins: dict[str, str], argv: list[str]) -> dict:
    """Run this same file as a child with `checkout` as its cwd; return its JSON."""
    command = [sys.executable, str(Path(__file__).resolve()), CHILD_FLAG, *argv]
    done = subprocess.run(command, cwd=str(checkout), env=child_env(checkout, pins),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    text = done.stdout.decode("utf-8", "replace").strip()
    if done.returncode != 0 or not text:
        return {"error": f"child exited {done.returncode}: "
                         f"{done.stderr.decode('utf-8', 'replace').strip()[:300]}"}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        return {"error": f"child stdout was not JSON: {exc}"}


def _load_app(checkout: Path):
    """Import THIS checkout's package. First on sys.path, and the child is a fresh process."""
    sys.path.insert(0, str(checkout))
    import app.structure_chunker as sc
    return sc


def _input_text(args) -> tuple[str, str]:
    """``(text, backend_name)`` for this side. A PDF is extracted by that side's own code."""
    if args.text_file:
        return Path(args.text_file).read_text(encoding="utf-8", errors="replace"), "text-file"
    from app import pdf_extract
    backend = args.pdf_backend or os.environ.get("WORKER_PDF_TEXT_BACKEND") or "auto"
    max_bytes = (args.max_bytes if args.max_bytes is not None
                 else int(os.environ.get("WORKER_PDF_MAX_BYTES") or DEFAULT_PDF_MAX_BYTES))
    max_chars = (args.max_chars if args.max_chars is not None
                 else int(os.environ.get("WORKER_PDF_MAX_CHARS") or DEFAULT_PDF_MAX_CHARS))
    text, used = pdf_extract.extract_pdf_payload_with_backend(
        Path(args.pdf), max_bytes=max_bytes, max_chars=max_chars, backend=backend)
    return text, used


CHILD_PARSER = argparse.ArgumentParser(add_help=False)
CHILD_PARSER.add_argument(CHILD_FLAG, action="store_true")
CHILD_PARSER.add_argument("--checkout", required=True)
CHILD_PARSER.add_argument("--mode", choices=("pieces", "headings"), required=True)
CHILD_PARSER.add_argument("--text-file", default="")
CHILD_PARSER.add_argument("--pdf", default="")
CHILD_PARSER.add_argument("--filename", default="document")
CHILD_PARSER.add_argument("--max-piece-tokens", type=int, default=None)
CHILD_PARSER.add_argument("--min-piece-tokens", type=int, default=None)
CHILD_PARSER.add_argument("--merge-until", default="floor")
CHILD_PARSER.add_argument("--header-tokens", type=int, default=None)
CHILD_PARSER.add_argument("--no-headers", action="store_true")
CHILD_PARSER.add_argument("--pdf-backend", default="")
CHILD_PARSER.add_argument("--max-bytes", type=int, default=None)
CHILD_PARSER.add_argument("--max-chars", type=int, default=None)


def child_main(argv: list[str]) -> int:
    args = CHILD_PARSER.parse_args(argv)
    checkout = Path(args.checkout)
    try:
        sc = _load_app(checkout)
        text, backend_used = _input_text(args)
    except Exception as exc:  # a side that cannot load or extract is an error, never a zero
        return _emit({"error": f"{type(exc).__name__}: {str(exc)[:300]}"})

    if args.mode == "headings":
        lines = text.splitlines()
        rows_caps = [[index, len(line), bool(sc._is_all_caps_heading(line))]
                     for index, line in enumerate(lines, start=1)]
        rows_heading = [[index, len(line), bool(sc._is_heading(line))]
                        for index, line in enumerate(lines, start=1)]
        return _emit({
            "mode": "headings",
            "lines_examined": len(lines),
            "all_caps_true": sum(1 for row in rows_caps if row[2]),
            "heading_true": sum(1 for row in rows_heading if row[2]),
            "all_caps": rows_caps,          # [1-based line, char length, verdict]
            "heading": rows_heading,
            "extract_backend": backend_used,
            "input_chars": len(text),
        })

    plan = sc.chunk_document(
        text,
        filename=args.filename,
        max_piece_tokens=args.max_piece_tokens,
        min_piece_tokens=args.min_piece_tokens,
        until_merge=args.merge_until,
        topic_embed=None,                    # no topic pass: deterministic, offline, comparable
        header_tokens=args.header_tokens,
        headers=not args.no_headers,
    )
    rendered = [piece.render() for piece in plan.pieces]
    return _emit({
        "mode": "pieces",
        "pieces": len(rendered),
        "shas": [hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] for text in rendered],
        "lengths": [len(text) for text in rendered],
        "stats": {key: value for key, value in plan.stats.items()
                  if isinstance(value, (int, float, str, bool))},
        "warnings": len(plan.warnings),
        "budget": {"max_piece_tokens": args.max_piece_tokens,
                   "min_piece_tokens": args.min_piece_tokens,
                   "merge_until": args.merge_until,
                   "header_tokens": args.header_tokens,
                   "headers": not args.no_headers},
        "extract_backend": backend_used,
        "input_chars": len(text),
        "input_lines": len(text.splitlines()),
    })


def _emit(payload: dict) -> int:
    json.dump(payload, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


# ── the diff: counts, offsets, digests ─────────────────────────────────────

def _same_at(index: int, left: dict, right: dict) -> bool:
    return (index < len(left["shas"]) and index < len(right["shas"])
            and left["shas"][index] == right["shas"][index]
            and left["lengths"][index] == right["lengths"][index])


def diff_pieces(left: dict, right: dict) -> dict:
    """Positional piece comparison. Offsets are character offsets in each side's own document."""
    count_left, count_right = len(left["shas"]), len(right["shas"])
    shared = min(count_left, count_right)
    identical = sum(1 for index in range(shared) if _same_at(index, left, right))
    first_divergence = next((index for index in range(shared) if not _same_at(index, left, right)),
                            None)
    if first_divergence is None and count_left != count_right:
        first_divergence = shared          # one side simply has extra pieces at the end
    regions: list[dict] = []
    offset_left = offset_right = 0
    for index in range(max(count_left, count_right)):
        left_length = left["lengths"][index] if index < count_left else 0
        right_length = right["lengths"][index] if index < count_right else 0
        if not _same_at(index, left, right):
            regions.append({"index": index,
                            "sha_left": left["shas"][index] if index < count_left else None,
                            "sha_right": right["shas"][index] if index < count_right else None,
                            "chars_left": [offset_left, offset_left + left_length],
                            "length_left": left_length,
                            "chars_right": [offset_right, offset_right + right_length],
                            "length_right": right_length})
        offset_left += left_length
        offset_right += right_length
    return {
        "pieces_left": count_left,
        "pieces_right": count_right,
        "identical_at_same_index": identical,
        "first_divergence_index": first_divergence,
        # Overlap is set membership, not position: a boundary that MOVES without changing any
        # piece's content still overlaps. Zero overlap means the pieces themselves differ.
        "sha_overlap": len(set(left["shas"]) & set(right["shas"])),
        "extra_on_left": max(0, count_left - count_right),
        "extra_on_right": max(0, count_right - count_left),
        "total_chars_left": sum(left["lengths"]),
        "total_chars_right": sum(right["lengths"]),
        "length_p50_left": _p50(left["lengths"]), "length_p50_right": _p50(right["lengths"]),
        "length_max_left": max(left["lengths"], default=0),
        "length_max_right": max(right["lengths"], default=0),
        "differing_positions": len(regions),
        "regions": regions,
    }


_HEADING_KEYS = {"all_caps": "all_caps_true", "heading": "heading_true"}


def diff_headings(left: dict, right: dict) -> dict:
    """Flips as (line, length, left verdict, right verdict). Line text is never carried."""
    report: dict[str, dict] = {}
    for key, total_key in _HEADING_KEYS.items():
        left_rows = {row[0]: row for row in left[key]}
        right_rows = {row[0]: row for row in right[key]}
        flips = [{"line": line, "length": left_rows[line][1],
                  "left": left_rows[line][2], "right": right_rows[line][2]}
                 for line in sorted(set(left_rows) & set(right_rows))
                 if left_rows[line][2] != right_rows[line][2]]
        report[key] = {
            "lines_examined": left["lines_examined"],
            "true_left": left[total_key],
            "true_right": right[total_key],
            "flipped": len(flips),
            "flips": flips,
        }
    return report


def _p50(values: list[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    return float(ordered[middle]) if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


# ── parent ─────────────────────────────────────────────────────────────────

def parse_pins(pairs: list[str]) -> dict[str, str]:
    pins: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--env expects KEY=VALUE, got {pair!r}")
        key, value = pair.split("=", 1)
        pins[key.strip()] = value.strip()
    return pins


def build_parent_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--left", required=True, help="commit-ish or repo tree for side A")
    parser.add_argument("--right", required=True, help="commit-ish or repo tree for side B")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text-file", help="path to a text file (for example extracted text)")
    source.add_argument("--pdf", help="path to a PDF; each side extracts with its own code")
    parser.add_argument("--filename", default="document", help="name handed to the chunker")
    parser.add_argument("--only-function", action="store_true",
                        help="heading-detector disagreement mode (no chunking, no pieces)")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                        help="pin an environment variable for both sides (repeatable)")
    parser.add_argument("--max-piece-tokens", type=int, default=7500)
    parser.add_argument("--min-piece-tokens", type=int, default=150)
    parser.add_argument("--merge-until", default="floor")
    parser.add_argument("--header-tokens", type=int, default=25)
    parser.add_argument("--no-headers", action="store_true")
    parser.add_argument("--pdf-backend", default="",
                        help="auto|pdftotext|pypdf (default: WORKER_PDF_TEXT_BACKEND or auto)")
    parser.add_argument("--max-bytes", type=int, default=None)
    parser.add_argument("--max-chars", type=int, default=None)
    parser.add_argument("--repo", default=".", help="git repo the refs resolve in")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    return parser


def side_argv(args) -> list[str]:
    argv = ["--checkout", "{CHECKOUT}", "--mode", "headings" if args.only_function else "pieces",
            "--text-file", args.text_file or "", "--pdf", args.pdf or "",
            "--filename", args.filename,
            "--max-piece-tokens", str(args.max_piece_tokens),
            "--min-piece-tokens", str(args.min_piece_tokens),
            "--merge-until", args.merge_until, "--header-tokens", str(args.header_tokens)]
    if args.no_headers:
        argv.append("--no-headers")
    if args.pdf_backend:
        argv += ["--pdf-backend", args.pdf_backend]
    if args.max_bytes is not None:
        argv += ["--max-bytes", str(args.max_bytes)]
    if args.max_chars is not None:
        argv += ["--max-chars", str(args.max_chars)]
    return argv


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if CHILD_FLAG in argv:
        return child_main(argv)

    args = build_parent_parser().parse_args(argv)
    pins = parse_pins(args.env)
    repo = resolve_repo(Path(args.repo).resolve())
    report: dict = {"mode": "headings" if args.only_function else "pieces",
                    "filename": args.filename, "env_pins": pins}
    sides: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="chunk-boundary-") as temp:
        workdir = Path(temp)
        for side in SIDES:
            ref = getattr(args, side)
            commit = commit_of(repo, ref)
            if commit:
                checkout, kind = export_checkout(repo, ref, workdir), "archive"
            else:
                candidate = Path(ref).resolve()
                if not (candidate / "app" / "structure_chunker.py").exists():
                    raise SystemExit(f"--{side} {ref!r} is neither a commit-ish in {repo} nor a "
                                     f"repo tree containing app/structure_chunker.py")
                checkout, kind = candidate, "tree (NOT a clean checkout)"
            child_argv = side_argv(args)
            child_argv[child_argv.index("{CHECKOUT}")] = str(checkout)
            result = run_child(checkout, pins, child_argv)
            result.update({"ref": ref, "commit": commit, "kind": kind})
            sides[side] = result

    heavy = ("shas", "lengths", "all_caps", "heading")
    for side in SIDES:
        report[side] = {key: value for key, value in sides[side].items() if key not in heavy}
        if "error" in sides[side]:
            report["ok"] = False
    if report.get("ok") is False:
        print(json.dumps(report, sort_keys=True) if args.json else _text_report(report))
        return 2
    if args.only_function:
        report["headings"] = diff_headings(sides["left"], sides["right"])
    else:
        report["diff"] = diff_pieces(sides["left"], sides["right"])
        report["pieces"] = {side: {"shas": sides[side]["shas"],
                                   "lengths": sides[side]["lengths"]} for side in SIDES}
    report["ok"] = True
    print(json.dumps(report, sort_keys=True) if args.json else _text_report(report))
    return 0


def _side_line(row: dict) -> str:
    if "error" in row:
        return f"  {row.get('ref')}: ERROR {row['error']}"
    if row.get("mode") == "headings":
        parts = [f"lines_examined={row.get('lines_examined')}",
                 f"heading_true={row.get('heading_true')}",
                 f"all_caps_true={row.get('all_caps_true')}"]
    else:
        parts = [f"pieces={row.get('pieces')}", f"warnings={row.get('warnings')}",
                 f"input_chars={row.get('input_chars')}"]
        for key in STATS_KEYS:
            if key in (row.get("stats") or {}):
                parts.append(f"{key}={row['stats'][key]}")
    parts.append(f"backend={row.get('extract_backend')}")
    parts.append(f"commit={row.get('commit')}")
    return "  " + " ".join(str(part) for part in parts)


def _text_report(report: dict) -> str:
    lines = [f"chunk boundary diff [{report['mode']}]: "
             f"left={report['left'].get('ref')} right={report['right'].get('ref')}"]
    for side in SIDES:
        lines.append(_side_line(report[side]))
    if "diff" in report:
        diff = report["diff"]
        lines.append(
            f"  identical_at_same_index={diff['identical_at_same_index']} "
            f"first_divergence_index={diff['first_divergence_index']} "
            f"sha_overlap={diff['sha_overlap']} extra_left={diff['extra_on_left']} "
            f"extra_right={diff['extra_on_right']} "
            f"p50_chars={diff['length_p50_left']}/{diff['length_p50_right']} "
            f"max_chars={diff['length_max_left']}/{diff['length_max_right']}")
        for region in diff["regions"][:25]:
            lines.append(f"    index {region['index']}: left {region['chars_left']} "
                         f"({region['length_left']} ch) | right {region['chars_right']} "
                         f"({region['length_right']} ch)")
        if len(diff["regions"]) > 25:
            lines.append(f"    ... {len(diff['regions']) - 25} more differing positions")
    if "headings" in report:
        for key, block in report["headings"].items():
            lines.append(f"  {key}: examined={block['lines_examined']} "
                         f"true_left={block['true_left']} true_right={block['true_right']} "
                         f"flipped={block['flipped']}")
            for flip in block["flips"][:40]:
                lines.append(f"    line {flip['line']} len={flip['length']} "
                             f"left={flip['left']} right={flip['right']}")
            if block["flipped"] > 40:
                lines.append(f"    ... {block['flipped'] - 40} more flipped lines")
    if report.get("env_pins"):
        lines.append("  env pins: " + " ".join(f"{key}={value}"
                                               for key, value in sorted(report["env_pins"].items())))
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
