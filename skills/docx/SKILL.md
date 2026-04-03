---
name: docx
description: Use this skill whenever the user wants to create, read, edit, or manipulate Word documents (.docx files). Triggers include: any mention of 'Word doc', 'word document', '.docx', or requests to produce professional documents with formatting like tables of contents, headings, page numbers, or letterheads. Also use when extracting or reorganizing content from .docx files, inserting or replacing images in documents, performing find-and-replace in Word files, working with tracked changes or comments, or converting content into a polished Word document. If the user asks for a 'report', 'memo', 'letter', 'template', or similar deliverable as a Word or .docx file, use this skill.
---

# DOCX creation, editing, and analysis

## Quick Reference
| Task | Approach |
|------|----------|
| Read/analyze content | `pandoc` or unpack for raw XML |
| Create new document | Use `docx-js` |
| Edit existing document | Unpack → edit XML → repack |

## Converting .doc to .docx
```bash
python scripts/office/soffice.py --headless --convert-to docx document.doc
```

## Reading Content
```bash
pandoc --track-changes=all document.docx -o output.md
python scripts/office/unpack.py document.docx unpacked/
```

## Creating New Documents (docx-js)
Install: `npm install -g docx`

### Critical Rules
- **Set page size explicitly**: US Letter = 12240 x 15840 DXA (docx-js defaults to A4)
- **Landscape**: pass portrait dimensions + `orientation: PageOrientation.LANDSCAPE`
- **Never use `\n`**: use separate Paragraph elements
- **Never use unicode bullets**: use `LevelFormat.BULLET` with numbering config
- **PageBreak must be in Paragraph**
- **ImageRun requires `type`**: always specify png/jpg/etc
- **Tables need dual widths**: `columnWidths` array AND cell `width`
- **Always use `WidthType.DXA`**: never `WidthType.PERCENTAGE` (breaks Google Docs)
- **Use `ShadingType.CLEAR`**: never SOLID for table shading
- **Override built-in styles**: use exact IDs "Heading1", "Heading2" etc
- **Include `outlineLevel`**: required for TOC (0 for H1, 1 for H2)

### Validation
```bash
python scripts/office/validate.py doc.docx
```

## Editing Existing Documents
1. Unpack: `python scripts/office/unpack.py document.docx unpacked/`
2. Edit XML directly using Edit tool (not scripts)
   - Use "Claude" as author for tracked changes/comments
   - Use smart quotes: `&#x2018;` `&#x2019;` `&#x201C;` `&#x201D;`
   - Use `comment.py` for adding comments
3. Pack: `python scripts/office/pack.py unpacked/ output.docx --original document.docx`

## XML Tracked Changes
```xml
<w:ins w:id="1" w:author="Claude" w:date="2025-01-01T00:00:00Z">
  <w:r><w:t>inserted text</w:t></w:r>
</w:ins>
<w:del w:id="2" w:author="Claude" w:date="2025-01-01T00:00:00Z">
  <w:r><w:delText>deleted text</w:delText></w:r>
</w:del>
```

## Dependencies
- pandoc, docx (`npm install -g docx`), LibreOffice, Poppler