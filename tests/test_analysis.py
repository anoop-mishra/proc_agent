from decimal import Decimal

from app.analysis import analyze_supplier, cross_supplier_insights
from app.models import AnalysisAssumptions, Money, RFQContext, Severity
from app.sample import SAMPLE_TERMS

RFQ = RFQContext(
    part_number="BRK-4471-A",
    annual_volume=60_000,
    destination="Columbus, Ohio",
)


def test_selects_volume_tier_and_computes_annual_material_cost() -> None:
    insight = analyze_supplier(
        "nordkant",
        SAMPLE_TERMS["nordkant"],
        RFQ,
        AnalysisAssumptions(),
    )

    assert insight.selected_price is not None
    assert insight.selected_price.unit_price.amount == Decimal("3.92")
    assert insight.selected_price.annual_material_cost.amount == Decimal("235200.00")
    assert insight.selected_price.annual_material_cost.currency == "EUR"
    assert insight.one_time_costs == []
    assert insight.refundable_deposits == [Money(amount=Decimal("2800"), currency="EUR")]
    assert not any(issue.category == "tooling" for issue in insight.issues)
    assert not any("tooling" in question.lower() for question in insight.clarification_questions)


def test_applies_only_explicit_cost_assumptions() -> None:
    assumptions = AnalysisAssumptions(
        target_currency="USD",
        fx_rates={"EUR_USD": Decimal("1.10")},
        freight_by_source={"nordkant": Money(amount=Decimal("1000"), currency="USD")},
        duty_rate_by_source={"nordkant": Decimal("0.05")},
    )

    insight = analyze_supplier("nordkant", SAMPLE_TERMS["nordkant"], RFQ, assumptions)

    assert insight.selected_price is not None
    assert insight.selected_price.estimated_landed_cost is not None
    assert insight.selected_price.estimated_landed_cost.amount == Decimal("272706.0000")
    assert insight.selected_price.estimated_landed_cost.currency == "USD"


def test_flags_landed_cost_and_working_capital_gaps() -> None:
    insight = analyze_supplier(
        "pacific-rim",
        SAMPLE_TERMS["pacific-rim"],
        RFQ,
        AnalysisAssumptions(),
    )

    categories = {issue.category for issue in insight.issues}
    assert {"landed_cost", "working_capital"}.issubset(categories)
    assert any("freight" in question.lower() for question in insight.clarification_questions)
    assert any("duty" in question.lower() for question in insight.clarification_questions)


def test_rfQ_revision_mismatch_is_critical() -> None:
    rfq = RFQ.model_copy(update={"drawing_revision": "C"})

    insight = analyze_supplier(
        "pacific-rim",
        SAMPLE_TERMS["pacific-rim"],
        rfq,
        AnalysisAssumptions(),
    )

    assert any(
        issue.severity == Severity.CRITICAL and issue.category == "specification"
        for issue in insight.issues
    )


def test_cross_supplier_insights_do_not_rank_or_recommend() -> None:
    insights = [
        analyze_supplier(source_id, terms, RFQ, AnalysisAssumptions())
        for source_id, terms in SAMPLE_TERMS.items()
    ]

    observations = cross_supplier_insights(SAMPLE_TERMS, insights)

    assert any("conflicting drawing revisions" in item for item in observations)
    assert any("USD, EUR" in item or "EUR, USD" in item for item in observations)
    assert all(
        "recommend" not in item.lower() and "winner" not in item.lower() for item in observations
    )
