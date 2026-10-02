from decimal import Decimal

import pytest

from app.models import Money
from app.quote_models import (
    ChangeOperationType,
    CreateQuoteRequest,
    LineResponseStatus,
    MergeConflictType,
    QuoteAttachment,
    QuoteChangeOperation,
    QuoteChangeRequest,
    QuoteLine,
    QuoteTotals,
    SupplierQuoteSnapshot,
)
from app.versioning import (
    ConcurrentQuoteUpdateError,
    InMemoryQuoteVersionRepository,
    QuoteValidationError,
    QuoteVersioningService,
)


def snapshot() -> SupplierQuoteSnapshot:
    return SupplierQuoteSnapshot(
        quote_id="quote-1",
        rfq_id="rfq-1",
        supplier_id="supplier-1",
        currency="USD",
        required_rfq_line_ids=["rfq-line-1"],
        lines=[
            QuoteLine(
                quote_line_id="line-1",
                rfq_line_id="rfq-line-1",
                response_status=LineResponseStatus.QUOTED,
                unit_price=Money(amount=Decimal("10"), currency="USD"),
            )
        ],
        attachments=[QuoteAttachment(attachment_id="attachment-1", filename="quote.pdf")],
    )


def service() -> QuoteVersioningService:
    result = QuoteVersioningService(InMemoryQuoteVersionRepository())
    result.create(
        CreateQuoteRequest(snapshot=snapshot(), actor="buyer", idempotency_key="create-1")
    )
    return result


def change(
    change_id: str,
    key: str,
    operations: list[QuoteChangeOperation],
    *,
    base_revision: int = 1,
    expected_revision: int | None = None,
    sequence: int | None = None,
) -> QuoteChangeRequest:
    return QuoteChangeRequest(
        change_id=change_id,
        base_revision=base_revision,
        expected_current_revision=expected_revision,
        idempotency_key=key,
        actor="supplier-user",
        operations=operations,
        supplier_sequence=sequence,
    )


def test_stale_non_conflicting_change_merges_into_complete_snapshot() -> None:
    quote_service = service()
    quote_service.commit(
        "quote-1",
        change(
            "change-1",
            "key-1",
            [
                QuoteChangeOperation(
                    operation=ChangeOperationType.UPDATE_LINE,
                    entity_id="line-1",
                    changes={"unit_price": {"amount": "9", "currency": "USD"}},
                )
            ],
        ),
    )

    result = quote_service.commit(
        "quote-1",
        change(
            "change-2",
            "key-2",
            [
                QuoteChangeOperation(
                    operation=ChangeOperationType.SET_FIELD,
                    path="/commercial_terms/payment_terms",
                    value="Net 60",
                )
            ],
            base_revision=1,
        ),
    )

    assert result.quote.current_revision == 3
    assert result.quote.current.commercial_terms.payment_terms == "Net 60"
    assert result.quote.current.lines[0].unit_price == Money(amount=9, currency="USD")
    assert result.revision is not None
    assert result.revision.based_on_revision == 1
    assert result.revision.parent_revision == 2


def test_stale_change_applies_independent_fields_and_surfaces_true_conflict() -> None:
    quote_service = service()
    quote_service.commit(
        "quote-1",
        change(
            "change-1",
            "key-1",
            [
                QuoteChangeOperation(
                    operation=ChangeOperationType.UPDATE_LINE,
                    entity_id="line-1",
                    changes={"unit_price": {"amount": "9", "currency": "USD"}},
                )
            ],
        ),
    )

    result = quote_service.commit(
        "quote-1",
        change(
            "change-2",
            "key-2",
            [
                QuoteChangeOperation(
                    operation=ChangeOperationType.UPDATE_LINE,
                    entity_id="line-1",
                    changes={
                        "unit_price": {"amount": "8", "currency": "USD"},
                        "notes": ["Expedite available"],
                    },
                )
            ],
        ),
    )

    assert result.quote.current_revision == 3
    assert result.quote.current.lines[0].unit_price == Money(amount=9, currency="USD")
    assert result.quote.current.lines[0].notes == ["Expedite available"]
    assert [item.conflict_type for item in result.conflicts] == [MergeConflictType.FIELD_CONFLICT]
    assert result.conflicts[0].path == "/lines/line-1/unit_price"


