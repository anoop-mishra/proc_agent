from pathlib import Path

from fastapi.testclient import TestClient

from app.api import create_app
from app.providers.fixture import FixtureExtractionProvider
from app.sample import sample_payload
from app.ui_payload import prepare_incremental_communication

TEST_DATA = Path(__file__).parents[1] / "data" / "test_data"


def test_sample_analysis_round_trip() -> None:
    client = TestClient(create_app(provider=FixtureExtractionProvider()))
    sample_response = client.get("/v1/samples/aron")

    assert sample_response.status_code == 200
    create_response = client.post(
        "/v1/analyses",
        json=sample_response.json()["request"],
    )

    assert create_response.status_code == 201
    analysis = create_response.json()
    assert len(analysis["normalized_quotes"]) == 4
    assert len(analysis["supplier_insights"]) == 4
    assert "award recommendation" in analysis["disclaimer"]

    get_response = client.get(f"/v1/analyses/{analysis['analysis_id']}")
    assert get_response.status_code == 200
    assert get_response.json() == analysis


def test_unknown_analysis_returns_not_found() -> None:
    client = TestClient(create_app(provider=FixtureExtractionProvider()))

    response = client.get("/v1/analyses/not-real")

    assert response.status_code == 404


def test_duplicate_source_ids_are_rejected() -> None:
    client = TestClient(create_app(provider=FixtureExtractionProvider()))
    request = sample_payload().request.model_dump(mode="json")
    request["quotes"][1]["source_id"] = request["quotes"][0]["source_id"]

    response = client.post("/v1/analyses", json=request)

    assert response.status_code == 422


def test_versioned_quote_and_communication_workflow() -> None:
    client = TestClient(create_app(provider=FixtureExtractionProvider()))
    quote_payload = {
        "actor": "buyer",
        "idempotency_key": "create-quote-1",
        "snapshot": {
            "quote_id": "quote-1",
            "rfq_id": "RFQ-100",
            "supplier_id": "supplier-1",
            "currency": "USD",
            "required_rfq_line_ids": ["rfq-line-1"],
            "lines": [
                {
                    "quote_line_id": "line-1",
                    "rfq_line_id": "rfq-line-1",
                    "response_status": "QUOTED",
                    "unit_price": {"amount": "10", "currency": "USD"},
                }
            ],
        },
    }
    created = client.post("/v1/supplier-quotes", json=quote_payload)
    assert created.status_code == 201
    assert created.json()["current_revision"] == 1

    changed = client.post(
        "/v1/supplier-quotes/quote-1/changes",
        json={
            "change_id": "change-1",
            "base_revision": 1,
            "expected_current_revision": 1,
            "idempotency_key": "change-key-1",
            "actor": "supplier",
            "operations": [
                {
                    "operation": "SET_FIELD",
                    "path": "/commercial_terms/payment_terms",
                    "value": "Net 60",
                }
            ],
        },
    )
    assert changed.status_code == 200
    assert changed.json()["quote"]["current_revision"] == 2
    assert len(client.get("/v1/supplier-quotes/quote-1/revisions").json()) == 2
    assert len(client.get("/v1/supplier-quotes/quote-1/changes").json()) == 1

    context = client.post(
        "/v1/rfq-match-contexts",
        json={"rfq_id": "RFQ-100", "supplier_addresses": ["sales@example.com"]},
    )
    assert context.status_code == 201
    communication = client.post(
        "/v1/communications",
        json={
            "channel": "EMAIL",
            "provider_message_id": "message-1",
            "mailbox_account_id": "buyer@example.com",
            "sender": "sales@example.com",
            "recipients": ["buyer@example.com"],
            "subject": "RFQ-100 quote",
            "raw_body": "Please find our RFQ-100 quote.",
        },
    )
    assert communication.status_code == 201
    communication_id = communication.json()["communication"]["communication_id"]
    resolution = client.post(f"/v1/communications/{communication_id}/resolve")
    assert resolution.status_code == 200
    assert resolution.json()["resolved_rfq_ids"] == ["RFQ-100"]

    flag = client.post(
        f"/v1/communications/{communication_id}/conflict-flags",
        json={
            "flag_type": "ORDERING_CONFLICT",
            "field_path": "/lines/line-1/delivery/lead_time",
            "quote_id": "quote-1",
            "based_on_revision": 1,
            "current_revision": 2,
            "base_value": "4 weeks",
            "current_value": "3 weeks",
            "proposed_value": "5 weeks",
            "note": "Older message conflicts with the accepted lead-time update.",
            "created_by": "buyer",
        },
    )
    assert flag.status_code == 201
    assert flag.json()["status"] == "OPEN"
    detail = client.get(f"/v1/communications/{communication_id}")
    assert detail.json()["conflict_flags"][0]["flag_type"] == "ORDERING_CONFLICT"


