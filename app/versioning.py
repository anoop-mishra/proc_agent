from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from threading import RLock
from typing import Any, Protocol

from pydantic import ValidationError

from app.quote_models import (
    ChangeOperationType,
    CommitResult,
    CreateQuoteRequest,
    LineResponseStatus,
    MergeConflict,
    MergeConflictType,
    QuoteChangeOperation,
    QuoteChangeRequest,
    QuoteStatus,
    SupplierQuote,
    SupplierQuoteChange,
    SupplierQuoteRevision,
    SupplierQuoteSnapshot,
)


class QuoteNotFoundError(KeyError):
    pass


class QuoteAlreadyExistsError(ValueError):
    pass


class ConcurrentQuoteUpdateError(RuntimeError):
    pass


class QuoteValidationError(ValueError):
    pass


class QuoteVersionRepository(Protocol):
    def create(self, quote: SupplierQuote, revision: SupplierQuoteRevision) -> None: ...

    def get(self, quote_id: str) -> SupplierQuote | None: ...

    def list_quotes(self) -> list[SupplierQuote]: ...

    def get_revision(self, quote_id: str, revision: int) -> SupplierQuoteRevision | None: ...

    def list_revisions(self, quote_id: str) -> list[SupplierQuoteRevision]: ...

    def list_changes(self, quote_id: str) -> list[SupplierQuoteChange]: ...

    def get_idempotent_result(self, quote_id: str, key: str) -> CommitResult | None: ...

    def save_idempotent_result(self, quote_id: str, key: str, result: CommitResult) -> None: ...

    def last_supplier_sequence(self, quote_id: str) -> int | None: ...

    def compare_and_swap(
        self,
        expected_revision: int,
        quote: SupplierQuote,
        revision: SupplierQuoteRevision,
        change: SupplierQuoteChange,
        result: CommitResult,
    ) -> bool: ...


class InMemoryQuoteVersionRepository:
    def __init__(self) -> None:
        self._quotes: dict[str, SupplierQuote] = {}
        self._revisions: dict[str, dict[int, SupplierQuoteRevision]] = {}
        self._changes: dict[str, list[SupplierQuoteChange]] = {}
        self._idempotency: dict[tuple[str, str], CommitResult] = {}
        self._last_sequence: dict[str, int] = {}
        self._lock = RLock()

    def create(self, quote: SupplierQuote, revision: SupplierQuoteRevision) -> None:
        with self._lock:
            if quote.quote_id in self._quotes:
                raise QuoteAlreadyExistsError(f"Quote {quote.quote_id} already exists")
            self._quotes[quote.quote_id] = quote.model_copy(deep=True)
            self._revisions[quote.quote_id] = {1: revision.model_copy(deep=True)}
            self._changes[quote.quote_id] = []

    def get(self, quote_id: str) -> SupplierQuote | None:
        with self._lock:
            quote = self._quotes.get(quote_id)
            return quote.model_copy(deep=True) if quote else None

    def list_quotes(self) -> list[SupplierQuote]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self._quotes.values()]

    def get_revision(self, quote_id: str, revision: int) -> SupplierQuoteRevision | None:
        with self._lock:
            item = self._revisions.get(quote_id, {}).get(revision)
            return item.model_copy(deep=True) if item else None

    def list_revisions(self, quote_id: str) -> list[SupplierQuoteRevision]:
        with self._lock:
            revisions = self._revisions.get(quote_id, {})
            return [revisions[key].model_copy(deep=True) for key in sorted(revisions)]

    def list_changes(self, quote_id: str) -> list[SupplierQuoteChange]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self._changes.get(quote_id, [])]

    def get_idempotent_result(self, quote_id: str, key: str) -> CommitResult | None:
        with self._lock:
            result = self._idempotency.get((quote_id, key))
            return result.model_copy(deep=True) if result else None

    def save_idempotent_result(self, quote_id: str, key: str, result: CommitResult) -> None:
        with self._lock:
            self._idempotency[(quote_id, key)] = result.model_copy(deep=True)

    def last_supplier_sequence(self, quote_id: str) -> int | None:
        with self._lock:
            return self._last_sequence.get(quote_id)

    def compare_and_swap(
        self,
        expected_revision: int,
        quote: SupplierQuote,
        revision: SupplierQuoteRevision,
        change: SupplierQuoteChange,
        result: CommitResult,
    ) -> bool:
        with self._lock:
            current = self._quotes.get(quote.quote_id)
            if current is None or current.current_revision != expected_revision:
                return False
            self._quotes[quote.quote_id] = quote.model_copy(deep=True)
            self._revisions[quote.quote_id][revision.committed_revision] = revision.model_copy(
                deep=True
            )
            self._changes[quote.quote_id].append(change.model_copy(deep=True))
            self._idempotency[(quote.quote_id, change.idempotency_key)] = result.model_copy(
                deep=True
            )
            if change.supplier_sequence is not None:
                self._last_sequence[quote.quote_id] = change.supplier_sequence
            return True


