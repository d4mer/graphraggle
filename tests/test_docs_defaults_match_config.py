"""docs/05 "defaults as shipped" must agree with app/config.py.

Why this exists
---------------
``docs/05-known-good-config.md`` is the operator's reference for what the stack does
out of the box. It once documented ``STRUCTURE_CHUNKING_ENABLED=true`` in a block
headed "defaults as shipped" while ``app/config.py`` defaulted it to ``false`` - an
operator following the doc would have believed the worker structure-chunks when it
does not. The same drift can happen to any line in that block, and no test noticed.

What it checks
--------------
The two fenced blocks under the "Structure chunking" heading are parsed as
``NAME=value`` lines (inline ``#`` comments stripped) and compared with the
``Field(default=...)`` of the matching ``Settings`` field, matched by alias. The
comparison is strict: unknown names, missing names and value drift all fail with the
doc value and the code default both printed.

Nothing here reads corpus, probe or LightRAG data, and nothing reaches the network:
``Settings`` is introspected through ``model_fields``, so no instance is built from
the ambient environment beyond what ``tests/conftest.py`` already provides.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

DOC = _ROOT / "docs" / "05-known-good-config.md"

_HEADING = "Structure chunking (the worker cuts, LightRAG obeys)"
_ASSIGNMENT = re.compile(r"^(?P<name>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")

# The flags that decide whether any of this runs. They are checked twice: once as
# part of the block (value == code default) and once for being listed in the
# "turn it on" block, so the doc cannot quietly drop the operator-facing advice.
_ENABLE_FLAGS = ("STRUCTURE_CHUNKING_ENABLED", "WORKER_PDF_EXTRACT_ENABLED",
                 "WORKER_DOCX_EXTRACT_ENABLED")


def _fenced_blocks(text: str, after_heading: str) -> list[list[str]]:
    """The fenced blocks that follow ``after_heading`` and precede the next heading."""
    start = text.index(after_heading)
    rest = text[start:]
    next_heading = rest.find("\n## ", len(after_heading))
    section = rest if next_heading == -1 else rest[:next_heading]
    blocks: list[list[str]] = []
    inside, buffer = False, []
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if inside:
                blocks.append(buffer)
                inside, buffer = False, []
            else:
                inside, buffer = True, []
            continue
        if inside:
            buffer.append(line)
    return blocks


def _documented_values(block: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in block:
        match = _ASSIGNMENT.match(line.strip())
        if not match:
            continue
        value = match.group("value").split("#", 1)[0].strip()
        values[match.group("name")] = value
    return values


def _code_defaults() -> dict[str, object]:
    from app.config import Settings
    return {info.alias: info.default for info in Settings.model_fields.values() if info.alias}


def _as_documented(default: object, written: str) -> bool:
    """Compare a Python default with how the doc writes it, without inventing tolerance."""
    if isinstance(default, bool):
        return written.lower() in {"true", "false"} and (written.lower() == "true") is default
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(written) == default
        except ValueError:
            return False
    if isinstance(default, float):
        try:
            return float(written) == default
        except ValueError:
            return False
    return written == (default or "")


class TestDocsDefaultsMatchConfig(unittest.TestCase):
    def setUp(self):
        self.assertTrue(DOC.exists(), f"{DOC} is missing")
        self.text = DOC.read_text(encoding="utf-8")
        self.assertIn(_HEADING, self.text, "the structure-chunking section was renamed")

    def test_the_shipped_defaults_block_matches_the_code(self):
        blocks = _fenced_blocks(self.text, _HEADING)
        self.assertTrue(blocks, "no fenced block under the structure-chunking heading")
        documented = _documented_values(blocks[0])
        self.assertGreaterEqual(len(documented), 10, "the defaults block lost its lines")
        defaults = _code_defaults()
        wrong = []
        for name, written in documented.items():
            if name not in defaults:
                wrong.append(f"{name}: documented but not a Settings alias")
                continue
            if not _as_documented(defaults[name], written):
                wrong.append(f"{name}: doc says {written!r}, code default is {defaults[name]!r}")
        self.assertEqual(wrong, [], "docs/05 defaults drifted from app/config.py:\n  "
                                     + "\n  ".join(wrong))

    def test_enable_flags_ship_off_and_are_listed_to_turn_them_on(self):
        defaults = _code_defaults()
        for flag in _ENABLE_FLAGS:
            with self.subTest(flag=flag):
                self.assertIs(defaults[flag], False,
                              f"{flag} no longer ships false - update the doc's framing")
        blocks = _fenced_blocks(self.text, _HEADING)
        self.assertGreaterEqual(len(blocks), 2,
                                "the block labelled 'defaults as shipped' needs a companion "
                                "'to turn it on' block naming " + ", ".join(_ENABLE_FLAGS))
        for block in blocks[1:]:
            turn_on = _documented_values(block)
            if all(flag in turn_on for flag in _ENABLE_FLAGS):
                for flag in _ENABLE_FLAGS:
                    self.assertEqual(turn_on[flag], "true", f"{flag} is not turned on in the list")
                return
        self.fail(f"no 'to turn it on' block names all of {', '.join(_ENABLE_FLAGS)}")

    def test_each_block_says_whether_it_is_defaults_or_production(self):
        """The recurring bug was a production value sitting under a "defaults" label."""
        start = self.text.index(_HEADING)
        next_heading = self.text.find("\n## ", start)
        section = self.text[start:next_heading if next_heading != -1 else len(self.text)]
        self.assertRegex(section, r"(?i)as shipped", "the defaults block needs labelling")
        self.assertRegex(section, r"(?i)to turn it on", "production values need their own labelled block")
        framing = section[:section.index("```")]
        self.assertIn("app/config.py", framing,
                      "the block must name where its values were read from")


if __name__ == "__main__":
    unittest.main()
