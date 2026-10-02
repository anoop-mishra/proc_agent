from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.models import Money, RangeValue


class QuoteResponseType(StrEnum):
    QUOTE = "QUOTE"
    NO_BID = "NO_BID"
    PARTIAL = "PARTIAL"
    ALTERNATIVE = "ALTERNATIVE"


class QuoteStatus(StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    WITHDRAWN = "WITHDRAWN"
    AWARDED = "AWARDED"
    CLOSED = "CLOSED"


class LineResponseStatus(StrEnum):
    QUOTED = "QUOTED"
    NO_BID = "NO_BID"
    ALTERNATIVE = "ALTERNATIVE"
    PARTIAL = "PARTIAL"


class Quantity(BaseModel):
    value: Decimal
    uom: str = Field(min_length=1)


class QuotePriceTier(BaseModel):
    tier_id: str = Field(min_length=1)
    minimum_quantity: Quantity
    maximum_quantity: Quantity | None = None
    unit_price: Money


class QuoteCharge(BaseModel):
    charge_id: str = Field(min_length=1)
    charge_type: str
    amount: Money | None = None
    rate: Decimal | None = None
    description: str | None = None


class DeliveryCommitment(BaseModel):
    delivery_date: datetime | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    lead_time: RangeValue | None = None
    origin: str | None = None


class QuoteLine(BaseModel):
    quote_line_id: str = Field(min_length=1)
    rfq_line_id: str = Field(min_length=1)
    response_status: LineResponseStatus
    no_bid_reason: str | None = None
    offered_item_id: str | None = None
    supplier_item_id: str | None = None
    description: str | None = None
    alternative_to_quote_line_id: str | None = None
    quantity: Quantity | None = None
    moq: Quantity | None = None
    order_multiple: Quantity | None = None
    unit_price: Money | None = None
    price_basis: Quantity | None = None
    price_tiers: list[QuotePriceTier] = Field(default_factory=list)
    discounts: list[QuoteCharge] = Field(default_factory=list)
    charges: list[QuoteCharge] = Field(default_factory=list)
    taxes: list[QuoteCharge] = Field(default_factory=list)
    line_total: Money | None = None
    delivery: DeliveryCommitment | None = None
    warranty: str | None = None
    notes: list[str] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_response(self) -> QuoteLine:
        if self.response_status == LineResponseStatus.NO_BID and not self.no_bid_reason:
            raise ValueError("NO_BID lines require no_bid_reason")
        if self.response_status == LineResponseStatus.NO_BID:
            if self.unit_price is not None or self.price_tiers or self.line_total is not None:
                raise ValueError("NO_BID lines cannot retain quoted prices or totals")
        if self.response_status in {LineResponseStatus.QUOTED, LineResponseStatus.ALTERNATIVE}:
            if self.unit_price is None and not self.price_tiers:
                raise ValueError("Quoted and alternative lines require a price or price tier")
        return self


class CommercialTerms(BaseModel):
    payment_terms: str | None = None
    delivery_terms: str | None = None
    freight_terms: str | None = None
    incoterm: str | None = None
    incoterm_location: str | None = None
    contract_period: str | None = None
    renewal_terms: str | None = None
    cancellation_terms: str | None = None
    supplier_terms: str | None = None
    exceptions: list[str] = Field(default_factory=list)


class QuoteTotals(BaseModel):
    line_extension: Money | None = None
    allowances: Money | None = None
    charges: Money | None = None
    tax_exclusive: Money | None = None
    tax: Money | None = None
    tax_inclusive: Money | None = None
    payable: Money | None = None


class QuoteAttachment(BaseModel):
    attachment_id: str = Field(min_length=1)
    artifact_id: str | None = None
    filename: str
    media_type: str | None = None
    checksum: str | None = None


class DeclarationAnswer(BaseModel):
    answer_id: str = Field(min_length=1)
    question_id: str = Field(min_length=1)
    value: Any


class FieldProvenance(BaseModel):
    field_path: str
    communication_id: str | None = None
    artifact_id: str | None = None
    location: str | None = None
    source_text: str | None = None
    decision_actor: str | None = None
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SupplierQuoteSnapshot(BaseModel):
    quote_id: str = Field(min_length=1)
    rfq_id: str = Field(min_length=1)
    supplier_id: str = Field(min_length=1)
    supplier_name: str | None = None
    drawing_revision: str | None = None
    response_type: QuoteResponseType = QuoteResponseType.QUOTE
    status: QuoteStatus = QuoteStatus.DRAFT
    issue_at: datetime | None = None
    submitted_at: datetime | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    currency: str | None = None
    language: str | None = None
    supplier_reference: str | None = None
    buyer_reference: str | None = None
    supplier_contact: str | None = None
    buyer_contact: str | None = None
    notes: list[str] = Field(default_factory=list)
    required_rfq_line_ids: list[str] = Field(default_factory=list)
    lines: list[QuoteLine] = Field(default_factory=list)
    commercial_terms: CommercialTerms = Field(default_factory=CommercialTerms)
    totals: QuoteTotals = Field(default_factory=QuoteTotals)
    attachments: list[QuoteAttachment] = Field(default_factory=list)
    declarations: list[DeclarationAnswer] = Field(default_factory=list)
    source_communication_ids: list[str] = Field(default_factory=list)
    provenance: list[FieldProvenance] = Field(default_factory=list)
    tombstones: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_snapshot(self) -> SupplierQuoteSnapshot:
        line_ids = [line.quote_line_id for line in self.lines]
        if len(line_ids) != len(set(line_ids)):
            raise ValueError("quote_line_id values must be unique")
        tier_ids = [tier.tier_id for line in self.lines for tier in line.price_tiers]
        if len(tier_ids) != len(set(tier_ids)):
            raise ValueError("tier_id values must be unique across the quote")
        attachment_ids = [item.attachment_id for item in self.attachments]
        if len(attachment_ids) != len(set(attachment_ids)):
            raise ValueError("attachment_id values must be unique")
        if self.status == QuoteStatus.SUBMITTED:
            responded = {line.rfq_line_id for line in self.lines}
            missing = set(self.required_rfq_line_ids) - responded
            if missing:
                raise ValueError(
                    "Submitted quote requires an explicit response for RFQ lines: "
                    + ", ".join(sorted(missing))
                )
            line_totals = [line.line_total for line in self.lines]
            if self.totals.line_extension and all(line_totals):
                currencies = {item.currency for item in line_totals if item}
                currencies.add(self.totals.line_extension.currency)
                if len(currencies) != 1:
                    raise ValueError("Line totals and quote line extension must use one currency")
                expected_extension = sum(
                    (item.amount for item in line_totals if item), start=Decimal("0")
                )
                if expected_extension != self.totals.line_extension.amount:
                    raise ValueError("Quote line extension does not reconcile with line totals")
            if self.totals.tax_exclusive and self.totals.tax and self.totals.tax_inclusive:
                amounts = (
                    self.totals.tax_exclusive,
                    self.totals.tax,
                    self.totals.tax_inclusive,
                )
                if len({item.currency for item in amounts}) != 1:
                    raise ValueError("Tax totals must use one currency")
                if amounts[0].amount + amounts[1].amount != amounts[2].amount:
                    raise ValueError("Tax-inclusive total does not reconcile")
            if self.totals.payable and self.totals.tax_inclusive:
                charges = self.totals.charges
                allowances = self.totals.allowances
                money_values = [
                    item
                    for item in (
                        self.totals.payable,
                        self.totals.tax_inclusive,
                        charges,
                        allowances,
                    )
                    if item
                ]
                if len({item.currency for item in money_values}) != 1:
                    raise ValueError("Payable total components must use one currency")
                expected_payable = self.totals.tax_inclusive.amount
                expected_payable += charges.amount if charges else Decimal("0")
                expected_payable -= allowances.amount if allowances else Decimal("0")
                if expected_payable != self.totals.payable.amount:
                    raise ValueError("Payable total does not reconcile")
        return self


class ChangeOperationType(StrEnum):
    SET_FIELD = "SET_FIELD"
    UPDATE_LINE = "UPDATE_LINE"
    NO_BID_LINE = "NO_BID_LINE"
    ADD_PRICE_TIER = "ADD_PRICE_TIER"
    REMOVE_ATTACHMENT = "REMOVE_ATTACHMENT"
    SUBMIT_QUOTE = "SUBMIT_QUOTE"
    WITHDRAW_QUOTE = "WITHDRAW_QUOTE"


class QuoteChangeOperation(BaseModel):
    operation: ChangeOperationType
    path: str | None = None
    entity_id: str | None = None
    value: Any = None
    changes: dict[str, Any] = Field(default_factory=dict)
    provenance: list[FieldProvenance] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_operation(self) -> QuoteChangeOperation:
        if self.operation == ChangeOperationType.SET_FIELD and not self.path:
            raise ValueError("SET_FIELD requires path")
        if (
            self.operation
            in {
                ChangeOperationType.UPDATE_LINE,
                ChangeOperationType.NO_BID_LINE,
                ChangeOperationType.ADD_PRICE_TIER,
                ChangeOperationType.REMOVE_ATTACHMENT,
            }
            and not self.entity_id
        ):
            raise ValueError(f"{self.operation} requires entity_id")
        if self.operation == ChangeOperationType.UPDATE_LINE and not self.changes:
            raise ValueError("UPDATE_LINE requires changes")
        return self


class QuoteChangeRequest(BaseModel):
    change_id: str = Field(min_length=1)
    base_revision: int = Field(ge=1)
    expected_current_revision: int | None = Field(default=None, ge=1)
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    reason: str | None = None
    submitted_at: datetime | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    effective_at: datetime | None = None
    supplier_sequence: int | None = Field(default=None, ge=1)
    operations: list[QuoteChangeOperation] = Field(min_length=1)


class SupplierQuoteChange(BaseModel):
    change_id: str
    quote_id: str
    based_on_revision: int
    parent_revision: int
    committed_revision: int
    idempotency_key: str
    actor: str
    reason: str | None = None
    submitted_at: datetime | None = None
    received_at: datetime
    effective_at: datetime | None = None
    committed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    supplier_sequence: int | None = None
    operations: list[QuoteChangeOperation]


class SupplierQuoteRevision(BaseModel):
    quote_id: str
    committed_revision: int
    parent_revision: int | None = None
    based_on_revision: int | None = None
    change_id: str | None = None
    committed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    snapshot: SupplierQuoteSnapshot


class SupplierQuote(BaseModel):
    quote_id: str
    rfq_id: str
    supplier_id: str
    current_revision: int
    current: SupplierQuoteSnapshot


class MergeConflictType(StrEnum):
    FIELD_CONFLICT = "FIELD_CONFLICT"
    ORDERING_CONFLICT = "ORDERING_CONFLICT"
    UNKNOWN_BASE_REVISION = "UNKNOWN_BASE_REVISION"
    CLOSED_QUOTE = "CLOSED_QUOTE"
    SEQUENCE_GAP = "SEQUENCE_GAP"


class MergeConflict(BaseModel):
    conflict_type: MergeConflictType
    path: str
    base_value: Any = None
    current_value: Any = None
    proposed_value: Any = None
    message: str


class CommitResult(BaseModel):
    quote: SupplierQuote
    revision: SupplierQuoteRevision | None = None
    accepted_change: SupplierQuoteChange | None = None
    conflicts: list[MergeConflict] = Field(default_factory=list)
    idempotent_replay: bool = False


class CreateQuoteRequest(BaseModel):
    snapshot: SupplierQuoteSnapshot
    actor: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
