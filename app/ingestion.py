from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field


class CommunicationChannel(StrEnum):
    EMAIL = "EMAIL"
    API = "API"
    PORTAL = "PORTAL"
    EDI = "EDI"
    OTHER = "OTHER"


class ArtifactKind(StrEnum):
    RAW_MESSAGE = "RAW_MESSAGE"
    BODY = "BODY"
    ATTACHMENT = "ATTACHMENT"


class CandidateRelationship(StrEnum):
    INITIAL_QUOTE = "INITIAL_QUOTE"
    SUPPLEMENT = "SUPPLEMENT"
    CORRECTION = "CORRECTION"
    FULL_REPLACEMENT = "FULL_REPLACEMENT"
    CONFIRMATION = "CONFIRMATION"
    WITHDRAWAL = "WITHDRAWAL"
    UNKNOWN = "UNKNOWN"


class ResolutionMethod(StrEnum):
    AUTOMATIC = "AUTOMATIC"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    MANUAL = "MANUAL"
    UNRESOLVED = "UNRESOLVED"


class ConflictFlagType(StrEnum):
    FIELD_CONFLICT = "FIELD_CONFLICT"
    ORDERING_CONFLICT = "ORDERING_CONFLICT"
    SPECIFICATION_CONFLICT = "SPECIFICATION_CONFLICT"
    STALE_OR_DUPLICATE = "STALE_OR_DUPLICATE"
    OTHER = "OTHER"


class ConflictFlagStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class SourceArtifactInput(BaseModel):
    kind: ArtifactKind
    filename: str | None = None
    media_type: str | None = None
    content: str


class SourceArtifact(BaseModel):
    artifact_id: str
    communication_id: str
    kind: ArtifactKind
    filename: str | None = None
    media_type: str | None = None
    content: str
    checksum_sha256: str


class InboundCommunicationInput(BaseModel):
    channel: CommunicationChannel
    mailbox_account_id: str | None = None
    provider_message_id: str | None = None
    rfc_message_id: str | None = None
    transport_event_id: str | None = None
    sender: str
    recipients: list[str] = Field(default_factory=list)
    subject: str | None = None
    thread_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    sent_at: datetime | None = None
    received_at: datetime | None = None
    raw_message: str | None = None
    raw_body: str
    attachments: list[SourceArtifactInput] = Field(default_factory=list)


class InboundCommunication(BaseModel):
    communication_id: str
    channel: CommunicationChannel
    mailbox_account_id: str | None = None
    provider_message_id: str | None = None
    rfc_message_id: str | None = None
    transport_event_id: str | None = None
    sender: str
    recipients: list[str]
    subject: str | None = None
    thread_id: str | None = None
    in_reply_to: str | None = None
    references: list[str]
    sent_at: datetime | None = None
    received_at: datetime | None = None
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    raw_body: str
    newly_authored_body: str
    raw_message_checksum_sha256: str | None = None
    composite_fingerprint: str
    artifact_ids: list[str]


class IngestionResult(BaseModel):
    communication: InboundCommunication
    artifacts: list[SourceArtifact]
    is_retry: bool = False
    duplicate_of_communication_id: str | None = None
    possible_resubmission_of: list[str] = Field(default_factory=list)


class RFQMatchContext(BaseModel):
    rfq_id: str
    supplier_ids: list[str] = Field(default_factory=list)
    supplier_addresses: list[str] = Field(default_factory=list)
    item_identifiers: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    contact_addresses: list[str] = Field(default_factory=list)
    active: bool = True


class ResolutionEvidence(BaseModel):
    evidence_type: str
    value: str
    weight: float


class RFQResolutionCandidate(BaseModel):
    rfq_id: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[ResolutionEvidence]


class RFQResolution(BaseModel):
    resolution_id: str
    communication_id: str
    method: ResolutionMethod
    resolved_rfq_ids: list[str] = Field(default_factory=list)
    candidates: list[RFQResolutionCandidate] = Field(default_factory=list)
    resolved_by: str | None = None
    resolved_at: datetime | None = None


class ManualResolutionRequest(BaseModel):
    rfq_ids: list[str] = Field(min_length=1)
    actor: str = Field(min_length=1)


