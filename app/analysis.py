from app.models import (
    AnalysisAssumptions,
    Issue,
    Money,
    QuoteTerms,
    RFQContext,
    SelectedPrice,
    Severity,
    SupplierInsight,
)

MISSING_LABELS = {
    "drawing_revision": "drawing revision",
    "incoterm": "Incoterm",
    "production_lead_time": "production lead time",
    "payment_terms": "payment terms",
    "quote_validity_days": "quote validity",
}


def _tooling_is_unresolved(terms: QuoteTerms) -> bool:
    if terms.tooling_cost is not None or not terms.tooling_notes:
        return False
    notes = terms.tooling_notes.lower()
    return any(word in notes for word in ("tbd", "confirm", "review", "unknown"))


def _select_price(terms: QuoteTerms, annual_volume: int) -> SelectedPrice | None:
    eligible = [
        tier
        for tier in terms.price_tiers
        if tier.minimum_quantity <= annual_volume
        and (tier.maximum_quantity is None or annual_volume <= tier.maximum_quantity)
    ]
    if not eligible:
        return None
    tier = max(eligible, key=lambda item: item.minimum_quantity)
    annual_cost = tier.unit_price.amount * annual_volume
    return SelectedPrice(
        unit_price=tier.unit_price,
        annual_material_cost=Money(amount=annual_cost, currency=tier.unit_price.currency),
        tier_minimum_quantity=tier.minimum_quantity,
    )


def _convert(
    money: Money,
    target_currency: str,
    assumptions: AnalysisAssumptions,
) -> tuple[Money, str] | None:
    if money.currency == target_currency:
        return money, f"{money.currency} already equals target currency"
    key = f"{money.currency}_{target_currency}"
    rate = assumptions.fx_rates.get(key)
    if rate is None:
        return None
    return (
        Money(amount=money.amount * rate, currency=target_currency),
        f"FX {key}={rate}",
    )


def _apply_cost_assumptions(
    source_id: str,
    selected: SelectedPrice,
    assumptions: AnalysisAssumptions,
) -> SelectedPrice:
    target = assumptions.target_currency
    if not target:
        return selected
    converted = _convert(selected.annual_material_cost, target, assumptions)
    if converted is None:
        return selected
    landed, fx_note = converted
    notes = [fx_note]

    freight = assumptions.freight_by_source.get(source_id)
    if freight:
        converted_freight = _convert(freight, target, assumptions)
        if converted_freight:
            freight_money, freight_note = converted_freight
            landed = Money(amount=landed.amount + freight_money.amount, currency=target)
            notes.extend([f"annual freight={freight_money.amount} {target}", freight_note])

    duty_rate = assumptions.duty_rate_by_source.get(source_id)
    if duty_rate is not None:
        duty = landed.amount * duty_rate
        landed = Money(amount=landed.amount + duty, currency=target)
        notes.append(f"duty={duty_rate * 100}% applied after assumed freight")

    selected.estimated_landed_cost = landed
    selected.applied_assumptions = notes
    return selected


def _base_issues(terms: QuoteTerms, rfq: RFQContext) -> list[Issue]:
    issues: list[Issue] = []
    if terms.part_number and terms.part_number.upper() != rfq.part_number.upper():
        issues.append(
            Issue(
                severity=Severity.CRITICAL,
                category="specification",
                message=(
                    f"Quoted part {terms.part_number} does not match RFQ part {rfq.part_number}."
                ),
                evidence=terms.evidence_for("part_number"),
            )
        )
    if rfq.drawing_revision and terms.drawing_revision != rfq.drawing_revision:
        issues.append(
            Issue(
                severity=Severity.CRITICAL,
                category="specification",
                message=(
                    f"Quote revision {terms.drawing_revision or 'not stated'} does not match "
                    f"RFQ revision {rfq.drawing_revision}."
                ),
                evidence=terms.evidence_for("drawing_revision"),
            )
        )
    if terms.freight_included is False or terms.duty_included is False:
        excluded = []
        if terms.freight_included is False:
            excluded.append("freight")
        if terms.duty_included is False:
            excluded.append("duty")
        issues.append(
            Issue(
                severity=Severity.WARNING,
                category="landed_cost",
                message=f"Headline price excludes {' and '.join(excluded)}.",
                evidence=terms.evidence_for("freight_included"),
            )
        )
    elif terms.incoterm in {"EXW", "FCA", "FOB"}:
        issues.append(
            Issue(
                severity=Severity.WARNING,
                category="landed_cost",
                message=f"{terms.incoterm} price is not a delivered landed cost.",
                evidence=terms.evidence_for("incoterm"),
            )
        )
    if _tooling_is_unresolved(terms):
        issues.append(
            Issue(
                severity=Severity.WARNING,
                category="tooling",
                message=f"Tooling cost is unresolved: {terms.tooling_notes}.",
                evidence=terms.evidence_for("tooling_notes"),
            )
        )
    if terms.price_adjustment:
        issues.append(
            Issue(
                severity=Severity.WARNING,
                category="price_exposure",
                message=f"Price is adjustable: {terms.price_adjustment}.",
                evidence=terms.evidence_for("price_adjustment"),
            )
        )
    if terms.deposit and terms.deposit_refundable is True:
        issues.append(
            Issue(
                severity=Severity.INFO,
                category="working_capital",
                message=(
                    f"Refundable deposit requires {terms.deposit.currency} "
                    f"{terms.deposit.amount:,.2f} cash outlay."
                ),
                evidence=terms.evidence_for("deposit"),
            )
        )
    if terms.payment_terms and any(
        phrase in terms.payment_terms.lower() for phrase in ("deposit", "before shipment", "prepay")
    ):
        issues.append(
            Issue(
                severity=Severity.WARNING,
                category="working_capital",
                message=f"Payment requires cash before delivery: {terms.payment_terms}.",
                evidence=terms.evidence_for("payment_terms"),
            )
        )
    if terms.minimum_release_quantity:
        issues.append(
            Issue(
                severity=Severity.INFO,
                category="ordering_constraint",
                message=(
                    f"Price requires releases of at least {terms.minimum_release_quantity:,} units."
                ),
                evidence=terms.evidence_for("minimum_release_quantity"),
            )
        )
    if terms.inventory_commitment:
        issues.append(
            Issue(
                severity=Severity.INFO,
                category="supply_continuity",
                message=terms.inventory_commitment,
                evidence=terms.evidence_for("inventory_commitment"),
            )
        )
    return issues


