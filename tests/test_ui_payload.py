import pytest

from app.ui_payload import (
    advance_editor_version,
    prepare_analysis_payload,
    prepare_incremental_communication,
)


def test_advances_editor_version_to_invalidate_stale_widgets() -> None:
    state = {"editor_version": 4}

    result = advance_editor_version(state)

    assert result == 5
    assert state["editor_version"] == 5


def test_ignores_blank_quote_placeholders() -> None:
    payload = {
        "rfq": {"part_number": " BRK-4471-A ", "annual_volume": 60_000},
        "quotes": [
            {"source_id": "supplier-1", "text": ""},
            {"source_id": " supplier-2 ", "text": " quoted terms "},
        ],
    }

    prepared = prepare_analysis_payload(payload, {})

    assert prepared["rfq"]["part_number"] == "BRK-4471-A"
    assert prepared["quotes"] == [{"source_id": "supplier-2", "text": "quoted terms"}]


def test_rejects_request_when_all_quotes_are_blank() -> None:
    payload = {
        "rfq": {"part_number": "BRK-4471-A", "annual_volume": 60_000},
        "quotes": [{"source_id": "supplier-1", "text": "  "}],
    }

    with pytest.raises(ValueError, match="at least one supplier response"):
        prepare_analysis_payload(payload, {})


def test_rejects_duplicate_non_empty_source_ids() -> None:
    payload = {
        "rfq": {"part_number": "BRK-4471-A", "annual_volume": 60_000},
        "quotes": [
            {"source_id": "supplier", "text": "first"},
            {"source_id": "supplier", "text": "second"},
        ],
    }

    with pytest.raises(ValueError, match="must be unique"):
        prepare_analysis_payload(payload, {})


def test_parses_incremental_supplier_file_and_preserves_raw_text() -> None:
    raw_text = """Supplier: Tri-State Metalcraft
From: dmorrow@tristatemetalcraft.com
To: karen@buyer.example
Subject: Lead time update
Date: October 1, 2026 9:14 AM EDT

Lead time is now three weeks.
"""

    result = prepare_incremental_communication(raw_text, mailbox_account_id="karen@buyer.example")

    assert result["supplier_label"] == "Tri-State Metalcraft"
    assert result["sender"] == "dmorrow@tristatemetalcraft.com"
    assert result["recipients"] == ["karen@buyer.example"]
    assert result["raw_body"] == "Lead time is now three weeks."
    assert result["raw_message"] == raw_text
    assert result["sent_at"].startswith("2026-10-01T09:14:00")
