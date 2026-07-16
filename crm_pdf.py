"""
Cedar Ridge letterhead PDF for CRM briefs (contact, firm, or product) — same visual
template pdf_gen.py already uses for Axiom research memos (logo, date, "Private &
Confidential" mark, HR divider, footer rule), reused here rather than duplicated,
just with brief-appropriate content and a "Cedar Ridge Capital" title matching the
header text in Axiom Insurance Policy.pdf.
"""
from __future__ import annotations
import io
import re
from datetime import datetime, timezone

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor, black
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_JUSTIFY
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.graphics.shapes import Drawing, String, Rect

from pdf_gen import _styles, _esc, _build_header, MARGIN, PAGE_W, GREY

# Claude is told to use <b> tags for section labels but doesn't always comply —
# markdown **bold** shows up often enough in practice that both need handling.
_SECTION_RE = re.compile(r'(?:<b>\s*\d+\.\s*(.+?)\s*</b>|\*\*\s*\d+\.\s*(.+?)\s*\*\*)', re.IGNORECASE)


def _parse_brief(brief_text: str) -> list[tuple[str | None, str]]:
    """Splits '<b>1. Section Name</b>\\ncontent...' (or '**1. Section Name**') blocks
    (crm_brief's output format) into (section_title, body) pairs."""
    matches = list(_SECTION_RE.finditer(brief_text))
    if not matches:
        return [(None, brief_text.strip())] if brief_text.strip() else []

    sections = []
    if brief_text[:matches[0].start()].strip():
        sections.append((None, brief_text[:matches[0].start()].strip()))

    for i, m in enumerate(matches):
        title = (m.group(1) or m.group(2)).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(brief_text)
        sections.append((title, brief_text[body_start:body_end].strip()))
    return sections


def _draw_brief_footer(canvas, doc):
    canvas.saveState()
    w = PAGE_W - 2 * MARGIN
    style = ParagraphStyle(
        "BriefFooter", fontName="Times-Roman", fontSize=7.5,
        textColor=GREY, alignment=TA_CENTER, leading=10,
    )
    p = Paragraph("Cedar Ridge Capital &mdash; Private &amp; Confidential &mdash; Internal use only", style)
    _, h = p.wrap(w, 1 * cm)
    y = 0.7 * cm
    canvas.setStrokeColor(GREY)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, y + h + 4, MARGIN + w, y + h + 4)
    p.drawOn(canvas, MARGIN, y)
    canvas.restoreState()


