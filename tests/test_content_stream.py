"""Test the content stream parser with a CMYK PDF."""
import sys
import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import fitz
from preview.content_stream import _tokenize, PageColorExtractor, find_text_color_at


def test_tokenizer():
    """Test the tokenizer with a simple content stream."""
    stream = b"""
    BT
    /F1 12 Tf
    0 0 0 1 k
    100 700 Td
    (Hello World) Tj
    ET
    """
    tokens = _tokenize(stream)
    print("Tokens:", tokens)
    assert 'k' in tokens, "Should have 'k' operator"
    assert 'Tj' in tokens, "Should have 'Tj' operator"
    assert '(Hello World)' in tokens, "Should have string token"
    print("Tokenizer test PASSED")


def test_with_pdf():
    """Test with a PDF containing CMYK black and RGB black text."""
    doc = fitz.open()
    page = doc.new_page(width=300, height=200)

    # Draw RGB black text
    page.insert_text(fitz.Point(20, 60), 'RGB B', fontsize=20, color=(0, 0, 0))

    # Draw RGB red text
    page.insert_text(fitz.Point(20, 100), 'RGB R', fontsize=20, color=(1, 0, 0))

    # Save and reload
    tmp = os.path.join(tempfile.gettempdir(), '_test_cs_parser.pdf')
    doc.save(tmp)
    doc.close()

    doc = fitz.open(tmp)
    page = doc[0]

    extractor = PageColorExtractor(doc)
    recorded = extractor.extract_page_colors(0)

    print(f"\nRecorded {len(recorded)} color entries:")
    for i, rec in enumerate(recorded):
        print(f"  [{i}] type={rec.get('type')}, fill_cs={rec.get('fill_cs')}, "
              f"fill_color={rec.get('fill_color')}, y_pdf={rec.get('y_pdf')}")

    # Find text at the black text position
    result = find_text_color_at(recorded, page, 60, 50)
    print(f"\nBlack text at (60, 50): {result}")

    if result.get('found'):
        print(f"  Colorspace: {result['colorspace']}")
        print(f"  Fill color: {result['fill_color']}")
        colorspace = result['colorspace']
        color = result['fill_color']
        if colorspace == 'DeviceCMYK':
            c, m, y, k = color
            print(f"  CMYK: C={c*100:.0f}% M={m*100:.0f}% Y={y*100:.0f}% K={k*100:.0f}%")
        elif colorspace == 'DeviceRGB':
            r, g, b = color
            print(f"  RGB: R={r*255:.0f} G={g*255:.0f} B={b*255:.0f}")
        elif colorspace == 'DeviceGray':
            print(f"  Gray: {color[0]*100:.0f}%")

    # Find text at the red text position
    result2 = find_text_color_at(recorded, page, 60, 90)
    print(f"\nRed text at (60, 90): {result2}")

    doc.close()
    os.unlink(tmp)
    print("\nTest completed")


if __name__ == "__main__":
    test_tokenizer()
    test_with_pdf()
