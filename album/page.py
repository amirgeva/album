"""A single album page: a QGraphicsScene holding movable / resizable image items."""
import math
from statistics import median

from PyQt6.QtCore import QPointF, QRect, QRectF, QSize, QSizeF, Qt, pyqtSignal
from PyQt6.QtGui import (QColor, QImage, QImageIOHandler, QImageReader, QPainter, QPainterPath, QPen,
                         QPixmap)
from PyQt6.QtWidgets import QGraphicsItem, QGraphicsScene

DISPLAY_MAX_SIDE = 2048     # longest side of the in-memory preview of each image
HANDLE_PX = 10              # resize handle size, in screen pixels
MIN_ITEM_SIZE = 20          # minimum item size, in page pixels
GRID_GAP = 8                # pixels between images in the automatic layout
OUTSIDE_COLOR = QColor(64, 64, 64)
OUTSIDE_OVERLAY = QColor(64, 64, 64, 190)
SELECTION_COLOR = QColor(0, 120, 215)
MARGIN_GUIDE_COLOR = QColor(128, 128, 128, 200)

CORNERS = ("tl", "tr", "bl", "br")
FULL_CROP = QRectF(0, 0, 1, 1)


def load_image(path, max_side=None):
    """Load an image honoring EXIF orientation, optionally downscaled. Returns None on failure."""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        return None
    if max_side and max(image.width(), image.height()) > max_side:
        image = image.scaled(max_side, max_side, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    return image


def source_size(path):
    """Full-resolution size of an image file after EXIF orientation, without decoding it."""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if not size.isValid():
        return None
    if reader.transformation() & QImageIOHandler.Transformation.TransformationRotate90:
        size.transpose()
    return size


def placeholder_image(aspect):
    """Image shown in place of a file that could not be loaded."""
    width = 400
    height = max(1, round(width / aspect))
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(120, 40, 40))
    painter = QPainter(image)
    painter.setPen(Qt.GlobalColor.white)
    painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, "Missing image")
    painter.end()
    return image


def fit_size(aspect, max_w, max_h):
    """Largest whole-pixel size with the given aspect fitting in max_w x max_h.

    The constraining side is floored to stay inside; the other is rounded to the nearest pixel.
    """
    if max_w / max_h <= aspect:
        w = max(1, math.floor(max_w))
        return w, max(1, round(w / aspect))
    h = max(1, math.floor(max_h))
    return max(1, round(h * aspect)), h


def clamp(value, lo, hi):
    """Clamp to [lo, hi]; if the range is empty (too big to fit), center instead."""
    if lo > hi:
        return round((lo + hi) / 2)
    return max(lo, min(hi, value))


def _balanced_partitions(weights):
    """For each group count k = 1..n, split the sequence into k contiguous groups so that
    the largest group sum is as small as possible. Yields lists of (start, end) index pairs."""
    n = len(weights)
    prefix = [0.0]
    for w in weights:
        prefix.append(prefix[-1] + w)
    best = prefix[:]                # best[j]: smallest max-sum for the first j items in k groups
    starts = [None, [0] * (n + 1)]  # starts[k][j]: where the last of k groups begins
    yield [(0, n)]
    for k in range(2, n + 1):
        cur, start = [math.inf] * (n + 1), [0] * (n + 1)
        for j in range(k, n + 1):
            for i in range(k - 1, j):
                cost = max(best[i], prefix[j] - prefix[i])
                if cost < cur[j]:
                    cur[j], start[j] = cost, i
        best = cur
        starts.append(start)
        groups, j = [], n
        for kk in range(k, 0, -1):
            i = starts[kk][j]
            groups.append((i, j))
            j = i
        yield groups[::-1]


