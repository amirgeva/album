"""Full-quality page rendering for the output files, using Pillow.

Each source photo is decoded at full resolution, oriented per EXIF, converted to sRGB,
and resampled exactly once with Lanczos directly to its whole-pixel size on the page.
"""
import io

from PIL import Image, ImageCms, ImageOps
from PyQt6.QtGui import QImage

from .page import load_image

Image.MAX_IMAGE_PIXELS = 500_000_000    # allow large panoramas without the decompression-bomb error

_SRGB = ImageCms.createProfile("sRGB")
SRGB_ICC = ImageCms.ImageCmsProfile(_SRGB).tobytes()


def qimage_to_pil(qimage):
    qimage = qimage.convertToFormat(QImage.Format.Format_RGBA8888)
    bits = qimage.constBits()
    bits.setsize(qimage.sizeInBytes())
    return Image.frombuffer("RGBA", (qimage.width(), qimage.height()), bytes(bits),
                            "raw", "RGBA", qimage.bytesPerLine(), 1)


def open_source(path):
    """Open a photo as an RGB or RGBA image in sRGB. Returns None if it can't be read."""
    try:
        img = Image.open(path)
        img.load()
    except (OSError, ValueError, Image.DecompressionBombError):
        # Formats Pillow can't decode but Qt can.
        qimage = load_image(path)
        return qimage_to_pil(qimage) if qimage is not None else None

    icc = img.info.get("icc_profile")
    img = ImageOps.exif_transpose(img)

    if img.mode in ("I;16", "I;16B", "I;16L", "I"):         # 16-bit grayscale
        img = img.convert("I").point(lambda v: v * (1 / 256)).convert("L")

    alpha = None
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        alpha = img.getchannel("A")
        img = img.convert("RGB")

    if icc and img.mode in ("RGB", "CMYK", "L"):
        try:
            img = ImageCms.profileToProfile(img, ImageCms.ImageCmsProfile(io.BytesIO(icc)), _SRGB,
                                            renderingIntent=ImageCms.Intent.PERCEPTUAL, outputMode="RGB")
        except (ImageCms.PyCMSError, OSError, ValueError):
            pass    # broken profile: fall back to treating the pixels as sRGB
    img = img.convert("RGB")

    if alpha is not None:
        img.putalpha(alpha)
    return img


def resize(img, size):
    if img.size == size:
        return img
    if img.mode == "RGBA":
        # Premultiply so transparent pixels don't bleed their color into the edges.
        return img.convert("RGBa").resize(size, Image.Resampling.LANCZOS).convert("RGBA")
    return img.resize(size, Image.Resampling.LANCZOS)


def render_page_full(scene, scale=1.0):
    """Render the page from the full-resolution files. Returns (PIL image, list of unreadable paths).

    With scale < 1 the page is rendered smaller (e.g. a lower-DPI PDF): each photo is still
    resampled only once, directly from the original to its scaled whole-pixel size.
    """
    width, height = round(scene.page_width * scale), round(scene.page_height * scale)
    canvas = Image.new("RGB", (width, height), scene.background.name())
    failed = []
    for item in scene.image_items():
        x0, y0, w0, h0 = item.int_rect()
        # Scale the edges, not the size, so neighbouring images keep their spacing.
        x, y = round(x0 * scale), round(y0 * scale)
        w = max(1, round((x0 + w0) * scale) - x)
        h = max(1, round((y0 + h0) * scale) - y)
        # Skip items entirely off the page.
        if x >= width or y >= height or x + w <= 0 or y + h <= 0:
            continue
        img = None if item.missing else open_source(item.path)
        if img is None:
            failed.append(item.path)
            img = qimage_to_pil(item.display)   # best effort: the preview / placeholder
        elif item.is_cropped():
            c = item.crop
            sw, sh = img.size
            img = img.crop((round(c.left() * sw), round(c.top() * sh),
                            round(c.right() * sw), round(c.bottom() * sh)))
        img = resize(img, (w, h))
        # Crop to the visible part so the paste box lies within the canvas.
        left, top = max(0, -x), max(0, -y)
        right, bottom = min(w, width - x), min(h, height - y)
        if (left, top, right, bottom) != (0, 0, w, h):
            img = img.crop((left, top, right, bottom))
        canvas.paste(img, (x + left, y + top), img if img.mode == "RGBA" else None)
    return canvas, failed


def append_pdf_page(img, path, page_width_inches, title, first):
    """Add one page to a PDF, creating the file for the first page.

    Pages are written one at a time so a long album never has to be held in memory at once.
    The page is printed `page_width_inches` wide whatever its pixel count; images are stored
    as high-quality JPEG.
    """
    # The title only on the first page: Pillow would repeat it in the metadata on every append.
    extra = {"title": title} if first else {}
    img.save(path, "PDF", append=not first, resolution=img.width / page_width_inches,
             quality=95, subsampling=0, **extra)


def save_page_image(img, path, dpi):
    """Save at maximum quality, tagged as sRGB and with the print DPI.

    Raises OSError / ValueError on failure.
    """
    ext = path.lower().rsplit(".", 1)[-1]
    common = {"icc_profile": SRGB_ICC, "dpi": (dpi, dpi)}
    if ext in ("jpg", "jpeg"):
        # 4:4:4 chroma (no subsampling) keeps fine colored detail sharp.
        img.save(path, "JPEG", quality=95, subsampling=0, optimize=True, **common)
    elif ext == "png":
        img.save(path, "PNG", **common)
    elif ext in ("tif", "tiff"):
        img.save(path, "TIFF", compression="tiff_lzw", **common)
    else:
        img.save(path, **common)