_MISSING = {"__missing__": True}


def _find_by_id(items: list[dict[str, Any]], key: str, entity_id: str) -> dict[str, Any] | None:
    return next((item for item in items if item.get(key) == entity_id), None)


def _pointer_parts(path: str) -> list[str]:
    if not path.startswith("/"):
        raise ValueError("Field paths must be JSON pointers beginning with '/'")
    return [part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:]]


def _get_pointer(document: dict[str, Any], path: str) -> Any:
    current: Any = document
    for part in _pointer_parts(path):
        if not isinstance(current, dict) or part not in current:
            return deepcopy(_MISSING)
        current = current[part]
    return deepcopy(current)


def _set_pointer(document: dict[str, Any], path: str, value: Any) -> None:
    parts = _pointer_parts(path)
    if not parts or parts[0] in {"quote_id", "rfq_id", "supplier_id", "lines", "attachments"}:
        raise ValueError(f"SET_FIELD cannot modify protected or entity collection path {path}")
    current: Any = document
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            raise ValueError(f"Unknown field path {path}")
        current = current[part]
    if not isinstance(current, dict) or parts[-1] not in current:
        raise ValueError(f"Unknown field path {path}")
    current[parts[-1]] = deepcopy(value)


def _atomic_operations(operation: QuoteChangeOperation) -> list[QuoteChangeOperation]:
    if operation.operation == ChangeOperationType.UPDATE_LINE:
        return [
            QuoteChangeOperation(
                operation=ChangeOperationType.UPDATE_LINE,
                entity_id=operation.entity_id,
                changes={field: value},
                provenance=operation.provenance,
            )
            for field, value in operation.changes.items()
        ]
    if operation.operation == ChangeOperationType.NO_BID_LINE:
        reason = operation.value
        no_bid_changes = {
            "response_status": LineResponseStatus.NO_BID.value,
            "no_bid_reason": reason,
            "unit_price": None,
            "price_tiers": [],
            "line_total": None,
        }
        return [
            QuoteChangeOperation(
                operation=ChangeOperationType.UPDATE_LINE,
                entity_id=operation.entity_id,
                changes={field: value},
                provenance=operation.provenance,
            )
            for field, value in no_bid_changes.items()
        ]
    return [operation]


def _logical_path(operation: QuoteChangeOperation) -> str:
    if operation.operation == ChangeOperationType.SET_FIELD:
        return operation.path or ""
    if operation.operation == ChangeOperationType.UPDATE_LINE:
        field = next(iter(operation.changes))
        return f"/lines/{operation.entity_id}/{field}"
    if operation.operation == ChangeOperationType.ADD_PRICE_TIER:
        tier_id = (operation.value or {}).get("tier_id", "unknown")
        return f"/lines/{operation.entity_id}/price_tiers/{tier_id}"
    if operation.operation == ChangeOperationType.REMOVE_ATTACHMENT:
        return f"/attachments/{operation.entity_id}"
    if operation.operation in {
        ChangeOperationType.SUBMIT_QUOTE,
        ChangeOperationType.WITHDRAW_QUOTE,
    }:
        return "/status"
    raise ValueError(f"Unsupported operation {operation.operation}")


def _operation_value(document: dict[str, Any], operation: QuoteChangeOperation) -> Any:
    if operation.operation == ChangeOperationType.SET_FIELD:
        return _get_pointer(document, operation.path or "")
    if operation.operation == ChangeOperationType.UPDATE_LINE:
        line = _find_by_id(document["lines"], "quote_line_id", operation.entity_id or "")
        if line is None:
            return deepcopy(_MISSING)
        return deepcopy(line.get(next(iter(operation.changes)), _MISSING))
    if operation.operation == ChangeOperationType.ADD_PRICE_TIER:
        tier_id = (operation.value or {}).get("tier_id")
        if tier_id in document.get("tombstones", {}).get("price_tiers", []):
            return {"__tombstoned__": True}
        line = _find_by_id(document["lines"], "quote_line_id", operation.entity_id or "")
        if line is None:
            return deepcopy(_MISSING)
        tier = _find_by_id(line["price_tiers"], "tier_id", tier_id)
        return deepcopy(tier) if tier else deepcopy(_MISSING)
    if operation.operation == ChangeOperationType.REMOVE_ATTACHMENT:
        if operation.entity_id in document.get("tombstones", {}).get("attachments", []):
            return {"__tombstoned__": True}
        attachment = _find_by_id(
            document["attachments"], "attachment_id", operation.entity_id or ""
        )
        return deepcopy(attachment) if attachment else deepcopy(_MISSING)
    if operation.operation in {
        ChangeOperationType.SUBMIT_QUOTE,
        ChangeOperationType.WITHDRAW_QUOTE,
    }:
        return document["status"]
    raise ValueError(f"Unsupported operation {operation.operation}")


