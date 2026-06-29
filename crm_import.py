"""
Bulk contact import — email the agent an Excel (.xlsx) or CSV contact list as an
attachment and it gets parsed and merged into the same crm_people/crm_firms tables
as everything else. Column names are matched flexibly (case-insensitive, common
aliases) rather than requiring an exact template.
"""
from __future__ import annotations
import csv
import io
import logging
import time

import crm_store

logger = logging.getLogger(__name__)

SPREADSHEET_EXTENSIONS = (".xlsx", ".csv")

COLUMN_ALIASES = {
    "name": ["name", "full name", "contact name", "contact"],
    "email": ["email", "e-mail", "email address"],
    "phone": ["phone", "phone number", "mobile", "cell", "telephone"],
    "firm_name": ["firm", "company", "organization", "organisation", "fund"],
    "role": ["role", "title", "position", "job title"],
    "relationship_type": ["relationship type", "relationship", "type", "category"],
    "mandate": ["mandate", "deal", "fund mandate"],
    "stage": ["stage", "status", "pipeline stage"],
    "next_step": ["next step", "next steps", "action", "follow up"],
    "notes": ["notes", "note", "comments", "comment"],
    "deal_amount_usd": ["deal amount", "amount", "deal size", "investment amount", "allocation"],
    "contact_channel": ["channel", "contact channel", "how connected", "connection method"],
}


def _normalize_headers(headers: list[str]) -> dict[int, str]:
    """Maps column index -> our internal field name, for whichever columns we recognize."""
    mapping = {}
    for idx, header in enumerate(headers):
        h = (header or "").strip().lower()
        for field, aliases in COLUMN_ALIASES.items():
            if h in aliases:
                mapping[idx] = field
                break
    return mapping


def _parse_amount(raw) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).replace("$", "").replace(",", "").strip()
    try:
        if text.lower().endswith("m"):
            return float(text[:-1]) * 1_000_000
        if text.lower().endswith("k"):
            return float(text[:-1]) * 1_000
        return float(text)
    except ValueError:
        return None


def _rows_from_csv(content: bytes) -> tuple[list[str], list[list]]:
    text = content.decode("utf-8", errors="ignore")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return [], []
    return rows[0], rows[1:]


def _rows_from_xlsx(content: bytes) -> tuple[list[str], list[list]]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    sheet = wb.active
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return [], []
    headers = [str(h) if h is not None else "" for h in rows[0]]
    return headers, [list(r) for r in rows[1:]]


def import_contacts(content: bytes, filename: str, on_new_person=None) -> dict:
    """Returns {"added": int, "updated": int, "skipped": int, "total": int}.

    on_new_person, if given, is called as on_new_person(person_id, extracted) for every
    brand-new contact (not updates to existing ones) — used to trigger enrichment the
    same way the email-ingestion path does, without this module needing to know about
    asyncio or crm_enrich directly."""
    filename_lower = filename.lower()
    if filename_lower.endswith(".csv"):
        headers, rows = _rows_from_csv(content)
    elif filename_lower.endswith(".xlsx"):
        headers, rows = _rows_from_xlsx(content)
    else:
        raise ValueError(f"Unsupported spreadsheet format: {filename}")

    col_map = _normalize_headers(headers)
    if "email" not in col_map.values() and "name" not in col_map.values():
        raise ValueError("Couldn't find a recognizable Name or Email column in the header row")

    added = updated = skipped = 0
    now = int(time.time())

    for row in rows:
        if not any(row):
            continue
        record = {}
        for idx, field in col_map.items():
            if idx < len(row):
                record[field] = row[idx]

        email = (str(record.get("email") or "")).strip().lower()
        name = (str(record.get("name") or "")).strip()
        if not email and not name:
            skipped += 1
            continue

        extracted = {
            "person_name": name or None,
            "person_email": email or None,
            "phone": str(record["phone"]).strip() if record.get("phone") else None,
            "role": str(record["role"]).strip() if record.get("role") else None,
            "relationship_type": str(record["relationship_type"]).strip().lower() if record.get("relationship_type") else None,
            "mandate": str(record["mandate"]).strip() if record.get("mandate") else None,
            "stage": str(record["stage"]).strip() if record.get("stage") else None,
            "next_step": str(record["next_step"]).strip() if record.get("next_step") else None,
            "notes": str(record["notes"]).strip() if record.get("notes") else None,
            "deal_amount_usd": _parse_amount(record.get("deal_amount_usd")),
            "contact_channel": str(record["contact_channel"]).strip().lower() if record.get("contact_channel") else None,
            "sentiment": None,
            "importance": None,
        }

        firm_name = str(record["firm_name"]).strip() if record.get("firm_name") else None
        extracted["firm_name"] = firm_name
        firm_id = crm_store.get_or_create_firm(firm_name)
        person_id, is_new = crm_store.upsert_person(extracted, firm_id, now, source="import")
        if is_new:
            added += 1
            if on_new_person:
                on_new_person(person_id, extracted)
        else:
            updated += 1

    return {"added": added, "updated": updated, "skipped": skipped, "total": added + updated + skipped}
