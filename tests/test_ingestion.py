from datetime import UTC, datetime

from app.ingestion import (
    CommunicationChannel,
    CommunicationIngestionService,
    InboundCommunicationInput,
    InMemoryCommunicationRepository,
    ResolutionMethod,
    RFQMatchContext,
    RFQResolutionService,
)


def communication(
    *,
    body: str = "RFQ-100 price is $10",
    provider_id: str | None = None,
    message_id: str | None = None,
    subject: str = "Quote for RFQ-100",
    thread_id: str | None = None,
    in_reply_to: str | None = None,
    raw_message: str | None = None,
) -> InboundCommunicationInput:
    return InboundCommunicationInput(
        channel=CommunicationChannel.EMAIL,
        mailbox_account_id="buying@example.com",
        provider_message_id=provider_id,
        rfc_message_id=message_id,
        sender="sales@supplier.example",
        recipients=["buying@example.com"],
        subject=subject,
        thread_id=thread_id,
        in_reply_to=in_reply_to,
        sent_at=datetime(2026, 10, 1, 12, tzinfo=UTC),
        raw_message=raw_message,
        raw_body=body,
    )


def test_provider_retry_returns_original_immutable_communication() -> None:
    repository = InMemoryCommunicationRepository()
    service = CommunicationIngestionService(repository)

    first = service.ingest(communication(provider_id="provider-1"))
    retry = service.ingest(
        communication(body="Webhook representation changed", provider_id="provider-1")
    )

    assert retry.is_retry is True
    assert retry.duplicate_of_communication_id == first.communication.communication_id
    assert retry.communication.raw_body == "RFQ-100 price is $10"


def test_exact_raw_message_retry_is_deduplicated_without_transport_ids() -> None:
    repository = InMemoryCommunicationRepository()
    service = CommunicationIngestionService(repository)

    first = service.ingest(communication(raw_message="complete RFC822 bytes"))
    retry = service.ingest(communication(raw_message="complete RFC822 bytes"))

    assert retry.is_retry is True
    assert retry.duplicate_of_communication_id == first.communication.communication_id


def test_normalized_match_is_flagged_but_not_discarded() -> None:
    repository = InMemoryCommunicationRepository()
    service = CommunicationIngestionService(repository)

    first = service.ingest(communication(body="RFQ-100 price is $10"))
    resend = service.ingest(communication(body="RFQ-100   price is   $10"))

    assert resend.is_retry is False
    assert resend.communication.communication_id != first.communication.communication_id
    assert resend.possible_resubmission_of == [first.communication.communication_id]


def test_quoted_history_and_signature_are_excluded_from_authored_body() -> None:
    repository = InMemoryCommunicationRepository()
    service = CommunicationIngestionService(repository)

    result = service.ingest(
        communication(
            body=(
                "New unit price is $9.50\n\n"
                "On Tue, Buyer wrote:\n"
                "> Previous unit price was $10\n"
                "-- \nSupplier signature"
            )
        )
    )

    assert result.communication.newly_authored_body == "New unit price is $9.50"
    assert result.communication.raw_body.endswith("Supplier signature")


def test_explicit_rfq_id_auto_resolves_with_explainable_evidence() -> None:
    repository = InMemoryCommunicationRepository()
    ingestion = CommunicationIngestionService(repository)
    resolver = RFQResolutionService(repository)
    resolver.register(
        RFQMatchContext(
            rfq_id="RFQ-100",
            supplier_addresses=["sales@supplier.example"],
            item_identifiers=["BRK-4471-A"],
        )
    )
    item = ingestion.ingest(communication())

    resolution = resolver.resolve(item.communication.communication_id)

    assert resolution.method == ResolutionMethod.AUTOMATIC
    assert resolution.resolved_rfq_ids == ["RFQ-100"]
    assert resolution.candidates[0].evidence[0].evidence_type == "EXPLICIT_RFQ_ID"


def test_ambiguous_item_match_requires_review() -> None:
    repository = InMemoryCommunicationRepository()
    ingestion = CommunicationIngestionService(repository)
    resolver = RFQResolutionService(repository)
    for rfq_id in ("RFQ-100", "RFQ-200"):
        resolver.register(
            RFQMatchContext(
                rfq_id=rfq_id,
                supplier_addresses=["sales@supplier.example"],
                item_identifiers=["BRK-4471-A"],
            )
        )
    item = ingestion.ingest(
        communication(body="Quote update for BRK-4471-A", subject="Quote update")
    )

    resolution = resolver.resolve(item.communication.communication_id)

    assert resolution.method == ResolutionMethod.REVIEW_REQUIRED
    assert resolution.resolved_rfq_ids == []
    assert {item.rfq_id for item in resolution.candidates} == {"RFQ-100", "RFQ-200"}


def test_existing_thread_association_resolves_follow_up() -> None:
    repository = InMemoryCommunicationRepository()
    ingestion = CommunicationIngestionService(repository)
    resolver = RFQResolutionService(repository)
    resolver.register(RFQMatchContext(rfq_id="RFQ-100"))
    initial = ingestion.ingest(
        communication(
            provider_id="provider-1",
            message_id="<message-1@example.com>",
            thread_id="thread-1",
        )
    )
    resolver.resolve(initial.communication.communication_id)
    follow_up = ingestion.ingest(
        communication(
            body="Correction: lead time is six weeks",
            subject="Re: quote",
            provider_id="provider-2",
            message_id="<message-2@example.com>",
            thread_id="thread-1",
            in_reply_to="<message-1@example.com>",
        )
    )

    resolution = resolver.resolve(follow_up.communication.communication_id)

    assert resolution.method == ResolutionMethod.AUTOMATIC
    assert resolution.resolved_rfq_ids == ["RFQ-100"]
    evidence_types = {item.evidence_type for item in resolution.candidates[0].evidence}
    assert {"REPLY_REFERENCE", "THREAD_ASSOCIATION"}.issubset(evidence_types)