def _proposed_value(operation: QuoteChangeOperation) -> Any:
    if operation.operation == ChangeOperationType.SET_FIELD:
        return deepcopy(operation.value)
    if operation.operation == ChangeOperationType.UPDATE_LINE:
        return deepcopy(next(iter(operation.changes.values())))
    if operation.operation == ChangeOperationType.ADD_PRICE_TIER:
        return deepcopy(operation.value)
    if operation.operation == ChangeOperationType.REMOVE_ATTACHMENT:
        return {"__tombstoned__": True}
    if operation.operation == ChangeOperationType.SUBMIT_QUOTE:
        return QuoteStatus.SUBMITTED.value
    if operation.operation == ChangeOperationType.WITHDRAW_QUOTE:
        return QuoteStatus.WITHDRAWN.value
    raise ValueError(f"Unsupported operation {operation.operation}")


def _apply_operation(document: dict[str, Any], operation: QuoteChangeOperation) -> None:
    if operation.operation == ChangeOperationType.SET_FIELD:
        _set_pointer(document, operation.path or "", operation.value)
    elif operation.operation == ChangeOperationType.UPDATE_LINE:
        line = _find_by_id(document["lines"], "quote_line_id", operation.entity_id or "")
        if line is None:
            raise ValueError(f"Unknown quote line {operation.entity_id}")
        field, value = next(iter(operation.changes.items()))
        if field in {"quote_line_id", "rfq_line_id"} or field not in line:
            raise ValueError(f"Cannot update quote line field {field}")
        line[field] = deepcopy(value)
    elif operation.operation == ChangeOperationType.ADD_PRICE_TIER:
        line = _find_by_id(document["lines"], "quote_line_id", operation.entity_id or "")
        if line is None:
            raise ValueError(f"Unknown quote line {operation.entity_id}")
        line["price_tiers"].append(deepcopy(operation.value))
    elif operation.operation == ChangeOperationType.REMOVE_ATTACHMENT:
        document["attachments"] = [
            item for item in document["attachments"] if item["attachment_id"] != operation.entity_id
        ]
        tombstones = document.setdefault("tombstones", {}).setdefault("attachments", [])
        if operation.entity_id not in tombstones:
            tombstones.append(operation.entity_id)
    elif operation.operation == ChangeOperationType.SUBMIT_QUOTE:
        document["status"] = QuoteStatus.SUBMITTED.value
        document["submitted_at"] = datetime.now(UTC)
    elif operation.operation == ChangeOperationType.WITHDRAW_QUOTE:
        document["status"] = QuoteStatus.WITHDRAWN.value
    else:
        raise ValueError(f"Unsupported operation {operation.operation}")
    document["provenance"].extend(item.model_dump(mode="python") for item in operation.provenance)


