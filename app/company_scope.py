"""GL-10: the company named in the question scopes the search and the answer.

The operator's rule is blunt: name a company in the question and the answer comes from that
company's documents; name several and the answer comes from those; name none and nothing changes.
No syntax, no selector. So the first job is to read the company or companies out of the question
text, and the first rule of that job is that a wrong match is worse than no match: a false scope
hides every other company's answer and looks like the system losing its memory, while a missed
scope only leaves today's behaviour.

Matching is therefore against the companies the store actually knows (distinct non-null
`documents.company`, plus operator aliases), never against guessed organisation names.

Rules, in order:

1. Text is reduced to a match key: casefolded, every run of non-alphanumerics a single space.
   "Northwind  Chemicals'" and "northwind chemicals" are the same key, so punctuation and spacing
   differences in the question do not silently unscope a query.
2. Whole-phrase matching. A company key matches only as a whole token sequence, so "Acme" does not
   match inside "Acmeist".
3. Longest match first, and a shorter name cannot match inside the span of a longer one. With
   "Acme Chemicals" and "Acme" both known, the question "compare Acme Chemicals and Acme Corp"
   scopes to the two companies the question actually names, not to "Acme" three times.
4. Single-token names get a proper-noun test. A one-word company whose name is also an ordinary
   English word ("Target", "Pilot") only matches when the question capitalises it. Multi-token
   names match case-insensitively: "northwind chemicals" is not an accident.
5. Aliases come from one small setting and are validated against the known-company list, so a typo
   in an alias cannot invent a company that no document belongs to.
6. A name the store does not know is not an error: the query stays unscoped (today's behaviour) and
   the capitalised token sequences that did not match are recorded as candidates, so the operator
   can see what to alias. Never a refusal.

Nothing here decides what the scope does; `app/api.py` applies it. Nothing reads the corpus or the
store: the only inputs are the question text and the company list from the state DB.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_KEY_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_MULTI_SPACE = re.compile(r"\s+")

# Words that are ordinary English and therefore useless as a lowercase scope cue. The list is
# deliberately short: it only ever applies to a company whose entire name is ONE word, and it errs
# toward requiring the capital letter rather than toward scoping on an ordinary noun.
COMMON_ENGLISH = frozenset("""
about above across after again against all almost along already also although always among another
any anything around as at automatically back because before begin below between both but call came
can case change clearly come common currently day days direct early eight either else end enough
even every few first five four from full further get give given group half hand head high his
however increase inside it item its just kind last late later least left less let life like line
list long look lot made make many may mean might million mine more most move much must name near
need never new next nine no none nor north not note number of off often once one only open or order
other out over own part past per perhaps place point possible present put quite rather reach right
run say second see seem several she should side simple since six small some soon still such sure
take tenth than that the their them then there these they thing think third this those though three
through thus to today together told too toward twenty two under until up upon us use used using
very week well were what when where whether which while who whole why will with within without work
would year years yet you your
account advance agency alliance amber angle arrow aspect atlas avenue bank bay bridge branch
career central chain chamber chance channel charter check choice circle crown district division
domain edge element element estate exchange field figure force future gate guide hall head
house industry interest island link market meeting member milestone minutes national note number
office order organization park part pass phase pilot point port post prime progress project
quarter record region report review river road scale score section sector service square stage
standard star station step stone store summer summary target task team track trust union unit
valley view west works world
""".split())

MAX_CANDIDATES = 10
MAX_CANDIDATE_CHARS = 40


def match_key(text: str) -> str:
    """The comparable form of a question or a company name: casefolded, punctuation-insensitive."""
    return _MULTI_SPACE.sub(" ", _KEY_NON_ALNUM.sub(" ", text.casefold())).strip()


def parse_alias_map(raw: str | None) -> dict[str, list[str]]:
    """`COMPANY_ALIASES`: `Northwind Chemicals=nwind,north wind;Contoso=contoso pharma`.

    Unknown companies are dropped rather than trusted: an alias is a bridge to a company the store
    has rows for, not a way to invent one.
    """
    parsed: dict[str, list[str]] = {}
    for chunk in (raw or "").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        company, _, values = chunk.partition("=")
        company = company.strip()
        aliases = [value.strip() for value in values.split(",") if value.strip()]
        if company and aliases:
            parsed[company] = aliases
    return parsed


def validate_aliases(aliases: dict[str, list[str]], known: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """Keep only aliases whose company the store knows. Returns (usable, rejected_company_names)."""
    known_by_key = {match_key(name): name for name in known}
    usable: dict[str, list[str]] = {}
    rejected: list[str] = []
    for company, values in aliases.items():
        canonical = known_by_key.get(match_key(company))
        if canonical is None:
            rejected.append(company)
            continue
        usable[canonical] = values
    return usable, rejected


@dataclass
class Detection:
    """What the question named. `companies` are canonical names as the store stores them."""

    companies: list[str] = field(default_factory=list)
    aliased_by: dict[str, str] = field(default_factory=dict)   # canonical -> alias that matched
    candidates: list[str] = field(default_factory=list)        # capitalised runs that matched nothing

    @property
    def mode(self) -> str:
        if not self.companies:
            return "unscoped"
        return "single" if len(self.companies) == 1 else "multi"


def _surface_candidates(question: str, matched_keys: list[str]) -> list[str]:
    """Capitalised token sequences that did not match a known company.

    A hint for the operator, not a scope: "Acme Labs asked about X" while the store knows "Acme
    Laboratory Inc" is an alias waiting to be written, and the only way the operator finds out is if
    the system says what it saw.
    """
    runs: list[str] = []
    for match in re.finditer(r"\b[A-Z][A-Za-z0-9&'.\-]*(?:\s+[A-Z][A-Za-z0-9&'.\-]*){0,2}",
                            question):
        run = _MULTI_SPACE.sub(" ", match.group(0)).strip()
        # A capital at the start of the question is grammar, not a name; and a one-word candidate
        # that is an ordinary noun would only generate alias requests.
        if match.start() == 0 and len(run.split()) == 1:
            continue
        key = match_key(run)
        # A run that sits inside (or around) a name that already matched is not a new lead.
        if any(key == matched or key in matched or matched in key for matched in matched_keys):
            continue
        if len(key.split()) == 1 and (key in COMMON_ENGLISH or (any(c.isdigit() for c in key)
                                                                 and len(key) <= 4)):
            continue   # ordinary noun, or a period/code like "Q3" rather than an organisation
        if not run or len(run) > MAX_CANDIDATE_CHARS:
            continue
        if run not in runs:
            runs.append(run)
    return runs[:MAX_CANDIDATES]


def detect_companies(
    question: str,
    known: list[str],
    *,
    aliases: dict[str, list[str]] | None = None,
) -> Detection:
    """Which of `known` companies the question names, longest match first."""
    if not question or not known:
        return Detection()

    # canonical name -> every phrase that names it (its own name plus validated aliases)
    phrases: list[tuple[str, str]] = []
    for canonical in known:
        phrases.append((canonical, canonical))
    for canonical, values in (aliases or {}).items():
        for alias in values:
            phrases.append((canonical, alias))

    keyed = [(canonical, phrase, match_key(phrase)) for canonical, phrase in phrases]
    keyed = [entry for entry in keyed if entry[2]]
    keyed.sort(key=lambda entry: (-len(entry[2].split()), entry[2]))

    haystack = match_key(question)
    padded = f" {haystack} "
    detection = Detection()
    matched_keys: list[str] = []
    working = list(padded)

    for canonical, phrase, key in keyed:
        needle = f" {key} "
        start = 0
        hits = 0
        while True:
            index = "".join(working).find(needle, start)
            if index < 0:
                break
            tokens = key.split()
            if len(tokens) == 1 and key in COMMON_ENGLISH:
                # An ordinary word only counts as a company when the question treats it as a name.
                surface = _surface_token(question, key)
                if surface is None:
                    start = index + 1
                    continue
            for position in range(index, index + len(needle)):
                if position < len(working) and working[position] != " ":
                    working[position] = "\u0000"      # consumed: a shorter name cannot match here
            matched_keys.append(key)
            hits += 1
            start = index + len(needle)
        if hits:
            if canonical not in detection.companies:
                detection.companies.append(canonical)
            if match_key(canonical) != key:
                detection.aliased_by[canonical] = phrase

    detection.candidates = _surface_candidates(question, matched_keys)
    return detection


def _surface_token(question: str, key: str) -> str | None:
    """The question's own spelling of `key`, but only when it is capitalised (a name cue)."""
    for match in re.finditer(rf"(?<![A-Za-z0-9]){re.escape(key)}(?![A-Za-z0-9])", question,
                             flags=re.IGNORECASE):
        if match.group(0)[:1].isupper():
            return match.group(0)
    return None
