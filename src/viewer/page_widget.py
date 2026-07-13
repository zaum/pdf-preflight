from PyQt6.QtWidgets import QGraphicsView, QGraphicsScene, QGraphicsPixmapItem
from PyQt6.QtCore import Qt, QPointF, QRectF, QTimer, pyqtSignal
from PyQt6.QtGui import (QPainter, QPixmap, QColor, QPen, QBrush,
                         QTransform, QPainterPath, QFont, QFontMetricsF)
import math
import time
from collections import OrderedDict

from .overlay import BoxOverlay

# Hide the right-click magnifier when the page is already displayed at or above
# this zoom. The page display zoom is capped at 5.0 (main_window._MAX_RENDER_ZOOM),
# so the magnifier (fixed ~10x absolute) is only ever hidden near max zoom, where
# it adds little over the already-large on-screen view.
MAGNIFIER_HIDE_ZOOM = 4.5


class PageWidget(QGraphicsView):
    clicked = pyqtSignal(object)
    empty_clicked = pyqtSignal()
    mouse_moved = pyqtSignal(object)
    zoom_changed = pyqtSignal(float)
    double_clicked = pyqtSignal()
    page_nav = pyqtSignal(int)
    page_activated = pyqtSignal(int)
    overview_clicked = pyqtSignal(int)
    view_resized = pyqtSignal()
    magnifier_requested = pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)

        self.pixmap_item = None
        self._detail_item = None
        self._page = None
        self._render_zoom = 1.0
        self._overlay = BoxOverlay()
        self._drag_start = None
        self._is_dragging = False
        self._overprint_active = False

        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)

        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        # Disable the default context menu — it triggers on right-click and
        # blocks the UI thread (app freeze). Right button is used for the
        # magnifier instead. Must be set on BOTH the view and its viewport,
        # since the contextMenuEvent is delivered to the viewport widget.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.viewport().setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        self._tooltip_label = None

        self._magnifier_active = False
        self._magnifier_pos = None
        self._magnifier_allowed = True
        self._source_qimage = None
        self._source_cmyk = None
        self._sampler_size = 1
        # CMYK badge values for the magnifier, resolved on the main window
        # (prefers the exact stored source color). Tuple of 4 floats (0..100),
        # or None until the first resolution. _mag_badge_approx marks the
        # rendered-only fallback (no resolvable exact source).
        self._mag_badge_cmyk = None
        self._mag_badge_approx = False
        # Cached high-quality magnifier image (rendered off the paint path,
        # asynchronously on the background render worker).
        self._mag_hq_img = None
        self._mag_hq_pos = None
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self._emit_deferred_click)
        self._pending_click_pos = None

        self._is_light_theme = False
        self._overview_active = False
        self._overview_entries = []
        self._overview_thumbs = OrderedDict()
        self._overview_current = 0
        self._overview_ov_scale = 1.0
        self._overview_accent = QColor('#1de9b6')
        self._overview_paper = QColor(235, 235, 235)
        self._overview_border = QColor(90, 90, 90)
        self._overview_label = QColor(220, 220, 220)
        self._overview_placeholder = QColor(110, 110, 110)
        self._apply_theme_colors(False)

    def _apply_theme_colors(self, is_light):
        self._is_light_theme = is_light
        if is_light:
            self._viewport_bg = '#d0d0d0'
            self._badge_bg = QColor(240, 240, 240, 220)
            self._badge_text = QColor(30, 30, 30)
            self._mask_color = QColor(160, 160, 160)
            self._magnifier_bg = QColor(200, 200, 200)
            self._tip_bg = '#e8e8e8'
            self._tip_text = '#212121'
            self._tip_border = '#ccc'
        else:
            self._viewport_bg = '#404040'
            self._badge_bg = QColor(30, 30, 30, 220)
            self._badge_text = QColor(255, 255, 255)
            self._mask_color = QColor(64, 64, 64)
            self._magnifier_bg = QColor(40, 40, 40)
            self._tip_bg = '#333'
            self._tip_text = '#fff'
            self._tip_border = '#666'
        if is_light:
            self._overview_paper = QColor(255, 255, 255)
            self._overview_border = QColor(150, 150, 150)
            self._overview_label = QColor(60, 60, 60)
            self._overview_placeholder = QColor(200, 200, 200)
        else:
            self._overview_paper = QColor(235, 235, 235)
            self._overview_border = QColor(90, 90, 90)
            self._overview_label = QColor(220, 220, 220)
            self._overview_placeholder = QColor(110, 110, 110)
        self.setStyleSheet(f"QGraphicsView {{ background: {self._viewport_bg}; border: none; }}")
        if self._tooltip_label:
            self._tooltip_label.setStyleSheet(
                f"background: {self._tip_bg}; color: {self._tip_text}; "
                f"padding: 6px 12px; border: 1px solid {self._tip_border}; "
                f"border-radius: 3px; font: 14px monospace;")

    def set_pixmap(self, qimage, page, render_zoom=1.0, overprint_active=False,
                   cmyk_buf=None):
        self._page = page
        self._render_zoom = render_zoom
        self._overprint_active = overprint_active
        self._source_qimage = qimage
        if cmyk_buf is not None:
            self._source_cmyk = cmyk_buf
        self._mag_badge_cmyk = None
        self._mag_badge_approx = False
        self.scene.clear()
        self._detail_item = None
        self.pixmap_item = QGraphicsPixmapItem(QPixmap.fromImage(qimage))
        self.pixmap_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.scene.addItem(self.pixmap_item)
        r = self.pixmap_item.boundingRect()
        margin_w = max(r.width(), self.viewport().width()) * 2.0
        margin_h = max(r.height(), self.viewport().height()) * 2.0
        self.scene.setSceneRect(r.adjusted(-margin_w, -margin_h, margin_w, margin_h))

    def set_detail_overlay(self, qimage, scene_x, scene_y, item_scale_x,
                            item_scale_y):
        """Place a high-resolution detail tile on top of the base pixmap.

        The tile covers only the visible region but is rendered at the true
        display zoom, so it appears vector-sharp. ``item_scale_x``/``item_scale_y``
        map the tile's native pixels back to the base-render scene units. They
        are derived from the tile's ACTUAL pixel dimensions (not from the
        nominal zoom ratio), otherwise a 1px rounding difference in the clip
        render would scale the whole tile slightly wrong and shift its content
        by up to a pixel relative to the base."""
        if self.pixmap_item is None or self.pixmap_item.scene() is None:
            return
        self.clear_detail_overlay()
        item = QGraphicsPixmapItem(QPixmap.fromImage(qimage))
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        from PyQt6.QtGui import QTransform
        item.setTransform(QTransform.fromScale(item_scale_x, item_scale_y))
        item.setPos(scene_x, scene_y)
        item.setZValue(1.0)
        self.scene.addItem(item)
        self._detail_item = item

    def clear_detail_overlay(self):
        if self._detail_item is not None:
            try:
                if self._detail_item.scene() is not None:
                    self.scene.removeItem(self._detail_item)
            except Exception:
                pass
            self._detail_item = None

    def set_interactive_transform_quality(self, interactive):
        if self.pixmap_item is None or self.pixmap_item.scene() is None:
            return
        mode = (
            Qt.TransformationMode.FastTransformation
            if interactive
            else Qt.TransformationMode.SmoothTransformation
        )
        self.pixmap_item.setTransformationMode(mode)

    def show_color_tooltip(self, pos, text):
        if not self._tooltip_label:
            from PyQt6.QtWidgets import QLabel
            self._tooltip_label = QLabel(self.viewport())
            self._tooltip_label.setStyleSheet(
                f"background: {self._tip_bg}; color: {self._tip_text}; "
                f"padding: 6px 12px; border: 1px solid {self._tip_border}; "
                f"border-radius: 3px; font: 14px monospace;"
            )
            self._tooltip_label.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._tooltip_label.setText(text)
        self._tooltip_label.adjustSize()
        pm = self._tooltip_label.sizeHint()
        x = pos.x() + 16
        y = pos.y() - pm.height() - 8
        vp = self.viewport()
        if x + pm.width() > vp.width():
            x = vp.width() - pm.width() - 4
        if y < 0:
            y = pos.y() + 16
        self._tooltip_label.move(x, y)
        self._tooltip_label.show()

    def hide_color_tooltip(self):
        if self._tooltip_label:
            self._tooltip_label.hide()

    def _emit_deferred_click(self):
        if self._pending_click_pos is not None:
            pos = self._pending_click_pos
            self._pending_click_pos = None
            self.clicked.emit(pos)

    @property
    def overlay(self):
        return self._overlay

    @property
    def page(self):
        return self._page

    @property
    def is_magnifier_active(self):
        return self._magnifier_active

    def set_magnifier_allowed(self, allowed):
        self._magnifier_allowed = allowed
        if not allowed:
            self._clear_magnifier_state()
            self.viewport().update()

    def set_sampler_size(self, size):
        try:
            self._sampler_size = int(size)
        except (TypeError, ValueError):
            self._sampler_size = 1

    def set_magnifier_hq(self, img, pos):
        """Receive the asynchronously rendered high-quality magnifier image
        for ``pos`` (a QPointF in scene coordinates) from the render worker."""
        if img is None or img.isNull() or img.width() <= 0:
            return
        self._mag_hq_img = img
        self._mag_hq_pos = pos
        self.viewport().update()

    def _clear_magnifier_state(self):
        self._magnifier_active = False
        self._magnifier_pos = None
        self._mag_hq_img = None
        self._mag_hq_pos = None
        self._mag_badge_cmyk = None
        self._mag_badge_approx = False

    def set_magnifier_cmyk(self, cmyk_0_100, approximate):
        """Provide the CMYK badge values (4 floats, 0..100) for the current
        magnifier position. Prefer the exact stored source color; set
        ``approximate`` True when only a rendered value is available so the
        badge can mark it as such."""
        self._mag_badge_cmyk = cmyk_0_100
        self._mag_badge_approx = bool(approximate)

    def request_hq_magnifier(self, scene_pos):
        """Request the high-quality magnifier image for the cursor position.

        The actual render happens asynchronously on the background render
        worker (the same thread that renders the page itself), so it uses the
        exact same color pipeline (raw CMYK -> overprint/separation -> ICC->RGB)
        and never blocks or crashes the GUI thread. Results arrive via
        ``set_magnifier_hq``.
        """
        if self._render_zoom >= MAGNIFIER_HIDE_ZOOM:
            return
        if scene_pos is None or not self._magnifier_active:
            return
        if self._source_qimage is None or self._page is None or self._render_zoom <= 0:
            return
        self.magnifier_requested.emit(scene_pos.x(), scene_pos.y())

    def drawForeground(self, painter, rect):
        if self._overview_active:
            self._draw_overview(painter)
            return
        if not self._page:
            return

        pages = getattr(self._page, 'pages', None)
        if pages:
            for pg in pages:
                self._draw_page_overlays(painter, pg)
        else:
            self._draw_single_overlay(painter, self._page)

        self._draw_magnifier(painter)

    # ===================== Overview mode =====================
    def enter_overview(self, entries, ov_scale, current_page, scene_rect,
                        accent, zoom):
        self._overview_active = True
        self._overview_entries = entries
        self._overview_ov_scale = ov_scale
        self._overview_current = current_page
        self._overview_accent = accent
        self._overview_thumbs = OrderedDict()
        self._clear_magnifier_state()
        self.scene.clear()
        self.pixmap_item = None
        self._detail_item = None
        self._page = None
        self._source_qimage = None
        self._source_cmyk = None
        self.hide_color_tooltip()
        self.scene.setSceneRect(scene_rect)
        self.resetTransform()
        self.scale(1.0, 1.0)
        self.set_overview_zoom(zoom)
        self._center_overview_on_current()
        self.viewport().update()

    def exit_overview(self):
        self._overview_active = False
        self._overview_entries = []
        self._overview_thumbs = OrderedDict()
        self.scene.clear()

    def set_overview_thumb(self, page_index, qimage):
        if not self._overview_active:
            return
        pix = QPixmap.fromImage(qimage)
        if pix.isNull():
            return
        self._overview_thumbs[page_index] = pix
        self._overview_thumbs.move_to_end(page_index)
        while len(self._overview_thumbs) > 30:
            self._overview_thumbs.popitem(last=False)
        self.viewport().update()

    def update_overview_current(self, page_index):
        self._overview_current = page_index
        self.viewport().update()

    def _block_overview_scroll(self, blocked):
        self.verticalScrollBar().blockSignals(blocked)
        self.horizontalScrollBar().blockSignals(blocked)

    def set_overview_zoom(self, zoom):
        if not self._overview_active:
            return
        vp = self.viewport().rect()
        center = (self.mapToScene(vp.center())
                  if (vp.width() > 0 and vp.height() > 0) else QPointF(0, 0))
        self.resetTransform()
        self.scale(zoom / self._overview_ov_scale, zoom / self._overview_ov_scale)
        if vp.width() > 0 and vp.height() > 0:
            self._block_overview_scroll(True)
            self.centerOn(center)
            self._block_overview_scroll(False)

    def _center_overview_on_current(self):
        self._center_overview_on_page(self._overview_current)

    def _center_overview_on_page(self, page_index):
        for e in self._overview_entries:
            if page_index in e['pages']:
                r = (e.get('rect2')
                     if (e.get('is_pair') and page_index == e['pages'][1])
                     else e['rect'])
                self._block_overview_scroll(True)
                self.centerOn(r.center())
                self._block_overview_scroll(False)
                return

    def overview_hit_test(self, scene_pos):
        if not self._overview_active:
            return None
        for e in self._overview_entries:
            if e.get('is_pair'):
                if e['rect2'].contains(scene_pos):
                    return e['pages'][1]
                if e['rect'].contains(scene_pos):
                    return e['pages'][0]
            elif e['rect'].contains(scene_pos):
                return e['pages'][0]
        return None

    def overview_centered_page(self):
        if not self._overview_active:
            return None
        vp = self.viewport().rect()
        center = self.mapToScene(vp.center())
        best = None
        best_dist = None
        for e in self._overview_entries:
            ry = (e['rect'].top() + e['rect'].bottom()) / 2.0
            d = abs(ry - center.y())
            if best_dist is None or d < best_dist:
                best_dist = d
                best = e
        if best is None:
            return None
        # prefer the sub-page whose rect contains the center horizontally
        for ri, r in enumerate([best['rect']] +
                               ([best['rect2']] if best.get('is_pair') else [])):
            if r.contains(center):
                return best['pages'][ri]
        return best['pages'][0]

    def overview_visible_pages(self):
        if not self._overview_active:
            return set()
        vp = self.viewport().rect()
        tl = self.mapToScene(vp.topLeft())
        br = self.mapToScene(vp.bottomRight())
        sr = QRectF(tl, br)
        pages = set()
        for e in self._overview_entries:
            if e['rect'].intersects(sr) or (e.get('is_pair')
                                           and e['rect2'].intersects(sr)):
                pages.update(e['pages'])
        return pages

    def _draw_overview(self, painter):
        if not self._overview_entries:
            return
        vp = self.viewport().rect()
        tl = self.mapToScene(vp.topLeft())
        br = self.mapToScene(vp.bottomRight())
        sr = QRectF(tl, br)
        scale = self.viewportTransform().m11()

        label_font = QFont("sans-serif")
        label_font.setPixelSize(max(1, int(13 / scale)))
        label_font.setBold(True)
        fm = QFontMetricsF(label_font)
        lh = fm.height()
        lpad = 14.0 / scale

        for e in self._overview_entries:
            rects = [e['rect']]
            if e.get('is_pair'):
                rects.append(e['rect2'])
            if not any(r.intersects(sr) for r in rects):
                continue
            is_pair = e.get('is_pair')
            for ri, r in enumerate(rects):
                pn = e['pages'][ri] if ri < len(e['pages']) else e['pages'][0]
                painter.fillRect(r, self._overview_paper)
                thumb = self._overview_thumbs.get(pn)
                if thumb is not None:
                    painter.drawPixmap(r, thumb, QRectF(thumb.rect()))
                else:
                    painter.fillRect(r, self._overview_placeholder)
                painter.setPen(QPen(self._overview_border, 1.0 / scale))
                painter.drawRect(r)
                # page number label OUTSIDE, beside the page (pair-aware):
                # single/left page -> to the left; right page of a pair -> right
                text = str(pn + 1)
                tw = fm.horizontalAdvance(text)
                cy = r.center().y()
                on_right = is_pair and ri == 1
                if on_right:
                    lr = QRectF(r.right() + lpad, cy - lh / 2, tw, lh)
                    align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                else:
                    lr = QRectF(r.left() - lpad - tw, cy - lh / 2, tw, lh)
                    align = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                painter.setPen(self._overview_label)
                painter.setFont(label_font)
                painter.drawText(lr, align, text)
            # current-page highlight across the whole entry
            if (e['pages'][0] == self._overview_current
                    or (e.get('is_pair')
                        and e['pages'][1] == self._overview_current)):
                hr = (e['rect'].united(e['rect2'])
                      if e.get('is_pair') else e['rect'])
                painter.setPen(QPen(self._overview_accent, 3.0 / scale))
                painter.drawRect(hr.adjusted(-2 / scale, -2 / scale,
                                            2 / scale, 2 / scale))

    def _draw_page_overlays(self, painter, pg):
        z = self._render_zoom
        x_off_pts = pg['x_offset']
        x_off_px = int(x_off_pts * z)
        page_rect = pg['rect']
        boxes = pg['boxes']

        if self._overprint_active:
            w = page_rect.width * z
            h = page_rect.height * z
            painter.setBrush(QColor(255, 128, 0, 30))
            painter.setPen(QPen(QColor(255, 80, 0, 180), max(1, int(3 * z))))
            painter.drawRect(x_off_px, 0, int(w), int(h))
            painter.setFont(self._overprint_font())
            tw = 160 * z
            th = 24 * z
            painter.setBrush(self._badge_bg)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRect(x_off_px + int(w) - int(tw) - 10, int(h) - int(th) - 10,
                             int(tw), int(th))
            painter.setPen(self._badge_text)
            painter.drawText(x_off_px + int(w) - int(tw) - 2, int(h) - 16, "OVERPRINT ON")

        mask_box = getattr(self, '_mask_box', None)
        if mask_box:
            b = boxes.get(mask_box)
            if b and b.x0 < b.x1 and b.y0 < b.y1:
                painter.setBrush(self._mask_color)
                painter.setPen(Qt.PenStyle.NoPen)
                pw = math.ceil(page_rect.width * z)
                ph = math.ceil(page_rect.height * z)
                ph_pdf = page_rect.height
                bx0 = math.ceil(b.x0 * z) + x_off_px
                by0 = math.ceil((ph_pdf - b.y1) * z)
                bx1 = math.ceil(b.x1 * z) + x_off_px
                by1 = math.ceil((ph_pdf - b.y0) * z)
                painter.drawRect(x_off_px, 0, pw, by0)
                painter.drawRect(x_off_px, by1, pw, ph - by1)
                painter.drawRect(x_off_px, by0, bx0 - x_off_px, by1 - by0)
                painter.drawRect(bx1, by0, x_off_px + pw - bx1, by1 - by0)

        self._overlay.draw(painter, None, z, x_off_px, boxes=boxes, page_height=page_rect.height)

    def _draw_single_overlay(self, painter, page):
        z = self._render_zoom

        if self._overprint_active:
            r = page.rect
            w = r.width * z
            h = r.height * z
            # Semi-transparent orange fill for the page area
            painter.setBrush(QColor(255, 128, 0, 30))
            painter.setPen(QPen(QColor(255, 80, 0, 180), max(1, int(4 * z))))
            painter.drawRect(0, 0, int(w), int(h))
            # "OVERPRINT ON" label at bottom-right
            painter.setFont(self._overprint_font())
            painter.setPen(QColor(255, 80, 0, 255))
            painter.setBrush(self._badge_bg)
            tw = 160 * z
            th = 24 * z
            painter.drawRect(int(w) - int(tw) - 10, int(h) - int(th) - 10,
                             int(tw), int(th))
            painter.setPen(self._badge_text)
            painter.drawText(int(w) - int(tw) - 2, int(h) - 16, "OVERPRINT ON")

        mask_box = getattr(self, '_mask_box', None)
        if mask_box:
            b = getattr(page, f'{mask_box}box', None)
            if b and b.x0 < b.x1 and b.y0 < b.y1:
                painter.setBrush(self._mask_color)
                painter.setPen(Qt.PenStyle.NoPen)
                if self.pixmap_item:
                    pr = self.pixmap_item.boundingRect()
                    pw = math.ceil(pr.width())
                    ph = math.ceil(pr.height())
                else:
                    pw = math.ceil(page.rect.width * z)
                    ph = math.ceil(page.rect.height * z)
                ph_pdf = page.rect.height
                bx0 = math.ceil(b.x0 * z)
                by0 = math.ceil((ph_pdf - b.y1) * z)
                bx1 = math.ceil(b.x1 * z)
                by1 = math.ceil((ph_pdf - b.y0) * z)
                painter.drawRect(0, 0, pw, by0)
                painter.drawRect(0, by1, pw, ph - by1)
                painter.drawRect(0, by0, bx0, by1 - by0)
                painter.drawRect(bx1, by0, pw - bx1, by1 - by0)

        self._overlay.draw(painter, page, z)

    def _overprint_font(self):
        f = QFont("sans-serif", int(10 * self._render_zoom))
        f.setBold(True)
        return f

    def _draw_magnifier(self, painter):
        if (not self._magnifier_allowed or not self._magnifier_active
                or self._magnifier_pos is None
                or not self._source_qimage
                or self._render_zoom >= MAGNIFIER_HIDE_ZOOM):
            return

        view_pos = self.mapFromScene(self._magnifier_pos)
        cx = int(view_pos.x())
        cy = int(view_pos.y())

        radius = 100
        mag_zoom = 10

        pos = self._magnifier_pos
        scene_x_f = pos.x()
        scene_y_f = pos.y()
        si = int(round(scene_x_f))
        sj = int(round(scene_y_f))

        qimage = self._source_qimage
        if (scene_x_f < 0 or scene_x_f >= qimage.width()
                or scene_y_f < 0 or scene_y_f >= qimage.height()):
            return

        # Use the cached high-quality re-render (built off the paint path on the
        # background worker) whenever the cursor is still inside the region it
        # covers. The HQ render lags the cursor by one worker pass, so we must
        # NOT require an exact position match (that would flicker the sharp
        # image against the pixelated fallback on every move). Instead we draw
        # the HQ image at its true position so the content stays aligned to the
        # cursor as it pans. Only fall back to the pixelated screen sampling
        # when no HQ is available yet or the cursor left the rendered region
        # (fast flick). Never render synchronously inside paint — that would
        # block the UI thread and freeze the app on right-drag.
        magnifier_img = None
        hq_cx, hq_cy = cx, cy
        if (self._mag_hq_img is not None
                and self._mag_hq_pos is not None):
            hq_view = self.mapFromScene(self._mag_hq_pos)
            if math.hypot(hq_view.x() - cx, hq_view.y() - cy) < radius * 0.98:
                magnifier_img = self._mag_hq_img
                hq_cx, hq_cy = hq_view.x(), hq_view.y()

        painter.save()
        painter.setWorldTransform(QTransform())

        clip_path = QPainterPath()
        clip_path.addEllipse(QPointF(cx, cy), radius, radius)
        painter.setClipPath(clip_path)

        # Dark background inside circle
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._magnifier_bg)
        painter.drawEllipse(QPointF(cx, cy), radius, radius)

        # Base layer: pixelated screen sampling, always aligned to the cursor
        # and filling the circle. It is the direct view when no HQ is ready and
        # acts as a gap-filler underneath the (panned) HQ image.
        src_half = math.ceil(radius / mag_zoom)
        x1 = max(0, si - src_half)
        y1 = max(0, sj - src_half)
        x2 = min(qimage.width(), si + src_half + 1)
        y2 = min(qimage.height(), sj + src_half + 1)
        src_width = x2 - x1
        src_height = y2 - y1
        if src_width > 0 and src_height > 0:
            fb_dst_x = cx - (si - x1) * mag_zoom
            fb_dst_y = cy - (sj - y1) * mag_zoom
            fb_dst_w = src_width * mag_zoom
            fb_dst_h = src_height * mag_zoom
            painter.drawImage(
                QRectF(fb_dst_x, fb_dst_y, fb_dst_w, fb_dst_h),
                qimage, QRectF(x1, y1, src_width, src_height))

        if magnifier_img is not None:
            # High-quality PDF re-render overlaid, drawn at its true position so
            # the content tracks the cursor as it pans (no flicker against the
            # pixelated base layer).
            src_w = magnifier_img.width()
            src_h = magnifier_img.height()
            if src_w > 0 and src_h > 0:
                scale = (radius * 2) / max(src_w, src_h)
                dst_w = int(src_w * scale)
                dst_h = int(src_h * scale)
                dst_x = int(hq_cx - dst_w // 2)
                dst_y = int(hq_cy - dst_h // 2)
                painter.drawImage(
                    QRectF(dst_x, dst_y, dst_w, dst_h),
                    magnifier_img, QRectF(0, 0, src_w, src_h))

                # Highlight the exact sampled point (the cursor). Maximum-contrast
                # mark: a black border (visible on light/white areas) around a
                # white square (visible on dark areas), so it reads on any color.
                center_mark = max(3, int(4 * scale))
                border = max(2, int(center_mark * 0.4))
                outer = center_mark + border * 2
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(0, 0, 0))
                painter.drawRect(QRectF(cx - outer / 2,
                                        cy - outer / 2,
                                        outer, outer))
                painter.setBrush(QColor(255, 255, 255))
                painter.drawRect(QRectF(cx - center_mark / 2,
                                        cy - center_mark / 2,
                                        center_mark, center_mark))
        elif src_width > 0 and src_height > 0:
            # No HQ available: pixel grid + center highlight on the fallback.
            pen_grid = QPen(QColor(255, 255, 255, 40), 0.5)
            painter.setPen(pen_grid)
            for i in range(src_width + 1):
                gx = fb_dst_x + i * mag_zoom
                painter.drawLine(QPointF(gx, fb_dst_y),
                                QPointF(gx, fb_dst_y + fb_dst_h))
            for j in range(src_height + 1):
                gy = fb_dst_y + j * mag_zoom
                painter.drawLine(QPointF(fb_dst_x, gy),
                                QPointF(fb_dst_x + fb_dst_w, gy))
            px_x = fb_dst_x + (si - x1) * mag_zoom
            px_y = fb_dst_y + (sj - y1) * mag_zoom
            # Maximum-contrast center mark: black border + white fill square.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0))
            painter.drawRect(QRectF(px_x - 1, px_y - 1, mag_zoom + 2, mag_zoom + 2))
            painter.setBrush(QColor(255, 255, 255))
            painter.drawRect(QRectF(px_x, px_y, mag_zoom, mag_zoom))

        painter.setClipping(False)

        # Outline ring: a contrasting dark outer stroke (visible on light page
        # areas) followed by a bright inner stroke (visible on dark page areas),
        # so the magnifier edge is always clearly delineated.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(0, 0, 0, 180), 3))
        painter.drawEllipse(QPointF(cx, cy), radius + 1, radius + 1)
        painter.setPen(QPen(QColor(255, 255, 255, 220), 2))
        painter.drawEllipse(QPointF(cx, cy), radius, radius)

        # CMYK values next to the magnifier. Use the resolved badge values
        # (exact stored source color when available; rendered fallback marked
        # as approximate) supplied by the main window. Fall back to sampling
        # the rendered CMYK buffer only if no resolved value is available yet.
        badge = self._mag_badge_cmyk
        if badge is None and self._source_cmyk is not None:
            h, w = self._source_cmyk.shape[:2]
            if 0 <= sj < h and 0 <= si < w:
                size = self._sampler_size
                if size > 1:
                    half = int(round((size / 2.0) * self._render_zoom))
                    y0 = max(0, sj - half)
                    y1 = min(h, sj + half + 1)
                    x0 = max(0, si - half)
                    x1 = min(w, si + half + 1)
                    cmyk = self._source_cmyk[y0:y1, x0:x1].mean(axis=(0, 1))
                else:
                    cmyk = self._source_cmyk[sj, si]
                badge = (cmyk[0] / 2.55, cmyk[1] / 2.55,
                         cmyk[2] / 2.55, cmyk[3] / 2.55)
                self._mag_badge_approx = True

        if badge is not None:
            c, m, y, k = (badge[0], badge[1], badge[2], badge[3])

            label_font = QFont("monospace", 9)
            painter.setFont(label_font)

            lines = [
                f"C  {c:.0f}%",
                f"M  {m:.0f}%",
                f"Y  {y:.0f}%",
                f"K  {k:.0f}%",
            ]
            if self._mag_badge_approx:
                lines.append("≈ approx")

            text_x = cx + radius + 16
            text_y = cy - 28
            fm = painter.fontMetrics()
            lh = fm.height() + 2
            bg_w = 0
            for line in lines:
                bg_w = max(bg_w, fm.horizontalAdvance(line))
            bg_w += 16
            bg_h = lh * len(lines) + 8
            if text_x + bg_w > self.viewport().width():
                text_x = cx - radius - bg_w - 16
            if text_y + bg_h > self.viewport().height():
                text_y = self.viewport().height() - bg_h - 4
            if text_y < 0:
                text_y = 4

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._badge_bg)
            painter.drawRoundedRect(QRectF(text_x, text_y, bg_w, bg_h), 4, 4)

            painter.setPen(self._badge_text)
            for i, line in enumerate(lines):
                painter.drawText(QPointF(text_x + 8, text_y + lh * (i + 1) - 2),
                                 line)

        painter.restore()

    def contextMenuEvent(self, event):
        # Right-click is used for the magnifier; never show a context menu
        # (showing one would block the UI thread).
        event.accept()

    def mousePressEvent(self, event):
        if event.button() in (Qt.MouseButton.LeftButton,
                               Qt.MouseButton.MiddleButton):
            self._drag_start = event.pos()
            self._is_dragging = False
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.MouseButton.RightButton:
            if self._overview_active:
                super().mousePressEvent(event)
                return
            self._click_timer.stop()
            self._pending_click_pos = None
            if not self._magnifier_allowed:
                super().mousePressEvent(event)
                return
            scene_pos = self.mapToScene(event.pos())
            if (self.pixmap_item is not None
                    and self.pixmap_item.boundingRect().contains(scene_pos)):
                if self._render_zoom < MAGNIFIER_HIDE_ZOOM:
                    self._magnifier_active = True
                    self._magnifier_pos = scene_pos
                    self.hide_color_tooltip()
                    self.request_hq_magnifier(scene_pos)
                    # Resolve the CMYK badge (exact stored source) for this position.
                    self.mouse_moved.emit(scene_pos)
                    self.viewport().update()
                # else: page already zoomed in past the threshold -> no magnifier
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        scene_pos = self.mapToScene(event.pos())
        on_page = (self.pixmap_item is not None
                   and self.pixmap_item.boundingRect().contains(scene_pos))

        if event.buttons() & (Qt.MouseButton.LeftButton | Qt.MouseButton.MiddleButton):
            if not self._is_dragging and self._drag_start:
                dist = (event.pos() - self._drag_start).manhattanLength()
                if dist > 10:
                    self._is_dragging = True
                    self._click_timer.stop()
                    self._pending_click_pos = None
            if self._is_dragging:
                sb_h = self.horizontalScrollBar()
                sb_v = self.verticalScrollBar()
                dx = self._drag_start.x() - event.pos().x()
                dy = self._drag_start.y() - event.pos().y()
                sb_h.setValue(sb_h.value() + dx)
                sb_v.setValue(sb_v.value() + dy)
                self._drag_start = event.pos()
        elif event.buttons() & Qt.MouseButton.RightButton:
            if on_page and self._magnifier_active and self._magnifier_allowed:
                self._magnifier_pos = scene_pos
                self.hide_color_tooltip()
                # Throttle the per-move work (repaint + signals + HQ render) so
                # continuous right-drag doesn't flood the UI thread and freeze.
                now = time.monotonic()
                if now - getattr(self, '_mag_last_update', 0) >= 1/30.0:
                    self._mag_last_update = now
                    self.request_hq_magnifier(scene_pos)
                    self.mouse_moved.emit(scene_pos)
                    # Repaint the whole viewport: the CMYK badge drawn beside
                    # the magnifier circle (and that may clamp to a screen edge)
                    # is not covered by a small region, leaving smeared remnants.
                    self.viewport().update()
        else:
            if on_page:
                self.mouse_moved.emit(scene_pos)
            else:
                self.hide_color_tooltip()

        super().mouseMoveEvent(event)

        if self._is_dragging:
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        elif self._magnifier_active and self._magnifier_allowed and on_page:
            self.viewport().setCursor(Qt.CursorShape.BlankCursor)
        elif on_page:
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._overview_active:
                if not self._is_dragging:
                    idx = self.overview_hit_test(self.mapToScene(event.pos()))
                    if idx is not None:
                        self.overview_clicked.emit(idx)
                self._drag_start = None
                self._is_dragging = False
                self.viewport().setCursor(Qt.CursorShape.CrossCursor)
                super().mouseReleaseEvent(event)
                return
            if not self._is_dragging:
                if self.pixmap_item:
                    scene_pos = self.mapToScene(event.pos())
                    if self.pixmap_item.boundingRect().contains(scene_pos):
                        self._pending_click_pos = scene_pos
                        from PyQt6.QtWidgets import QApplication
                        self._click_timer.start(
                            QApplication.doubleClickInterval() + 30)
                else:
                    self.empty_clicked.emit()
        if event.button() in (Qt.MouseButton.LeftButton,
                               Qt.MouseButton.MiddleButton):
            self._drag_start = None
            self._is_dragging = False
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        elif event.button() == Qt.MouseButton.RightButton:
            self._clear_magnifier_state()
            self.viewport().update()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._click_timer.stop()
            self._pending_click_pos = None
            if self._overview_active:
                idx = self.overview_hit_test(self.mapToScene(event.pos()))
                if idx is not None:
                    self.page_activated.emit(idx)
            else:
                self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)

    def leaveEvent(self, event):
        self.hide_color_tooltip()
        self._clear_magnifier_state()
        self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        self.viewport().update()
        super().leaveEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.view_resized.emit()

    def wheelEvent(self, event):
        self._clear_magnifier_state()
        delta = event.angleDelta().y()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if delta > 0:
                self.page_nav.emit(-1)
            else:
                self.page_nav.emit(1)
        else:
            if delta > 0:
                self.zoom_changed.emit(1.0)
            else:
                self.zoom_changed.emit(-1.0)
        event.accept()
