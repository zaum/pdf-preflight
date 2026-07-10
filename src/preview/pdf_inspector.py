"""PDF object inspector — extracts source color information from PDF objects
at a given position.

Uses content stream parsing to get the EXACT color operator values
(DeviceCMYK, DeviceRGB, DeviceGray) as defined in the PDF, bypassing
the render pipeline entirely.

Also falls back to get_text('rawdict') and get_drawings() sRGB colors
for cases where content stream parsing doesn't cover (e.g. images).
"""

import fitz


# Cache: {page_xref: extracted_colors}
_CS_CACHE = {}
# Cache for expensive MuPDF high-level calls, keyed by page xref
_TEXT_CACHE = {}
_DRAW_CACHE = {}


def _get_or_parse_page(doc, page_num):
    """Get cached or freshly parsed color records for a page."""
    page = doc[page_num]
    key = (id(doc), page.xref)
    if key in _CS_CACHE:
        return _CS_CACHE[key]

    from preview.content_stream import PageColorExtractor
    extractor = PageColorExtractor(doc)
    recorded = extractor.extract_page_colors(page_num)
    _CS_CACHE[key] = recorded
    return recorded


def _get_text_dict(page):
    """Cached page.get_text('rawdict') — expensive, cache per page."""
    key = (id(page.parent) if hasattr(page, 'parent') else id(page),
           page.xref)
    cached = _TEXT_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        td = page.get_text("rawdict")
    except Exception:
        return None
    _TEXT_CACHE[key] = td
    return td


def _get_drawings(page):
    """Cached page.get_drawings() — expensive, cache per page."""
    key = (id(page.parent) if hasattr(page, 'parent') else id(page),
           page.xref)
    cached = _DRAW_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        dw = page.get_drawings()
    except Exception:
        return None
    _DRAW_CACHE[key] = dw
    return dw


def clear_cache():
    """Clear all inspector caches (call when changing documents)."""
    _CS_CACHE.clear()
    _TEXT_CACHE.clear()
    _DRAW_CACHE.clear()


def _srgb_int_to_tuple(srgb_int):
    """Convert MuPDF's sRGB color integer to (R,G,B) tuple 0-255."""
    return (
        (srgb_int >> 16) & 0xFF,
        (srgb_int >> 8) & 0xFF,
        srgb_int & 0xFF,
    )


def _is_essentially_black(rgb):
    """True if the RGB value is visually black (all channels near 0)."""
    return all(ch < 10 for ch in rgb)


def get_text_at_position(page, pdf_x, pdf_y):
    """Find text at a PDF coordinate and return its source color info (sRGB)."""
    td = _get_text_dict(page)
    if td is None:
        return {'found': False}

    for block in td.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                bbox = span["bbox"]
                if bbox[0] <= pdf_x <= bbox[2] and bbox[1] <= pdf_y <= bbox[3]:
                    srgb = span.get("color", 0)
                    return {
                        'found': True,
                        'type': 'text',
                        'text': span.get("text", ""),
                        'font': span.get("font", ""),
                        'size': span.get("size", 0),
                        'color_srgb': srgb,
                        'color_rgb': _srgb_int_to_tuple(srgb),
                        'bbox': bbox,
                    }
    return {'found': False}


def get_drawing_at_position(page, pdf_x, pdf_y):
    """Find a filled/stroked path at a PDF coordinate and return its color info (sRGB)."""
    drawings = _get_drawings(page)
    if drawings is None:
        return {'found': False}

    for d in reversed(drawings):
        r = d.get("rect")
        if r is None:
            continue
        if not (r.x0 <= pdf_x <= r.x1 and r.y0 <= pdf_y <= r.y1):
            continue

        fill_val = d.get("fill")
        color_val = d.get("color")
        fill_opacity = d.get("fill_opacity", 1.0)
        stroke_opacity = d.get("stroke_opacity", 1.0)

        if fill_val is not None or color_val is not None:
            fill_rgb = None
            if fill_val is not None:
                fill_rgb = tuple(int(f * 255) for f in fill_val)
            stroke_rgb = None
            if color_val is not None:
                stroke_rgb = tuple(int(c * 255) for c in color_val)

            return {
                'found': True,
                'type': 'path',
                'fill_rgb': fill_rgb,
                'stroke_rgb': stroke_rgb,
                'fill_opacity': fill_opacity,
                'stroke_opacity': stroke_opacity,
                'even_odd': d.get("even_odd", False),
                'bbox': (r.x0, r.y0, r.x1, r.y1),
                'items': d.get("items", []),
            }
    return {'found': False}


def inspect_position_exact(page, pdf_x, pdf_y, doc=None):
    """Inspect PDF objects at a position and return the EXACT source color
    from the content stream operators."""
    if doc is None:
        return _inspect_fallback(page, pdf_x, pdf_y)

    from preview.content_stream import find_text_color_at, find_color_at

    page_num = None
    for pn in range(doc.page_count):
        if doc[pn].xref == page.xref:
            page_num = pn
            break

    if page_num is None:
        return _inspect_fallback(page, pdf_x, pdf_y)

    recorded = _get_or_parse_page(doc, page_num)

    result = find_color_at(recorded, page, pdf_x, pdf_y)
    if result.get('found'):
        return result

    return _inspect_fallback(page, pdf_x, pdf_y)


def _inspect_fallback(page, pdf_x, pdf_y):
    """Fallback inspection using MuPDF's high-level text/drawing APIs (sRGB only)."""
    text_info = get_text_at_position(page, pdf_x, pdf_y)
    if text_info['found']:
        return text_info

    drawing_info = get_drawing_at_position(page, pdf_x, pdf_y)
    if drawing_info['found']:
        return drawing_info

    return {'found': False}


def inspect_position(page, pdf_x, pdf_y, doc=None):
    """Inspect PDF objects at a position.

    Calls inspect_position_exact if doc is provided, else falls back.
    """
    if doc:
        return inspect_position_exact(page, pdf_x, pdf_y, doc)
    return _inspect_fallback(page, pdf_x, pdf_y)


def detect_rich_black(source_rgb, cmyk_rendered, threshold=3):
    """Detect if rendered CMYK is rich black when source appears pure black."""
    if source_rgb is None:
        return {'is_rich_black': False, 'source_is_black': False, 'message': None}

    source_is_black = _is_essentially_black(source_rgb)
    if not source_is_black:
        return {'is_rich_black': False, 'source_is_black': False, 'message': None}

    c, m, y, k = int(cmyk_rendered[0]), int(cmyk_rendered[1]), int(
        cmyk_rendered[2]), int(cmyk_rendered[3])

    if k < 10:
        return {'is_rich_black': False, 'source_is_black': True, 'message': None}

    if c <= threshold and m <= threshold and y <= threshold:
        return {'is_rich_black': False, 'source_is_black': True, 'message': None}

    return {
        'is_rich_black': True,
        'source_is_black': True,
        'message': (
            f"Rich black detected — source looks pure black "
            f"but CMYK rendering shows C:{c / 2.55:.0f}% M:{m / 2.55:.0f}% "
            f"Y:{y / 2.55:.0f}% K:{k / 2.55:.0f}%"
        ),
    }


def clear_cache():
    """Clear the content stream parse cache (call when changing documents)."""
    _CS_CACHE.clear()
