import json
from pathlib import Path

from app.providers.fixture import FixtureExtractionProvider
from app.repository import InMemoryAnalysisRepository
from app.sample import sample_payload
from app.service import AnalysisService

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "artifacts" / "aron-analysis.json"


def main() -> None:
    service = AnalysisService(
        provider=FixtureExtractionProvider(),
        repository=InMemoryAnalysisRepository(),
    )
    result = service.analyze(sample_payload().request)
    OUTPUT_PATH.write_text(
        json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
