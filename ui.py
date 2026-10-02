import json
import os
from typing import Any

import httpx
import streamlit as st

from app.ui_payload import (
    advance_editor_version,
    prepare_analysis_payload,
    prepare_incremental_communication,
)

API_URL = os.getenv("API_URL", "http://localhost:8000").rstrip("/")

st.set_page_config(page_title="QuoteLens", page_icon="◫", layout="wide")


def api_get(path: str) -> Any:
    response = httpx.get(f"{API_URL}{path}", timeout=15)
    response.raise_for_status()
    return response.json()


def api_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = httpx.post(f"{API_URL}{path}", json=payload, timeout=90)
    if response.is_error:
        detail = (
            response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else response.text
        )
        raise RuntimeError(f"API returned {response.status_code}: {detail}")
    return response.json()


def money_text(money: dict[str, Any] | None) -> str:
    if not money:
        return "—"
    amount = float(money["amount"])
    return f"{money['currency']} {amount:,.2f}"


def range_text(value: dict[str, Any] | None) -> str:
    if not value:
        return "—"
    minimum = float(value["minimum"])
    maximum = float(value["maximum"])
    amount = f"{minimum:g}" if minimum == maximum else f"{minimum:g}–{maximum:g}"
    return f"{amount} {value['unit']}"


def is_incremental_document(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "one update",
            "following up",
            "instead of",
            "revise the lead",
            "everything else stays the same",
        )
    )


def mark_conflict(value: str, conflicts: list[dict[str, Any]], *path_terms: str) -> str:
    if any(
        any(term in (item.get("field_path") or "").lower() for term in path_terms)
        for item in conflicts
    ):
        return f"⚠ {value}"
    return value


