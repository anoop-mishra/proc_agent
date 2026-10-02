import re
from uuid import uuid4

from app.analysis import analyze_supplier, cross_supplier_insights
from app.ingestion import CommunicationRepository
from app.models import AnalysisRequest, AnalysisResponse, PriceTier, QuoteTerms
from app.providers.base import ExtractionProvider
from app.quote_models import (
    CommercialTerms,
    CreateQuoteRequest,
    DeliveryCommitment,
    LineResponseStatus,
    Quantity,
    QuoteCharge,
    QuoteLine,
    QuotePriceTier,
    SupplierQuoteSnapshot,
)
from app.repository import AnalysisRepository
from app.versioning import QuoteNotFoundError, QuoteVersioningService


def quote_id_for(part_number: str, source_id: str) -> str:
    raw = f"{part_number}--{source_id}".lower()
    return re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-")


def _snapshot_from_terms(
    source_id: str,
    terms: QuoteTerms,
    request: AnalysisRequest,
) -> SupplierQuoteSnapshot:
    rfq_line_id = request.rfq.part_number
    quote_line_id = f"{source_id}-line-1"
    price_tiers = [
        QuotePriceTier(
            tier_id=f"{source_id}-tier-{index + 1}",
            minimum_quantity=Quantity(value=tier.minimum_quantity, uom="EA"),
            maximum_quantity=Quantity(value=tier.maximum_quantity, uom="EA")
            if tier.maximum_quantity is not None
            else None,
            unit_price=tier.unit_price,
        )
        for index, tier in enumerate(terms.price_tiers)
    ]
    selected_tier = next(
        (
            tier
            for tier in terms.price_tiers
            if tier.minimum_quantity <= request.rfq.annual_volume
            and (
                tier.maximum_quantity is None or request.rfq.annual_volume <= tier.maximum_quantity
            )
        ),
        None,
    )
    charges = []
    if terms.tooling_cost:
        charges.append(
            QuoteCharge(
                charge_id=f"{source_id}-tooling",
                charge_type="TOOLING",
                amount=terms.tooling_cost,
                description=terms.tooling_notes,
            )
        )
    notes = list(terms.notes)
    for item in (terms.inventory_commitment, terms.finishing, terms.price_adjustment):
        if item:
            notes.append(item)
    line = QuoteLine(
        quote_line_id=quote_line_id,
        rfq_line_id=rfq_line_id,
        response_status=LineResponseStatus.QUOTED,
        description=terms.part_number,
        quantity=Quantity(value=request.rfq.annual_volume, uom="EA"),
        moq=Quantity(value=terms.moq, uom="EA") if terms.moq else None,
        order_multiple=Quantity(value=terms.minimum_release_quantity, uom="EA")
        if terms.minimum_release_quantity
        else None,
        unit_price=selected_tier.unit_price if selected_tier else None,
        price_tiers=price_tiers,
        charges=charges,
        delivery=DeliveryCommitment(lead_time=terms.production_lead_time)
        if terms.production_lead_time
        else None,
        notes=notes,
    )
    return SupplierQuoteSnapshot(
        quote_id=quote_id_for(request.rfq.part_number, source_id),
        rfq_id=request.rfq.part_number,
        supplier_id=source_id,
        supplier_name=terms.supplier_name,
        drawing_revision=terms.drawing_revision,
        currency=selected_tier.unit_price.currency if selected_tier else None,
        required_rfq_line_ids=[rfq_line_id],
        lines=[line],
        commercial_terms=CommercialTerms(
            payment_terms=terms.payment_terms,
            delivery_terms=(
                f"{terms.incoterm or ''} {terms.incoterm_location or ''}".strip() or None
            ),
            freight_terms="Included"
            if terms.freight_included is True
            else "Excluded"
            if terms.freight_included is False
            else None,
            incoterm=terms.incoterm,
            incoterm_location=terms.incoterm_location,
        ),
    )


def _overlay_snapshot(terms: QuoteTerms, snapshot: SupplierQuoteSnapshot) -> QuoteTerms:
    overlaid = terms.model_copy(deep=True)
    overlaid.supplier_name = snapshot.supplier_name or overlaid.supplier_name
    overlaid.drawing_revision = snapshot.drawing_revision or overlaid.drawing_revision
    overlaid.payment_terms = snapshot.commercial_terms.payment_terms
    overlaid.incoterm = snapshot.commercial_terms.incoterm
    overlaid.incoterm_location = snapshot.commercial_terms.incoterm_location
    if snapshot.lines:
        line = snapshot.lines[0]
        if line.price_tiers:
            overlaid.price_tiers = [
                PriceTier(
                    minimum_quantity=int(tier.minimum_quantity.value),
                    maximum_quantity=int(tier.maximum_quantity.value)
                    if tier.maximum_quantity
                    else None,
                    unit_price=tier.unit_price,
                )
                for tier in line.price_tiers
            ]
        elif line.unit_price:
            overlaid.price_tiers = [PriceTier(minimum_quantity=0, unit_price=line.unit_price)]
        if line.delivery and line.delivery.lead_time:
            overlaid.production_lead_time = line.delivery.lead_time
        overlaid.notes = list(line.notes)
    return overlaid


class AnalysisService:
    def __init__(
        self,
        provider: ExtractionProvider,
        repository: AnalysisRepository,
        quote_versioning: QuoteVersioningService | None = None,
        communications: CommunicationRepository | None = None,
    ) -> None:
        self._provider = provider
        self._repository = repository
        self._quote_versioning = quote_versioning
        self._communications = communications

    def analyze(self, request: AnalysisRequest) -> AnalysisResponse:
        normalized: dict[str, QuoteTerms] = {}
        quote_revisions: dict[str, int] = {}
        canonical_quotes = {}
        for quote in request.quotes:
            terms = self._provider.extract(quote, request.rfq)
            if self._quote_versioning:
                quote_id = quote_id_for(request.rfq.part_number, quote.source_id)
                try:
                    canonical = self._quote_versioning.get(quote_id)
                except QuoteNotFoundError:
                    snapshot = _snapshot_from_terms(quote.source_id, terms, request)
                    canonical = self._quote_versioning.create(
                        CreateQuoteRequest(
                            snapshot=snapshot,
                            actor="analysis-service",
                            idempotency_key=f"initial-analysis:{quote.source_id}",
                        )
                    )
                terms = _overlay_snapshot(terms, canonical.current)
                canonical_quotes[quote.source_id] = canonical
                quote_revisions[quote.source_id] = canonical.current_revision
            normalized[quote.source_id] = terms

        supplier_insights = [
            analyze_supplier(source_id, terms, request.rfq, request.assumptions)
            for source_id, terms in normalized.items()
        ]
        for insight in supplier_insights:
            canonical = canonical_quotes.get(insight.source_id)
            if canonical:
                insight.quote_id = canonical.quote_id
                insight.current_revision = canonical.current_revision
        response = AnalysisResponse(
            analysis_id=str(uuid4()),
            rfq=request.rfq,
            assumptions=request.assumptions,
            normalized_quotes=normalized,
            supplier_insights=supplier_insights,
            cross_supplier_insights=cross_supplier_insights(normalized, supplier_insights),
            quote_revisions=quote_revisions,
            quote_conflicts={
                source_id: [
                    flag.model_dump(mode="json")
                    for flag in self._communications.conflict_flags_for_quote(canonical.quote_id)
                ]
                for source_id, canonical in canonical_quotes.items()
                if self._communications
            },
        )
        self._repository.save(response)
        return response

    def get(self, analysis_id: str) -> AnalysisResponse | None:
        return self._repository.get(analysis_id)
