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


def draw_box(painter, box, zoom, page_height, color, style, label=None,
             x_offset=0):
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
        painter.drawText(int(x0) + 4, int(y0) - 2, label)


class BoxOverlay:
    def __init__(self):
        self.active_boxes = set()

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
                         x_offset)