def render_incremental_updates() -> None:
    st.divider()
    st.header("Incremental quote updates")
    st.caption(
        "Upload supplier follow-ups as raw text. QuoteLens preserves the original file, "
        "checks retries, resolves RFQ context, applies accepted changes to the canonical "
        "revision, and records conflicts."
    )
    if "incremental_records" not in st.session_state:
        st.session_state.incremental_records = []

    context_col, quote_col, mailbox_col = st.columns(3)
    rfq_id = context_col.text_input(
        "RFQ ID for matching (optional)",
        placeholder="ARON-RFQ-BRK-4471-A",
        key="incremental-rfq-id",
    )
    quote_options = [None]
    quote_labels = {None: "Ingest only (do not apply)"}
    for insight in st.session_state.get("analysis", {}).get("supplier_insights", []):
        if insight.get("quote_id"):
            quote_options.append(insight["quote_id"])
            quote_labels[insight["quote_id"]] = (
                f"{insight['supplier_name']} · revision {insight['current_revision']}"
            )
    quote_id = quote_col.selectbox(
        "Apply updates to canonical quote",
        options=quote_options,
        format_func=lambda value: quote_labels[value],
        key="incremental-quote-id",
        help="Run an analysis first to create and select a canonical supplier quote.",
    )
    mailbox = mailbox_col.text_input(
        "Buyer mailbox",
        value="karen@buyer.example",
        key="incremental-mailbox",
    )
    fallback_supplier = st.text_input(
        "Supplier fallback when the file has no Supplier header",
        value="Unknown supplier",
        key="incremental-supplier",
    )
    update_files = st.file_uploader(
        "Upload incremental quote or update files",
        type=["txt"],
        accept_multiple_files=True,
        key="incremental-files",
        help="Email-style Supplier, From, To, Subject, and Date headers are parsed when present.",
    )
    action_col, clear_col = st.columns([1, 1])
    if action_col.button(
        "Ingest updates",
        type="primary",
        disabled=not update_files,
        use_container_width=True,
    ):
        applied_any = False
        for uploaded in update_files:
            try:
                raw_text = uploaded.getvalue().decode("utf-8")
                communication_payload = prepare_incremental_communication(
                    raw_text,
                    default_supplier=fallback_supplier,
                    mailbox_account_id=mailbox or None,
                )
                supplier_label = communication_payload.pop("supplier_label")
                if rfq_id:
                    item_identifier = st.session_state.request_payload["rfq"].get("part_number")
                    api_post(
                        "/v1/rfq-match-contexts",
                        {
                            "rfq_id": rfq_id,
                            "supplier_addresses": [communication_payload["sender"]],
                            "item_identifiers": [item_identifier] if item_identifier else [],
                        },
                    )
                ingestion = api_post("/v1/communications", communication_payload)
                communication_id = ingestion["communication"]["communication_id"]
                resolution = (
                    api_post(f"/v1/communications/{communication_id}/resolve", {})
                    if rfq_id
                    else None
                )
                application = None
                automatic_flags = []
                if quote_id:
                    application = api_post(
                        f"/v1/supplier-quotes/{quote_id}/communications/{communication_id}/apply",
                        {},
                    )
                    applied_any = applied_any or application.get("revision") is not None
                    accepted_change = application.get("accepted_change") or {}
                    for conflict in (
                        [] if application["idempotent_replay"] else application["conflicts"]
                    ):
                        flag_type = (
                            conflict["conflict_type"]
                            if conflict["conflict_type"]
                            in {
                                "FIELD_CONFLICT",
                                "ORDERING_CONFLICT",
                            }
                            else "OTHER"
                        )
                        automatic_flags.append(
                            api_post(
                                f"/v1/communications/{communication_id}/conflict-flags",
                                {
                                    "flag_type": flag_type,
                                    "field_path": conflict["path"],
                                    "quote_id": quote_id,
                                    "based_on_revision": accepted_change.get("based_on_revision"),
                                    "current_revision": application["quote"]["current_revision"],
                                    "base_value": conflict.get("base_value"),
                                    "current_value": conflict.get("current_value"),
                                    "proposed_value": conflict.get("proposed_value"),
                                    "note": conflict["message"],
                                    "created_by": "quote-update-service",
                                },
                            )
                        )
                record = {
                    "filename": uploaded.name,
                    "supplier": supplier_label,
                    "quote_id": quote_id or None,
                    "ingestion": ingestion,
                    "resolution": resolution,
                    "application": application,
                    "flags": api_get(f"/v1/communications/{communication_id}/conflict-flags"),
                }
                existing_index = next(
                    (
                        index
                        for index, item in enumerate(st.session_state.incremental_records)
                        if item["ingestion"]["communication"]["communication_id"]
                        == communication_id
                    ),
                    None,
                )
                if existing_index is None:
                    st.session_state.incremental_records.append(record)
                else:
                    st.session_state.incremental_records[existing_index] = record
            except (UnicodeDecodeError, ValueError, httpx.HTTPError, RuntimeError) as error:
                st.error(f"Could not ingest {uploaded.name}: {error}")
        if applied_any and st.session_state.get("analysis_request"):
            try:
                st.session_state.analysis = api_post(
                    "/v1/analyses", st.session_state.analysis_request
                )
            except (httpx.HTTPError, RuntimeError) as error:
                st.error(f"Updates were applied, but analysis refresh failed: {error}")
        st.rerun()
    if clear_col.button("Clear update workspace", use_container_width=True):
        st.session_state.incremental_records = []
        st.rerun()

    records = st.session_state.incremental_records
    if not records:
        st.info("Upload one or more `.txt` supplier updates to begin.")
        return

    retry_count = sum(item["ingestion"]["is_retry"] for item in records)
    flag_count = sum(len(item["flags"]) for item in records)
    metric_1, metric_2, metric_3 = st.columns(3)
    metric_1.metric("Update files", len(records))
    metric_2.metric("Transport retries", retry_count)
    metric_3.metric("Open conflict flags", flag_count)

    for record in records:
        communication = record["ingestion"]["communication"]
        communication_id = communication["communication_id"]
        label = f"{record['supplier']} · {record['filename']}"
        with st.expander(label, expanded=True):
            status_cols = st.columns(4)
            status_cols[0].write(f"**Subject**  \n{communication.get('subject') or 'Not stated'}")
            status_cols[1].write(f"**Sent**  \n{communication.get('sent_at') or 'Not stated'}")
            status_cols[2].write(
                "**Ingestion**  \n"
                + ("Retry—original retained" if record["ingestion"]["is_retry"] else "New evidence")
            )
            resolution = record.get("resolution")
            status_cols[3].write(
                f"**RFQ resolution**  \n{resolution['method'] if resolution else 'Not requested'}"
            )
            application = record.get("application")
            if application:
                if application.get("revision"):
                    st.success(
                        "Applied to canonical quote revision "
                        f"{application['quote']['current_revision']}."
                    )
                elif application.get("idempotent_replay"):
                    st.info(
                        "This update was already reflected in revision "
                        f"{application['quote']['current_revision']}; no new revision was created."
                    )
                if application["conflicts"]:
                    for conflict in application["conflicts"]:
                        st.error(
                            f"{conflict['conflict_type']} · {conflict['path']}: "
                            f"{conflict['message']}"
                        )
            if record["ingestion"]["possible_resubmission_of"]:
                st.warning(
                    "Content resembles an earlier message but was retained as a possible "
                    "legitimate resend."
                )
            st.code(communication["raw_body"], language=None)

            if record["flags"]:
                st.write("**Conflict flags**")
                for flag in record["flags"]:
                    path = f" · {flag['field_path']}" if flag.get("field_path") else ""
                    st.error(f"{flag['flag_type']}{path}: {flag['note']}")

            st.write("**Flag a conflict for buyer review**")
            flag_cols = st.columns([1, 1, 1, 1])
            flag_type = flag_cols[0].selectbox(
                "Conflict type",
                [
                    "FIELD_CONFLICT",
                    "ORDERING_CONFLICT",
                    "SPECIFICATION_CONFLICT",
                    "STALE_OR_DUPLICATE",
                    "OTHER",
                ],
                key=f"flag-type-{communication_id}",
            )
            field_path = flag_cols[1].text_input(
                "Field path (optional)",
                placeholder="/lines/line-1/delivery/lead_time",
                key=f"flag-path-{communication_id}",
            )
            based_on_revision = flag_cols[2].number_input(
                "Based on revision",
                min_value=0,
                value=0,
                key=f"flag-base-{communication_id}",
                help="Use 0 when the supplier did not identify a base revision.",
            )
            current_revision = flag_cols[3].number_input(
                "Current revision",
                min_value=0,
                value=0,
                key=f"flag-current-{communication_id}",
                help="Use 0 when the update is not yet associated with a canonical quote.",
            )
            note = st.text_input(
                "Why is this a conflict?",
                placeholder="Supplier proposes 5 weeks; current revision already says 3 weeks.",
                key=f"flag-note-{communication_id}",
            )
            created_by = st.text_input(
                "Flagged by",
                value="buyer",
                key=f"flag-actor-{communication_id}",
            )
            if st.button("Flag conflict", key=f"flag-submit-{communication_id}"):
                if not note.strip():
                    st.error("Add a short explanation before flagging the conflict.")
                else:
                    try:
                        flag = api_post(
                            f"/v1/communications/{communication_id}/conflict-flags",
                            {
                                "flag_type": flag_type,
                                "field_path": field_path or None,
                                "quote_id": record.get("quote_id"),
                                "based_on_revision": based_on_revision or None,
                                "current_revision": current_revision or None,
                                "note": note,
                                "created_by": created_by or "buyer",
                            },
                        )
                        record["flags"].append(flag)
                        st.rerun()
                    except (httpx.HTTPError, RuntimeError) as error:
                        st.error(f"Could not flag conflict: {error}")


