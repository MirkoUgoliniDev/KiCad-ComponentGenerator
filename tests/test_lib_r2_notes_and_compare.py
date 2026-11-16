"""@brief Round-2 fixes of 2026-11-15: package notes in the file's language, chunked file comparison.

T5 (real trial): an English footprint description got an Italian tail about
estimated pads, and it reached the installed library. Low: the identical-file
check of the install read both STEP files whole into memory.
"""
from pathlib import Path

import pytest

from kicadgen.core.package_geometry import PackageGeometry, PinSpec, package_from_mapping
from kicadgen.generators import package as package_module
from kicadgen.generators.package import (ESTIMATED_BODY_CENTRE_NOTE, ESTIMATED_PADS_LISTED, ESTIMATED_PADS_MORE,
                                         ESTIMATED_PADS_NOTE, ESTIMATED_PADS_UNNAMED, build_package,
                                         estimated_pad_list)
from kicadgen.services import kicad_library

#: Italian words the package notes used to write into the file; none of them
#: is an English word (nor part of one: «stimat» is inside «estimated»),
#: so their absence is a structural witness.
_ITALIAN_NOTE_WORDS = ("stimato", "stimati", "piazzola", "verificare", "disegno", "senza numero", "e altri", "generico",
                       "campo dei pad")


def _thru(number, x, sources=None):
    """One through-hole pin mapping at (x, 0), with its provenance when given."""
    pin = {"number": number, "x_mm": x, "y_mm": 0.0, "pad_length_mm": 1.65, "pad_width_mm": 1.65,
           "pad_type": "thru_hole", "shape": "circle", "drill_mm": 0.85}
    if sources is not None:
        pin["sources"] = sources
    return pin


def _geometry(*pins, **fields):
    """A package mapping with the given pins, read through the real loader."""
    return package_from_mapping({"name": "PART", "family": "custom", "body_length_mm": 12.0,
                                 "body_width_mm": 6.0, "pins": list(pins), **fields})


# ── T5: one language in the footprint description ─────────────────────────

def test_an_english_description_with_estimated_pads_gets_an_english_note():
    """Positive: estimated pads append the note, in the description's English."""
    estimated = {"drill_mm": "estimated", "pad_size": "derived"}
    geometry = _geometry(*(_thru(str(n), 2.54 * n, estimated) for n in range(1, 6)), _thru("6", 20.0))
    description = build_package(geometry, description="Signal relay, 2 changeover contacts").description
    assert description == ("Signal relay, 2 changeover contacts; "
                           "hole or land estimated on pads 1-5: verify against the drawing")
    assert not any(word in description.lower() for word in _ITALIAN_NOTE_WORDS)


def test_read_pads_add_no_note_at_all():
    """Negative twin: the same part with every value read keeps the description untouched."""
    read = {"drill_mm": "read", "pad_size": "read", "shape": "read"}
    geometry = _geometry(*(_thru(str(n), 2.54 * n, read) for n in range(1, 7)))
    description = build_package(geometry, description="Signal relay, 2 changeover contacts").description
    assert description == "Signal relay, 2 changeover contacts"


def test_every_tail_the_generator_appends_is_english():
    """Every fixed piece the package generator writes: body centre, count tails, generic text."""
    pins = [PinSpec(str(n), 1.27 * n, 0.0, 0.6, 1.5, pad_type="smd", sources={"pad_size": "estimated"})
            for n in range(1, 31, 2)]
    unnamed = PinSpec("", 0.0, 5.0, 1.0, 1.0, pad_type="np_thru_hole", shape="circle", drill_mm=1.0,
                      sources={"drill_mm": "estimated"})
    # origin other than body_center: the body centre is estimated from the pads.
    geometry = PackageGeometry(name="P", family="soic", origin="pin1", pins=[*pins, unnamed])
    description = build_package(geometry).description
    assert description.startswith("soic generic package; " + ESTIMATED_BODY_CENTRE_NOTE)
    assert ESTIMATED_PADS_MORE.format(count=len(pins) - ESTIMATED_PADS_LISTED) in description
    assert ESTIMATED_PADS_UNNAMED.format(count=1) in description
    assert not any(word in description.lower() for word in _ITALIAN_NOTE_WORDS)
    # The language is declared once, and each template keeps its placeholder.
    # CONTRACT CHANGED 2026-11-15 (user decision: file notes in English): the
    # package's own PACKAGE_NOTE_LANGUAGE is gone, one file language holds
    # for every generator (core.footprint.FOOTPRINT_TEXT_LANGUAGE).
    from kicadgen.core.footprint import FOOTPRINT_TEXT_LANGUAGE
    assert FOOTPRINT_TEXT_LANGUAGE == "en" and not hasattr(package_module, "PACKAGE_NOTE_LANGUAGE")
    assert "{numbers}" in ESTIMATED_PADS_NOTE and "{count}" in ESTIMATED_PADS_MORE


