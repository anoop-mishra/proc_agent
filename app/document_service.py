from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, Field

from app.ingestion import (
    CommunicationConflictFlag,
    CommunicationIngestionService,
    ConflictFlagType,
    InboundCommunicationInput,
    IngestionResult,
    InMemoryCommunicationRepository,
)
from app.quote_models import CommitResult, SupplierQuote
from app.update_provider import QuoteUpdateApplicationService
from app.versioning import QuoteVersioningService


class SupplierDocumentInput(BaseModel):
    filename: str = Field(min_length=1)
    supplier_name: str | None = None
    communication: InboundCommunicationInput


class SupplierDocumentBatchRequest(BaseModel):
    documents: list[SupplierDocumentInput] = Field(min_length=1)


class SupplierDocumentResult(BaseModel):
    filename: str
    supplier_name: str | None = None
    logical_order: int
    ingestion: IngestionResult
    matched_quote_id: str | None = None
    match_reason: str | None = None
    application: CommitResult | None = None
    conflict_flags: list[CommunicationConflictFlag] = Field(default_factory=list)


class SupplierDocumentBatchResult(BaseModel):
    documents: list[SupplierDocumentResult]


def _normalize(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _match_quote(
    document: SupplierDocumentInput, quotes: list[SupplierQuote]
) -> tuple[SupplierQuote | None, str | None]:
    supplier = _normalize(document.supplier_name)
    sender = _normalize(document.communication.sender)
    searchable = _normalize(
        " ".join(
            [
                document.communication.subject or "",
                document.communication.raw_body,
                document.communication.sender,
            ]
        )
    )
    scored: list[tuple[int, SupplierQuote, str]] = []
    for quote in quotes:
        name = _normalize(quote.current.supplier_name)
        supplier_id = _normalize(quote.supplier_id)
        score = 0
        reasons: list[str] = []
        if supplier and supplier in {name, supplier_id}:
            score += 10
            reasons.append("supplier metadata")
        elif supplier and (supplier in name or name in supplier):
            score += 6
            reasons.append("supplier name")
        name_tokens = {item for item in name.split() if len(item) > 2}
        if name_tokens and len(name_tokens & set(sender.split())) >= min(2, len(name_tokens)):
            score += 5
            reasons.append("sender domain")
        elif name.replace(" ", "") and name.replace(" ", "") in sender.replace(" ", ""):
            score += 5
            reasons.append("sender domain")
        if _normalize(quote.rfq_id) in searchable:
            score += 3
            reasons.append("RFQ identifier")
        if score:
            scored.append((score, quote, ", ".join(reasons)))
    if not scored:
        return None, None
    scored.sort(key=lambda item: item[0], reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None, "ambiguous supplier match"
    return scored[0][1], scored[0][2]


class SupplierDocumentService:
    def __init__(
        self,
        ingestion: CommunicationIngestionService,
        communications: InMemoryCommunicationRepository,
        versioning: QuoteVersioningService,
        updates: QuoteUpdateApplicationService,
    ) -> None:
        self._ingestion = ingestion
        self._communications = communications
        self._versioning = versioning
        self._updates = updates

    def ingest(self, request: SupplierDocumentBatchRequest) -> SupplierDocumentBatchResult:
        staged = []
        for index, document in enumerate(request.documents):
            ingestion = self._ingestion.ingest(document.communication)
            communication = ingestion.communication
            ordering_time = (
                communication.sent_at
                or communication.received_at
                or datetime.min.replace(tzinfo=UTC)
            )
            if ordering_time.tzinfo is None:
                ordering_time = ordering_time.replace(tzinfo=UTC)
            staged.append((index, document, ingestion, ordering_time))

        # Newest evidence is established first. Older stale documents can still merge
        # unrelated facts, but cannot silently replace a newer value.
        ordered = sorted(staged, key=lambda item: (item[3], -item[0]), reverse=True)
        results: list[SupplierDocumentResult] = []
        quotes = self._versioning.list_quotes()
        for logical_order, (_, document, ingestion, _) in enumerate(ordered, start=1):
            quote, match_reason = _match_quote(document, quotes)
            application = None
            flags: list[CommunicationConflictFlag] = []
            if quote is not None:
                application = self._updates.apply(
                    quote.quote_id, ingestion.communication.communication_id
                )
                if not application.idempotent_replay:
                    accepted = application.accepted_change
                    for conflict in application.conflicts:
                        flag_type = (
                            ConflictFlagType(conflict.conflict_type.value)
                            if conflict.conflict_type.value
                            in {
                                ConflictFlagType.FIELD_CONFLICT.value,
                                ConflictFlagType.ORDERING_CONFLICT.value,
                            }
                            else ConflictFlagType.OTHER
                        )
                        flag = CommunicationConflictFlag(
                            flag_id=str(uuid4()),
                            communication_id=ingestion.communication.communication_id,
                            flag_type=flag_type,
                            field_path=conflict.path,
                            quote_id=quote.quote_id,
                            based_on_revision=(accepted.based_on_revision if accepted else None),
                            current_revision=application.quote.current_revision,
                            base_value=conflict.base_value,
                            current_value=conflict.current_value,
                            proposed_value=conflict.proposed_value,
                            note=conflict.message,
                            created_by="document-ingestion-service",
                            created_at=datetime.now(UTC),
                        )
                        self._communications.save_conflict_flag(flag)
                        flags.append(flag)
            results.append(
                SupplierDocumentResult(
                    filename=document.filename,
                    supplier_name=document.supplier_name,
                    logical_order=logical_order,
                    ingestion=ingestion,
                    matched_quote_id=quote.quote_id if quote else None,
                    match_reason=match_reason,
                    application=application,
                    conflict_flags=flags,
                )
            )
        return SupplierDocumentBatchResult(documents=results)
