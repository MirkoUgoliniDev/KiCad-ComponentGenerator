"""@brief No document image leaves the computer with its metadata (audit 2026-10-15, security M2).

A phone photo held upright (EXIF orientation 1, or no orientation tag) used to
be sent byte for byte, GPS position and device make included. These tests
build synthetic photos carrying GPS, maker, XMP, ICC and comment metadata in a
temporary directory and inspect the content block that would be uploaded:
no network call is made (the pin-type path uses a fake `call_tool`).
"""
import base64
from io import BytesIO

import pytest
from PIL import Image, ImageOps

from kicadgen.services import datasheet_extractor as de
from kicadgen.services import pin_type_ai

#: EXIF tags used by the fixtures.
_ORIENTATION, _MAKE, _GPS_IFD = 0x0112, 0x010F, 0x8825
#: A fake XMP packet and ICC profile: only their presence is checked.
_XMP = b'<x:xmpmeta xmlns:x="adobe:ns:meta/">PhoneMakerXMP</x:xmpmeta>'
_ICC = b"FAKE-ICC-PhoneMaker"


def _pattern(width=40, height=20):
    """@brief An asymmetric picture: a red block in the top-left corner shows any rotation or flip."""
    image = Image.new("RGB", (width, height), (0, 128, 0))
    image.paste((255, 0, 0), (0, 0, width // 4, height // 4))
    return image


def _exif(orientation):
    """@brief EXIF with maker and GPS position, plus the orientation tag when given."""
    exif = Image.Exif()
    if orientation is not None:
        exif[_ORIENTATION] = orientation
    exif[_MAKE] = "PhoneMaker"
    exif[_GPS_IFD] = {1: "N", 2: (44.0, 30.0, 0.0), 3: "E", 4: (11.0, 20.0, 0.0)}
    return exif.tobytes()


def _phone_photo(path, fmt, orientation):
    """@brief Save the pattern with every metadata block the format can carry."""
    options = {"exif": _exif(orientation)}
    if fmt == "JPEG":
        options.update(quality=90, icc_profile=_ICC, comment=b"PhoneMaker comment")
    elif fmt == "PNG":
        options.update(icc_profile=_ICC)
    elif fmt == "WEBP":
        options.update(quality=90, icc_profile=_ICC, xmp=_XMP)
    _pattern().save(path, fmt, **options)
    return path


def _payload(block):
    """@brief The bytes carried by a content block's data URL."""
    url = block["image_url"]["url"]
    return base64.b64decode(url.split(",", 1)[1])


def _assert_no_metadata(data):
    """@brief Neither EXIF (GPS, maker, orientation), XMP, ICC nor comment survive."""
    assert b"PhoneMaker" not in data  # catches maker, XMP, ICC and comment in any container
    with Image.open(BytesIO(data)) as sent:
        exif = sent.getexif()
        assert _MAKE not in exif
        assert not dict(exif.get_ifd(_GPS_IFD))
        assert exif.get(_ORIENTATION, 1) == 1  # never rotated twice by the receiver
        for key in ("icc_profile", "xmp", "XML:com.adobe.xmp", "comment", "exif"):
            assert not sent.info.get(key), key


def _pixels(data):
    """@brief Decoded RGB pixels of encoded bytes."""
    with Image.open(BytesIO(data)) as image:
        return image.convert("RGB")


def _red_centre(image):
    """@brief Centre of the red block in fractions of width and height."""
    xs, ys = [], []
    for y in range(image.height):
        for x in range(image.width):
            r, g, _ = image.getpixel((x, y))
            if r > 180 and g < 80:
                xs.append(x)
                ys.append(y)
    assert xs, "red block lost"
    return ((sum(xs) / len(xs) + 0.5) / image.width, (sum(ys) / len(ys) + 0.5) / image.height)


SUFFIX = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
@pytest.mark.parametrize("orientation", [None, 1, 6])
@pytest.mark.parametrize("model", [None, de.DEFAULT_MODEL, "deepseek-chat"])
def test_uploaded_photo_carries_no_metadata(tmp_path, fmt, orientation, model):
    """Every format, orientation and provider: metadata gone, pixels as the analysis sees them."""
    path = _phone_photo(tmp_path / f"phone.{SUFFIX[fmt]}", fmt, orientation)
    sent = _payload(de._file_to_content_block(path, model))
    _assert_no_metadata(sent)
    with Image.open(path) as original:
        expected = ImageOps.exif_transpose(original).convert("RGB")
    received = _pixels(sent)
    # A provider that enlarges (DeepSeek) receives a bigger PNG and JPEG adds
    # noise: the orientation is compared through the proportions and the
    # relative position of the red block, not pixel by pixel.
    assert received.width / received.height == pytest.approx(expected.width / expected.height, rel=0.02)
    assert _red_centre(received) == pytest.approx(_red_centre(expected), abs=0.05)


@pytest.mark.parametrize("orientation", [None, 1])
def test_upright_jpeg_is_cleaned_without_reencoding(tmp_path, orientation):
    """The compressed pixels are copied, not re-encoded: same decoded pixels, smaller file."""
    path = _phone_photo(tmp_path / "phone.jpg", "JPEG", orientation)
    sent = _payload(de._file_to_content_block(path))
    assert len(sent) < len(path.read_bytes())
    with Image.open(path) as original:
        assert _pixels(sent).tobytes() == original.convert("RGB").tobytes()


def test_a_photo_without_metadata_is_sent_byte_for_byte(tmp_path):
    """Negative: nothing to remove -> the file is not touched (quality and size unchanged)."""
    path = tmp_path / "clean.jpg"
    _pattern().save(path, "JPEG", quality=90)
    assert _payload(de._file_to_content_block(path)) == path.read_bytes()
    png = tmp_path / "clean.png"
    _pattern().save(png, "PNG")
    assert _payload(de._file_to_content_block(png)) == png.read_bytes()


def test_data_appended_after_the_jpeg_end_is_not_sent(tmp_path):
    """Phones append a second picture (MPF) or a trailer with metadata after the end marker."""
    path = _phone_photo(tmp_path / "phone.jpg", "JPEG", 1)
    path.write_bytes(path.read_bytes() + b"\xff\xd8\xff\xe1PhoneMaker GPS trailer\xff\xd9")
    sent = _payload(de._file_to_content_block(path))
    _assert_no_metadata(sent)
    assert sent.endswith(b"\xff\xd9")


def test_progressive_jpeg_keeps_every_scan(tmp_path):
    """A progressive JPEG has several scans separated by markers: all of them are kept."""
    path = tmp_path / "progressive.jpg"
    _pattern(64, 32).save(path, "JPEG", quality=90, progressive=True, exif=_exif(1))
    sent = _payload(de._file_to_content_block(path))
    _assert_no_metadata(sent)
    with Image.open(path) as original:
        assert _pixels(sent).tobytes() == original.convert("RGB").tobytes()


def test_gif_comment_is_dropped(tmp_path):
    """Formats without a lossless cleaner are re-encoded from their bare pixels."""
    path = tmp_path / "drawing.gif"
    _pattern().convert("P").save(path, "GIF", comment=b"PhoneMaker comment")
    sent = _payload(de._file_to_content_block(path))
    _assert_no_metadata(sent)
    with Image.open(path) as original:
        assert _pixels(sent).tobytes() == original.convert("RGB").tobytes()


def _noisy_jpeg(path, orientation=1, size=(120, 60)):
    """@brief A JPEG whose scan is long enough to be cut in the middle, with GPS and maker."""
    import random
    random.seed(7)
    image = _pattern(*size)
    noise = Image.new("RGB", size)
    noise.putdata([(random.randrange(256),) * 3 for _ in range(size[0] * size[1])])
    Image.blend(image, noise, 0.2).save(path, "JPEG", quality=95, exif=_exif(orientation))
    return path


@pytest.mark.parametrize("cut", [2, 40, "half"])
def test_truncated_jpeg_is_cleaned_up_to_its_end(tmp_path, cut):
    """M1: no end-of-image marker, or a scan cut short: cleaned, never sent raw with its EXIF."""
    path = _noisy_jpeg(tmp_path / "phone.jpg")
    data = path.read_bytes()
    keep = len(data) - cut if isinstance(cut, int) else (data.find(b"\xff\xda") + len(data)) // 2
    path.write_bytes(data[:keep])
    sent = _payload(de._file_to_content_block(path))
    assert b"PhoneMaker" not in sent and sent.endswith(b"\xff\xd9")
    with Image.open(BytesIO(sent)) as image:
        assert image.size == (120, 60) and not dict(image.getexif())


def test_jpeg_cut_before_its_pixels_is_refused(tmp_path):
    """M1: only the header (with the GPS) is left: nothing to send, refused with the reason."""
    path = _noisy_jpeg(tmp_path / "phone.jpg")
    data = path.read_bytes()
    path.write_bytes(data[:data.find(b"\xff\xda")])
    with pytest.raises(de.ExtractionError, match="phone.jpg"):
        de._file_to_content_block(path)


@pytest.mark.parametrize("payload", [
    b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"PhoneMaker GPS" * 20,  # HEIC renamed
    b"\x89PNG\r\n\x1a\nnon-una-immagine PhoneMaker",                                 # broken PNG
])
def test_an_image_no_decoder_reads_is_refused_not_sent_raw(tmp_path, payload):
    """M1: fail closed - the raw bytes may carry metadata, so the upload is refused."""
    path = tmp_path / "phone.jpg"
    path.write_bytes(payload)
    with pytest.raises(de.ExtractionError, match="phone.jpg"):
        de._file_to_content_block(path)


def test_webp_private_and_c2pa_chunks_are_dropped(tmp_path):
    """M2: WebP is cleaned with an allow-list - a C2PA manifest or a private chunk never leaves."""
    import struct
    out = BytesIO()
    _pattern().save(out, "WEBP", lossless=True, exif=_exif(1), xmp=_XMP)
    data = out.getvalue()
    body = data[12:]
    for kind in (b"C2PA", b"priv"):
        content = b"PhoneMaker author=Mario Rossi"
        content += b"!" * (len(content) & 1)
        body += kind + struct.pack("<I", len(content)) + content
    path = tmp_path / "phone.webp"
    path.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body)
    sent = _payload(de._file_to_content_block(path))
    _assert_no_metadata(sent)
    assert b"Mario Rossi" not in sent and b"C2PA" not in sent
    with Image.open(path) as original:
        assert _pixels(sent).tobytes() == original.convert("RGB").tobytes()


def test_truncated_rotated_photo_is_rotated_by_the_other_decoder(tmp_path):
    """L7: PIL reads the tag but not the cut pixels; OpenCV rotates them, the tag is honoured."""
    path = _noisy_jpeg(tmp_path / "phone.jpg", orientation=6, size=(120, 60))
    path.write_bytes(path.read_bytes()[:-2])  # no end-of-image marker
    sent = _payload(de._file_to_content_block(path))
    assert b"PhoneMaker" not in sent
    with Image.open(BytesIO(sent)) as image:
        assert image.size == (60, 120)  # rotated as the analysis sees it
        assert image.getexif().get(0x0112, 1) == 1
        # Orientation 6 = 90 degrees clockwise: the red top-left block goes top-right.
        assert _red_centre(image.convert("RGB"))[0] > 0.5


def test_rotation_that_no_decoder_can_apply_is_refused(tmp_path, monkeypatch):
    """L7: with the tag unreadable by both decoders the photo is refused, never sent sideways."""
    path = _noisy_jpeg(tmp_path / "phone.jpg", orientation=6)
    path.write_bytes(path.read_bytes()[:-2])
    monkeypatch.setattr(de, "_oriented_with_opencv", lambda data, orientation: None)
    with pytest.raises(de.ExtractionError, match="rotazione"):
        de._file_to_content_block(path)


def test_pin_type_suggestions_upload_clean_photos(tmp_path, monkeypatch):
    """The pin-type proposal path shares the cleaned block builder (fake provider, no network)."""
    path = _phone_photo(tmp_path / "pinout.jpg", "JPEG", 1)
    captured = []

    def fake_call(**kwargs):
        captured.extend(kwargs["content_blocks"])
        return {"pins": []}

    monkeypatch.setattr(pin_type_ai, "make_logged_call", lambda call, progress, cancel=None: fake_call)
    monkeypatch.setattr(pin_type_ai, "ensure_providers_ready", lambda models, progress: None)
    pin_type_ai.suggest_pin_types("U1", [{"pin": 1, "name": "A"}], [path], "vendor/model")
    images = [block for block in captured if block.get("type") == "image_url"]
    assert len(images) == 1
    _assert_no_metadata(_payload(images[0]))
