"""Explore get_cdrawings vs get_drawings and overprint info."""
import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import fitz
import json

# Check what get_cdrawings returns
doc = fitz.open()
page = doc.new_page(width=200, height=200)

# Draw a simple rectangle
page.draw_rect(fitz.Rect(50, 50, 150, 150), color=(1, 0, 0), fill=(0, 0, 1))

tmp = os.path.join(tempfile.gettempdir(), '_cdraw_test.pdf')
doc.save(tmp)
doc.close()

doc = fitz.open(tmp)
page = doc[0]

print("=== get_drawings() ===")
drawings = page.get_drawings()
for d in drawings:
    print(f"Keys: {list(d.keys())}")
    for k, v in d.items():
        if k != 'items':
            print(f"  {k}: {v}")
    print(f"  items count: {len(d.get('items', []))}")
    print()

print("=== get_cdrawings() ===")
cdrawings = page.get_cdrawings()
for d in cdrawings:
    print(f"Keys: {list(d.keys())}")
    for k, v in d.items():
        if k != 'items':
            print(f"  {k}: {v}")
    print(f"  items count: {len(d.get('items', []))}")
    print()

# Check with overprint PDF
print("=== Overprint PDF test ===")
doc2 = fitz.open()
page2 = doc2.new_page(width=200, height=200)

# Create content with overprint
content = b"""
q
/OP 1 /op 1 /OPM 1
0 1 0 0 k
50 50 150 150 re f
Q
q
0 0 0 1 k
80 80 180 180 re f
Q
"""
doc2.update_stream(page2.get_contents()[0], content)

tmp2 = os.path.join(tempfile.gettempdir(), '_op_test.pdf')
doc2.save(tmp2)
doc2.close()

doc2 = fitz.open(tmp2)
page2 = doc2[0]

print("get_drawings() on overprint PDF:")
drawings2 = page2.get_drawings()
for d in drawings2:
    print(f"  Keys: {list(d.keys())}")
    for k, v in d.items():
        if k != 'items':
            print(f"    {k}: {v}")

print("\nget_cdrawings() on overprint PDF:")
cdrawings2 = page2.get_cdrawings()
for d in cdrawings2:
    print(f"  Keys: {list(d.keys())}")
    for k, v in d.items():
        if k != 'items':
            print(f"    {k}: {v}")

doc.close()
doc2.close()
os.unlink(tmp)
os.unlink(tmp2)
