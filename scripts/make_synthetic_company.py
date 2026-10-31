#!/usr/bin/env python3
"""GL-07: deterministic generator for a synthetic company document set.

Why this exists
---------------
The product's purpose is cross-company insight, but only one real company is in the
store. Before a real second company exists we need a second corpus that is
*shaped* like a real one, so cross-company retrieval, attribution, scoping and
duplicate handling can be tested (readiness plan GL-07, Plane GRAG-62).

What this is NOT
----------------
It is not derived from, and does not quote, anything in ``source_docs/``,
``lightrag_store/`` or ``state/``. It never reads those paths. Every person,
number, code, document title and sentence here is invented from the catalogues in
this file. The synthetic data proves the *mechanics* (does a scoped query stay
inside one company, is a shared file attributed to both, does a citation carry a
company), not real-world answer quality.

Determinism contract
--------------------
Same ``--seed`` plus same ``--company`` plus same flags => byte-identical files.
That holds because:

1. every random choice comes from :func:`rng_for`, seeded from a string built out
   of the seed and the company and the thing being chosen - never from the global
   ``random`` state and never from wall-clock time;
2. no part carries a timestamp: OOXML ``docProps`` dates are fixed, and every zip
   entry is repacked with a fixed date, because ``python-pptx`` stamps zip entries
   with the current time (measured: two runs in the same second match, two runs a
   minute apart do not);
3. the cross-company shared file is generated from the seed ALONE, with no company
   name anywhere in it, so the same seed gives both companies the same bytes.

Document set written to ``<out>/<Company>/``
--------------------------------------------
- 8 meeting transcripts (``Speaker:`` turns, date in the filename, 3-6 speakers,
  ASR-style messiness),
- 1 DOCX with headings, a table whose first row is a real header row, and notes,
- 1 PDF: numbered headings, tables, a running header and footer repeated on every
  page, short ALL-CAPS headings, at least 40 pages,
- 1 PPTX: ~25 slides with titles, bullet lists, one table, speaker notes on some
  slides, one grouped shape,
- a duplicate pair with identical bytes inside the company,
- one file whose bytes are identical for EVERY company at the same seed (the
  cross-company duplicate test), also emittable standalone with
  ``--emit-shared-file``,
- ``manifest.json``: counts, the topic map, every planted fact with the document
  that holds it, the unanswerable facts, the duplicate map, and generated probe
  shapes (scoped / cross-company / unanswerable).

Nothing here is committed except this script and its tests; the output directory
lives outside the repo and the script refuses to write inside the repo.

Usage
-----
    .venv/bin/python scripts/make_synthetic_company.py                      # both defaults
    .venv/bin/python scripts/make_synthetic_company.py --company "Contoso Pharma"
    .venv/bin/python scripts/make_synthetic_company.py --seed 7 --out /tmp/synth
    .venv/bin/python scripts/make_synthetic_company.py --emit-shared-file /tmp/shared.txt

Exit code 0 and a counts-only JSON summary on stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
import re
import sys
import zipfile
import xml.sax.saxutils as saxutils
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# ── fixed layout of the output ────────────────────────────────────────────────
DEFAULT_OUT = "/home/maverick/work/synthetic-co"
DEFAULT_COMPANY = "Northwind Chemicals"
DEFAULT_SEED = 20261101
MANIFEST_NAME = "manifest.json"
SHARED_FILE_REL = "shared-between-companies/cutover-freeze-checklist.txt"
DUPLICATE_DIR_REL = "reference/duplicate-pair"
TRANSCRIPT_DIR_REL = "transcripts"
PDF_DIR_REL = "documents"
DOCX_DIR_REL = "documents"
PPTX_DIR_REL = "deck"
MIN_PDF_PAGES = 40
SLIDE_TARGET = 25
TRANSCRIPT_COUNT = 8
MIN_SHARED_TOPICS = 10
UNIQUE_TOPIC_COUNT = 3
FIXED_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
FIXED_DOCX_DATE = "1980-01-01T00:00:00Z"
# A fact is one sentence, and the cap exists so it stays a small number of PDF
# lines. Containment tests normalise whitespace, so a wrapped fact still matches.
MAX_FACT_CHARS = 200
UNANSWERABLE_COUNT = 6

# ── catalogues: invented, and the only source of content ──────────────────────
# Shared-process topics: the same business problem two ERP programmes both have,
# which is what makes a cross-company probe meaningful ("how did A do what B is
# now trying"). 15 here, the brief asks for at least 10.
SHARED_TOPICS: list[tuple[str, str, str]] = [
    ("order_to_cash", "order-to-cash handling", "order-to-cash workstream"),
    ("master_data_cleansing", "master-data cleansing before migration", "master-data cleansing wave"),
    ("flat_file_interfaces", "flat-file interfaces between legacy and new ERP", "flat-file interface set"),
    ("cutover_rehearsal", "cutover rehearsal and go/no-go criteria", "cutover rehearsal"),
    ("uat", "user acceptance testing", "user acceptance test cycle"),
    ("order_book_integration", "integration with a cross-business-unit order book", "cross-unit order book"),
    ("invoice_matching", "three-way match and invoice exception handling", "invoice exception queue"),
    ("role_model", "role and authorisation model redesign", "role model redesign"),
    ("archive_retention", "legacy archive and retention handover", "archive handover"),
    ("interface_monitoring", "interface monitoring and alerting ownership", "interface alert rota"),
    ("training_superusers", "training and the super-user network", "super-user network"),
    ("decommissioning", "legacy decommissioning and parallel run", "parallel-run window"),
    ("data_quality_board", "data quality governance and defect triage", "defect triage board"),
    ("plant_freeze", "production freeze windows around go-live", "production freeze window"),
    ("reporting_transition", "reporting and warehouse transition", "reporting transition"),
]

# Unique topics: object x angle, so two companies drawing from the pool rarely land
# on the same one, and a scoped probe about them has one unambiguous answer.
UNIQUE_OBJECTS: list[str] = [
    "chlorination line pilot", "safety dossier handover", "rail siding weighbridge",
    "tanker fleet telematics", "cold-chain batch release", "unit serialisation register",
    "satellite depot kiosks", "catalyst reclamation loop", "effluent consent limits",
    "blast schedule board", "sterile compounding hood", "vial traceability ledger",
    "marine bunker delivery window", "kiln refractory lifetime model",
    "variant tasting laboratory", "hoist payload telemetry", "reel automation retrofit",
    "gearbox torque arm gauge",
]
UNIQUE_ANGLES: list[str] = [
    "paper pilot registry", "dual-book trial", "night-shift shadow board",
    "weighbridge audit trail", "dual sign-off release rule", "exception queue ownership",
    "append-only batch log", "sensor calibration drift register",
]

# Invented people, chosen per company. Two companies CAN share a name (the pool is
# finite), which is realistic and harmless: attribution in this product is per
# document, not per speaker. A probe must never assume a name identifies a company,
# and the manifest lists each company's people so a probe set can be checked for
# exactly that assumption.
SPEAKER_POOL: list[str] = [
    "H. Vandermeer", "R. Okonjo", "L. Prieto", "T. Bjornsson", "M. Kazadi",
    "S. Whitlock", "A. Devereaux", "J. Nakamura", "P. Lindqvist", "C. Mbeki",
    "F. Marchetti", "D. Halvorsen", "N. Abadi", "G. Rosenthal", "K. Oyelaran",
    "E. Tancredi", "B. Sorensen", "Y. Kowalski", "I. Ferreira", "W. Chandra",
    "V. Dubois", "Q. Zhao", "O. Petrenko", "U. Adeyemi", "Z. Marique",
    "A. Novak", "R. Delacroix", "M. Tervonen",
]
ROLE_POOL: list[str] = [
    "workstream lead", "data lead", "interface architect", "test manager",
    "site coordinator", "finance key user", "programme office analyst",
    "operations supervisor", "security analyst", "records manager",
]

# Meeting titles, filler sentences, table shells: the connective tissue between
# planted facts. Deliberately flat and repetitive, like real minutes.
MEETING_SHAPES: list[str] = [
    "{topic} workstream call",
    "{topic} decision log review",
    "{topic} defect triage",
    "{topic} readiness check-in",
    "{topic} handover walkthrough",
]
FILLER_TURNS: list[str] = [
    "agenda is short today, we only need the open items",
    "the programme office wants a status line by friday",
    "we keep doing this manually and it is not a secret anyone should own",
    "can someone put that in the decision log, i keep losing it",
    "that is the third time this week we have re-litigated the same boundary",
    "we will take it offline if it needs more than two people",
    "nothing here blocks us, but it will block us in a month",
    "the vendor says it is configurable, we have not proved it",
    "we should be careful about promising a date we have not rehearsed",
    "who signs that off, that is the whole question really",
    "the room agrees, the site does not, write both down",
    "we are two weeks from the window and nobody has the script",
]
ASR_NOISE: list[str] = [
    "uh", "erm", "(unintelligible)", "(cross-talk)", "sorry, go ahead",
    "yeah, so", "right, ok", "i think the recording is still going",
]
ACTION_VERBS: list[str] = [
    "re-run", "document", "assign", "close", "escalate", "schedule", "sign off",
]
NOUNS: list[str] = [
    "defect", "open question", "interface change request", "decision log entry",
    "data slice", "rehearsal gap", "exception rule", "conversion batch",
]

# ``{topic}`` is always a bare noun phrase ("order-to-cash workstream"), so the
# article belongs to the template, never to the topic.
FACT_TEMPLATES: list[str] = [
    "The {topic} review on {date} logged {n} {noun}s and closed entry {code}.",
    "Entry {code}: the {topic} window ran {hours} hours {minutes} minutes with {n} blockers.",
    "Owners agreed on {date} that the {topic} keeps a {adj} {artefact} (entry {code}).",
    "Entry {code} sets the {topic} threshold at {n} per {unit} for {months} months.",
    "The {topic} used {n} parallel passes; the count is recorded as {code} on {date}.",
    "Sign-off for the {topic} is held under {code} by the {role} at {site}.",
]
FACT_ADJECTIVES: list[str] = ["single", "append-only", "site-level", "time-stamped", "two-part"]
FACT_ARTEFACTS: list[str] = ["register", "checklist", "log", "roster", "ledger"]
FACT_UNITS: list[str] = ["batch", "wave", "site", "interface"]
SITES: list[str] = ["the north site", "the river site", "the pilot line", "the shared service hub"]


def rng_for(seed: int, *parts: Any) -> random.Random:
    """A fresh RNG for one decision. Never use the global ``random`` module."""
    key = "|".join([str(seed), *(str(p) for p in parts)])
    return random.Random(key)


def pick(rng: random.Random, items: list[str]) -> str:
    return rng.choice(items)


def normalise_whitespace(text: str) -> str:
    """Comparison form for fact containment: line wrapping is a layout detail."""
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class CompanyProfile:
    name: str
    seed: int
    code: str
    speakers: list[str]
    roles: dict[str, str]
    base_date: date

    @classmethod
    def build(cls, name: str, seed: int) -> "CompanyProfile":
        rng = rng_for(seed, "profile", name)
        initials = "".join(word[0].upper() for word in name.split() if word[:1].isalpha())[:2] or "SC"
        code = f"{initials}-{rng.randint(1000, 9999)}"
        speaker_count = rng.randint(4, 6)
        speakers = rng.sample(SPEAKER_POOL, speaker_count)
        roles = {speaker: pick(rng, ROLE_POOL) for speaker in speakers}
        base = date(2025, 1, 6) + timedelta(days=rng.randint(0, 400))
        return cls(name=name, seed=seed, code=code, speakers=speakers, roles=roles, base_date=base)

    def on(self, *parts: Any) -> random.Random:
        return rng_for(self.seed, self.name, *parts)

    def dated(self, offset_days: int) -> date:
        return self.base_date + timedelta(days=offset_days)

    def dated_text(self, offset_days: int) -> str:
        return self.dated(offset_days).isoformat()


@dataclass
class Topic:
    id: str
    label: str
    phrase: str
    kind: str  # "shared" | "unique"


@dataclass
class Fact:
    fact_id: str
    code: str
    text: str
    topic: str
    kind: str
    document: str = ""

    def as_manifest_entry(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "code": self.code,
            "text": self.text,
            "topic": self.topic,
            "kind": self.kind,
            "document": self.document,
        }


def build_topics(profile: CompanyProfile, forbidden_unique: list[str]) -> list[Topic]:
    """Shared-process topics plus UNIQUE_TOPIC_COUNT topics unique to this company."""
    topics = [Topic(tid, label, phrase, "shared") for tid, label, phrase in SHARED_TOPICS]
    rng = profile.on("unique-topics")
    objects = [obj for obj in UNIQUE_OBJECTS if f"unique:{obj}" not in forbidden_unique]
    angles = list(UNIQUE_ANGLES)
    rng.shuffle(objects)
    rng.shuffle(angles)
    chosen_objects = objects[:UNIQUE_TOPIC_COUNT]
    if len(chosen_objects) < UNIQUE_TOPIC_COUNT:
        raise SystemExit(
            f"unique-topic pool exhausted for {profile.name!r}: "
            f"{len(chosen_objects)} of {UNIQUE_TOPIC_COUNT} available after "
            f"{len(forbidden_unique)} --forbid-unique-topic exclusions"
        )
    for index, obj in enumerate(chosen_objects):
        angle = angles[index % len(angles)]
        label = f"{obj} - {angle}"
        topics.append(Topic(f"unique:{obj}", label, f"{obj} ({angle})", "unique"))
    return topics


DOCX_TOPICS = {
    "order_to_cash", "order_book_integration", "flat_file_interfaces",
    "invoice_matching", "master_data_cleansing",
}
PPTX_TOPICS = {
    "cutover_rehearsal", "uat", "training_superusers", "plant_freeze", "role_model",
}


def fact_home(profile: CompanyProfile, topic: Topic, fact_index: int,
              transcript_files: list[str], slots: dict[str, int]) -> str:
    """Which document holds this fact. A fact lives in exactly one document.

    Fact 1 of every topic is spoken in a transcript (so transcripts carry the
    attributed, quotable evidence). Fact 2 goes to the structured document that
    owns the topic. Unique topics get a third fact in the PDF, which is where a
    scoped probe should also be able to find them.
    """
    if fact_index == 0:
        return transcript_files[slots[topic.id]]
    if fact_index == 1:
        if topic.id in DOCX_TOPICS:
            return "docx"
        if topic.id in PPTX_TOPICS:
            return "pptx"
        return "pdf"
    return "pdf"


def hash_topic(topic_id: str) -> int:
    # Deterministic across runs and machines; Python's hash() is salted per process.
    return int(hashlib.sha256(topic_id.encode("utf-8")).hexdigest()[:8], 16)


def build_facts(profile: CompanyProfile, topics: list[Topic], transcript_files: list[str],
                slots: dict[str, int]) -> list[Fact]:
    facts: list[Fact] = []
    for topic_number, topic in enumerate(topics, start=1):
        fact_count = 3 if topic.kind == "unique" else 2
        topic_code = f"T{topic_number:02d}"
        for index in range(fact_count):
            rng = profile.on("fact", topic.id, index)
            code = f"{profile.code}-{topic_code}-{index + 1:02d}"
            template = FACT_TEMPLATES[(hash_topic(topic.id) + index) % len(FACT_TEMPLATES)]
            text = template.format(
                topic=topic.phrase,
                date=profile.dated_text(rng.randint(20, 620)),
                n=rng.randint(2, 47),
                noun=pick(rng, NOUNS),
                code=code,
                hours=rng.randint(3, 26),
                minutes=rng.choice([0, 10, 20, 25, 35, 40, 55]),
                adj=pick(rng, FACT_ADJECTIVES),
                artefact=pick(rng, FACT_ARTEFACTS),
                unit=pick(rng, FACT_UNITS),
                months=rng.randint(2, 18),
                role=pick(rng, ROLE_POOL),
                site=pick(rng, SITES),
            )
            if len(text) > MAX_FACT_CHARS:
                raise SystemExit(f"fact template produced text longer than {MAX_FACT_CHARS} chars: {text!r}")
            facts.append(Fact(
                fact_id=f"{profile.code}-{len(facts) + 1:03d}",
                code=code,
                text=text,
                topic=topic.id,
                kind=topic.kind,
                document=fact_home(profile, topic, index, transcript_files, slots),
            ))
    return facts


def build_unanswerable_facts(profile: CompanyProfile) -> list[dict[str, str]]:
    """Facts that exist in NO document of ANY company: refusal probes.

    They are generated from a separate RNG stream and use codes that could not
    collide with a planted fact, so an answer that cites one is inventing evidence.
    """
    out: list[dict[str, str]] = []
    for index in range(UNANSWERABLE_COUNT):
        rng = profile.on("unanswerable", index)
        topic_id, label, phrase = rng.choice(SHARED_TOPICS)
        code = f"{profile.code}-NA-{index + 1:02d}"
        text = (
            f"No record exists for {code}: the {phrase} was never measured in "
            f"{rng.randint(2, 9)} of the {rng.randint(10, 30)} reviews."
        )
        out.append({
            "fact_id": f"{profile.code}-NA-{index + 1:02d}",
            "code": code,
            "text": text,
            "topic": topic_id,
            "kind": "unanswerable",
            "document": "",
            "why": "invented for refusal probes; written into no document of any company",
        })
    return out


# ── transcripts ───────────────────────────────────────────────────────────────
def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "meeting"


def topic_slot_map(topics: list[Topic], profile: CompanyProfile) -> dict[str, int]:
    """Which transcript each topic speaks in.

    A shuffled permutation, not a hash: hashing can leave a meeting with nothing to
    report on, and then the meeting's title would be about a topic it never mentions.
    A permutation over ``TRANSCRIPT_COUNT`` buckets gives every meeting two or three
    topics for any topic count >= TRANSCRIPT_COUNT.
    """
    rng = profile.on("transcript-assignment")
    order = list(range(len(topics)))
    rng.shuffle(order)
    return {topic.id: order[index] % TRANSCRIPT_COUNT for index, topic in enumerate(topics)}


def build_transcript_plan(profile: CompanyProfile, topics: list[Topic],
                          slots: dict[str, int]) -> list[dict[str, Any]]:
    """Eight transcripts: a date in the filename, an invented meeting title.

    A transcript is named after a topic that actually speaks in it, so the title and
    the register entries inside it agree. Several topics share one meeting, which is
    what a real workstream call looks like.
    """
    primary: dict[int, Topic] = {}
    for topic in topics:
        primary.setdefault(slots[topic.id], topic)
    plan: list[dict[str, Any]] = []
    for index in range(TRANSCRIPT_COUNT):
        rng = profile.on("transcript", index)
        shape = MEETING_SHAPES[(index * 3 + 1) % len(MEETING_SHAPES)]
        topic = primary.get(index) or topics[index % len(topics)]
        title = shape.format(topic=topic.label)
        offset = 30 + index * 37 + rng.randint(0, 20)
        plan.append({
            "index": index,
            "title": title,
            "path": f"{TRANSCRIPT_DIR_REL}/{profile.dated_text(offset)} {slugify(title)}.txt",
            "topic": topic.id,
            "offset_days": offset,
        })
    return plan


def render_transcript(profile: CompanyProfile, entry: dict[str, Any], facts: list[Fact]) -> str:
    """A meeting transcript: ``Speaker: text`` turns, ASR-style messiness."""
    rng = profile.on("transcript-render", entry["index"])
    speakers = rng.sample(profile.speakers, rng.randint(3, min(6, len(profile.speakers))))
    turns: list[str] = []
    queue = list(facts)
    rng.shuffle(queue)
    turn_count = rng.randint(14, 24)
    for turn_index in range(turn_count):
        speaker = speakers[turn_index % len(speakers)]
        if queue and (turn_index % 2 == 1 or turn_index >= turn_count - len(queue)):
            fact = queue.pop(0) if queue else None
            if fact is not None:
                lead = pick(rng, [
                    "i have the entry in front of me,",
                    "reading from the log,",
                    "it says, and i quote,",
                    "the decision line is this:",
                ])
                turns.append(f"{speaker}: {lead} {fact.text}")
                continue
        if turn_index % 5 == 4:
            noise = pick(rng, ASR_NOISE)
            turns.append(f"{speaker}: {noise}")
        else:
            body = pick(rng, FILLER_TURNS)
            verb = pick(rng, ACTION_VERBS)
            noun = pick(rng, NOUNS)
            extra = rng.choice([
                "",
                f" we will {verb} the {noun} and report next week",
                f" {pick(rng, ROLE_POOL)} has it, not me",
            ])
            turns.append(f"{speaker}: {body}{extra}")
    header = [
        entry["title"],
        f"date: {profile.dated_text(entry['offset_days'])}",
        f"attendees: {', '.join(sorted(speakers))}",
        "transcript: automatic transcription, uncorrected",
    ]
    return "\n".join(header) + "\n\n" + "\n\n".join(turns) + "\n"


# ── the file that is identical for every company ──────────────────────────────
def build_shared_file_bytes(seed: int) -> bytes:
    """Bytes for the cross-company duplicate test.

    Deliberately seeded from ``seed`` alone: no company name, no company-specific
    number, no timestamp. That is what lets the same file sit in two companies and
    still be one set of bytes, which is the only way to test attribution of a
    shared document (LightRAG indexes identical bytes once, store-wide).
    """
    rng = rng_for(seed, "shared-file")
    lines = [
        "CUTOVER FREEZE CHECKLIST",
        "Programme-wide document. This page is identical in every company's set.",
        "",
    ]
    for index in range(1, 15):
        item = pick(rng, [
            "no new master-data edits in the freeze window",
            "interface queue drained and confirmed empty",
            "conversion dry-run log archived and referenced",
            "go/no-go board convened with both site leads present",
            "rollback script rehearsed within the window, not assumed",
            "training completion list signed by the site",
            "open defects re-triaged and none blocking",
            "reporting reconciliation sample agreed before the window",
        ])
        owner = pick(rng, ROLE_POOL)
        lines.append(f"{index:02d}. {item} - owner: {owner} - evidence: log reference required")
    lines += [
        "",
        f"checklist revision {rng.randint(2, 9)}.{rng.randint(0, 9)}",
        "end of checklist",
        "",
    ]
    return ("\n".join(lines)).encode("utf-8")


def build_duplicate_pair_bytes(profile: CompanyProfile) -> bytes:
    """One document, stored twice inside the same company (in-company duplicate)."""
    rng = profile.on("duplicate-pair")
    lines = [
        "INBOX SCANNING PROCEDURE (REFERENCE COPY)",
        f"Company: {profile.name}",
        "",
        "1. scan every paper invoice into the shared inbox at the site that opened it",
        "2. attach the goods-receipt reference before the scan leaves the site",
        "3. the shared service hub matches it, and only it, against the purchase order",
        "4. anything unmatched for more than five working days is escalated by the hub",
        "",
        f"exception limit: {rng.randint(3, 12)} percent of monthly volume",
        f"review cycle: every {rng.randint(2, 6)} months, minuted",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


# ── DOCX (written directly: no dependency, fixed dates, real header row) ─────
def _xml_escape(text: str) -> str:
    return saxutils.escape(text, {'"': "&quot;"})


def docx_paragraph(text: str, style: str | None = None) -> str:
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    if not text:
        return f"<w:p>{props}</w:p>"
    return (
        f"<w:p>{props}<w:r><w:t xml:space=\"preserve\">{_xml_escape(text)}</w:t></w:r></w:p>"
    )


def docx_table(rows: list[list[str]]) -> str:
    parts = ['<w:tbl><w:tblPr><w:tblStyle w:val="Table"/></w:tblPr>']
    parts.append(f"<w:tblGrid>{'<w:gridCol w:w=\"2160\"/>' * len(rows[0])}</w:tblGrid>")
    for row_index, row in enumerate(rows):
        header = "<w:trPr><w:tblHeader/></w:trPr>" if row_index == 0 else ""
        cells = []
        for cell in row:
            cells.append(
                "<w:tc><w:tcPr><w:tcW w:w=\"2160\" w:type=\"dxa\"/></w:tcPr>"
                f"{docx_paragraph(cell)}</w:tc>"
            )
        parts.append(f"<w:tr>{header}{''.join(cells)}</w:tr>")
    parts.append("</w:tbl>")
    return "".join(parts)


def build_docx_bytes(profile: CompanyProfile, sections: list[tuple[str, list[str]]],
                     tables: list[tuple[str, list[list[str]]]], notes: list[str]) -> bytes:
    body: list[str] = [docx_paragraph(profile.name + " - order book interface specification", "Title")]
    for heading, paragraphs in sections:
        body.append(docx_paragraph(heading, "Heading1"))
        for paragraph in paragraphs:
            body.append(docx_paragraph(paragraph))
    for caption, rows in tables:
        body.append(docx_paragraph(caption, "Heading2"))
        body.append(docx_table(rows))
    body.append(docx_paragraph("Notes", "Heading1"))
    for note in notes:
        body.append(docx_paragraph(note))
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(body)}<w:sectPr><w:pgSz w:w=\"12240\" w:h=\"15840\"/>"
        "<w:pgMar w:top=\"1134\" w:right=\"1134\" w:bottom=\"1134\" w:left=\"1134\"/></w:sectPr>"
        "</w:body></w:document>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:style w:styleId="Title" w:type="paragraph"><w:name w:val="Title"/></w:style>'
        '<w:style w:styleId="Heading1" w:type="paragraph"><w:name w:val="heading 1"/>'
        '<w:basedOn w:val="Normal"/><w:pPr><w:outlineLvl w:val="0"/></w:pPr>'
        '<w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>'
        '<w:style w:styleId="Heading2" w:type="paragraph"><w:name w:val="heading 2"/>'
        '<w:basedOn w:val="Normal"/><w:pPr><w:outlineLvl w:val="1"/></w:pPr>'
        '<w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style>'
        '<w:style w:styleId="Table" w:type="table"><w:name w:val="Table Grid"/></w:style>'
        "</w:styles>"
    )
    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{_xml_escape(profile.name)} order book interface specification</dc:title>"
        f"<dc:creator>{_xml_escape(profile.name)} programme office</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{FIXED_DOCX_DATE}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{FIXED_DOCX_DATE}</dcterms:modified>'
        "</cp:coreProperties>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        "</Relationships>"
    )
    document_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    return zip_bytes({
        "[Content_Types].xml": content_types,
        "_rels/.rels": root_rels,
        "word/document.xml": document,
        "word/styles.xml": styles,
        "word/_rels/document.xml.rels": document_rels,
        "docProps/core.xml": core,
    })


def zip_bytes(parts: dict[str, str]) -> bytes:
    """A zip with fixed entry dates. Zip entry times are why two runs differed."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, payload.encode("utf-8"))
    return buffer.getvalue()


