"""@brief Re-export of the shared component file-name rule.

The rule moved to `kicadgen.core.output_names` so that the footprint
parameter validators (core and generators) can use it without depending on
the 3D layer. This thin module keeps the historical import path working for
the 3D generator and for scripts.
"""

from __future__ import annotations

from ..core.output_names import is_valid_output_name, validate_output_name

__all__ = ["is_valid_output_name", "validate_output_name"]
