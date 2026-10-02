from typing import Protocol

from app.models import QuoteTerms, RawQuote, RFQContext


class ProviderError(RuntimeError):
    """Raised when quote extraction cannot produce validated terms."""


class ExtractionProvider(Protocol):
    def extract(self, quote: RawQuote, rfq: RFQContext) -> QuoteTerms: ...