# ── PDF (written directly: pypdf and poppler both read it, no dependency) ────
PAGE_WIDTH = 612.0
PAGE_HEIGHT = 792.0
PAGE_MARGIN = 54.0
BODY_TOP = 706.0
BODY_BOTTOM = 76.0
CHAR_WIDTH_RATIO = 0.52          # conservative average glyph width for Helvetica
PDF_FONTS = {"F1": "Helvetica", "F2": "Helvetica-Bold"}


@dataclass
class PdfLine:
    text: str
    font: str = "F1"
    size: float = 10.5
    gap_before: float = 0.0


def pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def pdf_max_chars(size: float) -> int:
    usable = PAGE_WIDTH - 2 * PAGE_MARGIN
    return max(20, int(usable / (size * CHAR_WIDTH_RATIO)))


def wrap_text(text: str, size: float) -> list[str]:
    limit = pdf_max_chars(size)
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    length = 0
    for word in words:
        word_length = len(word) + (1 if current else 0)
        if length + word_length > limit:
            lines.append(" ".join(current))
            current = [word]
            length = len(word)
        else:
            current.append(word)
            length += word_length
    if current:
        lines.append(" ".join(current))
    return lines


class PdfPaginator:
    """Collects lines and breaks pages. Headers and footers are added per page."""

    def __init__(self) -> None:
        self.pages: list[list[PdfLine]] = [[]]
        self.current: list[PdfLine] = []
        self.y = BODY_TOP

    def add(self, line: PdfLine) -> None:
        leading = line.size * 1.32 + line.gap_before
        if self.y - leading < BODY_BOTTOM and self.current:
            self.flush()
            leading = line.size * 1.32
        self.current.append(line)
        self.y -= leading

    def add_heading(self, text: str, size: float = 12.5) -> None:
        self.add(PdfLine(text=text.upper(), font="F2", size=size, gap_before=8.0))

    def add_paragraph(self, text: str, size: float = 10.5) -> None:
        for index, chunk in enumerate(wrap_text(text, size)):
            self.add(PdfLine(text=chunk, size=size, gap_before=4.0 if index == 0 else 0.0))

    def add_rows(self, rows: list[list[str]], size: float = 9.5) -> None:
        for index, row in enumerate(rows):
            joined = "  |  ".join(cell if cell else "-" for cell in row)
            self.add(PdfLine(text=joined, font="F2" if index == 0 else "F1", size=size,
                             gap_before=5.0 if index == 0 else 0.0))

    def flush(self) -> None:
        self.pages.append(self.current)
        self.current = []
        self.y = BODY_TOP

    def finish(self) -> list[list[PdfLine]]:
        self.flush()
        return [page for page in self.pages if page]


