"""Shared PDF text-extraction, used by every path in this project that
reads an existing PDF someone SENT Messa (as opposed to one she generates
herself -- that's tools/document_tools.py's generate_pdf, which is
unrelated and stays unlimited):

  - a PDF texted to her over SMS/iMessage (server.py's sendblue_webhook)
  - a PDF attached to inbound mail on her own address (server.py's
    personal_email_inbound_webhook, fed by cloudflare/personal-email-worker/
    worker.js's PostalMime attachment parsing)

Uses pypdf (pure Python, no system libraries) -- same reasoning
tools/document_tools.py already documents for choosing reportlab over a
weasyprint/HTML pipeline: this has to keep working unmodified on
HuggingFace Spaces' CPU-basic Docker image with no apt-level dependencies.

Reading is capped at config.MAX_PDF_READ_PAGES (default 10, explicitly
"easily modified by a variable when needed" per the product ask) and
config.MAX_PDF_READ_BYTES on the raw file size before parsing is even
attempted -- both apply ONLY to reading. Generating a PDF is untouched and
remains unlimited.
"""
from __future__ import annotations

from io import BytesIO

from pypdf import PdfReader

from . import config


class PdfReadFailure(RuntimeError):
    """Raised when a PDF can't be opened/parsed at all (corrupt file,
    password-protected with a real password, not actually a PDF despite
    the extension/content-type claiming otherwise), or when nothing you'd
    call "text" could be pulled out of it (e.g. a scanned/image-only PDF --
    OCR is out of scope here)."""


def extract_pdf_text(data: bytes, *, max_pages: int | None = None) -> tuple[str, int, bool]:
    """Extract text from raw PDF bytes.

    Returns (text, pages_read, truncated) where `truncated` is True iff the
    PDF had more pages than the limit allowed -- callers should mention
    that wherever this gets shown to the model/user, so "read the first 10
    pages of a 40-page PDF" is never silently presented as "read the whole
    document."

    Raises PdfReadFailure (never a raw pypdf exception) if the file can't
    be opened at all, or if extraction produced no usable text."""
    limit = config.MAX_PDF_READ_PAGES if max_pages is None else max_pages
    try:
        reader = PdfReader(BytesIO(data))
    except Exception as e:  # noqa: BLE001 - pypdf raises several different exception types for a bad file
        raise PdfReadFailure(f"Couldn't open that PDF: {e}") from e

    if reader.is_encrypted:
        try:
            # Some "encrypted" PDFs just have an empty owner password (set
            # by the tool that made them, not intentionally protected) --
            # worth one cheap attempt before giving up.
            reader.decrypt("")
        except Exception:  # noqa: BLE001 - fall through to the page-read loop below either way
            pass

    try:
        total_pages = len(reader.pages)
    except Exception as e:  # noqa: BLE001
        raise PdfReadFailure(f"Couldn't read that PDF's page list: {e}") from e

    truncated = total_pages > limit
    pages_to_read = min(total_pages, limit)

    parts: list[str] = []
    for i in range(pages_to_read):
        try:
            parts.append(reader.pages[i].extract_text() or "")
        except Exception as e:  # noqa: BLE001 - one bad page shouldn't sink the whole extraction
            parts.append(f"[page {i + 1}: couldn't extract text -- {e}]")

    text = "\n\n".join(p.strip() for p in parts if p.strip())
    if not text:
        raise PdfReadFailure(
            "No extractable text found in that PDF (it may be a scanned/image-only document, "
            "or password-protected)."
        )
    return text, pages_to_read, truncated
