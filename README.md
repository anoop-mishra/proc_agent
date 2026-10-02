# QuoteLens — Procurement RFQ Insight Agent

QuoteLens turns inconsistent free-text supplier responses into an auditable buyer brief. It
normalizes quoted facts, computes only defensible values, highlights comparison gaps, and drafts
supplier follow-up questions. It intentionally does **not** rank suppliers or recommend an award.

The included fixture reproduces the four quotes in `Aron-EngineeringTakeHome.pdf`, so the complete
demo works without an API key. Arbitrary quote text uses an OpenAI structured-output adapter behind
a provider-neutral interface.

## Run it

### With `uv`

```bash
cp .env.example .env
uv sync --extra dev
uv run uvicorn app.api:app --reload
```

In a second terminal:

```bash
uv run streamlit run ui.py
```

Open <http://localhost:8501>, select **Load Aron sample**, then **Analyze quotes**. API documentation
is available at <http://localhost:8000/docs>.

### With Docker

```bash
cp .env.example .env
docker compose up --build
```

The UI runs at <http://localhost:8501> and the API at <http://localhost:8000>.

### Analyze arbitrary quotes

Set the following in `.env`:

```dotenv
LLM_PROVIDER=openai
OPENAI_API_KEY=your-key
OPENAI_MODEL=gpt-4.1-mini
```

`OPENAI_MODEL` is configurable because model availability varies by account. The implementation
uses the Responses API structured-output flow described in the
[official OpenAI documentation](https://developers.openai.com/api/docs/guides/structured-outputs).

## API

- `GET /health` — readiness check.
- `GET /v1/samples/aron` — request payload for the included quote packet.
- `POST /v1/analyses` — extract, validate, and analyze quote text.
- `GET /v1/analyses/{analysis_id}` — retrieve an analysis for this process lifetime.
- `POST /v1/supplier-quotes` — create revision 1 from a complete canonical quote.
- `GET /v1/supplier-quotes/{quote_id}` — retrieve the current complete snapshot.
- `GET /v1/supplier-quotes/{quote_id}/revisions` — retrieve immutable snapshot history.
- `GET /v1/supplier-quotes/{quote_id}/changes` — retrieve accepted changes.
- `POST /v1/supplier-quotes/{quote_id}/changes` — apply a versioned partial change.
- `POST /v1/supplier-quotes/{quote_id}/communications/{id}/apply` — extract and apply an
  ingested incremental supplier update.
- `POST /v1/communications` — preserve an inbound message and its artifacts.
- `POST /v1/supplier-documents/ingest` — ingest a batch, establish logical document order,
  match suppliers to canonical quotes, apply updates, and persist conflicts.
- `POST /v1/rfq-match-contexts` — register evidence used for RFQ matching.
- `POST /v1/communications/{id}/resolve` — produce an explainable RFQ resolution.
- `POST /v1/communications/{id}/manual-resolution` — record a reviewed resolution.
- `POST /v1/communications/{id}/change-candidates` — retain extracted candidates with
  provenance before acceptance.
- `POST /v1/communications/{id}/conflict-flags` — record a buyer-reviewed field, ordering,
  specification, or stale-update conflict.
- `GET /v1/communications/{id}/conflict-flags` — retrieve open conflict flags.

Minimal request shape:

```json
{
  "rfq": {
    "part_number": "BRK-4471-A",
    "annual_volume": 60000,
    "drawing_revision": "C",
    "destination": "Columbus, Ohio"
  },
  "quotes": [
    {"source_id": "supplier-1", "text": "Supplier response here"}
  ],
  "assumptions": {}
}
```

Optional assumptions are explicit and auditable:

```json
{
  "target_currency": "USD",
  "fx_rates": {"EUR_USD": 1.10},
  "freight_by_source": {
    "supplier-1": {"amount": 12000, "currency": "USD"}
  },
  "duty_rate_by_source": {"supplier-1": 0.05}
}
```

Freight values represent an annual scenario. Duty is applied to converted material plus assumed
annual freight. These results are labeled as estimates with the assumptions used. Without explicit
inputs, mixed currencies and non-delivered Incoterms remain unresolved rather than being guessed.

## Architecture

```text
Streamlit / API caller
        │
        ▼
FastAPI → AnalysisService → ExtractionProvider
                    │          ├── Fixture provider (sample)
                    │          └── OpenAI provider (arbitrary text)
                    ▼
          deterministic analysis
                    │
                    ▼
       AnalysisRepository (in-memory v1)
```

The model extracts facts and exact evidence excerpts. Pydantic validates the canonical quote. Plain
Python then selects the applicable volume tier, computes annual material cost, applies only explicit
scenario assumptions, identifies gaps, and constructs questions. Supplier text is treated as
untrusted content and cannot instruct the extractor to rank or recommend.

### Versioned supplier responses

`app/quote_models.py` defines complete canonical quote snapshots with stable IDs for lines, tiers,
charges, attachments, and answers. `app/versioning.py` stores immutable revisions and accepted
changes. A stale change is compared against its base and the current snapshot:

- unchanged fields are applied;
- already-matching values are treated as applied;
- unrelated changes merge into the next server-assigned revision;
- values changed differently on both sides produce explicit conflicts;
- compare-and-swap prevents concurrent revision advancement;
- closed or awarded quotes require a separate amendment workflow.

Every committed revision is a complete snapshot. Submission validates explicit RFQ-line responses
and reconciles supplied line, tax, and payable totals. Deleted attachments leave tombstones.

### Communication ingestion

`app/ingestion.py` stores raw messages, authored body text, attachments, checksums, transport IDs,
and timestamps separately from canonical quote state. Exact provider/message/event IDs and exact raw
messages are retry keys. Similar normalized content is only flagged as a possible resubmission and
is not silently discarded.

RFQ resolution records ranked evidence and confidence. Explicit RFQ IDs, reply references, and
existing thread associations can auto-resolve an unambiguous match; weaker or competing candidates
remain review-required. Candidate commercial changes retain field path, typed value, confidence,
source text, artifact/location, and inferred relationship.

The first analysis creates revision 1 for each supplier. Later analyses read the current canonical
snapshot, so accepted facts from the original response and every incremental document are reflected
in the buyer brief.

The Streamlit UI has one **Supplier documents** uploader for initial quotes and subsequent updates.
It parses optional `Supplier`, `From`, `To`, `Subject`, and `Date` headers while preserving exact raw
text as immutable evidence. The ingestion API processes newer evidence first, matches documents to a
canonical supplier/RFQ, advances snapshots for accepted changes, and retains stale-field conflicts.
The Analyze action reads only the latest complete snapshot plus original extracted facts. Comparison
cells affected by conflicting updates are marked with `⚠`, with conflict details shown under the
supplier. Files under `data/test_data/` exercise an idempotent duplicate, a clean lead-time update,
and a stale update whose unrelated packaging term is accepted while its lead-time conflict remains
visible for review.

## Tests

```bash
uv run pytest
uv run ruff check .
```

Tests cover the supplied packet, price-tier math, explicit cost scenarios, stale three-way merges,
commercial conflicts, optimistic concurrency, idempotency, sequence gaps, totals reconciliation,
retry detection, RFQ resolution, end-to-end incremental revision and re-analysis, API persistence,
and the OpenAI adapter contract without making a live API call.

Regenerate the representative response with:

```bash
uv run python -m scripts.generate_sample
```

See [DECISIONS.md](DECISIONS.md) for product choices, limitations, and next steps. A representative
fixture response is stored in `artifacts/aron-analysis.json`.
