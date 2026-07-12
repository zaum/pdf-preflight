import sys
import os
import re
import threading
import queue
import time
from collections import OrderedDict
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QToolBar, QDockWidget,
    QLabel, QSpinBox, QCheckBox, QGroupBox, QGridLayout,
    QFileDialog, QApplication, QScrollArea, QMessageBox,
    QListWidget, QListWidgetItem, QDialog, QDialogButtonBox,
    QFormLayout, QComboBox, QToolButton,
    QAbstractScrollArea, QMenu, QRadioButton,
    QKeySequenceEdit, QSlider, QStyle
)
from PyQt6.QtCore import Qt, QSize, QSettings, pyqtSignal, QObject, QPointF, QUrl, QTimer, QRectF
from PyQt6.QtWidgets import QSizePolicy
from PyQt6.QtGui import QAction, QFont, QCursor, QImage, QIcon, QKeySequence, QShortcut, QPalette, QColor, QPixmap, QFontMetrics

from viewer.page_widget import PageWidget
from viewer.render_engine import RenderEngine, get_cmyk_icc_path
from preview.color_picker import ColorPicker
from preview.overprint import OverprintPreview
from preview.separation import SeparationPreview
from preview.simulation import SimulationEngine
from preflight.analyzer import PreflightAnalyzer
from preflight.rules import RuleEngine

# Max total per-channel CMYK difference (0-255 scale, sum of 4 channels)
# allowed between a resolved source color and the rendered pixel before we
# fall back to the rendered value (e.g. white text on a colored object).
_CMYK_SOURCE_MAX_DIFF = 70


def _cmyk_diff(a, b):
    """Sum of absolute per-channel differences of two (C,M,Y,K) 0-255 tuples."""
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2]) + abs(a[3] - b[3])


class CollapsibleBlock(QWidget):
    def __init__(self, title, settings_key, parent=None):
        super().__init__(parent)
        self._settings_key = settings_key
        self._content_widget = None
        self._settings = QSettings("PDFPreflight", "Viewer")

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        p = self.palette()
        p.setColor(QPalette.ColorRole.Window, QColor("#282828"))
        self.setPalette(p)
        self.setAutoFillBackground(True)
        self._base_palette = p

        self._btn = QToolButton()
        self._btn.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._btn.setArrowType(Qt.ArrowType.DownArrow)
        self._btn.setText(title)
        self._btn.setCheckable(True)
        self._btn.setChecked(True)
        self._btn.setFixedHeight(30)
        self._btn.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._btn.setStyleSheet("""
            QToolButton {
                background: transparent;
                border: none;
                padding: 4px 8px 4px 0;
                text-align: left;
                font-weight: bold;
                border-radius: 3px;
                margin: 1px 0;
            }
        """)
        self._btn.toggled.connect(self._on_toggle)

        self._content_area = QWidget()
        self._content_area.setAutoFillBackground(True)

        self._layout.addWidget(self._btn)
        self._layout.addWidget(self._content_area)

        self._restore_state()

    def _on_toggle(self, checked):
        self._content_area.setVisible(checked)
        self._btn.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    def set_content(self, widget):
        if self._content_widget:
            self._content_area.layout().removeWidget(self._content_widget)
            self._content_widget.deleteLater()
        self._content_widget = widget
        cl = self._content_area.layout()
        if cl is None:
            cl = QVBoxLayout(self._content_area)
            cl.setContentsMargins(16, 2, 2, 2)
        cl.addWidget(widget)

    def save_state(self):
        self._settings.setValue(
            f"collapse/{self._settings_key}", self._btn.isChecked())

    def _restore_state(self):
        val = self._settings.value(f"collapse/{self._settings_key}")
        if val is not None:
            checked = (str(val).lower() == 'true')
            self._btn.setChecked(checked)
            self._on_toggle(checked)

    def set_title(self, text):
        self._btn.setText(text)


class SettingsDialog(QDialog):
    shortcuts_changed = pyqtSignal()
    theme_changed = pyqtSignal(str)
    dock_side_changed = pyqtSignal(str)
    box_unit_changed = pyqtSignal(str)
    magnifier_changed = pyqtSignal(bool)
    dbl_click_zoom_changed = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        screen = QApplication.primaryScreen()
        avail_h = screen.availableGeometry().height() if screen else 800
        self.setMinimumHeight(max(520, int(avail_h * 0.5)))
        self._settings = QSettings("PDFPreflight", "Viewer")
        self._density_timer = QTimer(self)
        self._density_timer.setSingleShot(True)
        self._density_timer.setInterval(200)
        self._density_timer.timeout.connect(self._apply_density)
        from PyQt6.QtWidgets import QButtonGroup, QFrame

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        form = QFormLayout()
        form.setVerticalSpacing(8)

        # Base font size for all widget types (buttons keep their own size via QFont)
        self.setStyleSheet("""
            QLabel { font-size: 9pt; }
            QRadioButton, QCheckBox, QComboBox, QSlider, QKeySequenceEdit { font-size: 9pt; }
            QRadioButton::indicator { width: 14px; height: 14px; border-radius: 3px; }
        """)

        def sep():
            line = QFrame()
            line.setFrameShape(QFrame.Shape.HLine)
            line.setFrameShadow(QFrame.Shadow.Sunken)
            line.setStyleSheet("QFrame { color: rgba(128,128,128,0.3); max-height: 1px; margin: 4px 0; }")
            return line

        # --- User Interface ---
        form.addRow(sep())
        ui_lbl = QLabel("User Interface")
        ui_lbl.setStyleSheet("font-weight: bold; font-size: 12pt;")
        form.addRow(ui_lbl)

        # Theme selection
        theme_lbl = QLabel("Theme")
        theme_lbl.setStyleSheet("font-weight: bold;")
        form.addRow(theme_lbl)
        self.dark_radio = QRadioButton("Dark")
        self.light_radio = QRadioButton("Light")
        theme_group = QButtonGroup(self)
        theme_group.addButton(self.dark_radio)
        theme_group.addButton(self.light_radio)
        current_mode = self._settings.value("ui/theme_mode", "dark")
        self.dark_radio.setChecked(current_mode == "dark")
        self.light_radio.setChecked(current_mode != "dark")
        self.dark_radio.toggled.connect(lambda c: c and self._on_theme_mode_changed("dark"))
        self.light_radio.toggled.connect(lambda c: c and self._on_theme_mode_changed("light"))
        theme_row = QHBoxLayout()
        theme_row.addWidget(self.dark_radio)
        theme_row.addWidget(self.light_radio)
        theme_row.addStretch()
        form.addRow(theme_row)

        # Accent color
        accent_lbl = QLabel("Accent color")
        accent_lbl.setStyleSheet("font-weight: bold;")
        form.addRow(accent_lbl)
        accent_row = QHBoxLayout()
        self.accent_btn = QPushButton()
        self._accent_color_val = str(self._settings.value(
            "ui/accent_color", "#1de9b6"))
        self._update_accent_btn_color()
        self.accent_btn.setFixedSize(28, 28)
        self.accent_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.accent_btn.clicked.connect(self._pick_accent_color)
        self.accent_label = QLabel(self._accent_color_val)
        accent_row.addWidget(self.accent_btn)
        accent_row.addWidget(self.accent_label)
        accent_row.addStretch()
        form.addRow(accent_row)
        form.addRow(sep())

        # UI scale
        ui_scale_lbl = QLabel("UI scale")
        ui_scale_lbl.setStyleSheet("font-weight: bold;")
        form.addRow(ui_scale_lbl)
        slider_row = QHBoxLayout()
        self.ui_scale_slider = QSlider(Qt.Orientation.Horizontal)
        self.ui_scale_slider.setRange(-3, 3)
        self.ui_scale_slider.setValue(int(self._settings.value("ui/density_scale", 0)))
        self.ui_scale_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.ui_scale_slider.setTickInterval(1)
        self.ui_scale_slider.valueChanged.connect(self._on_density_changed)
        self.ui_scale_slider.sliderReleased.connect(self._apply_density)
        self.ui_scale_label = QLabel(str(self.ui_scale_slider.value()))
        self.ui_scale_label.setFixedWidth(24)
        slider_row.addWidget(self.ui_scale_slider)
        slider_row.addWidget(self.ui_scale_label)
        form.addRow(slider_row)
        form.addRow(sep())

        # Shift mode: radio buttons
        shift_lbl = QLabel("Shift key")
        shift_lbl.setStyleSheet("font-weight: bold;")
        form.addRow(shift_lbl)
        self.shift_show = QRadioButton("Show on Shift")
        self.shift_hide = QRadioButton("Hide on Shift")
        current_shift = self._settings.value("ui/shift_mode", "hide")
        self.shift_hide.setChecked(current_shift != "show")
        self.shift_show.setChecked(current_shift == "show")
        shift_row = QHBoxLayout()
        shift_row.addWidget(self.shift_show)
        shift_row.addWidget(self.shift_hide)
        shift_row.addStretch()
        form.addRow(shift_row)

        # Magnifier toggle
        self.magnifier_cb = QCheckBox("Enable magnifier (right-click)")
        self.magnifier_cb.setChecked(
            str(self._settings.value("ui/magnifier_enabled", "true")).lower() == "true")
        self.magnifier_cb.toggled.connect(
            lambda c: self.magnifier_changed.emit(c))
        magnifier_row = QHBoxLayout()
        magnifier_row.addWidget(self.magnifier_cb)
        magnifier_row.addStretch()
        form.addRow(magnifier_row)

        # Reopen last document on startup
        self.reopen_cb = QCheckBox("Reopen last document on startup")
        self.reopen_cb.setChecked(
            str(self._settings.value("ui/reopen_last", "false")).lower() == "true")
        self.reopen_cb.toggled.connect(
            lambda c: self._settings.setValue("ui/reopen_last", str(bool(c))))
        reopen_row = QHBoxLayout()
        reopen_row.addWidget(self.reopen_cb)
        reopen_row.addStretch()
        form.addRow(reopen_row)
        form.addRow(sep())

        # Box unit: radio buttons
        unit_lbl = QLabel("Page box unit")
        unit_lbl.setStyleSheet("font-weight: bold;")
        form.addRow(unit_lbl)
        self.unit_mm = QRadioButton("mm")
        self.unit_pt = QRadioButton("pt")
        unit_group = QButtonGroup(self)
        unit_group.addButton(self.unit_mm)
        unit_group.addButton(self.unit_pt)
        current_unit = self._settings.value("ui/box_unit", "mm")
        self.unit_mm.setChecked(current_unit == "mm")
        self.unit_pt.setChecked(current_unit != "mm")
        self.unit_mm.toggled.connect(lambda c: c and self._on_unit_changed("mm"))
        self.unit_pt.toggled.connect(lambda c: c and self._on_unit_changed("pt"))
        unit_row = QHBoxLayout()
        unit_row.addWidget(self.unit_mm)
        unit_row.addWidget(self.unit_pt)
        unit_row.addStretch()
        form.addRow(unit_row)
        form.addRow(sep())

        # Side panel position
        side_lbl = QLabel("Side Panel Position")
        side_lbl.setStyleSheet("font-weight: bold;")
        form.addRow(side_lbl)
        self.side_left = QRadioButton("Left")
        self.side_right = QRadioButton("Right")
        side_group = QButtonGroup(self)
        side_group.addButton(self.side_left)
        side_group.addButton(self.side_right)
        current_side = self._settings.value("ui/dock_side", "right")
        self.side_right.setChecked(current_side != "left")
        self.side_left.setChecked(current_side == "left")
        self.side_left.toggled.connect(self._on_side_changed)
        self.side_right.toggled.connect(self._on_side_changed)
        side_row = QHBoxLayout()
        side_row.addWidget(self.side_left)
        side_row.addWidget(self.side_right)
        side_row.addStretch()
        form.addRow(side_row)

        # Keyboard shortcuts
        form.addRow(sep())
        shortcut_lbl = QLabel("Keyboard Shortcuts")
        shortcut_lbl.setStyleSheet("font-weight: bold; font-size: 12pt;")
        form.addRow(shortcut_lbl)
        self._shortcut_edits = {}
        default_shortcuts = {
            "close": "Ctrl+W",
            "save": "Ctrl+S",
            "toggle_dock": "Tab",
            "fullscreen": "F",
            "page_up": "PageUp",
            "page_down": "PageDown",
        }
        for key, label_text in [
            ("close", "Close PDF"),
            ("save", "Save PDF"),
            ("toggle_dock", "Toggle Side Panel"),
            ("fullscreen", "Full Screen"),
            ("page_up", "Previous Page"),
            ("page_down", "Next Page"),
        ]:
            h = QHBoxLayout()
            edit = QKeySequenceEdit()
            edit.setFixedWidth(160)
            edit.setStyleSheet(
                "QKeySequenceEdit { background: transparent; border: 1px solid rgba(128,128,128,0.2); border-radius: 3px; padding: 2px 4px; }"
                "QKeySequenceEdit:focus { background: rgba(128,128,128,0.12); border: 1px solid rgba(128,128,128,0.4); }")
            current = self._settings.value(f"shortcuts/{key}", default_shortcuts[key])
            edit.setKeySequence(QKeySequence(current))
            h.addWidget(edit)
            reset_btn = QPushButton("Reset")
            reset_btn.setFixedWidth(100)
            default_val = default_shortcuts[key]
            reset_btn.clicked.connect(lambda *a, e=edit, d=default_val: e.setKeySequence(QKeySequence(d)))
            h.addWidget(reset_btn)
            form.addRow(label_text, h)
            self._shortcut_edits[key] = edit

        # Mouse behavior
        form.addRow(sep())
        mouse_lbl = QLabel("Mouse Behavior")
        mouse_lbl.setStyleSheet("font-weight: bold; font-size: 12pt;")
        form.addRow(mouse_lbl)
        dbl_lbl = QLabel("Double-click zoom")
        dbl_lbl.setStyleSheet("font-weight: bold;")
        form.addRow(dbl_lbl)
        dbl_row = QHBoxLayout()
        self._dbl_zoom_spin = QSpinBox()
        self._dbl_zoom_spin.setRange(25, 3200)
        self._dbl_zoom_spin.setSuffix("%")
        self._dbl_zoom_spin.setSingleStep(25)
        self._dbl_zoom_spin.setValue(
            int(self._settings.value("ui/dbl_click_zoom", 200)))
        self._dbl_zoom_spin.valueChanged.connect(self._on_dbl_zoom_changed)
        dbl_row.addWidget(self._dbl_zoom_spin)
        dbl_row.addStretch()
        form.addRow(dbl_row)
        self._mouse_dbl_desc = QLabel(
            "First double-click: fit to viewport\n"
            "Second double-click: zoom to this value")
        self._mouse_dbl_desc.setStyleSheet("font-size: 8pt; color: rgba(128,128,128,220); padding: 2px 0;")
        form.addRow("", self._mouse_dbl_desc)
        self._mouse_wheel = QLabel("Ctrl+Wheel: Navigate pages")
        self._mouse_wheel.setStyleSheet("padding: 2px 0;")
        form.addRow("", self._mouse_wheel)
        self._mouse_pan = QLabel("Drag: Pan view")
        self._mouse_pan.setStyleSheet("padding: 2px 0;")
        form.addRow("", self._mouse_pan)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { width: 10px; }")
        form_widget = QWidget()
        form_widget.setLayout(form)
        scroll.setWidget(form_widget)
        layout.addWidget(scroll)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        btn_font = QFont()
        btn_font.setPointSize(9)
        btn_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        btns.setFont(btn_font)
        layout.addWidget(btns)

    def _on_unit_changed(self, unit):
        self._settings.setValue("ui/box_unit", unit)
        self.box_unit_changed.emit(unit)

    def _on_density_changed(self, value):
        self._settings.setValue("ui/density_scale", value)
        self.ui_scale_label.setText(str(value))
        self._density_timer.start()

    def _apply_density(self):
        self._density_timer.stop()
        theme = self._settings.value("ui/theme", "dark_teal.xml")
        self.theme_changed.emit(theme)

    def _on_side_changed(self):
        side = "left" if self.side_left.isChecked() else "right"
        self._settings.setValue("ui/dock_side", side)
        self.dock_side_changed.emit(side)

    def _on_theme_mode_changed(self, mode):
        self._settings.setValue("ui/theme_mode", mode)
        accent = self._accent_color_val
        theme_name = f"{mode}_teal.xml"
        self._settings.setValue("ui/theme", theme_name)
        self.theme_changed.emit(theme_name)

    def _update_accent_btn_color(self):
        self.accent_btn.setStyleSheet(
            f"QPushButton {{ background-color: {self._accent_color_val}; "
            f"border: 2px solid rgba(128,128,128,0.4); border-radius: 4px; }}"
            f"QPushButton:hover {{ border: 2px solid {self._accent_color_val}; }}")

    def _pick_accent_color(self):
        from PyQt6.QtWidgets import QColorDialog
        from PyQt6.QtGui import QColor
        initial = QColor(self._accent_color_val)
        color = QColorDialog.getColor(initial, self, "Choose Accent Color")
        if color.isValid():
            self._accent_color_val = color.name()
            self.accent_label.setText(self._accent_color_val)
            self._update_accent_btn_color()
            self._settings.setValue("ui/accent_color", self._accent_color_val)
            mode = self._settings.value("ui/theme_mode", "dark")
            self.theme_changed.emit(f"{mode}_teal.xml")

    def _on_dbl_zoom_changed(self, value):
        self._settings.setValue("ui/dbl_click_zoom", value)
        self.dbl_click_zoom_changed.emit(value)

    def _save(self):
        mode = "hide" if self.shift_hide.isChecked() else "show"
        self._settings.setValue("ui/shift_mode", mode)
        side = "left" if self.side_left.isChecked() else "right"
        self._settings.setValue("ui/dock_side", side)
        self._settings.setValue("ui/magnifier_enabled",
                                str(self.magnifier_cb.isChecked()))
        for key, edit in self._shortcut_edits.items():
            self._settings.setValue(f"shortcuts/{key}", edit.keySequence().toString())
        self.accept()

    def exec(self):
        r = super().exec()
        if r:
            self._settings.sync()
            self.shortcuts_changed.emit()
        return r


class _RenderSignals(QObject):
    done = pyqtSignal(object, object, float, bool, object, object, float, int)
    # (rgb_arr, boxes, zoom, has_op, cmyk_buf, page_rect, duration_ms, seq)
    draft = pyqtSignal(object, object, float, object, int)
    # (rgb_arr, boxes, zoom, page_rect, seq)


class _PageProxy:
    def __init__(self, rect, boxes, rect2=None, boxes2=None, rect0=None, gap=0):
        self.rect = rect
        r0 = rect0 if rect0 is not None else rect
        self.pages = [{'rect': r0, 'boxes': boxes, 'x_offset': 0}]
        for name in ('art', 'bleed', 'trim', 'media', 'crop'):
            b = boxes.get(name)
            if b is not None:
                setattr(self, f'{name}box', b)
        if rect2 is not None and boxes2 is not None:
            x_off = r0.width + gap
            self.pages.append({'rect': rect2, 'boxes': boxes2, 'x_offset': x_off})
            for name in ('art', 'bleed', 'trim', 'media', 'crop'):
                b2 = boxes2.get(name)
                if b2 is not None:
                    setattr(self, f'{name}box', b2)


# Boxes for which the spread "crop preview" applies: when one of these masks
# is active, each page is rendered clipped to that box and the two boxes are
# butted edge-to-edge (no gutter). Media/Art boxes keep the normal spread.
CROP_PREVIEW_BOXES = ('trim', 'bleed', 'crop')


def _shift_boxes_to_clip(boxes, clip):
    """Shift box rects so they are relative to a clipped render's origin.

    When a page is rendered clipped to ``clip``, the resulting image's pixel
    (0,0) corresponds to ``clip``'s top-left, not the page origin. The overlay
    draws boxes in page coordinates, so without this shift it misaligns and
    paints a spurious masked strip at the gutter. Returns a new dict; the
    input is left untouched."""
    if clip is None:
        return boxes
    import fitz
    out = {}
    for k, b in boxes.items():
        if b is None:
            out[k] = b
        else:
            out[k] = fitz.Rect(b.x0 - clip.x0, b.y0 - clip.y0,
                               b.x1 - clip.x0, b.y1 - clip.x0)
    return out

# Module-level fitz document cache — avoids reopening the PDF on every render
_render_fitz_doc = None
_render_fitz_path = None
_render_fitz_lock = threading.Lock()
_bg_icc_transform_cache = {}
_bg_icc_transform_lock = threading.Lock()

