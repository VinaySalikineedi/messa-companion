"""Document subagent: generates PDFs on request.

Uses reportlab (pure Python, no system libraries) rather than a
weasyprint/HTML->PDF pipeline, specifically so this keeps working unmodified
on HuggingFace Spaces' CPU-basic Docker image in Phase 4 without needing
apt-level font/cairo/pango packages.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from langchain_core.tools import BaseTool, tool
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem, Table, TableStyle
from reportlab.lib import colors

from .. import config
from .common import trace_all

LABEL = "document_agent"


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "document"


def _build_pdf(title: str, sections: list[dict], out_path: Path) -> None:
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        str(out_path), pagesize=LETTER,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch, topMargin=0.9 * inch, bottomMargin=0.9 * inch,
    )
    story = [Paragraph(title, styles["Title"]), Spacer(1, 0.25 * inch)]

    for section in sections:
        heading = section.get("heading")
        if heading:
            story.append(Paragraph(heading, styles["Heading2"]))
            story.append(Spacer(1, 0.08 * inch))

        body = section.get("body")
        if body:
            for para in str(body).split("\n\n"):
                if para.strip():
                    story.append(Paragraph(para.strip().replace("\n", "<br/>"), styles["BodyText"]))
                    story.append(Spacer(1, 0.08 * inch))

        bullets = section.get("bullets")
        if bullets:
            items = [ListItem(Paragraph(str(b), styles["BodyText"])) for b in bullets]
            story.append(ListFlowable(items, bulletType="bullet"))
            story.append(Spacer(1, 0.1 * inch))

        table = section.get("table")  # list[list[str]], first row = header
        if table:
            t = Table(table, hAlign="LEFT")
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2d2d2d")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            story.append(t)
            story.append(Spacer(1, 0.15 * inch))

        story.append(Spacer(1, 0.15 * inch))

    doc.build(story)


def build_document_tools() -> list[BaseTool]:
    out_dir = Path(config.OUTPUTS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    @tool
    async def generate_pdf(title: str, sections: list[dict], filename: Optional[str] = None) -> str:
        """Generate a PDF document and return its file path.

        title: document title.
        sections: list of {heading?, body?, bullets?: list[str], table?: list[list[str]]}.
        filename: optional filename (without extension); defaults to a slug of the title.
        """
        name = _slugify(filename or title)
        out_path = out_dir / f"{name}.pdf"
        _build_pdf(title, sections, out_path)
        return f"Generated PDF at {out_path.resolve()}"

    return trace_all([generate_pdf], LABEL)


DOCUMENT_SYSTEM_PROMPT = (
    "You are the document specialist: you turn requests into well-structured PDFs, "
    "delegated to you by Messa.\n"
    "- Organize content into clear sections with headings before calling generate_pdf.\n"
    "- Use bullets for lists and a table for tabular data instead of cramming it into prose.\n"
    "- Report back the exact file path generate_pdf returns, verbatim, as the last thing in "
    "your reply -- Messa relays it to personal_inbox_agent when the user wants the document "
    "emailed/attached (as attachment_path on send_email/reply_to_email), or passes it to her "
    "own send_pdf_over_text tool when they want it texted instead, so it must be exact, not "
    "paraphrased or reformatted.\n"
)
