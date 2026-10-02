import os
from typing import Annotated
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.document_service import (
    SupplierDocumentBatchRequest,
    SupplierDocumentBatchResult,
    SupplierDocumentService,
)
from app.ingestion import (
    ChangeCandidateBatch,
    CommunicationConflictFlag,
    CommunicationDetail,
    CommunicationIngestionService,
    CreateConflictFlagRequest,
    InboundCommunicationInput,
    IngestionResult,
    InMemoryCommunicationRepository,
    ManualResolutionRequest,
    QuoteChangeCandidate,
    RFQMatchContext,
    RFQResolution,
    RFQResolutionService,
)
from app.models import AnalysisRequest, AnalysisResponse, SamplePayload
from app.providers import (
    ExtractionProvider,
    FixtureExtractionProvider,
    OpenAIExtractionProvider,
    ProviderError,
)
from app.quote_models import (
    CommitResult,
    CreateQuoteRequest,
    QuoteChangeRequest,
    SupplierQuote,
    SupplierQuoteChange,
    SupplierQuoteRevision,
)
from app.repository import AnalysisRepository, InMemoryAnalysisRepository
from app.sample import sample_payload
from app.service import AnalysisService
from app.update_provider import (
    DeterministicUpdateExtractionProvider,
    OpenAIUpdateExtractionProvider,
    QuoteUpdateApplicationService,
    QuoteUpdateExtractionProvider,
    UpdateExtractionError,
)
from app.versioning import (
    ConcurrentQuoteUpdateError,
    InMemoryQuoteVersionRepository,
    QuoteAlreadyExistsError,
    QuoteNotFoundError,
    QuoteValidationError,
    QuoteVersioningService,
    QuoteVersionRepository,
)

load_dotenv()


def _provider_from_environment() -> ExtractionProvider:
    provider_name = os.getenv("LLM_PROVIDER", "fixture").lower()
    if provider_name == "fixture":
        return FixtureExtractionProvider()
    if provider_name == "openai":
        return OpenAIExtractionProvider(model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))
    raise RuntimeError(f"Unsupported LLM_PROVIDER={provider_name!r}")


def _update_provider_from_environment() -> QuoteUpdateExtractionProvider:
    provider_name = os.getenv("LLM_PROVIDER", "fixture").lower()
    if provider_name == "fixture":
        return DeterministicUpdateExtractionProvider()
    if provider_name == "openai":
        return OpenAIUpdateExtractionProvider(model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))
    raise RuntimeError(f"Unsupported LLM_PROVIDER={provider_name!r}")


