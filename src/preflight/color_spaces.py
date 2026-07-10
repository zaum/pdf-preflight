import fitz


class ColorSpaceAnalyzer:
    def __init__(self):
        self.doc = None

    def get_all_spot_colors(self):
        spots = set()
        if not self.doc:
            return sorted(spots)

        for page_num in range(self.doc.page_count):
            page = self.doc[page_num]
            page_spots = self._get_page_spots(page)
            spots.update(page_spots)

        return sorted(spots)

    def _get_page_spots(self, page):
        spots = []
        try:
            xref = page.xref
            pag_obj = page.parent.xref_object(xref)
            if '/Resources' not in pag_obj:
                return spots
            import re
            for match in re.finditer(r'/ColorSpace\s*<<.*?/CS(\d+)\s*[/]([^\s]+)', pag_obj, re.DOTALL):
                name = match.group(2)
                if name not in ('DeviceRGB', 'DeviceCMYK', 'DeviceGray', 'ICCBased'):
                    spots.append(name)
            for match in re.finditer(r'/Separation\s*\[/(\w+)', pag_obj):
                spots.append(match.group(1))
        except Exception:
            pass
        return spots

    def identify_color_space(self, obj_ref):
        """Identify the color space of a PDF object by its reference"""
        try:
            obj = self.doc.xref_object(obj_ref)
            if '/ColorSpace' in obj:
                cs_ref = obj.split('/ColorSpace')[1].split('\n')[0].strip()
                return cs_ref
        except Exception:
            pass
        return None

    def is_device_cmyk(self, page_num):
        """Check if any object on the page uses DeviceCMYK"""
        page = self.doc[page_num]
        try:
            pag_obj = page.parent.xref_object(page.xref)
            return '/DeviceCMYK' in pag_obj
        except Exception:
            return False
