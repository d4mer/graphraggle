# Work Instruction: Getting The Best Answers From GraphRAG

**Audience:** anyone asking questions of the knowledge base (Open WebUI or the gateway API).
**Goal:** get answers that are correct, complete and checkable, in as few attempts as possible.
**Time budget:** expect roughly 1 to 3 minutes per question (see "What to expect").

This page is about asking. For loading documents see `02-daily-workflow.md`; for
settings see `05-known-good-config.md`.

## 1. The five rules

1. **Only ask about documents that are ready.** If a file is not `ingested` with
   `query_ready: true`, the system cannot see it and will answer from whatever else
   matches, sometimes confidently.
2. **One question, one fact, one subject.** Name the person, document, date or
   product you mean. Do not rely on the system to guess.
3. **Quote, don't paraphrase, the words you remember.** A short exact phrase from the
   source is the strongest retrieval hook there is.
4. **Check the sources on every answer that matters.** Every answer carries
   citations. If the cited passage does not say it, the answer is wrong.
5. **Treat "not in the documents" as a valid answer.** Do not push the system to
   produce something. A forced answer is how wrong facts get in.

## 2. Before you ask: is the document ready?

```bash
curl -s http://localhost:8020/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY" | jq '.summary.by_query_ready'
```

| What you see | What it means | What to do |
|---|---|---|
| `status: ingested`, `query_ready: true` | Searchable | Ask away |
| `submitted` / `processing` | Still being indexed | Wait; large files take a while |
| `failed` | Indexing failed | See `error_message`; ask the admin to reindex |
| `validation_state: reject` | File refused at the gate | Fix the file (format, empty, too big) |
| `validation_state: auto_split` | Too large | Split the file and upload the parts |
| `readiness_reason: superseded` | A newer version replaced it | Ask about the newer one |

If a question about a brand-new document returns nothing useful, check this table
first. It is the most common cause of a "bad answer".

## 3. Pick the right surface

| You want to | Use | Why |
|---|---|---|
| Explore, ask follow-ups, work through a topic | **Open WebUI** (`:3010`) | Keeps the thread; rewrites follow-ups into standalone questions |
| Restrict to one company, tune depth, get raw citations | **Gateway `/query`** (`:8020`) | Has `company`, `mode`, `top_k`, structured citations |
| Produce a memo, report or summary | **`/generate-document`** | Grounded draft from retrieval, but see section 7 |
| Debug why something is missing | LightRAG Web UI (`:9621`) | Inspect ingestion and the graph; not for asking |

Differences that affect your answers:

1. **Open WebUI always retrieves in `mix` mode with reranking on**, uses the last 3
   conversation turns, and appends a **Sources** block to the answer. It has **no
   company filter**: put the company or client name in the question itself.
2. **`/query` defaults to `mix`** when `mode` is omitted (older pages that say
   `hybrid` are out of date). It can be scoped with `"company": "Acme Corp"`, which
   returns only citations from that company's documents.
3. In both, answers are generated from retrieved passages plus the knowledge graph.
   Neither can see a document that is not `query_ready`.

## 4. How to write a good question

### 4.1 Be specific and self-contained

| Weak | Strong |
|---|---|
| "What did they decide?" | "What did the steering group decide about the Q3 budget in the 12 March meeting?" |
| "Tell me about the policy" | "What does the travel policy say about booking flights more than 14 days ahead?" |
| "Any risks?" | "List the operational risks named in the onboarding plan." |

Rules of thumb:

1. Use **proper names exactly** as they appear in the documents (people, products,
   systems, project codenames).
2. Add **one anchor** the document will contain: a date, a section name, a speaker.
3. If you remember a phrase, **put it in quotes** inside the question.
4. State the **scope**: which document, which period, which company.
5. Say the **form of the answer you want**: "a one-sentence answer", "a bulleted
   list", "who said it and when".

### 4.2 Transcripts and meeting recordings

Spoken-word sources are the hardest: speakers mumble, names are mis-transcribed,
and the same topic is spread over many minutes.

1. Include the **speaker's name or role**.
2. Include **when** (day, meeting, session) if you know it.
3. Include a **short exact phrase** you remember, or the term as it was likely
   transcribed.
4. If a term returns nothing, **try the spelling the transcript probably used**
   (speech-to-text mis-hears names and jargon) or describe the idea in plain words.
5. Words like "transcript", "workshop", "speaker", "meeting minutes", "recording"
   make `/query` retrieve more passages. Use one when you mean a transcript.

### 4.3 One fact per question

Multi-part questions are where answers go wrong: the system tends to answer the
parts it finds and **pad the list with something plausible**.

1. Ask for **one fact** per question. Combine afterwards yourself.
2. If you need a list ("all the risks named"), ask for the list, then **verify each
   item against its citation** and ask "is there anything in the sources about X?"
   for anything you suspected was missing.
3. Be suspicious of a list whose last item is vaguer or more generic than the
   rest. That is the usual tell.

### 4.4 Across documents

Questions that require combining several documents are the weakest case in our
measurements (see section 8).

1. **Name both documents** (or both topics) in the question.
2. Better: **ask once per document**, then compare the two answers yourself or ask
   the system to compare *those two answers*.
3. Avoid open "across everything" questions such as "what are the themes?" unless a
   rough overview is all you need.

### 4.5 Follow-ups

