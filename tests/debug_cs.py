"""Debug the content stream coordinate system."""
import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import fitz
from preview.content_stream import _tokenize, _to_str

doc = fitz.open()
page = doc.new_page(width=300, height=200)
page.insert_text(fitz.Point(20, 60), 'BLACK', fontsize=20, color=(0, 0, 0))
page.insert_text(fitz.Point(20, 100), 'RED', fontsize=20, color=(1, 0, 0))

tmp = os.path.join(tempfile.gettempdir(), '_debug_cs.pdf')
doc.save(tmp)
doc.close()

doc = fitz.open(tmp)
page = doc[0]

# Get raw content stream
xrefs = page.get_contents()
print(f"Content xrefs: {xrefs}")
for xri in xrefs:
    stream = doc.xref_stream(xri)
    stream_str = _to_str(stream)
    print(f"\n=== Content stream (xref {xri}) ===")
    print(stream_str[:2000])

# Get text dict
td = page.get_text("rawdict")
print(f"\n=== Text spans from rawdict ===")
for block in td.get("blocks", []):
    if block.get("type") != 0:
        continue
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            print(f"  text='{span.get('text')}' bbox={span['bbox']} "
                  f"origin={span.get('origin')} color={span.get('color'):#x}")

doc.close()
os.unlink(tmp)
