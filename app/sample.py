from decimal import Decimal

from app.models import (
    AnalysisRequest,
    Evidence,
    Money,
    PriceTier,
    QuoteTerms,
    RangeValue,
    RawQuote,
    RFQContext,
    SamplePayload,
)

TRI_STATE_TEXT = """From: dmorrow@tristatemetalcraft.com
Subject: RE: RFQ BRK-4471-A

Hi Karen,

$4.85 per piece, FOB our dock in Cleveland. That's at your 60k annual
number -- we'd hold that price for releases of 5,000 or more, below that
we'd have to look at it again.

Tooling is $6,200, one time, invoiced on PO.

Lead time 4 weeks from release, and we can usually beat that. Net 30.
Pricing firm for 90 days. Anodizing is done in house.

Dan
"""

NORDKANT_TEXT = """NORDKANT PRAZISIONSTEILE GMBH
Quotation No. NK-2026-1184

Part: BRK-4471-A (per customer drawing rev C)

Price schedule, annual volume:
  20,000 pcs EUR 4.30 / pc
  60,000 pcs EUR 3.92 / pc
 100,000 pcs EUR 3.71 / pc

Delivery: DDP Columbus, Ohio
Lead time: 11 weeks from order release
Payment: Net 60 days. 2% discount for payment within 10 days.
Tooling: included, amortized over first 40,000 pcs
Packaging: returnable steel containers, deposit EUR 2,800 (refundable)

Validity: 30 days from date of issue.
Prices subject to quarterly adjustment per LME aluminium index,
base 2026 Q1. Adjustments applied where index moves more than 4%.
"""

PACIFIC_RIM_TEXT = """PART NO         BRK-4471-A
UNIT PRICE      USD 3,240.00 / 1,000 PCS
MOQ             25,000 PCS
TERMS           FOB KAOHSIUNG
LEAD TIME       9 WEEKS PRODUCTION
SHIPPING        5 WEEKS OCEAN
TOOLING         USD 4,800.00
PAYMENT         30% T/T DEPOSIT, 70% BEFORE SHIPMENT
NOTES           PRICE BASED ON DRAWING REV B
                ANODIZING SUBCONTRACTED
                FREIGHT AND DUTY NOT INCLUDED
"""

BRIGHTWATER_TEXT = """From: l.vega@brightwaterprecision.mx
Subject: BRK-4471-A

Karen,

We can do this part. Looking at around $4.40 a piece at your volumes,
EXW Queretaro. Lead time 6-8 weeks once we're running.

Tooling we'd need to confirm after our engineering team reviews the
drawing -- I don't want to quote you a number and then change it.

We'd carry four weeks of safety stock for you at our facility at no charge.

Luis
"""


SAMPLE_QUOTES = [
    RawQuote(source_id="tri-state", text=TRI_STATE_TEXT),
    RawQuote(source_id="nordkant", text=NORDKANT_TEXT),
    RawQuote(source_id="pacific-rim", text=PACIFIC_RIM_TEXT),
    RawQuote(source_id="brightwater", text=BRIGHTWATER_TEXT),
]


