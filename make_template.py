"""Creates product_input_template.xlsx - the sheet you fill in (name, colour, sizes, price, photo file name)."""
import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

cols = ["Photo", "Sizes", "Price", "Compare At Price", "Stock"]
rows = [
    ["yellow-ikat.jpg", "M, L, XL", 290, 399, 10],
    ["maroon-floral.jpg", "L, XL, XXL", 360, 499, 10],
]
out = "product_input_template.xlsx"
pd.DataFrame(rows, columns=cols).to_excel(out, index=False, sheet_name="Products")
wb = load_workbook(out)
ws = wb.active
for i, c in enumerate(cols, 1):
    cell = ws.cell(1, i)
    cell.font = Font(bold=True, color="FFFFFF")
    cell.fill = PatternFill("solid", fgColor="1F4E78" if c in ("Photo", "Sizes", "Price") else "7F7F7F")
    ws.column_dimensions[get_column_letter(i)].width = max(14, len(c) + 4)
ws.column_dimensions["A"].width = 26
n = wb.create_sheet("Read me")
for line in [
    "You only fill Photo, Sizes and Price. AI works out title, description, type, colour, gender, tags, category, weight and SEO from the photo.",
    "Photo: the file name of the picture you upload with this sheet. Use commas for several photos of one product (front, back).",
    "Sizes: separate with commas, e.g. M, L, XL (one variant per size, same price).",
    "Optional extra columns you can add: Name, Color, Type, Gender, Notes, SKU. Whatever you type is kept; AI fills only the blanks.",
]:
    n.append([line])
n.column_dimensions["A"].width = 120
wb.save(out)
print("saved", out)
