"""Test per kicadgen.cad3d.annotations (validazione fillet/testo su faccia)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from kicadgen.cad3d.annotations import EdgeFilletAnnotation, FaceTextAnnotation


def test_edge_fillet_rejects_unknown_edge():
    with pytest.raises(ValueError, match="Spigolo non valido"):
        EdgeFilletAnnotation(edge="bogus", radius_mm=1.0).validate()


def test_edge_fillet_rejects_non_positive_radius():
    with pytest.raises(ValueError, match="raggio"):
        EdgeFilletAnnotation(edge="top-front", radius_mm=0).validate()


def test_edge_fillet_accepts_valid():
    EdgeFilletAnnotation(edge="front-left", radius_mm=0.5).validate()


def test_face_text_rejects_unknown_face():
    with pytest.raises(ValueError, match="Faccia non valida"):
        FaceTextAnnotation(face="bogus", text="X").validate()


def test_face_text_rejects_empty_text():
    with pytest.raises(ValueError, match="testo"):
        FaceTextAnnotation(face="top", text="  ").validate()


def test_face_text_rejects_non_positive_size_or_thickness():
    with pytest.raises(ValueError, match="dimensione"):
        FaceTextAnnotation(face="top", text="X", size_mm=0).validate()
    with pytest.raises(ValueError, match="spessore"):
        FaceTextAnnotation(face="top", text="X", thickness_mm=0).validate()


def test_face_text_rejects_invalid_color():
    with pytest.raises(ValueError, match="Colore del testo"):
        FaceTextAnnotation(face="top", text="X", color_hex="bianco").validate()


def test_face_text_accepts_valid():
    FaceTextAnnotation(
        face="top", text="ACME", size_mm=2.0, thickness_mm=0.06, color_hex="#ffffff"
    ).validate()


def test_body_params_reject_invalid_color():
    from kicadgen.cad3d.body_params import Body3DParams

    with pytest.raises(ValueError, match="Colore del corpo"):
        Body3DParams(length_mm=10, width_mm=8, height_mm=4, color_hex="#xyz").validate()