SAMPLE_TERMS = {
    "tri-state": QuoteTerms(
        supplier_name="Tri-State Metalcraft",
        part_number="BRK-4471-A",
        price_tiers=[
            PriceTier(
                minimum_quantity=60_000,
                unit_price=Money(amount=Decimal("4.85"), currency="USD"),
            )
        ],
        minimum_release_quantity=5_000,
        incoterm="FOB",
        incoterm_location="Cleveland supplier dock",
        tooling_cost=Money(amount=Decimal("6200"), currency="USD"),
        tooling_notes="One-time; invoiced on PO",
        production_lead_time=RangeValue(minimum=4, maximum=4, unit="weeks"),
        payment_terms="Net 30",
        quote_validity_days=90,
        finishing="Anodizing performed in-house",
        evidence=[
            Evidence(field="price_tiers", excerpt="$4.85 per piece ... at your 60k annual number"),
            Evidence(field="minimum_release_quantity", excerpt="releases of 5,000 or more"),
            Evidence(field="incoterm", excerpt="FOB our dock in Cleveland"),
            Evidence(field="tooling_cost", excerpt="Tooling is $6,200, one time"),
            Evidence(field="production_lead_time", excerpt="Lead time 4 weeks from release"),
        ],
    ),
    "nordkant": QuoteTerms(
        supplier_name="Nordkant Präzisionsteile GmbH",
        part_number="BRK-4471-A",
        drawing_revision="C",
        price_tiers=[
            PriceTier(
                minimum_quantity=20_000,
                maximum_quantity=59_999,
                unit_price=Money(amount=Decimal("4.30"), currency="EUR"),
            ),
            PriceTier(
                minimum_quantity=60_000,
                maximum_quantity=99_999,
                unit_price=Money(amount=Decimal("3.92"), currency="EUR"),
            ),
            PriceTier(
                minimum_quantity=100_000, unit_price=Money(amount=Decimal("3.71"), currency="EUR")
            ),
        ],
        incoterm="DDP",
        incoterm_location="Columbus, Ohio",
        tooling_notes="Included; amortized over first 40,000 pieces",
        deposit=Money(amount=Decimal("2800"), currency="EUR"),
        deposit_refundable=True,
        production_lead_time=RangeValue(minimum=11, maximum=11, unit="weeks"),
        payment_terms="Net 60; 2% discount if paid within 10 days",
        quote_validity_days=30,
        price_adjustment=(
            "Quarterly LME aluminium index adjustment when movement exceeds 4%; base 2026 Q1"
        ),
        freight_included=True,
        duty_included=True,
        packaging="Returnable steel containers",
        evidence=[
            Evidence(field="drawing_revision", excerpt="per customer drawing rev C"),
            Evidence(field="price_tiers", excerpt="60,000 pcs EUR 3.92 / pc"),
            Evidence(field="incoterm", excerpt="Delivery: DDP Columbus, Ohio"),
            Evidence(field="deposit", excerpt="deposit EUR 2,800 (refundable)"),
            Evidence(
                field="price_adjustment", excerpt="quarterly adjustment per LME aluminium index"
            ),
        ],
    ),
    "pacific-rim": QuoteTerms(
        supplier_name="Pacific Rim Components",
        part_number="BRK-4471-A",
        drawing_revision="B",
        price_tiers=[
            PriceTier(
                minimum_quantity=25_000,
                unit_price=Money(amount=Decimal("3.24"), currency="USD"),
            )
        ],
        moq=25_000,
        incoterm="FOB",
        incoterm_location="Kaohsiung",
        tooling_cost=Money(amount=Decimal("4800"), currency="USD"),
        production_lead_time=RangeValue(minimum=9, maximum=9, unit="weeks"),
        transit_lead_time=RangeValue(minimum=5, maximum=5, unit="weeks"),
        payment_terms="30% T/T deposit; 70% before shipment",
        freight_included=False,
        duty_included=False,
        finishing="Anodizing subcontracted",
        evidence=[
            Evidence(field="drawing_revision", excerpt="PRICE BASED ON DRAWING REV B"),
            Evidence(field="price_tiers", excerpt="USD 3,240.00 / 1,000 PCS"),
            Evidence(field="moq", excerpt="MOQ 25,000 PCS"),
            Evidence(field="incoterm", excerpt="FOB KAOHSIUNG"),
            Evidence(field="freight_included", excerpt="FREIGHT AND DUTY NOT INCLUDED"),
        ],
    ),
    "brightwater": QuoteTerms(
        supplier_name="Brightwater Precision",
        part_number="BRK-4471-A",
        price_tiers=[
            PriceTier(
                minimum_quantity=60_000,
                unit_price=Money(amount=Decimal("4.40"), currency="USD"),
            )
        ],
        incoterm="EXW",
        incoterm_location="Queretaro",
        tooling_notes="TBD after engineering review",
        production_lead_time=RangeValue(minimum=6, maximum=8, unit="weeks"),
        inventory_commitment="Four weeks of safety stock held at supplier at no charge",
        evidence=[
            Evidence(field="price_tiers", excerpt="around $4.40 a piece at your volumes"),
            Evidence(field="incoterm", excerpt="EXW Queretaro"),
            Evidence(field="tooling_notes", excerpt="Tooling we'd need to confirm"),
            Evidence(
                field="inventory_commitment",
                excerpt="carry four weeks of safety stock ... at no charge",
            ),
        ],
    ),
}


def sample_payload() -> SamplePayload:
    return SamplePayload(
        description="Aron engineering take-home: BRK-4471-A at 60,000 units annually",
        request=AnalysisRequest(
            rfq=RFQContext(
                part_number="BRK-4471-A",
                annual_volume=60_000,
                destination="Columbus, Ohio",
            ),
            quotes=SAMPLE_QUOTES,
        ),
    )