def generate_brief_pdf(title: str, subtitle: str | None, brief_text: str, date_str: str | None = None) -> bytes:
    """title: contact or firm name. subtitle: firm name (for a contact brief) or None."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=MARGIN + 1.2 * cm,
        title=f"{title} — Brief", author="Cedar Ridge Capital",
    )
    styles = _styles()
    date_str = date_str or datetime.now().strftime("%B %Y")
    story = []

    _build_header(story, styles, date_str, show_logo=False)

    story.append(Paragraph("Cedar Ridge Capital", styles["title"]))
    story.append(Spacer(1, 2))
    re_line = f"Re: {_esc(title)}" + (f" &mdash; {_esc(subtitle)}" if subtitle else "")
    story.append(Paragraph(re_line, styles["header_meta"]))
    story.append(Spacer(1, 6))

    for section_title, body in _parse_brief(brief_text):
        if section_title:
            story.append(Paragraph(f"<b>{_esc(section_title)}</b>", styles["body"]))
        body_style = styles["body_list"] if section_title else styles["body"]
        for line in body.split("\n"):
            line = line.strip()
            if line:
                story.append(Paragraph(_esc(line), body_style))
        story.append(Spacer(1, 4))

    doc.build(story, onFirstPage=_draw_brief_footer, onLaterPages=_draw_brief_footer)
    return buf.getvalue()


def _fmt_usd(amount: float | None) -> str:
    if not amount:
        return "$0"
    if amount >= 1_000_000:
        return f"${amount/1_000_000:.1f}M"
    if amount >= 1_000:
        return f"${amount/1_000:.0f}K"
    return f"${amount:,.0f}"


def _make_bar_chart(data: list[tuple[str, float]], title: str, width: float = 240, height: float = 150) -> Drawing | None:
    """Simple vertical bar chart as a reportlab Drawing — same hand-rolled-shapes
    approach as pdf_gen._make_sparkline, so no new charting dependency is needed."""
    data = [(label, val) for label, val in data if val is not None]
    if not data:
        return None

    drawing = Drawing(width, height)
    pad_left, pad_right, pad_top, pad_bottom = 10, 10, 22, 34
    chart_w = width - pad_left - pad_right
    chart_h = height - pad_top - pad_bottom

    drawing.add(Rect(0, 0, width, height, fillColor=HexColor("#F8F8F8"), strokeColor=None))
    drawing.add(String(width / 2, height - 14, title, textAnchor="middle",
                        fontSize=7.5, fontName="Times-Roman", fillColor=HexColor("#333333")))

    max_val = max(v for _, v in data) or 1
    n = len(data)
    slot_w = chart_w / n
    bar_w = min(slot_w * 0.6, 36)

    for i, (label, val) in enumerate(data):
        bar_h = (val / max_val) * chart_h
        x = pad_left + i * slot_w + (slot_w - bar_w) / 2
        y = pad_bottom
        drawing.add(Rect(x, y, bar_w, bar_h, fillColor=HexColor("#1A1A1A"), strokeColor=None))
        drawing.add(String(x + bar_w / 2, y + bar_h + 3, f"{val:,.0f}", textAnchor="middle",
                            fontSize=7, fontName="Times-Roman", fillColor=HexColor("#1A1A1A")))
        label_text = label if len(label) <= 14 else label[:12] + "…"
        drawing.add(String(x + bar_w / 2, pad_bottom - 12, label_text, textAnchor="middle",
                            fontSize=6.5, fontName="Times-Roman", fillColor=GREY))

    return drawing


def _side_by_side(drawings: list, styles: dict) -> Table:
    chart_w = (PAGE_W - 2 * MARGIN) / 2 - 6
    cells = list(drawings[:2]) + [""] * (2 - len(drawings[:2]))
    t = Table([cells], colWidths=[chart_w + 6, chart_w + 6])
    t.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t


def _make_table(rows: list[list], col_widths: list[float], styles: dict, header: bool = True) -> Table:
    header_style = ParagraphStyle("TblHeader", fontName="Times-Bold", fontSize=8.5, textColor=black, leading=11)
    cell_style = ParagraphStyle("TblCell", fontName="Times-Roman", fontSize=8.5, textColor=black, leading=11)
    data = []
    for i, row in enumerate(rows):
        style = header_style if (header and i == 0) else cell_style
        data.append([Paragraph(_esc(str(c)), style) for c in row])
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, 0 if header else -1), 0.75 if header else 0.25, GREY),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, HexColor("#EEEEEE")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t


_STATUS_SECTION_RE = re.compile(r'^\s*<b>\s*(.+?)\s*</b>\s*$')


def _render_narrative(story, text: str, styles: dict):
    """Renders crm_status.narrative output: lines wrapped in <b>..</b> become bold
    section headers, '-' lines become bullets, everything else flows as body. Uses a
    compact 9pt style (vs the 10pt brief body) so the where-we-are / feedback / next
    prose keeps the whole report on one page."""
    head = ParagraphStyle("NarrHead", fontName="Times-Bold", fontSize=9.5, textColor=black,
                          leading=12, spaceBefore=4, spaceAfter=1)
    body = ParagraphStyle("NarrBody", fontName="Times-Roman", fontSize=9, textColor=black,
                          leading=11.5, alignment=TA_JUSTIFY, spaceAfter=2)
    bullet_style = ParagraphStyle("NarrBullet", parent=body, leftIndent=10, spaceAfter=1)
    for raw in (text or "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = _STATUS_SECTION_RE.match(line)
        if m:
            story.append(Paragraph(f"<b>{_esc(m.group(1))}</b>", head))
            continue
        bullet = line.startswith("-")
        content = line[1:].strip() if bullet else line
        story.append(Paragraph(("&bull; " if bullet else "") + _esc(content),
                               bullet_style if bullet else body))


def _nonzero_stage_data(counts: dict, order: list, exclude: tuple = ()) -> list[tuple[str, float]]:
    return [(s, counts[s]) for s in order if s in counts and counts[s] and s not in exclude]


# Contact/deal pipeline order for the status one-pager. New is cold top-of-funnel
# (bulk import) and is reported as a headline metric, not plotted, so it doesn't
# dwarf every worked stage.
_STATUS_STAGE_ORDER = [
    "New", "Contacted", "Engaged", "Intro made", "Materials sent",
    "Call scheduled", "Diligence", "Soft circled", "Committed", "Passed", "Dormant",
]


def _kpi_strip(kpis: list[tuple[str, str]]) -> Table:
    """One row of value-over-label cells — a compact executive metric strip."""
    val_style = ParagraphStyle("KpiVal", fontName="Times-Bold", fontSize=15, textColor=black,
                               leading=17, alignment=TA_CENTER)
    lbl_style = ParagraphStyle("KpiLbl", fontName="Times-Roman", fontSize=7.5, textColor=GREY,
                               leading=9, alignment=TA_CENTER)
    cells = [[Paragraph(_esc(v), val_style), Paragraph(_esc(l), lbl_style)] for v, l in kpis]
    col_w = (PAGE_W - 2 * MARGIN) / len(kpis)
    # Each KPI is its own 2-row inner table so value stacks above label; outer table lays them across.
    inner = [Table([[c[0]], [c[1]]], colWidths=[col_w]) for c in cells]
    for t in inner:
        t.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ]))
    outer = Table([inner], colWidths=[col_w] * len(kpis))
    outer.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.75, GREY),
        ("LINEABOVE", (0, 0), (-1, -1), 0.75, GREY),
    ]))
    return outer


def generate_status_pdf(data: dict, narrative_text: str, date_str: str | None = None) -> bytes:
    """Capital-raise status one-pager (crm_status.generate output) on Cedar Ridge
    letterhead: overview strip, pipeline funnel + opportunity-stage charts, top
    prospects table, and the synthesized where-we-are / feedback / next-steps prose."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=MARGIN + 1.2 * cm,
        title="Cedar Ridge Capital Raise — Status Report", author="Cedar Ridge Capital",
    )
    styles = _styles()
    date_str = date_str or datetime.now().strftime("%B %Y")
    w = PAGE_W - 2 * MARGIN
    story = []

    _build_header(story, styles, date_str, show_logo=False)
    story.append(Paragraph("Cedar Ridge Capital", styles["title"]))
    story.append(Spacer(1, 2))
    story.append(Paragraph("Capital Raise &mdash; Status Report", styles["header_meta"]))
    story.append(Spacer(1, 10))

    # Overview: a compact horizontal KPI strip (one row of value-over-label cells)
    # rather than a tall table, to keep the whole report on a single page.
    pipeline_usd = data.get("pipeline_total_usd") or 0
    kpis = [
        (f"{data.get('active_count', 0):,}", "Active relationships"),
        (f"{data.get('new_count', 0):,}", "Sourced (not worked)"),
        (f"{data.get('pipeline_count', 0):,}", "Active opportunities"),
        (f"{data.get('total_interactions', 0):,}", f"Interactions ({data.get('days', 30)}d)"),
        (_fmt_usd(pipeline_usd) if pipeline_usd else "—", "Logged pipeline $"),
    ]
    story.append(_kpi_strip(kpis))
    story.append(Spacer(1, 8))

    # Charts: contacts by stage + active opportunities by stage
    funnel = data.get("funnel") or []
    opp_data = _nonzero_stage_data(
        data.get("opp_stage_counts", {}), _STATUS_STAGE_ORDER, exclude=("New", "Passed", "Dormant"),
    )
    contacts_chart = _make_bar_chart(funnel, "Contacts by Pipeline Stage", height=125)
    opps_chart = _make_bar_chart(opp_data, "Active Opportunities by Stage", height=125)
    charts = [c for c in (contacts_chart, opps_chart) if c]
    if charts:
        story.append(Paragraph("<b>Pipeline</b>", styles["body"]))
        story.append(Spacer(1, 2))
        story.append(_side_by_side(charts, styles))
        story.append(Spacer(1, 6))

    # Top prospects (cap the rows shown so the report stays one page; label the shown count)
    prospects = (data.get("top_prospects") or [])[:5]
    story.append(Paragraph(f"<b>Top Prospects ({len(prospects)})</b>", styles["body"]))
    story.append(Spacer(1, 2))
    if prospects:
        rows = [["Prospect", "Firm", "Stage", "Score", "Next Step"]]
        for p in prospects:
            score = p.get("composite_score")
            rows.append([
                p.get("name") or p.get("email") or "—",
                p.get("firm_name") or "—",
                p.get("stage") or "New",
                f"{score:.0f}" if score else "—",
                (p.get("next_step_display") or p.get("next_step") or "—")[:48],
            ])
        story.append(_make_table(rows, [w * 0.22, w * 0.24, w * 0.14, w * 0.08, w * 0.32], styles))
    else:
        story.append(Paragraph("No worked prospects on file yet.", styles["body_list"]))
    story.append(Spacer(1, 6))

    # Narrative: where we are / feedback / next steps
    _render_narrative(story, narrative_text, styles)

    doc.build(story, onFirstPage=_draw_brief_footer, onLaterPages=_draw_brief_footer)
    return buf.getvalue()