Follow-ups work well. The system rewrites "and who approved it?" into a standalone
question using the last few turns.

1. Keep a thread to **one subject**. Start a **new thread for a new topic**.
2. Only the **last 3 turns** are used. In a long thread, **restate the subject** in
   your question ("...the Q3 budget, not the Q4 one").
3. If a follow-up goes odd, **ask it again as a full standalone question**.

## 5. Choosing a retrieval mode (`/query` only)

Leave `mode` off unless you have a reason. `mix` is the default and the right choice
for almost every question.

| `mode` | Use when | Avoid when |
|---|---|---|
| `mix` (default) | Anything: facts, summaries, relationships | - |
| `hybrid` | You want answers weighted to entities and relationships | You need an exact quote |
| `local` | A question about one named entity and its immediate surroundings | Broad overview questions |
| `global` | A big-picture or thematic question across the graph | Exact facts or quotes |
| `naive` | You want plain passage search with no graph (good for exact wording) | Relationship questions |
| `bypass` | You are **not** using the documents at all | Anything you want grounded; it skips retrieval |

Practical tips:

1. For an **exact quote or number**, try `naive`, or add `"top_k": 24`.
2. For **who/what is connected to whom**, stay on `mix` or `local`.
3. `bypass` returns an ungrounded model answer with no citations. Never use it for
   facts about your documents.

```bash
curl -s -X POST http://localhost:8020/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What did the steering group decide about the Q3 budget on 12 March?",
       "company":"Acme Corp","mode":"mix","top_k":24}'
```

## 6. Read the answer like an editor

1. **Open the citations.** Does the cited passage actually contain the claim?
   Answers can be fluent and wrong; the citation is the check.
2. **Look for what is missing.** If you asked for "all X" and the sources only show
   three, say so and ask for the rest.
3. **Be wary of tidy details**: exact figures, dates, names of people. Confirm them
   in the source.
4. **A refusal is information.** "The documents do not mention this" usually means it
   is not there, or you used different words. Retry once with another phrasing
   before concluding it is absent.
5. **Different wording, different retrieval.** If an answer looks thin, rephrase using
   the document's own vocabulary and ask again. Do not just resend the same text.

## 7. Generating documents (`/generate-document`)

```bash
curl -s -X POST http://localhost:8020/generate-document \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"Memo summarising the decisions on Q3 budget from the March steering meetings",
       "document_type":"memo","company":"Acme Corp"}'
```

`document_type` is one of `summary`, `memo`, `report`, `proposal`, `policy`, `brief`,
`draft`.

1. A document is **only as good as the retrieval behind it**. Run the same query
   through `/query` first and confirm the answer and citations look right.
2. Make the query **narrow and concrete**. "A memo about the project" will be vague;
   "A memo on the three decisions taken at the 12 March meeting" will not.
3. Use `style` for tone and audience.
4. **Always review it.** Check every figure and name against the cited sources
   before it leaves your hands.

## 8. What to expect (measured, so you can calibrate)

Release candidate baseline (36 probe questions, 3 runs each, automatic scoring,
2026-10-09):

| Question type | Score | In plain words |
|---|---|---|
| Single fact | 1.00 | Reliable |
| Follow-up | 0.89 | Good |
| Multi-fact | 0.83 | Usually right; watch for an extra invented item |
| Unanswerable (should refuse) | 0.67 | One in three is answered when it should not be |
| Cross-document | 0.56 | The weak spot; split the question |

Later internal runs on a transcript corpus, with an improved judging rubric, showed
about 96% acceptable answers (correct, or correctly refused) and no fabrications on
fresh held-out questions. The remaining weaknesses are the same two: **multi-fact
synthesis** and **terms garbled by speech recognition**. Treat these numbers as a
guide, not a guarantee: they were measured on specific question sets.

**Speed:** expect one to three minutes per answer (about one minute on recent runs,
about three at the rc1 baseline). The first words can take 40 to 50 seconds because
the model reads a large amount of context first. Do not resend a question while
waiting. A very slow answer usually means the model server is busy.

## 9. When an answer is wrong or empty

Work down this list; stop at the first fix.

1. **Is the document ready?** (section 2)
2. **Is the subject named?** Add the person, document, date. Remove pronouns.
3. **Is it one fact?** Split a compound question.
4. **Different words:** use the document's vocabulary, or the likely mis-transcription.
5. **Different mode:** `naive` for exact wording; `mix` for relationships.
6. **More depth:** `"top_k": 24` or add "transcript"/"meeting" to deepen retrieval.
7. **Scope it:** on `/query` add `company`; in Open WebUI put the company in the
   question.
8. **Still wrong?** Capture the question, the answer, the citations and the time, and
   send them to the admin. Do not keep rephrasing indefinitely.

An empty answer is rare (the Open WebUI bridge retries once automatically). If you get one, ask
again; if it persists, report it.

## 10. Quick reference card

```
READY?   status=ingested AND query_ready=true
ASK      one fact, named subject, an anchor (date/speaker), quote a phrase
FOLLOW   one subject per thread; restate subject in long threads
CROSS    name both docs, or ask once per doc and compare
MODE     leave default (mix); naive for exact wording; never bypass for facts
CHECK    open the citation; look for what is missing; confirm numbers/names
STUCK    ready? named? one fact? other words? other mode? scope? report it
```
