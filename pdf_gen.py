import io
import os
import re
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor, black
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle, Image
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_JUSTIFY, TA_LEFT

LOGO_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "logo.png"))

ACCENT = HexColor("#1A1A1A")
GREY = HexColor("#888888")
PAGE_W, PAGE_H = A4
MARGIN = 2.2 * cm


def _styles():
    header_meta = ParagraphStyle(
        "HeaderMeta",
        fontName="Times-Roman",
        fontSize=9,
        textColor=ACCENT,
        leading=12,
    )
    title = ParagraphStyle(
        "MemoTitle",
        fontName="Times-Bold",
        fontSize=13,
        textColor=black,
        leading=18,
        spaceAfter=2,
    )

    body = ParagraphStyle(
        "Body",
        fontName="Times-Roman",
        fontSize=10,
        textColor=black,
        leading=14,
        alignment=TA_JUSTIFY,
        firstLineIndent=18,
        spaceAfter=6,
    )
    body_list = ParagraphStyle(
        "BodyList",
        fontName="Times-Roman",
        fontSize=10,
        textColor=black,
        leading=14,
        alignment=TA_JUSTIFY,
        leftIndent=18,
        spaceAfter=3,
    )
    footer_style = ParagraphStyle(
        "Footer",
        fontName="Times-Roman",
        fontSize=9,
        textColor=GREY,
        alignment=TA_CENTER,
    )
    confidential = ParagraphStyle(
        "Confidential",
        fontName="Times-Roman",
        fontSize=9,
        textColor=ACCENT,
        alignment=TA_RIGHT,
    )
    tagline = ParagraphStyle(
        "Tagline",
        fontName="Times-Roman",
        fontSize=10,
        textColor=GREY,
        leading=14,
        spaceAfter=8,
    )
    disclaimer = ParagraphStyle(
        "Disclaimer",
        fontName="Times-Roman",
        fontSize=7.5,
        textColor=GREY,
        alignment=TA_CENTER,
        leading=10,
        spaceBefore=6,
    )
    return {
        "header_meta": header_meta,
        "title": title,
        "body": body,
        "body_list": body_list,
        "footer": footer_style,
        "confidential": confidential,
        "disclaimer": disclaimer,
    }


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_SECTION_NAMES = (
    r'OVERVIEW|CORE INSIGHT|HOW IT WORKS|NETWORK HEALTH|COMPETITIVE LANDSCAPE|'
    r'VALIDATOR LANDSCAPE|MINER LANDSCAPE|EMISSIONS? (?:AND|&) ECONOMICS|'
    r'DEVELOPMENT ACTIVITY|RISK FACTORS|KEY RISKS|INVESTMENT VIEW|VERDICT|'
    r'RECOMMENDATIONS|COMPARISON|TOKENOMICS'
)

_SECTION_DISPLAY = {
    "EMISSION AND ECONOMICS": "Emission & Economics",
    "EMISSIONS AND ECONOMICS": "Emission & Economics",
    "EMISSION & ECONOMICS": "Emission & Economics",
    "EMISSIONS & ECONOMICS": "Emission & Economics",
}


def _parse_memo(memo_text: str) -> list[tuple[str, str]]:
    # Normalise "SECTION_NAME: content on same line" → "SECTION_NAME\ncontent"
    inline_pat = re.compile(
        r'^(' + _SECTION_NAMES + r'):\s+(.+)',
        re.IGNORECASE | re.MULTILINE,
    )
    memo_text = inline_pat.sub(r'\1\n\2', memo_text)

    section_pattern = re.compile(
        r'^(' + _SECTION_NAMES + r'):?\s*$',
        re.MULTILINE | re.IGNORECASE,
    )

    parts = section_pattern.split(memo_text)
    if len(parts) == 1:
        return [("", memo_text.strip())]

    sections = []
    if parts[0].strip():
        sections.append(("", parts[0].strip()))

    for i in range(1, len(parts), 2):
        header = parts[i].strip().upper()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        sections.append((header, body))

    return sections


