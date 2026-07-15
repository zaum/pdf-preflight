from PyQt6.QtGui import QPen, QColor
from PyQt6.QtCore import Qt


BOX_STYLES = {
    'art':   (QColor(255, 0, 0), Qt.PenStyle.DashLine),
    'bleed': (QColor(0, 100, 255), Qt.PenStyle.SolidLine),
    'trim':  (QColor(0, 200, 0), Qt.PenStyle.DotLine),
    'media': (QColor(128, 128, 128), Qt.PenStyle.DashDotLine),
    'crop':  (QColor(255, 128, 0), Qt.PenStyle.DashDotDotLine),
}

BOX_LABELS = {
    'art': 'ArtBox',
    'bleed': 'BleedBox',
    'trim': 'TrimBox',
    'media': 'MediaBox',
    'crop': 'CropBox',
}


def _format_box_dims(box, unit):
    if unit == "pt":
        return f"{box.width:.0f} x {box.height:.0f} pt"
    elif unit == "both":
        w_mm = box.width * 25.4 / 72
        h_mm = box.height * 25.4 / 72
        return f"{box.width:.0f} x {box.height:.0f} pt ({w_mm:.1f} x {h_mm:.1f} mm)"
    else:  # mm
        w_mm = box.width * 25.4 / 72
        h_mm = box.height * 25.4 / 72

        def fmt(v):
            s = f"{v:.1f}"
            if s.endswith(".0"):
                s = s[:-2]
            return s

        return f"{fmt(w_mm)} x {fmt(h_mm)} mm"


def draw_box(painter, box, zoom, page_height, color, style, label=None,
             x_offset=0, unit="mm", label_gap=8):
    if box.x0 >= box.x1 or box.y0 >= box.y1:
        return

    pen = QPen(color, max(1, int(2 * zoom)), style)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    x0 = box.x0 * zoom + x_offset
    y0 = (page_height - box.y1) * zoom
    w = (box.x1 - box.x0) * zoom
    h = (box.y1 - box.y0) * zoom

    painter.drawRect(int(x0), int(y0), int(w), int(h))

    if label:
        dims = _format_box_dims(box, unit)
        text = f"{label}  {dims}"
        painter.drawText(int(x0) + label_gap, int(y0) - label_gap, text)


class BoxOverlay:
    def __init__(self):
        self.active_boxes = set()
        self.unit = "mm"

    def toggle_box(self, box_name):
        if box_name in self.active_boxes:
            self.active_boxes.remove(box_name)
        else:
            self.active_boxes.add(box_name)

    def draw(self, painter, page, zoom, x_offset=0, boxes=None, page_height=None):
        if boxes is not None and page_height is not None:
            # Called with raw boxes dict + height (dual-page mode)
            pages_boxes = boxes
            ph = page_height
        else:
            # Called with page object (single-page mode)
            if not page or not self.active_boxes:
                return
            ph = page.rect.height
            pages_boxes = {
                'art': getattr(page, 'artbox', None),
                'bleed': getattr(page, 'bleedbox', None),
                'trim': getattr(page, 'trimbox', None),
                'media': getattr(page, 'mediabox', None),
                'crop': getattr(page, 'cropbox', None),
            }

        if not self.active_boxes:
            return

        for name in self.active_boxes:
            b = pages_boxes.get(name)
            if b is not None:
                color, style = BOX_STYLES.get(name, BOX_STYLES['media'])
                label = BOX_LABELS.get(name)
                draw_box(painter, b, zoom, ph, color, style, label,
                         x_offset, unit=self.unit)
