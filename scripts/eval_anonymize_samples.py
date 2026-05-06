#!/usr/bin/env python3
"""scripts/eval_anonymize_samples.py — Deterministic local anonymization.

Reads a JSON file of production query samples and produces an anonymized
output file with deterministic placeholder substitutions. No raw PII enters
the git repository.

Usage:
    python3 scripts/eval_anonymize_samples.py input.json output.json

Input format (JSON array):
    [{"id": "p001", "query": "What did John Smith earn?", ...}, ...]

Output format (same structure, PII replaced):
    [{"id": "p001", "query": "What did PERSON_0001 earn?", ...}, ...]

Determinism:
    - Uses a fixed salt for all substitutions.
    - The same input file always produces the same output.
    - No network calls; fully local.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import os

# Fixed salt for deterministic anonymization
FIXED_SALT = "graphrag-packet15-eval-salt-2026"

# Regex patterns for PII detection
PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Email addresses
    ("email", re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")),
    # Phone numbers (US-style variations)
    ("phone", re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")),
    # SSN-like patterns
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # Credit card-like patterns (16 digits with separators)
    ("cc", re.compile(r"\b(?:\d{4}[-.\s]?){3}\d{4}\b")),
    # IP addresses
    ("ip", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]

# Name-like patterns (simplified; in production, use a proper NER model)
# These catch capitalized words at the start of queries that look like names
NAME_PATTERN = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")


def _deterministic_hash(value: str, category: str, salt: str) -> str:
    """Generate a deterministic placeholder for a PII value.

    Format: CATEGORY_HHHH where HHHH is the first 4 hex chars of the hash.
    """
    h = hashlib.sha256(f"{salt}:{category}:{value}".encode("utf-8")).hexdigest()
    return f"{category.upper()}_{h[:4].upper()}"


def anonymize_text(text: str) -> str:
    """Anonymize PII in a single text string.

    Replaces detected PII with deterministic placeholders.
    Processing order matters: more specific patterns first.
    """
    result = text

    # Replace structured PII patterns first (emails, phones, SSNs, etc.)
    for category, pattern in PII_PATTERNS:
        def replacer(match):
            return _deterministic_hash(match.group(0), category, FIXED_SALT)
        result = pattern.sub(replacer, result)

    # Replace capitalized multi-word names (conservative — only if >2 words
    # to reduce false positives on normal text)
    def name_replacer(match):
        name = match.group(1).strip()
        # Skip if it looks like common query words
        skip_words = {"What", "How", "When", "Where", "Why", "Which", "Who",
                       "List", "Show", "Find", "Compare", "Extract", "Summarize"}
        words = name.split()
        if len(words) > 2 and words[0] not in skip_words:
            return _deterministic_hash(name, "person", FIXED_SALT)
        return match.group(0)

    result = NAME_PATTERN.sub(name_replacer, result)

    return result


def anonymize_query(query: dict) -> dict:
    """Anonymize a single query dict.

    Processes the 'query' field. Other fields are preserved as-is.
    """
    anonymized = dict(query)
    if "query" in anonymized:
        anonymized["query"] = anonymize_text(anonymized["query"])
    return anonymized


def anonymize_file(input_path: str, output_path: str) -> int:
    """Anonymize an entire query file.

    Returns the number of queries processed.
    """
    if not os.path.isfile(input_path):
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        return 0

    with open(input_path, "r", encoding="utf-8") as f:
        queries = json.load(f)

    if not isinstance(queries, list):
        print("ERROR: Input must be a JSON array of query objects", file=sys.stderr)
        return 0

    anonymized = [anonymize_query(q) for q in queries]

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(anonymized, f, indent=2, ensure_ascii=False)

    return len(anonymized)


def main():
    if len(sys.argv) < 3:
        print(
            "Usage: python3 scripts/eval_anonymize_samples.py <input.json> <output.json>",
            file=sys.stderr,
        )
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2]

    count = anonymize_file(input_path, output_path)

    print(f"Anonymized {count} queries")
    print(f"Output: {output_path}")
    print(f"Salt: {FIXED_SALT} (deterministic)")


if __name__ == "__main__":
    main()
