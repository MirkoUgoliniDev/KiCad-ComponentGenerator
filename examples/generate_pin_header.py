"""@brief Example: generate a 2x4 THT pin header footprint with 2.54 mm pitch.

The footprint is that of a male pin header.

Usage:
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