def build_pdf_bytes(profile: CompanyProfile, chapters: list[dict[str, Any]]) -> bytes:
    """Numbered headings, short ALL-CAPS headings, tables, running header/footer."""
    paginator = PdfPaginator()
    paginator.add(PdfLine(text=profile.name, font="F2", size=20.0))
    paginator.add(PdfLine(text="ERP integration analysis", font="F2", size=14.0, gap_before=10.0))
    paginator.add(PdfLine(text=f"Programme document {profile.code}-INT-04, revision 4", size=11.0, gap_before=14.0))
    paginator.add(PdfLine(text=f"Issued {profile.dated_text(640)} by the programme office", size=11.0))
    paginator.add_paragraph(
        "This document describes how the programme prepared, rehearsed and cut over its "
        "business processes. It is an invented document produced by a test generator; no "
        "person, number or site in it refers to any real programme.", size=11.0)
    paginator.add_heading("contents", size=13.0)
    for chapter in chapters:
        paginator.add(PdfLine(text=f"{chapter['number']}.  {chapter['title']}", size=10.5))
    for chapter in chapters:
        paginator.add_heading(f"{chapter['number']}.  {chapter['title']}", size=13.0)
        for block in chapter["blocks"]:
            kind = block["kind"]
            if kind == "subheading":
                paginator.add_heading(block["text"], size=11.0)
            elif kind == "table":
                paginator.add_rows(block["rows"])
            else:
                paginator.add_paragraph(block["text"])
    # Review log appendix: a plausible document part, and the honest way to reach
    # the 40-page floor the brief asks for without inventing more prose than needed.
    while len(paginator.pages) + (1 if paginator.current else 0) < MIN_PDF_PAGES + 2:
        batch = len(paginator.pages)
        paginator.add_heading(f"appendix b.{batch}.  review log", size=11.0)
        rows = [["item", "reviewed", "reviewer", "outcome"]]
        for row_index in range(1, 22):
            rng = profile.on("review-log", batch, row_index)
            rows.append([
                f"{profile.code}-R{batch:02d}{row_index:02d}",
                profile.dated_text(40 + rng.randint(0, 500)),
                pick(rng, ROLE_POOL),
                rng.choice(["accepted", "accepted with note", "returned", "deferred to board"]),
            ])
        paginator.add_rows(rows)
    pages = paginator.finish()
    if len(pages) < MIN_PDF_PAGES:
        raise SystemExit(f"generated PDF has {len(pages)} pages, below the {MIN_PDF_PAGES} page floor")
    total = len(pages)
    header_left = f"{profile.name} - ERP integration analysis"
    header_right = f"{profile.code}-INT-04 rev 4"
    body: list[list[PdfLine]] = []
    for index, page in enumerate(pages, start=1):
        framed = [
            PdfLine(text=header_left, font="F2", size=9.0),
            PdfLine(text=header_right, size=9.0),
            PdfLine(text="", size=6.0),
        ] + page + [
            PdfLine(text="", size=6.0),
            PdfLine(text=f"page {index} of {total}", size=9.0),
            PdfLine(text="uncontrolled when printed", size=9.0),
        ]
        body.append(framed)
    return write_pdf_object(body)


