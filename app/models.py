from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class Money(BaseModel):
    amount: Decimal
    currency: str = Field(min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class RangeValue(BaseModel):
    minimum: Decimal
    maximum: Decimal
    unit: str


class PriceTier(BaseModel):
    minimum_quantity: int
    maximum_quantity: int | None = None
    unit_price: Money


class Evidence(BaseModel):
    field: str
    excerpt: str


class QuoteTerms(BaseModel):
    """Canonical, provider-independent representation of quoted facts."""

    supplier_name: str
    part_number: str | None = None
    drawing_revision: str | None = None
    price_tiers: list[PriceTier] = Field(default_factory=list)
    moq: int | None = None
    minimum_release_quantity: int | None = None
    incoterm: str | None = None
    incoterm_location: str | None = None
    tooling_cost: Money | None = None
    tooling_notes: str | None = None
    deposit: Money | None = None
    deposit_refundable: bool | None = None
    production_lead_time: RangeValue | None = None
    transit_lead_time: RangeValue | None = None
    payment_terms: str | None = None
    quote_validity_days: int | None = None
    price_adjustment: str | None = None
    freight_included: bool | None = None
    duty_included: bool | None = None
    packaging: str | None = None
    inventory_commitment: str | None = None
    finishing: str | None = None
    notes: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)

    def evidence_for(self, field: str) -> str | None:
        return next((item.excerpt for item in self.evidence if item.field == field), None)


class RawQuote(BaseModel):
    source_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class RFQContext(BaseModel):
    part_number: str
    annual_volume: int = Field(gt=0)
    drawing_revision: str | None = None
    destination: str | None = None


class AnalysisAssumptions(BaseModel):
    target_currency: str | None = None
    fx_rates: dict[str, Decimal] = Field(default_factory=dict)
    freight_by_source: dict[str, Money] = Field(default_factory=dict)
    duty_rate_by_source: dict[str, Decimal] = Field(default_factory=dict)

    @field_validator("target_currency")
    @classmethod
    def normalize_target_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else None


class AnalysisRequest(BaseModel):
    rfq: RFQContext
    quotes: list[RawQuote] = Field(min_length=1)
    assumptions: AnalysisAssumptions = Field(default_factory=AnalysisAssumptions)

    @field_validator("quotes")
    @classmethod
    def unique_source_ids(cls, quotes: list[RawQuote]) -> list[RawQuote]:
        source_ids = [quote.source_id for quote in quotes]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("quote source_id values must be unique")
        return quotes


class SelectedPrice(BaseModel):
    unit_price: Money
    annual_material_cost: Money
    tier_minimum_quantity: int
    estimated_landed_cost: Money | None = None
    applied_assumptions: list[str] = Field(default_factory=list)


class Issue(BaseModel):
    severity: Severity
    category: str
    message: str
    evidence: str | None = None


class SupplierInsight(BaseModel):
    source_id: str
    supplier_name: str
    quote_id: str | None = None
    current_revision: int | None = None
    selected_price: SelectedPrice | None = None
    one_time_costs: list[Money] = Field(default_factory=list)
    refundable_deposits: list[Money] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)


class AnalysisResponse(BaseModel):
    analysis_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    rfq: RFQContext
    assumptions: AnalysisAssumptions
    normalized_quotes: dict[str, QuoteTerms]
    supplier_insights: list[SupplierInsight]
    cross_supplier_insights: list[str]
    quote_revisions: dict[str, int] = Field(default_factory=dict)
    quote_conflicts: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    disclaimer: str = (
        "Decision support only: unknown terms are not estimated and no award "
        "recommendation is made."
    )


class SamplePayload(BaseModel):
    request: AnalysisRequest
    description: str


class ErrorDetail(BaseModel):
    error: str
    context: dict[str, Any] = Field(default_factory=dict)
