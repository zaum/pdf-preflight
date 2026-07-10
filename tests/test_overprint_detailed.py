"""Detailed overprint simulation test."""
import sys, os, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import fitz

doc = fitz.open(r'I:\PDF preflight\resources\test_overprint.pdf')
page = doc[0]

fitz.TOOLS.set_icc(0)
mat = fitz.Matrix(1, 1)
pix = page.get_pixmap(matrix=mat, colorspace=fitz.csCMYK)
arr_base = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 4)

from preview.overprint import simulate_overprint_on_cmyk, _parse_content_sequence

ops = _parse_content_sequence(doc, page)
print(f"Total ops: {len(ops)}")
for i, op in enumerate(ops[:8]):
    print(f"  [{i}] {op['type']} fill={op.get('fill_color')} stroke={op.get('stroke_color')} "
          f"op_fill={op.get('overprint_fill')} op_stroke={op.get('overprint_stroke')}")

drawings = page.get_cdrawings()
print(f"\nDrawings: {len(drawings)}")
for i, d in enumerate(drawings[:5]):
    r = d.get('rect')
    print(f"  [{i}] type={d.get('type')} rect=({r[0]:.0f},{r[1]:.0f},{r[2]:.0f},{r[3]:.0f}) "
          f"fill={d.get('fill')} stroke={d.get('color')}")

arr_op = simulate_overprint_on_cmyk(arr_base, page, doc)

# Page height=842. Red rect: (50,592,200,200) bottom-up → top-down y0=250, y1=50
print("\nRed rect (overprint) vs base comparison:")
for y in [80, 150, 230]:
    for x in [80, 150, 230]:
        b = arr_base[y, x]
        o = arr_op[y, x]
        if (b != o).any():
            print(f"  pixel({x},{y}) base: C={b[0]} M={b[1]} Y={b[2]} K={b[3]} -> "
                  f"op: C={o[0]} M={o[1]} Y={o[2]} K={o[3]}  *** CHANGED")
        else:
            print(f"  pixel({x},{y}) base: C={b[0]} M={b[1]} Y={b[2]} K={b[3]} (unchanged)")

# Check if the base rendering already had overprint applied
print(f"\nBase rendering max per channel: C={arr_base[:,:,0].max()} M={arr_base[:,:,1].max()} "
      f"Y={arr_base[:,:,2].max()} K={arr_base[:,:,3].max()}")
print(f"Overprint render max per channel: C={arr_op[:,:,0].max()} M={arr_op[:,:,1].max()} "
      f"Y={arr_op[:,:,2].max()} K={arr_op[:,:,3].max()}")

diff = (arr_op.astype(np.int32) - arr_base.astype(np.int32))
changed = (diff != 0).any(axis=2)
print(f"\nTotal pixels changed: {changed.sum()} / {changed.size}")

doc.close()