def test_incremental_documents_advance_revision_and_refresh_analysis() -> None:
    client = TestClient(create_app(provider=FixtureExtractionProvider()))
    request = sample_payload().request.model_dump(mode="json")

    initial = client.post("/v1/analyses", json=request)
    assert initial.status_code == 201
    initial_analysis = initial.json()
    tri_state = next(
        item for item in initial_analysis["supplier_insights"] if item["source_id"] == "tri-state"
    )
    quote_id = tri_state["quote_id"]
    assert quote_id
    assert tri_state["current_revision"] == 1

    communication_ids = []
    for index, filename in enumerate(
        [
            "aron_tri_state_update_1_duplicate.txt",
            "aron_tri_state_update_2.txt",
            "aron_tri_state_update_3_conflict.txt",
        ],
        start=1,
    ):
        raw_text = (TEST_DATA / filename).read_text()
        ingested = client.post(
            "/v1/communications",
            json={
                "channel": "EMAIL",
                "provider_message_id": f"aron-update-{index}",
                "sender": "dmorrow@tristatemetalcraft.com",
                "recipients": ["buyer@example.com"],
                "raw_body": raw_text,
            },
        )
        assert ingested.status_code == 201
        communication_ids.append(ingested.json()["communication"]["communication_id"])

    duplicate = client.post(
        f"/v1/supplier-quotes/{quote_id}/communications/{communication_ids[0]}/apply"
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["idempotent_replay"] is True
    assert duplicate.json()["quote"]["current_revision"] == 1

    clean = client.post(
        f"/v1/supplier-quotes/{quote_id}/communications/{communication_ids[1]}/apply"
    )
    assert clean.status_code == 200
    assert clean.json()["quote"]["current_revision"] == 2
    assert clean.json()["conflicts"] == []

    stale = client.post(
        f"/v1/supplier-quotes/{quote_id}/communications/{communication_ids[2]}/apply"
    )
    assert stale.status_code == 200
    stale_result = stale.json()
    assert stale_result["quote"]["current_revision"] == 3
    assert stale_result["conflicts"][0]["path"].endswith("/delivery")
    assert stale_result["quote"]["current"]["lines"][0]["delivery"]["lead_time"]["minimum"] == "3"
    assert (
        "Returnable totes at no additional charge"
        in stale_result["quote"]["current"]["lines"][0]["notes"]
    )

    refreshed = client.post("/v1/analyses", json=request)
    assert refreshed.status_code == 201
    refreshed_analysis = refreshed.json()
    refreshed_tri_state = next(
        item for item in refreshed_analysis["supplier_insights"] if item["source_id"] == "tri-state"
    )
    refreshed_terms = refreshed_analysis["normalized_quotes"]["tri-state"]
    assert refreshed_tri_state["current_revision"] == 3
    assert refreshed_analysis["quote_revisions"]["tri-state"] == 3
    assert refreshed_terms["production_lead_time"]["minimum"] == "3"
    assert "Returnable totes at no additional charge" in refreshed_terms["notes"]

    replay = client.post(
        f"/v1/supplier-quotes/{quote_id}/communications/{communication_ids[2]}/apply"
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["quote"]["current_revision"] == 3
    assert len(client.get(f"/v1/supplier-quotes/{quote_id}/revisions").json()) == 3


def test_document_batch_orders_matches_and_surfaces_analysis_conflicts() -> None:
    client = TestClient(create_app(provider=FixtureExtractionProvider()))
    request = sample_payload().request.model_dump(mode="json")
    assert client.post("/v1/analyses", json=request).status_code == 201

    documents = []
    for filename in [
        "aron_tri_state_update_1_duplicate.txt",
        "aron_tri_state_update_3_conflict.txt",
        "aron_tri_state_update_2.txt",
    ]:
        communication = prepare_incremental_communication((TEST_DATA / filename).read_text())
        supplier_name = communication.pop("supplier_label")
        documents.append(
            {
                "filename": filename,
                "supplier_name": supplier_name,
                "communication": communication,
            }
        )

    response = client.post("/v1/supplier-documents/ingest", json={"documents": documents})
    assert response.status_code == 201
    processed = response.json()["documents"]
    assert [item["filename"] for item in processed] == [
        "aron_tri_state_update_2.txt",
        "aron_tri_state_update_3_conflict.txt",
        "aron_tri_state_update_1_duplicate.txt",
    ]
    assert all(item["matched_quote_id"] for item in processed)
    assert len(processed[1]["conflict_flags"]) == 1

    analysis = client.post("/v1/analyses", json=request).json()
    assert analysis["quote_revisions"]["tri-state"] == 3
    assert analysis["normalized_quotes"]["tri-state"]["production_lead_time"]["minimum"] == "3"
    assert len(analysis["quote_conflicts"]["tri-state"]) == 1
    assert analysis["quote_conflicts"]["tri-state"][0]["field_path"].endswith("/delivery")
