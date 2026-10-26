"""Dialogs."""
import math

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                             QHBoxLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from .document import DocumentSettings
from .page import FULL_CROP, MIN_ITEM_SIZE, source_size

DEFAULT_MARGIN = 100


def max_margin(width, height):
    """Largest margin that still leaves room for an image."""
    return max(0, (min(width, height) - MIN_ITEM_SIZE) // 2)


DEFAULT_DPI = 300

# (name, pixel size, print DPI or None to keep the current one)
PRESETS = [
    ("Custom", None, None),
    ("A4 landscape, 300 DPI", (3508, 2480), 300),
    ("A4 portrait, 300 DPI", (2480, 3508), 300),
    ("A3 landscape, 300 DPI", (4961, 3508), 300),
    ("Square 30 x 30 cm, 300 DPI", (3543, 3543), 300),
    ("Square 20 x 20 cm, 300 DPI", (2362, 2362), 300),
    ("3:2 landscape", (4500, 3000), None),
    ("4:3 landscape", (4000, 3000), None),
    ("16:9 Full HD", (1920, 1080), None),
    ("16:9 4K", (3840, 2160), None),
]


class NewDocumentDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Album")
        self._color = QColor(255, 255, 255)

        self.preset = QComboBox()
        for name, size, dpi in PRESETS:
            self.preset.addItem(f"{name} ({size[0]} x {size[1]})" if size else name, (size, dpi))
        self.width_spin = QSpinBox()
        self.height_spin = QSpinBox()
        for spin in (self.width_spin, self.height_spin):
            spin.setRange(100, 30000)
            spin.setSuffix(" px")
            spin.valueChanged.connect(self._size_changed)
        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(30, 2400)
        self.dpi_spin.setSuffix(" DPI")
        self.dpi_spin.setValue(DEFAULT_DPI)
        self.dpi_spin.valueChanged.connect(self._update_info)
        self.info = QLabel()
        self.margin_spin = QSpinBox()
        self.margin_spin.setSuffix(" px")
        self.margin_spin.setRange(0, 30000)     # narrowed to the page size in _update_info
        self.margin_spin.setValue(DEFAULT_MARGIN)
        self.color_button = QPushButton()
        self.color_button.clicked.connect(self._pick_color)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QFormLayout(self)
        layout.addRow("Preset:", self.preset)
        layout.addRow("Width:", self.width_spin)
        layout.addRow("Height:", self.height_spin)
        layout.addRow("Print resolution:", self.dpi_spin)
        layout.addRow("", self.info)
        layout.addRow("Margin:", self.margin_spin)
        layout.addRow("Background:", self.color_button)
        layout.addRow(buttons)

        self.preset.currentIndexChanged.connect(self._preset_changed)
        self.preset.setCurrentIndex(1)
        self._update_color_button()

    def _preset_changed(self, index):
        size, dpi = self.preset.itemData(index)
        if size:
            for spin, value in zip((self.width_spin, self.height_spin), size):
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)
            if dpi:
                self.dpi_spin.blockSignals(True)
                self.dpi_spin.setValue(dpi)
                self.dpi_spin.blockSignals(False)
            self._update_info()

    def _size_changed(self):
        size = (self.width_spin.value(), self.height_spin.value())
        index = next((i for i in range(self.preset.count()) if self.preset.itemData(i)[0] == size), 0)
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(index)
        self.preset.blockSignals(False)
        self._update_info()

    def _update_info(self):
        w, h = self.width_spin.value(), self.height_spin.value()
        self.margin_spin.setMaximum(max_margin(w, h))
        g = math.gcd(w, h)
        ratio = f"{w // g}:{h // g}" if max(w, h) // g <= 50 else f"{w / h:.3f}"
        dpi = self.dpi_spin.value()
        self.info.setText(f"Aspect ratio {ratio}  ·  {w * h / 1e6:.1f} MP  ·  "
                          f"prints at {w / dpi * 2.54:.1f} x {h / dpi * 2.54:.1f} cm")

    def _pick_color(self):
        color = QColorDialog.getColor(self._color, self, "Page Background")
        if color.isValid():
            self._color = color
            self._update_color_button()

    def _update_color_button(self):
        self.color_button.setText(self._color.name())
        text = "black" if self._color.lightness() > 128 else "white"
        self.color_button.setStyleSheet(f"background-color: {self._color.name()}; color: {text};")

    def settings(self):
        return DocumentSettings(self.width_spin.value(), self.height_spin.value(), QColor(self._color),
                                self.margin_spin.value(), self.dpi_spin.value())