def _justified_rows(aspects, groups, width, height, gap):
    """Rows that each span the width (scaled down together if too tall), all whole pixels.

    Returns (x, y, w, h) tuples relative to the area, or None if the gaps don't fit.
    """
    free = [width - gap * (j - i - 1) for i, j in groups]
    avail_h = height - gap * (len(groups) - 1)
    if min(free) <= 0 or avail_h <= 0:
        return None
    heights = [f / sum(aspects[i:j]) for f, (i, j) in zip(free, groups)]
    scale = min(1.0, avail_h / sum(heights))
    rows = []
    for (i, j), row_h in zip(groups, heights):
        # Flooring keeps every row inside the area; widths are off by < 1 px from the exact aspect.
        h = max(1, math.floor(row_h * scale))
        rows.append((h, [max(1, math.floor(h * aspects[k])) for k in range(i, j)]))
    y = (height - sum(h for h, _ in rows) - gap * (len(rows) - 1)) // 2
    rects = []
    for h, widths in rows:
        x = (width - sum(widths) - gap * (len(widths) - 1)) // 2
        for w in widths:
            rects.append((x, y, w, h))
            x += w + gap
        y += h + gap
    return rects


def _cluster(values, tolerance):
    """Group 1-D positions: a new group starts wherever consecutive sorted values jump by more
    than `tolerance`. Returns (group index per value, number of groups)."""
    order = sorted(range(len(values)), key=values.__getitem__)
    index = [0] * len(values)
    group = 0
    for prev, cur in zip(order, order[1:]):
        if values[cur] - values[prev] > tolerance:
            group += 1
        index[cur] = group
    return index, group + 1


def grid_layout(aspects, area, gap):
    """Arrange images, in order, in justified rows or columns filling the area (a QRectF).

    Images keep their aspect ratio and are exactly `gap` pixels apart, with no gap around the
    outside. Every row count is tried, in both orientations, and the layout covering the most
    area wins. Returns one whole-pixel QRectF per aspect, in the same order.
    """
    width, height = int(area.width()), int(area.height())
    best, best_cover = None, -1
    for transposed in (False, True):
        # Columns are rows of the transposed problem: swap the axes and invert the aspects.
        a = [1 / x for x in aspects] if transposed else aspects
        w, h = (height, width) if transposed else (width, height)
        for groups in _balanced_partitions(a):
            rects = _justified_rows(a, groups, w, h, gap)
            if rects is None:
                continue
            cover = sum(r[2] * r[3] for r in rects)
            if cover > best_cover:
                best_cover = cover
                best = [(y, x, rh, rw) for x, y, rw, rh in rects] if transposed else rects
    if best is None:
        return grid_layout(aspects, area, 0)
    return [QRectF(area.x() + x, area.y() + y, w, h) for x, y, w, h in best]