def write_pdf_object(pages: list[list[PdfLine]]) -> bytes:
    """Minimal but valid PDF: catalog, page tree, two base-14 fonts, page streams."""
    page_count = len(pages)
    first_page_object = 5
    kids = " ".join(f"{first_page_object + 2 * index} 0 R" for index in range(page_count))
    objects: list[str] = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Count {page_count} /Kids [{kids}] >>",
        f"<< /Type /Font /Subtype /Type1 /BaseFont /{PDF_FONTS['F1']} /Encoding /WinAnsiEncoding >>",
        f"<< /Type /Font /Subtype /Type1 /BaseFont /{PDF_FONTS['F2']} /Encoding /WinAnsiEncoding >>",
    ]
    for page in pages:
        stream_lines = ["BT"]
        y = BODY_TOP
        for line in page:
            leading = line.size * 1.32 + line.gap_before
            y -= leading
            if line.text:
                stream_lines.append(
                    f"/{line.font} {line.size:g} Tf 1 0 0 1 {PAGE_MARGIN:g} {y:g} Tm "
                    f"({pdf_escape(line.text)}) Tj"
                )
        stream_lines.append("ET")
        stream = "\n".join(stream_lines)
        page_object = len(objects) + 1
        content_object = page_object + 1
        objects.append(
            "<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 {PAGE_WIDTH:g} {PAGE_HEIGHT:g}] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
            f"/Contents {content_object} 0 R >>"
        )
        objects.append(f"<< /Length {len(stream.encode('utf-8'))} >>\nstream\n{stream}\nendstream")
    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets: list[int] = [len(header)]
    buffer = bytearray(header)
    for number, payload in enumerate(objects, start=1):
        offsets.append(len(buffer))
        buffer += f"{number} 0 obj\n{payload}\nendobj\n".encode("utf-8")
    xref_position = len(buffer)
    count = len(objects) + 1
    buffer += f"xref\n0 {count}\n".encode("utf-8")
    buffer += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        buffer += f"{offset:010d} 00000 n \n".encode("utf-8")
    buffer += (
        f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_position}\n%%EOF\n"
    ).encode("utf-8")
    return bytes(buffer)


