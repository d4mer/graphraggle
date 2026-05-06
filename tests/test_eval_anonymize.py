"""Tests for Packet 15: eval_anonymize_samples.py deterministic anonymization.

Covers:
- Email address replacement with deterministic placeholder
- Phone number replacement
- SSN replacement
- IP address replacement
- Multi-word name replacement
- Determinism: same input → same output
- Non-PII text passes through unchanged
- Round-trip: input file → output file → same output on re-run
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import scripts.eval_anonymize_samples as anon_module


class TestDeterministicHash(unittest.TestCase):
    """Test _deterministic_hash produces consistent placeholders."""

    def test_same_input_same_output(self):
        """Same value + category + salt → same hash."""
        h1 = anon_module._deterministic_hash("john@example.com", "email", anon_module.FIXED_SALT)
        h2 = anon_module._deterministic_hash("john@example.com", "email", anon_module.FIXED_SALT)
        self.assertEqual(h1, h2)

    def test_different_category_different_hash(self):
        """Same value, different category → different hash."""
        h1 = anon_module._deterministic_hash("john@example.com", "email", anon_module.FIXED_SALT)
        h2 = anon_module._deterministic_hash("john@example.com", "person", anon_module.FIXED_SALT)
        self.assertNotEqual(h1, h2)

    def test_different_value_different_hash(self):
        """Different values → different hash."""
        h1 = anon_module._deterministic_hash("a@b.com", "email", anon_module.FIXED_SALT)
        h2 = anon_module._deterministic_hash("c@d.com", "email", anon_module.FIXED_SALT)
        self.assertNotEqual(h1, h2)

    def test_placeholder_format(self):
        """Placeholder format: CATEGORY_HHHH."""
        h = anon_module._deterministic_hash("test@example.com", "email", anon_module.FIXED_SALT)
        self.assertTrue(h.startswith("EMAIL_"))
        self.assertEqual(len(h), 10)  # "EMAIL_" (6) + 4 hex chars


class TestAnonymizeText(unittest.TestCase):
    """Test anonymize_text function."""

    def test_email_replacement(self):
        """Email addresses are replaced with deterministic placeholders."""
        text = "Contact john@example.com for details."
        result = anon_module.anonymize_text(text)
        self.assertNotIn("john@example.com", result)
        self.assertIn("EMAIL_", result)

    def test_phone_replacement(self):
        """Phone numbers are replaced."""
        text = "Call 555-123-4567 for support."
        result = anon_module.anonymize_text(text)
        self.assertNotIn("555-123-4567", result)
        self.assertIn("PHONE_", result)

    def test_ssn_replacement(self):
        """SSN patterns are replaced."""
        text = "SSN: 123-45-6789"
        result = anon_module.anonymize_text(text)
        self.assertNotIn("123-45-6789", result)
        self.assertIn("SSN_", result)

    def test_ip_replacement(self):
        """IP addresses are replaced."""
        text = "Server at 192.168.1.100"
        result = anon_module.anonymize_text(text)
        self.assertNotIn("192.168.1.100", result)
        self.assertIn("IP_", result)

    def test_no_pii_unchanged(self):
        """Text without PII passes through unchanged."""
        text = "What is the Q3 revenue?"
        result = anon_module.anonymize_text(text)
        self.assertEqual(result, text)

    def test_multiple_pii_same_text(self):
        """Multiple PII items in one text → all replaced."""
        text = "Email john@example.com, phone 555-123-4567"
        result = anon_module.anonymize_text(text)
        self.assertNotIn("john@example.com", result)
        self.assertNotIn("555-123-4567", result)
        self.assertIn("EMAIL_", result)
        self.assertIn("PHONE_", result)

    def test_deterministic_across_calls(self):
        """Same text → same anonymized output on repeated calls."""
        text = "Contact john@example.com at 192.168.1.1"
        r1 = anon_module.anonymize_text(text)
        r2 = anon_module.anonymize_text(text)
        self.assertEqual(r1, r2)


class TestAnonymizeQuery(unittest.TestCase):
    """Test anonymize_query function."""

    def test_query_field_anonymized(self):
        """Query field is anonymized, other fields preserved."""
        query = {"id": "p001", "query": "Email john@example.com", "category": "test"}
        result = anon_module.anonymize_query(query)
        self.assertNotIn("john@example.com", result["query"])
        self.assertEqual(result["id"], "p001")
        self.assertEqual(result["category"], "test")

    def test_no_query_field(self):
        """Query without 'query' key → returned unchanged."""
        query = {"id": "p001", "category": "test"}
        result = anon_module.anonymize_query(query)
        self.assertEqual(result, query)


class TestAnonymizeFile(unittest.TestCase):
    """Test anonymize_file function."""

    def test_file_anonymization(self):
        """File with PII → anonymized output file."""
        queries = [
            {"id": "p001", "query": "Contact john@example.com"},
            {"id": "p002", "query": "Call 555-123-4567"},
        ]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as inp:
            json.dump(queries, inp)
            inp_path = inp.name

        out_path = inp_path + ".anonymized.json"
        try:
            count = anon_module.anonymize_file(inp_path, out_path)
            self.assertEqual(count, 2)

            with open(out_path) as f:
                result = json.load(f)

            self.assertNotIn("john@example.com", result[0]["query"])
            self.assertNotIn("555-123-4567", result[1]["query"])
        finally:
            os.unlink(inp_path)
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_deterministic_file_output(self):
        """Same input file → same output on re-run."""
        queries = [
            {"id": "p001", "query": "Email john@example.com"},
        ]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as inp:
            json.dump(queries, inp)
            inp_path = inp.name

        out_path1 = inp_path + ".out1.json"
        out_path2 = inp_path + ".out2.json"
        try:
            anon_module.anonymize_file(inp_path, out_path1)
            anon_module.anonymize_file(inp_path, out_path2)

            with open(out_path1) as f1, open(out_path2) as f2:
                self.assertEqual(f1.read(), f2.read())
        finally:
            os.unlink(inp_path)
            for p in [out_path1, out_path2]:
                if os.path.exists(p):
                    os.unlink(p)

    def test_missing_input_file(self):
        """Non-existent input file → returns 0."""
        count = anon_module.anonymize_file("/nonexistent/path.json", "/tmp/out.json")
        self.assertEqual(count, 0)

    def test_non_array_input(self):
        """Non-array JSON → returns 0 with error."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            f.write('{"not": "an array"}')
            path = f.name
        try:
            count = anon_module.anonymize_file(path, "/tmp/out.json")
            self.assertEqual(count, 0)
        finally:
            os.unlink(path)

    def test_empty_array(self):
        """Empty array → 0 queries processed, valid output."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            f.write("[]")
            path = f.name
        out = path + ".out.json"
        try:
            count = anon_module.anonymize_file(path, out)
            self.assertEqual(count, 0)
            with open(out) as f:
                self.assertEqual(json.load(f), [])
        finally:
            os.unlink(path)
            if os.path.exists(out):
                os.unlink(out)


class TestFixedSalt(unittest.TestCase):
    """Test that the salt is fixed (not random)."""

    def test_salt_is_constant(self):
        """Salt is a fixed string, not a random value."""
        self.assertIsInstance(anon_module.FIXED_SALT, str)
        self.assertEqual(
            anon_module.FIXED_SALT,
            "graphrag-packet15-eval-salt-2026",
        )

    def test_salt_not_empty(self):
        self.assertTrue(len(anon_module.FIXED_SALT) > 0)


if __name__ == "__main__":
    unittest.main()
