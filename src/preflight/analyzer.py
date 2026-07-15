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

    def _collect_referenced_xrefs(self):
        """Return the set of xref numbers reachable from any page.

        Walks page objects, their resources and all referenced children
        (XObjects, forms, fonts, ExtGStates, content streams, ...) so that
        only colour spaces actually used by the document are reported.
        """
        referenced = set()
        stack = []
        try:
            stack.append(self.doc.pdf_catalog())
        except Exception:
            pass
        for pgi in range(self.doc.page_count):
            stack.append(self.doc[pgi].xref)
        while stack:
            xri = stack.pop()
            if xri in referenced or xri < 1:
                continue
            referenced.add(xri)
            try:
                obj = self.doc.xref_object(xri)
            except Exception:
                continue
            for m in re.finditer(r'(\d+) 0 R', obj):
                stack.append(int(m.group(1)))
        return referenced

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

    def _classify_cs_def(self, text):
        """Classify a colour-space definition (object string) into a label."""
        t = text or ''
        if '/Separation' in t:
            m = re.search(r'/Separation\s+/(\w+)', t)
            return f'Separation: {m.group(1)}' if m else 'Separation'
        if '/DeviceN' in t:
            m = re.search(r'/DeviceN\s*\[([^\]]*)\]', t)
            if m:
                names = [n.strip('/') for n in m.group(1).split()
                         if n.strip().startswith('/')]
                if names:
                    if len(names) <= 4:
                        return 'DeviceN: ' + '/'.join(names)
                    return f'DeviceN ({len(names)})'
            return 'DeviceN'
        if '/Indexed' in t:
            return 'Indexed'
        if '/Pattern' in t:
            return 'Pattern'
        if '/Lab' in t:
            return 'Lab'
        if '/ICCBased' in t:
            m = re.search(r'/ICCBased\s+(\d+)', t)
            if m:
                cs = self._icc_colorspace(int(m.group(1)))
                if cs:
                    return cs
            return 'ICC'
        if '/DeviceCMYK' in t:
            return 'CMYK'
        if '/DeviceRGB' in t:
            return 'RGB'
        if '/DeviceGray' in t:
            return 'Gray'
        return None

    def _page_resources_obj(self, page):
        try:
            pobj = self.doc.xref_object(page.xref)
            m = re.search(r'/Resources\s+(\d+) 0 R', pobj)
            if not m:
                return None
            return self.doc.xref_object(int(m.group(1)))
        except Exception:
            return None

    def _resolve_named_cs(self, page, name):
        """Resolve a named (resource) colour space to a label."""
        try:
            robj = self._page_resources_obj(page)
            if not robj:
                return None
            m = re.search(r'/ColorSpace\s*<<(.*?)>>', robj, re.DOTALL)
            if not m:
                return None
            cs = m.group(1)
            mm = re.search(r'/' + re.escape(name) + r'\s+(\d+) 0 R', cs)
            if mm:
                return self._classify_cs_def(self.doc.xref_object(int(mm.group(1))))
            mm = re.search(r'/' + re.escape(name) + r'\s+(\[.*?\]|/\w+)', cs)
            if mm:
                return self._classify_cs_def(mm.group(1))
        except Exception:
            pass
        return None

    def _content_color_spaces(self):
        """Colour spaces actually used in page content streams.

        Uses the content-stream parser so that implicit operators
        (e.g. ``k`` for DeviceCMYK) and image XObjects are covered.
        """
        spaces = set()
        try:
            from preview.content_stream import PageColorExtractor
            parser = PageColorExtractor(self.doc)
            for pgi in range(self.doc.page_count):
                page = self.doc[pgi]
                try:
                    recs = parser.extract_page_colors(pgi)
                except Exception:
                    recs = []
                for r in recs:
                    for key in ('fill_cs', 'stroke_cs'):
                        cs = r.get(key)
                        if not cs:
                            continue
                        if cs in ('DeviceRGB', 'DeviceCMYK', 'DeviceGray'):
                            spaces.add(cs.replace('Device', ''))
                        elif cs.startswith('/'):
                            label = self._resolve_named_cs(page, cs[1:])
                            if label:
                                spaces.add(label)
        except Exception:
            pass
        return spaces

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

        # Colour spaces actually used by page content (vector/text/operators
        # and image XObjects via the parser's recursion).
        for cs in self._content_color_spaces():
            info['color_spaces'].add(cs)

        # Complementary scan of referenced objects (images, ExtGStates,
        # transparency groups, used ICC profiles). Orphan/unreferenced
        # objects (e.g. a dead sRGB profile) are intentionally ignored.
        referenced = self._collect_referenced_xrefs()
        for xri in referenced:
            try:
                obj = self.doc.xref_object(xri)
                for cs in ('/DeviceRGB', '/DeviceCMYK', '/DeviceGray'):
                    if cs in obj:
                        info['color_spaces'].add(cs.replace('/Device', ''))
                if '/ICCBased' in obj:
                    icc_xref = xri
                    m_icc = re.search(r'/ICCBased\s+(\d+)', obj)
                    if m_icc:
                        icc_xref = int(m_icc.group(1))
                    name = self._icc_name_from_obj(icc_xref, obj)
                    if name not in info['icc_profiles']:
                        info['icc_profiles'].append(name)
                    try:
                        desc = self._parse_icc_description(icc_xref)
                        d = desc if desc else name
                    except Exception:
                        d = name
                    if d not in info['icc_profile_descriptions']:
                        info['icc_profile_descriptions'].append(d)

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

    def _normalize_pdfx_version(self, ver):
        v = (ver or '').strip()
        if not v:
            return None
        u = v.upper()
        # Map known variants (with or without year suffix) to a canonical label.
        if 'PDF/X-1A' in u:
            return 'PDF/X-1a'
        if 'PDF/X-1' in u:
            return 'PDF/X-1'
        if 'PDF/X-2' in u:
            return 'PDF/X-2'
        if 'PDF/X-3' in u:
            return 'PDF/X-3'
        if 'PDF/X-4' in u:
            return 'PDF/X-4'
        if 'PDF/X-5' in u:
            return 'PDF/X-5'
        if 'PDF/X-6' in u:
            return 'PDF/X-6'
        if 'PDF/X' in u:
            return v
        return v

    def _check_pdfx(self):
        if not self.doc:
            return ('n/a', False)
        try:
            xml_meta = self.doc.get_xml_metadata()
            if xml_meta:
                # Match the version element regardless of its XML namespace
                # prefix (e.g. pdfx:PDFXVersion, pdfxid:GTS_PDFXVersion,
                # pdfx:PDFXConformance).
                m_pdfx = re.search(
                    r'<[\w-]*:?(?:PDFXVersion|GTS_PDFXVersion|PDFXConformance)\b[^>]*>(.*?)</[^>]+>',
                    xml_meta, re.IGNORECASE)
                if m_pdfx:
                    ver = self._normalize_pdfx_version(m_pdfx.group(1))
                    if ver:
                        return (ver, True)

            for xri in range(1, self.doc.xref_length()):
                try:
                    obj = self.doc.xref_object(xri)
                    if '/OutputIntent' in obj:
                        m_s = re.search(r'/S\s*/(GTS_PDFX\w*)', obj)
                        if m_s:
                            subtype = m_s.group(1).upper()
                            if subtype == 'GTS_PDFX1A':
                                return ('PDF/X-1a', True)
                            if subtype == 'GTS_PDFX3':
                                return ('PDF/X-3', True)
                            if subtype == 'GTS_PDFX4':
                                return ('PDF/X-4', True)
                            if subtype == 'GTS_PDFX':
                                return ('PDF/X', True)
                            return (f"PDF/X ({m_s.group(1)})", True)
                except Exception:
                    continue

            cat_xref = self.doc.pdf_catalog()
            cat_obj = self.doc.xref_object(cat_xref) if cat_xref else ''
            if '/OutputIntents' in cat_obj:
                return ('PDF/X', True)

            return ('Not PDF/X', False)
        except Exception:
            return ('Not PDF/X', False)

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