# ── PPTX (python-pptx, then repacked so the bytes do not depend on the clock) ─
def build_deck_spec(profile: CompanyProfile, topics: list[Topic],
                    facts: list[Fact]) -> list[dict[str, Any]]:
    """25 slides: title, three section dividers, one slide per topic, table, group, asks.

    The facts whose document is the deck are spoken here, as a bullet on their topic
    slide and (for some slides) again in the speaker notes, so the manifest's
    fact -> document map is true for the deck as well as for the text files.
    """
    by_topic: dict[str, list[Fact]] = {}
    for fact in facts:
        if fact.document == "pptx":
            by_topic.setdefault(fact.topic, []).append(fact)
    slides: list[dict[str, Any]] = [{  # called before the sentinels are resolved
        "kind": "title",
        "title": f"{profile.name}: cutover readiness review",
        "subtitle": f"programme office review {profile.dated_text(560)}",
        "notes": "Open by naming the three gates. Do not read the slide.",
    }]
    group_size = 6
    for group_index in range((len(topics) + group_size - 1) // group_size):
        slides.append({
            "kind": "section",
            "title": f"workstreams {group_index * group_size + 1}-"
                     f"{min(len(topics), (group_index + 1) * group_size)}",
            "notes": "", "bullets": [],
        })
    for index, topic in enumerate(topics):
        rng = profile.on("deck", topic.id)
        bullets = [
            f"{topic.label}: one owner named, one backup named",
            f"entry {profile.code}-D{index + 1:02d} carries the decision",
            pick(rng, [
                "the exception path is still manual",
                "two sites asked for a later window",
                "the interface is not monitored yet",
                "the key user left, the backup is untrained",
                "the register is complete for wave one only",
            ]),
            pick(rng, [
                "next step: rehearse it once, end to end",
                "next step: name the owner in the board log",
                "next step: write the acceptance note",
            ]),
        ]
        notes: list[str] = []
        if index % 3 == 0:
            notes.append("Read the entry number, not the bullet.")
        for fact in by_topic.get(topic.id, []):
            bullets.insert(2, fact.text)
            if index % 3 == 0:
                notes.append(f"If asked, quote the register line: {fact.text}")
        slides.append({
            "kind": "bullets",
            "title": f"{index + 1}. {topic.label}",
            "bullets": bullets,
            "notes": " ".join(notes),
        })
    table_rows = [["wave", "window", "gate", "owner"]]
    for row_index in range(1, 6):
        rng = profile.on("deck-table", row_index)
        table_rows.append([
            f"W{row_index}",
            profile.dated_text(500 + row_index * 14),
            rng.choice(["go", "conditional", "no-go until rehearsed"]),
            pick(rng, ROLE_POOL),
        ])
    slides.append({"kind": "table", "title": "Wave plan and gates", "table": table_rows,
                   "notes": "The conditional wave is the one to challenge."})
    slides.append({
        "kind": "group",
        "title": "Decision path",
        "group": [
            "board convenes with both site leads present",
            "the go/no-go note is signed before the window opens, not after",
        ],
        "notes": "",
    })
    slides.append({"kind": "bullets", "title": "Asks of the board",
                   "bullets": ["confirm the wave windows", "name the exception owner",
                               "accept the rehearsal evidence as sufficient"], "notes": ""})
    return slides


def build_pptx_bytes(profile: CompanyProfile, slides: list[dict[str, Any]]) -> bytes:
    try:
        from pptx import Presentation
        from pptx.util import Inches
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            "python-pptx is required to generate the deck "
            "(it is a declared dependency in requirements.txt)"
        ) from exc
    deck = Presentation()
    for spec in slides:
        if spec["kind"] == "title":
            slide = deck.slides.add_slide(deck.slide_layouts[0])
            slide.shapes.title.text = spec["title"]
            slide.placeholders[1].text = spec["subtitle"]
        elif spec["kind"] == "table":
            slide = deck.slides.add_slide(deck.slide_layouts[6])
            if slide.shapes.title is not None:
                slide.shapes.title.text = spec["title"]
            rows = len(spec["table"])
            columns = len(spec["table"][0])
            table = slide.shapes.add_table(
                rows, columns, Inches(0.6), Inches(1.6), Inches(8.2), Inches(0.4) * rows
            ).table
            for row_index, row in enumerate(spec["table"]):
                for column_index, cell in enumerate(row):
                    table.cell(row_index, column_index).text = cell
        elif spec["kind"] == "group":
            slide = deck.slides.add_slide(deck.slide_layouts[6])
            if slide.shapes.title is not None:
                slide.shapes.title.text = spec["title"]
            group = slide.shapes.add_group_shape()
            for offset, text in enumerate(spec["group"]):
                box = group.shapes.add_textbox(Inches(0.8), Inches(1.8 + 0.9 * offset),
                                               Inches(7.4), Inches(0.7))
                box.text_frame.text = text
        else:
            layout_index = 1 if spec["kind"] == "bullets" else 5  # 1 = title+content, 5 = title only
            slide = deck.slides.add_slide(deck.slide_layouts[layout_index])
            slide.shapes.title.text = spec["title"]
            if spec.get("bullets"):
                # ``x in slide.placeholders`` is NOT a key test in python-pptx (the
                # mapping iterates shape objects), so index it and catch the miss.
                try:
                    body = slide.placeholders[1]
                except KeyError:
                    body = None
                if body is not None:
                    body.text = "\n".join(spec["bullets"])
        if spec.get("notes"):
            slide.notes_slide.notes_text_frame.text = spec["notes"]
    if abs(len(deck.slides) - SLIDE_TARGET) > 3:
        raise SystemExit(
            f"deck has {len(deck.slides)} slides, expected about {SLIDE_TARGET}"
        )
    buffer = io.BytesIO()
    deck.save(buffer)
    return repack_deterministically(buffer.getvalue())


