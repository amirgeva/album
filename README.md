# Photo Album

A desktop application for composing photo album pages. Place photos on fixed-size pages,
arrange, crop and layer them, then export print-ready images or a multi-page PDF rendered
from the full-resolution originals.

Built with Python, PyQt6 (editor and preview) and Pillow (final rendering).

## Features

- **Fixed-size pages** — every page in an album shares the same pixel size, print
  resolution (DPI), margin and background colour. Presets are provided for A4, A3, square
  prints and common screen aspect ratios, or enter a custom size.
- **Free placement** — drag images to move them, drag a corner handle to resize (aspect
  ratio is always kept). Positions and sizes are always whole pixels, and images can never
  leave the area inside the page margin. Multi-selected images move as one block.
- **Automatic layout** — newly added images are arranged into justified rows or columns
  that fill the page, with an even gap between them. The layout tries every row/column
  count in both orientations and picks the one that covers the most area.
- **Align to grid** — snaps a roughly aligned arrangement into an exact grid of equal cells.
- **Cropping** — double-click an image to open the crop dialog, with free cropping or
  locked aspect ratios (original, 1:1, 3:2, 4:3, 16:9, their portrait versions, or the
  page's own ratio) and a rule-of-thirds overlay. Crops are non-destructive.
- **Layering** — bring to front / send to back for overlapping images.
- **Centering** — center the selection horizontally, vertically or both.
- **Multiple pages** — page thumbnails in a side panel; add, delete and reorder pages.
- **Duplicate detection** — when adding a photo already used elsewhere in the album, you
  are asked whether to insert a copy or skip it.
- **High-quality export**
  - Each page as JPEG (quality 95, no chroma subsampling), PNG or LZW-compressed TIFF,
    tagged with the sRGB profile and the album's DPI.
  - The whole album as a single PDF, at full resolution or a lower DPI for smaller files
    (the physical page size stays the same). Pages are written one at a time, so long
    albums don't need to fit in memory.
  - Export renders from the original files, not the previews: each photo is decoded at
    full resolution, oriented per EXIF, colour-converted from its embedded ICC profile to
    sRGB, and resampled exactly once with Lanczos to its final size. Transparency is
    handled with premultiplied alpha so edges don't fringe.
- **Robust loading** — EXIF orientation is honoured everywhere, very large photos and
  panoramas are supported, long loads show a cancellable progress dialog, and images that
  have gone missing are shown as placeholders so the layout is preserved.

## Requirements

- Python 3.10 or later
- [PyQt6](https://pypi.org/project/PyQt6/) 6.5+
- [Pillow](https://pypi.org/project/pillow/) 10.0+

## Installation

```sh
git clone https://github.com/amirgeva/album.git
cd album
python -m pip install -r requirements.txt
```

## Running

```sh
python main.py                    # start with the New Album dialog
python main.py my_album.album     # open an existing album
```

### Building a standalone Windows executable

`build_exe.bat` uses [PyInstaller](https://pyinstaller.org/) to produce a single
self-contained `PhotoAlbum.exe` (no Python installation needed to run it):

```bat
python -m pip install -r requirements.txt pyinstaller
build_exe.bat
```

The executable is written to the project folder; intermediate files go to `build\`.

## Usage

1. **Create an album** — choose a preset or a custom page size, the print resolution, the
   margin and the background colour. The dialog shows the aspect ratio, megapixels and the
   printed size in centimetres.
2. **Add images** (`Ctrl+I`, or right-click an empty part of the page). They are laid out
   automatically on the current page.
3. **Arrange** — move, resize, crop and layer images with the mouse, the context menu and
   the shortcuts below.
4. **Add more pages** (`Ctrl+Shift+N`) and reorder them as needed.
5. **Save** the album (`Ctrl+S`) and **export** pages as images or the whole album as a PDF.
   Exports are written next to the `.album` file as `<album name>_001.jpg`, … or
   `<album name>.pdf`.

The margin and print resolution can be changed later from the **Page** menu. Changing the
DPI only changes the printed size; the pixel layout stays the same.

### Mouse

| Action | Effect |
| --- | --- |
| Drag an image | Move it (or the whole selection) |
| Drag a corner handle | Resize, keeping the aspect ratio |
| Drag on empty space | Rubber-band selection |
| Double-click an image | Crop |
| Right-click | Context menu (arrange, align, center, crop, layer, remove) |
| Ctrl + wheel | Zoom |
| Middle-button drag | Pan |

### Keyboard shortcuts

| Shortcut | Command |
| --- | --- |
| `Ctrl+N` | New album |
| `Ctrl+O` | Open album |
| `Ctrl+S` / `Ctrl+Shift+S` | Save / Save as |
| `Ctrl+E` | Export current page |
| `Ctrl+Shift+E` | Export all pages |
| `Ctrl+Shift+P` | Export PDF |
| `Ctrl+I` | Add images |
| `Delete` | Remove selected images |
| `Ctrl+A` | Select all |
| `Ctrl+]` / `Ctrl+[` | Bring to front / Send to back |
| `Ctrl+G` | Auto arrange (selection if more than one image, otherwise the page) |
| `Ctrl+Shift+G` | Align to grid (same targeting) |
| `Ctrl+Shift+N` | Add page |
| `Ctrl+Shift+Up` / `Ctrl+Shift+Down` | Move page up / down |
| `Ctrl+0` | Fit page in view |
| `Ctrl++` / `Ctrl+-` | Zoom in / out |
| `Ctrl+Q` | Exit |

## The `.album` file format

Albums are saved as human-readable JSON. Image paths are stored relative to the album
file when possible, so an album folder can be moved or copied together with its photos.

```json
{
  "version": 1,
  "width": 3508,
  "height": 2480,
  "background": "#ffffff",
  "margin": 100,
  "dpi": 300,
  "pages": [
    {
      "items": [
        {"x": 100, "y": 100, "w": 1600, "h": 1067, "path": "photos/IMG_0001.jpg", "z": 1.0},
        {"x": 1708, "y": 100, "w": 1600, "h": 1200, "path": "photos/IMG_0002.jpg", "z": 2.0,
         "crop": [0.1, 0.0, 0.9, 0.8]}
      ]
    }
  ]
}
```

- `x`, `y`, `w`, `h` — position and size on the page, in output pixels.
- `z` — stacking order (higher is on top).
- `crop` — optional; the kept part of the photo as `[left, top, right, bottom]` fractions
  of its width and height, so it applies equally to previews and full-resolution files.

## Project structure

```
main.py              Entry point
build_exe.bat        PyInstaller build script for Windows
album/
  main_window.py     Main window, menus, page list, file and export commands
  page.py            Page scene, image items, layout algorithms, preview rendering
  dialogs.py         New album dialog and crop dialog
  document.py        Document settings and .album load / save
  export.py          Full-resolution rendering and image / PDF output (Pillow)
```

## License

Licensed under the GNU General Public License v3.0. See [LICENSE](LICENSE).