CROP_HANDLE_PX = 10
CROP_MIN_PX = 16    # minimum crop size, in preview pixels


class CropWidget(QWidget):
    """Shows an image with a crop rectangle that can be moved and resized.

    The crop is kept in image (preview) pixel coordinates. With an aspect ratio set,
    only the corner handles are shown and they keep that ratio.
    """

    changed = pyqtSignal()

    def __init__(self, image, crop, parent=None):
        super().__init__(parent)
        self.image = image
        self.pixmap = QPixmap.fromImage(image)
        self.bounds = QRectF(0, 0, image.width(), image.height())
        self.crop = QRectF(crop)
        self.aspect = None
        self._drag = None
        self._press = QPointF()
        self._start = QRectF()
        self.setMouseTracking(True)
        self.setMinimumSize(640, 480)

    # ---- coordinates ----------------------------------------------------

    def _view(self):
        """Where the image is drawn in the widget, and its scale."""
        m = CROP_HANDLE_PX
        avail_w, avail_h = self.width() - 2 * m, self.height() - 2 * m
        s = min(avail_w / self.bounds.width(), avail_h / self.bounds.height())
        w, h = self.bounds.width() * s, self.bounds.height() * s
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h), s

    def _to_widget(self, r):
        v, s = self._view()
        return QRectF(v.x() + r.x() * s, v.y() + r.y() * s, r.width() * s, r.height() * s)

    def _to_image(self, p):
        v, s = self._view()
        return QPointF((p.x() - v.x()) / s, (p.y() - v.y()) / s)

    def _handles(self):
        r = self._to_widget(self.crop)
        points = {"tl": r.topLeft(), "tr": r.topRight(), "bl": r.bottomLeft(), "br": r.bottomRight()}
        if self.aspect is None:
            points.update({"t": QPointF(r.center().x(), r.top()), "b": QPointF(r.center().x(), r.bottom()),
                           "l": QPointF(r.left(), r.center().y()), "r": QPointF(r.right(), r.center().y())})
        return points

    def _hit(self, pos):
        for name, p in self._handles().items():
            if abs(pos.x() - p.x()) <= CROP_HANDLE_PX and abs(pos.y() - p.y()) <= CROP_HANDLE_PX:
                return name
        return "move" if self._to_widget(self.crop).contains(pos) else None

    # ---- editing --------------------------------------------------------

    def set_crop(self, crop):
        self.crop = QRectF(crop)
        self.update()
        self.changed.emit()

    def set_aspect(self, aspect):
        """Lock the crop to an aspect ratio (image pixels w/h), or None for free."""
        self.aspect = aspect
        if aspect is not None:
            # Largest crop with that ratio, centered where the current one is.
            b = self.bounds
            w = min(b.width(), b.height() * aspect)
            h = w / aspect
            c = self.crop.center()
            x = min(max(c.x() - w / 2, 0), b.width() - w)
            y = min(max(c.y() - h / 2, 0), b.height() - h)
            self.crop = QRectF(x, y, w, h)
        self.update()
        self.changed.emit()

    def _drag_to(self, p):
        b, s, d = self.bounds, self._start, self._drag
        if d == "move":
            r = s.translated(p - self._press)
            r.moveLeft(min(max(r.left(), 0), b.width() - r.width()))
            r.moveTop(min(max(r.top(), 0), b.height() - r.height()))
        elif self.aspect is not None:
            # Corner with a locked ratio: the opposite corner stays fixed.
            sx = 1 if d in ("tr", "br") else -1
            sy = 1 if d in ("bl", "br") else -1
            a = QPointF(s.left() if sx > 0 else s.right(), s.top() if sy > 0 else s.bottom())
            space_x = b.width() - a.x() if sx > 0 else a.x()
            space_y = b.height() - a.y() if sy > 0 else a.y()
            w = max((p.x() - a.x()) * sx, (p.y() - a.y()) * sy * self.aspect, CROP_MIN_PX)
            w = min(w, space_x, space_y * self.aspect)
            h = w / self.aspect
            r = QRectF(a.x() if sx > 0 else a.x() - w, a.y() if sy > 0 else a.y() - h, w, h)
        else:
            left, top, right, bottom = s.left(), s.top(), s.right(), s.bottom()
            if "l" in d:
                left = min(max(p.x(), 0), right - CROP_MIN_PX)
            if "r" in d:
                right = max(min(p.x(), b.width()), left + CROP_MIN_PX)
            if "t" in d:
                top = min(max(p.y(), 0), bottom - CROP_MIN_PX)
            if "b" in d:
                bottom = max(min(p.y(), b.height()), top + CROP_MIN_PX)
            r = QRectF(QPointF(left, top), QPointF(right, bottom))
        self.set_crop(r)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = self._hit(event.position())
            self._press = self._to_image(event.position())
            self._start = QRectF(self.crop)

    def mouseMoveEvent(self, event):
        if self._drag:
            self._drag_to(self._to_image(event.position()))
            return
        hit = self._hit(event.position())
        cursors = {"tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
                   "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
                   "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
                   "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
                   "move": Qt.CursorShape.SizeAllCursor}
        self.setCursor(cursors.get(hit, Qt.CursorShape.ArrowCursor))

    def mouseReleaseEvent(self, event):
        self._drag = None

    # ---- painting -------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(48, 48, 48))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        view, _ = self._view()
        painter.drawPixmap(view, self.pixmap, QRectF(self.pixmap.rect()))

        crop = self._to_widget(self.crop)
        outside = QPainterPath()
        outside.addRect(view)
        inside = QPainterPath()
        inside.addRect(crop)
        painter.fillPath(outside.subtracted(inside), QColor(0, 0, 0, 160))

        painter.setPen(QPen(QColor(255, 255, 255, 90), 1))      # rule of thirds
        for i in (1, 2):
            x = crop.left() + crop.width() * i / 3
            y = crop.top() + crop.height() * i / 3
            painter.drawLine(QPointF(x, crop.top()), QPointF(x, crop.bottom()))
            painter.drawLine(QPointF(crop.left(), y), QPointF(crop.right(), y))
        painter.setPen(QPen(QColor(255, 255, 255), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(crop)
        painter.setBrush(QColor(255, 255, 255))
        painter.setPen(QPen(QColor(0, 0, 0), 1))
        h = CROP_HANDLE_PX
        for p in self._handles().values():
            painter.drawRect(QRectF(p.x() - h / 2, p.y() - h / 2, h, h))


class CropDialog(QDialog):
    """Edit an image item's crop. `page_aspect` is offered as a preset ratio."""

    def __init__(self, item, page_aspect, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Crop Image")
        image = item.image
        iw, ih = image.width(), image.height()
        self.full_size = source_size(item.path) or image.size()
        c = item.crop
        self.canvas = CropWidget(image, QRectF(c.x() * iw, c.y() * ih, c.width() * iw, c.height() * ih))
        self.canvas.changed.connect(self._update_info)

        self.aspect_combo = QComboBox()
        for name, value in [("Free", None), ("Original", iw / ih), ("1:1", 1.0),
                            ("3:2", 3 / 2), ("2:3", 2 / 3), ("4:3", 4 / 3), ("3:4", 3 / 4),
                            ("16:9", 16 / 9), ("9:16", 9 / 16), ("Page", page_aspect)]:
            self.aspect_combo.addItem(name, value)
        self.aspect_combo.currentIndexChanged.connect(
            lambda i: self.canvas.set_aspect(self.aspect_combo.itemData(i)))
        reset = QPushButton("Reset")
        reset.clicked.connect(self._reset)
        self.info = QLabel()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Aspect ratio:"))
        controls.addWidget(self.aspect_combo)
        controls.addWidget(reset)
        controls.addStretch(1)
        controls.addWidget(self.info)
        layout = QVBoxLayout(self)
        layout.addWidget(self.canvas, 1)
        layout.addLayout(controls)
        layout.addWidget(buttons)
        self._update_info()
        self.resize(1000, 750)

    def _reset(self):
        self.aspect_combo.setCurrentIndex(0)
        self.canvas.set_crop(self.canvas.bounds)

    def _update_info(self):
        c = self.crop()
        fw, fh = self.full_size.width(), self.full_size.height()
        self.info.setText(f"{round(c.width() * fw)} x {round(c.height() * fh)} px of {fw} x {fh}")

    def crop(self):
        """The chosen crop as fractions of the image size."""
        r, b = self.canvas.crop, self.canvas.bounds
        crop = QRectF(r.x() / b.width(), r.y() / b.height(), r.width() / b.width(), r.height() / b.height())
        return crop.intersected(FULL_CROP)