def repack_deterministically(payload: bytes) -> bytes:
    """Rewrite an OOXML zip with fixed entry dates.

    python-pptx writes each entry with the current time, so an otherwise identical
    deck hashes differently between runs. Content and order are untouched.
    """
    source = zipfile.ZipFile(io.BytesIO(payload))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            rewritten = zipfile.ZipInfo(info.filename, date_time=FIXED_ZIP_DATE)
            rewritten.compress_type = zipfile.ZIP_DEFLATED
            rewritten.external_attr = info.external_attr
            target.writestr(rewritten, source.read(info.filename))
    return buffer.getvalue()


# ── structured-document prose ─────────────────────────────────────────────────
PDF_PARAGRAPHS: list[str] = [
    "The process was documented as it was actually run, not as the procedure says it is run, "
    "and the difference turned out to be the main source of defects.",
    "Two sites ran the same step differently for a year and neither was wrong on paper; the "
    "conversion script had to carry both variants until the wave closed.",
    "The handover to the shared service hub is agreed in writing, but the exception path is "
    "still performed by whoever notices the queue first.",
    "Evidence is kept as a dated entry in the programme register, with one owner named and one "
    "backup named, because entries without an owner were reopened three times in the last wave.",
    "Monitoring exists for the interface itself and not for what the interface produced, which "
    "is the distinction the operations team keeps asking us to close.",
    "The rehearsal is the only artefact everyone trusts, and it is the one nobody wants to spend "
    "a weekend on again.",
    "Where a threshold was argued, it was written down with the argument attached, so the next "
    "wave can disagree with a record instead of with a memory.",
    "The board asked for one number per workstream and each workstream answered with a different "
    "definition of ready.",
    "Retention rules were settled with the records team rather than assumed from the legacy "
    "system, which moved two data groups out of scope and one into legal hold.",
    "Training completion is measured at the site, by name, and the list is the artefact the go-live "
    "sign-off points at.",
]
PDF_SUBHEADINGS: list[str] = ["position today", "what changed", "evidence and entries", "open risk"]

DOCX_SECTIONS: list[tuple[str, list[str]]] = [
    ("Purpose", [
        "This specification describes how order book records move between the legacy systems and "
        "the new ERP, and who owns each rule.",
        "It is an invented document produced by a test generator. No company, site, field or "
        "figure in it refers to any real programme.",
    ]),
    ("Scope of the interface", [
        "The interface carries header and line records for cross-business-unit orders, the pricing "
        "conditions attached to them, and the delivery commitment agreed with the customer.",
        "Credit blocks and rebate accruals are carried in a separate file, and that separation is "
        "the source of most of the open questions in the register.",
    ]),
    ("Rules agreed at the last review", [
        "One file per business unit per day, sequenced, with a trailer row counting the lines.",
        "A rejected line is returned in the same batch, never in a later one.",
        "The order book owner at the unit reconciles the trailer count before the batch is closed.",
    ]),
]
DOCX_TABLE_CAPTION = "Field mapping and ownership"
DOCX_TABLE_HEADER = ["field", "legacy source", "new ERP target", "rule", "owner"]
DOCX_FIELD_ROWS: list[list[str]] = [
    ["order number", "legacy order file", "sales document", "prefix stripped, kept unique", "unit order book owner"],
    ["customer code", "customer master", "customer", "one code per legal entity", "master data lead"],
    ["material code", "plant item list", "material", "converted by the site mapping table", "site coordinator"],
    ["quantity", "order line", "item quantity", "unit of measure validated, not converted", "finance key user"],
    ["price condition", "price sheet", "condition record", "effective date carried explicitly", "pricing analyst"],
    ["delivery date", "plan of record", "schedule line", "commitment copied, not recalculated", "supply planner"],
    ["business unit", "branch code", "sales organisation", "mapped once, reviewed yearly", "programme office analyst"],
    ["credit block", "manual flag", "credit status", "not carried; separate interface", "credit analyst"],
    ["rebate accrual", "monthly extract", "condition supplement", "monthly, after close", "finance key user"],
    ["order reason", "type code", "reason code", "blank rejected, never defaulted", "unit order book owner"],
]
DOCX_NOTES: list[str] = [
    "Note 1: the trailer row count is the reconciliation control; a batch closed without it is rejected.",
    "Note 2: a rejected line is returned in the same batch; this rule was agreed after the second wave.",
    "Note 3: the credit block stays outside this interface on purpose, and the exception queue owns it.",
    "Note 4: the mapping table is versioned with the wave number, not with the file name.",
    "Note 5: any change to a rule needs a register entry before the interface is touched.",
]


