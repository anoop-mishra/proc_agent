# 2026-10-01 -- Supplier RFQ response model

## Summary

Model supplier RFQ responses as versioned, auditable quotations that can arrive through structured APIs or informal channels such as email. Preserve every inbound communication as immutable evidence, extract candidate changes with provenance, resolve them to an RFQ, and materialize each accepted change as a complete quote revision.

The model must support partial and stale updates, duplicate delivery, alternatives and no-bids, and ambiguous ordering without silently overwriting commercial terms.

## Goals

- Represent comparable supplier pricing, quantities, delivery commitments, terms, taxes, charges, exceptions, and supporting documents.
- Accept full quotes and incremental updates while exposing a complete current snapshot.
- Resolve informal communications to the correct RFQ using explainable evidence and confidence.
- Detect transport retries without suppressing legitimate resubmissions.
- Merge out-of-order, non-conflicting changes and surface true conflicts.
- Retain field-level provenance and reproducible quote history.

## Non-goals

- Defining category-specific questionnaires or regulatory requirements.
- Storing supplier banking details in each quotation.
- Silently resolving ambiguous RFQ matches, commercial conflicts, or post-award amendments.

## Architectural Changes

### Previous

A quote revision is treated as a complete supplier response. This is simple to read but does not adequately represent partial updates, stale edits, or informal communications lacking RFQ and revision identifiers.

### New

Separate immutable source evidence, interpreted change candidates, and canonical quotation state:

```text
InboundCommunication
  -> SourceArtifact[]
  -> RFQResolution[]
  -> QuoteChangeCandidate[]
  -> SupplierQuoteChange
  -> SupplierQuoteRevision[]
  -> SupplierQuote current snapshot
```

- `InboundCommunication` records channel metadata and source identifiers.
- `SourceArtifact` preserves raw body content and attachments with checksums.
- `RFQResolution` stores candidate RFQs, confidence, evidence, and the resolution method.
- `QuoteChangeCandidate` contains extracted values, provenance, relationship inference, and confidence.
- `SupplierQuoteChange` is an immutable accepted patch or domain operation.
- `SupplierQuoteRevision` is a complete immutable snapshot produced by an accepted change.
- `SupplierQuote` points to the current canonical revision.

### Decisions

- Accept incremental changes but materialize a complete snapshot for every committed revision.
- Preserve supplier communications separately from canonical quote decisions.
- Use stable entity IDs for lines, tiers, charges, deliveries, answers, alternatives, and attachments.
- Use server-assigned `committedRevision` as canonical history; never order by sender timestamp alone.
- Use three-way merge for changes based on older revisions.
- Never use silent last-write-wins for commercial fields.
- Store monetary values as decimals with ISO 4217 currency codes and quantities with coded units of measure.

## Possible Approaches

| Approach | Pros | Cons |
|---|---|---|
| Full snapshots only | Simple reads and comparisons | Poor fit for partial or stale updates |
| Event/patch only | Compact writes and complete event history | Expensive and fragile reconstruction |
| Hybrid patches plus snapshots | Efficient updates, simple reads, strong auditability | Requires merge and projection logic |

**Recommendation:** Use the hybrid approach.

## Implementation Plan

### 1. Define the canonical quotation

At quote level, support:

- Quote ID, RFQ ID, revision, response type, status, issue/submission timestamps, validity period, currency, language, supplier/buyer references, contacts, notes, and source references.
- Commercial terms: payment, delivery, freight, Incoterms, contract period, renewals, cancellation, supplier terms, and explicit exceptions.
- Totals: line extension, allowances, charges, tax-exclusive, tax, tax-inclusive, and payable totals.
- Attachments and structured declarations/questionnaire answers.

At quote-line level, support:

- Stable quote-line ID and RFQ-line ID.
- Response status: `QUOTED`, `NO_BID`, `ALTERNATIVE`, or `PARTIAL`; require a reason for no-bid where configured.
- Offered item identifiers and description, substitution/alternative relationship, quantity and UOM, MOQ, and order multiple.
- Unit price, currency, price-basis quantity/UOM, price tiers, discounts, charges, taxes, and line totals.
- Delivery date/window or lead time, origin, warranty, notes, and exceptions.

Each RFQ line must have an explicit response status so absence is not confused with no-bid.

### 2. Add immutable revisions and partial changes

Represent a change with:

