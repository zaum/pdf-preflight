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

    def sample_cmyk(self, page_num, pdf_x, pdf_y, size=1):
        if not self.doc:
            return None
        page = self.doc[page_num]
        half = size / 2.0
        # Callers pass PDF user-space coords (bottom-left origin, y up).
        # fitz's pixmap clip uses a top-left origin (y down), so flip y.
        clip_y = page.rect.height - pdf_y
        clip = fitz.Rect(pdf_x - half, clip_y - half, pdf_x + half, clip_y + half)
        clip = clip & page.rect
        if clip.is_empty or clip.is_infinite:
            return None
        mat = fitz.Matrix(100, 100)
        with _no_icc():
            pix = page.get_pixmap(matrix=mat, clip=clip, colorspace=fitz.csCMYK)
        if pix.width == 0 or pix.height == 0:
            return None
        samples = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 4)
        if size > 1:
            return np.round(samples.reshape(-1, 4).mean(axis=0)).astype(np.uint8)
        cy = samples.shape[0] // 2
        cx = samples.shape[1] // 2
        return samples[cy, cx]

    def sample_rgb(self, page_num, pdf_x, pdf_y, size=1):
        if not self.doc:
            return None
        page = self.doc[page_num]
        half = size / 2.0
        # Callers pass PDF user-space coords (bottom-left origin, y up).
        # fitz's pixmap clip uses a top-left origin (y down), so flip y.
        clip_y = page.rect.height - pdf_y
        clip = fitz.Rect(pdf_x - half, clip_y - half, pdf_x + half, clip_y + half)
        clip = clip & page.rect
        if clip.is_empty or clip.is_infinite:
            return None
        mat = fitz.Matrix(100, 100)
        with _RENDER_LOCK:
            pix = page.get_pixmap(matrix=mat, clip=clip, colorspace=fitz.csRGB)
        if pix.width == 0 or pix.height == 0:
            return None
        samples = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
        if size > 1:
            return np.round(samples.reshape(-1, 3).mean(axis=0)).astype(np.uint8)
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

