"""Object-type visibility filter for the page preview.

Implements Acrobat-style "Output Preview" object filtering: when a category
(images / text / solid color / gradient / shading / strokes / vector) is
toggled off, the corresponding objects are removed from the rendered page.

The implementation keeps the CMYK render fully accurate for the *remaining*
objects: we first render the whole page to CMYK (with the normal
overprint / separation pipeline), then build a *coverage mask* of every
disabled category by re-drawing just those objects onto a scratch page.
Pixels covered by a disabled category are set to paper white (0,0,0,0)
in the CMYK array. Overlapping objects are handled naturally because the
mask is derived from the actual geometry of each category.
"""

import fitz
import numpy as np


# Category keys. ``vector`` is an umbrella that controls *all* vector
# graphics (it is AND-ed with the more specific vector categories).
CATEGORIES = (
    'images', 'text', 'solid', 'gradient', 'shading', 'strokes', 'vector'
)

# Display labels (English terms only, per project rules).
LABELS = {
    'images': "Images",
    'text': "Text",
    'solid': "Solid Color",
    'gradient': "Gradient Color",
    'shading': "Shadings",
    'strokes': "Strokes",
    'vector': "Vector",
}

# Accent color per category, used only for the small toggle indicator.
COLORS = {
    'images': "#3da5ff",
    'text': "#43c463",
    'solid': "#ff8c42",
    'gradient': "#c77dff",
    'shading': "#9b8cff",
    'strokes': "#1de9b6",
    'vector': "#ffd23f",
}


def classify_fill(fill):
    """Classify a drawing fill into 'solid' / 'gradient' / 'shading'."""
    if not isinstance(fill, dict):
        return 'solid'
    t = str(fill.get('type', '')).lower()
    if t in ('linear', 'axial', 'radial', 'triangle', 'mesh'):
        return 'gradient'
    # Coons/tensor/function/sample shadings and anything else.
    return 'shading'


def _drawing_categories(d):
    cats = set()
    if d.get('color') is not None:
        cats.add('strokes')
    fill = d.get('fill')
    if fill is not None:
        cats.add(classify_fill(fill))
    return cats


def _is_hidden(d, enabled):
    cats = _drawing_categories(d)
    if not cats:
        return False
    if not enabled.get('vector', True) and cats:
        return True
    if 'strokes' in cats and not enabled.get('strokes', True):
        return True
    if 'solid' in cats and not enabled.get('solid', True):
        return True
    if 'gradient' in cats and not enabled.get('gradient', True):
        return True
    if 'shading' in cats and not enabled.get('shading', True):
        return True
    return False


def _replay(shape, d):
    for item in d.get('items', []):
        kind = item[0]
        if kind == 'l':
            shape.draw_line(item[1], item[2])
        elif kind == 'c':
            shape.draw_bezier(item[1], item[2], item[3], item[4])
        elif kind == 're':
            shape.draw_rect(item[1])
        elif kind == 'qu':
            shape.draw_quad(item[1])


_WHITE = (1, 1, 1)
_BLACK = (0, 0, 0)


def build_object_mask(page, zoom, enabled, clip=None):
    """Return a boolean (H, W) mask that is True where a *disabled*
    category covers pixels. Pixels set to True will be hidden."""
    doc = fitz.open()
    try:
        doc.insert_page(-1, width=page.rect.width, height=page.rect.height)
        mp = doc[0]
        # Black background so drawn (white) objects show up as bright.
        mp.draw_rect(fitz.Rect(0, 0, mp.rect.width, mp.rect.height),
                     color=_BLACK, fill=_BLACK, width=0)

        try:
            drawings = page.get_drawings()
        except Exception:
            drawings = []

        for d in drawings:
            if not _is_hidden(d, enabled):
                continue
            sh = mp.new_shape()
            _replay(sh, d)
            has_fill = d.get('fill') is not None
            has_stroke = d.get('color') is not None
            try:
                sh.finish(
                    fill=_WHITE if has_fill else None,
                    color=_WHITE if has_stroke else None,
                    width=d.get('width', 1),
                    lineCap=d.get('lineCap', 0),
                    lineJoin=d.get('lineJoin', 0),
                    dashes=d.get('dashes'),
                    even_odd=d.get('even_odd', False),
                )
                sh.commit()
            except Exception:
                pass

        if not enabled.get('text', True):
            try:
                td = page.get_text('dict')
                for b in td.get('blocks', []):
                    if b.get('type') != 0:
                        continue
                    for line in b.get('lines', []):
                        for span in line.get('spans', []):
                            bbox = span.get('bbox')
                            if bbox:
                                mp.draw_rect(fitz.Rect(*bbox), color=_WHITE,
                                             fill=_WHITE, width=0)
            except Exception:
                pass

        if not enabled.get('images', True):
            try:
                for im in page.get_image_info():
                    bbox = im.get('bbox')
                    if bbox:
                        mp.draw_rect(fitz.Rect(*bbox), color=_WHITE,
                                     fill=_WHITE, width=0)
            except Exception:
                pass

        mat = fitz.Matrix(zoom, zoom)
        pix = mp.get_pixmap(matrix=mat, clip=clip,
                             colorspace=fitz.csGRAY, alpha=False)
    except Exception:
        try:
            doc.close()
        except Exception:
            pass
        return None
    finally:
        pass

    try:
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width)
    except Exception:
        arr = None
    try:
        doc.close()
    except Exception:
        pass
    if arr is None:
        return None
    return arr > 128


_MASK_CACHE = {}


def apply_object_filter(cmyk_arr, page, zoom, enabled, clip=None):
    """Zero (paper white) every CMYK pixel covered by a disabled category."""
    if cmyk_arr is None or enabled is None:
        return cmyk_arr
    if all(enabled.get(k, True) for k in CATEGORIES):
        return cmyk_arr

    cache_key = (
        id(page.parent) if hasattr(page, 'parent') else id(page),
        page.xref,
        round(zoom, 4),
        frozenset((k, bool(v)) for k, v in enabled.items()),
        tuple(clip) if clip is not None else None,
    )
    mask = _MASK_CACHE.get(cache_key)
    if mask is None:
        mask = build_object_mask(page, zoom, enabled, clip=clip)
        if len(_MASK_CACHE) > 8:
            _MASK_CACHE.clear()
        if mask is not None:
            _MASK_CACHE[cache_key] = mask

    if mask is None or mask.shape != cmyk_arr.shape[:2]:
        return cmyk_arr

    out = cmyk_arr.copy()
    out[mask] = 0
    return out


def clear_cache():
    _MASK_CACHE.clear()
