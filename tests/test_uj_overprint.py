"""Test overprint detection with uj.pdf."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import fitz
from preview.overprint import _get_extgstate_overprints, _parse_content_sequence

doc = fitz.open(r'I:\PDF preflight\uj.pdf')
page = doc[0]

gs = _get_extgstate_overprints(doc, page)
print(f"Overprint ExtGStates found:")
for name, (op_fill, op_stroke) in sorted(gs.items()):
    print(f"  /{name}: fill_op={op_fill}, stroke_op={op_stroke}")

ops = _parse_content_sequence(doc, page)
print(f"\nParsed {len(ops)} operations")
overprint_ops = [op for op in ops if op.get('overprint_fill') or op.get('overprint_stroke')]
print(f"Overprint operations: {len(overprint_ops)}")
for i, op in enumerate(overprint_ops[:10]):
    print(f"  [{i}] type={op['type']} fill_op={op.get('overprint_fill')} "
          f"stroke_op={op.get('overprint_stroke')} "
          f"fill={op.get('fill_color')} stroke={op.get('stroke_color')}")

doc.close()