def generate_dashboard_pdf(data: dict, date_str: str | None = None) -> bytes:
    """data: the dict returned by crm_dashboard.gather()."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=MARGIN + 1.2 * cm,
        title="Cedar Ridge CRM Activity Report", author="Cedar Ridge Capital",
    )
    styles = _styles()
    date_str = date_str or datetime.now().strftime("%B %Y")
    story = []
    days = data.get("days", 30)

    _build_header(story, styles, date_str, show_logo=False)
    story.append(Paragraph("Cedar Ridge Capital", styles["title"]))
    story.append(Spacer(1, 2))
    story.append(Paragraph(f"CRM Activity Report &mdash; trailing {days} days", styles["header_meta"]))
    story.append(Spacer(1, 10))

    # Overview
    story.append(Paragraph("<b>Overview</b>", styles["body"]))
    story.append(Spacer(1, 4))
    overview_rows = [
        ["Metric", "Value"],
        ["Total contacts", f"{data.get('total_contacts', 0):,}"],
        ["Total firms", f"{data.get('total_firms', 0):,}"],
        [f"Interactions (last {days}d)", f"{data.get('total_interactions', 0):,}"],
        ["Active opportunities", f"{data.get('pipeline_count', 0):,}"],
        ["Active pipeline value", _fmt_usd(data.get("pipeline_total_usd"))],
    ]
    story.append(_make_table(overview_rows, [(PAGE_W - 2 * MARGIN) * 0.6, (PAGE_W - 2 * MARGIN) * 0.4], styles))
    story.append(Spacer(1, 10))

    # Activity charts: direction + sentiment
    direction_chart = _make_bar_chart(
        list(data.get("direction_counts", {}).items()), "Interactions by Direction"
    )
    sentiment_chart = _make_bar_chart(
        list(data.get("sentiment_counts", {}).items()), "Interactions by Sentiment"
    )
    charts = [c for c in (direction_chart, sentiment_chart) if c]
    if charts:
        story.append(Paragraph("<b>Activity</b>", styles["body"]))
        story.append(Spacer(1, 4))
        story.append(_side_by_side(charts, styles))
        story.append(Spacer(1, 8))

    # Meetings & calls
    meetings = data.get("meetings") or []
    story.append(Paragraph(f"<b>Meetings &amp; Calls ({len(meetings)})</b>", styles["body"]))
    story.append(Spacer(1, 4))
    if meetings:
        rows = [["Date", "Contact", "Firm", "Subject"]]
        for m in meetings[:20]:
            dt = datetime.fromtimestamp(m["ts"], timezone.utc).strftime("%b %d")
            rows.append([dt, m.get("name") or m.get("email") or "unknown", m.get("firm_name") or "—", m.get("subject") or "—"])
        w = PAGE_W - 2 * MARGIN
        story.append(_make_table(rows, [w * 0.12, w * 0.28, w * 0.25, w * 0.35], styles))
    else:
        story.append(Paragraph("No calls, video calls, or in-person meetings logged in this window.", styles["body_list"]))
    story.append(Spacer(1, 10))

    # Pipeline
    stage_order = ["New", "Contacted", "Engaged", "Intro made", "Materials sent", "Call scheduled",
                    "Diligence", "Soft circled", "Committed", "Passed", "Dormant"]
    stage_counts = data.get("stage_counts", {})
    stage_data = [(s, stage_counts[s]) for s in stage_order if s in stage_counts]
    pipeline_chart = _make_bar_chart(stage_data, "Contacts by Pipeline Stage", width=PAGE_W - 2 * MARGIN, height=160)
    story.append(Paragraph("<b>Pipeline</b>", styles["body"]))
    story.append(Spacer(1, 4))
    if pipeline_chart:
        story.append(pipeline_chart)
    story.append(Spacer(1, 8))

    # Stage progressions (approximate — see crm_dashboard module docstring)
    progressions = data.get("progressions") or []
    story.append(Paragraph(f"<b>Stage Activity ({len(progressions)})</b>", styles["body"]))
    story.append(Paragraph(
        "Contacts past New stage updated within this window &mdash; an approximation, "
        "since the CRM doesn't yet log a full history of stage transitions.",
        styles["body_list"],
    ))
    story.append(Spacer(1, 4))
    if progressions:
        rows = [["Contact", "Firm", "Stage", "Updated"]]
        for p in progressions[:20]:
            dt = datetime.fromtimestamp(p["updated_at"], timezone.utc).strftime("%b %d")
            rows.append([p.get("name") or p.get("email") or "unknown", p.get("firm_name") or "—", p.get("stage") or "—", dt])
        w = PAGE_W - 2 * MARGIN
        story.append(_make_table(rows, [w * 0.32, w * 0.28, w * 0.25, w * 0.15], styles))
    story.append(Spacer(1, 10))

    # Prospective transactions
    transactions = data.get("transactions") or []
    story.append(Paragraph(f"<b>Prospective Transactions ({len(transactions)})</b>", styles["body"]))
    story.append(Spacer(1, 4))
    if transactions:
        rows = [["Product", "Contact / Firm", "Stage", "Amount", "Next Step"]]
        for t in transactions[:20]:
            rows.append([
                t.get("product") or "—", t.get("contact_or_firm") or t.get("firm_name") or "—",
                t.get("stage") or "New", _fmt_usd(t.get("deal_amount_usd")),
                (t.get("next_step") or "—")[:60],
            ])
        w = PAGE_W - 2 * MARGIN
        story.append(_make_table(rows, [w * 0.22, w * 0.2, w * 0.13, w * 0.12, w * 0.33], styles))
    else:
        story.append(Paragraph("No active opportunities on file.", styles["body_list"]))

    doc.build(story, onFirstPage=_draw_brief_footer, onLaterPages=_draw_brief_footer)
    return buf.getvalue()