class QuoteChangeCandidate(BaseModel):
    candidate_id: str
    communication_id: str
    artifact_id: str | None = None
    rfq_id: str | None = None
    field_path: str
    typed_value: Any
    confidence: float = Field(ge=0, le=1)
    location: str | None = None
    source_text: str
    relationship: CandidateRelationship = CandidateRelationship.UNKNOWN
    based_on_revision: int | None = None


class CreateConflictFlagRequest(BaseModel):
    flag_type: ConflictFlagType
    field_path: str | None = None
    quote_id: str | None = None
    based_on_revision: int | None = Field(default=None, ge=1)
    current_revision: int | None = Field(default=None, ge=1)
    base_value: Any = None
    current_value: Any = None
    proposed_value: Any = None
    note: str = Field(min_length=1)
    created_by: str = Field(min_length=1)


class CommunicationConflictFlag(CreateConflictFlagRequest):
    flag_id: str
    communication_id: str
    status: ConflictFlagStatus = ConflictFlagStatus.OPEN
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ChangeCandidateBatch(BaseModel):
    candidates: list[QuoteChangeCandidate] = Field(min_length=1)


class CommunicationDetail(BaseModel):
    communication: InboundCommunication
    artifacts: list[SourceArtifact]
    resolutions: list[RFQResolution] = Field(default_factory=list)
    change_candidates: list[QuoteChangeCandidate] = Field(default_factory=list)
    conflict_flags: list[CommunicationConflictFlag] = Field(default_factory=list)


class CommunicationRepository(Protocol):
    def find_retry(self, item: InboundCommunication) -> str | None: ...

    def find_by_fingerprint(self, fingerprint: str) -> list[str]: ...

    def save(self, item: InboundCommunication, artifacts: list[SourceArtifact]) -> None: ...

    def get(self, communication_id: str) -> InboundCommunication | None: ...

    def find_by_message_reference(self, reference: str) -> InboundCommunication | None: ...

    def find_by_thread(self, thread_id: str) -> list[InboundCommunication]: ...

    def artifacts(self, communication_id: str) -> list[SourceArtifact]: ...

    def save_resolution(self, resolution: RFQResolution) -> None: ...

    def resolutions(self, communication_id: str) -> list[RFQResolution]: ...

    def save_candidates(self, candidates: list[QuoteChangeCandidate]) -> None: ...

    def candidates(self, communication_id: str) -> list[QuoteChangeCandidate]: ...

    def save_conflict_flag(self, flag: CommunicationConflictFlag) -> None: ...

    def conflict_flags(self, communication_id: str) -> list[CommunicationConflictFlag]: ...

    def conflict_flags_for_quote(self, quote_id: str) -> list[CommunicationConflictFlag]: ...


