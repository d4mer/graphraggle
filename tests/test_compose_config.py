"""Guard test for compose.yml (packet-26: repo must match production).

Production was fixed on 2026-10-11 so Office/PDF files ingest on LightRAG
v1.5.7. Root cause: LightRAG's upload endpoint writes to the ``--input-dir`` CLI
argument while its parser looks the file up via the ``INPUT_DIR`` ENV var (image
default ``/app/data/inputs``). They differed, and the input dir also overlapped
the folder the ingest worker scans, so uploads either 409'd ("Input directory
already contains a file with the same canonical basename") or failed parsing
("Parse worker failed (native): legacy source file not found"). The fix is that
LightRAG gets its OWN input dir with ``--input-dir`` and ``INPUT_DIR`` equal.

These tests fail if that reconciliation drifts back. There is no YAML library in
the venv and adding a dependency for one test is not worth it, so this file
carries a small parser for the YAML subset compose.yml actually uses
(nested mappings, block sequences, flow sequences, quoted scalars, comments).

No network, no docker, no database.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

COMPOSE_PATH = Path(__file__).resolve().parents[1] / "compose.yml"
GITIGNORE_PATH = Path(__file__).resolve().parents[1] / ".gitignore"

# The container path the ingest worker's SOURCE_DOCS_DIR points at
# (./source_docs is mounted into gateway-api and ingest-worker).
WORKER_SCANNED_LIGHTRAG_PATH = "/data/source_docs"
SECRETISH_KEY = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS)", re.IGNORECASE)
LITERAL_SECRET = re.compile(r"\b(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b")


# ── Minimal YAML-subset parser ──────────────────────────────────────────────

def _strip_comment(line: str) -> str:
    out: list[str] = []
    quote: str | None = None
    for index, char in enumerate(line):
        if quote:
            out.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            out.append(char)
            continue
        if char == "#" and (index == 0 or line[index - 1] in " \t"):
            break
        out.append(char)
    return "".join(out).rstrip()


def _split_flow(inner: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in inner:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            current.append(char)
            continue
        if char == ",":
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _scalar(token: str):
    token = token.strip()
    if token.startswith("[") and token.endswith("]"):
        inner = token[1:-1].strip()
        return [] if not inner else [_scalar(part) for part in _split_flow(inner)]
    if token.startswith("{") and token.endswith("}"):
        inner = token[1:-1].strip()
        if not inner:
            return {}
        return {pair.split(":", 1)[0].strip(): _scalar(pair.split(":", 1)[1]) for pair in _split_flow(inner)}
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    if token in ("true", "True"):
        return True
    if token in ("false", "False"):
        return False
    return token


def _parse_lines(lines: list[tuple[int, str]], index: int, parent_indent: int):
    if index >= len(lines):
        return None, index
    indent, content = lines[index]
    if content.startswith("- "):
        sequence: list = []
        while index < len(lines) and lines[index][0] == indent and lines[index][1].startswith("- "):
            item = lines[index][1][2:].strip()
            index += 1
            if item:
                sequence.append(_scalar(item))
            else:
                child, index = _parse_lines(lines, index, indent)
                sequence.append(child)
        return sequence, index
    mapping: dict = {}
    while index < len(lines) and lines[index][0] == indent:
        key, _, value = lines[index][1].partition(":")
        key = key.strip().strip("\"'")
        value = value.strip()
        index += 1
        if value:
            mapping[key] = _scalar(value)
        elif index < len(lines) and lines[index][0] > indent:
            child, index = _parse_lines(lines, index, indent)
            mapping[key] = child
        else:
            mapping[key] = None
    return mapping, index


def load_compose() -> dict:
    lines: list[tuple[int, str]] = []
    for raw in COMPOSE_PATH.read_text(encoding="utf-8").splitlines():
        cleaned = _strip_comment(raw)
        if not cleaned.strip():
            continue
        lines.append((len(cleaned) - len(cleaned.lstrip(" ")), cleaned.strip()))
    document, consumed = _parse_lines(lines, 0, -1)
    assert consumed == len(lines), "compose.yml parser stopped early; it hit unsupported YAML"
    return document


# ── Guard assertions ────────────────────────────────────────────────────────

class TestComposeConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = load_compose()
        cls.services = cls.compose["services"]
        cls.lightrag = cls.services["lightrag-server"]

    def _command_args(self) -> list[str]:
        command = self.lightrag.get("command")
        self.assertIsInstance(command, list, "lightrag-server command must be a list")
        return [str(item) for item in command]

    def _volumes(self) -> list[str]:
        volumes = self.lightrag.get("volumes") or []
        return [str(item) for item in volumes]

    def test_compose_parses_and_has_the_expected_services(self):
        for name in ("lightrag-server", "gateway-api", "ingest-worker", "open-webui"):
            self.assertIn(name, self.services)

    def test_input_dir_argument_equals_input_dir_environment(self):
        """(a) --input-dir and INPUT_DIR must name the same directory."""
        args = self._command_args()
        self.assertIn("--input-dir", args)
        arg_value = args[args.index("--input-dir") + 1]
        environment = self.lightrag.get("environment") or {}
        self.assertIn("INPUT_DIR", environment, "INPUT_DIR env var is required")
        self.assertEqual(str(environment["INPUT_DIR"]), arg_value)

    def test_lightrag_input_dir_is_not_the_worker_scanned_directory(self):
        """(b) LightRAG needs an input dir of its own."""
        args = self._command_args()
        input_dir = args[args.index("--input-dir") + 1]
        self.assertNotEqual(input_dir, WORKER_SCANNED_LIGHTRAG_PATH)
        self.assertNotEqual(input_dir, "/app/source_docs")

    def test_lightrag_input_dir_is_mounted_and_source_docs_mount_kept(self):
        args = self._command_args()
        input_dir = args[args.index("--input-dir") + 1]
        mounts = {item.rsplit(":", 1)[0]: item.rsplit(":", 1)[1] for item in self._volumes()}
        self.assertIn(
            input_dir,
            mounts.values(),
            f"nothing mounts LightRAG's input dir {input_dir} into the container",
        )
        host_dir = next(host for host, target in mounts.items() if target == input_dir)
        self.assertNotEqual(host_dir, "./source_docs")
        self.assertEqual(host_dir, "./lightrag_inputs")
        self.assertIn("./source_docs", mounts, "./source_docs must stay mounted")

    def test_lightrag_image_tag_is_pinned(self):
        """(c) no :latest / :main on the LightRAG image."""
        image = str(self.lightrag.get("image") or "")
        self.assertTrue(image.startswith("ghcr.io/hkuds/lightrag:"))
        tag = image.rsplit(":", 1)[1]
        self.assertNotIn(tag.lower(), {"latest", "main", "stable"})
        self.assertRegex(tag, r"^v?\d+(\.\d+)+$")

    def test_open_webui_image_tag_is_pinned(self):
        image = str(self.services["open-webui"].get("image") or "")
        tag = image.rsplit(":", 1)[1]
        self.assertNotIn(tag.lower(), {"latest", "main", "stable"})

    def test_no_literal_secret_values_in_environment(self):
        """(d) every secret-ish env value is a ${...} reference or empty."""
        offenders: list[str] = []
        for service_name, service in self.services.items():
            environment = service.get("environment") or {}
            if not isinstance(environment, dict):
                continue
            for key, value in environment.items():
                if not SECRETISH_KEY.search(str(key)):
                    continue
                text = "" if value is None else str(value)
                if text == "" or text.startswith("${"):
                    continue
                offenders.append(f"{service_name}.environment.{key}")
        self.assertEqual(offenders, [], f"literal secret values in compose.yml: {offenders}")

    def test_openai_api_keys_is_parameterised(self):
        environment = self.services["open-webui"].get("environment") or {}
        self.assertIn("OPENAI_API_KEYS", environment)
        self.assertEqual(environment["OPENAI_API_KEYS"], "${OPENWEBUI_OPENAI_API_KEYS:-}")

    def test_compose_text_contains_no_token_shaped_strings(self):
        text = COMPOSE_PATH.read_text(encoding="utf-8")
        self.assertIsNone(LITERAL_SECRET.search(text))

    def test_ports_match_production(self):
        def host_ports(service: str) -> list[str]:
            return [str(item).split(":")[0] for item in (self.services[service].get("ports") or [])]

        self.assertEqual(host_ports("lightrag-server"), ["9621"])
        self.assertEqual(host_ports("gateway-api"), ["8020"])
        self.assertEqual(host_ports("open-webui"), ["3010"])

    def test_open_webui_hyperqwen_connection_is_present(self):
        environment = self.services["open-webui"].get("environment") or {}
        self.assertEqual(
            environment.get("OPENAI_API_BASE_URLS"),
            "http://192.168.1.195:1234/v1;http://192.168.1.195:18020/v1",
        )
        self.assertEqual(environment.get("OPENCLAW_BRIDGE_STATE_DIR"), "/app/backend/data/openclaw-bridge")
        self.assertIn("host.docker.internal:host-gateway", [str(h) for h in (self.services["open-webui"].get("extra_hosts") or [])])

    def test_gitignore_covers_the_lightrag_input_dir(self):
        lines = [line.strip() for line in GITIGNORE_PATH.read_text(encoding="utf-8").splitlines()]
        self.assertIn("lightrag_inputs/", lines)


if __name__ == "__main__":
    unittest.main()
