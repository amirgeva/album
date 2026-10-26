"""Document settings and the .album file format (JSON)."""
import json
import os
from dataclasses import dataclass

from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QColor

from .page import PageScene

FILE_VERSION = 1
FILE_FILTER = "Photo Album (*.album)"


@dataclass
class DocumentSettings:
    """Fixed for the whole document: every page shares the same size and background."""
    width: int
    height: int
    background: QColor
    margin: int = 0         # pixels on every side that images may not enter
    dpi: int = 300          # print resolution: together with the pixel size it sets the physical size

    def physical_size_cm(self):
        return self.width / self.dpi * 2.54, self.height / self.dpi * 2.54


def _relative(path, base_dir):
    try:
        return os.path.relpath(path, base_dir)
    except ValueError:      # different drive on Windows
        return path


def _item_data(item, base_dir):
    data = dict(zip(("x", "y", "w", "h"), item.int_rect()), path=_relative(item.path, base_dir), z=item.zValue())
    if item.is_cropped():
        c = item.crop
        data["crop"] = [c.left(), c.top(), c.right(), c.bottom()]     # fractions of the image size
    return data


def save_album(path, settings, pages):
    base_dir = os.path.dirname(os.path.abspath(path))
    data = {
        "version": FILE_VERSION,
        "width": settings.width,
        "height": settings.height,
        "background": settings.background.name(),
        "margin": settings.margin,
        "dpi": settings.dpi,
        "pages": [
            {"items": [
                _item_data(item, base_dir)
                for item in page.image_items()
            ]}
            for page in pages
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_album(path, progress=None):
    """Returns (settings, pages, missing_paths), or None if cancelled.

    progress(done, total, image_path) is called before each image is loaded;
    returning False cancels.
    """
    base_dir = os.path.dirname(os.path.abspath(path))
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    settings = DocumentSettings(int(data["width"]), int(data["height"]), QColor(data["background"]),
                                int(data.get("margin", 0)), int(data.get("dpi", 300)))
    total = sum(len(p.get("items", [])) for p in data.get("pages", []))
    done = 0
    pages, missing = [], []
    for page_data in data.get("pages", []):
        page = PageScene(settings)
        for d in page_data.get("items", []):
            item_path = d["path"]
            if not os.path.isabs(item_path):
                item_path = os.path.normpath(os.path.join(base_dir, item_path))
            if progress and not progress(done, total, item_path):
                return None
            done += 1
            crop = d.get("crop")
            if crop:
                crop = QRectF(QPointF(crop[0], crop[1]), QPointF(crop[2], crop[3]))
            item = page.add_image_item(item_path, QRectF(d["x"], d["y"], d["w"], d["h"]), d.get("z", 0), crop)
            if item.missing:
                missing.append(item_path)
        pages.append(page)
    if not pages:
        pages.append(PageScene(settings))
    return settings, pages, missing