class InMemoryCommunicationRepository:
    def __init__(self) -> None:
        self._communications: dict[str, InboundCommunication] = {}
        self._artifacts: dict[str, list[SourceArtifact]] = {}
        self._resolutions: dict[str, list[RFQResolution]] = {}
        self._candidates: dict[str, list[QuoteChangeCandidate]] = {}
        self._conflict_flags: dict[str, list[CommunicationConflictFlag]] = {}
        self._provider_ids: dict[tuple[str | None, str], str] = {}
        self._message_ids: dict[str, str] = {}
        self._transport_ids: dict[str, str] = {}
        self._raw_message_checksums: dict[str, str] = {}
        self._fingerprints: dict[str, list[str]] = {}
        self._lock = RLock()

    def find_retry(self, item: InboundCommunication) -> str | None:
        with self._lock:
            if item.provider_message_id:
                match = self._provider_ids.get((item.mailbox_account_id, item.provider_message_id))
                if match:
                    return match
            if item.rfc_message_id and item.rfc_message_id in self._message_ids:
                return self._message_ids[item.rfc_message_id]
            if item.transport_event_id and item.transport_event_id in self._transport_ids:
                return self._transport_ids[item.transport_event_id]
            if item.raw_message_checksum_sha256:
                return self._raw_message_checksums.get(item.raw_message_checksum_sha256)
            return None

    def find_by_fingerprint(self, fingerprint: str) -> list[str]:
        with self._lock:
            return list(self._fingerprints.get(fingerprint, []))

    def save(self, item: InboundCommunication, artifacts: list[SourceArtifact]) -> None:
        with self._lock:
            self._communications[item.communication_id] = item.model_copy(deep=True)
            self._artifacts[item.communication_id] = [
                artifact.model_copy(deep=True) for artifact in artifacts
            ]
            if item.provider_message_id:
                self._provider_ids[(item.mailbox_account_id, item.provider_message_id)] = (
                    item.communication_id
                )
            if item.rfc_message_id:
                self._message_ids[item.rfc_message_id] = item.communication_id
            if item.transport_event_id:
                self._transport_ids[item.transport_event_id] = item.communication_id
            if item.raw_message_checksum_sha256:
                self._raw_message_checksums[item.raw_message_checksum_sha256] = (
                    item.communication_id
                )
            self._fingerprints.setdefault(item.composite_fingerprint, []).append(
                item.communication_id
            )

    def get(self, communication_id: str) -> InboundCommunication | None:
        with self._lock:
            item = self._communications.get(communication_id)
            return item.model_copy(deep=True) if item else None

    def find_by_message_reference(self, reference: str) -> InboundCommunication | None:
        with self._lock:
            communication_id = self._message_ids.get(reference)
            if communication_id is None:
                communication_id = next(
                    (
                        item.communication_id
                        for item in self._communications.values()
                        if item.provider_message_id == reference
                    ),
                    None,
                )
            item = self._communications.get(communication_id or "")
            return item.model_copy(deep=True) if item else None

    def find_by_thread(self, thread_id: str) -> list[InboundCommunication]:
        with self._lock:
            return [
                item.model_copy(deep=True)
                for item in self._communications.values()
                if item.thread_id == thread_id
            ]

    def artifacts(self, communication_id: str) -> list[SourceArtifact]:
        with self._lock:
            return [
                item.model_copy(deep=True) for item in self._artifacts.get(communication_id, [])
            ]

    def save_resolution(self, resolution: RFQResolution) -> None:
        with self._lock:
            self._resolutions.setdefault(resolution.communication_id, []).append(
                resolution.model_copy(deep=True)
            )

    def resolutions(self, communication_id: str) -> list[RFQResolution]:
        with self._lock:
            return [
                item.model_copy(deep=True) for item in self._resolutions.get(communication_id, [])
            ]

    def save_candidates(self, candidates: list[QuoteChangeCandidate]) -> None:
        with self._lock:
            for candidate in candidates:
                self._candidates.setdefault(candidate.communication_id, []).append(
                    candidate.model_copy(deep=True)
                )

    def candidates(self, communication_id: str) -> list[QuoteChangeCandidate]:
        with self._lock:
            return [
                item.model_copy(deep=True) for item in self._candidates.get(communication_id, [])
            ]

    def save_conflict_flag(self, flag: CommunicationConflictFlag) -> None:
        with self._lock:
            self._conflict_flags.setdefault(flag.communication_id, []).append(
                flag.model_copy(deep=True)
            )

    def conflict_flags(self, communication_id: str) -> list[CommunicationConflictFlag]:
        with self._lock:
            return [
                item.model_copy(deep=True)
                for item in self._conflict_flags.get(communication_id, [])
            ]

    def conflict_flags_for_quote(self, quote_id: str) -> list[CommunicationConflictFlag]:
        with self._lock:
            return [
                item.model_copy(deep=True)
                for flags in self._conflict_flags.values()
                for item in flags
                if item.quote_id == quote_id and item.status == ConflictFlagStatus.OPEN
            ]


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize(value: str | None) -> str:
    return " ".join((value or "").lower().split())


def _newly_authored_body(body: str) -> str:
    lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if re.match(r"^on .+wrote:$", stripped, flags=re.IGNORECASE):
            break
        if stripped.startswith(">") or stripped in {"--", "-- "}:
            break
        lines.append(line)
    return "\n".join(lines).strip()