- Change/event ID, quote ID, base revision, idempotency key, actor, reason, submitted/received timestamps, and operations.
- Domain operations such as `SET_FIELD`, `UPDATE_LINE`, `NO_BID_LINE`, `ADD_PRICE_TIER`, `REMOVE_ATTACHMENT`, `SUBMIT_QUOTE`, and `WITHDRAW_QUOTE`.
- `parentRevision` for actual history and `basedOnRevision` for the state the supplier edited.

For an update based on an older revision:

1. Load the base snapshot, current snapshot, and supplier proposal.
2. Apply changes automatically where the current value still equals the base value.
3. Treat an already-matching proposed value as already applied.
4. Merge unrelated changes.
5. Create a conflict where current and proposed values changed differently.
6. Validate the full result and atomically append the change, create the next snapshot, and advance the current revision using optimistic concurrency.

Retain tombstones for deleted entities so delayed events cannot accidentally recreate them.

### 3. Ingest informal communications

Store channel, external/provider/transport message IDs, sender, recipients, subject, thread headers, sent/received/ingested timestamps, raw body, attachments, and checksums.

Separate newly authored email content from quoted history and signatures for extraction, while retaining the unmodified source.

Extract candidate values with:

- Field path and typed value.
- Confidence.
- Communication/artifact ID, page or location, and exact source text.
- Inferred relationship: `INITIAL_QUOTE`, `SUPPLEMENT`, `CORRECTION`, `FULL_REPLACEMENT`, `CONFIRMATION`, `WITHDRAWAL`, or `UNKNOWN`.

### 4. Resolve communications to RFQs

Rank evidence in this order:

1. Explicit RFQ ID in body, attachment, or subject.
2. Reply/reference to a message associated with an RFQ.
3. Existing thread association.
4. Supplier invitation and active-RFQ context.
5. Matching items, quantities, locations, contacts, and timing.

Automatically resolve one strong, unambiguous match. Route multiple credible candidates or no credible candidate for review. Allow one communication to produce candidates for multiple RFQs.

### 5. Implement retry idempotency

Deduplicate using, in priority order:

1. Provider message ID scoped to mailbox/account.
2. RFC email `Message-ID`.
3. Transport/webhook event ID.
4. Raw-message checksum.
5. Composite fingerprint of normalized sender, recipients, sent time, subject, newly authored body, and attachment checksums.

Treat matching provider/event/message IDs or raw bytes as retries. Do not automatically discard different messages merely because their normalized content matches; they may be legitimate confirmations or resends.

### 6. Handle ordering

Maintain `sentAt`, `receivedAt`, `ingestedAt`, `effectiveAt`, `committedAt`, optional supplier sequence, and canonical committed revision separately.

- Use explicit revision/replacement language, reply relationships, thread position, attachment quote dates/numbers, and timestamps as ordering evidence.
- Process by receipt order if necessary, but do not let receipt order decide conflicting commercial meaning.
- Merge independent changes into the next canonical revision.
- Surface overlapping changes with uncertain order as `ORDERING_CONFLICT`.
- Reject or hold references to unknown future revisions; detect supplier sequence gaps when sequences exist.
- Require an amendment workflow for closed or awarded quotes.

## Dependencies

- Durable object storage for raw emails and attachments.
- Email/channel metadata including provider IDs and thread headers where available.
- Text and attachment extraction with confidence and provenance.
- Configurable confidence thresholds and a human-review workflow.
- Transactional storage or compare-and-swap support for revision commits.
- Currency, unit-of-measure, tax, and Incoterms reference data.

## Rollout

1. Introduce canonical quotes and full immutable snapshots.
2. Add structured partial-update operations and conflict handling.
3. Add source communication storage and retry deduplication.
4. Enable RFQ matching and extraction in review-only mode.
5. Auto-apply high-confidence, non-conflicting changes behind configurable thresholds.
6. Add amendments for closed or awarded quotes.

Historical and submitted revisions remain immutable throughout rollout.

## Validate / Monitor

- Verify quote totals reconcile with line totals and all configured RFQ requirements are satisfied before submission.
- Replay accepted changes by `committedRevision` and confirm they reproduce stored snapshots.
- Test duplicate retries, stale non-conflicting changes, overlapping conflicts, deletion tombstones, sequence gaps, and concurrent commits.
- Measure RFQ-match precision, review rate, extraction corrections, false deduplication, merge conflicts, and projection failures.
- Audit every canonical field back to its source communication or explicit user/system decision.
