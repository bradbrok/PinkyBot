---
name: xlsx
description: Use this skill any time a spreadsheet file is the primary input or output. This means any task where the user wants to: open, read, edit, or fix an existing .xlsx, .xlsm, .csv, or .tsv file (e.g., adding columns, computing formulas, formatting, charting, cleaning messy data); create a new spreadsheet from scratch or from other data sources; or convert between tabular file formats. Trigger especially when the user references a spreadsheet file by name or path — even casually (like "the xlsx in my downloads") — and wants something done to it or produced from it.
---

# XLSX Skill

## Output Requirements

### All Excel Files
- Professional font (Arial, Times New Roman) consistently
- Zero formula errors (#REF!, #DIV/0!, #VALUE!, #N/A, #NAME?)
- Preserve existing template conventions (they ALWAYS override these guidelines)

### Financial Models
**Color Coding (industry standard)**:
- Blue text (RGB: 0,0,255): Hardcoded inputs users will change
- Black text: ALL formulas and calculations
- Green text (RGB: 0,128,0): Links from other worksheets in same workbook
- Red text: External links to other files
- Yellow background: Key assumptions needing attention

**Number Formatting**:
- Years: Text strings ("2024" not "2,024")
- Currency: `$#,##0` with units in headers ("Revenue ($mm)")
- Zeros: Format as "-" including percentages
- Percentages: `0.0%` format (one decimal)
- Negatives: Parentheses (123) not minus -123

## CRITICAL: Use Formulas, Not Hardcoded Values

```python
# WRONG — hardcoding calculated values
sheet['B10'] = df['Sales'].sum()  # BAD

# CORRECT — use Excel formulas
sheet['B10'] = '=SUM(B2:B9)'  # GOOD
sheet['C5'] = '=(C4-C2)/C2'   # Growth rate
sheet['D20'] = '=AVERAGE(D2:D19)'
```

## Common Workflow
1. **Choose tool**: pandas for data analysis; openpyxl for formulas/formatting
2. **Create/Load**: New workbook or load existing
3. **Modify**: Add data, formulas, formatting
4. **Save**: Write to file
5. **Recalculate (MANDATORY if using formulas)**:
   ```bash
   python scripts/recalc.py output.xlsx
   ```
6. **Verify and fix errors** (check JSON output for #REF!, #DIV/0!, etc.)

## openpyxl Patterns
```python
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment

# Create
wb = Workbook()
sheet = wb.active
sheet['A1'] = 'Hello'
sheet['B2'] = '=SUM(A1:A10)'
sheet['A1'].font = Font(bold=True, color='FF0000')
sheet.column_dimensions['A'].width = 20
wb.save('output.xlsx')

# Load existing
wb = load_workbook('existing.xlsx')
# WARNING: data_only=True loses formulas if saved
```

## pandas Patterns
```python
import pandas as pd
df = pd.read_excel('file.xlsx')
all_sheets = pd.read_excel('file.xlsx', sheet_name=None)
df.to_excel('output.xlsx', index=False)
```

## Formula Verification Checklist
- Test 2-3 sample references before building full model
- Check column mapping (Excel col 64 = BL, not BK)
- Remember 1-indexed rows (DataFrame row 5 = Excel row 6)
- Check for NaN with `pd.notna()`
- Verify denominators before `/` in formulas (#DIV/0!)
- Use correct cross-sheet format: `Sheet1!A1`

## recalc.py Output
```json
{"status": "success", "total_errors": 0, "total_formulas": 42}
{"status": "errors_found", "error_summary": {"#REF!": {"count": 2, "locations": ["Sheet1!B5"]}}}
```

## Dependencies
- pandas, openpyxl, LibreOffice (for recalc)