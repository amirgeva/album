"""Main window: page list on the left, page editor on the right."""
import os
import sys

from PyQt6.QtCore import QSettings, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon, QImageReader, QKeySequence, QPainter, QPixmap
from PyQt6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QGraphicsView, QInputDialog, QLabel, QListView,
                             QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressDialog,
                             QSplitter)

from .dialogs import CropDialog, NewDocumentDialog, max_margin
from .document import FILE_FILTER, DocumentSettings, load_album, save_album
from .export import append_pdf_page, render_page_full, save_page_image
from .page import FULL_CROP, ImageItem, PageScene, render_page

APP_NAME = "Photo Album"
THUMB_SIZE = 160
ZOOM_STEP = 1.15
PDF_DPI_CHOICES = (300, 200, 150, 100, 72)


def normalize_path(path):
    """Canonical form for comparing file paths (separators, case on Windows, relative parts)."""
    return os.path.normcase(os.path.abspath(path))


def image_file_filter():
    formats = sorted({bytes(f).decode() for f in QImageReader.supportedImageFormats()})
    return "Images (" + " ".join(f"*.{f}" for f in formats) + ");;All files (*)"


class PageView(QGraphicsView):
    """Shows one page. Ctrl+wheel zooms, middle button pans, right click opens a context menu."""

    add_images_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._fit = True
        self._pan_start = None

    def setScene(self, scene):
        super().setScene(scene)
        self.fit_page()

    def fit_page(self):
        self._fit = True
        scene = self.scene()
        if scene is None:
            return
        r = scene.page_rect()
        m = 0.03 * max(r.width(), r.height())
        self.fitInView(r.adjusted(-m, -m, m, m), Qt.AspectRatioMode.KeepAspectRatio)
        self._scale_changed()

    def zoom(self, factor):
        self._fit = False
        self.scale(factor, factor)
        self._scale_changed()

    def _scale_changed(self):
        for item in self.scene().image_items():
            item.view_scale_changed()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit:
            self.fit_page()

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(ZOOM_STEP ** (event.angleDelta().y() / 120))
            event.accept()
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan_start = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pan_start is not None:
            delta = event.position() - self._pan_start
            self._pan_start = event.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - round(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - round(delta.y()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton and self._pan_start is not None:
            self._pan_start = None
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        scene = self.scene()
        menu = QMenu(self)

        def add(text, slot):
            menu.addAction(text).triggered.connect(lambda: slot())

        item = self.itemAt(event.pos())
        if isinstance(item, ImageItem):
            if not item.isSelected():
                scene.clearSelection()
                item.setSelected(True)
            selected = scene.selected_image_items()
            add("Bring to Front", lambda: scene.bring_to_front(selected))
            add("Send to Back", lambda: scene.send_to_back(selected))
            if len(selected) == 1:
                add("Fit to Page", lambda: scene.fit_to_page(item))
            else:
                add("Auto Arrange Selected", lambda: scene.arrange_grid(selected))
                add("Align Selected to Grid", lambda: scene.align_to_grid(selected))
            menu.addSeparator()
            add("Center Horizontally", lambda: scene.center_items(selected, True, False))
            add("Center Vertically", lambda: scene.center_items(selected, False, True))
            add("Center on Page", lambda: scene.center_items(selected, True, True))
            menu.addSeparator()
            add("Crop...", lambda: self.crop_item(item))
            menu.actions()[-1].setEnabled(not item.missing)
            if item.is_cropped():
                add("Reset Crop", lambda: item.set_crop(FULL_CROP))
            menu.addSeparator()
            add("Remove", lambda: scene.remove_items(selected))
        else:
            add("Add Images...", self.add_images_requested.emit)
            if scene.image_items():
                add("Auto Arrange All", lambda: scene.arrange_grid(scene.image_items()))
                add("Align All to Grid", lambda: scene.align_to_grid(scene.image_items()))
        menu.exec(event.globalPos())

    def mouseDoubleClickEvent(self, event):
        item = self.itemAt(event.pos())
        if isinstance(item, ImageItem) and event.button() == Qt.MouseButton.LeftButton:
            self.crop_item(item)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def crop_item(self, item):
        if item.missing:
            return
        content = self.scene().content_rect()
        dialog = CropDialog(item, content.width() / content.height(), self)
        if dialog.exec():
            item.set_crop(dialog.crop())


class LoadProgress:
    """A progress(done, total, path) callback that shows a cancellable progress dialog.

    The dialog is created on the first call, once the total is known, and only appears
    if loading takes longer than a moment. Returns False once the user cancels.
    """

    def __init__(self, parent, title):
        self.parent = parent
        self.title = title
        self.dialog = None
        self.cancelled = False

    def __call__(self, done, total, path):
        if self.dialog is None:
            self.dialog = QProgressDialog(self.title, "Cancel", 0, total, self.parent)
            self.dialog.setWindowTitle(APP_NAME)
            self.dialog.setWindowModality(Qt.WindowModality.WindowModal)
            self.dialog.setMinimumDuration(400)
            self.dialog.setAutoReset(False)
            self.dialog.setMinimumWidth(420)
        self.dialog.setLabelText(f"{self.title}\nImage {done + 1} of {total}: {os.path.basename(path)}")
        self.dialog.setValue(done)     # also processes events, so Cancel and repaints work
        self.cancelled = self.dialog.wasCanceled()
        return not self.cancelled

    def close(self):
        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.prefs = QSettings("album", "PhotoAlbum")
        self.settings = None
        self.pages = []
        self.file_path = None
        self.dirty = False

        self.view = PageView()
        self.view.add_images_requested.connect(self.add_images)

        self.page_list = QListWidget()
        self.page_list.setViewMode(QListView.ViewMode.IconMode)
        self.page_list.setFlow(QListView.Flow.TopToBottom)
        self.page_list.setWrapping(False)
        self.page_list.setMovement(QListView.Movement.Static)
        self.page_list.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.page_list.setSpacing(6)
        self.page_list.setMinimumWidth(THUMB_SIZE + 40)
        self.page_list.currentRowChanged.connect(self._on_page_selected)

        splitter = QSplitter()
        splitter.addWidget(self.page_list)
        splitter.addWidget(self.view)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([THUMB_SIZE + 50, 1000])
        self.setCentralWidget(splitter)

        # Thumbnails are re-rendered shortly after the last change, not on every mouse move.
        self._stale_thumbs = set()
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.setInterval(300)
        self._thumb_timer.timeout.connect(self._refresh_thumbnails)

        self.status_label = QLabel()
        self.statusBar().addPermanentWidget(self.status_label)

        self._create_actions()
        self.resize(1400, 900)

    # ---- actions --------------------------------------------------------

    def _action(self, menu, text, slot, shortcut=None):
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(lambda: slot())
        menu.addAction(action)
        return action

    def _create_actions(self):
        bar = self.menuBar()
        m = bar.addMenu("&File")
        self._action(m, "&New Album...", self.new_document, QKeySequence.StandardKey.New)
        self._action(m, "&Open...", self.open_document, QKeySequence.StandardKey.Open)
        self._action(m, "&Save", self.save, QKeySequence.StandardKey.Save)
        self._action(m, "Save &As...", self.save_as, "Ctrl+Shift+S")
        m.addSeparator()
        self._action(m, "&Export Page...", self.export_page, "Ctrl+E")
        self._action(m, "Export &All Pages...", self.export_all, "Ctrl+Shift+E")
        self._action(m, "Export &PDF...", self.export_pdf, "Ctrl+Shift+P")
        m.addSeparator()
        self._action(m, "E&xit", self.close, "Ctrl+Q")

        m = bar.addMenu("&Edit")
        self._action(m, "&Add Images...", self.add_images, "Ctrl+I")
        self._action(m, "&Remove Selected", lambda: self.page.remove_items(self.page.selected_image_items()),
                     QKeySequence.StandardKey.Delete)
        self._action(m, "Select A&ll", self._select_all, QKeySequence.StandardKey.SelectAll)
        m.addSeparator()
        self._action(m, "Bring to &Front", lambda: self.page.bring_to_front(self.page.selected_image_items()),
                     "Ctrl+]")
        self._action(m, "Send to &Back", lambda: self.page.send_to_back(self.page.selected_image_items()),
                     "Ctrl+[")
        self._action(m, "A&uto Arrange", self._auto_arrange, "Ctrl+G")
        self._action(m, "Align to &Grid", self._align_to_grid, "Ctrl+Shift+G")

        m = bar.addMenu("&Page")
        self._action(m, "&Add Page", self.add_page, "Ctrl+Shift+N")
        self._action(m, "&Delete Page", self.delete_page)
        self._action(m, "Move Page &Up", lambda: self.move_page(-1), "Ctrl+Shift+Up")
        self._action(m, "Move Page Do&wn", lambda: self.move_page(1), "Ctrl+Shift+Down")
        m.addSeparator()
        self._action(m, "&Margin...", self.edit_margin)
        self._action(m, "&Print Resolution...", self.edit_dpi)

        m = bar.addMenu("&View")
        self._action(m, "&Fit Page", self.view.fit_page, "Ctrl+0")
        self._action(m, "Zoom &In", lambda: self.view.zoom(ZOOM_STEP), QKeySequence.StandardKey.ZoomIn)
        self._action(m, "Zoom &Out", lambda: self.view.zoom(1 / ZOOM_STEP), QKeySequence.StandardKey.ZoomOut)

    @property
    def page(self):
        return self.view.scene()

    def _select_all(self):
        for item in self.page.image_items():
            item.setSelected(True)

    def _target_items(self):
        """The selection if it has more than one image, otherwise the whole page."""
        selected = self.page.selected_image_items()
        return selected if len(selected) > 1 else self.page.image_items()

    def _auto_arrange(self):
        self.page.arrange_grid(self._target_items())

    def _align_to_grid(self):
        self.page.align_to_grid(self._target_items())

    # ---- document -------------------------------------------------------

    def set_document(self, settings, pages, path=None):
        self.settings = settings
        self.pages = pages
        self.file_path = path
        for page in pages:
            self._connect_page(page)
        self._rebuild_page_list(0)
        self._set_dirty(False)
        self._update_status()

    def _update_status(self):
        s = self.settings
        width_cm, height_cm = s.physical_size_cm()
        self.status_label.setText(f"Page {s.width} x {s.height} px  ·  {width_cm:.1f} x {height_cm:.1f} cm "
                                  f"at {s.dpi} DPI  ·  margin {s.margin} px  ·  background {s.background.name()}")

    def edit_dpi(self):
        """Change the print resolution: the pixels and layout stay, the physical size changes."""
        s = self.settings
        dpi, ok = QInputDialog.getInt(
            self, "Print Resolution",
            f"Print resolution (DPI) for the {s.width} x {s.height} px pages.\n"
            "This sets the printed size; the layout is not changed.", s.dpi, 30, 2400)
        if not ok or dpi == s.dpi:
            return
        s.dpi = dpi
        self._set_dirty(True)
        self._update_status()

    def edit_margin(self):
        s = self.settings
        margin, ok = QInputDialog.getInt(self, "Page Margin", "Margin (px), applied to all pages:",
                                         s.margin, 0, max_margin(s.width, s.height))
        if not ok or margin == s.margin:
            return
        s.margin = margin
        for page in self.pages:
            page.set_margin(margin)
        self._update_status()

    def _connect_page(self, page):
        page.modified.connect(lambda p=page: self._on_page_modified(p))

    def _set_dirty(self, dirty):
        self.dirty = dirty
        name = os.path.basename(self.file_path) if self.file_path else "Untitled"
        self.setWindowTitle(f"{name}{'*' if dirty else ''} - {APP_NAME}")

    def maybe_save(self):
        """Ask to save unsaved changes. Returns False if the user cancelled."""
        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self, APP_NAME, "The album has unsaved changes. Save them?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Save:
            return self.save()
        return answer == QMessageBox.StandardButton.Discard

    def new_document(self):
        if not self.maybe_save():
            return
        dialog = NewDocumentDialog(self)
        if dialog.exec():
            settings = dialog.settings()
            self.set_document(settings, [PageScene(settings)])

    def open_document(self, path=None):
        if not self.maybe_save():
            return
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Open Album", self.prefs.value("album_dir", ""),
                                                  FILE_FILTER)
            if not path:
                return
        progress = LoadProgress(self, f"Opening {os.path.basename(path)}")
        try:
            result = load_album(path, progress)
        except (OSError, ValueError, KeyError) as e:
            QMessageBox.critical(self, APP_NAME, f"Could not open {path}:\n{e}")
            return
        finally:
            progress.close()
        if result is None:
            self.statusBar().showMessage("Opening cancelled", 5000)
            return
        settings, pages, missing = result
        self.prefs.setValue("album_dir", os.path.dirname(path))
        self.set_document(settings, pages, path)
        if missing:
            QMessageBox.warning(self, APP_NAME, "These images could not be found:\n\n" + "\n".join(missing))

    def save(self):
        if not self.file_path:
            return self.save_as()
        try:
            save_album(self.file_path, self.settings, self.pages)
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"Could not save {self.file_path}:\n{e}")
            return False
        self._set_dirty(False)
        return True

    def save_as(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save Album", self.prefs.value("album_dir", ""),
                                              FILE_FILTER)
        if not path:
            return False
        if not path.lower().endswith(".album"):
            path += ".album"
        self.prefs.setValue("album_dir", os.path.dirname(path))
        self.file_path = path
        return self.save()

    def closeEvent(self, event):
        if self.maybe_save():
            event.accept()
        else:
            event.ignore()

    # ---- images ---------------------------------------------------------

    def add_images(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add Images", self.prefs.value("image_dir", ""),
                                                image_file_filter())
        if not paths:
            return
        self.prefs.setValue("image_dir", os.path.dirname(paths[0]))
        paths = self._filter_duplicates(paths)
        if not paths:
            return
        progress = LoadProgress(self, "Loading images")
        try:
            _, failed = self.page.add_images(paths, progress)
        finally:
            progress.close()
        if progress.cancelled:
            self.statusBar().showMessage("Adding images cancelled", 5000)
            return
        if failed:
            QMessageBox.warning(self, APP_NAME, "These files could not be loaded:\n\n" + "\n".join(failed))

    def _filter_duplicates(self, paths):
        """Ask what to do with files already used in the album. Returns the paths to insert."""
        used = {}   # normalized path -> page numbers using it
        for number, page in enumerate(self.pages, 1):
            for item in page.image_items():
                pages = used.setdefault(normalize_path(item.path), [])
                if number not in pages:
                    pages.append(number)

        result = []
        remembered = None   # "insert" / "skip" once "apply to all" is checked
        for path in paths:
            pages = used.get(normalize_path(path))
            if not pages:
                result.append(path)
                continue
            choice = remembered
            if choice is None:
                box = QMessageBox(QMessageBox.Icon.Question, APP_NAME,
                                  f"{os.path.basename(path)} is already used on page "
                                  f"{', '.join(map(str, pages))}.", parent=self)
                box.setInformativeText(path)
                insert = box.addButton("Insert Copy", QMessageBox.ButtonRole.AcceptRole)
                skip = box.addButton("Skip", QMessageBox.ButtonRole.RejectRole)
                box.setDefaultButton(skip)
                box.setEscapeButton(skip)
                # Parented and referenced: PyQt doesn't transfer ownership in setCheckBox.
                apply_to_all = QCheckBox("Do this for all remaining duplicates", box)
                box.setCheckBox(apply_to_all)
                box.exec()
                choice = "insert" if box.clickedButton() is insert else "skip"
                if apply_to_all.isChecked():
                    remembered = choice
                box.deleteLater()
            if choice == "insert":
                result.append(path)
        return result

    # ---- pages ----------------------------------------------------------

    def _rebuild_page_list(self, current):
        self.page_list.blockSignals(True)
        self.page_list.clear()
        for i in range(len(self.pages)):
            QListWidgetItem(f"Page {i + 1}", self.page_list)
        self.page_list.blockSignals(False)
        self._stale_thumbs.update(self.pages)
        self._refresh_thumbnails()
        self.page_list.setCurrentRow(current)
        self._on_page_selected(current)

    def _on_page_selected(self, row):
        if 0 <= row < len(self.pages) and self.view.scene() is not self.pages[row]:
            self.view.setScene(self.pages[row])

    def add_page(self):
        page = PageScene(self.settings)
        self._connect_page(page)
        index = self.page_list.currentRow() + 1
        self.pages.insert(index, page)
        self._rebuild_page_list(index)
        self._set_dirty(True)

    def delete_page(self):
        index = self.page_list.currentRow()
        page = self.pages[index]
        if page.image_items():
            answer = QMessageBox.question(self, APP_NAME, f"Delete page {index + 1} and its images?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        del self.pages[index]
        if not self.pages:
            new_page = PageScene(self.settings)
            self._connect_page(new_page)
            self.pages.append(new_page)
        self._rebuild_page_list(min(index, len(self.pages) - 1))
        self._set_dirty(True)

    def move_page(self, delta):
        index = self.page_list.currentRow()
        target = index + delta
        if not 0 <= target < len(self.pages):
            return
        self.pages[index], self.pages[target] = self.pages[target], self.pages[index]
        self._rebuild_page_list(target)
        self._set_dirty(True)

    def _on_page_modified(self, page):
        self._set_dirty(True)
        self._stale_thumbs.add(page)
        self._thumb_timer.start()

    def _refresh_thumbnails(self):
        s = THUMB_SIZE / max(self.settings.width, self.settings.height)
        w = max(1, round(self.settings.width * s))
        h = max(1, round(self.settings.height * s))
        for page in self._stale_thumbs:
            if page in self.pages:
                image = render_page(page, w, h)
                self.page_list.item(self.pages.index(page)).setIcon(QIcon(QPixmap.fromImage(image)))
        self._stale_thumbs.clear()

    # ---- export ---------------------------------------------------------

    def _render_and_write(self, pages, write, target, scale=1.0):
        """Render pages from the full-resolution files and pass each to write(index, image).

        `scale` < 1 renders smaller pages (lower output DPI). Returns True if every page was written.
        """
        failed = []
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for index, page in enumerate(pages):
                self.statusBar().showMessage(f"Exporting page {index + 1} of {len(pages)}...")
                QApplication.processEvents()
                image, unreadable = render_page_full(page, scale)
                failed.extend(unreadable)
                try:
                    write(index, image)
                except (OSError, ValueError) as e:
                    QApplication.restoreOverrideCursor()
                    QMessageBox.critical(self, APP_NAME, f"Could not write {target(index)}:\n{e}")
                    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                    return False
        finally:
            QApplication.restoreOverrideCursor()
            self.statusBar().clearMessage()
        if failed:
            QMessageBox.warning(self, APP_NAME, "These images could not be read at full resolution; "
                                "their previews were used instead:\n\n" + "\n".join(failed))
        return True

    def _export_pages(self, pages, paths):
        dpi = self.settings.dpi
        return self._render_and_write(pages, lambda i, image: save_page_image(image, paths[i], dpi),
                                      lambda i: paths[i])

    def _ensure_saved(self):
        """Exports go next to the album file, so it must have been saved. Returns False if cancelled."""
        if self.file_path:
            return True
        answer = QMessageBox.question(self, APP_NAME, "Exports are written to the album's folder. "
                                      "Save the album now?")
        return answer == QMessageBox.StandardButton.Yes and self.save_as()

    def _confirm_overwrite(self, paths):
        existing = [os.path.basename(p) for p in paths if os.path.exists(p)]
        if not existing:
            return True
        answer = QMessageBox.question(self, APP_NAME, "Overwrite existing files?\n\n" + "\n".join(existing))
        return answer == QMessageBox.StandardButton.Yes

    def _album_stem(self):
        """Album folder and file name without extension."""
        return (os.path.dirname(os.path.abspath(self.file_path)),
                os.path.splitext(os.path.basename(self.file_path))[0])

    def _export_paths(self, indices):
        """Output paths next to the album file: <album name>_<page number>.<format>.

        Returns None if cancelled.
        """
        if not self._ensure_saved():
            return None
        formats = ["jpg", "png", "tif"]
        last = self.prefs.value("export_format", "jpg")
        fmt, ok = QInputDialog.getItem(self, "Export Format", "Image format:", formats,
                                       formats.index(last) if last in formats else 0, False)
        if not ok:
            return None
        self.prefs.setValue("export_format", fmt)
        directory, stem = self._album_stem()
        paths = [os.path.join(directory, f"{stem}_{i + 1:03d}.{fmt}") for i in indices]
        return paths if self._confirm_overwrite(paths) else None

    def export_page(self):
        index = self.page_list.currentRow()
        paths = self._export_paths([index])
        if paths and self._export_pages([self.page], paths):
            self.statusBar().showMessage(f"Exported {paths[0]}", 5000)

    def export_all(self):
        paths = self._export_paths(range(len(self.pages)))
        if paths and self._export_pages(self.pages, paths):
            self.statusBar().showMessage(f"Exported {len(paths)} page(s) to {os.path.dirname(paths[0])}", 5000)

    def export_pdf(self):
        """All pages into <album name>.pdf next to the album, one PDF page per album page."""
        if not self._ensure_saved():
            return
        s = self.settings
        width_cm, height_cm = s.physical_size_cm()
        # Output resolutions up to the album's own; lower ones give smaller files, same page size.
        choices = [s.dpi] + [d for d in PDF_DPI_CHOICES if d < s.dpi]
        labels = []
        for d in choices:
            f = d / s.dpi
            quality = "full quality" if d == s.dpi else "smaller file"
            labels.append(f"{d} DPI - {quality} ({round(s.width * f)} x {round(s.height * f)} px per page)")
        last = int(self.prefs.value("pdf_dpi", s.dpi))
        label, ok = QInputDialog.getItem(
            self, "Export PDF", f"Pages print at {width_cm:.1f} x {height_cm:.1f} cm.\nOutput resolution:",
            labels, choices.index(last) if last in choices else 0, False)
        if not ok:
            return
        dpi = choices[labels.index(label)]
        self.prefs.setValue("pdf_dpi", dpi)
        directory, stem = self._album_stem()
        path = os.path.join(directory, f"{stem}.pdf")
        if not self._confirm_overwrite([path]):
            return
        # Write to a temporary file so a failed export doesn't leave a truncated PDF behind.
        temp = path + ".part"
        page_width_inches = s.width / s.dpi
        written = self._render_and_write(
            self.pages, lambda i, image: append_pdf_page(image, temp, page_width_inches, stem, first=i == 0),
            lambda i: path, scale=dpi / s.dpi)
        if not written:
            if os.path.exists(temp):
                os.remove(temp)
            return
        os.replace(temp, path)
        self.statusBar().showMessage(f"Exported {path} ({len(self.pages)} page(s), "
                                     f"{width_cm:.1f} x {height_cm:.1f} cm at {dpi} DPI)", 8000)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    QImageReader.setAllocationLimit(2048)   # MB; the default 256 rejects very large photos
    window = MainWindow()
    if len(sys.argv) > 1:
        default = DocumentSettings(3508, 2480, QColor(255, 255, 255))
        window.set_document(default, [PageScene(default)])
        window.show()       # first, so the loading progress appears over the window
        window.open_document(sys.argv[1])
    else:
        dialog = NewDocumentDialog()
        if not dialog.exec():
            return 0
        settings = dialog.settings()
        window.set_document(settings, [PageScene(settings)])
    window.show()
    return app.exec()
