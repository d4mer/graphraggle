# Large PDF Ingestion

Use this guide for large, important PDFs before ingesting them into the GraphRAG stack.

## Recommendation

For a large PDF such as a 600-page knowledge corpus, do **not** ingest it as one monolithic file if:

1. it covers multiple topics
2. it includes appendices, workshop notes, and design sections
3. you need reliable retrieval by topic
4. you want easier retries when ingestion fails

Preferred approach:

1. split it into logical sections
2. give each section a clear filename
3. ingest the sections separately

## Why Split It

Benefits:

1. better retrieval precision
2. easier troubleshooting
3. shorter re-ingestion time for failures
4. more meaningful citations
5. easier lifecycle management for updates

## How To Split It

Split by **topic**, not just by equal page count.

Good section boundaries:

1. executive summary
2. business context
3. current-state process
4. future-state process
5. solution design
6. integration design
7. roles and responsibilities
8. decisions and assumptions
9. appendix / reference material

Avoid splitting by arbitrary 50-page chunks unless you truly do not know the structure.

## Naming Convention

Use sortable names like:

```text
01-executive-summary.pdf
02-business-context.pdf
03-current-state-process.pdf
04-future-state-process.pdf
05-solution-design.pdf
06-integrations.pdf
07-roles-and-responsibilities.pdf
08-decisions-and-assumptions.pdf
09-appendix.pdf
```

This helps:

1. ingestion tracking
2. citations
3. operational search and reindexing

## Option A: Split With `qpdf`

If `qpdf` is installed, use the helper script in this repo.

Example:

```bash
cd /Users/imac/Documents/Programming/graphrag-implementation
./scripts/split_pdf_qpdf.sh \
  "/Users/imac/Desktop/your-large.pdf" \
  "/Users/imac/Desktop/your-large-split" \
  intro 1-40 \
  current-state 41-150 \
  design 151-320 \
  integration 321-470 \
  appendix 471-z
```

This creates:

```text
/Users/imac/Desktop/your-large-split/
├── 01-intro.pdf
├── 02-current-state.pdf
├── 03-design.pdf
├── 04-integration.pdf
└── 05-appendix.pdf
```

## Option B: Split With Preview

If you do not want to install anything:

1. open the PDF in Preview
2. show thumbnails
3. duplicate the document as many times as needed
4. in each copy, delete all pages except the target section
5. save each copy with the naming convention above

This is slower but completely workable for important corpora.

## Where To Put The Split Files

Recommended:

```bash
mkdir -p ~/rag-project/source_docs/one-logistics-design
cp /Users/imac/Desktop/your-large-split/*.pdf ~/rag-project/source_docs/one-logistics-design/
```

Or batch-upload them through the API:

```bash
cd /Users/imac/Documents/Programming/graphrag-implementation
export RAG_API_KEY='your-gateway-key'
./scripts/upload_batch.sh "/Users/imac/Desktop/your-large-split"
```

## Validation After Ingestion

Check status:

```bash
curl -i http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY"
```

Then run targeted queries against known sections.

Examples:

```bash
curl -i -X POST http://localhost:8000/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"Summarize the solution design section.","mode":"mix"}'

curl -i -X POST http://localhost:8000/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What integrations are described in the integration section?","mode":"mix"}'
```

## Best Practice For Very Important Corpora

1. split by logical section
2. ingest section PDFs separately
3. keep original monolithic PDF for archive only
4. use section names that match real business topics
5. test retrieval after every major ingestion batch

## When Not To Split

You can keep a PDF whole if:

1. it is under roughly 100-150 pages
2. it is topically narrow
3. it is already well-structured and easy to search

For a 600-page corpus, splitting is the safer choice.
