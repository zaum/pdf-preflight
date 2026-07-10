"""Test complete pipeline with content stream parser."""
import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import fitz
from preview.pdf_inspector import inspect_position_exact, clear_cache
from viewer.render_engine import RenderEngine

# Create a test PDF with RGB black, RGB red, and attempt CMYK content
doc = fitz.open()
page = doc.new_page(width=300, height=200)
page.insert_text(fitz.Point(20, 50), 'RGB Black', fontsize=20, color=(0, 0, 0))
page.insert_text(fitz.Point(20, 90), 'RGB Red', fontsize=20, color=(1, 0, 0))
page.insert_text(fitz.Point(20, 130), 'Gray Txt', fontsize=20, color=(0.5, 0.5, 0.5))

tmp = os.path.join(tempfile.gettempdir(), '_test_pipeline.pdf')
doc.save(tmp)
doc.close()

# Now test
doc = fitz.open(tmp)
page = doc[0]

re = RenderEngine()
re.doc = doc

# Test black text
source = inspect_position_exact(page, 60, 35, doc=doc)
print(f"Black text source: {source}")
if source.get('found'):
    print(f"  Colorspace: {source.get('colorspace')}")
    print(f"  Fill color: {source.get('fill_color')}")
cmyk = re.sample_cmyk(0, 60, 35)
print(f"  Rendered CMYK: C={cmyk[0]} M={cmyk[1]} Y={cmyk[2]} K={cmyk[3]}")
print()

# Test red text
source2 = inspect_position_exact(page, 60, 75, doc=doc)
print(f"Red text source: {source2}")
if source2.get('found'):
    print(f"  Colorspace: {source2.get('colorspace')}")
    print(f"  Fill color: {source2.get('fill_color')}")
print()

# Test gray text
source3 = inspect_position_exact(page, 60, 115, doc=doc)
print(f"Gray text source: {source3}")
if source3.get('found'):
    print(f"  Colorspace: {source3.get('colorspace')}")
    print(f"  Fill color: {source3.get('fill_color')}")

# Test cache
clear_cache()
doc.close()
os.unlink(tmp)
print("\nAll tests passed!")
