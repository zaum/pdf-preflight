"""Test: Does MuPDF apply overprint during CMYK rendering?"""
import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import fitz
import numpy as np

# Create a PDF with overprint: cyan rect on TOP of magenta rect, cyan has overprint fill
doc = fitz.open()
page = doc.new_page(width=200, height=200)

# Build PDF with overprint
# ExtGState for overprint
page_objects = []
# Add ExtGState with overprint
content = b"""% Draw magenta rectangle (bottom)
q
0 1 0 0 k
20 20 120 160 re f
Q
% Draw cyan rectangle (top, with overprint fill)
q
/GS1 gs
1 0 0 0 k
60 60 160 100 re f
Q
"""

# We need to add the ExtGState to page resources
# First, insert some text to create font resource
page.insert_text(fitz.Point(10, 10), '.', fontsize=1, color=(1, 1, 1))

# Now add ExtGState to page
xref = page.xref
page_obj = doc.xref_object(xref)
print(f"Page object before: {page_obj[:200]}")

# Add /ExtGState to resources
if '/Resources' not in page_obj:
    print("No Resources in page object!")

# Read existing content
existing_xrefs = page.get_contents()
existing = doc.xref_stream(existing_xrefs[0])
combined = existing + content

doc.update_stream(existing_xrefs[0], combined)

tmp = os.path.join(tempfile.gettempdir(), '_optest.pdf')
doc.save(tmp)
doc.close()

# Render and check
doc = fitz.open(tmp)
page = doc[0]

# Render to CMYK WITH overprint (MuPDF default)
mat = fitz.Matrix(2, 2)
pix_cmyk = page.get_pixmap(matrix=mat, colorspace=fitz.csCMYK)
arr = np.frombuffer(pix_cmyk.samples, dtype=np.uint8).reshape(pix_cmyk.height, pix_cmyk.width, 4)

# Check the overlap area: cyan rect (60,60)-(160,100) overlaps magenta rect (20,20)-(120,160)
# Overlap region: x=60..120, y=60..100
# At (90, 80): this should show cyan (if overprint is applied - cyan printed on magenta)
#           or cyan (if knockout - cyan replaces magenta)

# Convert to pixel coords: page height=200, zoom=2
# Top-down: y_px = (200 - y_pdf) * 2
# Overlap center: x=90, y_pdf=80 (bottom-up) → y_px = (200-80)*2 = 240

cy = int((200 - 80) * 2)  # Y in pixmap
cx = int(90 * 2)           # X in pixmap
print(f"\nRendered CMYK at overlap center (should be cyan if knockout, cyan+magenta if overprint):")
print(f"  C={arr[cy, cx, 0]} M={arr[cy, cx, 1]} Y={arr[cy, cx, 2]} K={arr[cy, cx, 3]}")

# Check pure cyan area (no overlap): x=140, y=80
cy2 = int((200 - 80) * 2)
cx2 = int(140 * 2)
print(f"\nRendered CMYK in cyan-only area:")
print(f"  C={arr[cy2, cx2, 0]} M={arr[cy2, cx2, 1]} Y={arr[cy2, cx2, 2]} K={arr[cy2, cx2, 3]}")

# Check pure magenta area (no overlap): x=40, y=80
cx3 = int(40 * 2)
print(f"\nRendered CMYK in magenta-only area:")
print(f"  C={arr[cy2, cx3, 0]} M={arr[cy2, cx3, 1]} Y={arr[cy2, cx3, 2]} K={arr[cy2, cx3, 3]}")

# Also check what the text looks like
for xri in page.get_contents():
    s = doc.xref_stream(xri)
    print(f"\nContent stream: {s[:300]}")

doc.close()
os.unlink(tmp)
