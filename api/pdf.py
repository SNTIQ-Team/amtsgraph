"""PDF letter rendering — DIN 5008, modeled on the SNTIQ org templates
(1st_Gericht / 2nd_Antwort) with deliberate departures:

  - NO logo and NO org contact block: documents are filed in the
    user's own name (RDG risk of implied representation);
  - the old § 6 RDG "nachbarschaftliche Hilfe" footer is replaced by a
    FIRST-PERSON self-help notice (draft generated -> checked/adapted ->
    signed by me), rendered on page 1 only; later pages carry a slim
    generator line;
  - "Per Fax vorab: <fax>" appears only when a fax number is supplied;
  - the "Ihr Az. / Schr. v." info lines are optional.

Contract (Schutzbrief module README): known template ids only, strict
payload limits, stateless render -> stream -> discard, never cached,
render pool capped for the 1-GB host.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from fpdf import FPDF
from pydantic import BaseModel, Field, field_validator

router = APIRouter(prefix="/v1/pdf", tags=["pdf"])

FONT_DIR = Path(os.environ.get(
    "SNTIQ_PDF_FONT_DIR", Path(__file__).resolve().parent / "fonts"))

RDG_NOTE = (
    "Hinweis nach dem Rechtsdienstleistungsgesetz (RDG): Dieses Dokument "
    "wurde mit dem nichtkommerziellen Selbsthilfe-Generator „Briefcraft“ "
    "(SNTIQ: Deutschland Together n. e. V.) als Entwurf erstellt. Ich habe "
    "den Inhalt selbst geprüft, bei Bedarf angepasst und eigenverantwortlich "
    "unterschrieben; SNTIQ hat meinen Einzelfall nicht rechtlich geprüft, "
    "nicht beraten und vertritt mich nicht (keine Rechtsdienstleistung "
    "i. S. d. § 2 Abs. 1 RDG). Die Verantwortung für Inhalt, Richtigkeit "
    "und Einreichung liegt allein bei mir.")

SLIM_NOTE = "Erstellt mit Briefcraft / SNTIQ — sntiq.com · kein Rechtsbeistand"

# at most N concurrent renders; the rest wait briefly, then 503
_POOL = threading.BoundedSemaphore(2)
_POOL_WAIT_S = 5


class LetterPayload(BaseModel):
    template: Literal["letter", "lawsuit"]
    # docx: exact org template, opens in Word/LibreOffice;
    # pdf: docx converted via unoserver, fpdf renderer as fallback
    format: Literal["pdf", "docx"] = "pdf"
    sender: list[str] = Field(min_length=1, max_length=6)
    recipient: list[str] = Field(min_length=1, max_length=8)
    date: str = Field(min_length=1, max_length=40)
    # sender city for the "Ort, den …" line
    ort: str | None = Field(default=None, max_length=60)
    fax_vorab: str | None = Field(default=None, max_length=40)
    info_lines: list[str] = Field(default_factory=list, max_length=4)
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20_000)
    # newline-separated for co-applicants (multiple dotted lines)
    signature_name: str | None = Field(default=None, max_length=400)
    attachments: str | None = Field(default=None, max_length=1_000)

    @field_validator("sender", "recipient", "info_lines")
    @classmethod
    def _line_length(cls, v: list[str]) -> list[str]:
        for line in v:
            if len(line) > 120:
                raise ValueError("line too long")
        return v


class DinPdf(FPDF):
    """A4 portrait, DIN 5008 margins; RDG footer p.1, slim footer after."""

    def __init__(self) -> None:
        super().__init__("P", "mm", "A4")
        self.set_margins(25, 20, 20)
        self.set_auto_page_break(True, 34)
        self.add_font("din", "", FONT_DIR / "DejaVuSans.ttf")
        self.add_font("din", "B", FONT_DIR / "DejaVuSans-Bold.ttf")
        self.alias_nb_pages()

    def footer(self) -> None:  # called by fpdf2 on every page
        self.set_text_color(120, 120, 120)
        if self.page_no() == 1:
            self.set_y(-30)
            self.set_font("din", "", 6.3)
            self.multi_cell(0, 2.7, RDG_NOTE)
            self.set_y(-7)
            self.set_font("din", "", 6.5)
            self.cell(0, 3, SLIM_NOTE, align="L")
            self.cell(0, 3, f"Seite {self.page_no()} von {{nb}}", align="R")
        else:
            # RDG note appears on page 1 only; later pages: number only
            self.set_y(-10)
            self.set_font("din", "", 6.5)
            self.cell(0, 3, f"- Seite {self.page_no()} von {{nb}} -",
                      align="C")
        self.set_text_color(0, 0, 0)


def render(p: LetterPayload) -> bytes:
    pdf = DinPdf()
    pdf.add_page()

    # fold marks (105 / 210 mm) + punch mark (148.5 mm)
    pdf.set_draw_color(170, 170, 170)
    pdf.set_line_width(0.2)
    pdf.line(3, 105, 6, 105)
    pdf.line(3, 210, 6, 210)
    pdf.line(3, 148.5, 8, 148.5)

    # Rücksendeangabe "Name · Str. · PLZ Ort" + rule; fax notice right
    pdf.set_xy(25, 44)
    pdf.set_font("din", "", 7.5)
    pdf.set_text_color(110, 110, 110)
    pdf.cell(105, 3.5, " ● ".join(
        s.strip() for s in p.sender if s.strip())[:118])
    if p.fax_vorab:
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("din", "B", 8.5)
        pdf.set_xy(118, 44)
        pdf.cell(72, 3.5, f"Per Fax vorab: {p.fax_vorab}", align="R")
    pdf.set_draw_color(140, 140, 140)
    pdf.line(25, 48.2, 190, 48.2)
    pdf.set_draw_color(0, 0, 0)
    pdf.set_text_color(0, 0, 0)

    # recipient address window (left) | sender block (right)
    pdf.set_font("din", "", 10.5)
    y = 52.0
    for line in p.recipient:
        pdf.set_xy(25, y)
        pdf.cell(85, 4.6, line)
        y += 4.6

    pdf.set_font("din", "B", 9)
    pdf.set_xy(125, 52)
    pdf.cell(65, 4.2, p.sender[0])
    pdf.set_font("din", "", 9)
    ys = 56.2
    for line in p.sender[1:]:
        pdf.set_xy(125, ys)
        pdf.cell(65, 4.2, line)
        ys += 4.2

    # info row: "Ort, den DATE" left; optional Az./Schreiben lines right
    y_info = 92.0
    pdf.set_font("din", "", 9.5)
    ort_den = f"{p.ort}, den {p.date}" if p.ort else p.date
    pdf.set_xy(25, y_info)
    pdf.cell(80, 4.4, ort_den)
    if p.info_lines:
        yi = y_info
        pdf.set_font("din", "", 8.5)
        pdf.set_text_color(70, 70, 70)
        for line in p.info_lines:
            pdf.set_xy(110, yi)
            pdf.cell(80, 4.0, line, align="R")
            yi += 4.0
        pdf.set_text_color(0, 0, 0)

    # subject (bold, no "Betreff:" prefix per DIN 5008)
    pdf.set_xy(25, 102)
    pdf.set_font("din", "B", 10.5)
    pdf.multi_cell(0, 4.8, p.subject)

    # body
    pdf.set_y(pdf.get_y() + 6)
    pdf.set_font("din", "", 10.5)
    pdf.multi_cell(0, 4.8, p.body)

    # dotted signature block(s) — dots sized to each name
    if p.signature_name:
        names = [n for n in p.signature_name.split("\n") if n.strip()]
        for name in names:
            if pdf.get_y() > 240:
                pdf.add_page()
            pdf.set_y(pdf.get_y() + 14)
            pdf.set_font("din", "", 10.5)
            dots = "…" * max(14, min(34, len(name) + 6))
            pdf.cell(0, 4.8, dots, align="R", new_x="LMARGIN",
                     new_y="NEXT")
            pdf.cell(0, 4.8, name + "  ", align="R", new_x="LMARGIN",
                     new_y="NEXT")

    # Anlagen block after the signature
    if p.attachments:
        pdf.set_y(pdf.get_y() + 12)
        pdf.set_font("din", "", 9.5)
        pdf.multi_cell(0, 4.4, p.attachments)

    return bytes(pdf.output())


def _render_via_docx(payload: LetterPayload) -> tuple[bytes, str] | None:
    """Org-template path: docx always; pdf when unoserver converts."""
    try:
        from api.docx_render import docx_to_pdf, render_docx
        docx_bytes = render_docx(
            sender=payload.sender, recipient=payload.recipient,
            ort=payload.ort, date=payload.date,
            fax_vorab=payload.fax_vorab, info_lines=payload.info_lines,
            subject=payload.subject, body=payload.body,
            signature_name=payload.signature_name,
            attachments=payload.attachments)
    except Exception:  # noqa: BLE001 — template path optional
        return None
    if payload.format == "docx":
        return docx_bytes, ("application/vnd.openxmlformats-officedocument"
                            ".wordprocessingml.document")
    pdf = docx_to_pdf(docx_bytes)
    return (pdf, "application/pdf") if pdf else None


@router.post("/letter")
def pdf_letter(payload: LetterPayload) -> Response:
    if not _POOL.acquire(timeout=_POOL_WAIT_S):
        raise HTTPException(503, "render pool busy — retry shortly")
    try:
        result = _render_via_docx(payload)
        if result is None:
            if payload.format == "docx":
                raise HTTPException(503, "docx template unavailable")
            result = render(payload), "application/pdf"  # fpdf fallback
        data, media_type = result
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — never leak payload details
        raise HTTPException(500, "render failed")
    finally:
        try:
            _POOL.release()
        except ValueError:
            pass

    ext = "docx" if media_type.endswith("document") else "pdf"
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition":
                f'attachment; filename="schutzbrief-{payload.template}.{ext}"',
            # PII inside — never cache anywhere
            "Cache-Control": "no-store",
        },
    )
