"""The conftest socket guard itself.

Why this test exists: the overnight-loop driver used to be steered at a stub by
patching module constants, and a change that froze those constants at import time
made the suite open real connections toward the production LightRAG and gateway.
It ran for hours instead of seconds. The guard in tests/conftest.py turns that
failure mode into an instant assertion.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# tests/ is a package, so conftest is importable under its real name and the
# already-imported module is reused (the guard is not wrapped twice).
from tests.conftest import BLOCKED_TEST_PORTS


class TestSocketGuard(unittest.TestCase):
    def test_production_service_ports_are_blocked(self):
        for port in sorted(BLOCKED_TEST_PORTS):
            with self.subTest(port=port):
                with self.assertRaises(AssertionError) as caught:
                    socket.create_connection(("127.0.0.1", port), timeout=1)
                self.assertIn("blocked in tests", str(caught.exception))

    def test_connect_ex_is_blocked_too(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            with self.assertRaises(AssertionError):
                sock.connect_ex(("127.0.0.1", min(BLOCKED_TEST_PORTS)))

    def test_an_ephemeral_local_listener_still_works(self):
        """The guard must not break the suite's own stub servers (port 0)."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def accept_once():
            conn, _addr = listener.accept()
            conn.close()

        server = threading.Thread(target=accept_once, daemon=True)
        server.start()
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
                self.assertIsNotNone(client)
        finally:
            listener.close()
            server.join(timeout=2)

    def test_blocked_ports_are_the_documented_ones(self):
        self.assertEqual(BLOCKED_TEST_PORTS, frozenset({9621, 8020}))


if __name__ == "__main__":
    unittest.main()
