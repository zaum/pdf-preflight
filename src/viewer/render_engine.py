import fitz
import numpy as np
import threading
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage
from PIL import Image
import os
from contextlib import contextmanager


_ICC_CACHE = {}

# Serializes MuPDF's *global* ICC toggle (fitz.TOOLS.set_icc) so it is never
# flipped from two threads at once — doing so crashes the process.
_RENDER_LOCK = threading.Lock()


@contextmanager
def _no_icc():
    """Temporarily disable MuPDF's ICC color management for raw DeviceCMYK rendering."""
    with _RENDER_LOCK:
        old = fitz.TOOLS.set_icc(0)
        try:
            yield
        finally:
            fitz.TOOLS.set_icc(old)


def _disable_overprint(doc):
    """Neutralize overprint flags in all ExtGState objects of a document.
    Returns list of (xref, original_obj_string) for restoration.
    The doc should be a temporary copy (e.g. in a background render thread)."""
    modified = []
    for xri in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xri)
        except Exception:
            continue
        if '/Type' not in obj or '/ExtGState' not in obj:
            continue
        has_op = '/op true' in obj or '/op\ttrue' in obj
        has_OP = '/OP true' in obj or '/OP\ttrue' in obj
        if has_op or has_OP:
            original = obj
            new_obj = obj
            import re
            new_obj = re.sub(r'/op\s+true', '/op false', new_obj)
            new_obj = re.sub(r'/OP\s+true', '/OP false', new_obj)
            try:
                doc.update_object(xri, new_obj)
                modified.append((xri, original))
            except Exception:
                pass
    return modified


def _restore_overprint(doc, modified):
    """Restore original ExtGState objects."""
    for xri, original in modified:
        try:
            doc.update_object(xri, original)
        except Exception:
            pass


def _find_system_cmyk_icc():
    paths = [
        os.environ.get('WINDIR', '') + r'\System32\spool\drivers\color',
        '/Library/ColorSync/Profiles',
        '/usr/share/color/icc',
    ]
    candidates = [
        'CoatedFOGRA39.icc',
        'ISOcoated_v2_300_bas.ICC',
        'USWebCoatedSWOP.icc',
        'CoatedGRACoL2006.icc',
    ]
    icc_dir = os.environ.get('ICC_PROFILE_DIR', '')
    if icc_dir and os.path.isdir(icc_dir):
        paths.insert(0, icc_dir)
    for d in paths:
        if not os.path.isdir(d):
            continue
        for c in candidates:
            p = os.path.join(d, c)
            if os.path.isfile(p):
                return p
        for f in os.listdir(d):
            if f.lower().endswith('.icc') or f.lower().endswith('.icm'):
                return os.path.join(d, f)
    return None


def _try_extract_doc_icc(doc):
    try:
        for xri in range(1, doc.xref_length()):
            obj = doc.xref_object(xri)
            if '/DestOutputProfile' in obj:
                parts = obj.split('/DestOutputProfile')
                ref = parts[1].strip().split()[0] if len(parts) > 1 else ''
                if ref.isdigit():
                    stream = doc.xref_stream(int(ref))
                    if stream and len(stream) > 128:
                        return stream
        for xri in range(1, doc.xref_length()):
            obj = doc.xref_object(xri)
            if '/ICCBased' in obj and '/N' in obj:
                n_str = obj.split('/N')[1].strip().split()[0]
                try:
                    n = int(n_str)
                except ValueError:
                    n = 0
                if n == 4:
                    stream = doc.xref_stream(xri)
                    if stream and len(stream) > 128:
                        return stream
    except Exception:
        pass
    return None


def get_cmyk_icc_path(doc=None):
    key = id(doc) if doc else 0
    if key in _ICC_CACHE:
        return _ICC_CACHE[key]
    icc_path = None
    if doc:
        raw = _try_extract_doc_icc(doc)
        if raw:
            tmp = os.path.join(os.environ.get('TEMP', '.'),
                               f'_pdf_icc_{id(doc)}.icc')
            try:
                with open(tmp, 'wb') as f:
                    f.write(raw)
                icc_path = tmp
            except Exception:
                pass
    if not icc_path:
        icc_path = _find_system_cmyk_icc()
    _ICC_CACHE[key] = icc_path
    return icc_path