def docx_field_rows_for(profile: CompanyProfile) -> list[list[str]]:
    rows = [DOCX_TABLE_HEADER]
    for index, row in enumerate(DOCX_FIELD_ROWS):
        rng = profile.on("docx-row", index)
        rows.append(row[:4] + [rng.choice(profile.speakers) if rng.random() < 0.35 else row[4]])
    return rows


def build_pdf_chapters(profile: CompanyProfile, topics: list[Topic],
                       facts: list[Fact]) -> list[dict[str, Any]]:
    by_topic: dict[str, list[Fact]] = {}
    for fact in facts:
        if fact.document == "pdf":
            by_topic.setdefault(fact.topic, []).append(fact)
    chapters: list[dict[str, Any]] = []
    for number, topic in enumerate(topics, start=1):
        rng = profile.on("pdf-chapter", topic.id)
        blocks: list[dict[str, Any]] = []
        for sub_index, sub in enumerate(PDF_SUBHEADINGS):
            if sub_index == 2:
                for fact in by_topic.get(topic.id, []):
                    blocks.append({"kind": "paragraph", "text": fact.text})
            blocks.append({"kind": "subheading", "text": sub})
            for _ in range(rng.randint(1, 2)):
                blocks.append({"kind": "paragraph", "text": pick(rng, PDF_PARAGRAPHS)})
            if sub_index == 3 and topic.kind == "unique":
                blocks.append({"kind": "paragraph", "text": (
                    f"This section is specific to {profile.name}; no other company in this data set "
                    f"describes {topic.label}."
                )})
        if number % 4 == 0:
            rows = [["entry", "date", "owner", "state"]]
            for row_index in range(1, 7):
                rows.append([
                    f"{profile.code}-T{number:02d}{row_index:02d}",
                    profile.dated_text(rng.randint(10, 640)),
                    pick(rng, ROLE_POOL),
                    rng.choice(["closed", "open", "watch"]),
                ])
            blocks.insert(1, {"kind": "table", "rows": rows})
        chapters.append({"number": number, "title": topic.label, "blocks": blocks})
    return chapters


# ── generation ────────────────────────────────────────────────────────────────
@dataclass
class GeneratedFile:
    path: str
    kind: str
    sha256: str
    bytes: int
    topics: list[str] = field(default_factory=list)
    fact_ids: list[str] = field(default_factory=list)

    def as_manifest_entry(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "topics": sorted(set(self.topics)),
            "fact_ids": sorted(self.fact_ids),
        }


def generate_company(company: str, seed: int, out_root: Path,
                     forbidden_unique: list[str] | None = None) -> tuple[Path, dict[str, Any]]:
    """Build one company's document set in memory, write it, and return the manifest."""
    forbidden_unique = forbidden_unique or []
    profile = CompanyProfile.build(company, seed)
    topics = build_topics(profile, forbidden_unique)
    slots = topic_slot_map(topics, profile)
    transcript_plan = build_transcript_plan(profile, topics, slots)
    transcript_paths = [entry["path"] for entry in transcript_plan]
    facts = build_facts(profile, topics, transcript_paths, slots)
    facts_by_document: dict[str, list[Fact]] = {}
    for fact in facts:
        facts_by_document.setdefault(fact.document, []).append(fact)

    docx_rel = f"{DOCX_DIR_REL}/{slugify(company)}-order-book-interface-specification.docx"
    pdf_rel = f"{PDF_DIR_REL}/{slugify(company)}-integration-analysis.pdf"
    pptx_rel = f"{PPTX_DIR_REL}/{slugify(company)}-cutover-readiness-review.pptx"
    duplicate_a_rel = f"{DUPLICATE_DIR_REL}/inbox-scanning-procedure-copy-a.txt"
    duplicate_b_rel = f"{DUPLICATE_DIR_REL}/inbox-scanning-procedure-copy-b.txt"

    payloads: dict[str, bytes] = {}
    file_index: dict[str, GeneratedFile] = {}

    for entry in transcript_plan:
        payload = render_transcript(profile, entry, facts_by_document.get(entry["path"], [])).encode("utf-8")
        payloads[entry["path"]] = payload
        file_index[entry["path"]] = GeneratedFile(
            path=entry["path"], kind="transcript", sha256=hashlib.sha256(payload).hexdigest(),
            bytes=len(payload), topics=[entry["topic"]],
            fact_ids=[f.fact_id for f in facts_by_document.get(entry["path"], [])],
        )

    pdf_facts = facts_by_document.get("pdf", [])
    docx_facts = facts_by_document.get("docx", [])
    pptx_facts = facts_by_document.get("pptx", [])

    sections = [(heading, list(paragraphs)) for heading, paragraphs in DOCX_SECTIONS]
    topic_labels = {topic.id: topic.label for topic in topics}
    docx_paragraph_facts = [
        f"Register entry ({fact.code}) for the {topic_labels[fact.topic]}: {fact.text}"
        for fact in docx_facts
    ]
    if docx_paragraph_facts:
        sections.append(("Register entries held by this specification", docx_paragraph_facts))
    docx_payload = build_docx_bytes(profile, sections,
                                    [(DOCX_TABLE_CAPTION, docx_field_rows_for(profile))],
                                    DOCX_NOTES)
    payloads[docx_rel] = docx_payload
    file_index[docx_rel] = GeneratedFile(
        path=docx_rel, kind="docx", sha256=hashlib.sha256(docx_payload).hexdigest(),
        bytes=len(docx_payload), topics=[f.topic for f in docx_facts],
        fact_ids=[f.fact_id for f in docx_facts],
    )

    pdf_payload = build_pdf_bytes(profile, build_pdf_chapters(profile, topics, facts))
    payloads[pdf_rel] = pdf_payload
    file_index[pdf_rel] = GeneratedFile(
        path=pdf_rel, kind="pdf", sha256=hashlib.sha256(pdf_payload).hexdigest(),
        bytes=len(pdf_payload), topics=[f.topic for f in pdf_facts],
        fact_ids=[f.fact_id for f in pdf_facts],
    )

    pptx_payload = build_pptx_bytes(profile, build_deck_spec(profile, topics, facts))
    payloads[pptx_rel] = pptx_payload
    file_index[pptx_rel] = GeneratedFile(
        path=pptx_rel, kind="pptx", sha256=hashlib.sha256(pptx_payload).hexdigest(),
        bytes=len(pptx_payload), topics=[f.topic for f in pptx_facts],
        fact_ids=[f.fact_id for f in pptx_facts],
    )

    shared_payload = build_shared_file_bytes(seed)
    payloads[SHARED_FILE_REL] = shared_payload
    file_index[SHARED_FILE_REL] = GeneratedFile(
        path=SHARED_FILE_REL, kind="text", sha256=hashlib.sha256(shared_payload).hexdigest(),
        bytes=len(shared_payload), topics=["cutover_rehearsal"],
    )

    duplicate_payload = build_duplicate_pair_bytes(profile)
    for rel in (duplicate_a_rel, duplicate_b_rel):
        payloads[rel] = duplicate_payload
        file_index[rel] = GeneratedFile(
            path=rel, kind="text", sha256=hashlib.sha256(duplicate_payload).hexdigest(),
            bytes=len(duplicate_payload), topics=["invoice_matching"],
        )
    # A manifest that names "docx" is not a map. The sentinels build_facts used to
    # route a fact are swapped for the real relative paths now that the files exist.
    sentinels = {"docx": docx_rel, "pdf": pdf_rel, "pptx": pptx_rel}
    for fact in facts:
        fact.document = sentinels.get(fact.document, fact.document)
        entry = file_index.get(fact.document)
        if entry is not None:
            entry.topics.append(fact.topic)

    unanswerable = build_unanswerable_facts(profile)
    company_dir = out_root / company
    company_dir.mkdir(parents=True, exist_ok=True)
    for rel, payload in payloads.items():
        target = company_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    manifest = build_manifest(profile, topics, facts, unanswerable, file_index,
                              duplicate_a_rel, duplicate_b_rel, seed)
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (company_dir / MANIFEST_NAME).write_bytes(manifest_bytes)
    return company_dir, manifest


