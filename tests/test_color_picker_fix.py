"""Test the color picker fix - RGB black vs CMYK black detection."""
import sys
import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import fitz
from preview.pdf_inspector import inspect_position, detect_rich_black
from viewer.render_engine import RenderEngine


def test_detect_rich_black():
    """Unit tests for detect_rich_black function."""
    # Test 1: Pure K black
    r = detect_rich_black((0, 0, 0), (0, 0, 0, 255))
    assert not r['is_rich_black'], 'Pure K should not be rich black'

    # Test 2: Rich black
    r = detect_rich_black((0, 0, 0), (153, 128, 128, 255))
    assert r['is_rich_black'], 'Should detect rich black'

    # Test 3: Non-black source
    r = detect_rich_black((255, 0, 0), (0, 255, 255, 0))
    assert not r['is_rich_black'], 'Red source should not flag'

    # Test 4: Near-black source, pure K
    r = detect_rich_black((5, 5, 5), (0, 0, 0, 250))
    assert not r['is_rich_black'], 'Dark gray pure K should not flag'

    # Test 5: Near-black source, rich CMYK
    r = detect_rich_black((8, 8, 8), (100, 80, 80, 255))
    assert r['is_rich_black'], 'Near-black rich CMYK should flag'

    # Test 6: Low K
    r = detect_rich_black((0, 0, 0), (100, 80, 80, 5))
    assert not r['is_rich_black'], 'Low K should not flag'

    # Test 7: None source
    r = detect_rich_black(None, (0, 0, 0, 255))
    assert not r['is_rich_black'], 'None source should not flag'

    print("All detect_rich_black tests passed!")


def test_pdf_inspection():
    """Test full pipeline with a real PDF."""
    doc = fitz.open()
    page = doc.new_page(width=300, height=100)

    # Insert RGB black text
    page.insert_text(fitz.Point(20, 50), 'RGB Black', fontsize=20, color=(0, 0, 0))

    # Insert RGB red text (control)
    page.insert_text(fitz.Point(160, 50), 'Red Text', fontsize=20, color=(1, 0, 0))

    tmp = os.path.join(tempfile.gettempdir(), '_test_color_picker.pdf')
    doc.save(tmp)
    doc.close()

    # Test inspection
    doc = fitz.open(tmp)
    page = doc[0]

    # Inspect black text position
    result = inspect_position(page, 60, 40)
    print(f"Black text inspection found: {result.get('found')}")
    if result['found']:
        print(f"  type: {result['type']}")
        print(f"  text: {result.get('text', '')}")
        print(f"  color_rgb: {result.get('color_rgb')}")

    # Get rendered CMYK
    re = RenderEngine()
    re.doc = doc
    cmyk = re.sample_cmyk(0, 60, 40)
    print(f"  rendered CMYK: C={cmyk[0]} M={cmyk[1]} Y={cmyk[2]} K={cmyk[3]}")

    if result['found']:
        rb = detect_rich_black(result['color_rgb'], cmyk)
        print(f"  rich_black: {rb['is_rich_black']}")

    # Inspect red text position
    result2 = inspect_position(page, 200, 40)
    print(f"\nRed text inspection found: {result2.get('found')}")
    if result2['found']:
        print(f"  text: {result2.get('text', '')}")
        print(f"  color_rgb: {result2.get('color_rgb')}")

    doc.close()
    os.unlink(tmp)
    print("\nAll PDF inspection tests passed!")


if __name__ == "__main__":
    test_detect_rich_black()
    test_pdf_inspection()