class CommunicationIngestionService:
    def __init__(self, repository: CommunicationRepository) -> None:
        self._repository = repository

    def ingest(self, request: InboundCommunicationInput) -> IngestionResult:
        communication_id = str(uuid4())
        body_artifact = SourceArtifact(
            artifact_id=str(uuid4()),
            communication_id=communication_id,
            kind=ArtifactKind.BODY,
            filename=None,
            media_type="text/plain",
            content=request.raw_body,
            checksum_sha256=_sha256(request.raw_body),
        )
        artifacts = [body_artifact]
        if request.raw_message is not None:
            artifacts.append(
                SourceArtifact(
                    artifact_id=str(uuid4()),
                    communication_id=communication_id,
                    kind=ArtifactKind.RAW_MESSAGE,
                    filename=None,
                    media_type="message/rfc822",
                    content=request.raw_message,
                    checksum_sha256=_sha256(request.raw_message),
                )
            )
        for source in request.attachments:
            artifacts.append(
                SourceArtifact(
                    artifact_id=str(uuid4()),
                    communication_id=communication_id,
                    kind=source.kind,
                    filename=source.filename,
                    media_type=source.media_type,
                    content=source.content,
                    checksum_sha256=_sha256(source.content),
                )
            )
        raw_checksum = _sha256(request.raw_message) if request.raw_message is not None else None
        authored = _newly_authored_body(request.raw_body)
        sent_time = request.sent_at.isoformat() if request.sent_at else ""
        fingerprint_material = "|".join(
            [
                _normalize(request.sender),
                ",".join(sorted(_normalize(item) for item in request.recipients)),
                sent_time,
                _normalize(request.subject),
                _normalize(authored),
                ",".join(
                    sorted(
                        item.checksum_sha256
                        for item in artifacts
                        if item.kind == ArtifactKind.ATTACHMENT
                    )
                ),
            ]
        )
        item = InboundCommunication(
            communication_id=communication_id,
            channel=request.channel,
            mailbox_account_id=request.mailbox_account_id,
            provider_message_id=request.provider_message_id,
            rfc_message_id=request.rfc_message_id,
            transport_event_id=request.transport_event_id,
            sender=request.sender,
            recipients=request.recipients,
            subject=request.subject,
            thread_id=request.thread_id,
            in_reply_to=request.in_reply_to,
            references=request.references,
            sent_at=request.sent_at,
            received_at=request.received_at,
            raw_body=request.raw_body,
            newly_authored_body=authored,
            raw_message_checksum_sha256=raw_checksum,
            composite_fingerprint=_sha256(fingerprint_material),
            artifact_ids=[artifact.artifact_id for artifact in artifacts],
        )
        duplicate_id = self._repository.find_retry(item)
        if duplicate_id:
            existing = self._repository.get(duplicate_id)
            if existing is None:
                raise RuntimeError("Deduplication index points to a missing communication")
            return IngestionResult(
                communication=existing,
                artifacts=self._repository.artifacts(duplicate_id),
                is_retry=True,
                duplicate_of_communication_id=duplicate_id,
            )
        possible_resubmissions = self._repository.find_by_fingerprint(item.composite_fingerprint)
        self._repository.save(item, artifacts)
        return IngestionResult(
            communication=item,
            artifacts=artifacts,
            possible_resubmission_of=possible_resubmissions,
        )


