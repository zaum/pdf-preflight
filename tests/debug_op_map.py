"""Debug overprint map building."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import fitz
from preview.overprint import _parse_content_sequence, _get_extgstate_overprints

doc = fitz.open(r'I:\PDF preflight\uj.pdf')
page = doc[0]

ops = _parse_content_sequence(doc, page)
print(f"Total ops: {len(ops)}")
for i, op in enumerate(ops):
    fc = op.get('fill_color')
    sc = op.get('stroke_color')
    has_color = fc is not None or sc is not None
    print(f"  [{i}] {op['type']} fill_op={op.get('overprint_fill')} stroke_op={op.get('overprint_stroke')} "
          f"color={has_color} fill={fc} stroke={sc}")

drawings = page.get_cdrawings()
print(f"\nDrawings: {len(drawings)}")

# Manually check correlation
print("\nPath ops vs drawings:")
path_idx = 0
for i, op in enumerate(ops):
    if op['type'].startswith('path'):
        if path_idx < len(drawings):
            d = drawings[path_idx]
            r = d.get('rect')
            fc = op.get('fill_color')
            sc = op.get('stroke_color')
            print(f"  path_op[{i}] -> drawing[{path_idx}] rect=({r[0]:.0f},{r[1]:.0f},{r[2]:.0f},{r[3]:.0f}) "
                  f"fill={fc is not None} stroke={sc is not None} "
                  f"op_fill={op.get('overprint_fill')} op_stroke={op.get('overprint_stroke')}")
        path_idx += 1

doc.close()