class RenderEngine:
    def __init__(self):
        self.doc = None
        self._page_count = 0
        self._icc_transform_cache: dict = {}

    def open(self, path):
        self.doc = fitz.open(path)
        self._page_count = self.doc.page_count
        return self._page_count

    def close(self):
        if self.doc:
            self.doc.close()
            self.doc = None
        self._mag_cache = None
        self._mag_cache_key = None
        from preview.pdf_inspector import clear_cache
        clear_cache()

    @property
    def page_count(self):
        return self._page_count

    def render_rgb(self, page_num, zoom=1.0):
        if not self.doc:
            return None, None
        page = self.doc[page_num]
        mat = fitz.Matrix(zoom, zoom)
        with _RENDER_LOCK:
            pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
        img = QImage(
            pix.samples, pix.width, pix.height,
            pix.stride, QImage.Format.Format_RGB888
        )
        return img, page

    def render_cmyk_array(self, page_num, zoom=1.0, simulate_overprint=True):
        if not self.doc:
            return None, None
        page = self.doc[page_num]
        mat = fitz.Matrix(zoom, zoom)
        with _no_icc():
            if not simulate_overprint:
                modified = _disable_overprint(self.doc)
                try:
                    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csCMYK)
                finally:
                    _restore_overprint(self.doc, modified)
            else:
                pix = page.get_pixmap(matrix=mat, colorspace=fitz.csCMYK)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, 4)
        return arr, page

    def sample_cmyk(self, page_num, pdf_x, pdf_y):
        if not self.doc:
            return None
        page = self.doc[page_num]
        clip = fitz.Rect(pdf_x - 0.5, pdf_y - 0.5, pdf_x + 0.5, pdf_y + 0.5)
        mat = fitz.Matrix(100, 100)
        with _no_icc():
            pix = page.get_pixmap(matrix=mat, clip=clip, colorspace=fitz.csCMYK)
        samples = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 4)
        cy = samples.shape[0] // 2
        cx = samples.shape[1] // 2
        return samples[cy, cx]

    def sample_rgb(self, page_num, pdf_x, pdf_y):
        if not self.doc:
            return None
        page = self.doc[page_num]
        clip = fitz.Rect(pdf_x - 0.5, pdf_y - 0.5, pdf_x + 0.5, pdf_y + 0.5)
        mat = fitz.Matrix(100, 100)
        with _RENDER_LOCK:
            pix = page.get_pixmap(matrix=mat, clip=clip, colorspace=fitz.csRGB)
        samples = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
        cy = samples.shape[0] // 2
        cx = samples.shape[1] // 2
        return samples[cy, cx]

    def get_source_color_at(self, page_num, pdf_x, pdf_y):
        """Inspect PDF objects at position for original color definition.
        Uses content stream parsing for exact operator values when possible.
        Returns dict from pdf_inspector.inspect_position_exact()."""
        if not self.doc:
            return {'found': False}
        from preview.pdf_inspector import inspect_position
        page = self.doc[page_num]
        return inspect_position(page, pdf_x, pdf_y, doc=self.doc)

    def _get_icc_transform(self, icc_path):
        """Build (and cache) a CMYK→sRGB ImageCms transform.
        Returns None on failure so callers can fall back gracefully."""
        if icc_path in self._icc_transform_cache:
            return self._icc_transform_cache[icc_path]
        try:
            from PIL import ImageCms
            cmyk_prof = ImageCms.getOpenProfile(icc_path)
            rgb_prof  = ImageCms.createProfile('sRGB')
            # 0x2000 = BLACKPOINTCOMPENSATION for accurate shadow rendering
            t = ImageCms.buildTransform(
                cmyk_prof, rgb_prof, 'CMYK', 'RGB',
                renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
                flags=0x2000,
            )
            self._icc_transform_cache[icc_path] = t
            return t
        except Exception:
            self._icc_transform_cache[icc_path] = None   # don't retry
            return None

    def _icc_to_rgb(self, cmyk_arr, icc_path):
        img = Image.fromarray(cmyk_arr, mode='CMYK')
        if not icc_path or not os.path.isfile(icc_path):
            return np.ascontiguousarray(np.asarray(img.convert('RGB')))
        try:
            from PIL import ImageCms
            t = self._get_icc_transform(icc_path)
            if t is not None:
                rgb = ImageCms.applyTransform(img, t)
                return np.ascontiguousarray(np.asarray(rgb))
        except Exception:
            pass
        return np.ascontiguousarray(np.asarray(img.convert('RGB')))

    def cmyk_to_rgb_image(self, cmyk_arr, icc_path=None,
                          simulation_icc_path=None):
        if cmyk_arr is None:
            return None
        # simulation profile takes priority over document profile
        effective_path = simulation_icc_path if (
            simulation_icc_path and os.path.isfile(simulation_icc_path)
        ) else icc_path
        rgb_arr = self._icc_to_rgb(cmyk_arr, effective_path)
        h, w = rgb_arr.shape[:2]
        return QImage(rgb_arr.data, w, h, w * 3,
                      QImage.Format.Format_RGB888)

    def cmyk_to_rgb_array(self, cmyk_arr, icc_path=None,
                          simulation_icc_path=None):
        if cmyk_arr is None:
            return None
        effective_path = simulation_icc_path if (
            simulation_icc_path and os.path.isfile(simulation_icc_path)
        ) else icc_path
        return self._icc_to_rgb(cmyk_arr, effective_path)

    def get_page_boxes(self, page):
        return {
            'media': page.mediabox,
            'crop': page.cropbox,
            'art': page.artbox,
            'bleed': page.bleedbox,
            'trim': page.trimbox,
        }

    def build_magnifier_cache(self, page_num):
        """Build the magnifier cache for a page (call from background thread).

        Stores the raw CMYK pixmap — fast to build, RGB conversion is
        deferred until cropping (on a tiny region).
        """
        if not self.doc:
            return
        cache_zoom = 4.0
        cache_key = (page_num, id(self.doc))
        if getattr(self, '_mag_cache_key', None) == cache_key and self._mag_cache is not None:
            return

        page = self.doc[page_num]
        mat = fitz.Matrix(cache_zoom, cache_zoom)
        with _no_icc():
            pix_cmyk = page.get_pixmap(matrix=mat, colorspace=fitz.csCMYK)
        if pix_cmyk.width == 0 or pix_cmyk.height == 0:
            return
        cmyk_arr = np.frombuffer(pix_cmyk.samples, dtype=np.uint8).reshape(
            pix_cmyk.height, pix_cmyk.width, 4).copy()
        self._mag_cache = cmyk_arr
        self._mag_cache_key = cache_key

    def render_magnifier_region(self, page_num, pdf_x, pdf_y,
                                 capture_size_pt=20, output_size_px=200):
        """Render a small area around a PDF point at high resolution.

        Uses the pre-built CMYK cache; crops then converts to RGB via ICC.
        Returns QImage (RGB, copied) or None on failure.
        """
        if not self.doc:
            return None

        cache_zoom = 4.0

        cache_key = (page_num, id(self.doc))
        # Do NOT build the cache synchronously here — building a full-page
        # 4x CMYK pixmap on the main thread (inside paintEvent) freezes the UI.
        # The cache is warmed on a background thread; until then return None so
        # the magnifier falls back to fast QImage sampling.
        if getattr(self, '_mag_cache_key', None) != cache_key or self._mag_cache is None:
            return None

        cmyk_cache = self._mag_cache
        if cmyk_cache is None:
            return None

        page = self.doc[page_num]
        ph = page.rect.height

        # Map PDF coords to cache image pixels
        cx_px = int(pdf_x * cache_zoom)
        cy_px = int((ph - pdf_y) * cache_zoom)
        half_px = int((capture_size_pt / 2.0) * cache_zoom)

        x1 = max(0, cx_px - half_px)
        y1 = max(0, cy_px - half_px)
        x2 = min(cmyk_cache.shape[1], cx_px + half_px)
        y2 = min(cmyk_cache.shape[0], cy_px + half_px)
        crop_w = x2 - x1
        crop_h = y2 - y1

        if crop_w <= 0 or crop_h <= 0:
            return None

        # Crop tiny CMYK region and convert just that to RGB
        crop_cmyk = cmyk_cache[y1:y2, x1:x2, :].copy()
        icc_path = get_cmyk_icc_path(self.doc)
        crop_rgb = self._icc_to_rgb(crop_cmyk, icc_path)

        # Upscale with Lanczos for sharper magnifier image
        pil_img = Image.fromarray(crop_rgb)
        pil_img = pil_img.resize(
            (output_size_px, output_size_px), Image.Resampling.LANCZOS)
        crop_rgb = np.asarray(pil_img)

        qimg = QImage(crop_rgb.data, crop_rgb.shape[1], crop_rgb.shape[0],
                      crop_rgb.shape[1] * 3, QImage.Format.Format_RGB888)
        return qimg