def render_analysis(analysis: dict[str, Any]) -> None:
    insights = analysis["supplier_insights"]
    issue_list = [issue for supplier in insights for issue in supplier["issues"]]
    critical_count = sum(issue["severity"] == "critical" for issue in issue_list)
    warning_count = sum(issue["severity"] == "warning" for issue in issue_list)

    st.divider()
    st.subheader("Buyer brief")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Suppliers", len(insights))
    col2.metric("Critical exceptions", critical_count)
    col3.metric("Commercial warnings", warning_count)
    col4.metric("Annual volume", f"{analysis['rfq']['annual_volume']:,}")

    for observation in analysis["cross_supplier_insights"]:
        st.info(observation, icon="ℹ️")

    st.subheader("Commercial comparison")
    rows = []
    for supplier in insights:
        source_id = supplier["source_id"]
        terms = analysis["normalized_quotes"][source_id]
        conflicts = analysis.get("quote_conflicts", {}).get(source_id, [])
        selected = supplier["selected_price"]
        unit_price = selected["unit_price"] if selected else None
        annual_cost = selected["annual_material_cost"] if selected else None
        lead_time = range_text(terms.get("production_lead_time"))
        if terms.get("transit_lead_time"):
            lead_time += f" + {range_text(terms['transit_lead_time'])} transit"
        rows.append(
            {
                "Supplier": supplier["supplier_name"],
                "Quote revision": supplier.get("current_revision") or "—",
                "Quote ID": supplier.get("quote_id") or "—",
                "Drawing revision": mark_conflict(
                    terms.get("drawing_revision") or "Not stated",
                    conflicts,
                    "drawing_revision",
                ),
                "Unit price": mark_conflict(
                    money_text(unit_price), conflicts, "unit_price", "price_tiers"
                ),
                "Annual material": money_text(annual_cost),
                "Incoterm": mark_conflict(
                    " ".join(
                        item
                        for item in [
                            terms.get("incoterm"),
                            terms.get("incoterm_location"),
                        ]
                        if item
                    )
                    or "Not stated",
                    conflicts,
                    "incoterm",
                    "delivery_terms",
                ),
                "Lead time": mark_conflict(lead_time, conflicts, "delivery", "lead_time"),
                "Tooling": mark_conflict(
                    money_text(terms.get("tooling_cost"))
                    if terms.get("tooling_cost")
                    else terms.get("tooling_notes") or "Not stated",
                    conflicts,
                    "charges",
                    "tooling",
                ),
                "Refundable deposit": money_text(terms.get("deposit"))
                if terms.get("deposit_refundable")
                else "—",
                "Payment": mark_conflict(
                    terms.get("payment_terms") or "Not stated",
                    conflicts,
                    "payment_terms",
                ),
                "Validity": f"{terms['quote_validity_days']} days"
                if terms.get("quote_validity_days")
                else "Not stated",
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)
    if any(analysis.get("quote_conflicts", {}).values()):
        st.caption("⚠ This value has conflicting supplier updates; inspect the supplier details.")

    st.caption(analysis["disclaimer"])

    st.subheader("Exceptions and follow-ups")
    for supplier in insights:
        source_id = supplier["source_id"]
        terms = analysis["normalized_quotes"][source_id]
        with st.expander(supplier["supplier_name"], expanded=True):
            conflicts = analysis.get("quote_conflicts", {}).get(source_id, [])
            for conflict in conflicts:
                st.error(
                    f"Update conflict · {conflict.get('field_path') or 'unknown field'}: "
                    f"{conflict['note']}"
                )
            for issue in supplier["issues"]:
                message = issue["message"]
                if issue["severity"] == "critical":
                    st.error(message, icon="🚨")
                elif issue["severity"] == "warning":
                    st.warning(message, icon="⚠️")
                else:
                    st.info(message, icon="ℹ️")

            if supplier["missing_fields"]:
                st.write("**Not stated:** " + ", ".join(supplier["missing_fields"]))
            if supplier["clarification_questions"]:
                st.write("**Questions to send**")
                for question in supplier["clarification_questions"]:
                    st.write(f"- {question}")

            if terms["evidence"]:
                st.write("**Source evidence**")
                for evidence in terms["evidence"]:
                    st.caption(f"{evidence['field']}: “{evidence['excerpt']}”")


st.title("QuoteLens")
st.caption("Turn inconsistent RFQ responses into an auditable buyer brief.")

if "request_payload" not in st.session_state:
    st.session_state.request_payload = {
        "rfq": {
            "part_number": "",
            "annual_volume": 1,
            "drawing_revision": None,
            "destination": None,
        },
        "quotes": [{"source_id": "supplier-1", "text": ""}],
        "assumptions": {},
    }
if "editor_version" not in st.session_state:
    st.session_state.editor_version = 0

top_left, top_right = st.columns([1, 4])
if top_left.button("Load Aron sample", type="primary", use_container_width=True):
    try:
        sample = api_get("/v1/samples/aron")
        st.session_state.request_payload = sample["request"]
        st.session_state.pop("analysis", None)
        st.session_state.pop("analysis_request", None)
        st.session_state.pop("document_results", None)
        st.session_state.pop("pending_documents", None)
        advance_editor_version(st.session_state)
        st.rerun()
    except (httpx.HTTPError, RuntimeError) as error:
        st.error(f"Could not load sample: {error}")
top_right.caption(f"Backend: {API_URL}")

payload = st.session_state.request_payload
editor_version = st.session_state.editor_version
st.subheader("RFQ context")
context_cols = st.columns(4)
payload["rfq"]["part_number"] = context_cols[0].text_input(
    "Part number",
    value=payload["rfq"].get("part_number", ""),
    key=f"part-number-{editor_version}",
)
payload["rfq"]["annual_volume"] = context_cols[1].number_input(
    "Annual volume",
    min_value=1,
    value=int(payload["rfq"].get("annual_volume", 1)),
    key=f"annual-volume-{editor_version}",
)
payload["rfq"]["drawing_revision"] = (
    context_cols[2].text_input(
        "Current revision (optional)",
        value=payload["rfq"].get("drawing_revision") or "",
        key=f"drawing-revision-{editor_version}",
    )
    or None
)
payload["rfq"]["destination"] = (
    context_cols[3].text_input(
        "Destination (optional)",
        value=payload["rfq"].get("destination") or "",
        key=f"destination-{editor_version}",
    )
    or None
)

st.subheader("Supplier responses")
uploaded_files = st.file_uploader(
    "Upload supplier documents",
    type=["txt"],
    accept_multiple_files=True,
    key=f"uploads-{editor_version}",
    help=(
        "Upload initial quotes and later updates together. Documents are preserved, ordered, "
        "matched to suppliers, and applied to canonical quote snapshots."
    ),
)
if st.button("Ingest uploaded documents", disabled=not uploaded_files):
    try:
        documents = []
        decoded = []
        for uploaded in uploaded_files:
            raw_text = uploaded.getvalue().decode("utf-8")
            communication = prepare_incremental_communication(raw_text)
            supplier_name = communication.pop("supplier_label")
            documents.append(
                {
                    "filename": uploaded.name,
                    "supplier_name": supplier_name,
                    "communication": communication,
                }
            )
            decoded.append((uploaded.name, supplier_name, raw_text))

        batch_payload = {"documents": documents}
        result = api_post("/v1/supplier-documents/ingest", batch_payload)
        st.session_state.document_results = result["documents"]
        unmatched = {
            item["filename"] for item in result["documents"] if not item["matched_quote_id"]
        }
        known_source_ids = {
            quote["source_id"].lower().replace("-", " ")
            for quote in payload["quotes"]
            if quote["text"].strip()
        }
        pending_filenames = {
            filename
            for filename, supplier_name, raw_text in decoded
            if filename in unmatched
            and (
                is_incremental_document(raw_text)
                or any(
                    source_id in supplier_name.lower().replace("-", " ")
                    for source_id in known_source_ids
                )
            )
        }
        st.session_state.pending_documents = [
            document for document in documents if document["filename"] in pending_filenames
        ]

        added_initial = False
        payload["quotes"] = [quote for quote in payload["quotes"] if quote["text"].strip()]
        existing_ids = {quote["source_id"] for quote in payload["quotes"]}
        for filename, supplier_name, raw_text in decoded:
            if filename not in unmatched or filename in pending_filenames:
                continue
            base_id = supplier_name.lower().replace(" ", "-")
            if base_id == "unknown-supplier":
                base_id = filename.rsplit(".", 1)[0].lower().replace(" ", "-")
            source_id = base_id
            suffix = 2
            while source_id in existing_ids:
                source_id = f"{base_id}-{suffix}"
                suffix += 1
            existing_ids.add(source_id)
            payload["quotes"].append({"source_id": source_id, "text": raw_text})
            added_initial = True

        if "analysis" in st.session_state and st.session_state.get("analysis_request"):
            st.session_state.analysis = api_post("/v1/analyses", st.session_state.analysis_request)
        if added_initial:
            advance_editor_version(st.session_state)
        st.rerun()
    except (UnicodeDecodeError, ValueError, httpx.HTTPError, RuntimeError) as error:
        st.error(f"Could not ingest supplier documents: {error}")

if st.session_state.get("document_results"):
    with st.expander("Document processing", expanded=True):
        for item in sorted(
            st.session_state.document_results,
            key=lambda value: value["logical_order"],
        ):
            application = item.get("application")
            if application and application.get("revision"):
                outcome = f"revision {application['quote']['current_revision']}"
            elif application and application.get("idempotent_replay"):
                outcome = "already represented"
            elif item.get("matched_quote_id"):
                outcome = "matched; no accepted change"
            else:
                outcome = "awaiting initial analysis"
            conflict_count = len(item.get("conflict_flags", []))
            conflict_text = f" · ⚠ {conflict_count} conflict" if conflict_count else ""
            st.write(f"{item['logical_order']}. **{item['filename']}** — {outcome}{conflict_text}")

remove_index = None
for index, quote in enumerate(payload["quotes"]):
    with st.expander(quote["source_id"], expanded=len(payload["quotes"]) <= 2):
        quote["source_id"] = st.text_input(
            "Source ID",
            value=quote["source_id"],
            key=f"source-{editor_version}-{index}",
        )
        quote["text"] = st.text_area(
            "Raw supplier response",
            value=quote["text"],
            height=180,
            key=f"quote-{editor_version}-{index}",
        )
        if st.button("Remove response", key=f"remove-{editor_version}-{index}"):
            remove_index = index
if remove_index is not None:
    payload["quotes"].pop(remove_index)
    advance_editor_version(st.session_state)
    st.rerun()
if st.button("Add blank response"):
    payload["quotes"].append({"source_id": f"supplier-{len(payload['quotes']) + 1}", "text": ""})
    advance_editor_version(st.session_state)
    st.rerun()

with st.expander("Optional cost assumptions"):
    st.caption(
        'Assumptions are never inferred. JSON examples: FX {"EUR_USD": 1.1}; '
        'freight {"pacific-rim": {"amount": 12000, "currency": "USD"}}.'
    )
    target_currency = st.text_input(
        "Target currency", value="", key=f"target-currency-{editor_version}"
    )
    fx_json = st.text_area("FX rates", value="{}", height=80, key=f"fx-rates-{editor_version}")
    freight_json = st.text_area(
        "Annual freight by source",
        value="{}",
        height=80,
        key=f"freight-{editor_version}",
    )
    duty_json = st.text_area(
        "Duty rates by source", value="{}", height=80, key=f"duty-{editor_version}"
    )

if st.button("Analyze quotes", type="primary", use_container_width=True):
    try:
        assumptions = {
            "target_currency": target_currency or None,
            "fx_rates": json.loads(fx_json),
            "freight_by_source": json.loads(freight_json),
            "duty_rate_by_source": json.loads(duty_json),
        }
        request_payload = prepare_analysis_payload(payload, assumptions)
        st.session_state.analysis_request = request_payload
        st.session_state.analysis = api_post("/v1/analyses", request_payload)
        if st.session_state.get("pending_documents"):
            processed = api_post(
                "/v1/supplier-documents/ingest",
                {"documents": st.session_state.pending_documents},
            )
            st.session_state.document_results = processed["documents"]
            st.session_state.pending_documents = []
            st.session_state.analysis = api_post("/v1/analyses", request_payload)
    except (json.JSONDecodeError, httpx.HTTPError, RuntimeError, ValueError) as error:
        st.error(f"Analysis failed: {error}")

if "analysis" in st.session_state:
    render_analysis(st.session_state.analysis)