class RFQResolutionService:
    def __init__(self, repository: CommunicationRepository) -> None:
        self._repository = repository
        self._contexts: dict[str, RFQMatchContext] = {}

    def register(self, context: RFQMatchContext) -> RFQMatchContext:
        self._contexts[context.rfq_id] = context.model_copy(deep=True)
        return context

    def resolve(self, communication_id: str) -> RFQResolution:
        communication = self._repository.get(communication_id)
        if communication is None:
            raise KeyError(communication_id)
        artifacts = self._repository.artifacts(communication_id)
        searchable = "\n".join(
            [communication.subject or "", communication.newly_authored_body]
            + [
                artifact.content
                for artifact in artifacts
                if artifact.kind == ArtifactKind.ATTACHMENT
            ]
        ).lower()
        candidates: list[RFQResolutionCandidate] = []
        for context in self._contexts.values():
            if not context.active:
                continue
            evidence: list[ResolutionEvidence] = []
            if context.rfq_id.lower() in searchable:
                evidence.append(
                    ResolutionEvidence(
                        evidence_type="EXPLICIT_RFQ_ID",
                        value=context.rfq_id,
                        weight=1.0,
                    )
                )
            prior_resolutions = []
            referenced_ids = [communication.in_reply_to, *communication.references]
            for reference in filter(None, referenced_ids):
                referenced_communication = self._repository.find_by_message_reference(reference)
                if referenced_communication is None:
                    continue
                referenced = next(
                    (
                        item
                        for item in self._repository.resolutions(
                            referenced_communication.communication_id
                        )
                        if context.rfq_id in item.resolved_rfq_ids
                    ),
                    None,
                )
                if referenced:
                    prior_resolutions.append(referenced)
            if prior_resolutions:
                evidence.append(
                    ResolutionEvidence(
                        evidence_type="REPLY_REFERENCE",
                        value=context.rfq_id,
                        weight=0.95,
                    )
                )
            if communication.thread_id:
                thread_matches = [
                    prior
                    for prior in self._repository.find_by_thread(communication.thread_id)
                    if prior.communication_id != communication_id
                    for resolution in self._repository.resolutions(prior.communication_id)
                    if context.rfq_id in resolution.resolved_rfq_ids
                ]
                if thread_matches:
                    evidence.append(
                        ResolutionEvidence(
                            evidence_type="THREAD_ASSOCIATION",
                            value=communication.thread_id,
                            weight=0.90,
                        )
                    )
            sender = _normalize(communication.sender)
            if sender in {_normalize(item) for item in context.supplier_addresses}:
                evidence.append(
                    ResolutionEvidence(
                        evidence_type="SUPPLIER_INVITATION",
                        value=communication.sender,
                        weight=0.45,
                    )
                )
            for identifier in context.item_identifiers:
                if identifier.lower() in searchable:
                    evidence.append(
                        ResolutionEvidence(
                            evidence_type="ITEM_IDENTIFIER",
                            value=identifier,
                            weight=0.25,
                        )
                    )
            for location in context.locations:
                if location.lower() in searchable:
                    evidence.append(
                        ResolutionEvidence(
                            evidence_type="LOCATION",
                            value=location,
                            weight=0.10,
                        )
                    )
            if evidence:
                confidence = min(1.0, sum(item.weight for item in evidence))
                candidates.append(
                    RFQResolutionCandidate(
                        rfq_id=context.rfq_id,
                        confidence=confidence,
                        evidence=evidence,
                    )
                )
        candidates.sort(key=lambda item: item.confidence, reverse=True)
        method = ResolutionMethod.UNRESOLVED
        resolved: list[str] = []
        if candidates:
            runner_up = candidates[1].confidence if len(candidates) > 1 else 0.0
            if candidates[0].confidence >= 0.9 and candidates[0].confidence - runner_up >= 0.15:
                method = ResolutionMethod.AUTOMATIC
                resolved = [candidates[0].rfq_id]
            else:
                method = ResolutionMethod.REVIEW_REQUIRED
        resolution = RFQResolution(
            resolution_id=str(uuid4()),
            communication_id=communication_id,
            method=method,
            resolved_rfq_ids=resolved,
            candidates=candidates,
            resolved_at=datetime.now(UTC) if resolved else None,
        )
        self._repository.save_resolution(resolution)
        return resolution

    def manually_resolve(
        self, communication_id: str, rfq_ids: list[str], actor: str
    ) -> RFQResolution:
        if self._repository.get(communication_id) is None:
            raise KeyError(communication_id)
        unknown = set(rfq_ids) - self._contexts.keys()
        if unknown:
            raise ValueError(f"Unknown RFQ IDs: {', '.join(sorted(unknown))}")
        resolution = RFQResolution(
            resolution_id=str(uuid4()),
            communication_id=communication_id,
            method=ResolutionMethod.MANUAL,
            resolved_rfq_ids=rfq_ids,
            resolved_by=actor,
            resolved_at=datetime.now(UTC),
        )
        self._repository.save_resolution(resolution)
        return resolution