def test_idempotent_change_returns_original_revision() -> None:
    quote_service = service()
    request = change(
        "change-1",
        "same-key",
        [
            QuoteChangeOperation(
                operation=ChangeOperationType.SET_FIELD,
                path="/buyer_reference",
                value="PO-123",
            )
        ],
    )

    first = quote_service.commit("quote-1", request)
    replay = quote_service.commit("quote-1", request)

    assert first.quote.current_revision == 2
    assert replay.quote.current_revision == 2
    assert replay.idempotent_replay is True
    assert len(quote_service.revisions("quote-1")) == 2
    assert len(quote_service.changes("quote-1")) == 1


def test_submit_requires_explicit_response_for_every_rfq_line() -> None:
    quote_service = service()
    quote_service.commit(
        "quote-1",
        change(
            "requirements",
            "requirements-key",
            [
                QuoteChangeOperation(
                    operation=ChangeOperationType.SET_FIELD,
                    path="/required_rfq_line_ids",
                    value=["rfq-line-1", "rfq-line-2"],
                )
            ],
        ),
    )

    with pytest.raises(QuoteValidationError, match="rfq-line-2"):
        quote_service.commit(
            "quote-1",
            change(
                "submit",
                "submit-key",
                [QuoteChangeOperation(operation=ChangeOperationType.SUBMIT_QUOTE)],
                base_revision=2,
            ),
        )


def test_optimistic_concurrency_and_supplier_sequence_gaps_are_detected() -> None:
    quote_service = service()

    with pytest.raises(ConcurrentQuoteUpdateError):
        quote_service.commit(
            "quote-1",
            change(
                "change-1",
                "key-1",
                [
                    QuoteChangeOperation(
                        operation=ChangeOperationType.SET_FIELD,
                        path="/notes",
                        value=["note"],
                    )
                ],
                expected_revision=99,
            ),
        )

    result = quote_service.commit(
        "quote-1",
        change(
            "change-2",
            "key-2",
            [
                QuoteChangeOperation(
                    operation=ChangeOperationType.SET_FIELD,
                    path="/notes",
                    value=["note"],
                )
            ],
            sequence=3,
        ),
    )

    assert result.conflicts[0].conflict_type == MergeConflictType.SEQUENCE_GAP
    assert result.quote.current_revision == 1


def test_submitted_totals_must_reconcile() -> None:
    initial = snapshot().model_copy(deep=True)
    initial.lines[0].line_total = Money(amount=Decimal("100"), currency="USD")
    initial.totals = QuoteTotals(line_extension=Money(amount=Decimal("99"), currency="USD"))
    quote_service = QuoteVersioningService(InMemoryQuoteVersionRepository())
    quote_service.create(
        CreateQuoteRequest(snapshot=initial, actor="buyer", idempotency_key="create-1")
    )

    with pytest.raises(QuoteValidationError, match="does not reconcile"):
        quote_service.commit(
            "quote-1",
            change(
                "submit",
                "submit-key",
                [QuoteChangeOperation(operation=ChangeOperationType.SUBMIT_QUOTE)],
            ),
        )


def test_stale_no_bid_is_atomic_when_price_changed() -> None:
    quote_service = service()
    quote_service.commit(
        "quote-1",
        change(
            "price-change",
            "price-key",
            [
                QuoteChangeOperation(
                    operation=ChangeOperationType.UPDATE_LINE,
                    entity_id="line-1",
                    changes={"unit_price": {"amount": "9", "currency": "USD"}},
                )
            ],
        ),
    )

    result = quote_service.commit(
        "quote-1",
        change(
            "no-bid",
            "no-bid-key",
            [
                QuoteChangeOperation(
                    operation=ChangeOperationType.NO_BID_LINE,
                    entity_id="line-1",
                    value="Capacity unavailable",
                )
            ],
            base_revision=1,
        ),
    )

    assert result.revision is None
    assert result.conflicts[0].path == "/lines/line-1/unit_price"
    assert result.quote.current.lines[0].response_status == LineResponseStatus.QUOTED
    assert result.quote.current.lines[0].unit_price == Money(amount=9, currency="USD")
