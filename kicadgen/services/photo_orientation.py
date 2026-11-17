"""@brief Decode a photo or a drawing upright, with the EXIF orientation the AI request applies.

Phones store the pixels one way and the right way in the EXIF orientation
tag. The document request (`datasheet_extractor._upright_image_bytes`) reads
that tag with Pillow and rotates the image before sending it, so the model
always sees it upright. Every local reader of the same file (board
calibration, drawn-hole witness) must decode the SAME pixels, or a phone photo
of a drawing is checked sideways while the model reads it upright (review
2026-11-16, R8-1: `drawing_holes._read_image` decoded with
`cv2.IMREAD_UNCHANGED`, which ignores the tag).

The tag is read here exactly as the request reads it (Pillow's `getexif`),
and applied explicitly with the transforms of Pillow's
`ImageOps.exif_transpose`: whether a given OpenCV decoder applies the tag by
itself (JPEG/PNG/WebP with `IMREAD_COLOR` do, `IMREAD_UNCHANGED` does not,
TIFF always does) no longer decides what the program sees.
"""
from __future__ import annotations

from io import BytesIO

#: EXIF tag holding the orientation (1..8), as Pillow and the request read it.
EXIF_ORIENTATION_TAG = 0x0112


def exif_orientation(data) -> int:
    """@brief The EXIF orientation of encoded image bytes, read as the AI request reads it.

    @param data Encoded image (bytes, bytearray or a uint8 NumPy array).
    @return 1..8; 1 (upright, nothing to apply) when there is no tag, the tag
        is not an integer in 1..8, or Pillow cannot open the header — the
        same outcome as `datasheet_extractor._upright_image_bytes`, which then
        sends the pixels unrotated.
    """
    try:
        from PIL import Image
        raw = data.tobytes() if hasattr(data, "tobytes") else bytes(data)
        with Image.open(BytesIO(raw)) as image:
            value = image.getexif().get(EXIF_ORIENTATION_TAG, 1)
    except Exception:  # noqa: BLE001 - an unreadable header means «no tag», as in the request
        return 1
    if isinstance(value, bool) or not isinstance(value, int) or value not in range(1, 9):
        return 1
    return value


def apply_orientation(pixels, orientation: int):
    """@brief Turn decoded pixels upright according to an EXIF orientation.

    @param pixels H x W (x C) NumPy array decoded WITHOUT the tag applied.
    @param orientation EXIF orientation 1..8.
    @return A C-contiguous array; the input itself for orientation 1.
    @note Same transforms as Pillow's `ImageOps.exif_transpose` and as
        `datasheet_extractor._oriented_with_opencv`.
    """
    import numpy as np
    if orientation == 1:
        return pixels
    swap = (1, 0) + tuple(range(2, pixels.ndim))
    transforms = {
        2: lambda a: a[:, ::-1],                      # mirror left-right
        3: lambda a: a[::-1, ::-1],                   # rotate 180
        4: lambda a: a[::-1, :],                      # mirror top-bottom
        5: lambda a: a.transpose(swap),               # main diagonal
        6: lambda a: np.rot90(a, -1),                 # rotate 90 clockwise
        7: lambda a: a[::-1, ::-1].transpose(swap),   # anti-diagonal
        8: lambda a: np.rot90(a, 1),                  # rotate 90 counter-clockwise
    }
    transform = transforms.get(orientation)
    return pixels if transform is None else np.ascontiguousarray(transform(pixels))


def _pillow_upright(raw: bytes, keep_alpha: bool):
    """The image upright through Pillow's own `exif_transpose`, as BGR(A); None when Pillow cannot decode it."""
    try:
        import numpy as np
        from PIL import Image, ImageOps
        with Image.open(BytesIO(raw)) as picture:
            upright = ImageOps.exif_transpose(picture)
            pixels = np.asarray(upright.convert("RGBA" if keep_alpha else "RGB"))
    except Exception:  # noqa: BLE001 - any decoder failure falls back to OpenCV
        return None
    # RGB(A) -> BGR(A): the channel order of every OpenCV reader of the pipeline.
    order = [2, 1, 0, 3] if keep_alpha else [2, 1, 0]
    return np.ascontiguousarray(pixels[:, :, order])


def decode_upright(data, *, keep_alpha: bool = False):
    """@brief Decode encoded image bytes with their EXIF orientation applied, as the AI sees them.

    @param data Encoded image (bytes, bytearray or a uint8 NumPy array).
    @param keep_alpha False: 8-bit BGR. True: the image as stored
        (`cv2.IMREAD_UNCHANGED`: alpha and bit depth kept) when it is upright,
        8-bit BGRA when it is rotated.
    @return The decoded array, or None when OpenCV cannot decode the bytes
        (the caller decides on another decoder, e.g. Pillow for GIF).
    @note An upright image (no tag for Pillow) is decoded by OpenCV asked to
        IGNORE any tag (`IMREAD_IGNORE_ORIENTATION`), so a tag only OpenCV
        understands cannot make the program and the model see two different
        pictures. A rotated image is decoded by Pillow with its own
        `exif_transpose`, exactly as the request rotates it; when Pillow
        reads the tag but cannot decode the pixels (a truncated phone photo),
        OpenCV decodes them and the same transform is applied, as
        `datasheet_extractor._oriented_with_opencv`.
    """
    import cv2
    import numpy as np
    raw = data.tobytes() if hasattr(data, "tobytes") else bytes(data)
    encoded = np.frombuffer(raw, dtype=np.uint8)
    orientation = exif_orientation(raw)
    if orientation == 1:
        if keep_alpha:
            # IMREAD_UNCHANGED ignores the tag for JPEG/PNG/WebP; IMREAD_COLOR
            # would apply a tag Pillow does not see, hence the explicit flag.
            return cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
        return cv2.imdecode(encoded, cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    # A rotated image is decoded by Pillow, the library that read the tag:
    # OpenCV's TIFF decoder applies the tag even with IMREAD_IGNORE_ORIENTATION,
    # so applying it again on top would turn the picture twice.
    upright = _pillow_upright(raw, keep_alpha)
    if upright is not None:
        return upright
    pixels = cv2.imdecode(encoded, cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    return None if pixels is None else apply_orientation(pixels, orientation)