def create_app(
    provider: ExtractionProvider | None = None,
    repository: AnalysisRepository | None = None,
    quote_repository: QuoteVersionRepository | None = None,
    communication_repository: InMemoryCommunicationRepository | None = None,
    update_provider: QuoteUpdateExtractionProvider | None = None,
) -> FastAPI:
    api = FastAPI(
        title="Procurement RFQ Insight Agent",
        version="0.1.0",
        description="Normalizes supplier quotes and surfaces auditable buyer insights.",
    )
    quote_service = QuoteVersioningService(
        repository=quote_repository or InMemoryQuoteVersionRepository()
    )
    communication_store = communication_repository or InMemoryCommunicationRepository()
    service = AnalysisService(
        provider=provider or _provider_from_environment(),
        repository=repository or InMemoryAnalysisRepository(),
        quote_versioning=quote_service,
        communications=communication_store,
    )
    ingestion_service = CommunicationIngestionService(communication_store)
    resolution_service = RFQResolutionService(communication_store)
    update_service = QuoteUpdateApplicationService(
        communications=communication_store,
        versioning=quote_service,
        provider=update_provider or _update_provider_from_environment(),
    )
    document_service = SupplierDocumentService(
        ingestion=ingestion_service,
        communications=communication_store,
        versioning=quote_service,
        updates=update_service,
    )

    def get_service() -> AnalysisService:
        return service

    @api.exception_handler(ProviderError)
    def handle_provider_error(request: Request, error: ProviderError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={"error": "quote_extraction_failed", "detail": str(error)},
        )

    @api.exception_handler(QuoteNotFoundError)
    def handle_quote_not_found(request: Request, error: QuoteNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": f"Quote {error.args[0]} not found"})

    @api.exception_handler(QuoteAlreadyExistsError)
    def handle_quote_exists(request: Request, error: QuoteAlreadyExistsError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @api.exception_handler(ConcurrentQuoteUpdateError)
    def handle_concurrent_update(
        request: Request, error: ConcurrentQuoteUpdateError
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @api.exception_handler(QuoteValidationError)
    def handle_quote_validation(request: Request, error: QuoteValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(error)})

    @api.exception_handler(UpdateExtractionError)
    def handle_update_extraction(request: Request, error: UpdateExtractionError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(error)})

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/v1/samples/aron", response_model=SamplePayload)
    def get_aron_sample() -> SamplePayload:
        return sample_payload()

    @api.post("/v1/analyses", response_model=AnalysisResponse, status_code=201)
    def create_analysis(
        analysis_request: AnalysisRequest,
        analysis_service: Annotated[AnalysisService, Depends(get_service)],
    ) -> AnalysisResponse:
        return analysis_service.analyze(analysis_request)

    @api.get("/v1/analyses/{analysis_id}", response_model=AnalysisResponse)
    def get_analysis(
        analysis_id: str,
        analysis_service: Annotated[AnalysisService, Depends(get_service)],
    ) -> AnalysisResponse:
        analysis = analysis_service.get(analysis_id)
        if analysis is None:
            raise HTTPException(status_code=404, detail="Analysis not found")
        return analysis

    @api.post("/v1/supplier-quotes", response_model=SupplierQuote, status_code=201)
    def create_supplier_quote(request: CreateQuoteRequest) -> SupplierQuote:
        return quote_service.create(request)

    @api.get("/v1/supplier-quotes/{quote_id}", response_model=SupplierQuote)
    def get_supplier_quote(quote_id: str) -> SupplierQuote:
        return quote_service.get(quote_id)

    @api.get(
        "/v1/supplier-quotes/{quote_id}/revisions",
        response_model=list[SupplierQuoteRevision],
    )
    def get_supplier_quote_revisions(quote_id: str) -> list[SupplierQuoteRevision]:
        return quote_service.revisions(quote_id)

    @api.get(
        "/v1/supplier-quotes/{quote_id}/changes",
        response_model=list[SupplierQuoteChange],
    )
    def get_supplier_quote_changes(quote_id: str) -> list[SupplierQuoteChange]:
        return quote_service.changes(quote_id)

    @api.post(
        "/v1/supplier-quotes/{quote_id}/changes",
        response_model=CommitResult,
    )
    def commit_supplier_quote_change(quote_id: str, request: QuoteChangeRequest) -> CommitResult:
        return quote_service.commit(quote_id, request)

    @api.post(
        "/v1/supplier-quotes/{quote_id}/communications/{communication_id}/apply",
        response_model=CommitResult,
    )
    def apply_communication_update(quote_id: str, communication_id: str) -> CommitResult:
        try:
            return update_service.apply(quote_id, communication_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Communication not found") from error

    @api.post("/v1/communications", response_model=IngestionResult, status_code=201)
    def ingest_communication(request: InboundCommunicationInput) -> IngestionResult:
        return ingestion_service.ingest(request)

    @api.post(
        "/v1/supplier-documents/ingest",
        response_model=SupplierDocumentBatchResult,
        status_code=201,
    )
    def ingest_supplier_documents(
        request: SupplierDocumentBatchRequest,
    ) -> SupplierDocumentBatchResult:
        return document_service.ingest(request)

    @api.get("/v1/communications/{communication_id}", response_model=CommunicationDetail)
    def get_communication(communication_id: str) -> CommunicationDetail:
        communication = communication_store.get(communication_id)
        if communication is None:
            raise HTTPException(status_code=404, detail="Communication not found")
        return CommunicationDetail(
            communication=communication,
            artifacts=communication_store.artifacts(communication_id),
            resolutions=communication_store.resolutions(communication_id),
            change_candidates=communication_store.candidates(communication_id),
            conflict_flags=communication_store.conflict_flags(communication_id),
        )

    @api.post("/v1/rfq-match-contexts", response_model=RFQMatchContext, status_code=201)
    def register_rfq_match_context(context: RFQMatchContext) -> RFQMatchContext:
        return resolution_service.register(context)

    @api.post(
        "/v1/communications/{communication_id}/resolve",
        response_model=RFQResolution,
    )
    def resolve_communication(communication_id: str) -> RFQResolution:
        try:
            return resolution_service.resolve(communication_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Communication not found") from error

    @api.post(
        "/v1/communications/{communication_id}/manual-resolution",
        response_model=RFQResolution,
    )
    def manually_resolve_communication(
        communication_id: str, request: ManualResolutionRequest
    ) -> RFQResolution:
        try:
            return resolution_service.manually_resolve(
                communication_id, request.rfq_ids, request.actor
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Communication not found") from error

    @api.post(
        "/v1/communications/{communication_id}/change-candidates",
        response_model=list[QuoteChangeCandidate],
        status_code=201,
    )
    def save_change_candidates(
        communication_id: str, batch: ChangeCandidateBatch
    ) -> list[QuoteChangeCandidate]:
        if communication_store.get(communication_id) is None:
            raise HTTPException(status_code=404, detail="Communication not found")
        if any(item.communication_id != communication_id for item in batch.candidates):
            raise HTTPException(
                status_code=422,
                detail="Candidate communication_id must match path communication_id",
            )
        communication_store.save_candidates(batch.candidates)
        return batch.candidates

    @api.post(
        "/v1/communications/{communication_id}/conflict-flags",
        response_model=CommunicationConflictFlag,
        status_code=201,
    )
    def create_communication_conflict_flag(
        communication_id: str, request: CreateConflictFlagRequest
    ) -> CommunicationConflictFlag:
        if communication_store.get(communication_id) is None:
            raise HTTPException(status_code=404, detail="Communication not found")
        flag = CommunicationConflictFlag(
            **request.model_dump(),
            flag_id=str(uuid4()),
            communication_id=communication_id,
        )
        communication_store.save_conflict_flag(flag)
        return flag

    @api.get(
        "/v1/communications/{communication_id}/conflict-flags",
        response_model=list[CommunicationConflictFlag],
    )
    def get_communication_conflict_flags(
        communication_id: str,
    ) -> list[CommunicationConflictFlag]:
        if communication_store.get(communication_id) is None:
            raise HTTPException(status_code=404, detail="Communication not found")
        return communication_store.conflict_flags(communication_id)

    return api


app = create_app()
