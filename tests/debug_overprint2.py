import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import fitz
from preview.overprint import build_overprint_position_map, clear_overprint_cache

doc = fitz.open(r'I:\PDF preflight\overprint.pdf')
page = doc[0]

dws = page.get_cdrawings()
print(f"Drawings: {len(dws)}")
for d in dws:
    r = d.get('rect')
    print(f"  rect={r} type={d.get('type')}")

m = build_overprint_position_map(doc, page)
print(f"\nMap entries: {len(m)}")
for e in m:
    print(f"  bbox={e['bbox']} fill={e['op_fill']} stroke={e['op_stroke']}")
    bw = e['bbox'][2] - e['bbox'][0]
    bh = e['bbox'][3] - e['bbox'][1]
    pw = page.rect.width
    ph = page.rect.height
    area_ratio = (bw * bh) / (pw * ph) * 100
    print(f"    area={bw:.0f}x{bh:.0f} = {area_ratio:.1f}% of page")

clear_overprint_cache()
doc.close()
