"""Test overprint position map with uj.pdf."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import fitz
from preview.overprint import check_overprint_at, build_overprint_position_map, clear_overprint_cache

doc = fitz.open(r'I:\PDF preflight\uj.pdf')
page = doc[0]

op_map = build_overprint_position_map(doc, page)
print(f"Overprint map entries: {len(op_map)}")
for e in op_map:
    print(f"  bbox=({e['bbox'][0]:.0f},{e['bbox'][1]:.0f},{e['bbox'][2]:.0f},{e['bbox'][3]:.0f}) "
          f"fill={e['op_fill']} stroke={e['op_stroke']}")

# Check at bottom color bar (y near 940 from top is near page bottom)
r = check_overprint_at(doc, page, 490, 940)
print(f"\nAt (490, 940): {r}")

# Check near registration mark
r2 = check_overprint_at(doc, page, 650, 470)
print(f"At (650, 470): {r2}")

clear_overprint_cache()
doc.close()
