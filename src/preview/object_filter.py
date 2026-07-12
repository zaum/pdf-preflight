"""Object-type visibility filter for the page preview.

Implements Acrobat-style "Output Preview" object filtering: when a category
(images / text / vector) is toggled off, the corresponding objects are
removed from the rendered page by stripping them from the content stream
(via redactions) and re-rendering. This reveals whatever is drawn *behind*
them (instead of a white box) and preserves objects drawn *on top* of them.

``vector`` removes all vector graphics (solid fills, gradients, shadings and
strokes), leaving images and text untouched.
"""

import fitz
import numpy as np


# Category keys.
CATEGORIES = ('images', 'text', 'vector')

# Display labels (English terms only, per project rules).
LABELS = {
    'images': "Images",
    'text': "Text",
    'vector': "Vector",
}

# Accent color per category, used only for the small toggle indicator.
COLORS = {
    'images': "#3da5ff",
    'text': "#43c463",
    'vector': "#ffd23f",
}


# Cache of already-redacted temp documents, keyed by the removal plan. The
# expensive part (copying the page + applying redactions) is independent of
# zoom/clip, so we build the filtered page ONCE and reuse it for every zoom
# level and detail tile. The rendered pixmap itself is cheap per zoom.
_FILTERED_DOC_CACHE = {}
_FILTERED_DOC_ORDER = []


def _build_filtered_page(page, remove_images, remove_text, remove_vector):
    """Return a temp (document, page) with the requested object categories
    removed from the content stream. A single full-page redaction removes all
    objects of the selected categories in one apply_redactions call, which is
    orders of magnitude faster than one annotation per object."""
    try:
        src = page.parent
        pno = page.number
    except Exception:
        return None, None
    tmp = fitz.open()
    try:
        tmp.insert_pdf(src, from_page=pno, to_page=pno)
        tp = tmp[0]

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
        return tmp, tp
    except Exception:
        try:
            tmp.close()
        except Exception:
            pass
        return None, None


def _get_filtered_page(page, plan_key, remove_images, remove_text,
                       remove_vector):
    cached = _FILTERED_DOC_CACHE.get(plan_key)
    if cached is not None:
        return cached[1]
    tmp, tp = _build_filtered_page(
        page, remove_images, remove_text, remove_vector)
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
                        plan_key=None):
    """Render the page to CMYK with whole object categories *removed* from the
    content stream, revealing whatever is drawn behind them (Acrobat Output
    Preview behaviour). The redacted page is cached per removal plan and reused
    across zoom levels, so only the (cheap) pixmap render happens per zoom."""
    if not (remove_images or remove_text or remove_vector):
        return None
    if plan_key is None:
        plan_key = (
            id(page.parent), page.xref, remove_images, remove_text,
            remove_vector,
        )
    tp = _get_filtered_page(page, plan_key, remove_images, remove_text,
                            remove_vector)
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


def apply_object_filter(cmyk_arr, page, zoom, enabled, clip=None):
    """Remove every disabled object category from the CMYK render.

    All categories are removed by re-rendering the page with the corresponding
    objects stripped from the content stream, so the content *behind* them
    shows through instead of a white box (matching Acrobat's Output Preview)
    and objects drawn *on top* of them are preserved.
    """
    if cmyk_arr is None or enabled is None:
        return cmyk_arr
    if all(enabled.get(k, True) for k in CATEGORIES):
        return cmyk_arr

    doc_id = id(page.parent) if hasattr(page, 'parent') else id(page)
    clip_key = tuple(clip) if clip is not None else None

    remove_images = not enabled.get('images', True)
    remove_text = not enabled.get('text', True)
    remove_vector = not enabled.get('vector', True)

    if not (remove_images or remove_text or remove_vector):
        return cmyk_arr

    # The redacted page depends only on doc + page + removal plan (zoom-free).
    plan_key = (doc_id, page.xref, remove_images, remove_text, remove_vector)
    # The rendered CMYK array additionally depends on zoom + clip.
    bg_key = plan_key + (round(zoom, 4), clip_key)

    bg = _BG_CACHE.get(bg_key)
    if bg is None:
        bg = build_filtered_cmyk(
            page, zoom, clip=clip, remove_images=remove_images,
            remove_text=remove_text, remove_vector=remove_vector,
            plan_key=plan_key)
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
