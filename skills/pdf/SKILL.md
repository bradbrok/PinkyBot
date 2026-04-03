---
name: pdf
description: Use this skill whenever the user wants to do anything with PDF files. This includes reading or extracting text/tables from PDFs, combining or merging multiple PDFs into one, splitting PDFs apart, rotating pages, adding watermarks, creating new PDFs, filling PDF forms, encrypting/decrypting PDFs, extracting images, and OCR on scanned PDFs to make them searchable. If the user mentions a .pdf file or asks to produce one, use this skill.
---

# PDF Processing Guide

## Quick Start
```python
from pypdf import PdfReader, PdfWriter

reader = PdfReader("document.pdf")
text = "".join(page.extract_text() for page in reader.pages)
```

## Python Libraries

### pypdf - Basic Operations
- Merge, split, rotate, metadata, password protection
- `writer.add_page(page)`, `page.rotate(90)`, `reader.metadata`

### pdfplumber - Text and Table Extraction
```python
import pdfplumber
with pdfplumber.open("document.pdf") as pdf:
    tables = pdf.pages[0].extract_tables()
```

### reportlab - Create PDFs
```python
from reportlab.platypus import SimpleDocTemplate, Paragraph
from reportlab.lib.styles import getSampleStyleSheet
```

**IMPORTANT**: Never use Unicode subscript/superscript chars (₀₁₂₃) — use `<sub>` and `<super>` XML tags in Paragraph objects instead.

## Command-Line Tools
- `pdftotext -layout input.pdf output.txt`
- `qpdf --empty --pages file1.pdf file2.pdf -- merged.pdf`
- `qpdf input.pdf --pages . 1-5 -- pages1-5.pdf`

## Common Tasks
- **OCR scanned PDFs**: `pytesseract` + `pdf2image` → convert to images first
- **Watermark**: `page.merge_page(watermark)` with pypdf
- **Extract images**: `pdfimages -j input.pdf prefix`
- **Password protect**: `writer.encrypt("userpass", "ownerpass")`
- **Fill forms**: See FORMS.md

## Quick Reference
| Task | Best Tool |
|------|-----------|
| Merge PDFs | pypdf |
| Extract text | pdfplumber |
| Extract tables | pdfplumber |
| Create PDFs | reportlab |
| OCR scanned | pytesseract |
| Fill forms | pdf-lib or pypdf |

## Dependencies
- pypdf, pdfplumber, reportlab, pytesseract, pdf2image, qpdf, poppler-utils