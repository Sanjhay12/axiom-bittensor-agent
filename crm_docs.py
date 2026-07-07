"""
Product/deal documents (fact sheets, pitch decks, offering memos) sent as PDF
attachments. A PDF is always about one contact's one opportunity — not a standalone
product record — so this identifies which contact and which product it's for (from
the filename, any note in the email, and the document itself) and writes a notes
summary into that specific crm_opportunities row. If the contact or product can't be
confidently determined, it says so instead of guessing and writing to the wrong record.
"""
from __future__ import annotations
import io
import json
import logging
import os

from anthropic import AsyncAnthropic
from pypdf import PdfReader

logger = logging.getLogger(__name__)

claude = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-5"

DOC_PROMPT = """You're reading a product/deal document (fact sheet, pitch deck, offering memo, etc.) \
emailed to a private fundraising CRM agent as an attachment, along with the sender's note (if any) \
and the filename. This document is about ONE contact's ONE specific opportunity — not a general \
product record — so identify which contact it's for and which product/deal it covers, using the \
note, filename, and document content together.

Return ONLY valid JSON:
{
  "contact": "string or null — the name or email of the contact this applies to",
  "product": "string or null — the fund/deal/product name",
  "category": "string or null — if this is a credit/debt investment, the specific strategy type (e.g. direct lending, distressed debt, mezzanine, senior secured, unitranche, special situations, structured credit, high yield, leveraged loans, venture debt); null if it isn't a credit product or the type isn't clear from the document",
  "notes": "string or null — a concise summary of the document's key points relevant to a CRM opportunity note: terms, strategy, fees, key facts. Keep it tight, not a full restatement."
}

If you cannot confidently determine BOTH the contact and the product, set both to null — never guess \
a contact or product you're not reasonably sure of; writing a note to the wrong contact is worse than \
asking.
"""


async def extract_product_note(pdf_content: bytes, filename: str, email_note: str) -> dict:
    """Returns {"contact", "product", "notes"} — any of which may be None if not determinable."""
    try:
        reader = PdfReader(io.BytesIO(pdf_content))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:15])
    except Exception as e:
        logger.error(f"crm_docs: failed to read PDF {filename}: {e}")
        return {"contact": None, "product": None, "notes": None, "error": str(e)}

    content = (
        f"Filename: {filename}\nSender's note: {email_note or '(none)'}\n\n"
        f"Document text:\n{text[:12000]}"
    )

    try:
        resp = await claude.messages.create(
            model=MODEL, max_tokens=500, system=DOC_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:
        logger.error(f"crm_docs: extraction failed for {filename}: {e}")
        return {"contact": None, "product": None, "notes": None, "error": str(e)}

    text_out = resp.content[0].text.strip().strip("`")
    if text_out.lower().startswith("json"):
        text_out = text_out[4:]
    try:
        return json.loads(text_out)
    except json.JSONDecodeError:
        return {"contact": None, "product": None, "notes": None}
