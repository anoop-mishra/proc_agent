from email import policy
from email.parser import Parser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any


def advance_editor_version(state: Any) -> int:
    """Force Streamlit to recreate widgets after replacing or reordering quotes."""
    next_version = int(state.get("editor_version", 0)) + 1
    state["editor_version"] = next_version
    return next_version


def prepare_analysis_payload(
    payload: dict[str, Any],
    assumptions: dict[str, Any],
) -> dict[str, Any]:
    """Remove unused UI placeholders and validate inputs before calling the API."""
    quotes = [
        {
            "source_id": quote.get("source_id", "").strip(),
            "text": quote.get("text", "").strip(),
        }
        for quote in payload.get("quotes", [])
        if quote.get("text", "").strip()
    ]
    if not quotes:
        raise ValueError("Add or upload at least one supplier response before analyzing.")
    if any(not quote["source_id"] for quote in quotes):
        raise ValueError("Every non-empty supplier response needs a source ID.")

    source_ids = [quote["source_id"] for quote in quotes]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("Supplier source IDs must be unique.")

    rfq = dict(payload.get("rfq", {}))
    rfq["part_number"] = rfq.get("part_number", "").strip()
    if not rfq["part_number"]:
        raise ValueError("Enter the RFQ part number before analyzing.")

    return {
        "rfq": rfq,
        "quotes": quotes,
        "assumptions": assumptions,
    }


def prepare_incremental_communication(
    raw_text: str,
    *,
    default_supplier: str = "Unknown supplier",
    mailbox_account_id: str | None = None,
) -> dict[str, Any]:
    """Parse email-style headers while preserving the exact uploaded source."""
    if not raw_text.strip():
        raise ValueError("Incremental update file is empty.")
    message = Parser(policy=policy.default).parsestr(raw_text)
    supplier = (message.get("Supplier") or default_supplier).strip()
    sender = (message.get("From") or f"supplier:{supplier}").strip()
    recipients = [address for _, address in getaddresses(message.get_all("To", [])) if address]
    body = message.get_payload()
    if not isinstance(body, str):
        body = raw_text
    sent_at = None
    if message.get("Date"):
        try:
            sent_at = parsedate_to_datetime(message["Date"]).isoformat()
        except (TypeError, ValueError):
            sent_at = None
    return {
        "channel": "EMAIL",
        "mailbox_account_id": mailbox_account_id,
        "sender": sender,
        "recipients": recipients,
        "subject": message.get("Subject"),
        "sent_at": sent_at,
        "raw_message": raw_text,
        "raw_body": body.strip(),
        "supplier_label": supplier,
    }
