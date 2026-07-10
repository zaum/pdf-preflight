"""Verify CMYK matching with correct coordinates."""
import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import fitz, re
from preview.content_stream import PageColorExtractor, find_text_color_at

doc = fitz.open()
page = doc.new_page(width=300, height=200)
page.insert_text(fitz.Point(20, 20), '.', fontsize=1, color=(1, 1, 1))

xrefs = page.get_contents()
existing = doc.xref_stream(xrefs[0])
existing_str = existing.decode('latin-1') if isinstance(existing, bytes) else existing
fonts = re.findall(r'/(\w+)\s+\d+\s+Tf', existing_str)
font_name = fonts[0] if fonts else 'F0'

cmyk_stream = (
    f'q BT /{font_name} 18 Tf 0 0 0 1 k 1 0 0 1 20 140 Tm (K100) Tj ET Q '
    f'q BT /{font_name} 18 Tf 0 1 0 0 k 1 0 0 1 20 100 Tm (C100) Tj ET Q '
    f'q BT /{font_name} 18 Tf 0 0 0 0.5 k 1 0 0 1 20 60 Tm (K50) Tj ET Q'
)

doc.update_stream(xrefs[0], (existing_str + cmyk_stream).encode('latin-1'))
tmp = os.path.join(tempfile.gettempdir(), '_tc2.pdf')
doc.save(tmp, deflate=True)
doc.close()

doc = fitz.open(tmp)
page = doc[0]
extractor = PageColorExtractor(doc)
recorded = extractor.extract_page_colors(0)

td = page.get_text("rawdict")
spans = []
for block in td.get("blocks", []):
    if block.get("type") != 0:
        continue
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            spans.append(span)

print("Text spans by index:")
for i, s in enumerate(spans):
    print(f"  [{i}] text={s.get('text')!r} bbox={s['bbox']}")

print("\nText records by index:")
txt_recs = [r for r in recorded if r.get('type') == 'text']
for i, r in enumerate(txt_recs):
    cs = r['fill_cs']
    fill = r['fill_color']
    if cs == 'DeviceCMYK' and len(fill) >= 4:
        print(f"  [{i}] {cs} C={fill[0]*100:.0f}% M={fill[1]*100:.0f}% "
              f"Y={fill[2]*100:.0f}% K={fill[3]*100:.0f}%  y_pdf={r['y_pdf']}")
    else:
        print(f"  [{i}] {cs} {fill}  y_pdf={r['y_pdf']}")

# Correct Y coords (page height=200, top-down):
# Tm[5]=140 -> y_td=60 baseline, bbox ~42-78
# Tm[5]=100 -> y_td=100 baseline, bbox ~82-118
# Tm[5]=60 -> y_td=140 baseline, bbox ~122-158
print("\nLookup results:")
failed = 0
tests = [
    ("K100 (pure black)", 40, 55, "DeviceCMYK", [0, 0, 0, 1]),
    ("C100 (pure cyan)", 40, 95, "DeviceCMYK", [0, 1, 0, 0]),
    ("K50 (50% gray)", 40, 135, "DeviceCMYK", [0, 0, 0, 0.5]),
]
for label, x, y_td, exp_cs, exp_color in tests:
    result = find_text_color_at(recorded, page, x, y_td)
    if result.get('found'):
        cs = result['colorspace']
        fill = result['fill_color']
        ok = (cs == exp_cs and all(abs(fill[i] - exp_color[i]) < 0.01
                                    for i in range(len(exp_color))))
        status = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        if cs == 'DeviceCMYK' and len(fill) >= 4:
            print(f"  {status}: {label} -> {cs} C={fill[0]*100:.0f}% "
                  f"M={fill[1]*100:.0f}% Y={fill[2]*100:.0f}% K={fill[3]*100:.0f}%")
        else:
            print(f"  {status}: {label} -> {cs} {fill}")
    else:
        print(f"  FAIL: {label} -> NOT FOUND")
        failed += 1

doc.close()
os.unlink(tmp)

if failed:
    print(f"\n{failed} test(s) FAILED!")
    sys.exit(1)
else:
    print("\nAll tests PASSED!")
