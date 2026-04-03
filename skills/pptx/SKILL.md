---
name: pptx
description: Use this skill any time a .pptx file is involved in any way — as input, output, or both. This includes: creating slide decks, pitch decks, or presentations; reading, parsing, or extracting text from any .pptx file (even if the extracted content will be used elsewhere, like in an email or summary); editing, modifying, or updating existing presentations; combining or splitting slide files; working with templates, layouts, speaker notes, or comments. Trigger whenever the user mentions "deck," "slides," "presentation," or references a .pptx filename, regardless of what they plan to do with the content afterward.
---

# PPTX Skill

## Quick Reference
| Task | Guide |
|------|-------|
| Read/analyze content | `python -m markitdown presentation.pptx` |
| Edit or create from template | Read editing.md |
| Create from scratch | Read pptxgenjs.md |

## Reading
```bash
python -m markitdown presentation.pptx
python scripts/thumbnail.py presentation.pptx
python scripts/office/unpack.py presentation.pptx unpacked/
```

## Design Guidelines

**Every slide needs a visual element** — image, chart, icon, or shape. Text-only slides are forgettable.

**Pick a bold color palette** specific to the topic. Use dominance: one color 60-70%, 1-2 supporting, one sharp accent. "Sandwich": dark title+conclusion, light content. OR dark throughout.

**Commit to a visual motif**: rounded image frames, icons in colored circles, thick single-side borders. Carry across every slide.

**Font pairing** — avoid Arial defaults:
| Header | Body |
|--------|------|
| Georgia | Calibri |
| Arial Black | Arial |
| Cambria | Calibri |

**Slide title**: 36-44pt bold. Body: 14-16pt. Leave 0.5" margins, 0.3-0.5" between blocks.

## AVOID (Common Mistakes)
- Same layout repeated
- Centering body text
- Defaulting to blue
- Text-only slides
- **NEVER use accent lines under titles** — hallmark of AI-generated slides
- Low-contrast elements
- Leftover placeholder text

## QA (Required)

Check for leftover placeholders:
```bash
python -m markitdown output.pptx | grep -iE "xxxx|lorem|ipsum|this.*(page|slide).*layout"
```

**Visual QA — use subagents with fresh eyes.** Convert to images:
```bash
python scripts/office/soffice.py --headless --convert-to pdf output.pptx
pdftoppm -jpeg -r 150 output.pdf slide
```

Check for: overlapping elements, text overflow, elements too close, uneven gaps, low-contrast text/icons, leftover placeholders.

**Verification loop**: Generate → inspect → fix → re-verify. At least one fix-and-verify cycle before declaring success.

## Dependencies
- `pip install "markitdown[pptx]"` - text extraction
- `npm install -g pptxgenjs` - creating from scratch
- LibreOffice - PDF conversion
- Poppler (`pdftoppm`) - PDF to images