class ImageItem(QGraphicsItem):
    """An image placed on the page. Drag to move, drag a corner handle to resize (keeps aspect).

    Position and size are always whole page pixels, so export never resamples to fractional sizes.
    """

    def __init__(self, path, image, rect, missing=False, crop=None):
        super().__init__()
        self.path = path
        self.missing = missing
        self.image = image                  # downscaled preview of the whole file (QImage)
        # Kept part of the image, as fractions of its width / height, so it applies equally
        # to the preview and the full-resolution file.
        self.crop = QRectF(crop) if crop is not None else QRectF(FULL_CROP)
        self.display = image                # the cropped preview, what's drawn
        self.aspect = 1.0
        self._cache = None                  # pixmap scaled to the current on-screen size
        self._update_display()
        self._w = max(1, round(rect.width()))
        self._h = max(1, round(rect.height()))
        self._resize_corner = None
        self._anchor = QPointF()
        self.setPos(round(rect.x()), round(rect.y()))
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable
                      | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
                      | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)

    # ---- crop -----------------------------------------------------------

    def is_cropped(self):
        return self.crop != FULL_CROP

    def _update_display(self):
        iw, ih = self.image.width(), self.image.height()
        c = self.crop
        if self.missing or not self.is_cropped():
            # A placeholder can't show the crop; it already has the cropped aspect.
            self.display = self.image
            self.aspect = iw / ih
        else:
            box = QRect(round(c.x() * iw), round(c.y() * ih),
                        max(1, round(c.width() * iw)), max(1, round(c.height() * ih)))
            self.display = self.image.copy(box.intersected(self.image.rect()))
            self.aspect = (c.width() * iw) / (c.height() * ih)
        self._cache = None

    def set_crop(self, crop):
        """Change the crop, keeping the image's scale and the kept area's position on the page."""
        old = self.crop
        x, y, w, h = self.int_rect()
        sx, sy = w / old.width(), h / old.height()     # page pixels per whole-image width / height
        self.crop = QRectF(crop)
        self._update_display()
        new_w = max(1, round(crop.width() * sx))
        new_h = max(1, round(new_w / self.aspect))
        new_x = x + (crop.x() - old.x()) * sx
        new_y = y + (crop.y() - old.y()) * sy
        self.set_geometry(QRectF(new_x, new_y, new_w, new_h))

    # ---- geometry -------------------------------------------------------

    def rect(self):
        return QRectF(0, 0, self._w, self._h)

    def scene_rect(self):
        return QRectF(self.pos(), QSizeF(self._w, self._h))

    def int_rect(self):
        """(x, y, w, h) in whole page pixels."""
        return round(self.pos().x()), round(self.pos().y()), self._w, self._h

    def set_geometry(self, rect):
        scene = self.scene()
        if isinstance(scene, PageScene):
            rect = scene.constrain_rect(self.aspect, rect)
        self.prepareGeometryChange()
        self._w = max(1, round(rect.width()))
        self._h = max(1, round(rect.height()))
        self.setPos(round(rect.x()), round(rect.y()))
        self.update()
        self._notify()

    def _view_scale(self):
        scene = self.scene()
        if scene and scene.views():
            return scene.views()[0].transform().m11()
        return 1.0

    def _handle_size(self):
        return HANDLE_PX / self._view_scale()

    def view_scale_changed(self):
        """Handles have a constant screen size, so the bounding rect depends on the zoom."""
        self.prepareGeometryChange()

    def _handle_rects(self):
        s = self._handle_size()
        r = self.rect()
        points = {"tl": r.topLeft(), "tr": r.topRight(), "bl": r.bottomLeft(), "br": r.bottomRight()}
        return {c: QRectF(p.x() - s / 2, p.y() - s / 2, s, s) for c, p in points.items()}

    def _corner_at(self, pos):
        if not self.isSelected():
            return None
        for corner, r in self._handle_rects().items():
            if r.contains(pos):
                return corner
        return None

    def boundingRect(self):
        s = self._handle_size()
        return self.rect().adjusted(-s, -s, s, s)

    # ---- painting -------------------------------------------------------

    def paint(self, painter, option, widget=None):
        scale = painter.deviceTransform().m11()
        target_size = QSize(max(1, round(self._w * scale)), max(1, round(self._h * scale)))
        if self._cache is None or self._cache.size() != target_size:
            if target_size.width() >= self.display.width():
                self._cache = QPixmap.fromImage(self.display)
            else:
                self._cache = QPixmap.fromImage(self.display.scaled(
                    target_size, Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(self.rect(), self._cache, QRectF(self._cache.rect()))

        if self.isSelected():
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            pen = QPen(SELECTION_COLOR, 2)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self.rect())
            pen.setWidth(1)
            painter.setPen(pen)
            painter.setBrush(QColor(255, 255, 255))
            for r in self._handle_rects().values():
                painter.drawRect(r)

    # ---- interaction ----------------------------------------------------

    def hoverMoveEvent(self, event):
        corner = self._corner_at(event.pos())
        if corner in ("tl", "br"):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif corner:
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)
        else:
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):
        corner = self._corner_at(event.pos()) if event.button() == Qt.MouseButton.LeftButton else None
        if corner:
            r = self.scene_rect()
            opposite = {"tl": r.bottomRight(), "tr": r.bottomLeft(),
                        "bl": r.topRight(), "br": r.topLeft()}
            self._resize_corner = corner
            self._anchor = opposite[corner]
            event.accept()
            return
        super().mousePressEvent(event)      # updates the selection
        if isinstance(self.scene(), PageScene):
            self.scene().begin_drag()

    def mouseMoveEvent(self, event):
        if not self._resize_corner:
            super().mouseMoveEvent(event)
            return
        corner = self._resize_corner
        sx = 1 if corner in ("tr", "br") else -1
        sy = 1 if corner in ("bl", "br") else -1
        p = event.scenePos()
        a = self._anchor
        dx = (p.x() - a.x()) * sx
        dy = (p.y() - a.y()) * sy
        w = round(max(dx, dy * self.aspect, MIN_ITEM_SIZE))
        h = max(1, round(w / self.aspect))
        # Keep the anchor fixed and stop growing at the margin.
        c = self.scene().content_rect()
        space_x = c.right() - a.x() if sx > 0 else a.x() - c.left()
        space_y = c.bottom() - a.y() if sy > 0 else a.y() - c.top()
        if space_x >= 1 and space_y >= 1:
            max_w, max_h = fit_size(self.aspect, space_x, space_y)
            if w > max_w:
                w, h = max_w, max_h
        x = a.x() if sx > 0 else a.x() - w
        y = a.y() if sy > 0 else a.y() - h
        self.set_geometry(QRectF(x, y, w, h))

    def mouseReleaseEvent(self, event):
        if self._resize_corner:
            self._resize_corner = None
            event.accept()
            return
        super().mouseReleaseEvent(event)
        if isinstance(self.scene(), PageScene):
            self.scene().end_drag()

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            scene = self.scene()
            if isinstance(scene, PageScene):
                return scene.constrain_position(self, value)
            return QPointF(round(value.x()), round(value.y()))
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._notify()
        return super().itemChange(change, value)

    def _notify(self):
        scene = self.scene()
        if isinstance(scene, PageScene):
            scene.modified.emit()


