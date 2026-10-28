"""@brief A real, minimal PDF for tests that upload a document.

Since the review of 2026-10-27 (finding E1) every PDF is rebuilt without its
metadata before it reaches a provider, and a file that cannot be parsed as a
PDF is refused (fail closed, like the images). Tests that used to write fake
bytes such as ``b"%PDF-1.4 offline"`` now write this document instead: one
blank page, no metadata, built with pypdf, so the upload path runs as it does
for a real datasheet.
"""
from __future__ import annotations

import io


def minimal_pdf_bytes() -> bytes:
    """@brief Bytes of a one-page blank PDF that pypdf can read back.

    @return A complete PDF document (about 400 bytes).
    """
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


#: Built once per test session: every caller gets the same bytes.
MINIMAL_PDF = minimal_pdf_bytes()
