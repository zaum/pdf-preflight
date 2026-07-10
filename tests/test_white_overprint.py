"""Test overprint with white text on colored background."""
import sys, os, tempfile, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import fitz, re

# Create PDF: colored rect, then white text with overprint on top
doc = fitz.open()
page = doc.new_page(width=300, height=200)

# Insert placeholder to create font resource
page.insert_text(fitz.Point(10, 10), '.', fontsize=1, color=(1, 1, 1))

xrefs = page.get_contents()
existing = doc.xref_stream(xrefs[0]).decode('latin-1')
font_name = re.findall(r'/(\w+)\s+\d+\s+Tf', existing)[0]

# Add ExtGState with overprint to page resources
gs_obj = '<< /Type /ExtGState /OP true /op true /OPM 1 >>'
gs_xref = doc.get_new_xref()
doc.update_object(gs_xref, gs_obj)

page_obj = doc.xref_object(page.xref)
res_xref = None
for line in page_obj.split('\n'):
    if '/Resources' in line:
        parts = line.split()
        for p in parts:
            clean = p.strip().rstrip('R').strip()
            if clean.isdigit():
                res_xref = int(clean)
                break

if res_xref:
    res = doc.xref_object(res_xref)
    new_res = res.rstrip('>>').rstrip() + '\n  /ExtGState << /GS1 ' + str(gs_xref) + ' 0 R >>\n>>'
    doc.update_object(res_xref, new_res)

# Content: red rect, then white text (overprint) on top
content = f"""
q
% Red rect background (knockout)
0 1 1 0 k
30 30 200 140 re f
Q
q
% White text with overprint
/GS1 gs
BT
/{font_name} 20 Tf
0 0 0 0 k
1 0 0 1 50 100 Tm
(WHITE TEXT) Tj
ET
Q
"""
combined = (existing + content).encode('latin-1')
doc.update_stream(xrefs[0], combined)

tmp = os.path.join(tempfile.gettempdir(), '_white_overprint.pdf')
doc.save(tmp, deflate=True)
doc.close()

# Test
doc = fitz.open(tmp)
page = doc[0]

fitz.TOOLS.set_icc(0)
mat = fitz.Matrix(1, 1)
pix = page.get_pixmap(matrix=mat, colorspace=fitz.csCMYK)
arr_base = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 4)

from preview.overprint import simulate_overprint_on_cmyk, _parse_content_sequence

ops = _parse_content_sequence(doc, page)
print("Operations:")
for op in ops:
    print(f"  {op['type']} fill={op.get('fill_color')} op_fill={op.get('overprint_fill')}")

arr_op = simulate_overprint_on_cmyk(arr_base, page, doc)

# Check text area (red rect is at y=30-170 top-down, white text at baseline y=100 top-down)
# Text should be around y=80-120 top-down
print("\nText overlap area (should show RED background with overprint ON):")
page_h = page.rect.height
for y in [90, 100, 110]:
    for x in [70, 120, 180]:
        b = arr_base[y, x]
        o = arr_op[y, x]
        if (b != o).any():
            print(f"  ({x},{y}) base: C={b[0]} M={b[1]} Y={b[2]} K={b[3]} -> op: C={o[0]} M={o[1]} Y={o[2]} K={o[3]} ***")
        else:
            print(f"  ({x},{y}) base: C={b[0]} M={b[1]} Y={b[2]} K={b[3]} (ok)")

# Check BLACK plate ONLY (K channel)
print("\n--- Black plate (K channel) view ---")
print("Base (knockout): white text knocks out red background")
print("Overprint sim (ON): white text has K=0, should not appear")
for y in [90, 100, 110]:
    for x in [70, 120, 180]:
        b_k = arr_base[y, x, 3]
        o_k = arr_op[y, x, 3]
        status = "OK" if o_k == 0 else "WRONG"
        print(f"  ({x},{y}) base K={b_k} -> op K={o_k} {status}")

doc.close()
os.unlink(tmp)
