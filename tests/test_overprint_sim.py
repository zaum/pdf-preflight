"""Test overprint simulation in separation channels."""
import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import fitz
import numpy as np
from viewer.render_engine import _disable_overprint, _restore_overprint

# Create PDF with overprint using the resource test PDF
# We'll create a proper PDF with ExtGState for overprint
doc = fitz.open()
page = doc.new_page(width=200, height=200)

# First, let's try using PyMuPDF's built-in approach for ExtGState
# We need to add /ExtGState to the page's Resources dictionary
# And then use /GS1 gs in the content stream

# Get the page xref to add resources
page_xref = page.xref

# Create ExtGState object with overprint enabled
# ExtGState: << /Type /ExtGState /OP true /op true /OPM 1 >>
gs_obj = b"<<\n  /Type /ExtGState\n  /OP true\n  /op true\n  /OPM 1\n>>\n"
gs_xref = doc.get_new_xref()
doc.update_object(gs_xref, gs_obj.decode('latin-1'))

# Add the ExtGState to the page's resource dictionary
# The resource dict is at xref 3 (typically for a new page)
res_xref = None
page_obj = doc.xref_object(page_xref)
for line in page_obj.split('\n'):
    if '/Resources' in line:
        parts = line.split()
        for p in parts:
            clean = p.strip().rstrip('R').strip()
            if clean.isdigit():
                res_xref = int(clean)
                break

if res_xref is None:
    print("ERROR: Cannot find Resources xref")
    doc.close()
    sys.exit(1)

# Modify the Resources dict to include ExtGState
res_obj = doc.xref_object(res_xref)
if '/ExtGState' not in res_obj:
    # Add ExtGState to the dict
    new_res = res_obj.rstrip('>>').rstrip() + '\n  /ExtGState << /GS1 ' + str(gs_xref) + ' 0 R >>\n>>'
    doc.update_object(res_xref, new_res)

# Now add content that uses overprint
xrefs = page.get_contents()
if not xrefs:
    # Insert some content to create a content stream
    page.insert_text(fitz.Point(10, 10), '.', fontsize=1, color=(1, 1, 1))
    xrefs = page.get_contents()

existing = doc.xref_stream(xrefs[0])

overprint_content = b"""
q
% Magenta rectangle (bottom - no overprint)
0 1 0 0 k
20 20 120 160 re f
Q
q
% Cyan rectangle (top - WITH overprint)
/GS1 gs
1 0 0 0 k
60 60 100 100 re f
Q
"""

combined = existing + overprint_content
doc.update_stream(xrefs[0], combined)

tmp = os.path.join(tempfile.gettempdir(), '_op_sim.pdf')
doc.save(tmp, deflate=True)
doc.close()

# ===== TEST: Render with and without overprint simulation =====
doc = fitz.open(tmp)
page = doc[0]

# Render WITH overprint (default)
fitz.TOOLS.set_icc(0)
mat = fitz.Matrix(3, 3)
pix = page.get_pixmap(matrix=mat, colorspace=fitz.csCMYK)
arr_with = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 4)

# Overlap region: cyan (60,60)-(160,160) over magenta (20,20)-(140,180)
# Overlap: x=60..140, y=60..160 (bottom-up coords)
# Page height=200, zoom=3
# Check overlap center at (100, 110) bottom-up
# Pixel Y = (200 - 110) * 3 = 270
# Pixel X = 100 * 3 = 300
px = int(100 * 3)
py = int((200 - 110) * 3)
print(f"WITH overprint (overlap area at {100},{110}):")
print(f"  C={arr_with[py,px,0]} M={arr_with[py,px,1]} Y={arr_with[py,px,2]} K={arr_with[py,px,3]}")
print(f"  Expect: C=255 M=255 (cyan overprint on magenta)")

# Check non-overlap cyan area (140, 140)
px2 = int(140 * 3)
py2 = int((200 - 140) * 3)
print(f"WITH overprint (cyan-only at {140},{140}):")
print(f"  C={arr_with[py2,px2,0]} M={arr_with[py2,px2,1]} Y={arr_with[py2,px2,2]} K={arr_with[py2,px2,3]}")

# Check non-overlap magenta area (40, 80)
px3 = int(40 * 3)
py3 = int((200 - 80) * 3)
print(f"WITH overprint (magenta-only at {40},{80}):")
print(f"  C={arr_with[py3,px3,0]} M={arr_with[py3,px3,1]} Y={arr_with[py3,px3,2]} K={arr_with[py3,px3,3]}")

# Render WITHOUT overprint
modified = _disable_overprint(doc)
pix2 = page.get_pixmap(matrix=mat, colorspace=fitz.csCMYK)
_restore_overprint(doc, modified)
arr_without = np.frombuffer(pix2.samples, dtype=np.uint8).reshape(pix2.height, pix2.width, 4)

print(f"\nWITHOUT overprint (overlap area):")
print(f"  C={arr_without[py,px,0]} M={arr_without[py,px,1]} Y={arr_without[py,px,2]} K={arr_without[py,px,3]}")
print(f"  Expect: C=255 M=0 (cyan knocks out magenta)")

# Verify
with_op_correct = arr_with[py, px, 0] > 200 and arr_with[py, px, 1] > 200
without_op_correct = arr_without[py, px, 0] > 200 and arr_without[py, px, 1] < 20

print(f"\nOverprint simulation correct: {with_op_correct}")
print(f"Knockout simulation correct: {without_op_correct}")
print(f"\nTest {'PASSED' if with_op_correct and without_op_correct else 'FAILED'}!")

fitz.TOOLS.set_icc(1)
doc.close()
os.unlink(tmp)

if not (with_op_correct and without_op_correct):
    sys.exit(1)