def _add_network_health_table(story, header_display: str, body: str, styles: dict):
    story.append(Paragraph(f'<b>{_esc(header_display)}</b>', styles["body"]))
    story.append(Spacer(1, 4))

    rows = []
    for line in body.split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue
        label, _, value = line.partition(":")
        rows.append([label.strip(), value.strip()])

    if not rows:
        return

    col_w = (PAGE_W - 2 * MARGIN) / 2
    label_style = ParagraphStyle("NHLabel", fontName="Times-Bold", fontSize=9,
                                 textColor=ACCENT, leading=13)
    value_style = ParagraphStyle("NHValue", fontName="Times-Roman", fontSize=9,
                                 textColor=black, leading=13, alignment=TA_RIGHT)

    table_data = [
        [Paragraph(_esc(r[0]), label_style), Paragraph(_esc(r[1]), value_style)]
        for r in rows
    ]
    t = Table(table_data, colWidths=[col_w, col_w])
    t.setStyle(TableStyle([
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING",   (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, GREY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(t)
    story.append(Spacer(1, 8))


def _add_recommendations(story, body: str, styles: dict):
    story.append(Paragraph("<b>Recommendations</b>", styles["body"]))
    story.append(Spacer(1, 4))
    role_style = ParagraphStyle("RoleLabel", fontName="Times-Bold", fontSize=10,
                                textColor=black, leading=14)
    for line in body.split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue
        role, _, text = line.partition(":")
        story.append(Paragraph(
            f'<b>{_esc(role.strip())}</b> — {_esc(text.strip())}',
            styles["body"],
        ))
    story.append(Spacer(1, 4))


def _add_section(story, header: str, body: str, styles: dict):
    display = _SECTION_DISPLAY.get(header.upper(), header.title())

    if header.upper() == "NETWORK HEALTH":
        _add_network_health_table(story, display, body, styles)
        return

    if header.upper() == "RECOMMENDATIONS":
        _add_recommendations(story, body, styles)
        return

    label = f'<b>{_esc(display)}</b>'
    lines = [l.strip() for l in body.split("\n") if l.strip()]

    if not lines:
        story.append(Paragraph(label, styles["body"]))
        return

    is_list = all(re.match(r'^[-•\d]', l) for l in lines)

    if is_list:
        story.append(Paragraph(label, styles["body"]))
        for line in lines:
            clean = re.sub(r'^[-•]\s*', '', line)
            story.append(Paragraph(_esc(clean), styles["body_list"]))
    else:
        first = _esc(lines[0])
        story.append(Paragraph(f"{label} - {first}", styles["body"]))
        for line in lines[1:]:
            story.append(Paragraph(_esc(line), styles["body"]))


def _build_header(story, styles, date_str):
    """Logo top-left, date below logo, Private & Confidential top-right, then HR."""
    col = (PAGE_W - 2 * MARGIN) / 2

    logo_cell = ""
    if os.path.exists(LOGO_PATH):
        logo_cell = Image(LOGO_PATH, width=2.2 * cm, height=2.2 * cm)

    conf_para = Paragraph("Private &amp; Confidential", styles["confidential"])
    date_para = Paragraph(date_str, styles["header_meta"])

    # Row 1: logo | confidential
    # Row 2: date  | empty
    header_table = Table(
        [
            [logo_cell, conf_para],
            [date_para, ""],
        ],
        colWidths=[col, col],
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (0, 0), "TOP"),
        ("VALIGN", (1, 0), (1, 0), "TOP"),
        ("VALIGN", (0, 1), (-1, 1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    story.append(header_table)
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=1, color=black, spaceAfter=12))


_SOURCES = "Prepared from live on-chain and GitHub data  ·  Sources: Bittensor finney chain, GitHub, r/bittensor_"

_DISCLAIMER = (
    "For informational purposes only. Does not constitute financial advice, an offer to buy or sell any security, "
    "or a solicitation of any investment. Past performance is not indicative of future results. "
    "No representation is made as to accuracy or completeness. Conduct your own due diligence."
)


def _draw_page_footer(canvas, doc):
    canvas.saveState()
    w = PAGE_W - 2 * MARGIN
    style = ParagraphStyle(
        "FooterText",
        fontName="Times-Roman",
        fontSize=7.5,
        textColor=GREY,
        alignment=TA_CENTER,
        leading=10,
    )

    # Measure disclaimer height first so we can stack from bottom up
    p_disc = Paragraph(_DISCLAIMER, style)
    _, dh = p_disc.wrap(w, 2 * cm)

    p_src = Paragraph(_SOURCES, style)
    _, sh = p_src.wrap(w, 1 * cm)

    y_hr = 0.5 * cm + dh + sh + 6
    canvas.setStrokeColor(GREY)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, y_hr, MARGIN + w, y_hr)

    p_src.drawOn(canvas, MARGIN, y_hr - sh - 2)
    p_disc.drawOn(canvas, MARGIN, y_hr - sh - dh - 4)

    canvas.restoreState()


def generate_pdf(
    subnet_id: int,
    subnet_name: str,
    memo_text: str,
    tagline: str = None,
    date_str: str = None,
) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN + 1.5 * cm,
        title=f"SN{subnet_id} Research Memo",
        author="Axiom",
    )

    styles = _styles()
    date_str = date_str or datetime.now().strftime("%B %Y")
    story = []

    _build_header(story, styles, date_str)

    re_line = f"Re: SN{subnet_id} Research Memo — {_esc(subnet_name)}"
    if tagline:
        re_line += f" · {_esc(tagline)}"
    story.append(Paragraph(re_line, styles["title"]))
    story.append(Spacer(1, 6))

    for header, body in _parse_memo(memo_text):
        if header:
            _add_section(story, header, body, styles)
        else:
            for line in body.split("\n"):
                line = line.strip()
                if line:
                    story.append(Paragraph(_esc(line), styles["body"]))

    doc.build(story, onFirstPage=_draw_page_footer, onLaterPages=_draw_page_footer)
    return buf.getvalue()


def generate_watchlist_pdf(
    picks: list[dict],
    deep_netuid: int,
    deep_name: str,
    deep_memo: str,
    date_str: str = None,
) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN + 1.5 * cm,
        title="Bittensor Subnet Watchlist",
        author="Axiom",
    )

    styles = _styles()
    date_str = date_str or datetime.now().strftime("%B %Y")
    story = []

    _build_header(story, styles, date_str)

    story.append(Paragraph("Bittensor Subnet Watchlist", styles["title"]))
    story.append(Spacer(1, 4))

    story.append(Paragraph("<b>This Week's Picks</b>", styles["body"]))
    story.append(Spacer(1, 4))

    for i, pick in enumerate(picks, 1):
        netuid = pick["netuid"]
        name = _esc(pick.get("name") or f"Subnet {netuid}")
        reason = pick.get("reason", "")
        is_chosen = netuid == deep_netuid
        star = " ★ Deep Dive" if is_chosen else ""
        story.append(Paragraph(
            f'<b>{i}. SN{netuid} — {name}{star}</b>',
            styles["body"],
        ))
        if reason:
            story.append(Paragraph(_esc(reason), styles["body_list"]))
        story.append(Spacer(1, 4))

    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=0.5, color=GREY, spaceAfter=10))

    story.append(Paragraph(
        f"Deep Dive: SN{deep_netuid} — {_esc(deep_name)}",
        styles["title"],
    ))
    story.append(Spacer(1, 4))

    for header, body in _parse_memo(deep_memo):
        if header:
            _add_section(story, header, body, styles)
        else:
            for line in body.split("\n"):
                line = line.strip()
                if line:
                    story.append(Paragraph(_esc(line), styles["body"]))

    doc.build(story, onFirstPage=_draw_page_footer, onLaterPages=_draw_page_footer)
    return buf.getvalue()
