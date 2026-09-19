"""Provider image budget regressions without external API calls."""
import base64
import cv2
import numpy as np
from kicadgen.services.photo_inventory import image_block


def test_large_png_is_compressed_without_losing_pixel_dimensions():
    pixels = np.random.default_rng(42).integers(0, 256, (1200, 1600, 3), dtype=np.uint8)
    url = image_block(pixels)['image_url']['url']
    header, data = url.split(',', 1)
    assert header == 'data:image/jpeg;base64'
    assert len(data) <= 5 * 1024 * 1024
    decoded = cv2.imdecode(np.frombuffer(base64.b64decode(data), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape == pixels.shape


def test_small_png_remains_lossless():
    pixels = np.zeros((40, 80, 3), dtype=np.uint8)
    url = image_block(pixels)['image_url']['url']
    assert url.startswith('data:image/png;base64,')
    decoded = cv2.imdecode(np.frombuffer(base64.b64decode(url.split(',', 1)[1]), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert np.array_equal(decoded, pixels)
