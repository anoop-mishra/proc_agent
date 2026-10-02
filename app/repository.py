from threading import RLock
from typing import Protocol

from app.models import AnalysisResponse


class AnalysisRepository(Protocol):
    def save(self, analysis: AnalysisResponse) -> None: ...

    def get(self, analysis_id: str) -> AnalysisResponse | None: ...


class InMemoryAnalysisRepository:
    def __init__(self) -> None:
        self._analyses: dict[str, AnalysisResponse] = {}
        self._lock = RLock()

    def save(self, analysis: AnalysisResponse) -> None:
        with self._lock:
            self._analyses[analysis.analysis_id] = analysis

    def get(self, analysis_id: str) -> AnalysisResponse | None:
        with self._lock:
            return self._analyses.get(analysis_id)