class QuoteVersioningService:
    def __init__(self, repository: QuoteVersionRepository) -> None:
        self._repository = repository

    def create(self, request: CreateQuoteRequest) -> SupplierQuote:
        snapshot = request.snapshot.model_copy(deep=True)
        replay = self._repository.get_idempotent_result(snapshot.quote_id, request.idempotency_key)
        if replay:
            return replay.quote
        quote = SupplierQuote(
            quote_id=snapshot.quote_id,
            rfq_id=snapshot.rfq_id,
            supplier_id=snapshot.supplier_id,
            current_revision=1,
            current=snapshot,
        )
        revision = SupplierQuoteRevision(
            quote_id=snapshot.quote_id,
            committed_revision=1,
            snapshot=snapshot,
        )
        self._repository.create(quote, revision)
        result = CommitResult(quote=quote, revision=revision)
        self._repository.save_idempotent_result(snapshot.quote_id, request.idempotency_key, result)
        return quote

    def get(self, quote_id: str) -> SupplierQuote:
        quote = self._repository.get(quote_id)
        if quote is None:
            raise QuoteNotFoundError(quote_id)
        return quote

    def list_quotes(self) -> list[SupplierQuote]:
        return self._repository.list_quotes()

    def revisions(self, quote_id: str) -> list[SupplierQuoteRevision]:
        self.get(quote_id)
        return self._repository.list_revisions(quote_id)

    def changes(self, quote_id: str) -> list[SupplierQuoteChange]:
        self.get(quote_id)
        return self._repository.list_changes(quote_id)

    def commit(self, quote_id: str, request: QuoteChangeRequest) -> CommitResult:
        replay = self._repository.get_idempotent_result(quote_id, request.idempotency_key)
        if replay:
            replay.idempotent_replay = True
            return replay

        current = self.get(quote_id)
        if request.expected_current_revision is not None:
            if request.expected_current_revision != current.current_revision:
                raise ConcurrentQuoteUpdateError(
                    f"Expected revision {request.expected_current_revision}, "
                    f"current revision is {current.current_revision}"
                )
        base_revision = self._repository.get_revision(quote_id, request.base_revision)
        if base_revision is None:
            conflict = MergeConflict(
                conflict_type=MergeConflictType.UNKNOWN_BASE_REVISION,
                path="/committedRevision",
                proposed_value=request.base_revision,
                message=f"Base revision {request.base_revision} does not exist.",
            )
            return CommitResult(quote=current, conflicts=[conflict])
        if current.current.status in {QuoteStatus.AWARDED, QuoteStatus.CLOSED}:
            conflict = MergeConflict(
                conflict_type=MergeConflictType.CLOSED_QUOTE,
                path="/status",
                current_value=current.current.status,
                message="Closed or awarded quotes require an amendment workflow.",
            )
            return CommitResult(quote=current, conflicts=[conflict])

        last_sequence = self._repository.last_supplier_sequence(quote_id)
        if request.supplier_sequence is not None:
            expected_sequence = 1 if last_sequence is None else last_sequence + 1
            if request.supplier_sequence != expected_sequence:
                conflict = MergeConflict(
                    conflict_type=MergeConflictType.SEQUENCE_GAP,
                    path="/supplierSequence",
                    current_value=last_sequence,
                    proposed_value=request.supplier_sequence,
                    message=f"Expected supplier sequence {expected_sequence}.",
                )
                return CommitResult(quote=current, conflicts=[conflict])

        base_document = base_revision.snapshot.model_dump(mode="python")
        current_document = current.current.model_dump(mode="python")
        accepted: list[QuoteChangeOperation] = []
        conflicts: list[MergeConflict] = []
        already_applied = False
        for operation in request.operations:
            operation_accepted: list[QuoteChangeOperation] = []
            operation_conflicts: list[MergeConflict] = []
            for atomic in _atomic_operations(operation):
                path = _logical_path(atomic)
                base_value = _operation_value(base_document, atomic)
                current_value = _operation_value(current_document, atomic)
                proposed_value = _proposed_value(atomic)
                if current_value == proposed_value:
                    already_applied = True
                    continue
                if current_value == base_value:
                    operation_accepted.append(atomic)
                    continue
                conflict_type = (
                    MergeConflictType.ORDERING_CONFLICT
                    if current_value == {"__tombstoned__": True}
                    else MergeConflictType.FIELD_CONFLICT
                )
                operation_conflicts.append(
                    MergeConflict(
                        conflict_type=conflict_type,
                        path=path,
                        base_value=base_value,
                        current_value=current_value,
                        proposed_value=proposed_value,
                        message="Current and proposed values both changed from the base revision.",
                    )
                )
            if operation.operation == ChangeOperationType.NO_BID_LINE and operation_conflicts:
                conflicts.extend(operation_conflicts)
                continue
            for atomic in operation_accepted:
                _apply_operation(current_document, atomic)
            accepted.extend(operation_accepted)
            conflicts.extend(operation_conflicts)

        if not accepted:
            result = CommitResult(
                quote=current,
                conflicts=conflicts,
                idempotent_replay=already_applied and not conflicts,
            )
            if result.idempotent_replay:
                self._repository.save_idempotent_result(quote_id, request.idempotency_key, result)
            return result

        try:
            snapshot = SupplierQuoteSnapshot.model_validate(current_document)
        except ValidationError as error:
            raise QuoteValidationError(str(error)) from error
        next_revision = current.current_revision + 1
        change = SupplierQuoteChange(
            change_id=request.change_id,
            quote_id=quote_id,
            based_on_revision=request.base_revision,
            parent_revision=current.current_revision,
            committed_revision=next_revision,
            idempotency_key=request.idempotency_key,
            actor=request.actor,
            reason=request.reason,
            submitted_at=request.submitted_at,
            received_at=request.received_at,
            effective_at=request.effective_at,
            supplier_sequence=request.supplier_sequence,
            operations=accepted,
        )
        revision = SupplierQuoteRevision(
            quote_id=quote_id,
            committed_revision=next_revision,
            parent_revision=current.current_revision,
            based_on_revision=request.base_revision,
            change_id=request.change_id,
            snapshot=snapshot,
        )
        updated = SupplierQuote(
            quote_id=quote_id,
            rfq_id=current.rfq_id,
            supplier_id=current.supplier_id,
            current_revision=next_revision,
            current=snapshot,
        )
        result = CommitResult(
            quote=updated,
            revision=revision,
            accepted_change=change,
            conflicts=conflicts,
        )
        if not self._repository.compare_and_swap(
            current.current_revision, updated, revision, change, result
        ):
            raise ConcurrentQuoteUpdateError(
                "Quote changed during commit; retry from current revision"
            )
        return result
