"""Esempio: genera un header THT 2x4 passo 2.54mm (tipo pin header maschio).

Uso:
    python examples/generate_pin_header.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kicadgen.generators.pin_header import PinHeaderParams, build_pin_header

NAME = "PinHeader_2x04_P2.54mm_THT"

if __name__ == "__main__":
    params = PinHeaderParams(name=NAME, rows=2, cols=4, pitch=2.54, pad_diameter=1.7, drill=1.0)
    footprint = build_pin_header(params)
    out_path = os.path.join(
        os.path.dirname(__file__), "output", f"{NAME}.pretty", f"{NAME}.kicad_mod"
    )
    footprint.save(out_path)
    print(f"Scritto: {out_path}")