def _clarification_questions(
    terms: QuoteTerms,
    rfq: RFQContext,
    selected: SelectedPrice | None,
) -> list[str]:
    questions: list[str] = []
    if terms.drawing_revision is None:
        questions.append("Which drawing revision is this quote based on?")
    elif rfq.drawing_revision and terms.drawing_revision != rfq.drawing_revision:
        questions.append(f"Please requote against drawing revision {rfq.drawing_revision}.")
    if selected is None:
        questions.append(f"What unit price applies at {rfq.annual_volume:,} units annually?")
    if terms.incoterm is None:
        questions.append("What Incoterm and named delivery location apply?")
    if terms.incoterm in {"EXW", "FCA", "FOB"} or terms.freight_included is False:
        questions.append(f"Please provide freight to {rfq.destination or 'our destination'}.")
    if terms.duty_included is False:
        questions.append("What tariff classification and duty rate should we use?")
    if _tooling_is_unresolved(terms):
        questions.append("Please confirm the tooling cost and payment timing.")
    if terms.quote_validity_days is None:
        questions.append("How long is the quote valid?")
    return questions


def analyze_supplier(
    source_id: str,
    terms: QuoteTerms,
    rfq: RFQContext,
    assumptions: AnalysisAssumptions,
) -> SupplierInsight:
    selected = _select_price(terms, rfq.annual_volume)
    issues = _base_issues(terms, rfq)
    if selected is None:
        issues.append(
            Issue(
                severity=Severity.CRITICAL,
                category="price",
                message=f"No quoted price tier covers {rfq.annual_volume:,} annual units.",
                evidence=terms.evidence_for("price_tiers"),
            )
        )
    else:
        selected = _apply_cost_assumptions(source_id, selected, assumptions)

    missing = [label for field, label in MISSING_LABELS.items() if getattr(terms, field) is None]
    one_time = [terms.tooling_cost] if terms.tooling_cost else []
    if terms.deposit and terms.deposit_refundable is not True:
        one_time.append(terms.deposit)
    refundable_deposits = (
        [terms.deposit] if terms.deposit and terms.deposit_refundable is True else []
    )
    return SupplierInsight(
        source_id=source_id,
        supplier_name=terms.supplier_name,
        selected_price=selected,
        one_time_costs=one_time,
        refundable_deposits=refundable_deposits,
        issues=issues,
        missing_fields=missing,
        clarification_questions=_clarification_questions(terms, rfq, selected),
    )


def cross_supplier_insights(
    terms_by_source: dict[str, QuoteTerms],
    insights: list[SupplierInsight],
) -> list[str]:
    observations: list[str] = []
    revisions = sorted(
        {terms.drawing_revision for terms in terms_by_source.values() if terms.drawing_revision}
    )
    if len(revisions) > 1:
        observations.append(
            f"Quotes reference conflicting drawing revisions ({', '.join(revisions)}); "
            "prices are not specification-equivalent."
        )

    currencies = sorted(
        {
            insight.selected_price.unit_price.currency
            for insight in insights
            if insight.selected_price is not None
        }
    )
    if len(currencies) > 1:
        observations.append(
            f"Quotes span {', '.join(currencies)}; do not compare totals without an "
            "explicit FX assumption."
        )

    incoterms = sorted({terms.incoterm for terms in terms_by_source.values() if terms.incoterm})
    if len(incoterms) > 1:
        observations.append(
            f"Delivery terms vary ({', '.join(incoterms)}), so headline unit prices "
            "carry different logistics scope."
        )

    for currency in currencies:
        priced = [
            insight
            for insight in insights
            if insight.selected_price and insight.selected_price.unit_price.currency == currency
        ]
        if len(priced) < 2:
            continue
        low = min(priced, key=lambda item: item.selected_price.unit_price.amount)  # type: ignore[union-attr]
        high = max(priced, key=lambda item: item.selected_price.unit_price.amount)  # type: ignore[union-attr]
        observations.append(
            f"Within {currency}, quoted unit prices range from "
            f"{low.selected_price.unit_price.amount} ({low.supplier_name}) to "
            f"{high.selected_price.unit_price.amount} ({high.supplier_name}); delivery scope "
            "and gaps still differ."
        )
    return observations
