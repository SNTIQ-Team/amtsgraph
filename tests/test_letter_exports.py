from io import BytesIO

from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfReader

from api import docx_render
from api.server import server


def test_downloaded_letters_preserve_the_postal_address_without_a_converter(monkeypatch):
    monkeypatch.setattr(docx_render, "docx_to_pdf", lambda _: None)
    recipient = ["Sozialamt Musterstadt", "Postfach 12345", "12345 Musterstadt"]
    letter = {
        "template": "letter",
        "sender": ["Alex Muster"],
        "recipient": recipient,
        "date": "18.11.2026",
        "subject": "Übersetzungskosten",
        "body": "Ich beantrage die Erstattung.",
    }
    with TestClient(server) as client:
        docx = client.post("/pdf/letter", json={**letter, "format": "docx"})
        pdf = client.post("/pdf/letter", json={**letter, "format": "pdf"})
    assert docx.status_code == pdf.status_code == 200
    assert docx.headers["content-disposition"].endswith('.docx"')
    assert pdf.headers["content-disposition"].endswith('.pdf"')
    word = Document(BytesIO(docx.content))
    word_text = " ".join(word.element.body.itertext())
    pdf_text = " ".join(page.extract_text() for page in PdfReader(BytesIO(pdf.content)).pages)
    for line in [*recipient, letter["subject"], letter["body"]]:
        assert line in word_text
        assert line in pdf_text