def test_a_message_can_ask_the_pad_list_with_its_own_translated_tails():
    """The list keeps its compression and bound; only the tails change with the caller's templates."""
    numbers = [str(n) for n in range(1, 40, 2)] + [""]
    file_text = estimated_pad_list(numbers)
    shown = estimated_pad_list(numbers, more="e altri {count}", unnamed_text="{count} senza numero")
    left = len(numbers) - 1 - ESTIMATED_PADS_LISTED
    assert file_text.endswith(f"and {left} more, 1 unnumbered")
    assert shown.endswith(f"e altri {left}, 1 senza numero")
    # Negative twin: the entries themselves are the same text in both.
    assert file_text.split(" and ")[0] == shown.split(" e altri ")[0]


# ── chunked identical-file comparison ─────────────────────────────────────

class _ReadSpy:
    """Wraps an open binary file and records the size asked by each read()."""

    def __init__(self, handle, sizes):
        self._handle, self._sizes = handle, sizes

    def read(self, size=-1):
        """Record the requested size and read from the real file."""
        self._sizes.append(size)
        return self._handle.read(size)

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._handle.close()
        return False


@pytest.fixture
def read_sizes(monkeypatch):
    """Every read() size the comparison asks; whole-file reads are refused."""
    sizes: list = []
    real_open = Path.open

    def spy_open(self, mode="r", *args, **kwargs):
        return _ReadSpy(real_open(self, mode, *args, **kwargs), sizes)

    def no_whole_read(self):
        raise AssertionError(f"whole-file read of {self}")

    monkeypatch.setattr(Path, "open", spy_open)
    monkeypatch.setattr(Path, "read_bytes", no_whole_read)
    return sizes


def _big(path: Path, size: int, *, flip_at: int | None = None) -> Path:
    """A file of `size` pseudo-random bytes, with one byte changed at `flip_at`."""
    data = bytearray((index * 7919 + 13) % 251 for index in range(size))
    if flip_at is not None:
        data[flip_at] ^= 0xFF
    path.write_bytes(bytes(data))
    return path


def test_identical_large_files_are_equal_and_read_in_bounded_chunks(tmp_path, read_sizes):
    """Positive: a 5 MB STEP and its copy are the same; no read asks more than one chunk."""
    size = 5 * 1024 * 1024 + 123
    first, second = _big(tmp_path / "a.step", size), _big(tmp_path / "b.step", size)
    assert kicad_library._same_file_bytes(first, second) is True
    assert read_sizes and all(0 < asked <= kicad_library._COMPARE_CHUNK_BYTES for asked in read_sizes)
    # Both files read to the end: at least size / chunk reads per file.
    assert len(read_sizes) >= 2 * (size // kicad_library._COMPARE_CHUNK_BYTES)


def test_a_difference_stops_the_reading_at_the_first_differing_chunk(tmp_path, read_sizes):
    """Negative: one byte changed in the first chunk -> False after one read per file."""
    size = 5 * 1024 * 1024
    first, second = _big(tmp_path / "a.step", size), _big(tmp_path / "b.step", size, flip_at=10)
    assert kicad_library._same_file_bytes(first, second) is False
    assert len(read_sizes) == 2


def test_a_difference_in_the_last_byte_is_still_seen(tmp_path):
    """Negative: equal size, equal start, one changed last byte -> not the same content."""
    size = 3 * kicad_library._COMPARE_CHUNK_BYTES + 5
    first, second = _big(tmp_path / "a.step", size), _big(tmp_path / "b.step", size, flip_at=size - 1)
    assert kicad_library._same_content(first, second) is False
    assert kicad_library._same_content(first, _big(tmp_path / "c.step", size)) is True


def test_different_sizes_missing_files_and_folders_are_never_the_same(tmp_path, read_sizes):
    """A size difference answers without reading; unreadable sides answer False."""
    first = _big(tmp_path / "a.step", 1000)
    longer = _big(tmp_path / "b.step", 1001)
    assert kicad_library._same_content(first, longer) is False
    assert read_sizes == []
    assert kicad_library._same_content(tmp_path / "missing.step", first) is False
    assert kicad_library._same_content(first, tmp_path / "missing.step") is False
    assert kicad_library._same_content(tmp_path, first) is False
    # Two empty files are the same content.
    empty_a, empty_b = tmp_path / "e1", tmp_path / "e2"
    empty_a.write_bytes(b"")
    empty_b.write_bytes(b"")
    assert kicad_library._same_content(empty_a, empty_b) is True
