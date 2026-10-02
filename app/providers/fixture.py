from app.models import QuoteTerms, RawQuote, RFQContext
from app.providers.base import ProviderError
from app.sample import SAMPLE_TERMS


class FixtureExtractionProvider:
    """Deterministic provider for the supplied take-home packet."""

    def extract(self, quote: RawQuote, rfq: RFQContext) -> QuoteTerms:
        terms = SAMPLE_TERMS.get(quote.source_id)
        if terms is None:
            raise ProviderError(
                "Fixture provider only supports sample sources; "
                f"unknown source_id={quote.source_id!r}"
            )
        return terms.model_copy(deep=True)
