import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import fitz
from preview.overprint import build_overprint_position_map, clear_overprint_cache

doc = fitz.open(r'I:\PDF preflight\uj.pdf')
page = doc[0]
m = build_overprint_position_map(doc, page)

# Check black rect position (254,287)-(375,380), center at 315,333
print(f"Total map entries: {len(m)}")
matches = [e for e in m if e['bbox'][0] <= 315 <= e['bbox'][2] and e['bbox'][1] <= 333 <= e['bbox'][3]]
print(f"Entries containing black rect center (315,333): {len(matches)}")
for e in matches:
    print(f"  bbox=({e['bbox'][0]:.0f},{e['bbox'][1]:.0f},{e['bbox'][2]:.0f},{e['bbox'][3]:.0f}) "
          f"fill={e['op_fill']} stroke={e['op_stroke']}")

# Show ALL map entries to find the problem
print("\nAll 48 entries:")
for i, e in enumerate(m):
    print(f"  [{i}] ({e['bbox'][0]:.0f},{e['bbox'][1]:.0f},{e['bbox'][2]:.0f},{e['bbox'][3]:.0f}) "
          f"fill={e['op_fill']} stroke={e['op_stroke']}")

clear_overprint_cache()
doc.close()
