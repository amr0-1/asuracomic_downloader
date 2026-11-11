# utils/converter.py
"""
Image compilation helpers.

The functions here are **pure and blocking** - they take bytes that are
already in memory and turn them into a PDF or a CBZ.  Nothing here touches
the network, and nothing here is async; the async engine in
:mod:`core.downloader` calls them through :func:`asyncio.to_thread` so the
CPU-bound work never blocks the event loop.

Pillow is imported lazily *inside* the functions that need it, so that
merely importing this module does not pull the imaging library into a
process that is only, say, listing chapters.
"""

from __future__ import annotations

import io
import os
import zipfile
from collections.abc import Sequence
from typing import Final

__all__ = [
    "PDF_MAX_PAGE_UNITS",
    "PDF_BASE_RESOLUTION",
    "PDF_JPEG_QUALITY",
    "build_pdf_from_bytes",
    "build_cbz_from_bytes",
    "get_image_files",
    "delete_images",
]

#: The PDF specification caps a page's MediaBox at 14400 units (200 inches).
#: A single webtoon page can be 900x16178 pixels, which at 72 DPI would be
#: 16178 units tall and therefore invalid.  We scale the resolution down
#: just enough to stay inside the limit.
PDF_MAX_PAGE_UNITS: Final[float] = 14400.0
PDF_BASE_RESOLUTION: Final[float] = 72.0

#: JPEG quality used when Pillow re-encodes pages for the PDF container.
#: Pillow's PDF writer always emits DCTDecode (JPEG), so WebP pages are
#: transcoded on the way in.
PDF_JPEG_QUALITY: Final[int] = 90


def _fit_resolution(width: int, height: int) -> float:
    """
    Return a DPI that keeps the page inside the PDF MediaBox limit.

    The aspect ratio is preserved - only the physical page size shrinks.
    """
    longest = max(width, height)
    if longest <= 0:
        return PDF_BASE_RESOLUTION
    needed = longest * PDF_BASE_RESOLUTION / PDF_MAX_PAGE_UNITS
    return max(PDF_BASE_RESOLUTION, needed)


def build_pdf_from_bytes(
    pages: Sequence[tuple[bytes, str]],
    output_path: str,
    *,
    quality: int = PDF_JPEG_QUALITY,
) -> str:
    """
    Build a PDF from in-memory image bytes.

    This is a **blocking, CPU-bound** function.  Callers must run it inside
    :func:`asyncio.to_thread` so it cannot stall the event loop.

    Pages are written incrementally with Pillow's ``append`` mode, which
    keeps only ONE decoded page resident at a time.  That matters a lot
    for webtoon pages: a single 900x16178 image decodes to ~42 MB of RGB,
    so holding a whole chapter at once costs ~890 MB, versus ~250 MB for
    the incremental approach.

    Args:
        pages: ``(image_bytes, extension)`` pairs, in reading order.
        output_path: where to write the PDF.
        quality: JPEG quality for the transcoded pages.

    Returns:
        The path that was written.
    """
    from PIL import Image, ImageFile

    ImageFile.LOAD_TRUNCATED_IMAGES = True

    if not pages:
        raise ValueError("No pages to compile.")

    buffer = io.BytesIO()
    first = True
    for data, _extension in pages:
        try:
            with Image.open(io.BytesIO(data)) as source:
                image = source.convert("RGB")
        except Exception:
            # A single corrupt page should not sink the whole chapter.
            continue
        try:
            if first:
                image.save(
                    buffer,
                    format="PDF",
                    resolution=_fit_resolution(image.width, image.height),
                    quality=quality,
                )
                first = False
            else:
                image.save(
                    buffer,
                    format="PDF",
                    append=True,
                    resolution=_fit_resolution(image.width, image.height),
                    quality=quality,
                )
        finally:
            image.close()

    if first:
        raise ValueError("None of the supplied pages could be decoded as images.")

    # ``getvalue()`` copies the whole buffer; ``getbuffer()`` hands out a
    # memoryview instead, which avoids duplicating a 20 MB PDF in RAM.
    with open(output_path, "wb") as handle:
        handle.write(buffer.getbuffer())
    return output_path


def build_cbz_from_bytes(
    pages: Sequence[tuple[bytes, str]],
    output_path: str,
) -> str:
    """
    Build a CBZ archive from in-memory image bytes.

    A CBZ is just a ZIP of images, so this needs no image library at all -
    the original bytes are stored verbatim, with their true extension.

    This is a blocking, CPU-bound function; run it in a thread.

    Returns:
        The path that was written.
    """
    if not pages:
        raise ValueError("No pages to compile.")

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for index, (data, extension) in enumerate(pages, start=1):
            archive.writestr(f"{index:03d}{extension}", data)
    return output_path


def get_image_files(directory: str) -> list[str]:
    """
    Return a naturally sorted list of image files inside *directory*.
    """
    image_files = []
    for name in os.listdir(directory):
        if name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            image_files.append(os.path.join(directory, name))
    from natsort import natsorted

    return natsorted(image_files)


def delete_images(image_files: Sequence[str]) -> None:
    """
    Delete the given image files, ignoring the ones that cannot be removed.
    """
    for image_file in image_files:
        try:
            os.remove(image_file)
        except OSError:
            pass
