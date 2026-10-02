# Product and Engineering Decisions

## What I built

The useful unit for a buyer is not a chatbot response; it is a trustworthy comparison with a clear
line back to what each supplier actually wrote. QuoteLens therefore produces normalized commercial
terms, comparable known costs, exceptions, missing information, and ready-to-send questions.

FastAPI is the product boundary and Streamlit is a thin demonstration client. The core analysis has
no UI dependency and its repository and extraction provider are interfaces, making database or
model-provider changes local.

Supplier responses now have a separate audit model: immutable communications and artifacts produce
provenance-bearing change candidates; accepted operations produce immutable complete quote
revisions. Current quote reads remain simple while stale partial updates use a three-way merge.

## Where I went deep

- **Auditability:** commercially important extracted values carry exact source excerpts.
- **Unknowns:** the system never silently invents FX, freight, duty, revision, or tooling.
- **Deterministic math:** the LLM extracts; application code selects tiers and computes costs.
- **Decision hygiene:** conflicting revisions and Incoterms are first-class comparison problems,
  not small-print notes beneath a misleading ranking.
- **Prompt-injection boundary:** supplier text is explicitly untrusted and only used as extraction
  input.

## What I cut

- No award recommendation or composite supplier score. The packet does not provide buyer weights,
  quality history, capacity evidence, or a confirmed current drawing revision.
- No OCR, PDF, email, or spreadsheet ingestion. V1 accepts pasted or uploaded text.
- No editable extraction or confidence score. Evidence makes review possible; correction workflow is
  the next product increment.
- No durable database, authentication, queues, or background jobs. Quote, revision, communication,
  and analysis repositories are in-memory interfaces so infrastructure remains replaceable.
- No live FX, freight, tariff, or supplier-risk data. Scenario inputs must be explicit.

## Where it falls over

- Fixture mode supports only the supplied sample; arbitrary responses require a model API key.
- LLM extraction can still omit or misread a term. Schema validation prevents malformed output, not
  semantic mistakes.
- A single response cannot represent complex multi-part bids, alternate materials, or stepped
  logistics charges without extending the canonical schema.
- Cost scenarios simplify duty to a supplied rate applied after annual freight. A production system
  needs customs-value rules and tariff provenance.
- In-memory analyses disappear on restart and should not be used for production records.
- The ingestion API stores artifact content in memory. Production must place immutable raw content
  in durable object storage and retain only checksums and object references in transactional data.

## With another day

1. Add field-level confidence and an evidence-backed buyer correction workflow.
2. Persist RFQs, quote versions, assumptions, and analysis lineage in Postgres.
3. Add digital-PDF/email ingestion with file-level provenance.
4. Build an evaluation set with intentionally adversarial and messy supplier replies.
5. Add configurable buyer priorities before introducing scenario-based recommendations.
6. Generate reviewed supplier clarification emails and track responses back into the comparison.
