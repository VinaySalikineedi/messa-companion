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

from .. import config
from .common import trace_all

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

    raw_tools: list[BaseTool] = [generate_contract_pdf, generate_report_pdf, generate_pdf]
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
        "contracts, executive reports, and PDFs, delegated to you by Messa.\n\n"
        f"{user_identity}"
        "TOOL SELECTION:\n"
        "- generate_contract_pdf: For legal agreements, NDAs, independent contractor agreements, "
        "consulting agreements, MSAs, SOWs, bills of sale, and offer letters.\n"
        "- generate_report_pdf: For market research, intelligence digests, executive briefings, "
        "financial summaries, and competitive audits.\n"
        "- generate_pdf: For general multi-section documents that do not require legal hierarchy "
        "or executive callouts.\n\n"
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