class PageScene(QGraphicsScene):
    """One album page. Page coordinates are output-image pixels, origin at the top-left."""

    modified = pyqtSignal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.page_width = settings.width
        self.page_height = settings.height
        self.background = QColor(settings.background)
        self.margin = settings.margin
        self._drag_start = {}       # item -> scene rect at the start of a mouse drag
        self._drag_limits = (0, 0, 0, 0)
        # Leave room around the page for panning.
        w, h = self.page_width, self.page_height
        self.setSceneRect(-w, -h, 3 * w, 3 * h)

    def page_rect(self):
        return QRectF(0, 0, self.page_width, self.page_height)

    def content_rect(self):
        """The area inside the margin, where images may be placed."""
        m = self.margin
        return QRectF(m, m, self.page_width - 2 * m, self.page_height - 2 * m)

    def set_margin(self, margin):
        """Change the margin and pull every image inside it."""
        self.margin = margin
        for item in self.image_items():
            item.set_geometry(item.scene_rect())
        self.update()
        self.modified.emit()

    # ---- constraints ----------------------------------------------------

    def constrain_rect(self, aspect, rect):
        """Whole-pixel rect, shrunk (keeping aspect) and shifted to lie inside the margin."""
        c = self.content_rect()
        w, h = max(1, round(rect.width())), max(1, round(rect.height()))
        if w > c.width() or h > c.height():
            w, h = fit_size(aspect, min(w, c.width()), min(h, c.height()))
        x = clamp(round(rect.x()), c.left(), c.right() - w)
        y = clamp(round(rect.y()), c.top(), c.bottom() - h)
        return QRectF(x, y, w, h)

    def begin_drag(self):
        """Record the selection so a multi-item drag is clamped as one block."""
        self._drag_start = {i: i.scene_rect() for i in self.selected_image_items()}
        if not self._drag_start:
            return
        rects = list(self._drag_start.values())
        bounds = rects[0]
        for r in rects[1:]:
            bounds = bounds.united(r)
        c = self.content_rect()
        self._drag_limits = (c.left() - bounds.left(), c.right() - bounds.right(),
                             c.top() - bounds.top(), c.bottom() - bounds.bottom())

    def end_drag(self):
        self._drag_start = {}

    def constrain_position(self, item, pos):
        start = self._drag_start.get(item)
        if start is not None:
            min_dx, max_dx, min_dy, max_dy = self._drag_limits
            dx = clamp(round(pos.x() - start.x()), min_dx, max_dx)
            dy = clamp(round(pos.y() - start.y()), min_dy, max_dy)
            return QPointF(start.x() + dx, start.y() + dy)
        _, _, w, h = item.int_rect()
        c = self.content_rect()
        return QPointF(clamp(round(pos.x()), c.left(), c.right() - w),
                       clamp(round(pos.y()), c.top(), c.bottom() - h))

    def image_items(self):
        """All images, bottom to top."""
        items = [i for i in self.items() if isinstance(i, ImageItem)]
        return sorted(items, key=lambda i: i.zValue())

    def selected_image_items(self):
        return [i for i in self.image_items() if i.isSelected()]

    # ---- drawing --------------------------------------------------------

    def drawBackground(self, painter, rect):
        painter.fillRect(rect, OUTSIDE_COLOR)
        painter.fillRect(self.page_rect(), self.background)

    def drawForeground(self, painter, rect):
        # Dim whatever lies outside the page: it will be cropped on export.
        outside = QPainterPath()
        outside.addRect(rect)
        inside = QPainterPath()
        inside.addRect(self.page_rect())
        painter.fillPath(outside.subtracted(inside), OUTSIDE_OVERLAY)
        pen = QPen(QColor(0, 0, 0), 1)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.page_rect())
        if self.margin > 0:
            guide = QPen(MARGIN_GUIDE_COLOR, 1, Qt.PenStyle.DashLine)
            guide.setCosmetic(True)
            painter.setPen(guide)
            painter.drawRect(self.content_rect())

    # ---- editing --------------------------------------------------------

    def _top_z(self):
        items = self.image_items()
        return items[-1].zValue() if items else 0.0

    def add_images(self, paths, progress=None):
        """Load image files and lay them out in a grid. Returns (added items, failed paths).

        progress(done, total, path) is called before each file; if it returns False the
        operation is cancelled and nothing is added.
        """
        loaded, failed = [], []
        for done, path in enumerate(paths):
            if progress and not progress(done, len(paths), path):
                return [], []
            image = load_image(path, DISPLAY_MAX_SIDE)
            if image is None:
                failed.append(path)
            else:
                loaded.append((path, image))
        added = []
        z = self._top_z()
        for path, image in loaded:
            item = ImageItem(path, image, QRectF(0, 0, image.width(), image.height()))
            z += 1
            item.setZValue(z)
            self.addItem(item)
            added.append(item)
        if added:
            self.arrange_grid(added)
            self.clearSelection()
            for item in added:
                item.setSelected(True)
            self.modified.emit()
        return added, failed

    def add_image_item(self, path, rect, z, crop=None):
        """Restore a saved item. Missing files get a placeholder so the layout is kept."""
        image = load_image(path, DISPLAY_MAX_SIDE)
        missing = image is None
        if missing:
            image = placeholder_image(rect.width() / rect.height())
        item = ImageItem(path, image, rect, missing, crop)
        item.setZValue(z)
        self.addItem(item)
        return item

    def arrange_grid(self, items):
        if not items:
            return
        rects = grid_layout([i.aspect for i in items], self.content_rect(), GRID_GAP)
        for item, rect in zip(items, rects):
            item.set_geometry(rect)

    def align_to_grid(self, items):
        """Snap a roughly aligned arrangement to an exact grid.

        Rows and columns are detected from the image centers. Every cell gets the same size,
        the grid fills the area inside the margin and images are GRID_GAP apart.
        """
        if not items:
            return
        rects = [i.scene_rect() for i in items]
        rows, n_rows = _cluster([r.center().y() for r in rects], median(r.height() for r in rects) / 2)
        cols, n_cols = _cluster([r.center().x() for r in rects], median(r.width() for r in rects) / 2)
        if len(set(zip(rows, cols))) < len(items):
            # Two images in one cell: number each row's images left to right instead.
            per_row = {}
            for k in sorted(range(len(items)), key=lambda k: rects[k].center().x()):
                cols[k] = per_row.get(rows[k], 0)
                per_row[rows[k]] = cols[k] + 1
            n_cols = max(per_row.values())

        c = self.content_rect()
        area_w, area_h = int(c.width()), int(c.height())
        cell_w = (area_w - GRID_GAP * (n_cols - 1)) // n_cols
        cell_h = (area_h - GRID_GAP * (n_rows - 1)) // n_rows
        if cell_w < 1 or cell_h < 1:
            return
        sizes = [fit_size(i.aspect, cell_w, cell_h) for i in items]
        # Shrink the cells to the largest image, so same-shaped images sit exactly GRID_GAP apart.
        cell_w = max(w for w, _ in sizes)
        cell_h = max(h for _, h in sizes)
        x0 = int(c.x()) + (area_w - n_cols * cell_w - GRID_GAP * (n_cols - 1)) // 2
        y0 = int(c.y()) + (area_h - n_rows * cell_h - GRID_GAP * (n_rows - 1)) // 2
        for item, row, col, (w, h) in zip(items, rows, cols, sizes):
            item.set_geometry(QRectF(x0 + col * (cell_w + GRID_GAP) + (cell_w - w) // 2,
                                     y0 + row * (cell_h + GRID_GAP) + (cell_h - h) // 2, w, h))

    def fit_to_page(self, item):
        """Scale the item to the largest size fitting inside the margin, centered."""
        item.set_geometry(grid_layout([item.aspect], self.content_rect(), 0)[0])

    def center_items(self, items, horizontal, vertical):
        """Center the items' combined bounding box on the page, keeping their relative layout."""
        if not items:
            return
        bounds = items[0].scene_rect()
        for item in items[1:]:
            bounds = bounds.united(item.scene_rect())
        dx = round((self.page_width - bounds.width()) / 2 - bounds.x()) if horizontal else 0
        dy = round((self.page_height - bounds.height()) / 2 - bounds.y()) if vertical else 0
        for item in items:
            item.moveBy(dx, dy)

    def bring_to_front(self, items):
        z = self._top_z()
        for item in sorted(items, key=lambda i: i.zValue()):
            z += 1
            item.setZValue(z)
        self.modified.emit()

    def send_to_back(self, items):
        bottom = self.image_items()
        z = bottom[0].zValue() if bottom else 0.0
        for item in sorted(items, key=lambda i: i.zValue(), reverse=True):
            z -= 1
            item.setZValue(z)
        self.modified.emit()

    def remove_items(self, items):
        for item in items:
            self.removeItem(item)
        if items:
            self.modified.emit()


def render_page(scene, width, height):
    """Render a quick preview of the page from the in-memory images (thumbnails).

    The output file is rendered by export.render_page_full instead.
    """
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(scene.background)
    sx = width / scene.page_width
    sy = height / scene.page_height
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    for item in scene.image_items():
        r = item.scene_rect()
        target = QRectF(r.x() * sx, r.y() * sy, r.width() * sx, r.height() * sy)
        source = item.display
        tw, th = round(target.width()), round(target.height())
        if 0 < tw < source.width() and 0 < th < source.height():
            # Pre-scale with a proper filter; drawImage's bilinear scaling aliases when shrinking.
            source = source.scaled(tw, th, Qt.AspectRatioMode.IgnoreAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
        painter.drawImage(target, source, QRectF(source.rect()))
    painter.end()
    return image
