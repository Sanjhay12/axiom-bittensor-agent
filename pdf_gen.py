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
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_JUSTIFY

LOGO_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "logo.png"))

CEDAR_BLUE = HexColor("#2B4C8C")
DISCLAIMER_GREY = HexColor("#888888")
PAGE_W, PAGE_H = A4
MARGIN = 2.2 * cm


def _styles():
    header_meta = ParagraphStyle(
        "HeaderMeta",
        fontName="Times-Roman",
        fontSize=9,
        textColor=CEDAR_BLUE,
        leading=12,
    )
    title = ParagraphStyle(
        "MemoTitle",
        fontName="Times-Bold",
        fontSize=13,
        textColor=CEDAR_BLUE,
        leading=18,
        spaceAfter=10,
    )
    body = ParagraphStyle(
        "Body",
        fontName="Times-Roman",
        fontSize=10,
        textColor=CEDAR_BLUE,
        leading=14,
        alignment=TA_JUSTIFY,
        firstLineIndent=18,
        spaceAfter=6,
    )
    body_list = ParagraphStyle(
        "BodyList",
        fontName="Times-Roman",
        fontSize=10,
        textColor=CEDAR_BLUE,
        leading=14,
        alignment=TA_JUSTIFY,
        leftIndent=18,
        spaceAfter=3,
    )
    footer_style = ParagraphStyle(
        "Footer",
        fontName="Times-Roman",
        fontSize=9,
        textColor=CEDAR_BLUE,
        alignment=TA_CENTER,
    )
    confidential = ParagraphStyle(
        "Confidential",
        fontName="Times-Roman",
        fontSize=9,
        textColor=CEDAR_BLUE,
        alignment=TA_RIGHT,
    )
    disclaimer = ParagraphStyle(
        "Disclaimer",
        fontName="Times-Roman",
        fontSize=7.5,
        textColor=DISCLAIMER_GREY,
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


def _parse_memo(memo_text: str) -> list[tuple[str, str]]:
    section_pattern = re.compile(
        r'^(OVERVIEW|CORE INSIGHT|HOW IT WORKS|NETWORK HEALTH|VALIDATOR LANDSCAPE|'
        r'MINER LANDSCAPE|EMISSION & ECONOMICS|DEVELOPMENT ACTIVITY|'
        r'RISK FACTORS|KEY RISKS|INVESTMENT VIEW|VERDICT|COMPARISON|TOKENOMICS):?\s*$',
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


def _add_section(story, header: str, body: str, styles: dict):
    """Render a section matching the Cedar Ridge style: Bold Header - first sentence..."""
    label = f'<b>{_esc(header.title())}</b>'
    lines = [l.strip() for l in body.split("\n") if l.strip()]

    if not lines:
        story.append(Paragraph(label, styles["body"]))
        return

    # Check if content is a list (lines starting with - or numbers)
    is_list = all(re.match(r'^[-•\d]', l) for l in lines)

    if is_list:
        # Render header as standalone bold line then list items below
        story.append(Paragraph(label, styles["body"]))
        for line in lines:
            clean = re.sub(r'^[-•]\s*', '', line)
            story.append(Paragraph(_esc(clean), styles["body_list"]))
    else:
        # Inline header with first paragraph (Cedar Ridge style)
        first = _esc(lines[0])
        story.append(Paragraph(f"{label} - {first}", styles["body"]))
        for line in lines[1:]:
            story.append(Paragraph(_esc(line), styles["body"]))


def generate_pdf(
    subnet_id: int,
    subnet_name: str,
    memo_text: str,
    date_str: str = None,
) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN + 0.5 * cm,
        title=f"SN{subnet_id} Research Memo",
        author="Cedar Ridge Capital",
    )

    styles = _styles()
    date_str = date_str or datetime.now().strftime("%B %Y")
    story = []

    # ── Logo ──
    if os.path.exists(LOGO_PATH):
        logo = Image(LOGO_PATH, width=1.8 * cm, height=1.8 * cm)
        story.append(logo)
        story.append(Spacer(1, 4))

    # ── Header row ──
    col = (PAGE_W - 2 * MARGIN) / 2
    header_table = Table(
        [[
            Paragraph(date_str, styles["header_meta"]),
            Paragraph("Private &amp; Confidential", styles["confidential"]),
        ]],
        colWidths=[col, col],
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=1, color=CEDAR_BLUE, spaceAfter=10))

    # ── Title ──
    story.append(Paragraph(f"Re: SN{subnet_id} — {subnet_name}", styles["title"]))
    story.append(Spacer(1, 4))

    # ── Body ──
    for header, body in _parse_memo(memo_text):
        if header:
            _add_section(story, header, body, styles)
        else:
            for line in body.split("\n"):
                line = line.strip()
                if line:
                    story.append(Paragraph(_esc(line), styles["body"]))

    # ── Footer ──
    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="100%", thickness=0.5, color=CEDAR_BLUE, spaceAfter=6))
    story.append(Paragraph("Cedar Ridge Capital", styles["footer"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "This document is for informational purposes only and does not constitute financial advice, "
        "an offer to buy or sell any security, or a solicitation of any investment. "
        "Past performance is not indicative of future results. Cedar Ridge Capital makes no representation "
        "as to the accuracy or completeness of the information contained herein. "
        "Recipients should conduct their own due diligence before making any investment decisions.",
        styles["disclaimer"],
    ))

    doc.build(story)
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
        bottomMargin=MARGIN + 0.5 * cm,
        title="Bittensor Subnet Watchlist",
        author="Cedar Ridge Capital",
    )

    styles = _styles()
    date_str = date_str or datetime.now().strftime("%B %Y")
    story = []

    # ── Logo ──
    if os.path.exists(LOGO_PATH):
        story.append(Image(LOGO_PATH, width=1.8 * cm, height=1.8 * cm))
        story.append(Spacer(1, 4))

    # ── Header ──
    col = (PAGE_W - 2 * MARGIN) / 2
    header_table = Table(
        [[
            Paragraph(date_str, styles["header_meta"]),
            Paragraph("Private &amp; Confidential", styles["confidential"]),
        ]],
        colWidths=[col, col],
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=1, color=CEDAR_BLUE, spaceAfter=10))

    # ── Watchlist title ──
    story.append(Paragraph("Re: Bittensor Subnet Watchlist", styles["title"]))
    story.append(Spacer(1, 4))

    # ── 5 picks ──
    story.append(Paragraph("<b>This Week's Picks</b>", styles["body"]))
    story.append(Spacer(1, 4))

    for i, pick in enumerate(picks, 1):
        netuid = pick["netuid"]
        name = pick.get("name") or f"Subnet {netuid}"
        reason = pick.get("reason", "")
        is_chosen = netuid == deep_netuid
        star = " ★ Deep Dive" if is_chosen else ""
        story.append(Paragraph(
            f'<b>{i}. SN{netuid} — {name}{star}</b>',
            styles["body"],
        ))
        if reason:
            story.append(Paragraph(reason, styles["body_list"]))
        story.append(Spacer(1, 4))

    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=0.5, color=CEDAR_BLUE, spaceAfter=10))

    # ── Deep dive ──
    story.append(Paragraph(
        f"Deep Dive: SN{deep_netuid} — {deep_name}",
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

    # ── Footer ──
    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="100%", thickness=0.5, color=CEDAR_BLUE, spaceAfter=6))
    story.append(Paragraph("Cedar Ridge Capital", styles["footer"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "This document is for informational purposes only and does not constitute financial advice, "
        "an offer to buy or sell any security, or a solicitation of any investment. "
        "Past performance is not indicative of future results. Cedar Ridge Capital makes no representation "
        "as to the accuracy or completeness of the information contained herein. "
        "Recipients should conduct their own due diligence before making any investment decisions.",
        styles["disclaimer"],
    ))

    doc.build(story)
    return buf.getvalue()
