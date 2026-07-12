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
    'vector': "All Vector",
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


def collect_hidden_vector_rects(page, enabled):
    """Return the bounding rects of every drawing that belongs to a disabled
    vector sub-category (partial selection). Used to remove exactly those
    drawings from the content stream so that objects drawn *on top* of them
    (images, text, other vectors) are preserved."""
    rects = []
    try:
        for d in page.get_drawings():
            if not _is_hidden(d, enabled):
                continue
            r = d.get('rect')
            if r is not None:
                rects.append(fitz.Rect(r))
    except Exception:
        pass
    return rects


# Cache of already-redacted temp documents, keyed by the removal plan. The
# expensive part (copying the page + applying redactions) is independent of
# zoom/clip, so we build the filtered page ONCE and reuse it for every zoom
# level and detail tile. The rendered pixmap itself is cheap per zoom.
_FILTERED_DOC_CACHE = {}
_FILTERED_DOC_ORDER = []


def _build_filtered_page(page, remove_images, remove_text, remove_vector,
                         vector_rects):
    """Return a temp (document, page) with the requested object categories
    removed from the content stream. Redactions are applied with a single
    full-page annotation per pass (instead of one per object), which is orders
    of magnitude faster for text-heavy pages."""
    try:
        src = page.parent
        pno = page.number
    except Exception:
        return None, None
    tmp = fitz.open()
    try:
        tmp.insert_pdf(src, from_page=pno, to_page=pno)
        tp = tmp[0]

        # Images / text / full-vector: a single full-page redaction removes all
        # objects of the selected categories in one apply_redactions call.
        if remove_images or remove_text or remove_vector:
            tp.add_redact_annot(tp.rect, fill=False)
            tp.apply_redactions(
                images=(fitz.PDF_REDACT_IMAGE_REMOVE if remove_images
                        else fitz.PDF_REDACT_IMAGE_NONE),
                graphics=(fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED
                          if remove_vector
                          else fitz.PDF_REDACT_LINE_ART_NONE),
                text=(fitz.PDF_REDACT_TEXT_REMOVE if remove_text
                      else fitz.PDF_REDACT_TEXT_NONE),
            )
        elif vector_rects:
            # Partial vector selection: remove only the specific disabled
            # drawings. REMOVE_IF_COVERED drops a path only when its bounding
            # box is contained in a redaction rect, so objects drawn on top
            # (images, text) survive and the background shows through.
            for r in vector_rects:
                tp.add_redact_annot(fitz.Rect(r), fill=False)
            tp.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                text=fitz.PDF_REDACT_TEXT_NONE,
            )
        return tmp, tp
    except Exception:
        try:
            tmp.close()
        except Exception:
            pass
        return None, None


def _get_filtered_page(page, plan_key, remove_images, remove_text,
                       remove_vector, vector_rects):
    cached = _FILTERED_DOC_CACHE.get(plan_key)
    if cached is not None:
        return cached[1]
    tmp, tp = _build_filtered_page(
        page, remove_images, remove_text, remove_vector, vector_rects)
    if tmp is None:
        return None
    _FILTERED_DOC_CACHE[plan_key] = (tmp, tp)
    _FILTERED_DOC_ORDER.append(plan_key)
    while len(_FILTERED_DOC_ORDER) > 4:
        old = _FILTERED_DOC_ORDER.pop(0)
        entry = _FILTERED_DOC_CACHE.pop(old, None)
        if entry is not None:
            try:
                entry[0].close()
            except Exception:
                pass
    return tp


def build_filtered_cmyk(page, zoom, clip=None, remove_images=False,
                        remove_text=False, remove_vector=False,
                        vector_rects=None, plan_key=None):
    """Render the page to CMYK with whole object categories *removed* from the
    content stream, revealing whatever is drawn behind them (Acrobat Output
    Preview behaviour). The redacted page is cached per removal plan and reused
    across zoom levels, so only the (cheap) pixmap render happens per zoom."""
    if not (remove_images or remove_text or remove_vector or vector_rects):
        return None
    if plan_key is None:
        plan_key = (
            id(page.parent), page.xref, remove_images, remove_text,
            remove_vector,
            tuple(tuple(r) for r in vector_rects) if vector_rects else None,
        )
    tp = _get_filtered_page(page, plan_key, remove_images, remove_text,
                            remove_vector, vector_rects)
    if tp is None:
        return None
    try:
        mat = fitz.Matrix(zoom, zoom)
        pix = tp.get_pixmap(matrix=mat, clip=clip, colorspace=fitz.csCMYK)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, 4).copy()
    except Exception:
        arr = None
    return arr


_BG_CACHE = {}


_VECTOR_SUBS = ('solid', 'gradient', 'shading', 'strokes')


def apply_object_filter(cmyk_arr, page, zoom, enabled, clip=None):
    """Remove every disabled object category from the CMYK render.

    All categories are removed by re-rendering the page with the corresponding
    objects stripped from the content stream, so the content *behind* them
    shows through instead of a white box (matching Acrobat's Output Preview)
    and objects drawn *on top* of them are preserved. A partial vector
    selection removes only the specific disabled drawings.
    """
    if cmyk_arr is None or enabled is None:
        return cmyk_arr
    if all(enabled.get(k, True) for k in CATEGORIES):
        return cmyk_arr

    doc_id = id(page.parent) if hasattr(page, 'parent') else id(page)
    clip_key = tuple(clip) if clip is not None else None

    remove_images = not enabled.get('images', True)
    remove_text = not enabled.get('text', True)
    # Full vector removal when the umbrella is off or every sub-category is off.
    remove_vector = (not enabled.get('vector', True)) or all(
        not enabled.get(s, True) for s in _VECTOR_SUBS)

    # Partial vector selection: collect the rects of the disabled drawings so
    # only those specific objects are removed (objects on top survive).
    vector_rects = None
    if not remove_vector:
        me = {k: True for k in CATEGORIES}
        partial = False
        for s in _VECTOR_SUBS:
            if not enabled.get(s, True):
                me[s] = False
                partial = True
        if partial:
            rects = collect_hidden_vector_rects(page, me)
            if rects:
                vector_rects = rects

    if not (remove_images or remove_text or remove_vector or vector_rects):
        return cmyk_arr

    vector_key = (
        tuple(tuple(r) for r in vector_rects) if vector_rects else None)
    # The redacted page depends only on doc + page + removal plan (zoom-free).
    plan_key = (doc_id, page.xref, remove_images, remove_text,
                remove_vector, vector_key)
    # The rendered CMYK array additionally depends on zoom + clip.
    bg_key = plan_key + (round(zoom, 4), clip_key)

    bg = _BG_CACHE.get(bg_key)
    if bg is None:
        bg = build_filtered_cmyk(
            page, zoom, clip=clip, remove_images=remove_images,
            remove_text=remove_text, remove_vector=remove_vector,
            vector_rects=vector_rects, plan_key=plan_key)
        if len(_BG_CACHE) > 8:
            _BG_CACHE.clear()
        if bg is not None:
            _BG_CACHE[bg_key] = bg

    if bg is not None and bg.shape == cmyk_arr.shape:
        return bg
    return cmyk_arr


def clear_cache():
    _BG_CACHE.clear()
    for entry in _FILTERED_DOC_CACHE.values():
        try:
            entry[0].close()
        except Exception:
            pass
    _FILTERED_DOC_CACHE.clear()
    _FILTERED_DOC_ORDER.clear()
