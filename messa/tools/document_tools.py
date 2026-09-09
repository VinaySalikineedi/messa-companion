"""Document subagent: generates professional contracts, executive reports, and PDFs.

Uses reportlab (pure Python, standard built-in fonts, no system libraries) rather than
a weasyprint/HTML->PDF pipeline, specifically so this keeps working unmodified on
HuggingFace Spaces' CPU-basic Docker image without needing apt-level font/cairo/pango
packages.

Features:
- Two-pass NumberedCanvas: running headers and dynamic 'Page X of Y' footers.
- Zero-Bracket Validation Gate: prevents generating drafts with leftover placeholders
  (e.g. '[Insert Date]', '<Client Name>', 'TBD').
- Specialized Contract Generator: legal hierarchy (1.1, 1.2), parties box, non-orphaning
  signature execution blocks (KeepTogether), and standard boilerplate standards.
- Specialized Executive Report Generator: cover styling, executive summary callout boxes,
  key findings blocks, and clean zebra-striped tables.
- Full backward compatibility for generic generate_pdf calls.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool, tool
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    ListFlowable,
    ListItem,
    Table,
    TableStyle,
    KeepTogether,
    HRFlowable,
)
from reportlab.lib import colors
from reportlab.pdfgen import canvas

from .. import config, reliability
from .common import last_ai_text, run_inner_agent_with_claim_check, trace_all
from .integration_circuit_breaker import ToolFailureLadderMiddleware
from .scratchpad_tools import build_scratchpad_tools, scratchpad_prompt_block

LABEL = "document_agent"

# Palette: modern slate and executive navy
COLOR_TITLE = colors.HexColor("#0f172a")       # Slate 900
COLOR_BODY = colors.HexColor("#1e293b")        # Slate 800
COLOR_MUTED = colors.HexColor("#64748b")       # Slate 500
COLOR_BORDER = colors.HexColor("#cbd5e1")      # Slate 300
COLOR_DIVIDER = colors.HexColor("#e2e8f0")     # Slate 200
COLOR_BG_LIGHT = colors.HexColor("#f8fafc")    # Slate 50
COLOR_ACCENT = colors.HexColor("#2563eb")      # Blue 600
COLOR_NAVY = colors.HexColor("#1e3a8a")        # Navy 900

# Zero-Bracket Validator regex
_PLACEHOLDER_REGEX = re.compile(
    r"(\[[A-Za-z][A-Za-z0-9\s/_-]{2,40}\]|\<[A-Za-z][A-Za-z0-9\s/_-]{2,40}\>|\b(?:TBD|TODO|INSERT\s+[A-Za-z0-9_-]+)\b)",
    re.IGNORECASE,
)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "document"


def _find_unresolved_placeholders(obj: Any) -> list[str]:
    """Recursively scans strings, dicts, and lists for leftover placeholder brackets."""
    placeholders: list[str] = []
    if isinstance(obj, str):
        found = _PLACEHOLDER_REGEX.findall(obj)
        for f in found:
            # Exclude common footnote markers or checkboxes like [1] or [x]
            cleaned = f.strip("[]<>").strip()
            if cleaned.isdigit() or len(cleaned) <= 1:
                continue
            placeholders.append(f)
    elif isinstance(obj, dict):
        for v in obj.values():
            placeholders.extend(_find_unresolved_placeholders(v))
    elif isinstance(obj, list):
        for item in obj:
            placeholders.extend(_find_unresolved_placeholders(item))
    return list(dict.fromkeys(placeholders))


class NumberedCanvas(canvas.Canvas):
    """Two-pass canvas that computes total page count and prints running
    headers on pages > 1 and dynamic 'Page X of Y' running footers on all pages."""
    doc_title: str = ""
    doc_footer: str = ""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._saved_page_states: list[dict[str, Any]] = []

    def showPage(self) -> None:
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count: int) -> None:
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(COLOR_MUTED)

        # Running Header (pages 2+)
        if self._pageNumber > 1 and self.doc_title:
            self.drawString(54, 750, self.doc_title[:85])
            self.setStrokeColor(COLOR_DIVIDER)
            self.setLineWidth(0.5)
            self.line(54, 742, 612 - 54, 742)

        # Running Footer (all pages)
        footer_msg = self.doc_footer or "CONFIDENTIAL"
        self.drawString(54, 34, footer_msg[:80])
        page_str = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(612 - 54, 34, page_str)
        self.setStrokeColor(COLOR_DIVIDER)
        self.setLineWidth(0.5)
        self.line(54, 44, 612 - 54, 44)

        self.restoreState()


def _make_numbered_canvas(doc_title: str = "", doc_footer: str = "") -> type[NumberedCanvas]:
    class CustomNumberedCanvas(NumberedCanvas):
        pass
    CustomNumberedCanvas.doc_title = doc_title
    CustomNumberedCanvas.doc_footer = doc_footer
    return CustomNumberedCanvas


def _get_document_styles() -> dict[str, ParagraphStyle]:
    sample = getSampleStyleSheet()
    return {
        "ContractTitle": ParagraphStyle(
            "ContractTitle",
            parent=sample["Normal"],
            fontName="Helvetica-Bold",
            fontSize=16,
            leading=22,
            alignment=1,  # Center
            textColor=COLOR_TITLE,
            spaceAfter=6,
        ),
        "ContractSubtitle": ParagraphStyle(
            "ContractSubtitle",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=14,
            alignment=1,  # Center
            textColor=COLOR_MUTED,
            spaceAfter=14,
        ),
        "Preamble": ParagraphStyle(
            "Preamble",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=14.5,
            textColor=COLOR_BODY,
            spaceAfter=12,
        ),
        "SectionHeading": ParagraphStyle(
            "SectionHeading",
            parent=sample["Normal"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=16,
            textColor=COLOR_TITLE,
            spaceBefore=12,
            spaceAfter=6,
            keepWithNext=True,
        ),
        "ClauseText": ParagraphStyle(
            "ClauseText",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=9,
            leading=14,
            textColor=COLOR_BODY,
            spaceAfter=6,
        ),
        "ReportTitle": ParagraphStyle(
            "ReportTitle",
            parent=sample["Normal"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=26,
            textColor=COLOR_TITLE,
            spaceAfter=4,
        ),
        "ReportSubtitle": ParagraphStyle(
            "ReportSubtitle",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=10,
            leading=15,
            textColor=COLOR_MUTED,
            spaceAfter=16,
        ),
        "ReportHeading": ParagraphStyle(
            "ReportHeading",
            parent=sample["Normal"],
            fontName="Helvetica-Bold",
            fontSize=13,
            leading=18,
            textColor=COLOR_NAVY,
            spaceBefore=14,
            spaceAfter=8,
            keepWithNext=True,
        ),
        "Body": ParagraphStyle(
            "DocBody",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=14.5,
            textColor=COLOR_BODY,
            spaceAfter=8,
        ),
        "SummaryText": ParagraphStyle(
            "SummaryText",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=15,
            textColor=COLOR_BODY,
        ),
        "TableCell": ParagraphStyle(
            "TableCell",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=12,
            textColor=COLOR_BODY,
        ),
        "TableHeaderCell": ParagraphStyle(
            "TableHeaderCell",
            parent=sample["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8.5,
            leading=12,
            textColor=colors.white,
        ),
        "SigText": ParagraphStyle(
            "SigText",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=9,
            leading=14,
            textColor=COLOR_BODY,
        ),
    }


def _build_contract_pdf(
    title: str,
    preamble: str,
    parties: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    signatures: list[dict[str, Any]],
    governing_law: str | None,
    out_path: Path,
) -> None:
    styles = _get_document_styles()
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=LETTER,
        leftMargin=54,
        rightMargin=54,
        topMargin=54,
        bottomMargin=54,
    )
    story: list[Any] = []

    # Title & Header divider
    story.append(Paragraph(title.upper(), styles["ContractTitle"]))
    if governing_law:
        story.append(Paragraph(f"Governing Jurisdiction: {governing_law}", styles["ContractSubtitle"]))
    story.append(HRFlowable(width="100%", thickness=1, color=COLOR_BORDER, spaceBefore=4, spaceAfter=14))

    # Preamble
    if preamble:
        for para in preamble.split("\n\n"):
            if para.strip():
                story.append(Paragraph(para.strip().replace("\n", " "), styles["Preamble"]))

    # Parties Summary Box
    if parties:
        party_rows: list[list[Any]] = []
        for p in parties:
            role = p.get("role", "Party").upper()
            name = p.get("name", "")
            entity_type = p.get("type", "")
            address = p.get("address", "")
            details = f"<b>{role}:</b> {name}"
            if entity_type:
                details += f" ({entity_type})"
            if address:
                details += f"<br/><i>Address:</i> {address}"
            party_rows.append([Paragraph(details, styles["ClauseText"])])

        party_table = Table(party_rows, colWidths=[504])
        party_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), COLOR_BG_LIGHT),
            ("BOX", (0, 0), (-1, -1), 0.75, COLOR_BORDER),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(party_table)
        story.append(Spacer(1, 14))

    # Sections & Clauses
    for idx, sec in enumerate(sections):
        sec_num = sec.get("number") or f"{idx + 1}"
        heading = sec.get("heading", "")
        if heading:
            sec_title = f"{sec_num}. {heading.upper()}" if not heading.startswith(f"{sec_num}.") else heading.upper()
            story.append(Paragraph(sec_title, styles["SectionHeading"]))

        body = sec.get("body")
        if body:
            for p in str(body).split("\n\n"):
                if p.strip():
                    story.append(Paragraph(p.strip().replace("\n", " "), styles["ClauseText"]))

        clauses = sec.get("clauses") or []
        for c in clauses:
            clause_str = str(c).strip()
            if clause_str:
                story.append(Paragraph(clause_str, styles["ClauseText"]))

        bullets = sec.get("bullets") or []
        if bullets:
            items = [ListItem(Paragraph(str(b), styles["ClauseText"])) for b in bullets]
            story.append(ListFlowable(items, bulletType="bullet", start="square", leftIndent=12))
            story.append(Spacer(1, 4))

        table_data = sec.get("table")
        if table_data and isinstance(table_data, list):
            formatted_table = []
            for r_idx, row in enumerate(table_data):
                cell_style = styles["TableHeaderCell"] if r_idx == 0 else styles["TableCell"]
                formatted_table.append([Paragraph(str(cell), cell_style) for cell in row])
            t = Table(formatted_table, hAlign="LEFT")
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), COLOR_TITLE),
                ("GRID", (0, 0), (-1, -1), 0.5, COLOR_BORDER),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]))
            story.append(t)
            story.append(Spacer(1, 8))

        story.append(Spacer(1, 6))

    # Execution / Signatures Block
    if signatures:
        story.append(Spacer(1, 10))
        story.append(Paragraph("<b>IN WITNESS WHEREOF</b>, the parties hereto have executed this Agreement as of the Effective Date.", styles["Preamble"]))
        story.append(Spacer(1, 10))

        # Lay out signatures side-by-side or stacked
        sig_cols: list[list[Any]] = []
        if len(signatures) >= 2:
            p1, p2 = signatures[0], signatures[1]
            left = (
                f"<b>{p1.get('party', 'PARTY 1').upper()}:</b><br/>"
                f"{p1.get('entity', '')}<br/><br/>"
                "By: _____________________________________<br/>"
                f"Name: {p1.get('name', '')}<br/>"
                f"Title: {p1.get('title', '')}<br/>"
                f"Date: {p1.get('date', '')}"
            )
            right = (
                f"<b>{p2.get('party', 'PARTY 2').upper()}:</b><br/>"
                f"{p2.get('entity', '')}<br/><br/>"
                "By: _____________________________________<br/>"
                f"Name: {p2.get('name', '')}<br/>"
                f"Title: {p2.get('title', '')}<br/>"
                f"Date: {p2.get('date', '')}"
            )
            sig_table = Table([[Paragraph(left, styles["SigText"]), Paragraph("", styles["SigText"]), Paragraph(right, styles["SigText"])]], colWidths=[240, 24, 240])
            sig_table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]))
            sig_cols.append([sig_table])
        else:
            for s in signatures:
                single = (
                    f"<b>{s.get('party', 'SIGNATORY').upper()}:</b><br/>"
                    f"{s.get('entity', '')}<br/><br/>"
                    "By: _____________________________________<br/>"
                    f"Name: {s.get('name', '')}<br/>"
                    f"Title: {s.get('title', '')}<br/>"
                    f"Date: {s.get('date', '')}"
                )
                sig_table = Table([[Paragraph(single, styles["SigText"])]], colWidths=[280])
                sig_cols.append([sig_table])

        for st in sig_cols:
            story.append(KeepTogether(st))

    footer_text = "Legal document generated for execution based on user specifications."
    canvas_cls = _make_numbered_canvas(title, footer_text)
    doc.build(story, canvasmaker=canvas_cls)


def _build_report_pdf(
    title: str,
    subtitle: str | None,
    executive_summary: str | None,
    key_findings: list[str] | None,
    sections: list[dict[str, Any]],
    metadata: dict[str, Any] | None,
    out_path: Path,
) -> None:
    styles = _get_document_styles()
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=LETTER,
        leftMargin=54,
        rightMargin=54,
        topMargin=54,
        bottomMargin=54,
    )
    story: list[Any] = []

    # Title Banner
    story.append(Paragraph(title, styles["ReportTitle"]))
    if subtitle:
        story.append(Paragraph(subtitle, styles["ReportSubtitle"]))
    story.append(HRFlowable(width="100%", thickness=1.5, color=COLOR_ACCENT, spaceBefore=2, spaceAfter=14))

    # Metadata row (Date, Author, Subject)
    if metadata:
        meta_items = [f"<b>{k.replace('_', ' ').title()}:</b> {v}" for k, v in metadata.items()]
        story.append(Paragraph(" &nbsp;|&nbsp; ".join(meta_items), styles["ContractSubtitle"]))
        story.append(Spacer(1, 8))

    # Executive Summary Callout Box
    if executive_summary:
        summary_content = [
            Paragraph("<b>EXECUTIVE SUMMARY</b>", ParagraphStyle("H", parent=styles["ReportHeading"], fontSize=10, leading=14, spaceBefore=0, spaceAfter=4, textColor=COLOR_NAVY)),
            Paragraph(executive_summary.replace("\n", "<br/>"), styles["SummaryText"]),
        ]
        callout_table = Table([[summary_content]], colWidths=[504])
        callout_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), COLOR_BG_LIGHT),
            ("BOX", (0, 0), (-1, -1), 0.5, COLOR_BORDER),
            ("LINELEFT", (0, 0), (0, -1), 3.5, COLOR_ACCENT),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ]))
        story.append(callout_table)
        story.append(Spacer(1, 14))

    # Key Findings / Highlights
    if key_findings:
        story.append(Paragraph("KEY HIGHLIGHTS & FINDINGS", styles["ReportHeading"]))
        finding_items = [ListItem(Paragraph(str(f), styles["Body"])) for f in key_findings]
        story.append(ListFlowable(finding_items, bulletType="bullet", start="circle", leftIndent=12))
        story.append(Spacer(1, 12))

    # Sections
    for sec in sections:
        heading = sec.get("heading")
        if heading:
            story.append(Paragraph(heading, styles["ReportHeading"]))

        body = sec.get("body")
        if body:
            for p in str(body).split("\n\n"):
                if p.strip():
                    story.append(Paragraph(p.strip().replace("\n", "<br/>"), styles["Body"]))

        bullets = sec.get("bullets")
        if bullets:
            b_items = [ListItem(Paragraph(str(b), styles["Body"])) for b in bullets]
            story.append(ListFlowable(b_items, bulletType="bullet", leftIndent=12))
            story.append(Spacer(1, 6))

        table_data = sec.get("table")
        if table_data and isinstance(table_data, list):
            formatted_table = []
            for r_idx, row in enumerate(table_data):
                cell_style = styles["TableHeaderCell"] if r_idx == 0 else styles["TableCell"]
                formatted_table.append([Paragraph(str(cell), cell_style) for cell in row])
            t = Table(formatted_table, hAlign="LEFT")
            table_style = [
                ("BACKGROUND", (0, 0), (-1, 0), COLOR_TITLE),
                ("GRID", (0, 0), (-1, -1), 0.5, COLOR_BORDER),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
            # Zebra striping for data rows
            for r in range(1, len(formatted_table)):
                bg = COLOR_BG_LIGHT if r % 2 == 1 else colors.white
                table_style.append(("BACKGROUND", (0, r), (-1, r), bg))
            t.setStyle(TableStyle(table_style))
            story.append(t)
            story.append(Spacer(1, 10))

        story.append(Spacer(1, 10))

    canvas_cls = _make_numbered_canvas(title, "Executive Briefing & Report")
    doc.build(story, canvasmaker=canvas_cls)


def _build_generic_pdf(title: str, sections: list[dict[str, Any]], out_path: Path) -> None:
    styles = _get_document_styles()
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=LETTER,
        leftMargin=54,
        rightMargin=54,
        topMargin=54,
        bottomMargin=54,
    )
    story: list[Any] = [
        Paragraph(title, styles["ReportTitle"]),
        HRFlowable(width="100%", thickness=1, color=COLOR_BORDER, spaceBefore=4, spaceAfter=14),
    ]

    for sec in sections:
        heading = sec.get("heading")
        if heading:
            story.append(Paragraph(heading, styles["ReportHeading"]))

        body = sec.get("body")
        if body:
            for p in str(body).split("\n\n"):
                if p.strip():
                    story.append(Paragraph(p.strip().replace("\n", "<br/>"), styles["Body"]))

        bullets = sec.get("bullets")
        if bullets:
            b_items = [ListItem(Paragraph(str(b), styles["Body"])) for b in bullets]
            story.append(ListFlowable(b_items, bulletType="bullet", leftIndent=12))
            story.append(Spacer(1, 6))

        table_data = sec.get("table")
        if table_data and isinstance(table_data, list):
            formatted_table = []
            for r_idx, row in enumerate(table_data):
                cell_style = styles["TableHeaderCell"] if r_idx == 0 else styles["TableCell"]
                formatted_table.append([Paragraph(str(cell), cell_style) for cell in row])
            t = Table(formatted_table, hAlign="LEFT")
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), COLOR_TITLE),
                ("GRID", (0, 0), (-1, -1), 0.5, COLOR_BORDER),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            story.append(t)
            story.append(Spacer(1, 8))

        story.append(Spacer(1, 8))

    canvas_cls = _make_numbered_canvas(title, "Generated Document")
    doc.build(story, canvasmaker=canvas_cls)


def _analyze_contract_text(contract_text: str, user_role: str = "service_provider") -> dict[str, Any]:
    """Perform deterministic risk analysis on contract text across the core legal risk pillars."""
    text_lower = contract_text.lower()

    # Extract detected title
    first_lines = [
        line.strip()
        for line in contract_text.split("\n")
        if line.strip() and not line.strip().lower().startswith("[attachment:")
    ][:5]
    detected_title = "Agreement"
    for line in first_lines:
        clean = line.strip("#-* \t\r")
        if any(w in clean.lower() for w in ["agreement", "contract", "nda", "terms", "sow", "order", "statement of work"]):
            detected_title = clean
            break
    if detected_title == "Agreement" and first_lines:
        detected_title = first_lines[0].strip("#-* \t\r")

    risk_score = 0
    findings: list[dict[str, Any]] = []
    missing_clauses: list[str] = []
    redlines: list[dict[str, str]] = []

    # --- PILLAR 1: Limitation of Liability ---
    has_liability_cap = any(
        phrase in text_lower
        for phrase in [
            "limitation of liability",
            "aggregate liability",
            "shall not exceed",
            "liability is limited to",
            "in no event shall either party be liable for",
            "consequential damages",
            "indirect, incidental",
        ]
    )
    if not has_liability_cap:
        risk_score += 35
        findings.append({
            "pillar": "Limitation of Liability",
            "severity": "CRITICAL",
            "issue": "Missing Limitation of Liability Clause",
            "explanation": (
                "The agreement contains no cap on liability and no waiver of indirect/consequential damages. "
                "Under general contract law, this exposes you to unlimited financial and indirect liability in any dispute."
            ),
        })
        missing_clauses.append("Limitation of Liability (Mutual Fee Cap & Consequential Damages Waiver)")
        redlines.append({
            "section": "Limitation of Liability",
            "proposed_clause": (
                "Limitation of Liability. EXCEPT FOR GROSS NEGLIGENCE OR WILLFUL MISCONDUCT, NEITHER PARTY SHALL "
                "BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, PUNITIVE, OR CONSEQUENTIAL DAMAGES ARISING OUT OF "
                "OR RELATED TO THIS AGREEMENT. EACH PARTY'S TOTAL AGGREGATE LIABILITY UNDER THIS AGREEMENT SHALL BE "
                "STRICTLY LIMITED TO THE TOTAL AMOUNTS PAID OR PAYABLE UNDER THIS AGREEMENT IN THE TWELVE (12) MONTHS "
                "PRECEDING THE CLAIM."
            ),
        })
    else:
        is_onesided = (
            ("client's liability" in text_lower and "provider's liability" not in text_lower and "contractor's liability" not in text_lower)
            or ("service provider's liability shall not exceed" in text_lower and "client's liability" not in text_lower)
        )
        if is_onesided:
            risk_score += 25
            findings.append({
                "pillar": "Limitation of Liability",
                "severity": "HIGH",
                "issue": "One-Sided / Asymmetrical Liability Cap",
                "explanation": "The liability cap protects the counterparty while leaving your own liability uncapped.",
            })
            redlines.append({
                "section": "Limitation of Liability",
                "proposed_clause": "Amend the limitation of liability to be expressly mutual so both parties receive identical protection.",
            })

    # --- PILLAR 2: Indemnification ---
    has_indemnity = any(phrase in text_lower for phrase in ["indemnif", "hold harmless", "defend and hold"])
    if has_indemnity:
        provider_indemnifies = bool(
            re.search(r"\b(contractor|provider|consultant)\b\s+(?:agrees\s+to\s+|shall\s+)?(?:defend[,\s]+)?indemnif", text_lower)
        ) or any(p in text_lower for p in ["contractor agrees to indemnify", "provider agrees to indemnify", "consultant shall indemnify"])
        client_indemnifies = bool(
            re.search(r"\bclient\b\s+(?:agrees\s+to\s+|shall\s+)?(?:defend[,\s]+)?indemnif", text_lower)
        ) or any(p in text_lower for p in ["client agrees to indemnify", "mutually indemnify", "each party shall indemnify", "each party agrees to indemnify"])
        if provider_indemnifies and not client_indemnifies:
            risk_score += 25
            findings.append({
                "pillar": "Indemnification",
                "severity": "HIGH",
                "issue": "One-Sided Indemnification Exposure",
                "explanation": "You are required to defend and indemnify the counterparty without reciprocal indemnification protection.",
            })
            redlines.append({
                "section": "Indemnification",
                "proposed_clause": (
                    "Indemnification. Each party agrees to defend, indemnify, and hold harmless the other party from "
                    "and against third-party claims arising solely out of the indemnifying party's gross negligence, "
                    "willful misconduct, or infringement of third-party intellectual property rights, conditioned upon "
                    "prompt written notice of any claim."
                ),
            })

    # --- PILLAR 3: Intellectual Property & Work for Hire ---
    has_ip = any(phrase in text_lower for phrase in ["intellectual property", "work made for hire", "work for hire", "ownership of deliverables", "assigns all right"])
    if has_ip:
        conditioned_on_payment = bool(
            re.search(r"upon\s+(?:[a-zA-Z'\s]{1,25}\s+)?receipt\s+of\s+(?:full|payment)", text_lower)
        ) or any(
            phrase in text_lower
            for phrase in [
                "upon receipt of full",
                "upon full payment",
                "subject to full payment",
                "conditioned upon payment",
                "contingent upon receipt",
                "receipt of full and final payment",
            ]
        )
        if not conditioned_on_payment and user_role in ["service_provider", "contractor", "consultant"]:
            risk_score += 20
            findings.append({
                "pillar": "Intellectual Property",
                "severity": "HIGH",
                "issue": "Premature IP Assignment Before Payment",
                "explanation": (
                    "Deliverables transfer ownership immediately upon creation. If the client refuses or delays "
                    "payment, they will already legally own your work product."
                ),
            })
            redlines.append({
                "section": "Ownership of Deliverables",
                "proposed_clause": (
                    "Ownership of Deliverables. Conditioned expressly upon Provider's receipt of full and final "
                    "payment for the applicable services, Provider hereby assigns to Client all right, title, and "
                    "interest in and to the custom deliverables created specifically for Client. Provider retains "
                    "ownership of all pre-existing tools, templates, and background technology."
                ),
            })
    elif user_role in ["service_provider", "contractor", "consultant"]:
        risk_score += 15
        missing_clauses.append("Intellectual Property & Deliverables Ownership (with Background IP Reservation)")
        findings.append({
            "pillar": "Intellectual Property",
            "severity": "MODERATE",
            "issue": "Missing IP Ownership & Background IP Reservation",
            "explanation": "No clause establishes who owns deliverables or reserves your pre-existing tools and methodologies.",
        })

    # --- PILLAR 4: Payment Terms & Invoicing ---
    has_payment = any(phrase in text_lower for phrase in ["fee", "fees", "compensation", "payment", "invoice"])
    if has_payment:
        if "net 60" in text_lower or "net 90" in text_lower:
            risk_score += 15
            findings.append({
                "pillar": "Payment Terms",
                "severity": "MODERATE",
                "issue": "Extended Payment Terms (Net 60/90)",
                "explanation": "Payment window is excessively delayed, creating working capital friction.",
            })
            redlines.append({
                "section": "Payment Terms",
                "proposed_clause": "Invoices shall be due and payable within thirty (30) days of receipt (Net 30).",
            })
        has_late_fees = any(phrase in text_lower for phrase in ["late fee", "late charge", "interest of", "1.5%", "per month", "overdue"])
        if not has_late_fees and user_role in ["service_provider", "contractor", "consultant"]:
            risk_score += 10
            missing_clauses.append("Late Payment Interest & Costs of Collection")
            redlines.append({
                "section": "Late Payments",
                "proposed_clause": (
                    "Late Invoices. Past-due balances shall accrue interest at 1.5% per month (or the maximum permitted "
                    "by law), plus reasonable costs of collection."
                ),
            })
    else:
        risk_score += 20
        missing_clauses.append("Payment Terms, Invoicing Cadence, and Due Dates")

    # --- PILLAR 5: Termination & Payment on Cancellation ---
    has_term = any(phrase in text_lower for phrase in ["cancellation", "terminate", "termination"])
    if has_term:
        if any(p in text_lower for p in ["terminate for convenience", "cancel this agreement", "either party may cancel", "terminate at any time"]):
            has_payment_on_term = bool(
                re.search(r"\bpay(?:s|ment|ed)?\b.*?\bservices\b", text_lower)
            ) or any(
                p in text_lower
                for p in [
                    "paid for services", "payment for work completed", "reimbursed for",
                    "services performed up to", "promptly pay", "pays for all services",
                    "pay for all services",
                ]
            )
            if not has_payment_on_term and user_role in ["service_provider", "contractor", "consultant"]:
                risk_score += 20
                findings.append({
                    "pillar": "Termination",
                    "severity": "HIGH",
                    "issue": "Termination for Convenience Without Payment Guarantee",
                    "explanation": (
                        "The contract allows termination or cancellation without guaranteeing prompt payment for work "
                        "performed and commitments incurred up to the cancellation date."
                    ),
                })
                redlines.append({
                    "section": "Payment Upon Termination",
                    "proposed_clause": (
                        "Payment Upon Termination. Upon any early termination or cancellation, Client shall promptly pay "
                        "Provider for all services rendered, deliverables completed, and approved expenses incurred "
                        "through the effective date of termination."
                    ),
                })
    else:
        risk_score += 15
        missing_clauses.append("Term and Termination Notice Clause")

    # --- PILLAR 6: Restrictive Covenants / Non-Compete ---
    if any(p in text_lower for p in ["non-compete", "covenant not to compete", "shall not engage in any business"]):
        risk_score += 25
        findings.append({
            "pillar": "Restrictive Covenants",
            "severity": "HIGH",
            "issue": "Overly Restrictive Non-Compete Clause",
            "explanation": "Restricts your right to practice your profession or take on other industry clients.",
        })
        redlines.append({
            "section": "Non-Compete",
            "proposed_clause": "Strike the non-compete clause in its entirety.",
        })

    # --- PILLAR 7: Governing Law & Jurisdiction ---
    has_law = any(p in text_lower for p in ["governing law", "governed by", "laws of the state of", "jurisdiction", "venue"])
    if not has_law:
        risk_score += 15
        missing_clauses.append("Governing Law, Jurisdiction, and Dispute Venue")
        redlines.append({
            "section": "Governing Law",
            "proposed_clause": (
                "Governing Law and Jurisdiction. This Agreement shall be governed by and construed in accordance with "
                "the laws of the State of Delaware (or Provider's home jurisdiction), without regard to conflicts of law. "
                "The parties submit to the exclusive jurisdiction of the courts located therein."
            ),
        })

    # --- PILLAR 8: Integration & Entire Agreement ---
    has_integration = any(p in text_lower for p in ["entire agreement", "integration", "supersedes all prior"])
    if not has_integration:
        missing_clauses.append("Entire Agreement / Integration Clause")

    # Risk Rating
    if risk_score >= 50:
        overall_rating = "CRITICAL RISK"
        summary_verdict = (
            "DO NOT SIGN IN PRESENT FORM. The contract contains critical structural vulnerabilities, "
            "including missing liability protections and potential financial exposure. Mandatory redlines required."
        )
    elif risk_score >= 30:
        overall_rating = "HIGH RISK"
        summary_verdict = (
            "SUBSTANTIAL RISK DETECTED. The contract lacks essential defensive clauses or contains one-sided terms. "
            "Negotiate recommended redlines before signing."
        )
    elif risk_score >= 15:
        overall_rating = "MODERATE RISK"
        summary_verdict = (
            "MODERATE COMMERCIAL FRICTION. The agreement is workable but has minor gaps (e.g. payment timing, late fees, "
            "or missing boilerplate) that should be clarified."
        )
    else:
        overall_rating = "LOW RISK"
        summary_verdict = "WELL-BALANCED AGREEMENT. The terms appear balanced and customary for commercial transactions."

    return {
        "title": detected_title,
        "overall_rating": overall_rating,
        "risk_score": risk_score,
        "summary_verdict": summary_verdict,
        "findings": findings,
        "missing_clauses": missing_clauses,
        "redlines": redlines,
    }


def build_document_tools(user: config.UserContext | None = None) -> list[BaseTool]:
    out_dir = Path(config.OUTPUTS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    @tool
    async def generate_contract_pdf(
        title: str,
        preamble: str,
        parties: list[dict],
        sections: list[dict],
        signatures: list[dict],
        governing_law: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> str:
        """Generate a professionally styled, legally structured contract PDF ready for execution.

        Parameters:
        - title: Name of agreement (e.g. 'INDEPENDENT CONTRACTOR AGREEMENT', 'NON-DISCLOSURE AGREEMENT').
        - preamble: Introductory sentence stating effective date and intent.
        - parties: List of party dictionaries: [{'role': 'Client'|'Contractor'|'Disclosing Party', 'name': str, 'type': str (e.g. 'Delaware LLC' or 'Individual'), 'address': str}].
        - sections: List of numbered sections with clauses:
            [{'number': '1', 'heading': 'Services and Deliverables', 'clauses': ['1.1 Scope: ...', '1.2 Schedule: ...'], 'table'?: list[list[str]]}].
        - signatures: List of execution block dicts: [{'party': 'Client', 'name': str, 'title': str, 'entity': str, 'date': str}].
        - governing_law: State or jurisdiction for governing law (defaults to user's home state or Delaware).
        - filename: Optional output file name (slug of title used if omitted).

        ZERO-BRACKET GATE: All fields must contain concrete real values. If any bracketed placeholder
        like '[Client Name]' or '[Insert Date]' or 'TBD' is detected, compilation will be rejected.
        """
        # Validate Zero-Bracket Gate
        all_content = [title, preamble, parties, sections, signatures, governing_law]
        placeholders = _find_unresolved_placeholders(all_content)
        if placeholders:
            return (
                f"VALIDATION ERROR: Found {len(placeholders)} unresolved placeholder(s): {', '.join(placeholders)}. "
                "Contracts must be complete and ready to sign without placeholder brackets. "
                "Please replace these with real concrete values or standard legal defaults "
                "(e.g. standard Delaware law, Net 30 payment, mutual 2-year confidentiality) and call again."
            )

        name = _slugify(filename or title)
        out_path = out_dir / f"{name}.pdf"
        try:
            _build_contract_pdf(
                title=title,
                preamble=preamble,
                parties=parties,
                sections=sections,
                signatures=signatures,
                governing_law=governing_law,
                out_path=out_path,
            )
        except Exception as e:
            return f"Error generating contract PDF: {e}"
        return f"Generated Contract PDF at {out_path.resolve()}"

    @tool
    async def generate_report_pdf(
        title: str,
        sections: list[dict],
        subtitle: Optional[str] = None,
        executive_summary: Optional[str] = None,
        key_findings: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
        filename: Optional[str] = None,
    ) -> str:
        """Generate a polished executive report or research briefing PDF.

        Parameters:
        - title: Report title (e.g. 'Q1 Competitor & Market Analysis').
        - sections: List of sections: [{'heading': str, 'body'?: str, 'bullets'?: list[str], 'table'?: list[list[str]]}].
        - subtitle: Optional descriptive subtitle (e.g. 'Prepared for Leadership Team').
        - executive_summary: High-level summary rendered in a styled executive callout box.
        - key_findings: List of critical takeaway points or metrics.
        - metadata: Optional key-value metadata dict (e.g. {'Date': 'March 2026', 'Author': 'Messa Intelligence'}).
        - filename: Optional output file name.
        """
        all_content = [title, subtitle, executive_summary, key_findings, sections, metadata]
        placeholders = _find_unresolved_placeholders(all_content)
        if placeholders:
            return (
                f"VALIDATION ERROR: Found unresolved placeholder(s): {', '.join(placeholders)}. "
                "Please replace with real values before generating the report."
            )

        name = _slugify(filename or title)
        out_path = out_dir / f"{name}.pdf"
        try:
            _build_report_pdf(
                title=title,
                subtitle=subtitle,
                executive_summary=executive_summary,
                key_findings=key_findings,
                sections=sections,
                metadata=metadata,
                out_path=out_path,
            )
        except Exception as e:
            return f"Error generating report PDF: {e}"
        return f"Generated Report PDF at {out_path.resolve()}"

    @tool
    async def generate_pdf(
        title: str,
        sections: list[dict],
        filename: Optional[str] = None,
    ) -> str:
        """Generate a standard formatted PDF document and return its file path.
        For specialized legal contracts or executive reports, prefer generate_contract_pdf
        or generate_report_pdf respectively.

        title: document title.
        sections: list of {heading?, body?, bullets?: list[str], table?: list[list[str]]}.
        filename: optional filename (without extension); defaults to a slug of the title.
        """
        name = _slugify(filename or title)
        out_path = out_dir / f"{name}.pdf"
        try:
            _build_generic_pdf(title, sections, out_path)
        except Exception as e:
            return f"Error generating PDF: {e}"
        return f"Generated PDF at {out_path.resolve()}"

    @tool
    async def audit_contract(
        contract_text: str,
        user_role: str = "service_provider",
        generate_audit_report_pdf: bool = False,
        report_filename: Optional[str] = None,
    ) -> str:
        """Analyze a legal contract, agreement, or SOW to identify high-risk clauses,
        one-sided liabilities, missing protections, and generate actionable redlines.

        contract_text: The full text of the agreement (extracted from PDF or email).
        user_role: The user's role in the contract ('service_provider', 'client', 'contractor', 'consultant').
        generate_audit_report_pdf: If True, also compiles a formal executive PDF risk audit report in /outputs/.
        report_filename: Optional filename for the generated audit report PDF.
        """
        analysis = _analyze_contract_text(contract_text, user_role)
        lines = [
            f"# Legal Risk Audit: {analysis['title']}",
            f"**Overall Risk Rating**: {analysis['overall_rating']} (Risk Score: {analysis['risk_score']}/100)",
            f"**Executive Verdict**: {analysis['summary_verdict']}\n",
            "_This is an automated review for informational purposes only, not legal advice. "
            "For anything above low risk, or before signing, have a licensed attorney review "
            "the actual agreement._\n",
        ]

        if analysis["findings"]:
            lines.append("### Key Red Flags & Exposure Points:")
            for idx, f in enumerate(analysis["findings"], 1):
                lines.append(f"{idx}. **{f['issue']}** [{f['severity']}] ({f['pillar']})")
                lines.append(f"   - **Risk**: {f['explanation']}")
            lines.append("")

        if analysis["missing_clauses"]:
            lines.append("### Missing Critical Protections:")
            for mc in analysis["missing_clauses"]:
                lines.append(f"- {mc}")
            lines.append("")

        if analysis["redlines"]:
            lines.append("### Recommended Redlines & Counter-Proposals:")
            for r in analysis["redlines"]:
                lines.append(f"**Section: {r['section']}**")
                lines.append(f"> \"{r['proposed_clause']}\"\n")

        if generate_audit_report_pdf:
            doc_title = f"Legal Risk Audit: {analysis['title']}"
            sections = []

            if analysis["findings"]:
                findings_table = [["Issue", "Severity", "Risk Pillar", "Analysis"]]
                for f in analysis["findings"]:
                    findings_table.append([f["issue"], f["severity"], f["pillar"], f["explanation"]])
                sections.append({
                    "heading": "1. Key Red Flags & Identified Vulnerabilities",
                    "table": findings_table,
                })

            if analysis["missing_clauses"]:
                sections.append({
                    "heading": "2. Missing Standard Boilerplate Protections",
                    "bullets": analysis["missing_clauses"],
                })

            if analysis["redlines"]:
                redline_bullets = [
                    f"<b>{r['section']}</b>: \"{r['proposed_clause']}\""
                    for r in analysis["redlines"]
                ]
                sections.append({
                    "heading": "3. Actionable Redline Amendments",
                    "bullets": redline_bullets,
                })

            sections.append({
                "heading": "Disclaimer",
                "bullets": [
                    "This report is an automated review generated for informational purposes only "
                    "and does not constitute legal advice. It is not a substitute for review by a "
                    "licensed attorney, and no attorney-client relationship is formed by its use. "
                    "Have a qualified attorney review this agreement before signing, especially for "
                    "any risk rating above Low."
                ],
            })

            slug = _slugify(report_filename or f"audit-{analysis['title']}")
            pdf_path = out_dir / f"{slug}.pdf"
            try:
                _build_report_pdf(
                    title=doc_title,
                    subtitle="Comprehensive Contract Risk Assessment & Redline Counter-Offer",
                    executive_summary=f"Risk Rating: {analysis['overall_rating']}. {analysis['summary_verdict']}",
                    key_findings=[f"{f['issue']} ({f['severity']})" for f in analysis["findings"][:4]],
                    sections=sections,
                    metadata={"Assessed Role": user_role.replace("_", " ").title(), "Document": analysis["title"]},
                    out_path=pdf_path,
                )
                lines.append(f"Generated Report PDF at {pdf_path.resolve()}")
            except Exception as e:
                lines.append(f"(Note: Failed to compile audit PDF report: {e})")

        return "\n".join(lines)

    raw_tools: list[BaseTool] = [
        generate_contract_pdf,
        generate_report_pdf,
        generate_pdf,
        audit_contract,
    ]
    return trace_all(raw_tools, LABEL)


def build_document_system_prompt(user: config.UserContext | None = None) -> str:
    user_identity = ""
    if user:
        name = user.name or "the user"
        city = f", {user.city}" if user.city else ""
        email = f" (email: {user.email or user.messa_email})" if (user.email or user.messa_email) else ""
        user_identity = (
            f"User Context: You are generating documents for {name}{city}{email}. "
            "When drafting agreements where the user is a party, use this information to populate "
            "their legal name, entity/individual status, and notice address without asking.\n\n"
        )

    return (
        "You are the document specialist: you turn requests into professional, ready-to-use "
        "contracts, executive reports, legal audits, and PDFs, delegated to you by Messa.\n\n"
        f"{user_identity}"
        "TOOL SELECTION:\n"
        "- generate_contract_pdf: For drafting binding legal agreements, NDAs, independent contractor agreements, "
        "consulting agreements, MSAs, SOWs, bills of sale, and offer letters.\n"
        "- generate_report_pdf: For market research, intelligence digests, executive briefings, "
        "financial summaries, and competitive audits.\n"
        "- generate_pdf: For general multi-section documents that do not require legal hierarchy "
        "or executive callouts.\n"
        "- audit_contract: For analyzing existing contracts or agreements (sent by counterparties via email "
        "or text) to identify one-sided liabilities, uncapped risks, missing protections, and formulate "
        "concrete redline counter-proposals.\n\n"
        "CLARIFICATION PROTOCOL (Ask Once With Smart Defaults, Don't Spam):\n"
        "When the user asks you to draft a contract or report and certain commercial terms are missing:\n"
        "1. NEVER stall drafting with endless questions or multiple text messages.\n"
        "2. Formulate ONE concise reply proposing standard commercial defaults:\n"
        "   - Payment: Net 30, invoice upon monthly completion or milestone deliverables.\n"
        "   - Term & Termination: 30 days mutual written notice; immediate for uncured breach.\n"
        "   - Limitation of Liability: Mutual cap equal to fees paid in prior 12 months, waiver of consequential damages.\n"
        "   - Governing Law: User's home state or Delaware.\n"
        "   - Confidentiality: Mutual 2-year non-disclosure obligation.\n"
        "3. Ask ONLY for the truly variable deal points (e.g. Counterparty legal name and agreed price/rate) "
        "in a single message, explicitly stating that standard protective defaults will be used for all other terms "
        "unless they specify otherwise.\n\n"
        "LEGAL CONTRACT DRAFTING STANDARDS (Zero-Mistake Contract Engine):\n"
        "1. Complete & Actionable: Contracts must be ready to sign immediately. NEVER leave placeholders "
        "such as [Insert Date], [Client Name], <TBD>, or [State]. The Zero-Bracket validator will reject them.\n"
        "2. Essential Boilerplate: Every agreement must include the 5 standard protective pillars:\n"
        "   a) Consideration & Terms: Clear payment amount, schedule, and deliverables.\n"
        "   b) Limitation of Liability: Always cap liability at total fees paid under the contract and include "
        "mutual waiver of indirect/punitive damages to prevent catastrophic exposure.\n"
        "   c) Governing Law & Jurisdiction: Default to the user's home state or Delaware.\n"
        "   d) Term & Termination: Clear notice period for convenience (e.g. 14 or 30 days written notice) "
        "and immediate termination for breach.\n"
        "   e) Severability & Entire Agreement: Protects against informal chat being construed as modifications.\n"
        "3. Structured Clauses: Group clauses logically into numbered sections (1.1, 1.2, 2.1).\n"
        "4. Signature Blocks: Always supply execution details for both parties so formal signature lines "
        "are generated at the bottom.\n\n"
        "CONTRACT RISK AUDITING STANDARDS:\n"
        "- When auditing a contract with audit_contract, scrutinize all 8 risk pillars: Liability Cap, "
        "Indemnification, IP Assignment, Payment Terms, Termination Rights, Restrictive Covenants, "
        "Governing Law, and Entire Agreement.\n"
        "- Deliver clear, actionable redline amendments that the user can immediately paste into an email "
        "or counter-offer back to the other party.\n\n"
        "EXECUTIVE REPORT STANDARDS:\n"
        "- Provide a concise 2-3 sentence executive_summary for the styled callout box.\n"
        "- Provide 3-5 key_findings as bullet takeaways.\n"
        "- Use structured tables for tabular/financial metrics instead of dense prose paragraphs.\n\n"
        "VERBATIM PATH REPORTING:\n"
        "Report back the EXACT file path returned by the tool verbatim as the last line in your reply "
        "(e.g. 'Generated Contract PDF at /outputs/...'). Messa relays this exact path to text or email "
        "the document to the user.\n"
    )


# Backward-compatible static prompt string
DOCUMENT_SYSTEM_PROMPT = build_document_system_prompt(None)


def build_document_subagent(user: config.UserContext, model: BaseChatModel) -> dict[str, Any]:
    """Returns a deepagents `CompiledSubAgent` spec (feature/agentic-upgrade
    plan) -- same shape/reasoning as email_tools.build_email_subagent.
    document_agent used to be a plain declarative SubAgent dict (tools/
    system_prompt frozen at build_orchestrator's start) -- see
    tools/integration_tools.py's build_integration_subagent docstring for
    the concrete "artifact created earlier this turn is invisible to a
    later delegation" bug this fixes (e.g. a document referencing a
    spreadsheet id another subagent just created this same turn)."""

    async def _run(state: dict[str, Any]) -> dict[str, Any]:
        messages = list(state["messages"])
        tools = build_document_tools(user)
        system_prompt = build_document_system_prompt(user) + reliability.RELIABILITY_GUARDRAIL_STR
        if config.SCRATCHPAD_AND_SKILLS_ENABLED:
            tools = tools + build_scratchpad_tools(user, "document_agent")
            system_prompt = system_prompt + await scratchpad_prompt_block(user)
        run_config = {"recursion_limit": config.RECURSION_LIMIT}
        inner_agent = create_agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            middleware=[
                ToolFailureLadderMiddleware("generate_contract_pdf"),
                ToolFailureLadderMiddleware("generate_report_pdf"),
                ToolFailureLadderMiddleware("generate_pdf"),
                ToolFailureLadderMiddleware("audit_contract"),
            ],
        )
        final_messages = await run_inner_agent_with_claim_check(
            inner_agent, messages, run_config, label="document_agent"
        )
        return {"messages": [AIMessage(content=last_ai_text(final_messages))]}

    return {
        "name": "document_agent",
        "description": (
            "Generates legally structured contracts (NDAs, consulting agreements, MSAs, SOWs, "
            "offers) and polished executive reports/briefings as PDFs from structured content."
        ),
        "runnable": RunnableLambda(_run),
    }

