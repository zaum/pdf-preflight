"""Full integration test: inspect_position_exact -> analyze_source -> UI fields."""
import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import fitz, re
import numpy as np
from preview.pdf_inspector import inspect_position_exact, clear_cache
from preview.color_picker import ColorPicker
from viewer.render_engine import RenderEngine

doc = fitz.open()
page = doc.new_page(width=300, height=200)
page.insert_text(fitz.Point(20, 20), '.', fontsize=1, color=(1, 1, 1))

xrefs = page.get_contents()
existing = doc.xref_stream(xrefs[0]).decode('latin-1')
font_name = re.findall(r'/(\w+)\s+\d+\s+Tf', existing)[0]

cmyk_stream = (
    f'q BT /{font_name} 18 Tf 0 0 0 1 k 1 0 0 1 20 140 Tm (K100) Tj ET Q '
    f'q BT /{font_name} 18 Tf 1 0 0 0 k 1 0 0 1 20 100 Tm (C100) Tj ET Q '
    f'q BT /{font_name} 18 Tf 0 1 0 0 k 1 0 0 1 20 60 Tm (M100) Tj ET Q '
)
doc.update_stream(xrefs[0], (existing + cmyk_stream).encode('latin-1'))
tmp = os.path.join(tempfile.gettempdir(), '_integration.pdf')
doc.save(tmp, deflate=True)
doc.close()

doc = fitz.open(tmp)
page = doc[0]

re = RenderEngine()
re.doc = doc
picker = ColorPicker()

print("=" * 60)
print("Full pipeline test: click -> source inspection -> UI display")
print("=" * 60)

# Simulate clicks on each text
# Page height=200. Tm[5]=140,100,60 (bottom-up)
# top-down: y_td = 200-140=60, 200-100=100, 200-60=140
# bbox approx: 42-78, 82-118, 122-158
for label, x, y_td, expected_c, expected_m, expected_y, expected_k in [
    ("K100", 40, 55, 0, 0, 0, 100),
    ("C100", 40, 95, 100, 0, 0, 0),
    ("M100", 40, 135, 0, 100, 0, 0),
]:
    source = inspect_position_exact(page, x, y_td, doc=doc)
    cmyk_arr = np.array([0, 0, 0, 0], dtype=np.uint8)
    analysis = picker.analyze_source(source, cmyk_arr)

    print(f"\n{label} text at ({x},{y_td}):")
    print(f"  Source desc: {analysis['source_color_desc']}")
    print(f"  Rich black: {analysis['rich_black']}")
    print(f"  Warning: {analysis['warning']}")

    # Verify
    cs = source.get('colorspace', '')
    fill = source.get('fill_color', ())
    assert cs == 'DeviceCMYK', f"Expected DeviceCMYK, got {cs}"
    assert abs(fill[0] * 100 - expected_c) < 1, f"C mismatch: {fill[0]*100} vs {expected_c}"
    assert abs(fill[1] * 100 - expected_m) < 1, f"M mismatch"
    assert abs(fill[2] * 100 - expected_y) < 1, f"Y mismatch"
    assert abs(fill[3] * 100 - expected_k) < 1, f"K mismatch"

# Test rich black detection with DeviceRGB black
print("\n" + "=" * 60)
print("Rich black detection test")
print("=" * 60)

doc2 = fitz.open()
page2 = doc2.new_page(width=300, height=200)
page2.insert_text(fitz.Point(20, 60), 'RGB Black', fontsize=20, color=(0, 0, 0))
tmp2 = os.path.join(tempfile.gettempdir(), '_rgb_black.pdf')
doc2.save(tmp2)
doc2.close()

doc2 = fitz.open(tmp2)
page2 = doc2[0]
re2 = RenderEngine()
re2.doc = doc2

# Get text bbox to find correct y coordinate
td = page2.get_text("rawdict")
for block in td.get("blocks", []):
    if block.get("type") != 0:
        continue
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            bbox = span["bbox"]
            print(f"RGB text bbox: {bbox}, origin={span.get('origin')}")
            # Use midpoint of bbox for clicking
            mx = (bbox[0] + bbox[2]) / 2
            my = (bbox[1] + bbox[3]) / 2

            source2 = inspect_position_exact(page2, mx, my, doc=doc2)
            rendered_cmyk = re2.sample_cmyk(0, mx, my)
            print(f"Source: colorspace={source2.get('colorspace')}, fill={source2.get('fill_color')}")
            print(f"Rendered CMYK: C={rendered_cmyk[0]} M={rendered_cmyk[1]} "
                  f"Y={rendered_cmyk[2]} K={rendered_cmyk[3]}")
            analysis2 = picker.analyze_source(source2, rendered_cmyk)
            print(f"Source desc: {analysis2['source_color_desc']}")
            print(f"Rich black: {analysis2['rich_black']}")
            print(f"Warning: {analysis2['warning']}")

            assert source2['colorspace'] == 'DeviceRGB', \
                f"Expected DeviceRGB, got {source2.get('colorspace')}"
            assert source2['fill_color'] == (0, 0, 0), \
                f"Expected (0,0,0), got {source2.get('fill_color')}"
            break

clear_cache()
doc.close()
os.unlink(tmp)

clear_cache()
doc2.close()
os.unlink(tmp2)
print("\nAll integration tests passed!")