def build_manifest(profile: CompanyProfile, topics: list[Topic], facts: list[Fact],
                   unanswerable: list[dict[str, str]], files: dict[str, GeneratedFile],
                   duplicate_a_rel: str, duplicate_b_rel: str, seed: int) -> dict[str, Any]:
    shared = files[SHARED_FILE_REL]
    shared_topics = [topic for topic in topics if topic.kind == "shared"]
    unique_topics = [topic for topic in topics if topic.kind == "unique"]
    scoped_probes: list[dict[str, Any]] = []
    for fact in facts[:14]:
        label = next(t.label for t in topics if t.id == fact.topic)
        scoped_probes.append({
            "question": f"In {profile.name}, what is recorded about {label}?",
            "shape": "scoped",
            "expected_company": profile.name,
            "expected_document": fact.document,
            "expected_fact_id": fact.fact_id,
            "expected_fact_code": fact.code,
        })
    cross_probes: list[dict[str, Any]] = []
    for topic in shared_topics:
        mine = [fact for fact in facts if fact.topic == topic.id]
        cross_probes.append({
            "question": f"How did each company in the store handle {topic.label}?",
            "shape": "cross-company",
            "topic": topic.id,
            "expected_companies": "all indexed companies",
            "this_company_fact_ids": [fact.fact_id for fact in mine],
            "this_company_documents": sorted({fact.document for fact in mine}),
        })
    unanswerable_probes = [{
        "question": f"What outcome was recorded under {entry['code']}?",
        "shape": "unanswerable",
        "expected": "refusal: the code exists in no document of any company",
        "fact_code": entry["code"],
    } for entry in unanswerable]
    return {
        "format_version": 1,
        "generator": "scripts/make_synthetic_company.py",
        "company": profile.name,
        "company_code": profile.code,
        "seed": seed,
        "safety": (
            "Invented data. Not derived from any real document. Safe to stage for a test "
            "index; not evidence about any real company."
        ),
        "counts": {
            "files": len(files),
            "transcripts": sum(1 for f in files.values() if f.kind == "transcript"),
            "docx": sum(1 for f in files.values() if f.kind == "docx"),
            "pdf": sum(1 for f in files.values() if f.kind == "pdf"),
            "pptx": sum(1 for f in files.values() if f.kind == "pptx"),
            "other_text": sum(1 for f in files.values() if f.kind == "text"),
            "facts": len(facts),
            "unanswerable_facts": len(unanswerable),
            "shared_topics": len(shared_topics),
            "unique_topics": len(unique_topics),
        },
        "people": [
            {"name": name, "role": profile.roles[name]} for name in sorted(profile.speakers)
        ],
        "files": [entry.as_manifest_entry() for _, entry in sorted(files.items())],
        "topics": [{
            "id": topic.id, "label": topic.label, "kind": topic.kind,
            "cross_company_useful": topic.kind == "shared",
        } for topic in topics],
        "facts": [fact.as_manifest_entry() for fact in facts],
        "unanswerable_facts": unanswerable,
        "duplicates": {
            "within_company_pair": [duplicate_a_rel, duplicate_b_rel],
            "within_company_pair_sha256": files[duplicate_a_rel].sha256,
            "cross_company_shared": {
                "path": SHARED_FILE_REL,
                "sha256": shared.sha256,
                "byte_identical_across_companies": True,
                "note": (
                    "Same bytes for every company at the same seed, because the shared file is "
                    "generated from the seed alone. LightRAG indexes identical content once "
                    "store-wide, so this file tests attribution to both companies, not two copies."
                ),
            },
        },
        "probes": {
            "scoped": scoped_probes,
            "cross_company": cross_probes,
            "unanswerable": unanswerable_probes,
        },
    }


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--company", default=DEFAULT_COMPANY,
                        help=f"company name, i.e. the directory name (default {DEFAULT_COMPANY!r})")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help=f"seed for every random choice (default {DEFAULT_SEED})")
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help=f"output root, outside the repo (default {DEFAULT_OUT})")
    parser.add_argument("--emit-shared-file", metavar="PATH", default=None,
                        help="write the cross-company shared file's bytes to PATH and exit")
    parser.add_argument("--forbid-unique-topic", action="append", default=[],
                        metavar="ID", help="exclude a unique-topic id (repeatable), e.g. "
                        "'unique:rail siding weighbridge', to force two companies apart")
    parser.add_argument("--quiet", action="store_true", help="no stdout summary")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.emit_shared_file:
        payload = build_shared_file_bytes(args.seed)
        target = Path(args.emit_shared_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        if not args.quiet:
            print(json.dumps({
                "emitted": str(target), "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload), "seed": args.seed,
                "note": "byte-identical to the copy inside every company built with this seed",
            }, sort_keys=True))
        return 0

    out_root = Path(args.out).expanduser().resolve()
    root = repo_root()
    try:
        out_root.relative_to(root)
    except ValueError:
        pass
    else:
        raise SystemExit(
            f"refusing to write synthetic data inside the repo ({out_root} is under {root}); "
            "use --out outside the working tree"
        )
    company_dir, manifest = generate_company(args.company, args.seed, out_root,
                                             list(args.forbid_unique_topic))
    if not args.quiet:
        summary = {
            "company": manifest["company"],
            "company_code": manifest["company_code"],
            "seed": manifest["seed"],
            "dir": str(company_dir),
            "manifest": str(company_dir / MANIFEST_NAME),
            "counts": manifest["counts"],
            "duplicates": {
                "within_company_pair_sha256": manifest["duplicates"]["within_company_pair_sha256"],
                "cross_company_shared_sha256": manifest["duplicates"]["cross_company_shared"]["sha256"],
            },
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
