import fitz
import numpy as np
import re

from .color_spaces import ColorSpaceAnalyzer


class PreflightAnalyzer:
    def __init__(self):
        self.doc = None
        self.color_analyzer = ColorSpaceAnalyzer()

    def open(self, path):
        self.doc = fitz.open(path)
        self.color_analyzer.doc = self.doc
        return self.doc

    def close(self):
        if self.doc:
            self.doc.close()
            self.doc = None

    def get_boxes(self, page_num):
        page = self.doc[page_num]
        return {
            'media': page.mediabox,
            'crop': page.cropbox,
            'art': page.artbox,
            'bleed': page.bleedbox,
            'trim': page.trimbox,
        }

    def check_overprint_at(self, page_num, x, y):
        page = self.doc[page_num]
        try:
            xref = page.xref
            pag_obj = self.doc.xref_object(xref)
            if '/ExtGState' in pag_obj:
                return True
        except Exception:
            pass
        return False

    def has_overprint_on_page(self, page_num):
        page = self.doc[page_num]
        try:
            xref = page.xref
            pag_obj = self.doc.xref_object(xref)
            if '/ExtGState' not in pag_obj:
                return False
            for xref_i in range(1, self.doc.xref_length()):
                obj = self.doc.xref_object(xref_i)
                if '/Type' in obj and '/ExtGState' in obj:
                    if '/OP' in obj or '/op' in obj:
                        return True
        except Exception:
            pass
        return False

    def calculate_tac(self, page_num, zoom=0.3):
        from viewer.render_engine import _RENDER_LOCK
        page = self.doc[page_num]
        mat = fitz.Matrix(zoom, zoom)
        with _RENDER_LOCK:
            old_icc = fitz.TOOLS.set_icc(0)
            try:
                pix = page.get_pixmap(matrix=mat, colorspace=fitz.csCMYK)
            finally:
                fitz.TOOLS.set_icc(old_icc)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, 4)
        return self.tac_from_cmyk(arr)

    @staticmethod
    def tac_from_cmyk(arr):
        """Compute Total Area Coverage statistics from a raw CMYK array.

        Pure numpy — safe to call from any thread (no fitz)."""
        total = arr.astype(np.float32)
        ink_sum = total[:, :, 0] + total[:, :, 1] + total[:, :, 2] + total[:, :, 3]
        ink_pct = ink_sum / 255.0 * 100.0
        return {
            'max': float(ink_pct.max()),
            'avg': float(ink_pct.mean()),
            'over_limit_pixels': int((ink_pct > 300).sum()),
            'total_pixels': ink_pct.size,
            'array': ink_pct,
        }

    def get_spot_colors(self):
        return self.color_analyzer.get_all_spot_colors()

    def _parse_icc_description(self, xref_num):
        try:
            raw = self.doc.xref_stream(xref_num)
            if not raw or len(raw) < 132:
                return None
            tag_count = int.from_bytes(raw[128:132], 'big')
            pos = 132
            for _ in range(tag_count):
                if pos + 12 > len(raw):
                    break
                sig = raw[pos:pos+4]
                offset = int.from_bytes(raw[pos+4:pos+8], 'big')
                size = int.from_bytes(raw[pos+8:pos+12], 'big')
                pos += 12
                if sig == b'desc' and offset + size <= len(raw):
                    data = raw[offset:offset+size]
                    if len(data) >= 12:
                        str_len = int.from_bytes(data[8:12], 'big')
                        if str_len > 0 and 8 + str_len <= len(data):
                            return data[8:8+str_len].decode('ascii', errors='replace').strip('\x00')
            return None
        except Exception:
            return None

    def _icc_name_from_obj(self, xri, obj):
        try:
            desc = self._parse_icc_description(xri)
            if desc:
                return desc
        except Exception:
            pass
        try:
            if '/N' in obj:
                parts = obj.split('/N')
                for part in parts[1:]:
                    n_str = part.strip().split()[0]
                    try:
                        n = int(n_str)
                        return f"ICC {n}-channel"
                    except ValueError:
                        continue
        except Exception:
            pass
        return f"ICC xref={xri}"

    def _icc_colorspace(self, xref_num):
        try:
            raw = self.doc.xref_stream(xref_num)
            if raw and len(raw) >= 20:
                sig = raw[16:20]
                if sig == b'CMYK':
                    return 'CMYK'
                if sig == b'RGB ':
                    return 'RGB'
                if sig == b'GRAY':
                    return 'Gray'
                if sig == b'LAB ':
                    return 'Lab'
        except Exception:
            pass
        return None

    def get_color_info(self):
        info = {
            'color_spaces': set(),
            'icc_profiles': [],
            'output_intent': None,
            'pdfx_status': None,
            'icc_profile_descriptions': [],
        }
        if not self.doc:
            return info

        for pgi in range(self.doc.page_count):
            page = self.doc[pgi]
            try:
                pag_obj = self.doc.xref_object(page.xref)
                for cs in ('/DeviceRGB', '/DeviceCMYK', '/DeviceGray'):
                    if cs in pag_obj:
                        info['color_spaces'].add(cs.replace('/Device', ''))
            except Exception:
                pass

        for xri in range(1, self.doc.xref_length()):
            try:
                obj = self.doc.xref_object(xri)
                if '/ICCBased' in obj:
                    m_icc = re.search(r'/ICCBased\s+(\d+)', obj)
                    icc_xref = int(m_icc.group(1)) if m_icc else xri
                    name = self._icc_name_from_obj(icc_xref, obj)
                    info['icc_profiles'].append(name)
                    try:
                        desc = self._parse_icc_description(icc_xref)
                        if desc:
                            info['icc_profile_descriptions'].append(desc)
                        else:
                            info['icc_profile_descriptions'].append(name)
                    except Exception:
                        info['icc_profile_descriptions'].append(name)

                    cs = self._icc_colorspace(icc_xref)
                    if cs:
                        info['color_spaces'].add(cs)

                if '/OutputIntent' in obj or '/DestOutputProfile' in obj:
                    if '/OutputConditionIdentifier' in obj:
                        oci = obj.split('/OutputConditionIdentifier')[1].strip().split('/')[0].strip()
                        oci = oci.strip('()')
                        info['output_intent'] = oci
                    if '/S' in obj:
                        s_val = obj.split('/S')[1].strip().split()[0].strip('()')
                        if info['output_intent']:
                            info['output_intent'] = f"{s_val} / {info['output_intent']}"
                        else:
                            info['output_intent'] = s_val
            except Exception:
                pass

        if not info['color_spaces']:
            info['color_spaces'].add('unknown')

        info['pdfx_status'] = self._check_pdfx()
        return info

    def _check_pdfx(self):
        if not self.doc:
            return ('n/a', False)
        try:
            xml_meta = self.doc.get_xml_metadata()
            if xml_meta:
                import re
                m_pdfx = re.search(r'<pdfx:PDFXVersion[^>]*>(.*?)</pdfx:PDFXVersion>', xml_meta, re.IGNORECASE)
                if m_pdfx:
                    ver = m_pdfx.group(1).strip()
                    return (ver, True)

            for xri in range(1, self.doc.xref_length()):
                try:
                    obj = self.doc.xref_object(xri)
                    if '/OutputIntent' in obj:
                        import re
                        obj_flat = re.sub(r'\s+', '', obj)
                        if '/S/GTS_PDFX1A' in obj_flat:
                            return ("PDF/X-1a:2001", True)
                        if '/S/GTS_PDFX4' in obj_flat:
                            return ("PDF/X-4:2010", True)
                        if '/S/GTS_PDFX3' in obj_flat:
                            return ("PDF/X-3:2003", True)
                        m_s = re.search(r'/S/(GTS_PDFX[^\s/]+)', obj)
                        if m_s:
                            return (f"PDF/X ({m_s.group(1)})", True)
                except Exception:
                    continue

            cat_xref = self.doc.pdf_catalog()
            cat_obj = self.doc.xref_object(cat_xref) if cat_xref else ''
            has_oi = '/OutputIntents' in cat_obj

            if has_oi:
                return ('PDF/X (unknown variant)', True)

            return ('Not PDF/X', False)
        except Exception:
            return ('error', False)

    def get_security_info(self):
        sec = []
        if not self.doc:
            return sec
        try:
            if self.doc.is_encrypted:
                sec.append("Encrypted")
                if self.doc.needs_pass:
                    sec.append("Password required")
            perm = self.doc.permissions
            sec.append(f"Print: {'Y' if (perm & 4) else 'N'}")
            sec.append(f"Modify: {'Y' if (perm & 8) else 'N'}")
            sec.append(f"Copy: {'Y' if (perm & 16) else 'N'}")
            sec.append(f"Annotate: {'Y' if (perm & 32) else 'N'}")
        except Exception:
            sec.append("n/a")
        return sec

    def get_fonts(self, page_num):
        fonts = []
        try:
            page = self.doc[page_num]
            for f in page.get_fonts():
                name = f[3] if len(f) > 3 else str(f[0])
                if name not in fonts:
                    fonts.append(name)
        except Exception:
            pass
        return fonts

    def get_all_fonts(self):
        fonts = []
        try:
            for page_num in range(self.doc.page_count):
                page = self.doc[page_num]
                for f in page.get_fonts():
                    name = f[3] if len(f) > 3 else str(f[0])
                    if name not in fonts:
                        fonts.append(name)
        except Exception:
            pass
        return fonts