def _get_render_doc(path):
    global _render_fitz_doc, _render_fitz_path
    with _render_fitz_lock:
        if _render_fitz_path == path and _render_fitz_doc is not None:
            return _render_fitz_doc
        if _render_fitz_doc is not None:
            try:
                _render_fitz_doc.close()
            except Exception:
                pass
        import fitz
        _render_fitz_doc = fitz.open(path)
        _render_fitz_path = path
        return _render_fitz_doc

def _close_render_doc_cache():
    global _render_fitz_doc, _render_fitz_path
    with _render_fitz_lock:
        if _render_fitz_doc is not None:
            try:
                _render_fitz_doc.close()
            except Exception:
                pass
        _render_fitz_doc = None
        _render_fitz_path = None

def _get_bg_icc_transform(icc_path):
    with _bg_icc_transform_lock:
        if icc_path in _bg_icc_transform_cache:
            return _bg_icc_transform_cache[icc_path]
        try:
            from PIL import ImageCms
            cmyk_prof = ImageCms.getOpenProfile(icc_path)
            rgb_prof = ImageCms.createProfile('sRGB')
            transform = ImageCms.buildTransform(
                cmyk_prof, rgb_prof, 'CMYK', 'RGB',
                renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
                flags=0x2000)
        except Exception:
            transform = None
        _bg_icc_transform_cache[icc_path] = transform
        return transform

def render_one_data(doc, page_num, zoom, mode, channels, icc_path,
                    sim_profile, simulate_overprint, clip=None,
                    object_filter=None):
    """Render a single page to an RGB numpy array following the active
    mode (normal / overprint / separation), channels, ICC and simulation
    profile. Returns (rgb_arr, fast_rgb_arr, cmyk_arr, boxes, page_rect, has_op).
    Used by both the full-page background render and the overview thumbnails.
    When ``clip`` (a fitz.Rect in page coordinates) is given, only that region
    is rendered — used for the high-resolution detail overlay tile."""
    import fitz
    import numpy as np
    from PIL import Image
    import os

    modified = []
    try:
        pg = doc[page_num]
        mat = fitz.Matrix(zoom, zoom)
        from viewer.render_engine import _RENDER_LOCK
        with _RENDER_LOCK:
            old_icc = fitz.TOOLS.set_icc(0)
            try:
                if not simulate_overprint:
                    from viewer.render_engine import _disable_overprint
                    modified = _disable_overprint(doc)
                if clip is not None:
                    pix_cmyk = pg.get_pixmap(matrix=mat, clip=clip,
                                             colorspace=fitz.csCMYK)
                else:
                    pix_cmyk = pg.get_pixmap(matrix=mat, colorspace=fitz.csCMYK)
            finally:
                fitz.TOOLS.set_icc(old_icc)
                if modified:
                    from viewer.render_engine import _restore_overprint
                    _restore_overprint(doc, modified)
        cmyk_arr = np.frombuffer(pix_cmyk.samples, dtype=np.uint8).reshape(
            pix_cmyk.height, pix_cmyk.width, 4)
        if object_filter is not None:
            from preview.object_filter import apply_object_filter
            cmyk_arr = apply_object_filter(
                cmyk_arr, pg, zoom, object_filter, clip=clip)
        has_op = False
        display_cmyk = cmyk_arr
        if mode == "overprint":
            from preview.overprint import (
                OverprintPreview, simulate_overprint_on_cmyk)
            op = OverprintPreview()
            has_op = op.detect_overprint(pg)
            if has_op and simulate_overprint:
                # MuPDF's get_pixmap ignores overprint, so we must simulate it
                # ourselves by reconstructing the page in content-stream order.
                display_cmyk = simulate_overprint_on_cmyk(cmyk_arr, pg, doc)
        elif mode == "separation":
            from preview.separation import SeparationPreview
            sp = SeparationPreview()
            display_cmyk = sp.composite(cmyk_arr, channels)
            if simulate_overprint:
                from preview.overprint import simulate_overprint_on_cmyk
                display_cmyk = simulate_overprint_on_cmyk(
                    display_cmyk, pg, doc, active_channels=channels)
        # Fast RGB draft: use MuPDF csRGB on raw pixmap, or PIL convert
        if display_cmyk is cmyk_arr and mode != "overprint":
            pix_rgb = fitz.Pixmap(fitz.csRGB, pix_cmyk)
            fast_rgb_arr = np.frombuffer(
                pix_rgb.samples, dtype=np.uint8
            ).reshape(pix_rgb.height, pix_rgb.width, 3).copy()
        else:
            fast_img = Image.fromarray(display_cmyk, mode='CMYK')
            fast_rgb_arr = np.ascontiguousarray(
                np.asarray(fast_img.convert('RGB')))
        use_simulation = sim_profile and os.path.isfile(sim_profile)
        effective_icc = (
            sim_profile if use_simulation
            else (icc_path if (icc_path and os.path.isfile(icc_path)) else None)
        )
        if effective_icc:
            img_pil = Image.fromarray(display_cmyk, mode='CMYK')
            try:
                from PIL import ImageCms
                transform = _get_bg_icc_transform(effective_icc)
                if transform is None:
                    raise RuntimeError("ICC transform unavailable")
                rgb_pil = ImageCms.applyTransform(img_pil, transform)
                rgb_arr = np.ascontiguousarray(np.asarray(rgb_pil))
            except Exception:
                if display_cmyk is cmyk_arr:
                    pix_rgb = fitz.Pixmap(fitz.csRGB, pix_cmyk)
                    rgb_arr = np.frombuffer(
                        pix_rgb.samples, dtype=np.uint8
                    ).reshape(pix_rgb.height, pix_rgb.width, 3).copy()
                else:
                    rgb_arr = np.ascontiguousarray(
                        np.asarray(Image.fromarray(
                            display_cmyk, mode='CMYK').convert('RGB')))
        else:
            if display_cmyk is cmyk_arr:
                pix_rgb = fitz.Pixmap(fitz.csRGB, pix_cmyk)
                rgb_arr = np.frombuffer(
                    pix_rgb.samples, dtype=np.uint8
                ).reshape(pix_rgb.height, pix_rgb.width, 3).copy()
            else:
                rgb_arr = np.ascontiguousarray(
                    np.asarray(Image.fromarray(
                        display_cmyk, mode='CMYK').convert('RGB')))
        boxes = {
            'art': pg.artbox, 'bleed': pg.bleedbox,
            'trim': pg.trimbox, 'media': pg.mediabox, 'crop': pg.cropbox,
        }
        return rgb_arr, fast_rgb_arr, cmyk_arr.copy(), boxes, pg.rect, has_op
    finally:
        pass


class FitzThumbWorker(QObject):
    """Renders ALL fitz content on a single dedicated background thread.

    MuPDF's global ICC toggle (fitz.TOOLS.set_icc) and get_pixmap are unsafe
    across multiple threads AND unsafe to call from inside a Qt event-loop
    callback (deep call stack -> STATUS_STACK_BUFFER_OVERRUN). Rendering here,
    on a dedicated background thread with a shallow stack, avoids both problems.
    There is a SINGLE worker that handles thumbnails, TAC, page renders,
    magnifier cache builds, AND page prefetches — every fitz operation goes
    through this one thread. Results are delivered to the GUI thread via
    queued signals.
    """

    thumb_ready = pyqtSignal(int, QImage)
    tac_ready = pyqtSignal(int, object)
    page_draft_ready = pyqtSignal(object, object, float, object, int)
    page_done_ready = pyqtSignal(object, object, float, bool, object, object, float, int, object)
    mag_cache_ready = pyqtSignal(object, object)
    prefetch_done = pyqtSignal(object, object)
    detail_ready = pyqtSignal(object, object, float, int)

    def __init__(self):
        super().__init__()
        self._queue = queue.Queue()
        self._cancel = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._doc = None
        self._doc_path = None

    def _get_doc(self, path):
        import fitz
        if self._doc_path != path or self._doc is None:
            try:
                if self._doc is not None:
                    self._doc.close()
            except Exception:
                pass
            self._doc = fitz.open(path)
            self._doc_path = path
        return self._doc

    @staticmethod
    def _crop_box_rect(doc, page_num, name):
        """Return a valid fitz.Rect for the named box of a page, or None."""
        import fitz
        try:
            pg = doc[page_num]
            b = getattr(pg, f'{name}box', None)
            if b is not None and b.x0 < b.x1 and b.y0 < b.y1:
                return fitz.Rect(b)
        except Exception:
            pass
        return None

    def _run(self):
        import fitz
        from viewer.render_engine import get_cmyk_icc_path
        from preflight.analyzer import PreflightAnalyzer
        while True:
            item = self._queue.get()
            if item is None:
                self._close_doc()
                break
            self._cancel.clear()
            kind = item[0]
            if kind == 'thumb':
                _, path, page_index, ov_scale, mode, channels, sim_profile, op = item
                doc = None
                try:
                    doc = fitz.open(path)
                    icc_path = get_cmyk_icc_path(doc)
                    rgb_arr, _, _, _, _, _ = render_one_data(
                        doc, page_index, ov_scale, mode, channels,
                        icc_path, sim_profile, op)
                except Exception:
                    rgb_arr = None
                finally:
                    if doc is not None:
                        try:
                            doc.close()
                        except Exception:
                            pass
                if rgb_arr is None or rgb_arr.size == 0 or self._cancel.is_set():
                    continue
                h, w = rgb_arr.shape[:2]
                if h == 0 or w == 0:
                    continue
                qimg = QImage(rgb_arr.data, w, h, w * 3,
                              QImage.Format.Format_RGB888).copy()
                self.thumb_ready.emit(page_index, qimg)
            elif kind == 'tac':
                _, path, page_index, zoom = item
                doc = None
                try:
                    doc = fitz.open(path)
                    icc_path = get_cmyk_icc_path(doc)
                    cmyk_arr, _, _, _, _, _ = render_one_data(
                        doc, page_index, zoom, 'normal',
                        {'cyan': True, 'magenta': True,
                         'yellow': True, 'black': True},
                        icc_path, None, False)
                    tac = PreflightAnalyzer.tac_from_cmyk(cmyk_arr)
                except Exception:
                    tac = None
                finally:
                    if doc is not None:
                        try:
                            doc.close()
                        except Exception:
                            pass
                if tac is not None and not self._cancel.is_set():
                    self.tac_ready.emit(page_index, tac)
            elif kind == 'page':
                self._process_page(item)
            elif kind == 'mag':
                self._process_mag(item)
            elif kind == 'prefetch':
                self._process_prefetch(item)
            elif kind == 'detail':
                self._process_detail(item)

    def _process_page(self, item):
        import fitz
        import numpy as np
        import os
        import time

        (_, path, page_num, zoom, mode, channels, icc_path, sim_profile,
          spread, is_offset_single, simulate_overprint, box_mask,
          cancel_event, seq, draft_seq, cache_key, object_filter) = item

        doc = self._get_doc(path)

        crop_box = box_mask if box_mask in CROP_PREVIEW_BOXES else None

        def _r1(pn, clip=None):
            return render_one_data(doc, pn, zoom, mode, channels, icc_path,
                                    sim_profile, simulate_overprint, clip=clip,
                                    object_filter=object_filter)
        try:
            clip1 = (self._crop_box_rect(doc, page_num, crop_box)
                     if crop_box else None)
            rgb_arr, fast_rgb_arr, cmyk_arr, boxes, page_rect, has_op = _r1(page_num, clip=clip1)
            if clip1 is not None:
                page_rect = clip1
                boxes = _shift_boxes_to_clip(boxes, clip1)
            if cancel_event.is_set() or self._cancel.is_set():
                return

            self.page_draft_ready.emit(fast_rgb_arr, boxes, zoom, page_rect, draft_seq)

            if spread:
                GAP_PX = 0 if crop_box else 20
                BG_RGB = np.array([64, 64, 64], dtype=np.uint8)
                clip2 = (self._crop_box_rect(doc, page_num + 1, crop_box)
                         if crop_box else None)
                rgb_arr2, fast_rgb_arr2, cmyk_arr2, boxes2, rect2, has_op2 = _r1(page_num + 1, clip=clip2)
                if clip2 is not None:
                    rect2 = clip2
                    boxes2 = _shift_boxes_to_clip(boxes2, clip2)
                if cancel_event.is_set() or self._cancel.is_set():
                    return
                h1, w1 = rgb_arr.shape[:2]
                h2, w2 = rgb_arr2.shape[:2]
                max_h = max(h1, h2)
                if h1 < max_h:
                    pad = np.full((max_h - h1, w1, 3), BG_RGB, dtype=np.uint8)
                    rgb_arr = np.vstack((rgb_arr, pad))
                    cmyk_pad = np.zeros((max_h - h1, w1, 4), dtype=np.uint8)
                    cmyk_arr = np.vstack((cmyk_arr, cmyk_pad))
                if h2 < max_h:
                    pad = np.full((max_h - h2, w2, 3), BG_RGB, dtype=np.uint8)
                    rgb_arr2 = np.vstack((rgb_arr2, pad))
                    cmyk_pad = np.zeros((max_h - h2, w2, 4), dtype=np.uint8)
                    cmyk_arr2 = np.vstack((cmyk_arr2, cmyk_pad))
                gap_rgb = np.full((max_h, GAP_PX, 3), BG_RGB, dtype=np.uint8)
                gap_cmyk = np.zeros((max_h, GAP_PX, 4), dtype=np.uint8)
                rgb_arr = np.hstack((rgb_arr, gap_rgb, rgb_arr2))
                cmyk_arr = np.hstack((cmyk_arr, gap_cmyk, cmyk_arr2))
                has_op = has_op or has_op2
                gap_pt = GAP_PX / zoom
                boxes['_page2'] = {'rect': rect2, 'boxes': boxes2, 'rect0': page_rect,
                                   'gap': gap_pt}
                page_rect = fitz.Rect(0, 0, page_rect.width + gap_pt + rect2.width,
                                       max(page_rect.height, rect2.height))

            if is_offset_single:
                h, w = rgb_arr.shape[:2]
                BG_RGB = np.array([64, 64, 64], dtype=np.uint8)
                blank = np.full((h, w, 3), BG_RGB, dtype=np.uint8)
                rgb_arr = np.hstack((blank, rgb_arr))
                cmyk_blank = np.zeros((h, w, 4), dtype=np.uint8)
                cmyk_arr = np.hstack((cmyk_blank, cmyk_arr))
                blank_r = fitz.Rect(0, 0, 0, 0)
                blank_boxes = {k: blank_r for k in boxes}
                boxes['_page2'] = {'rect': page_rect, 'boxes': boxes.copy(),
                                   'rect0': page_rect}
                boxes.update(blank_boxes)
                page_rect = fitz.Rect(0, 0, page_rect.width * 2, page_rect.height)

            if cancel_event.is_set() or self._cancel.is_set():
                return
            self.page_done_ready.emit(rgb_arr, boxes, zoom, has_op, cmyk_arr,
                                      page_rect, time.monotonic(), seq, cache_key)
        except Exception:
            import traceback
            traceback.print_exc()

    def _process_mag(self, item):
        import fitz
        import numpy as np
        from viewer.render_engine import _no_icc

        _, path, page_num, cancel_event = item
        if cancel_event.is_set() or self._cancel.is_set():
            return
        doc = self._get_doc(path)
        try:
            page = doc[page_num]
            mat = fitz.Matrix(4.0, 4.0)
            with _no_icc():
                pix = page.get_pixmap(matrix=mat, colorspace=fitz.csCMYK)
            if pix.width == 0 or pix.height == 0:
                return
            cmyk_arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, 4).copy()
            cache_key = (page_num, id(path))
            self.mag_cache_ready.emit(cache_key, cmyk_arr)
        except Exception:
            pass

    def _process_prefetch(self, item):
        import fitz
        import numpy as np

        (_, path, page_num, zoom, mode, channels, icc_path, sim_profile,
          spread, is_offset_single, simulate_overprint, box_mask,
          cache_key, object_filter) = item

        doc = self._get_doc(path)

        crop_box = box_mask if box_mask in CROP_PREVIEW_BOXES else None

        def _r1(pn, clip=None):
            return render_one_data(doc, pn, zoom, mode, channels, icc_path,
                                    sim_profile, simulate_overprint, clip=clip,
                                    object_filter=object_filter)
        try:
            clip1 = (self._crop_box_rect(doc, page_num, crop_box)
                     if crop_box else None)
            rgb_arr, fast_rgb_arr, cmyk_arr, boxes, page_rect, has_op = _r1(page_num, clip=clip1)
            if clip1 is not None:
                page_rect = clip1
                boxes = _shift_boxes_to_clip(boxes, clip1)
            if self._cancel.is_set():
                return

            if spread:
                GAP_PX = 0 if crop_box else 20
                BG_RGB = np.array([64, 64, 64], dtype=np.uint8)
                clip2 = (self._crop_box_rect(doc, page_num + 1, crop_box)
                         if crop_box else None)
                _, _, cmyk_arr2, boxes2, rect2, _ = _r1(page_num + 1, clip=clip2)
                if clip2 is not None:
                    rect2 = clip2
                    boxes2 = _shift_boxes_to_clip(boxes2, clip2)
                if self._cancel.is_set():
                    return
                h1, w1 = cmyk_arr.shape[:2]
                h2, w2 = cmyk_arr2.shape[:2]
                max_h = max(h1, h2)
                if h1 < max_h:
                    pad = np.zeros((max_h - h1, w1, 4), dtype=np.uint8)
                    cmyk_arr = np.vstack((cmyk_arr, pad))
                if h2 < max_h:
                    pad = np.zeros((max_h - h2, w2, 4), dtype=np.uint8)
                    cmyk_arr2 = np.vstack((cmyk_arr2, pad))
                gap = np.zeros((max_h, GAP_PX, 4), dtype=np.uint8)
                cmyk_arr = np.hstack((cmyk_arr, gap, cmyk_arr2))
                gap_pt = GAP_PX / zoom
                boxes['_page2'] = {'rect': rect2, 'boxes': boxes2, 'rect0': page_rect,
                                   'gap': gap_pt}
                page_rect = fitz.Rect(0, 0, page_rect.width + gap_pt + rect2.width,
                                       max(page_rect.height, rect2.height))

            if is_offset_single:
                h, w = cmyk_arr.shape[:2]
                cmyk_blank = np.zeros((h, w, 4), dtype=np.uint8)
                cmyk_arr = np.hstack((cmyk_blank, cmyk_arr))
                blank_r = fitz.Rect(0, 0, 0, 0)
                blank_boxes = {k: blank_r for k in boxes}
                boxes['_page2'] = {'rect': page_rect, 'boxes': boxes.copy(),
                                   'rect0': page_rect}
                boxes.update(blank_boxes)
                page_rect = fitz.Rect(0, 0, page_rect.width * 2, page_rect.height)

            pg2 = boxes.pop('_page2', None)
            pg2_info = None
            if pg2 is not None:
                pg2_info = {
                    'rect': pg2['rect'],
                    'boxes': pg2['boxes'],
                    'rect0': pg2.pop('rect0', None),
                    'gap': pg2.pop('gap', 0),
                }
            entry = {
                'rgb_arr': rgb_arr,
                'cmyk_buf': cmyk_arr,
                'boxes': boxes,
                'zoom': zoom,
                'has_op': has_op,
                'page_rect': page_rect,
            }
            if pg2_info is not None:
                entry['pg2_info'] = pg2_info
            self.prefetch_done.emit(cache_key, entry)
        except Exception:
            pass

    def _process_detail(self, item):
        import fitz

        (_, path, page_num, zoom, clip, mode, channels, icc_path,
         sim_profile, simulate_overprint, cancel_event, seq,
         object_filter) = item

        if cancel_event.is_set() or self._cancel.is_set():
            return
        doc = self._get_doc(path)
        try:
            clip_rect = fitz.Rect(*clip)
            rgb_arr, _, _, _, _, _ = render_one_data(
                doc, page_num, zoom, mode, channels, icc_path,
                sim_profile, simulate_overprint, clip=clip_rect,
                object_filter=object_filter)
            if cancel_event.is_set() or self._cancel.is_set():
                return
            if rgb_arr is None or rgb_arr.size == 0:
                return
            self.detail_ready.emit(rgb_arr, clip, zoom, seq)
        except Exception:
            pass

    def submit_detail(self, path, page_num, zoom, clip, mode, channels,
                      icc_path, sim_profile, simulate_overprint,
                      cancel_event, seq, object_filter=None):
        self._queue.put(('detail', path, page_num, zoom, clip, mode, channels,
                         icc_path, sim_profile, simulate_overprint,
                         cancel_event, seq, object_filter))

    def submit_thumb(self, path, page_index, ov_scale, mode, channels,
                     sim_profile, op):
        self._queue.put(('thumb', path, page_index, ov_scale, mode, channels,
                         sim_profile, op))

    def submit_tac(self, path, page_index, zoom):
        self._queue.put(('tac', path, page_index, zoom))

    def submit_page(self, path, page_num, zoom, mode, channels,
                     icc_path, sim_profile, spread, is_offset_single,
                     simulate_overprint, box_mask, cancel_event, seq,
                     draft_seq, cache_key, object_filter=None):
        self._queue.put(('page', path, page_num, zoom, mode, channels,
                          icc_path, sim_profile, spread, is_offset_single,
                          simulate_overprint, box_mask, cancel_event, seq,
                          draft_seq, cache_key, object_filter))

    def submit_mag(self, path, page_num, cancel_event):
        self._queue.put(('mag', path, page_num, cancel_event))

    def submit_prefetch(self, path, page_num, zoom, mode, channels,
                         icc_path, sim_profile, spread, is_offset_single,
                         simulate_overprint, box_mask, cache_key,
                         object_filter=None):
        self._queue.put(('prefetch', path, page_num, zoom, mode, channels,
                          icc_path, sim_profile, spread, is_offset_single,
                          simulate_overprint, box_mask, cache_key,
                          object_filter))

    def clear_pending(self):
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except Exception:
                break

    def stop(self):
        self._cancel.set()
        self.clear_pending()
        self._queue.put(None)

    def _close_doc(self):
        if self._doc is not None:
            try:
                self._doc.close()
            except Exception:
                pass
            self._doc = None
            self._doc_path = None


class PreflightWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self._settings = QSettings("PDFPreflight", "Viewer")
        self.setWindowTitle("PDF Preflight Viewer")
        self._restore_geometry()

        self.render = RenderEngine()
        self.analyzer = PreflightAnalyzer()
        self.color_picker = ColorPicker()
        self.overprint = OverprintPreview()
        self.separation = SeparationPreview()
        self.simulation = SimulationEngine()
        self.rules = RuleEngine()

        self._current_page = 0
        self._mode = "normal"
        self._zoom = 2.0
        self._cmyk_buf = None
        self._last_pixel = None
        self._overprint_on_page = False
        self._recent_files = self._load_recent()
        self._current_path = None
        self._MAX_RENDER_ZOOM = 5.0
        self._DETAIL_MAX_ZOOM = 20.0
        self._detail_seq = 0
        self._detail_cancel = threading.Event()
        self._detail_timer = None
        self._DETAIL_RENDER_DELAY_MS = 140
        self._accent_color = '#1de9b6'
        self._muted_color = 'rgba(224, 224, 224, 0.35)'
        self._last_render_zoom = 0.0  # 0 = no pixmap yet
        self._center_on_page = False
        self._is_fit_viewport = False
        self._color_overlay_enabled = False
        self._active_box_mask = None
        self._box_eye_btns = {}
        self._render_seq = 0
        self._page_cache = OrderedDict()
        self._page_cache_lock = threading.Lock()
        self._page_prefetch_threads = {}
        self._PAGE_CACHE_LIMIT = 6
        self._PAGE_PREFETCH_LIMIT = 1
        self._ZOOM_RENDER_DELAY_MS = 240
        self._INFO_UPDATE_DELAY_MS = 120
        self._info_update_timer = None
        self._magnifier_cache_timer = None
        self._mag_cancel = threading.Event()

        self._render_cancel = threading.Event()
        self._render_signals = _RenderSignals()
        self._draft_seq = 0
        self._of_batch_update = False

        # ---- Overview (zoom-driven, continuous) state ----
        self._overview_active = False
        self._overview_current = 0
        self._overview_ov_scale = 1.0
        self._overview_entries = []
        self._thumb_gen = 0
        self._overview_doc = None
        self._overview_icc_path = None
        self._overview_sim_profile = None
        self._thumb_pump = QTimer(self)
        self._thumb_pump.setInterval(40)
        self._thumb_pump.timeout.connect(self._pump_thumb_results)
        self._thumb_pump.start()

        self._fullscreen_prefs = None  # saved prefs for fullscreen restore
        self._build_ui()
        self._thumb_worker = FitzThumbWorker()
        self._thumb_worker.thumb_ready.connect(self.page_widget.set_overview_thumb)
        self._thumb_worker.tac_ready.connect(self._on_tac_ready)
        self._thumb_worker.page_done_ready.connect(self._on_page_done_ready)
        self._thumb_worker.page_draft_ready.connect(self._on_page_draft_done)
        self._thumb_worker.mag_cache_ready.connect(self._on_mag_cache_ready)
        self._thumb_worker.prefetch_done.connect(self._on_prefetch_done)
        self._thumb_worker.detail_ready.connect(self._on_detail_ready)
        self._setup_shortcuts()
        self._connect_signals()
        mode = str(self._settings.value("ui/theme_mode", "dark"))
        theme = f"{mode}_teal.xml"
        self._apply_theme(theme)
        self._restore_collapse_states()
        self._maybe_reopen_last()

    def _load_recent(self):
        raw = self._settings.value("recent/files", [])
        if isinstance(raw, str):
            raw = [raw]
        return list(raw)[:20] if raw else []

    def _maybe_reopen_last(self):
        if str(self._settings.value("ui/reopen_last", "false")).lower() != "true":
            return
        for path in self._recent_files:
            if os.path.isfile(path):
                self.open_file(path)
                return

    def _save_recent(self):
        self._settings.setValue("recent/files", self._recent_files[:20])
        self._settings.sync()

    def _add_recent(self, path):
        path = os.path.abspath(path)
        if path in self._recent_files:
            self._recent_files.remove(path)
        self._recent_files.insert(0, path)
        self._recent_files = self._recent_files[:20]
        self._save_recent()
        self._rebuild_recent_menu()

    def _clear_recent(self):
        self._recent_files = []
        self._save_recent()
        self._rebuild_recent_menu()
        self._recent_menu.close()

    def _save_pdf(self):
        if not self.render.doc:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save PDF", "", "PDF files (*.pdf)")
        if path:
            try:
                self.render.doc.save(path, garbage=4, deflate=True)
                QMessageBox.information(self, "Saved", f"PDF saved to:\n{path}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Save failed:\n{e}")

    def _close_file(self):
        self._render_cancel.set()
        self._render_seq += 1
        self._current_path = None
        self._overview_active = False
        self._overview_entries = []
        self._thumb_gen += 1
        self._thumb_worker.clear_pending()
        if hasattr(self, '_render_debounce'):
            self._render_debounce.stop()
        if self._info_update_timer is not None:
            self._info_update_timer.stop()
        if self._magnifier_cache_timer is not None:
            self._magnifier_cache_timer.stop()
        self._cmyk_buf = None
        self._last_render_zoom = 0
        self._cache_clear()
        self.page_widget.exit_overview()
        self.page_widget.scene.clear()
        self.page_widget.pixmap_item = None
        self.page_widget._detail_item = None
        self.setWindowTitle("PDF Preflight Viewer")
        self.simulation.clear_simulation_profile()
        if hasattr(self, '_populate_sim_profiles'):
            self._populate_sim_profiles()
        self._update_info()
        self.act_save.setVisible(False)
        self.act_close.setVisible(False)
        from preview.pdf_inspector import clear_cache
        clear_cache()
        from preview.overprint import clear_overprint_cache
        clear_overprint_cache()
        from preview.object_filter import clear_cache as of_clear_cache
        of_clear_cache()
        self.analyzer.close()
        self.render.close()
        _close_render_doc_cache()

    def _rebuild_recent_menu(self):
        self._recent_menu.clear()
        self._recent_menu.setMinimumWidth(280)
        self._recent_menu.setStyleSheet(
            "QMenu { padding: 2px 4px; font-size: 10pt; }"
            "QMenu::item { padding: 2px 8px; }")
        for p in self._recent_files:
            label = os.path.basename(p)
            act = QAction(label, self)
            act.setToolTip(p)
            act.triggered.connect(
                lambda checked, path=p: self.open_file(path))
            self._recent_menu.addAction(act)
        if self._recent_files:
            self._recent_menu.addSeparator()
        from PyQt6.QtWidgets import QWidgetAction, QPushButton
        btn_act = QWidgetAction(self)
        btn = QPushButton("Clear recents")
        btn.setStyleSheet(
            "QPushButton { border: none; "
            "border-radius: 4px; padding: 4px 12px; margin: 2px 0; "
            "font-size: 9pt; }")
        btn.clicked.connect(self._clear_recent)
        btn_act.setDefaultWidget(btn)
        self._recent_menu.addAction(btn_act)

    def _restore_geometry(self):
        geo = self._settings.value("window/geometry")
        state = self._settings.value("window/state")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.setWindowState(Qt.WindowState.WindowMaximized)
        if state is not None:
            self.restoreState(state)

    def _save_collapse_states(self):
        for key, blk in self._collapse_blocks.items():
            blk.save_state()

    def _restore_collapse_states(self):
        for key, blk in self._collapse_blocks.items():
            blk._restore_state()

    def _apply_theme(self, theme_name):
        from qt_material import apply_stylesheet
        app = QApplication.instance()
        if app:
            density = int(self._settings.value("ui/density_scale", 0))
            accent = str(self._settings.value("ui/accent_color", "#1de9b6"))
            extra = {'density_scale': str(density), 'primaryColor': accent}
            apply_stylesheet(app, theme=theme_name, extra=extra)
        self._accent_color = str(self._settings.value("ui/accent_color", "#1de9b6"))
        is_light = 'light' in theme_name
        if is_light:
            self._muted_color = 'rgba(33, 33, 33, 0.35)'
        else:
            self._muted_color = 'rgba(224, 224, 224, 0.35)'
        self._reload_icons()
        if hasattr(self, 'page_widget') and self.page_widget:
            self.page_widget._apply_theme_colors(is_light)
        self._update_sidebar_colors(is_light)
        self._update_accent_styles()
        self._update_of_styles()

    def _reload_icons(self):
        for act in [self.act_save, self.act_close, self.act_first,
                     self.act_prev, self.act_next, self.act_last,
                     self.act_zoom_fit, self.act_single,
                     self.act_spread, self.act_spread_offset]:
            act.setIcon(self._icon(act._icon_name))
        self._open_btn.setIcon(self._icon('open'))
        self._recent_btn.setIcon(self._icon('recent'))
        self._settings_btn.setIcon(self._icon('settings'))
        for name, btn in self._box_eye_btns.items():
            btn.setIcon(self._icon('crop'))

    def _update_accent_styles(self):
        accent = self._accent_color
        muted = self._muted_color
        self._toolbar.setStyleSheet(f"""
            QToolBar {{ padding: 4px 2px; spacing: 2px; }}
            QToolBar QToolButton {{ border: none; padding: 6px; border-radius: 4px; color: {muted}; }}
            QToolBar QToolButton:hover {{ border: none; background: rgba(128,128,128,0.2); }}
            QToolBar QToolButton:checked {{ border: none; background: {accent}; color: #fff; }}
            QToolBar QToolButton:disabled {{ color: {muted}; opacity: 0.35; }}
        """)
        for btn in self._box_eye_btns.values():
            btn.setStyleSheet(
                f"QPushButton {{ border: none; border-radius: 3px; padding: 0px; }}"
                f"QPushButton:hover {{ background: rgba(128,128,128,0.2); }}"
                f"QPushButton:checked {{ border: none; background: transparent; }}")
            btn.setIcon(self._icon('crop', color=accent if btn.isChecked() else None))
            if not hasattr(btn, '_hover_connected'):
                btn._hover_connected = True
                btn.enterEvent = lambda e, b=btn: b.setIcon(self._icon('crop', color=accent))
                btn.leaveEvent = lambda e, b=btn: b.setIcon(
                    self._icon('crop', color=accent if b.isChecked() else None))
        for btn in self._box_btns.values():
            btn.setStyleSheet(
                f"QPushButton {{ border: 1px solid {muted}; "
                f"border-radius: 3px; padding: 2px 4px; color: {muted}; font-weight: normal; font-size: 9pt; }}"
                f"QPushButton:hover {{ border: 1px solid {accent}; color: {accent}; }}"
                f"QPushButton:checked {{ background: transparent; border: 1px solid {accent}; "
                f"color: {accent}; }}")
        for btn in [self.chk_c, self.chk_m, self.chk_y, self.chk_k]:
            color, _ = self._sep_btn_style.get(btn._sep_key, ("#888", "#888"))
            new_styles = (
                f"QPushButton {{ border: 1px solid {muted}; border-left: 3px solid {color}; "
                f"text-align: center; padding: 4px 8px; color: {muted}; font-weight: normal; font-size: 9pt; }}"
                f"QPushButton:hover {{ border-left: 4px solid {color}; background: rgba(128,128,128,0.15); }}"
                f"QPushButton:checked {{ border-left: 4px solid {color}; }}")
            btn.setStyleSheet(new_styles)
        if hasattr(self, '_btn_curves'):
            self._btn_curves.setStyleSheet(
                f"QPushButton {{ border: 1px solid {accent}; color: {muted}; "
                f"border-radius: 4px; padding: 6px 16px; font-weight: normal; font-size: 9pt; }}"
                f"QPushButton:hover {{ border: 1px solid {accent}; color: {accent}; }}"
                f"QPushButton:pressed {{ border: 1px solid {accent}; color: {accent}; }}")

    def _update_sidebar_colors(self, is_light):
        dock_bg = '#d0d0d0' if is_light else '#404040'
        block_bg = '#dddddd' if is_light else '#4e4e4e'
        self._dock_content.setStyleSheet(f"#dock_content {{ background-color: {dock_bg}; }}")
        for blk in self._collapse_blocks.values():
            blk.setStyleSheet(f"background-color: {block_bg};")
            blk._content_area.setStyleSheet(f"background-color: {block_bg};")

    def _actual_zoom(self):
        return min(self._zoom, self._MAX_RENDER_ZOOM)

    def _render_zoom(self):
        return min(self._zoom, self._MAX_RENDER_ZOOM)

    def _spread_mode(self):
        if self.act_spread_offset.isChecked():
            return 'offset'
        if self.act_spread.isChecked():
            return 'normal'
        return None

    def _render_plan(self, page_num):
        spread_mode = self._spread_mode()
        if spread_mode == 'offset':
            if page_num == 0:
                return 0, False, True
            render_page = min(page_num + 1, self.render.page_count - 1)
            return render_page, render_page + 1 < self.render.page_count, False
        if spread_mode == 'normal':
            return page_num, page_num + 1 < self.render.page_count, False
        return page_num, False, False

    def _render_cache_key(self, page_num=None):
        if page_num is None:
            page_num = self._current_page
        render_page, spread, is_offset_single = self._render_plan(page_num)
        channels = (
            self.chk_c.isChecked(),
            self.chk_m.isChecked(),
            self.chk_y.isChecked(),
            self.chk_k.isChecked(),
        )
        sim_profile = self.simulation.get_active_profile_path()
        icc_path = get_cmyk_icc_path(self.render.doc)
        return (
            self._current_path,
            page_num,
            render_page,
            round(self._render_zoom(), 3),
            self._mode,
            channels,
            os.path.abspath(icc_path) if icc_path else None,
            os.path.abspath(sim_profile) if sim_profile else None,
            spread,
            is_offset_single,
            self.chk_simulate_overprint.isChecked(),
            self._active_box_mask,
            self._object_filter_key(),
        )

    def _cache_get(self, key):
        with self._page_cache_lock:
            entry = self._page_cache.get(key)
            if entry is not None:
                self._page_cache.move_to_end(key)
            return entry

    def _cache_put(self, key, entry):
        with self._page_cache_lock:
            self._page_cache[key] = entry
            self._page_cache.move_to_end(key)
            while len(self._page_cache) > self._PAGE_CACHE_LIMIT:
                self._page_cache.popitem(last=False)

    def _start_bg_render(self, debounce_ms=80):
        if not self._current_path or not self.render.doc:
            return
        if self._overview_active:
            self._request_visible_thumbs()
            return

        cached = self._cache_get(self._render_cache_key())
        if cached is not None:
            if hasattr(self, '_render_debounce') and self._render_debounce.isActive():
                self._render_debounce.stop()
            self._display_render_result(
                cached['rgb_arr'], cached['cmyk_buf'], cached['boxes'],
                cached['zoom'], cached['has_op'], cached['page_rect'],
                cached.get('pg2_info'))
            self._schedule_page_prefetch()
            return

        if hasattr(self, '_render_debounce') and self._render_debounce.isActive():
            self._render_debounce.start(debounce_ms)
            return
        from PyQt6.QtCore import QTimer
        self._render_debounce = QTimer(self)
        self._render_debounce.setSingleShot(True)
        self._render_debounce.timeout.connect(self._do_bg_render)
        self._render_debounce.start(debounce_ms)

    def _do_bg_render(self):
        if not self._current_path or not self.render.doc:
            return
        self._render_cancel.set()
        self._render_seq += 1
        page_num = self._current_page
        zoom = self._render_zoom()
        mode = self._mode
        path = self._current_path
        channels = {
            'cyan': self.chk_c.isChecked(),
            'magenta': self.chk_m.isChecked(),
            'yellow': self.chk_y.isChecked(),
            'black': self.chk_k.isChecked(),
        }
        icc_path = get_cmyk_icc_path(self.render.doc)

        sim_profile = self.simulation.get_active_profile_path()

        render_page, spread, is_offset_single = self._render_plan(page_num)
        cache_key = self._render_cache_key(page_num)

        if (self._last_render_zoom > 0
                and abs(self._zoom - self._last_render_zoom) > 0.01
                and self.page_widget.pixmap_item is not None):
            self._apply_live_zoom_transform()

        seq = self._render_seq
        draft_seq = self._draft_seq
        cancel_event = threading.Event()
        self._render_cancel = cancel_event
        simulate_op = self.chk_simulate_overprint.isChecked()
        object_filter = self._object_filter_state()
        self._thumb_worker.submit_page(
            path, render_page, zoom, mode, channels,
            icc_path, sim_profile, spread, is_offset_single,
            simulate_op, self._active_box_mask, cancel_event, seq,
            draft_seq, cache_key, object_filter)

    @staticmethod
    def _render_bg(path, page_num, zoom, mode, channels,
                   icc_path, sim_profile, spread, is_offset_single,
                   simulate_overprint, box_mask=None,
                   cancel_event=None, signals=None, seq=0, use_cached_doc=True,
                   draft_seq=0, object_filter=None):
        import fitz
        import numpy as np
        from PIL import Image
        import os

        def _render_one(pn, doc, clip=None):
            return render_one_data(doc, pn, zoom, mode, channels, icc_path,
                                    sim_profile, simulate_overprint, clip=clip,
                                    object_filter=object_filter)

        doc = None
        try:
            doc = _get_render_doc(path) if use_cached_doc else fitz.open(path)
            crop_box = box_mask if box_mask in CROP_PREVIEW_BOXES else None
            clip1 = (FitzThumbWorker._crop_box_rect(doc, page_num, crop_box)
                     if crop_box else None)
            rgb_arr, fast_rgb_arr, cmyk_arr, boxes, page_rect, has_op = _render_one(page_num, doc, clip=clip1)
            if clip1 is not None:
                page_rect = clip1
                boxes = _shift_boxes_to_clip(boxes, clip1)
            if cancel_event.is_set():
                return

            # Emit draft immediately for fast visual feedback
            signals.draft.emit(fast_rgb_arr, boxes, zoom, page_rect, draft_seq)

            if spread:
                GAP_PX = 0 if crop_box else 20
                BG_RGB = np.array([64, 64, 64], dtype=np.uint8)
                clip2 = (FitzThumbWorker._crop_box_rect(doc, page_num + 1, crop_box)
                         if crop_box else None)
                rgb_arr2, fast_rgb_arr2, cmyk_arr2, boxes2, rect2, has_op2 = _render_one(page_num + 1, doc, clip=clip2)
                if clip2 is not None:
                    rect2 = clip2
                    boxes2 = _shift_boxes_to_clip(boxes2, clip2)
                if cancel_event.is_set():
                    return
                # Stitch fast RGB for draft signal
                h1, w1 = fast_rgb_arr.shape[:2]
                h2, w2 = fast_rgb_arr2.shape[:2]
                max_hf = max(h1, h2)
                if h1 < max_hf:
                    pad = np.full((max_hf - h1, w1, 3), BG_RGB, dtype=np.uint8)
                    fast_rgb_arr = np.vstack((fast_rgb_arr, pad))
                if h2 < max_hf:
                    pad = np.full((max_hf - h2, w2, 3), BG_RGB, dtype=np.uint8)
                    fast_rgb_arr2 = np.vstack((fast_rgb_arr2, pad))
                gap_fast = np.full((max_hf, GAP_PX, 3), BG_RGB, dtype=np.uint8)
                fast_rgb_stitched = np.hstack((fast_rgb_arr, gap_fast, fast_rgb_arr2))
                # Emit draft for spread
                spread_boxes_draft = boxes.copy()
                spread_boxes_draft['_page2'] = {'rect': rect2, 'boxes': boxes2,
                                                 'rect0': page_rect, 'gap': GAP_PX / zoom}
                spread_pg_rect = fitz.Rect(0, 0, page_rect.width + (GAP_PX / zoom) + rect2.width,
                                            max(page_rect.height, rect2.height))
                signals.draft.emit(fast_rgb_stitched, spread_boxes_draft, zoom,
                                    spread_pg_rect, draft_seq)

                h1, w1 = rgb_arr.shape[:2]
                h2, w2 = rgb_arr2.shape[:2]
                max_h = max(h1, h2)
                if h1 < max_h:
                    pad = np.full((max_h - h1, w1, 3), BG_RGB, dtype=np.uint8)
                    rgb_arr = np.vstack((rgb_arr, pad))
                    cmyk_pad = np.zeros((max_h - h1, w1, 4), dtype=np.uint8)
                    cmyk_arr = np.vstack((cmyk_arr, cmyk_pad))
                if h2 < max_h:
                    pad = np.full((max_h - h2, w2, 3), BG_RGB, dtype=np.uint8)
                    rgb_arr2 = np.vstack((rgb_arr2, pad))
                    cmyk_pad = np.zeros((max_h - h2, w2, 4), dtype=np.uint8)
                    cmyk_arr2 = np.vstack((cmyk_arr2, cmyk_pad))
                gap_rgb = np.full((max_h, GAP_PX, 3), BG_RGB, dtype=np.uint8)
                gap_cmyk = np.zeros((max_h, GAP_PX, 4), dtype=np.uint8)
                rgb_arr = np.hstack((rgb_arr, gap_rgb, rgb_arr2))
                cmyk_arr = np.hstack((cmyk_arr, gap_cmyk, cmyk_arr2))
                has_op = has_op or has_op2
                gap_pt = GAP_PX / zoom
                boxes['_page2'] = {'rect': rect2, 'boxes': boxes2, 'rect0': page_rect,
                                   'gap': gap_pt}
                page_rect = fitz.Rect(0, 0, page_rect.width + gap_pt + rect2.width,
                                      max(page_rect.height, rect2.height))

            if is_offset_single:
                h, w = rgb_arr.shape[:2]
                BG_RGB = np.array([64, 64, 64], dtype=np.uint8)
                blank = np.full((h, w, 3), BG_RGB, dtype=np.uint8)
                fast_rgb_arr = np.hstack((blank, fast_rgb_arr))
                rgb_arr = np.hstack((blank, rgb_arr))
                cmyk_blank = np.zeros((h, w, 4), dtype=np.uint8)
                cmyk_arr = np.hstack((cmyk_blank, cmyk_arr))
                # left side gets blank boxes, right side gets original page 0 boxes
                blank_r = fitz.Rect(0, 0, 0, 0)
                blank_boxes = {k: blank_r for k in boxes}
                boxes['_page2'] = {'rect': page_rect, 'boxes': boxes.copy(),
                                   'rect0': page_rect}
                boxes.update(blank_boxes)
                page_rect = fitz.Rect(0, 0, page_rect.width * 2, page_rect.height)
                # Emit draft for offset single
                offset_boxes_draft = boxes.copy()
                signals.draft.emit(fast_rgb_arr, offset_boxes_draft, zoom,
                                    page_rect, draft_seq)

            if cancel_event.is_set():
                return
            signals.done.emit(rgb_arr, boxes, zoom, has_op, cmyk_arr,
                              page_rect, time.monotonic(), seq)
        except Exception as e:
            import traceback
            traceback.print_exc()
        finally:
            if doc is not None and not use_cached_doc:
                try:
                    doc.close()
                except Exception:
                    pass

    @staticmethod
    def _render_to_entry(path, page_num, zoom, mode, channels,
                          icc_path, sim_profile, spread, is_offset_single,
                          simulate_overprint, box_mask=None, object_filter=None):
        result = {}

        class _Done:
            def emit(self, rgb_arr, boxes, zoom, has_op, cmyk_buf,
                     page_rect, _start_time, _seq):
                pg2 = boxes.pop('_page2', None)
                pg2_info = None
                if pg2 is not None:
                    pg2_info = {
                        'rect': pg2['rect'],
                        'boxes': pg2['boxes'],
                        'rect0': pg2.pop('rect0', None),
                        'gap': pg2.pop('gap', 0),
                    }
                entry = {
                    'rgb_arr': rgb_arr,
                    'cmyk_buf': cmyk_buf,
                    'boxes': boxes,
                    'zoom': zoom,
                    'has_op': has_op,
                    'page_rect': page_rect,
                }
                if pg2_info is not None:
                    entry['pg2_info'] = pg2_info
                result['entry'] = entry

        class _DraftDone:
            def emit(self, rgb_arr, boxes, zoom, page_rect, seq):
                pass

        class _Signals:
            done = _Done()
            draft = _DraftDone()

        cancel_event = threading.Event()
        PreflightWindow._render_bg(
            path, page_num, zoom, mode, channels, icc_path, sim_profile,
            spread, is_offset_single, simulate_overprint, box_mask,
            cancel_event, _Signals(), 0, use_cached_doc=False,
            object_filter=object_filter)
        return result.get('entry')

    def _on_page_done_ready(self, rgb_arr, boxes, zoom, has_op, cmyk_buf,
                            page_rect, _start_time, seq, cache_key):
        if self._overview_active:
            return
        if self._render_cancel.is_set() or seq != self._render_seq:
            return
        pg2 = boxes.pop('_page2', None)
        pg2_info = None
        if pg2 is not None:
            pg2_info = {
                'rect': pg2['rect'],
                'boxes': pg2['boxes'],
                'rect0': pg2.pop('rect0', None),
                'gap': pg2.pop('gap', 0),
            }
        self._display_render_result(rgb_arr, cmyk_buf, boxes, zoom, has_op,
                                     page_rect, pg2_info)
        entry = {
            'rgb_arr': rgb_arr,
            'cmyk_buf': cmyk_buf,
            'boxes': boxes,
            'zoom': zoom,
            'has_op': has_op,
            'page_rect': page_rect,
        }
        if pg2_info is not None:
            entry['pg2_info'] = pg2_info
        if cache_key is not None:
            self._cache_put(cache_key, entry)
        self._schedule_magnifier_cache_build()
        self._schedule_page_prefetch()

    def _on_page_draft_done(self, rgb_arr, boxes, zoom, page_rect, seq):
        if self._overview_active:
            return
        if self._render_cancel.is_set() or seq != self._draft_seq:
            return
        boxes = boxes.copy()
        pg2 = boxes.pop('_page2', None)
        pg2_info = None
        if pg2 is not None:
            pg2_info = {
                'rect': pg2['rect'],
                'boxes': pg2['boxes'],
                'rect0': pg2.get('rect0'),
                'gap': pg2.get('gap', 0),
            }
        self._display_render_result(rgb_arr, None, boxes, zoom,
                                      False, page_rect, pg2_info)

    def _on_mag_cache_ready(self, cache_key, cmyk_arr):
        self.render._mag_cache = cmyk_arr
        self.render._mag_cache_key = cache_key

    def _on_prefetch_done(self, cache_key, entry):
        if self._current_path and self.render.doc:
            self._cache_put(cache_key, entry)

    def _schedule_page_prefetch(self):
        if not self._current_path or not self.render.doc:
            return
        step = 2 if self._spread_mode() in ('normal', 'offset') else 1
        candidates = [
            self._current_page + step,
            self._current_page - step,
        ]
        for page_num in candidates:
            if not 0 <= page_num < self.render.page_count:
                continue
            key = self._render_cache_key(page_num)
            if self._cache_get(key) is not None:
                continue
            self._start_page_prefetch(page_num, key)

    def _start_page_prefetch(self, page_num, cache_key):
        render_page, spread, is_offset_single = self._render_plan(page_num)
        path = self._current_path
        zoom = self._render_zoom()
        mode = self._mode
        channels = {
            'cyan': self.chk_c.isChecked(),
            'magenta': self.chk_m.isChecked(),
            'yellow': self.chk_y.isChecked(),
            'black': self.chk_k.isChecked(),
        }
        icc_path = get_cmyk_icc_path(self.render.doc)
        sim_profile = self.simulation.get_active_profile_path()
        simulate_op = self.chk_simulate_overprint.isChecked()
        object_filter = self._object_filter_state()
        self._thumb_worker.submit_prefetch(
            path, render_page, zoom, mode, channels, icc_path, sim_profile,
            spread, is_offset_single, simulate_op, self._active_box_mask,
            cache_key, object_filter)

    def _start_magnifier_cache_build(self):
        """Build magnifier cache on the worker thread so it's ready on right-click."""
        if not self._current_path:
            return
        enabled = str(self._settings.value(
            "ui/magnifier_enabled", "true")).lower() == "true"
        if not enabled:
            return
        self._thumb_worker.submit_mag(
            self._current_path, self._current_page, self._mag_cancel)

    def _format_box(self, box):
        """Format box dimensions per settings: mm/pt/both"""
        unit = str(self._settings.value("ui/box_unit", "mm"))
        w_mm = box.width * 25.4 / 72
        h_mm = box.height * 25.4 / 72
        if unit == "mm":
            return f"{w_mm:.1f} x {h_mm:.1f} mm"
        elif unit == "pt":
            return f"{box.width:.0f} x {box.height:.0f} pt"
        else:
            return f"{box.width:.0f} x {box.height:.0f} pt ({w_mm:.1f} x {h_mm:.1f} mm)"

    def _format_diff(self, dw, dh):
        unit = str(self._settings.value("ui/box_unit", "mm"))
        if unit == "mm":
            w = dw * 25.4 / 72
            h = dh * 25.4 / 72
            return f"{w:+.1f} x {h:+.1f} mm"
        elif unit == "pt":
            return f"{dw:+.0f} x {dh:+.0f} pt"
        else:
            w = dw * 25.4 / 72
            h = dh * 25.4 / 72
            return f"{dw:+.0f} x {dh:+.0f} pt ({w:+.1f} x {h:+.1f} mm)"

    def _set_box_mask(self, name, active):
        if active:
            for n, btn in self._box_eye_btns.items():
                if n != name:
                    btn.blockSignals(True)
                    btn.setChecked(False)
                    btn.setIcon(self._icon('crop'))
                    btn.blockSignals(False)
        self._active_box_mask = name if active else None
        self.page_widget._mask_box = self._active_box_mask
        btn = self._box_eye_btns.get(name)
        if btn:
            btn.setIcon(self._icon('crop_on' if active else 'crop'))
        self.page_widget.viewport().update()
        self._start_bg_render()

    def _icon(self, name, color=None):
        path = os.path.join(os.path.dirname(__file__), '..', '..',
                            'resources', 'icons', f'{name}.svg')
        if not os.path.isfile(path):
            return QIcon()
        if color is None:
            theme = QSettings("PDFPreflight", "Viewer").value("ui/theme", "dark_teal.xml")
            color = '#212121' if 'light' in theme else '#e0e0e0'
        with open(path) as f:
            svg = f.read()
        svg = svg.replace('fill="currentColor"', f'fill="{color}"')
        pixmap = QPixmap()
        pixmap.loadFromData(svg.encode())
        return QIcon(pixmap)

    def _toolbar_spacer(self, width=8):
        w = QWidget()
        w.setFixedWidth(width)
        return w

    def _toolbar_stretch(self):
        w = QWidget()
        w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        return w

    def _update_page_spin_width(self, max_value):
        digits = max(1, len(str(max(1, max_value))))
        fm = self.spin_page.fontMetrics()
        text_w = fm.horizontalAdvance("9" * digits)
        frame_w = self.style().pixelMetric(
            QStyle.PixelMetric.PM_SpinBoxFrameWidth)
        self.spin_page.setFixedWidth(text_w + 2 * frame_w + 8)

    def _build_ui(self):
        self.page_widget = PageWidget()
        self.page_widget.viewport().setAcceptDrops(True)
        self.page_widget.viewport().installEventFilter(self)
        self.setCentralWidget(self.page_widget)

        self._toolbar = QToolBar("Main")
        self._toolbar.setIconSize(QSize(20, 20))
        self._toolbar.setMinimumHeight(40)
        self._toolbar.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._toolbar.setMovable(False)
        self._toolbar.setStyleSheet(f"""
            QToolBar {{ padding: 4px 2px; spacing: 4px; }}
            QToolBar QToolButton {{ border: none; padding: 6px; border-radius: 4px; }}
            QToolBar QToolButton:hover {{ border: none; background: rgba(128,128,128,0.2); }}
            QToolBar QToolButton:checked {{ border: none; background: {self._accent_color}; color: #fff; }}
            QToolBar QToolButton::menu-button {{ border: none; width: 16px; padding-left: 4px; }}
        """)
        self.addToolBar(self._toolbar)
        tb = self._toolbar

        # ── Left group ──────────────────────────────────────────
        tb.addWidget(self._toolbar_spacer(4))
        self._open_btn = QToolButton()
        self._open_btn.setIcon(self._icon('open'))
        self._open_btn.setToolTip("Open PDF (Ctrl+O)")
        self._open_btn.clicked.connect(self._open_file)
        tb.addWidget(self._open_btn)
        self._recent_btn = QToolButton()
        self._recent_btn.setIcon(self._icon('recent'))
        self._recent_btn.setToolTip("Open recent file")
        self._recent_menu = QMenu(self)
        self._recent_menu.setTearOffEnabled(False)
        self._recent_btn.setMenu(self._recent_menu)
        self._recent_btn.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        self._recent_btn.setStyleSheet(
            "QToolButton::menu-indicator { image: none; }")
        self._rebuild_recent_menu()
        tb.addWidget(self._recent_btn)
        tb.addWidget(self._toolbar_spacer(12))
        self.act_save = QAction(self._icon('save'), "Save PDF (Ctrl+S)", self)
        self.act_save._icon_name = 'save'
        self.act_save.setToolTip("Save PDF (Ctrl+S)")
        self.act_save.triggered.connect(self._save_pdf)
        self.act_save.setVisible(False)
        tb.addAction(self.act_save)
        self.act_close = QAction(self._icon('close'), "Close PDF (Ctrl+W)", self)
        self.act_close._icon_name = 'close'
        self.act_close.setToolTip("Close PDF (Ctrl+W)")
        self.act_close.triggered.connect(self._close_file)
        self.act_close.setVisible(False)
        tb.addAction(self.act_close)
        tb.addWidget(self._toolbar_spacer(32))
        self.act_zoom_fit = QAction(self._icon('zoom_fit'), "Fit to viewport", self)
        self.act_zoom_fit._icon_name = 'zoom_fit'
        self.act_zoom_fit.setToolTip("Fit to viewport")
        tb.addAction(self.act_zoom_fit)
        self.lbl_zoom = QLabel(f" {self._zoom*100:.0f}%")
        tb.addWidget(self.lbl_zoom)

        # ── Center: page navigation ──────────────────────────────
        tb.addWidget(self._toolbar_stretch())
        self.act_first = QAction(self._icon('first'), "First page", self)
        self.act_first._icon_name = 'first'
        self.act_first.setToolTip("First page")
        self.act_prev = QAction(self._icon('prev'), "Previous page", self)
        self.act_prev._icon_name = 'prev'
        self.act_prev.setToolTip("Previous page")
        self.act_next = QAction(self._icon('next'), "Next page", self)
        self.act_next._icon_name = 'next'
        self.act_next.setToolTip("Next page")
        self.act_last = QAction(self._icon('last'), "Last page", self)
        self.act_last._icon_name = 'last'
        self.act_last.setToolTip("Last page")
        tb.addAction(self.act_first)
        tb.addAction(self.act_prev)
        _prev_btn = tb.widgetForAction(self.act_prev)
        if _prev_btn:
            _prev_btn.setFixedWidth(48)
        self.spin_page = QSpinBox()
        self.spin_page.setMinimum(1)
        self.spin_page.setMaximum(1)
        self.spin_page.setButtonSymbols(
            QSpinBox.ButtonSymbols.NoButtons)
        self._update_page_spin_width(1)
        tb.addWidget(self.spin_page)
        self.lbl_page_total = QLabel("/ 1")
        tb.addWidget(self.lbl_page_total)
        tb.addAction(self.act_next)
        _next_btn = tb.widgetForAction(self.act_next)
        if _next_btn:
            _next_btn.setFixedWidth(48)
        tb.addAction(self.act_last)

        # ── Right group ─────────────────────────────────────────
        tb.addWidget(self._toolbar_stretch())
        self.act_single = QAction(self._icon('single'), "Single page", self)
        self.act_single._icon_name = 'single'
        self.act_single.setToolTip("Single page")
        self.act_single.setCheckable(True)
        self.act_single.setChecked(True)
        self.act_spread = QAction(self._icon('spread'), "Two-page spread", self)
        self.act_spread._icon_name = 'spread'
        self.act_spread.setToolTip("Two-page spread")
        self.act_spread.setCheckable(True)
        self.act_spread_offset = QAction(self._icon('spread_offset'), "Offset spread", self)
        self.act_spread_offset._icon_name = 'spread_offset'
        self.act_spread_offset.setToolTip("Offset spread (first page alone on right)")
        self.act_spread_offset.setCheckable(True)
        tb.addAction(self.act_single)
        tb.addAction(self.act_spread)
        tb.addAction(self.act_spread_offset)
        tb.addWidget(self._toolbar_stretch())
        self._settings_btn = QToolButton()
        self._settings_btn.setIcon(self._icon('settings'))
        self._settings_btn.setToolTip("Settings")
        self._settings_btn.setStyleSheet(
            "QToolButton { padding: 4px; border-radius: 4px; }")
        self._settings_btn.clicked.connect(self._show_settings)
        tb.addWidget(self._settings_btn)

        # ===================== Right dock =====================
        self._dock = QDockWidget("", self)
        self._dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea)
        self._dock.setFeatures(
            QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)

        dock_container = QWidget()
        dock_container.setStyleSheet("background: transparent;")
        dock_hl = QHBoxLayout(dock_container)
        dock_hl.setContentsMargins(0, 0, 0, 0)
        dock_hl.setSpacing(0)

        self._dock_collapse_btn = QToolButton()
        self._dock_collapse_btn.setArrowType(Qt.ArrowType.RightArrow)
        self._dock_collapse_btn.setFixedSize(16, 60)
        self._dock_collapse_btn.setStyleSheet(
            "QToolButton { border: none; border-radius: 2px; margin: 0; }"
            "QToolButton:hover { background: rgba(255,255,255,0.1); }")
        self._dock_collapse_btn.clicked.connect(self._toggle_dock)
        self._dock_collapsed = False
        self._dock_old_width = 300

        self._dock_content = QWidget()
        self._dock_content.setObjectName("dock_content")
        self._dock_content.setMinimumWidth(480)
        dp = self._dock_content.palette()
        dp.setColor(QPalette.ColorRole.Window, QColor('#1a1a1a'))
        self._dock_content.setPalette(dp)
        self._dock_content.setAutoFillBackground(True)
        dock_font = QFont()
        dock_font.setPointSize(9)
        dock_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        self._dock_content.setFont(dock_font)
        dvl = QVBoxLayout(self._dock_content)
        dvl.setContentsMargins(4, 4, 4, 4)
        dvl.setSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; }"
            "QScrollBar:vertical { width: 0px; }"
            "QScrollBar::handle:vertical { width: 0px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { width: 0px; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { width: 0px; }")

        info = QWidget()
        info.setStyleSheet("background: transparent;")
        vl = QVBoxLayout(info)
        vl.setSpacing(4)
        vl.setContentsMargins(2, 2, 2, 2)

        self._collapse_blocks = {}

        # Color Spaces & Profiles (top)
        blk = CollapsibleBlock("Color Profiles", "colorspace")
        csw = QWidget()
        csv = QVBoxLayout(csw)
        csv.setContentsMargins(4, 4, 4, 4)
        self.cs_grid = QWidget()
        self.cs_grid.setStyleSheet("background: transparent;")
        self.cs_grid_layout = QGridLayout(self.cs_grid)
        self.cs_grid_layout.setContentsMargins(0, 0, 0, 0)
        self.cs_grid_layout.setHorizontalSpacing(14)
        self.cs_grid_layout.setVerticalSpacing(5)
        csv.addWidget(self.cs_grid)
        blk.set_content(csw)
        self._collapse_blocks['colorspace'] = blk
        vl.addWidget(blk)

        # Color Picker
        blk = CollapsibleBlock("Color Picker", "color_picker")
        cw = QWidget()
        gl = QGridLayout(cw)
        gl.setContentsMargins(4, 4, 4, 4)
        gl.setHorizontalSpacing(4)
        self.l_c = QLabel("—")
        self.l_c.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.l_m = QLabel("—")
        self.l_m.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.l_y = QLabel("—")
        self.l_y.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.l_k = QLabel("—")
        self.l_k.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.l_op = QLabel("—")
        self.l_op.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.l_spot_info = QLabel("—")
        self.l_spot_info.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.l_source = QLabel("")
        self.l_source.setStyleSheet("font-size: 9pt;")
        self.l_source.setVisible(False)
        self.l_warning = QLabel("")
        self.l_warning.setStyleSheet("color: #ffaa00; font-weight: bold; font-size: 9pt;")
        self.l_warning.setWordWrap(True)
        self.l_warning.setVisible(False)
        # Fixed-height container so the block never jumps when the source /
        # warning info lines appear or disappear.
        self._cp_info_box = QWidget()
        info_layout = QVBoxLayout(self._cp_info_box)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(3)
        info_layout.addWidget(self.l_source)
        info_layout.addWidget(self.l_warning)
        info_layout.addStretch(1)
        cp_fm = QFontMetrics(self.l_warning.font())
        self._cp_info_box.setFixedHeight(int(cp_fm.height() * 4 + 6))
        gl.addWidget(QLabel("Cyan"), 0, 0)
        gl.addWidget(self.l_c, 0, 1)
        gl.addWidget(QLabel("Magenta"), 1, 0)
        gl.addWidget(self.l_m, 1, 1)
        gl.addWidget(QLabel("Yellow"), 2, 0)
        gl.addWidget(self.l_y, 2, 1)
        gl.addWidget(QLabel("Black"), 3, 0)
        gl.addWidget(self.l_k, 3, 1)
        gl.addWidget(QLabel("Spot"), 4, 0)
        gl.addWidget(self.l_spot_info, 4, 1)
        spacer = QLabel("")
        spacer.setFixedHeight(6)
        gl.addWidget(spacer, 5, 0, 1, 2)
        gl.addWidget(QLabel("Overprint"), 6, 0)
        gl.addWidget(self.l_op, 6, 1)
        gl.addWidget(self._cp_info_box, 7, 0, 1, 2)
        blk.set_content(cw)
        self._collapse_blocks['color_picker'] = blk
        vl.addWidget(blk)

        # Separation Channels
        blk = CollapsibleBlock("Separation Channels", "separation")
        sw = QWidget()
        sv = QVBoxLayout(sw)
        sv.setContentsMargins(4, 4, 4, 4)
        sv.setSpacing(4)
        self._sep_btn_style = {
            'cyan': ("#00aaff", "#00aaff"),
            'magenta': ("#ff00aa", "#ff00aa"),
            'yellow': ("#aaaa00", "#aaaa00"),
            'black': ("#888", "#888"),
        }
        self.chk_c = QPushButton("Cyan")
        self.chk_c.setCheckable(True)
        self.chk_c.setChecked(True)
        self.chk_m = QPushButton("Magenta")
        self.chk_m.setCheckable(True)
        self.chk_m.setChecked(True)
        self.chk_y = QPushButton("Yellow")
        self.chk_y.setCheckable(True)
        self.chk_y.setChecked(True)
        self.chk_k = QPushButton("Black")
        self.chk_k.setCheckable(True)
        self.chk_k.setChecked(True)
        sep_hl = QHBoxLayout()
        sep_hl.setSpacing(12)
        for btn, key in [(self.chk_c, 'cyan'), (self.chk_m, 'magenta'),
                         (self.chk_y, 'yellow'), (self.chk_k, 'black')]:
            color, _ = self._sep_btn_style[key]
            btn._sep_key = key
            btn.toggled.connect(self._on_sep_btn_style)
            btn.setStyleSheet(
                f"QPushButton {{ border: 1px solid {self._muted_color}; border-left: 3px solid {color}; "
                f"text-align: center; padding: 4px 8px; color: {self._muted_color}; font-weight: normal; font-size: 9pt; }}"
                f"QPushButton:hover {{ border-left: 4px solid {color}; background: rgba(128,128,128,0.15); }}"
                f"QPushButton:checked {{ border-left: 4px solid {color}; }}"
                f"QPushButton:disabled {{ border: 1px solid {self._muted_color}; border-left: 3px solid {self._muted_color}; "
                f"color: {self._muted_color}; background: transparent; }}")
            btn.setFixedHeight(28)
            sep_hl.addWidget(btn, 1)  # stretch factor 1 — fill space equally
        sv.addLayout(sep_hl)
        self._spot_container = QWidget()
        self._spot_layout = QHBoxLayout(self._spot_container)
        self._spot_layout.setContentsMargins(0, 0, 0, 0)
        self._spot_layout.setSpacing(4)
        sv.addWidget(self._spot_container)

        # Simulate Overprint checkbox
        self.chk_simulate_overprint = QCheckBox("Simulate Overprint")
        self.chk_simulate_overprint.setChecked(True)
        self.chk_simulate_overprint.setToolTip(
            "When checked, overprint is applied during separation preview.\n"
            "When unchecked, all objects are rendered as knockout.")
        self.chk_simulate_overprint.setStyleSheet(
            "QCheckBox { font-size: 9pt; }"
            "QCheckBox::indicator { width: 14px; height: 14px; }")
        self.chk_simulate_overprint.toggled.connect(self._on_overprint_sim_toggled)
        sv.addWidget(self.chk_simulate_overprint)

        blk.set_content(sw)
        self._collapse_blocks['separation'] = blk
        vl.addWidget(blk)

        # Object Filter
        from PyQt6.QtWidgets import QStyle
        blk = CollapsibleBlock("Object Filter", "object_filter")
        ow = QWidget()
        ov = QVBoxLayout(ow)
        ov.setContentsMargins(4, 4, 4, 4)
        ov.setSpacing(4)

        from preview.object_filter import CATEGORIES, LABELS
        self._of_rows = {}

        def _make_of_cb(key):
            cb = QCheckBox(LABELS[key].lower())
            cb.setChecked(True)
            cb.setStyleSheet(
                "QCheckBox { font-size: 9pt; color: #fff; background: transparent; }"
                "QCheckBox::indicator { width: 14px; height: 14px; }")
            cb.toggled.connect(
                lambda checked, k=key: self._on_of_toggle(k, checked))
            self._of_rows[key] = cb
            return cb

        # Simple vertical list of the three object categories.
        for key in ('images', 'text', 'vector'):
            ov.addWidget(_make_of_cb(key))

        # "View All" reset button below all filters.
        self._of_reset_row = QWidget()
        self._of_reset_row.setFixedHeight(30)
        reset_hl = QHBoxLayout(self._of_reset_row)
        reset_hl.setContentsMargins(4, 2, 4, 2)
        reset_hl.addStretch(1)
        self._of_reset_btn = QPushButton("View All")
        self._of_reset_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self._of_reset_btn.setStyleSheet(
            f"QPushButton {{ border: 1px solid {self._accent_color}; "
            f"color: {self._accent_color}; border-radius: 4px; "
            f"padding: 4px 12px; font-size: 8pt; }}"
            f"QPushButton:hover {{ background: rgba(29, 233, 182, 0.1); }}"
            f"QPushButton:disabled {{ border: 1px solid #777; "
            f"color: #777; background: transparent; }}")
        self._of_reset_btn.clicked.connect(self._on_of_reset)
        self._of_reset_btn.setEnabled(False)
        reset_hl.addWidget(self._of_reset_btn, 0, Qt.AlignmentFlag.AlignRight)
        ov.addWidget(self._of_reset_row)

        blk.set_content(ow)
        self._collapse_blocks['object_filter'] = blk
        vl.addWidget(blk)

        # Page Boxes
        blk = CollapsibleBlock("Page Boxes", "boxes")
        bw = QWidget()
        bx = QVBoxLayout(bw)
        bx.setContentsMargins(4, 4, 4, 4)

        self._box_btns = {}

        self.boxes_grid = QWidget()
        self.boxes_grid.setStyleSheet("background: transparent;")
        self.boxes_grid_layout = QGridLayout(self.boxes_grid)
        self.boxes_grid_layout.setContentsMargins(0, 0, 0, 0)
        self.boxes_grid_layout.setVerticalSpacing(6)
        self.boxes_grid_layout.setHorizontalSpacing(14)
        self.boxes_grid_layout.setColumnMinimumWidth(0, 24)
        self.boxes_grid_layout.setColumnMinimumWidth(1, 70)
        bx.addWidget(self.boxes_grid)
        blk.set_content(bw)
        self._collapse_blocks['boxes'] = blk
        vl.addWidget(blk)

        # TAC
        blk = CollapsibleBlock("TAC (Total Area Coverage)", "tac")
        tw = QWidget()
        tac_l = QVBoxLayout(tw)
        tac_l.setContentsMargins(4, 4, 4, 4)
        self.l_tac = QLabel("—")
        tac_l.addWidget(self.l_tac)
        blk.set_content(tw)
        self._collapse_blocks['tac'] = blk
        vl.addWidget(blk)

        # Fonts
        blk = CollapsibleBlock("Fonts", "fonts")
        fw = QWidget()
        fv = QVBoxLayout(fw)
        fv.setContentsMargins(4, 4, 4, 4)
        fv.setSpacing(2)
        self.font_list = QListWidget()
        self.font_list.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.font_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.font_list.setStyleSheet(
            "QListWidget::item { padding: 0px 4px; font-size: 7pt; margin: 0px; }"
            "QListWidget { outline: none; }"
            "QScrollBar:vertical { width: 8px; background: transparent; }"
            "QScrollBar::handle:vertical { background: rgba(128,128,128,0.5);"
            " border-radius: 4px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
            " { height: 0px; }")
        fv.addWidget(self.font_list)
        hl = QHBoxLayout()
        hl.setSpacing(4)
        self._font_all_pages = QCheckBox("All pages")
        self._font_all_pages.setChecked(True)
        self._font_all_pages.toggled.connect(self._update_info)
        hl.addWidget(self._font_all_pages)
        self._btn_curves = QPushButton("Convert to curves")
        self._btn_curves.clicked.connect(self._fonts_to_curves)
        self._btn_curves.setStyleSheet(
            f"QPushButton {{ border: 1px solid {self._accent_color}; color: {self._muted_color}; "
            f"border-radius: 4px; padding: 6px 16px; font-weight: normal; font-size: 9pt; }}"
            f"QPushButton:hover {{ border: 1px solid {self._accent_color}; color: {self._accent_color}; }}"
            f"QPushButton:pressed {{ border: 1px solid {self._accent_color}; color: {self._accent_color}; }}")
        hl.addWidget(self._btn_curves)
        fv.addLayout(hl)
        blk.set_content(fw)
        self._collapse_blocks['fonts'] = blk
        vl.addWidget(blk)

        # Security
        blk = CollapsibleBlock("Security", "security")
        sew = QWidget()
        sev = QVBoxLayout(sew)
        sev.setContentsMargins(4, 4, 4, 4)
        self.sec_grid = QWidget()
        self.sec_grid.setStyleSheet("background: transparent;")
        self.sec_grid_layout = QGridLayout(self.sec_grid)
        self.sec_grid_layout.setContentsMargins(0, 0, 0, 0)
        self.sec_grid_layout.setSpacing(5)
        sev.addWidget(self.sec_grid)
        blk.set_content(sew)
        self._collapse_blocks['security'] = blk
        vl.addWidget(blk)

        vl.addStretch()
        scroll.setWidget(info)
        dvl.addWidget(scroll)
        dock_hl.addWidget(self._dock_collapse_btn)
        dock_hl.addWidget(self._dock_content, 1)
        self._dock.setWidget(dock_container)
        dock_side = self._settings.value("ui/dock_side", "right")
        area = (Qt.DockWidgetArea.LeftDockWidgetArea
                if dock_side == 'left'
                else Qt.DockWidgetArea.RightDockWidgetArea)
        self.addDockWidget(area, self._dock)



    def _toggle_dock(self):
        if self._dock_collapsed:
            self._dock.setMaximumWidth(16777215)
            self._dock_content.show()
            self._dock.setMinimumWidth(0)
            self.resizeDocks([self._dock], [self._dock_old_width], Qt.Orientation.Horizontal)
            self._dock_collapse_btn.setArrowType(Qt.ArrowType.RightArrow)
            self._dock_collapsed = False
        else:
            self._dock_old_width = self._dock.width()
            btn_w = self._dock_collapse_btn.width() + 4
            self._dock.setMinimumWidth(btn_w)
            self._dock.setMaximumWidth(btn_w)
            self._dock_content.hide()
            self._dock_collapse_btn.setArrowType(Qt.ArrowType.LeftArrow)
            self._dock_collapsed = True

    def _connect_signals(self):
        self.act_first.triggered.connect(self._first_page)
        self.act_prev.triggered.connect(self._prev_page)
        self.act_next.triggered.connect(self._next_page)
        self.act_last.triggered.connect(self._last_page)
        self.spin_page.valueChanged.connect(self._go_to_page)

        self.act_zoom_fit.triggered.connect(self._zoom_fit)

        self.act_single.triggered.connect(self._set_view_single)
        self.act_spread.triggered.connect(self._set_view_spread)
        self.act_spread_offset.triggered.connect(self._set_view_spread_offset)

        self.page_widget.clicked.connect(self._on_click)
        self.page_widget.empty_clicked.connect(self._open_file)
        self.page_widget.mouse_moved.connect(self._on_mouse_move)
        self.page_widget.zoom_changed.connect(self._on_wheel)
        self.page_widget.double_clicked.connect(self._zoom_fit)
        self.page_widget.page_nav.connect(self._on_page_nav)
        self.page_widget.page_activated.connect(self._on_overview_activate)
        self.page_widget.overview_clicked.connect(self._on_overview_current)
        self.page_widget.verticalScrollBar().valueChanged.connect(
            self._on_overview_scroll)
        self.page_widget.horizontalScrollBar().valueChanged.connect(
            self._on_overview_scroll)
        self.page_widget.view_resized.connect(self._on_overview_resized)

        self.chk_c.toggled.connect(self._on_sep_changed)
        self.chk_m.toggled.connect(self._on_sep_changed)
        self.chk_y.toggled.connect(self._on_sep_changed)
        self.chk_k.toggled.connect(self._on_sep_changed)

        self.page_widget.set_magnifier_renderer(self._render_magnifier_region)
        self._apply_magnifier_setting()

    def _apply_magnifier_setting(self, enabled=None):
        if enabled is None:
            enabled = str(self._settings.value(
                "ui/magnifier_enabled", "true")).lower() == "true"
        self.page_widget.set_magnifier_allowed(enabled)
        if not enabled:
            if self._magnifier_cache_timer is not None:
                self._magnifier_cache_timer.stop()
            self.render._mag_cache = None
            self.render._mag_cache_key = None

    def _show_settings(self):
        dlg = SettingsDialog(self)
        dlg.shortcuts_changed.connect(self._setup_shortcuts)
        dlg.theme_changed.connect(self._apply_theme)
        dlg.dock_side_changed.connect(self._reposition_dock)
        dlg.box_unit_changed.connect(self._update_info)
        dlg.magnifier_changed.connect(self._apply_magnifier_setting)
        if dlg.exec():
            self._settings.sync()

    def _reposition_dock(self, side):
        self.removeDockWidget(self._dock)
        area = (Qt.DockWidgetArea.LeftDockWidgetArea
                if side == 'left'
                else Qt.DockWidgetArea.RightDockWidgetArea)
        self.addDockWidget(area, self._dock)
        self._dock.show()
        self._dock_collapse_btn.show()
        if self._dock_collapsed:
            btn_w = self._dock_collapse_btn.width() + 4
            self._dock.setMinimumWidth(btn_w)
            self._dock.setMaximumWidth(btn_w)
            self._dock_content.hide()

    def _get_cmyk_at_scene(self, scene_pos):
        if self._cmyk_buf is None:
            return None, None, None
        sx = int(round(scene_pos.x()))
        sy = int(round(scene_pos.y()))
        h, w = self._cmyk_buf.shape[:2]
        sx = max(0, min(w - 1, sx))
        sy = max(0, min(h - 1, sy))
        cmyk = self._cmyk_buf[sy, sx]
        rz = self._last_render_zoom if self._last_render_zoom > 0 else self._actual_zoom()
        pdf_x = sx / rz
        pdf_y = (self.render.doc[self._current_page].rect.height
                 - sy / rz)
        return cmyk, pdf_x, pdf_y

    def _get_cmyk_precise(self, scene_pos):
        if not self.render.doc:
            return None, None, None
        rz = self._last_render_zoom if self._last_render_zoom > 0 else self._actual_zoom()
        page = self.render.doc[self._current_page]
        sx = int(round(scene_pos.x()))
        sy = int(round(scene_pos.y()))
        pdf_x = sx / rz
        pdf_y = page.rect.height - sy / rz
        cmyk = self.render.sample_cmyk(self._current_page, pdf_x, pdf_y)
        if cmyk is None:
            return None, None, None
        return cmyk, pdf_x, pdf_y

    def _is_cmyk_source(self, source_info):
        """True if source_info carries a CMYK-based stored color
        (DeviceCMYK or ICCBased / Separation CMYK)."""
        if not source_info or not source_info.get('found'):
            return False
        cs = source_info.get('colorspace')
        if cs is None:
            return False
        if cs == 'DeviceCMYK':
            return True
        # ICCBased / Separation / DeviceN CMYK colorspaces are stored by name
        return cs not in ('DeviceRGB', 'DeviceGray')

    def _uses_source(self, cmyk, source_info):
        """Whether the exact source CMYK should be shown for this pixel."""
        c, m, y_, k = int(cmyk[0]), int(cmyk[1]), int(cmyk[2]), int(cmyk[3])
        if not self._is_cmyk_source(source_info):
            return False
        fill = source_info.get('fill_color', (0, 0, 0, 0))
        if len(fill) < 4:
            return False
        s255 = (int(fill[0] * 2.55), int(fill[1] * 2.55),
                int(fill[2] * 2.55), int(fill[3] * 2.55))
        return _cmyk_diff(s255, (c, m, y_, k)) <= _CMYK_SOURCE_MAX_DIFF

    def _choose_cmyk_display(self, cmyk, source_info):
        """Return (C,M,Y,K) percentages to display, preferring the exact
        stored source value when it matches the rendered pixel closely."""
        c, m, y_, k = int(cmyk[0]), int(cmyk[1]), int(cmyk[2]), int(cmyk[3])
        if self._uses_source(cmyk, source_info):
            fill = source_info['fill_color']
            return (fill[0] * 100, fill[1] * 100, fill[2] * 100, fill[3] * 100)
        return (c / 2.55, m / 2.55, y_ / 2.55, k / 2.55)

    def _update_cmyk_labels(self, cmyk, pdf_x, pdf_y, source_info=None):
        c, m, y_, k = int(cmyk[0]), int(cmyk[1]), int(cmyk[2]), int(cmyk[3])

        # If we have exact CMYK source values (DeviceCMYK or ICCBased),
        # show those instead of the rendered/ICC-converted pixmap value.
        # But cross-check against the rendered pixel: if the source color is
        # very different from what is actually painted at this point (e.g.
        # white text sitting on a colored object, or an anti-aliasing
        # boundary), the resolved source is NOT the true pixel color — fall
        # back to the rendered value so a wrong "previous" color doesn't flash.
        cc, cm, cy_, ck = self._choose_cmyk_display(cmyk, source_info)

        self.l_c.setText(f"{cc:.1f}%")
        self.l_m.setText(f"{cm:.1f}%")
        self.l_y.setText(f"{cy_:.1f}%")
        self.l_k.setText(f"{ck:.1f}%")

        if self._uses_source(cmyk, source_info):
            fill = source_info['fill_color']
            self._last_pixel = (int(fill[0]*2.55), int(fill[1]*2.55),
                                int(fill[2]*2.55), int(fill[3]*2.55), pdf_x, pdf_y)
        else:
            self._last_pixel = (c, m, y_, k, pdf_x, pdf_y)

        has_op = False
        op_detail = ""
        if self.render.doc:
            from preview.overprint import check_overprint_at
            op_result = check_overprint_at(self.render.doc,
                                           self.render.doc[self._current_page],
                                           pdf_x, pdf_y)
            has_op = op_result['overprint']
            if has_op:
                parts = []
                if op_result.get('fill'):
                    parts.append("Fill")
                if op_result.get('stroke'):
                    parts.append("Stroke")
                op_detail = " + ".join(parts) if parts else "Yes"
        self.l_op.setText(op_detail if op_detail else "No")

        analysis = self.color_picker.analyze_source(source_info, cmyk)
        if source_info is not None:
            if analysis['source_color_desc']:
                self.l_source.setText(f"Source: {analysis['source_color_desc']}")
                self.l_source.setVisible(True)
            else:
                self.l_source.setText("")
                self.l_source.setVisible(False)
            if analysis['warning']:
                self.l_warning.setText(analysis['warning'])
                self.l_warning.setVisible(True)
            else:
                self.l_warning.setText("")
                self.l_warning.setVisible(False)
            if analysis['rich_black']:
                self.l_warning.setStyleSheet(
                    "color: #ffaa00; font-weight: bold; font-size: 9pt;")
            else:
                self.l_warning.setStyleSheet("")
        else:
            self.l_source.setText("")
            self.l_source.setVisible(False)
            self.l_warning.setText("")
            self.l_warning.setVisible(False)
            self.l_warning.setStyleSheet("")

    def _clear_cmyk_labels(self):
        self.l_c.setText("—")
        self.l_m.setText("—")
        self.l_y.setText("—")
        self.l_k.setText("—")
        self.l_op.setText("—")
        self.l_source.setText("")
        self.l_source.setVisible(False)
        self.l_warning.setText("")
        self.l_warning.setVisible(False)
        self.l_warning.setStyleSheet("")
        self._last_pixel = None



    def _render_magnifier_region(self, scene_x, scene_y):
        if not self.render.doc:
            return None
        rz = self._last_render_zoom if self._last_render_zoom > 0 else self._actual_zoom()
        page = self.render.doc[self._current_page]
        pdf_x = scene_x / rz
        pdf_y = page.rect.height - scene_y / rz
        return self.render.render_magnifier_region(
            self._current_page, pdf_x, pdf_y)

    def _on_click(self, scene_pos):
        cmyk, px, py = self._get_cmyk_precise(scene_pos)
        if cmyk is not None:
            source_info = self.render.get_source_color_at(self._current_page, px, py)
            self._update_cmyk_labels(cmyk, px, py, source_info=source_info)
        else:
            self._clear_cmyk_labels()

    def _on_mouse_move(self, scene_pos):
        cmyk, px, py = self._get_cmyk_at_scene(scene_pos)
        if cmyk is None:
            self.page_widget.hide_color_tooltip()
            self._clear_cmyk_labels()
            # Cancel any pending debounced source lookup
            if hasattr(self, '_hover_src_timer') and self._hover_src_timer.isActive():
                self._hover_src_timer.stop()
            return

        c, m, y_, k = int(cmyk[0]), int(cmyk[1]), int(cmyk[2]), int(cmyk[3])

        # Resolve the exact stored CMYK SYNCHRONOUSLY. The lookup is cached
        # (content stream parsed once per page; text/drawings cached), so it is
        # cheap on every move. Showing it immediately prevents the rendered vs.
        # source oscillation that occurred when two updates (immediate + the
        # 45 ms debounce) raced and disagreed (e.g. gray text flashing 100% K).
        source_info = None
        if self.render.doc:
            source_info = self.render.get_source_color_at(self._current_page, px, py)

        self._update_cmyk_labels(cmyk, px, py, source_info=source_info)

        # Tooltip uses the same chosen value (source when valid, else rendered)
        cc, cm, cy_, ck = self._choose_cmyk_display(cmyk, source_info)
        modifiers = QApplication.keyboardModifiers()
        shift_mode = self._settings.value("ui/shift_mode", "hide")
        show_on_shift = (shift_mode == "show")
        shift_down = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if self._color_overlay_enabled:
            show_on_shift = not show_on_shift
        tooltip_text = (
            f"C  {cc:.0f}%\n"
            f"M  {cm:.0f}%\n"
            f"Y  {cy_:.0f}%\n"
            f"K  {ck:.0f}%"
        )
        if (show_on_shift and shift_down) or (
                not show_on_shift and not shift_down):
            if not self.page_widget.is_magnifier_active:
                self.page_widget.show_color_tooltip(
                    self.page_widget.viewport().mapFromGlobal(QCursor.pos()),
                    tooltip_text
                )
        else:
            self.page_widget.hide_color_tooltip()

        # Debounced lookup still runs to attach overprint info; it re-shows
        # the same source value, so the panel stays stable (no oscillation).
        if not hasattr(self, '_hover_src_timer'):
            from PyQt6.QtCore import QTimer
            self._hover_src_timer = QTimer(self)
            self._hover_src_timer.setSingleShot(True)
            self._hover_src_timer.timeout.connect(self._hover_resolve_source)
        self._hover_px, self._hover_py = px, py
        self._hover_cmyk = (c, m, y_, k)
        self._hover_src_timer.start(45)

    def _hover_resolve_source(self):
        """Debounced: resolve the EXACT stored CMYK at the last hover position
        and update the panel/tooltip once the mouse settles briefly."""
        if not hasattr(self, '_hover_cmyk'):
            return
        px, py = self._hover_px, self._hover_py
        c, m, y_, k = self._hover_cmyk

        source_info = None
        op_info = ""
        if self.render.doc:
            source_info = self.render.get_source_color_at(self._current_page, px, py)
            from preview.overprint import check_overprint_at
            op_result = check_overprint_at(self.render.doc,
                                           self.render.doc[self._current_page],
                                           px, py)
            if op_result['overprint']:
                parts = []
                if op_result.get('fill'):
                    parts.append("Fill")
                if op_result.get('stroke'):
                    parts.append("Stroke")
                op_info = "\nOP: " + "+".join(parts) if parts else ""

        self._update_cmyk_labels(
            (c, m, y_, k), px, py, source_info=source_info)

        modifiers = QApplication.keyboardModifiers()
        shift_mode = self._settings.value("ui/shift_mode", "hide")
        show_on_shift = (shift_mode == "show")
        shift_down = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if self._color_overlay_enabled:
            show_on_shift = not show_on_shift
        if (show_on_shift and shift_down) or (
                not show_on_shift and not shift_down):
            if not self.page_widget.is_magnifier_active:
                tcc, tcm, tcy_, tck = self._choose_cmyk_display(
                    (c, m, y_, k), source_info)
                tooltip_text = (
                    f"C  {tcc:.0f}%\n"
                    f"M  {tcm:.0f}%\n"
                    f"Y  {tcy_:.0f}%\n"
                    f"K  {tck:.0f}%"
                    f"{op_info}"
                )
                self.page_widget.show_color_tooltip(
                    self.page_widget.viewport().mapFromGlobal(QCursor.pos()),
                    tooltip_text
                )

    def _clear_grid(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _add_row(self, layout, row, label, value, value_color=None):
        lbl = QLabel(label)
        lbl.setStyleSheet("font-weight: bold; background: transparent;")
        val = QLabel(value)
        if value_color:
            val.setStyleSheet(f"color: {value_color}; background: transparent;")
        else:
            val.setStyleSheet("background: transparent;")
        layout.addWidget(lbl, row, 0)
        layout.addWidget(val, row, 1)

    _SUBSET_RE = re.compile(r'^[A-Z]{6}\+(.+)')

    def _add_font_item(self, raw_name):
        m = self._SUBSET_RE.match(raw_name)
        clean_name = m.group(1) if m else raw_name
        is_subset = m is not None
        item = QListWidgetItem()
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(4, 1, 4, 1)
        layout.setSpacing(4)
        name_label = QLabel(clean_name)
        name_label.setStyleSheet("background: transparent;")
        layout.addWidget(name_label)
        layout.addStretch()
        if is_subset:
            subset_label = QLabel("subset")
            subset_label.setStyleSheet(
                "font-size: 7pt; background: transparent;")
            layout.addWidget(subset_label)
        item.setSizeHint(QSize(0, 22))
        self.font_list.addItem(item)
        self.font_list.setItemWidget(item, widget)

    def _update_info(self):
        if not self.render.doc:
            self._clear_grid(self.boxes_grid_layout)
            self._add_row(self.boxes_grid_layout, 0, "Page", "No page")
            self.l_tac.setText("—")
            self._clear_grid(self.cs_grid_layout)
            self._add_row(self.cs_grid_layout, 0, "Color Space", "—")
            self.font_list.clear()
            self._clear_grid(self.sec_grid_layout)
            self._add_row(self.sec_grid_layout, 0, "Print", "—")
            return

        doc = self.render.doc
        page = doc[self._current_page]

        # Boxes + page size with eye icons and diff
        boxes = self.render.get_page_boxes(page)
        self._clear_grid(self.boxes_grid_layout)
        self._box_eye_btns.clear()
        row = 0
        order = ['media', 'art', 'bleed', 'trim']
        media_box = boxes.get('media')
        for name in order:
            box = boxes.get(name)
            if box and box.x0 < box.x1 and box.y0 < box.y1:
                fmt = self._format_box(box)

                has_eye = name in CROP_PREVIEW_BOXES
                if has_eye:
                    eye_btn = QPushButton()
                    eye_btn.setCheckable(True)
                    eye_btn.setFixedWidth(24)
                    eye_btn.setFixedHeight(24)
                    eye_btn.setIcon(self._icon('crop'))
                    eye_btn.setIconSize(QSize(18, 18))
                    eye_btn.setToolTip(f"Crop to {name} box")
                    eye_btn.setStyleSheet(
                        f"QPushButton {{ border: none; border-radius: 3px; padding: 0px; }}"
                        f"QPushButton:hover {{ background: rgba(128,128,128,0.2); }}"
                        f"QPushButton:checked {{ border: none; background: transparent; }}")
                    eye_btn._hover_connected = True
                    eye_btn.enterEvent = lambda e, b=eye_btn: b.setIcon(self._icon('crop', color=self._accent_color))
                    eye_btn.leaveEvent = lambda e, b=eye_btn: b.setIcon(
                        self._icon('crop', color=self._accent_color if b.isChecked() else None))
                    eye_btn.toggled.connect(
                        lambda checked, n=name, btn=eye_btn: (
                            self._set_box_mask(n, checked),
                            btn.setIcon(self._icon('crop', color=self._accent_color if checked else None))
                        )[-1])
                    self._box_eye_btns[name] = eye_btn
                    self.boxes_grid_layout.addWidget(eye_btn, row, 0)

                toggle_btn = QPushButton(f"{name.title()} box" if name != 'media' else "Media box")
                toggle_btn.setCheckable(True)
                toggle_btn.setFixedWidth(96)
                toggle_btn.setFixedHeight(30)
                toggle_btn._box_name = name
                self._box_btns[name] = toggle_btn
                toggle_btn.setStyleSheet(
                    f"QPushButton {{ border: 1px solid {self._muted_color}; "
                    f"border-radius: 3px; padding: 2px 4px; color: {self._muted_color}; font-weight: normal; font-size: 9pt; }}"
                    f"QPushButton:hover {{ border: 1px solid {self._accent_color}; color: {self._accent_color}; }}"
                    f"QPushButton:checked {{ background: transparent; border: 1px solid {self._accent_color}; "
                    f"color: {self._accent_color}; }}")
                toggle_btn.toggled.connect(lambda v, n=name: self._toggle_box(n, v))
                self.boxes_grid_layout.addWidget(toggle_btn, row, 1)

                if media_box and name != 'media':
                    dw = box.width - media_box.width
                    dh = box.height - media_box.height
                    if dw != 0 or dh != 0:
                        dfmt = self._format_diff(dw, dh)
                        fmt = f"{fmt}  {dfmt}"
                val = QLabel(fmt)
                val.setStyleSheet("background: transparent;")
                self.boxes_grid_layout.addWidget(val, row, 2, 1, 2)
                row += 1

        # TAC
        if self._overview_active:
            # Resolve TAC on the thumbnail worker thread (no fitz on GUI thread
            # while the worker is rendering — that crashes MuPDF). The result
            # arrives via _on_tac_ready.
            self._thumb_worker.submit_tac(
                self._current_path, self._current_page, 0.3)
        else:
            try:
                tac = self.analyzer.calculate_tac(
                    self._current_page, zoom=0.3)
                self.l_tac.setText(
                    f"Max: {tac['max']:.1f}%\n"
                    f"Avg: {tac['avg']:.1f}%\n"
                    f">300%: {tac['over_limit_pixels']} / "
                    f"{tac['total_pixels']} px"
                )
            except Exception:
                self.l_tac.setText("Error")

        # Fonts
        self.font_list.clear()
        if self._font_all_pages.isChecked():
            fonts = self.analyzer.get_all_fonts()
        else:
            fonts = self.analyzer.get_fonts(self._current_page)
        for f in fonts:
            self._add_font_item(f)
        if not fonts:
            no_item = QListWidgetItem("(no fonts)")
            no_item.setSizeHint(QSize(0, 22))
            self.font_list.addItem(no_item)
        visible = min(self.font_list.count(), 4) if self.font_list.count() else 1
        self.font_list.setFixedHeight(visible * 22 + 2)

        # Color spaces
        try:
            ci = self.analyzer.get_color_info()
            self._clear_grid(self.cs_grid_layout)
            row = 0
            cs_lbl = QLabel("Color Space")
            cs_lbl.setStyleSheet("font-weight: bold; background: transparent;")
            self.cs_grid_layout.addWidget(cs_lbl, row, 0)
            cs_widget = QWidget()
            cs_row = QHBoxLayout(cs_widget)
            cs_row.setContentsMargins(0, 0, 0, 0)
            cs_row.setSpacing(4)
            cs_colors = {'RGB': '#f44336', 'CMYK': '#4caf50'}
            for cs in sorted(ci['color_spaces']):
                cs_tag = QLabel(cs)
                color = cs_colors.get(cs)
                if color:
                    cs_tag.setStyleSheet(f"color: {color}; background: transparent; font-weight: bold;")
                else:
                    cs_tag.setStyleSheet("background: transparent;")
                cs_row.addWidget(cs_tag)
            cs_row.addStretch()
            self.cs_grid_layout.addWidget(cs_widget, row, 1)
            row += 1
            # Page has overprint?
            try:
                from preview.overprint import build_overprint_position_map
                op_map = build_overprint_position_map(doc, page)
                if op_map:
                    fill_count = sum(1 for e in op_map if e['op_fill'])
                    stroke_count = sum(1 for e in op_map if e['op_stroke'])
                    parts = []
                    if fill_count:
                        parts.append(f"{fill_count} fill")
                    if stroke_count:
                        parts.append(f"{stroke_count} stroke")
                    self._add_row(self.cs_grid_layout, row, "Overprint",
                                  "Yes — " + ", ".join(parts), "#ffaa00")
                else:
                    self._add_row(self.cs_grid_layout, row, "Overprint", "No", "#4caf50")
                row += 1
            except Exception:
                pass
            if ci.get('output_intent'):
                val = re.sub(r'\s*GTS_PDFX\d*\s*[/\\]\s*', '', ci['output_intent'])
                val = re.sub(r'\s*GTS_PDFX\d*', '', val).strip('/\\ \t\n\r')
                if val:
                    self._add_row(self.cs_grid_layout, row, "Output Color Intent", val)
                    row += 1
            level, ok = ci.get('pdfx_status', ('n/a', False))
            if ok and level not in ('n/a', None, ''):
                self._add_row(self.cs_grid_layout, row, "PDF/X", level, "#4caf50")
            else:
                extra = f" ({level})" if level and level not in ('n/a', 'Not PDF/X') else ""
                self._add_row(self.cs_grid_layout, row, "PDF/X", "Not PDF/X" + extra, "#f44336")
        except Exception:
            self._clear_grid(self.cs_grid_layout)
            self._add_row(self.cs_grid_layout, 0, "Color Space", "—")
        row = self.cs_grid_layout.rowCount()

        # PDF filename at bottom
        if self._current_path:
            fname_label = QLabel("File name")
            fname_label.setStyleSheet("color: #fff; background: transparent; font-weight: bold; font-size: 9pt;")
            self.cs_grid_layout.addWidget(fname_label, row, 0)
            fname = os.path.basename(self._current_path)
            fname_val = QLabel(fname)
            fname_val.setStyleSheet(
                "color: #fff; background: transparent; font-weight: bold; font-size: 9pt; text-decoration: underline;")
            fname_val.setToolTip(self._current_path)
            fname_val.setCursor(Qt.CursorShape.PointingHandCursor)
            def _open_folder(e, p=self._current_path):
                from PyQt6.QtCore import Qt
                if e.button() == Qt.MouseButton.LeftButton:
                    try:
                        os.startfile(os.path.dirname(p))
                    except Exception:
                        pass
            fname_val.mousePressEvent = _open_folder
            fname_val.enterEvent = lambda e, lbl=fname_val: lbl.setStyleSheet(
                f"color: {self._accent_color}; background: transparent; font-weight: bold; font-size: 9pt; text-decoration: underline;")
            fname_val.leaveEvent = lambda e, lbl=fname_val: lbl.setStyleSheet(
                "color: #fff; background: transparent; font-weight: bold; font-size: 9pt; text-decoration: underline;")
            self.cs_grid_layout.addWidget(fname_val, row, 1)

        # Security
        try:
            sec = self.analyzer.get_security_info()
            self._clear_grid(self.sec_grid_layout)
            row = 0
            for s in sec:
                if ': Y' in s:
                    label, _ = s.split(': Y', 1)
                    value = "✓ allowed"
                    color = "#4caf50"
                elif ': N' in s:
                    label, _ = s.split(': N', 1)
                    value = "✗ denied"
                    color = "#f44336"
                else:
                    label = s
                    value = ""
                    color = None
                self._add_row(self.sec_grid_layout, row, label.strip(), value, color)
                row += 1
        except Exception:
            self._clear_grid(self.sec_grid_layout)
            self._add_row(self.sec_grid_layout, 0, "Print", "—")

        # Spot colors
        spots = self.analyzer.get_spot_colors() if hasattr(self.analyzer, 'get_spot_colors') else []
        while self._spot_layout.count():
            w = self._spot_layout.takeAt(0).widget()
            if w:
                w.deleteLater()
        for spot in spots[:8]:
            btn = QPushButton(spot)
            btn.setCheckable(True)
            btn.setChecked(True)
            btn.setFixedHeight(24)
            btn.setStyleSheet(
                "QPushButton { text-align: center; padding: 2px 6px; font-size: 8pt; }")
            self._spot_layout.addWidget(btn)
        self._spot_container.setVisible(len(spots) > 0)

    def _fonts_to_curves(self):
        if not self.render.doc:
            return
        try:
            from PyQt6.QtWidgets import QProgressDialog
            pages = self.render.doc.page_count
            prog = QProgressDialog(
                "Converting fonts to curves...", None, 0, pages, self)
            prog.setWindowTitle("Font to Curves")
            for i in range(pages):
                if prog.wasCanceled():
                    break
                prog.setValue(i)
                page = self.render.doc[i]
                page.clean_contents()
            prog.setValue(pages)
            self.font_list.clear()
            if self._font_all_pages.isChecked():
                fonts = self.analyzer.get_all_fonts()
            else:
                fonts = self.analyzer.get_fonts(self._current_page)
            if fonts:
                for f in fonts:
                    name = f[3] if len(f) > 3 else str(f[0])
                    self._add_font_item(name)
            else:
                self.font_list.addItem("(all fonts converted to curves)")
            QMessageBox.information(
                self, "Done",
                "Fonts converted to curves.\n"
                "Save the PDF to keep changes.")
        except Exception as e:
            QMessageBox.critical(self, "Error",
                                 f"Font conversion failed:\n{e}")

    def closeEvent(self, event):
        self._render_cancel.set()
        self._render_seq += 1
        self._current_path = None
        self._thumb_gen += 1
        if hasattr(self, '_thumb_worker'):
            self._thumb_worker.stop()
        if hasattr(self, '_render_debounce'):
            self._render_debounce.stop()
        if self._info_update_timer is not None:
            self._info_update_timer.stop()
        if self._magnifier_cache_timer is not None:
            self._magnifier_cache_timer.stop()
        self._save_collapse_states()
        self._settings.setValue("window/geometry", self.saveGeometry())
        self._settings.setValue("window/state", self.saveState())
        self._settings.sync()
        self.analyzer.close()
        self.render.close()
        _close_render_doc_cache()
        super().closeEvent(event)

    def _cancel_active_render(self):
        self._draft_seq += 1
        self._mag_cancel.set()
        self._render_cancel.set()
        self._render_seq += 1
        self._render_cancel = threading.Event()
        self._detail_seq += 1
        self._detail_cancel.set()

    def _join_background_fitz_threads(self):
        """Cancel all pending background fitz work.

        MuPDF operations must run on a SINGLE thread (the FitzThumbWorker).
        When entering overview, all pending page/mag/prefetch tasks must be
        cancelled so the worker only handles thumb/TAC tasks.
        """
        self._mag_cancel.set()
        if self._magnifier_cache_timer is not None:
            self._magnifier_cache_timer.stop()
        self._thumb_worker.clear_pending()
        self._page_prefetch_threads.clear()

    def _apply_live_zoom_transform(self):
        if (self._last_render_zoom <= 0
                or self.page_widget.pixmap_item is None
                or self.page_widget.pixmap_item.scene() is None):
            return

        pdf_center = None
        vp = self.page_widget.viewport().rect()
        if vp.width() > 0 and vp.height() > 0:
            sc = self.page_widget.mapToScene(vp.center())
            pdf_center = QPointF(
                sc.x() / self._last_render_zoom,
                sc.y() / self._last_render_zoom)

        self._detail_seq += 1
        self._detail_cancel.set()
        self.page_widget.clear_detail_overlay()
        self._suppress_pan_detail = True
        self.page_widget.set_interactive_transform_quality(True)
        self.page_widget.resetTransform()
        scale = self._zoom / self._last_render_zoom
        if abs(scale - 1.0) > 0.001:
            self.page_widget.scale(scale, scale)
        if pdf_center is not None:
            self.page_widget.centerOn(
                QPointF(pdf_center.x() * self._last_render_zoom,
                        pdf_center.y() * self._last_render_zoom))
        self.page_widget.viewport().update()
        self._suppress_pan_detail = False

    def _schedule_info_update(self, delay_ms=None):
        if delay_ms is None:
            delay_ms = self._INFO_UPDATE_DELAY_MS
        if self._info_update_timer is None:
            self._info_update_timer = QTimer(self)
            self._info_update_timer.setSingleShot(True)
            self._info_update_timer.timeout.connect(self._update_info)
        self._info_update_timer.start(delay_ms)

    def _schedule_magnifier_cache_build(self):
        if not self._current_path or not self.render.doc:
            return
        enabled = str(self._settings.value(
            "ui/magnifier_enabled", "true")).lower() == "true"
        if not enabled:
            return
        if self._magnifier_cache_timer is None:
            self._magnifier_cache_timer = QTimer(self)
            self._magnifier_cache_timer.setSingleShot(True)
            self._magnifier_cache_timer.timeout.connect(
                self._start_magnifier_cache_build)
        self._mag_cancel.clear()
        self._magnifier_cache_timer.start(800)

    def _cache_clear(self):
        with self._page_cache_lock:
            self._page_cache.clear()

    def _display_render_result(self, rgb_arr, cmyk_buf, boxes, zoom, has_op, page_rect, pg2_info=None):
        old_pdf_center = None
        # When the render scale (and thus the pixmap size) is unchanged, the
        # view must stay pixel-identical -- so capture the exact scroll position
        # and restore it. Re-deriving the center from the (integer) viewport
        # center and re-applying centerOn drifts by ~1px per render because the
        # view is scaled, so 1 widget pixel maps to several scene pixels and the
        # rounding error accumulates every re-render (visible as a slow shift
        # when toggling separation / overprint buttons).
        preserve_scroll = False
        saved_sx = saved_sy = 0
        if (self._last_render_zoom > 0
                and self.page_widget.pixmap_item is not None
                and abs(zoom - self._last_render_zoom) < 0.001):
            sb = self.page_widget.horizontalScrollBar()
            vb = self.page_widget.verticalScrollBar()
            saved_sx = sb.value()
            saved_sy = vb.value()
            preserve_scroll = True
        elif (self._last_render_zoom > 0
                and self.page_widget.pixmap_item is not None):
            vp = self.page_widget.viewport().rect()
            if vp.width() > 0 and vp.height() > 0:
                sc = self.page_widget.mapToScene(vp.center())
                old_pdf_center = QPointF(
                    sc.x() / self._last_render_zoom,
                    sc.y() / self._last_render_zoom)
        h, w = rgb_arr.shape[:2]
        qimg = QImage(rgb_arr.data, w, h, w * 3,
                      QImage.Format.Format_RGB888)
        qimg = qimg.copy()
        if pg2_info is not None:
            proxy = _PageProxy(page_rect, boxes, pg2_info['rect'],
                              pg2_info['boxes'], rect0=pg2_info.get('rect0'),
                              gap=pg2_info.get('gap', 0))
        else:
            proxy = _PageProxy(page_rect, boxes)
        self._cmyk_buf = cmyk_buf
        self._overprint_on_page = has_op
        self.page_widget.resetTransform()
        if abs(self._zoom - zoom) > 0.001:
            self.page_widget.scale(self._zoom / zoom, self._zoom / zoom)
        self.page_widget.set_pixmap(qimg, proxy, zoom,
                                    overprint_active=has_op,
                                    cmyk_buf=cmyk_buf)
        if self._center_on_page:
            self._center_on_page = False
            self.page_widget.centerOn(w / 2, h / 2)
        elif preserve_scroll:
            self.page_widget.horizontalScrollBar().setValue(saved_sx)
            self.page_widget.verticalScrollBar().setValue(saved_sy)
        elif old_pdf_center is not None:
            self.page_widget.centerOn(
                QPointF(old_pdf_center.x() * zoom,
                        old_pdf_center.y() * zoom))
        elif self._last_render_zoom == 0:
            self.page_widget.centerOn(w / 2, h / 2)
        self._last_render_zoom = zoom
        self._schedule_magnifier_cache_build()
        self._warm_caches()
        self._schedule_detail_render()

    def _schedule_detail_render(self):
        """Debounced request for the high-resolution detail overlay tile."""
        self._detail_seq += 1
        self._detail_cancel.set()
        if not self._detail_needed():
            self.page_widget.clear_detail_overlay()
            return
        if self._detail_timer is None:
            self._detail_timer = QTimer(self)
            self._detail_timer.setSingleShot(True)
            self._detail_timer.timeout.connect(self._do_detail_render)
        self._detail_timer.start(self._DETAIL_RENDER_DELAY_MS)

    def _detail_needed(self):
        if self._overview_active or not self._current_path or not self.render.doc:
            return False
        if self._spread_mode() is not None:
            return False
        if self.page_widget.pixmap_item is None:
            return False
        if self._last_render_zoom <= 0:
            return False
        # Only enable the clipped high-res tile in modes where rendering a clip
        # is pixel-identical to the full-page render. Overprint simulation maps
        # object geometry from the page origin, so a clip would be misaligned —
        # fall back to the (blurrier) upscale in those modes to keep colors exact.
        if self._mode == 'overprint':
            return False
        if self._mode == 'separation' and self.chk_simulate_overprint.isChecked():
            return False
        return self._zoom > self._last_render_zoom * 1.01

    def _do_detail_render(self):
        if not self._detail_needed():
            self.page_widget.clear_detail_overlay()
            return
        base_zoom = self._last_render_zoom
        try:
            page_rect = self.render.doc[self._current_page].rect
        except Exception:
            return
        pw, ph = page_rect.width, page_rect.height

        vp = self.page_widget.viewport().rect()
        if vp.width() <= 0 or vp.height() <= 0:
            return
        tl = self.page_widget.mapToScene(vp.topLeft())
        br = self.page_widget.mapToScene(vp.bottomRight())
        sx0, sx1 = min(tl.x(), br.x()), max(tl.x(), br.x())
        sy0, sy1 = min(tl.y(), br.y()), max(tl.y(), br.y())

        cx0 = sx0 / base_zoom
        cy0 = sy0 / base_zoom
        cx1 = sx1 / base_zoom
        cy1 = sy1 / base_zoom
        mx = (cx1 - cx0) * 0.08
        my = (cy1 - cy0) * 0.08
        cx0 = max(0.0, cx0 - mx)
        cy0 = max(0.0, cy0 - my)
        cx1 = min(pw, cx1 + mx)
        cy1 = min(ph, cy1 + my)
        # Snap the tile origin to the base-render pixel grid so the sharp tile
        # aligns exactly with the underlying pixmap (avoids a sub-pixel drift
        # that, combined with the tile scale, reads as a 1px shift).
        cx0 = round(cx0 * base_zoom) / base_zoom
        cy0 = round(cy0 * base_zoom) / base_zoom
        if cx1 - cx0 < 1.0 or cy1 - cy0 < 1.0:
            self.page_widget.clear_detail_overlay()
            return

        detail_zoom = min(self._zoom, self._DETAIL_MAX_ZOOM)
        channels = {
            'cyan': self.chk_c.isChecked(),
            'magenta': self.chk_m.isChecked(),
            'yellow': self.chk_y.isChecked(),
            'black': self.chk_k.isChecked(),
        }
        icc_path = get_cmyk_icc_path(self.render.doc)
        sim_profile = self.simulation.get_active_profile_path()
        simulate_op = self.chk_simulate_overprint.isChecked()
        object_filter = self._object_filter_state()
        self._detail_seq += 1
        self._detail_cancel = threading.Event()
        self._thumb_worker.submit_detail(
            self._current_path, self._current_page, detail_zoom,
            (cx0, cy0, cx1, cy1), self._mode, channels, icc_path,
            sim_profile, simulate_op, self._detail_cancel, self._detail_seq,
            object_filter)

    def _on_detail_ready(self, rgb_arr, clip, detail_zoom, seq):
        if seq != self._detail_seq or self._overview_active:
            return
        if self.page_widget.pixmap_item is None or self._last_render_zoom <= 0:
            return
        h, w = rgb_arr.shape[:2]
        qimg = QImage(rgb_arr.data, w, h, w * 3,
                      QImage.Format.Format_RGB888).copy()
        base_zoom = self._last_render_zoom
        cx0, cy0, cx1, cy1 = clip
        # Use the tile's ACTUAL pixel size so the scale matches the clip
        # exactly (a 1px rounding difference in the clip render would
        # otherwise scale the whole tile slightly wrong and shift it).
        item_scale_x = (cx1 - cx0) * base_zoom / w if w else 1.0
        item_scale_y = (cy1 - cy0) * base_zoom / h if h else 1.0
        self.page_widget.set_detail_overlay(
            qimg, cx0 * base_zoom, cy0 * base_zoom,
            item_scale_x, item_scale_y)

    def _warm_caches(self):
        """Pre-warm content stream and overprint caches in background."""
        doc = self.render.doc
        if doc is None:
            return
        page_num = self._current_page

        def _warm():
            try:
                page = doc[page_num]
                from preview.pdf_inspector import _get_or_parse_page
                _get_or_parse_page(doc, page_num)
                from preview.overprint import build_overprint_position_map
                build_overprint_position_map(doc, page)
            except Exception:
                pass
        threading.Thread(target=_warm, daemon=True).start()

    def open_file(self, path):
        try:
            count = self.render.open(path)
            self.analyzer.open(path)
            self._current_path = os.path.abspath(path)
            self._current_page = 0
            self._overview_active = False
            self._overview_entries = []
            self._thumb_gen += 1
            self.act_save.setVisible(True)
            self.act_close.setVisible(True)
            self.spin_page.setMaximum(count)
            self._update_page_spin_width(count)
            self.lbl_page_total.setText(f"/ {count}")
            multi = count > 1
            self.act_first.setEnabled(multi)
            self.act_prev.setEnabled(multi)
            self.act_next.setEnabled(multi)
            self.act_last.setEnabled(multi)
            self.spin_page.setEnabled(multi)
            self.spin_page.blockSignals(True)
            self.spin_page.setValue(1)
            self.spin_page.blockSignals(False)
            self.setWindowTitle(
                f"PDF Preflight Viewer — {os.path.basename(path)}")
            self._add_recent(path)
            profiles = self.simulation.get_embedded_profiles(self.render.doc) if hasattr(self.simulation, 'get_embedded_profiles') else []
            selected = False
            if profiles:
                for name, prof_path in profiles:
                    if os.path.isfile(prof_path):
                        self.simulation.set_simulation_profile(prof_path)
                        selected = True
                        break
            if not selected:
                self.simulation.clear_simulation_profile()
            self._update_info()
            self._zoom_fit()
        except Exception as e:
            QMessageBox.critical(self, "Error",
                                 f"Failed to open PDF:\n{e}")

    def _open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open PDF", "",
            "PDF files (*.pdf);;All files (*.*)")
        if path:
            self.open_file(path)

    def _first_page(self):
        if self.render.doc and self._current_page != 0:
            if self._overview_active:
                self._overview_goto_page(0)
            else:
                self._go_to_page(1)

    def _last_page(self):
        if self.render.doc:
            if self._overview_active:
                self._overview_goto_page(self.render.page_count - 1)
            elif self._spread_mode() == 'offset':
                self._go_to_page(self.render.page_count - 1)
            else:
                self._go_to_page(self.render.page_count)

    def _prev_page(self):
        mode = self._spread_mode()
        step = 2 if mode in ('normal', 'offset') else 1
        if self._overview_active:
            target = max(0, self._current_page - step)
            self._overview_goto_page(target)
            return
        if self._current_page >= step:
            self._current_page -= step
            self.spin_page.setValue(self._current_page + 1)

    def _next_page(self):
        mode = self._spread_mode()
        step = 2 if mode in ('normal', 'offset') else 1
        if self._overview_active:
            target = min(self.render.page_count - 1,
                         self._current_page + step)
            self._overview_goto_page(target)
            return
        if self._current_page + step < self.render.page_count:
            self._current_page += step
            self.spin_page.setValue(self._current_page + 1)

    def _go_to_page(self, num):
        if self._overview_active:
            self._overview_goto_page(num - 1)
            return
        self._current_page = num - 1
        self.spin_page.setValue(num)
        self._schedule_info_update()
        self._start_bg_render(debounce_ms=20)

    def _set_zoom(self, z):
        if not self.render.doc:
            return
        new_zoom = max(0.1, min(20.0, z))
        if abs(new_zoom - self._zoom) < 0.0001:
            return
        count = self.render.page_count
        multi = count > 1
        was_overview = self._overview_active
        fit_zoom = self._compute_fit_zoom(self._current_page) if multi else new_zoom
        entering = (not was_overview) and multi and new_zoom < fit_zoom
        exiting = was_overview and (not multi or new_zoom >= fit_zoom)

        self._zoom = new_zoom
        self._is_fit_viewport = False
        self.lbl_zoom.setText(f"{self._zoom*100:.0f}%")
        self._cancel_active_render()

        if exiting:
            target = self._overview_current
            self._exit_overview(target)
            self._apply_live_zoom_transform()
            self._start_bg_render(debounce_ms=self._ZOOM_RENDER_DELAY_MS)
        elif entering:
            self._overview_center_lock = True
            self._enter_overview()
            self._update_overview_zoom()
            # Keep the lock until the next event-loop tick so any
            # (synchronously or queued) scroll signals emitted by the
            # programmatic centerOn() above are suppressed. Clearing it
            # here (synchronously) is too early when the signal is queued,
            # which would let _track_overview_center reset to the cover page.
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, self._release_overview_center_lock)
        elif was_overview:
            self._update_overview_zoom()
        else:
            self._apply_live_zoom_transform()
            self._start_bg_render(debounce_ms=self._ZOOM_RENDER_DELAY_MS)

    def _zoom_fit(self):
        if not self.render.doc:
            return
        if self._overview_active:
            self._exit_overview(self._overview_current)
        # If already fit-to-viewport, toggle to dbl-click zoom
        if self._is_fit_viewport and self._last_render_zoom > 0:
            dbl_zoom = max(0.1, min(20.0,
                           int(self._settings.value("ui/dbl_click_zoom", 200)) / 100.0))
            self._is_fit_viewport = False
            self._zoom = dbl_zoom
            self.lbl_zoom.setText(f"{self._zoom*100:.0f}%")
            self._cancel_active_render()
            self._apply_live_zoom_transform()
            self._start_bg_render(debounce_ms=self._ZOOM_RENDER_DELAY_MS)
            return

        page = self.render.doc[self._current_page]
        pw, ph = page.rect.width, page.rect.height
        w = self.page_widget.width()
        h = self.page_widget.height()
        self._is_fit_viewport = True
        self._zoom = max(0.1, min(20.0,
                         min((w - 4) / pw, (h - 4) / ph) * 0.95))
        self.lbl_zoom.setText(f"{self._zoom*100:.0f}%")
        self._center_on_page = True
        self._cancel_active_render()
        self._apply_live_zoom_transform()
        self._start_bg_render(debounce_ms=0)

    def _set_view_single(self):
        self.act_single.setChecked(True)
        self.act_spread.setChecked(False)
        self.act_spread_offset.setChecked(False)
        if self._overview_active:
            self._rebuild_overview_if_active()
            return
        self._cache_clear()
        self._start_bg_render()

    def _fit_spread(self):
        if not self.render.doc or self._overview_active:
            return
        if self._spread_mode() is None:
            return
        render_page, spread, is_offset_single = self._render_plan(self._current_page)
        doc = self.render.doc
        r1 = doc[render_page].rect
        if is_offset_single:
            total_w = r1.width * 2
            total_h = r1.height
        elif spread and render_page + 1 < self.render.page_count:
            r2 = doc[render_page + 1].rect
            total_w = r1.width + r2.width
            total_h = max(r1.height, r2.height)
        else:
            total_w = r1.width
            total_h = r1.height
        if total_w <= 0 or total_h <= 0:
            return
        w = self.page_widget.width()
        h = self.page_widget.height()
        # The spread gutter is rendered as a fixed 20px pixel gap; reserve
        # it so the whole pair stays visible.
        self._is_fit_viewport = True
        self._zoom = max(0.1, min(20.0,
                         min((w - 4 - 20) / total_w, (h - 4) / total_h) * 0.95))
        self.lbl_zoom.setText(f"{self._zoom*100:.0f}%")
        self._center_on_page = True
        self._cancel_active_render()
        self._apply_live_zoom_transform()
        self._start_bg_render(debounce_ms=0)

    def _set_view_spread(self):
        self.act_single.setChecked(False)
        self.act_spread.setChecked(True)
        self.act_spread_offset.setChecked(False)
        if self._overview_active:
            self._rebuild_overview_if_active()
            return
        self._cache_clear()
        self._fit_spread()

    def _set_view_spread_offset(self):
        self.act_single.setChecked(False)
        self.act_spread.setChecked(False)
        self.act_spread_offset.setChecked(True)
        if self._overview_active:
            self._rebuild_overview_if_active()
            return
        self._cache_clear()
        self._fit_spread()

    def _on_wheel(self, direction):
        if direction > 0:
            self._set_zoom(self._zoom * 1.15)
        else:
            self._set_zoom(self._zoom / 1.15)

    def _on_page_nav(self, direction):
        if not hasattr(self, '_nav_accumulator'):
            self._nav_accumulator = 0
        self._nav_accumulator += direction

        if hasattr(self, '_nav_debounce') and self._nav_debounce.isActive():
            self._nav_debounce.start(50)
            return

        from PyQt6.QtCore import QTimer
        self._nav_debounce = QTimer(self)
        self._nav_debounce.setSingleShot(True)
        self._nav_debounce.timeout.connect(self._nav_flush)
        self._nav_debounce.start(50)

    def _nav_flush(self):
        acc = getattr(self, '_nav_accumulator', 0)
        self._nav_accumulator = 0
        if acc == 0:
            return
        if self._overview_active:
            mode = self._spread_mode()
            step = 2 if mode in ('normal', 'offset') else 1
            target = max(0, min(self._current_page + acc * step,
                                self.render.page_count - 1))
            self._overview_goto_page(target)
            return
        mode = self._spread_mode()
        step = 2 if mode in ('normal', 'offset') else 1
        target = self._current_page + acc * step
        target = max(0, min(target, self.render.page_count - 1))
        if target != self._current_page:
            self._current_page = target
            self.spin_page.blockSignals(True)
            self.spin_page.setValue(self._current_page + 1)
            self.spin_page.blockSignals(False)
            self._schedule_info_update()
            self._start_bg_render(debounce_ms=20)

    def _render_page(self):
        if self.render.doc:
            self._schedule_info_update()
            self._start_bg_render()

    def _toggle_separation(self, on):
        if on:
            self._mode = "separation"
        else:
            self._mode = "normal"
        if self._overview_active:
            self._invalidate_thumb_cache()
        else:
            self._start_bg_render()

    def _on_sep_btn_style(self, checked):
        btn = self.sender()
        color, _ = self._sep_btn_style.get(btn._sep_key, ("#888", "#888"))
        muted = self._muted_color
        if checked:
            btn.setStyleSheet(
                f"QPushButton {{ border: 1px solid {color}; border-left: 4px solid {color}; "
                f"text-align: center; padding: 4px 8px; color: #fff; font-weight: normal; }}")
        else:
            btn.setStyleSheet(
                f"QPushButton {{ border: 1px solid {muted}; border-left: 3px solid {color}; "
                f"text-align: center; padding: 4px 8px; color: {muted}; font-weight: normal; }}")

    def _on_sep_changed(self):
        all_on = all([
            self.chk_c.isChecked(),
            self.chk_m.isChecked(),
            self.chk_y.isChecked(),
            self.chk_k.isChecked(),
        ])
        if not all_on:
            self._mode = "separation"
        elif self._mode == "separation":
            self._mode = "normal"
        self._cache_clear()
        if self._overview_active:
            self._invalidate_thumb_cache()
        else:
            self._start_bg_render()

    def _on_overprint_sim_toggled(self):
        self._cache_clear()
        if self._overview_active:
            self._invalidate_thumb_cache()
        elif self._mode == "separation":
            self._start_bg_render()

    # ---- Object Filter state & callbacks ---------------------------------

    def _object_filter_state(self):
        """Return dict of enabled categories, or None if nothing hidden."""
        enabled = {}
        for key, cb in self._of_rows.items():
            enabled[key] = cb.isChecked()
        if all(enabled.values()):
            return None
        return enabled

    def _object_filter_key(self):
        state = self._object_filter_state()
        if state is None:
            return None
        return frozenset(state.items())

    def _on_of_toggle(self, key, checked):
        if not self._of_batch_update:
            self._of_update_reset_visibility()
            self._on_of_changed()

    def _on_of_reset(self):
        """Reset all categories to default (all on)."""
        self._of_batch_update = True
        for cb in self._of_rows.values():
            cb.setChecked(True)
            cb.setEnabled(True)
        self._of_batch_update = False
        self._of_update_reset_visibility()
        self._on_of_changed()

    def _of_is_default(self):
        return not any(not cb.isChecked() for cb in self._of_rows.values())

    def _of_update_reset_visibility(self):
        self._of_reset_btn.setEnabled(not self._of_is_default())

    def _on_of_changed(self):
        from preview.object_filter import clear_cache as of_clear_cache
        of_clear_cache()
        self._cache_clear()
        if self._overview_active:
            self._invalidate_thumb_cache()
        else:
            self._start_bg_render()

    def _update_of_styles(self):
        """Update filter checkbox styles on theme change."""
        from preview.object_filter import LABELS
        for key, cb in self._of_rows.items():
            cb.setText(LABELS[key].lower())
            cb.setStyleSheet(
                "QCheckBox { font-size: 9pt; color: #fff; }"
                "QCheckBox::indicator { width: 14px; height: 14px; }")

    # ===================== Overview (zoom-driven) =====================
    def _page_dims_for_fit(self, page_num):
        render_page, spread, is_offset_single = self._render_plan(page_num)
        page = self.render.doc[render_page]
        pw, ph = page.rect.width, page.rect.height
        if spread and render_page + 1 < self.render.page_count:
            p2 = self.render.doc[render_page + 1]
            pw = pw + p2.rect.width
            ph = max(ph, p2.rect.height)
        elif is_offset_single:
            pw = pw * 2
        return pw, ph

    def _compute_fit_zoom(self, page_num):
        if not self.render.doc:
            return 1.0
        w = self.page_widget.width()
        h = self.page_widget.height()
        pw, ph = self._page_dims_for_fit(page_num)
        if pw <= 0 or ph <= 0:
            return 1.0
        return max(0.1, min(20.0, min((w - 4) / pw, (h - 4) / ph) * 0.95))

    def _build_overview_entries(self, ov_scale, mode):
        doc = self.render.doc
        count = doc.page_count
        gap = 16 * ov_scale
        inner_gap = 20 * ov_scale
        if mode == 'offset':
            rows = []
            if count >= 1:
                rows.append(('single_right', 0))
            i = 1
            while i < count - 1:
                rows.append(('pair', i, i + 1))
                i += 2
            if i < count:
                rows.append(('single_left', i))
        elif mode == 'normal':
            rows = []
            i = 0
            while i < count - 1:
                rows.append(('pair', i, i + 1))
                i += 2
            if i < count:
                rows.append(('single', i))
        else:
            rows = [('single', i) for i in range(count)]

        entries = []
        y = 0.0
        max_w = 0.0
        for row in rows:
            kind = row[0]
            if kind == 'single':
                pn = row[1]
                pw = doc[pn].rect.width * ov_scale
                ph = doc[pn].rect.height * ov_scale
                row_w = pw
                x0 = -row_w / 2.0
                entries.append({'pages': (pn,), 'rect': QRectF(x0, y, pw, ph)})
                max_h = ph
            elif kind == 'single_right':
                pn = row[1]
                pw = doc[pn].rect.width * ov_scale
                ph = doc[pn].rect.height * ov_scale
                row_w = pw * 2
                x0 = -row_w / 2.0
                entries.append({'pages': (pn,),
                                'rect': QRectF(x0 + pw, y, pw, ph)})
                max_h = ph
            elif kind == 'single_left':
                pn = row[1]
                pw = doc[pn].rect.width * ov_scale
                ph = doc[pn].rect.height * ov_scale
                row_w = pw * 2
                x0 = -row_w / 2.0
                entries.append({'pages': (pn,),
                                'rect': QRectF(x0, y, pw, ph)})
                max_h = ph
            elif kind == 'pair':
                p1, p2 = row[1], row[2]
                pw1 = doc[p1].rect.width * ov_scale
                ph1 = doc[p1].rect.height * ov_scale
                pw2 = doc[p2].rect.width * ov_scale
                ph2 = doc[p2].rect.height * ov_scale
                row_w = pw1 + inner_gap + pw2
                x0 = -row_w / 2.0
                r1 = QRectF(x0, y, pw1, ph1)
                r2 = QRectF(x0 + pw1 + inner_gap, y, pw2, ph2)
                entries.append({'pages': (p1, p2), 'rect': r1,
                                'rect2': r2, 'is_pair': True})
                max_h = max(ph1, ph2)
            else:
                continue
            max_w = max(max_w, row_w)
            y += max_h + gap
        total_h = y - gap if y > 0 else 0
        # Reserve horizontal room for the page-number labels drawn beside the
        # pages (outside), so they aren't clipped when the view fits to width.
        from PyQt6.QtGui import QFont, QFontMetricsF
        scale = (self._zoom / ov_scale) if ov_scale > 0 else 1.0
        lf = QFont("sans-serif")
        lf.setPixelSize(max(1, int(13 / scale)))
        lf.setBold(True)
        fm = QFontMetricsF(lf)
        label_gutter = fm.horizontalAdvance(str(count)) + 20.0 / scale
        scene_rect = QRectF(-(max_w / 2.0 + label_gutter), 0.0,
                            max_w + 2.0 * label_gutter, total_h)
        return entries, scene_rect

    def _enter_overview(self, rebuild=False):
        count = self.render.page_count
        if count <= 1:
            return
        if not rebuild:
            fit = self._compute_fit_zoom(self._current_page)
            self._overview_ov_scale = max(0.3, min(fit, 1.5))
        mode = self._spread_mode()
        entries, scene_rect = self._build_overview_entries(
            self._overview_ov_scale, mode)
        self._overview_entries = entries
        self._overview_active = True
        self._overview_current = self._current_page
        accent = QColor(self._accent_color)
        self.page_widget.enter_overview(
            entries, self._overview_ov_scale, self._current_page,
            scene_rect, accent, self._zoom)
        self._cancel_active_render()
        # MuPDF's global ICC toggle (fitz.TOOLS.set_icc) and get_pixmap are
        # NOT safe across multiple threads — even serialized with a Python
        # lock, concurrent fitz threads crash the process (STATUS_STACK_BUFFER_OVERRUN).
        # During overview, thumbnails render on a dedicated background thread
        # (FitzThumbWorker); all OTHER background fitz threads must be stopped so
        # the thumb worker is the only fitz thread in flight. Join them here.
        self._join_background_fitz_threads()
        self._thumb_worker.clear_pending()
        # NOTE: no fitz may run on the GUI thread while the thumbnail worker is
        # active — get_cmyk_icc_path / calculate_tac are therefore resolved on
        # the worker thread itself (see FitzThumbWorker). The simulation profile
        # path is a plain string, safe to read here.
        self._overview_sim_profile = self.simulation.get_active_profile_path()
        self._open_overview_doc()
        self._request_visible_thumbs()
        self._update_info()

    def _exit_overview(self, target_page):
        self.page_widget.exit_overview()
        self._overview_active = False
        self._overview_entries = []
        self._overview_current = 0
        self._thumb_worker.clear_pending()
        self._close_overview_doc()
        self._current_page = max(0, min(target_page,
                                       self.render.page_count - 1))
        self.spin_page.blockSignals(True)
        self.spin_page.setValue(self._current_page + 1)
        self.spin_page.blockSignals(False)
        self._center_on_page = True
        self._is_fit_viewport = False
        self._update_info()

    def _open_overview_doc(self):
        import fitz
        self._close_overview_doc()
        try:
            self._overview_doc = fitz.open(self._current_path)
        except Exception as e:
            import logging
            logging.warning("Failed to open overview doc: %s", e)
            self._overview_doc = None

    def _close_overview_doc(self):
        if self._overview_doc is not None:
            try:
                self._overview_doc.close()
            except Exception:
                pass
            self._overview_doc = None

    def _on_overview_activate(self, page_index):
        self._exit_overview(page_index)
        self._is_fit_viewport = False
        self._zoom_fit()

    def _on_tac_ready(self, page_index, tac):
        # TAC computed on the worker thread; only apply if still relevant.
        if not self._overview_active or page_index != self._current_page:
            return
        try:
            self.l_tac.setText(
                f"Max: {tac['max']:.1f}%\n"
                f"Avg: {tac['avg']:.1f}%\n"
                f">300%: {tac['over_limit_pixels']} / "
                f"{tac['total_pixels']} px"
            )
        except Exception:
            pass

    def _update_overview_zoom(self):
        self.page_widget.set_overview_zoom(self._zoom)
        self._request_visible_thumbs()
        self._track_overview_center()

    def _request_visible_thumbs(self):
        # Thumbnails are rendered lazily by _pump_thumb_results on the main
        # thread (one per tick) — no background fitz thread, so there is never
        # more than one fitz operation on the GUI thread at a time.
        if not self._overview_active:
            return
        if self._overview_doc is None:
            self._open_overview_doc()
            if self._overview_doc is None:
                return
        visible = self.page_widget.overview_visible_pages()
        if not visible:
            return
        pending = [p for p in visible
                   if p not in self.page_widget._overview_thumbs]
        if pending:
            self._thumb_pending = pending
        else:
            self._thumb_pending = []

    def _pump_thumb_results(self):
        if not self._overview_active:
            return
        pending = getattr(self, '_thumb_pending', None)
        if not pending:
            return
        # Render a single thumbnail per tick (nearest the current page first).
        # The actual fitz render happens on FitzThumbWorker's background thread
        # (shallow stack, no Qt event loop) to avoid MuPDF stack-buffer overruns.
        page_index = min(pending, key=lambda p: abs(p - self._overview_current))
        pending.remove(page_index)
        self._thumb_pending = pending
        if page_index in self.page_widget._overview_thumbs:
            return
        channels = {
            'cyan': self.chk_c.isChecked(),
            'magenta': self.chk_m.isChecked(),
            'yellow': self.chk_y.isChecked(),
            'black': self.chk_k.isChecked(),
        }
        # ICC path is resolved on the worker thread (get_cmyk_icc_path touches
        # the fitz document and must not run on the GUI thread).
        self._thumb_worker.submit_thumb(
            self._current_path, page_index, self._overview_ov_scale,
            self._mode, channels,
            os.path.abspath(self._overview_sim_profile) if self._overview_sim_profile else None,
            self.chk_simulate_overprint.isChecked())

    def _invalidate_thumb_cache(self):
        self._thumb_gen += 1
        if self.page_widget is not None:
            self.page_widget._overview_thumbs.clear()
        self._thumb_worker.clear_pending()
        if self._overview_active:
            self._request_visible_thumbs()

    def _rebuild_overview_if_active(self):
        if not self._overview_active:
            return
        self._enter_overview(rebuild=True)

    def _on_overview_scroll(self, value=None):
        if not self._overview_active:
            if not getattr(self, '_suppress_pan_detail', False):
                self._schedule_detail_render()
            return
        self._request_visible_thumbs()
        if getattr(self, '_overview_center_lock', False):
            return
        self._track_overview_center()

    def _on_overview_resized(self):
        if not self._overview_active:
            return
        self._request_visible_thumbs()
        self._track_overview_center()

    def _release_overview_center_lock(self):
        self._overview_center_lock = False
        # Re-assert centering on the current page. The programmatic centerOn()
        # during enter-overview may not have taken effect yet (layout/transform
        # not finalized), so guarantee we land on the current page (pair).
        if self._overview_active:
            self.page_widget._center_overview_on_page(self._current_page)

    def _track_overview_center(self):
        if getattr(self, '_overview_center_lock', False):
            return
        idx = self.page_widget.overview_centered_page()
        if idx is None or idx == self._overview_current:
            return
        self._overview_current = idx
        self._current_page = idx
        self.spin_page.blockSignals(True)
        self.spin_page.setValue(idx + 1)
        self.spin_page.blockSignals(False)
        self.page_widget.update_overview_current(idx)
        self._update_info()

    def _on_overview_current(self, page_index):
        if not self._overview_active:
            return
        self._overview_current = page_index
        self._current_page = page_index
        self.spin_page.blockSignals(True)
        self.spin_page.setValue(page_index + 1)
        self.spin_page.blockSignals(False)
        self.page_widget.update_overview_current(page_index)
        self._update_info()

    def _overview_goto_page(self, page_index):
        if not self._overview_active:
            return
        page_index = max(0, min(page_index, self.render.page_count - 1))
        self._overview_current = page_index
        self._current_page = page_index
        self.spin_page.blockSignals(True)
        self.spin_page.setValue(page_index + 1)
        self.spin_page.blockSignals(False)
        self.page_widget.update_overview_current(page_index)
        self.page_widget._center_overview_on_page(page_index)
        self._request_visible_thumbs()
        self._update_info()

    def _toggle_box(self, name, visible):
        if visible:
            self.page_widget.overlay.active_boxes.add(name)
        else:
            self.page_widget.overlay.active_boxes.discard(name)
        self.page_widget.viewport().update()

    # ===================== Keyboard shortcuts =====================
    def _default_shortcut(self, key):
        defaults = {
            "close": "Ctrl+W",
            "save": "Ctrl+S",
            "toggle_dock": "Tab",
            "fullscreen": "F",
            "page_up": "PageUp",
            "page_down": "PageDown",
        }
        return self._settings.value(f"shortcuts/{key}", defaults[key])

    def _setup_shortcuts(self):
        for sc in getattr(self, '_shortcut_objects', []):
            try:
                sc.setEnabled(False)
            except RuntimeError:
                pass
        self._shortcut_objects = []

        def make(key, slot):
            ks = QKeySequence(self._default_shortcut(key))
            sc = QShortcut(ks, self)
            sc.activated.connect(slot)
            self._shortcut_objects.append(sc)
            return sc

        make("close", self._close_file)
        make("save", self._save_pdf)
        make("fullscreen", self._toggle_fullscreen)
        make("page_up", self._prev_page)
        make("page_down", self._next_page)

        # Tab toggles dock — we need keyPressEvent override too
        self._shortcut_dock = make("toggle_dock", self._toggle_dock_visibility)

        # Esc to exit fullscreen
        self._esc_shortcut = QShortcut(QKeySequence("Esc"), self)
        self._esc_shortcut.activated.connect(self._exit_fullscreen)
        self._shortcut_objects.append(self._esc_shortcut)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Home:
            self._go_to_page(1)
            return
        elif key == Qt.Key.Key_End:
            if self.render.doc:
                self._go_to_page(self.render.page_count)
            return
        elif key == Qt.Key.Key_CapsLock:
            self._color_overlay_enabled = not self._color_overlay_enabled
            vp = self.page_widget.viewport()
            if vp.underMouse():
                pos = vp.mapFromGlobal(QCursor.pos())
                scene_pos = self.page_widget.mapToScene(pos)
                self._on_mouse_move(scene_pos)
            return
        super().keyPressEvent(event)

    def eventFilter(self, obj, event):
        if obj is self.page_widget.viewport():
            if event.type() == event.Type.DragEnter:
                self.dragEnterEvent(event)
                return True
            if event.type() == event.Type.DragMove:
                self.dragMoveEvent(event)
                return True
            if event.type() == event.Type.Drop:
                self.dropEvent(event)
                return True
        return super().eventFilter(obj, event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().lower().endswith('.pdf'):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().lower().endswith('.pdf'):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith('.pdf'):
                if self.render.doc:
                    self._close_file()
                self.open_file(path)
                event.acceptProposedAction()
                return
        event.ignore()

    def _toggle_dock_visibility(self):
        visible = self._dock.isVisible()
        self._dock.setVisible(not visible)
        self._dock_collapse_btn.setVisible(not visible)

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self):
        self._fullscreen_prefs = {
            'toolbar': self._toolbar.isVisible(),
            'dock': self._dock.isVisible(),
            'menu': self.menuBar().isVisible(),
        }
        self._toolbar.hide()
        self._dock.hide()
        self._dock_collapse_btn.hide()
        self.menuBar().hide()
        self.showFullScreen()

    def _exit_fullscreen(self):
        if not self.isFullScreen():
            return
        self.showNormal()
        prefs = self._fullscreen_prefs or {}
        self._toolbar.setVisible(prefs.get('toolbar', True))
        self._dock.setVisible(prefs.get('dock', True))
        self._dock_collapse_btn.setVisible(prefs.get('dock', True))
        self.menuBar().setVisible(prefs.get('menu', True))
        self._fullscreen_prefs = None

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("PDF Preflight Viewer")
    app.setOrganizationName("PDFPreflight")
    app.setStyle('Fusion')
    from qt_material import apply_stylesheet
    settings = QSettings("PDFPreflight", "Viewer")
    mode = str(settings.value("ui/theme_mode", "dark"))
    theme = f"{mode}_teal.xml"
    accent = str(settings.value("ui/accent_color", "#1de9b6"))
    apply_stylesheet(app, theme=theme, extra={'primaryColor': accent})
    _apply_windows_accent(app)
    w = PreflightWindow()
    w.show()
    return app.exec()


def _apply_windows_accent(app):
    try:
        import ctypes
        from ctypes import wintypes
        dwm = ctypes.windll.dwmapi
        color = wintypes.DWORD()
        opaque = wintypes.BOOL()
        if dwm.DwmGetColorizationColor(ctypes.byref(color), ctypes.byref(opaque)) == 0:
            b = (color.value >> 16) & 0xFF
            g = (color.value >> 8) & 0xFF
            r = color.value & 0xFF
            p = app.palette()
            p.setColor(QPalette.ColorRole.Highlight, QColor(r, g, b))
            p.setColor(QPalette.ColorRole.Accent, QColor(r, g, b))
            app.setPalette(p)
            return
    except Exception:
        pass
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             "Software\\Microsoft\\Windows\\DWM")
        value, _ = winreg.QueryValueEx(key, "AccentColor")
        winreg.CloseKey(key)
        r = (value >> 16) & 0xFF
        g = (value >> 8) & 0xFF
        b = value & 0xFF
        p = app.palette()
        p.setColor(QPalette.ColorRole.Highlight, QColor(r, g, b))
        p.setColor(QPalette.ColorRole.Accent, QColor(r, g, b))
        app.setPalette(p)
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
