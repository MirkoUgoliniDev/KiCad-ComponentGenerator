"""@brief Footprint parameter extraction from datasheets through the OpenRouter API.

The documentation files (PDF/images) are sent to OpenRouter -- which gives
access to the models of every vendor through a single key -- with the request
to fill in a tool (tool calling) whose schema is that of the pin header
parameters, so the answer is always valid structured JSON, never free text
to be parsed by hand.

The model is chosen from the GUI (OpenRouter id, e.g.
"anthropic/claude-sonnet-5"): the GUI setting lives in the SQLite profiles,
while a single extraction may use a more capable one without changing the
default.

The environment variable OPENROUTER_API_KEY is required (normally in the
.env file next to main.py). No key is ever written into the code.
"""

from __future__ import annotations

import base64
import copy
import mimetypes
import math
import numbers
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from ..cad3d.annotations import EDGE_NAMES
from ..core.footprint import KICAD_COORD_LIMIT_MM
from ..core.package_geometry import PAD_SHAPES, PinSpec, package_doubts, package_from_mapping
# The one rule for holes and pads not printed in the drawing, and its three
# constants (formerly copied here): one definition with the footprint checks.
from ..core.tht_estimates import (HOLE_TO_PAD_ANNULUS_MM, LEAD_TO_HOLE_CLEARANCE_MM,  # noqa: F401
                                  MIN_ANNULAR_RING_MM, estimated_hole, estimated_pad_diameter,
                                  estimated_round_pad, lead_passes, normalised_lead,
                                  unverified_values)
from ..core.validation import finite_number
from ..generators.castellated_module import (
    CONTACT_STYLES, EDGE_LABELS, EDGES,
)
# Free copper a derived land keeps from its neighbours (review G1): the
# minimum of the pin header generator, one value for both.
from ..generators.package import ESTIMATED_PADS_LISTED, ESTIMATED_PADS_MORE, estimated_pad_list
from ..generators.pin_header import MIN_PAD_GAP_MM
from .ai_diagnostics import CallCancelled
from .openrouter import (DEFAULT_MODEL, OpenRouterError, ToolArgumentsError, api_key, call_tool,
                         check_credit, reset_usage, usage_totals)
from .edge_notches import EdgeCount
from .messages import end_sentence, tx
# The registry imports no owning module at load time, so no import cycle.
from .prompt_catalog import effective_text
from .prompt_catalog import sending as prompt_sending
from .prompt_models import model_for, usable_model
from .prompt_models import step as prompt_step

MAX_FILES = 5

#: Mechanical body kinds a photo or a drawing may declare for a mounted part,
#: each with the description the model reads (Italian, like every prompt).
#:
#: A kind is a STRUCTURAL class -- the shape of the body and of its terminals,
#: which decides the 3D shape and the plausible height of an unmeasured body --
#: never a brand, a part number or an electrical function. The set covers the
#: shapes whose height or outline differs enough to matter for a mechanical
#: envelope (a flat chip, a gull-wing package, a can standing on the board, a
#: strip of posts, a flat panel...); anything else is "generic", and an
#: unknown value sent by a model degrades to "generic" with a note
#: (`_components_3d`). The 3D side keeps its own copy (cad3d/freecad_parts.
#: BUILDERS, which only runs inside FreeCAD): a kind it does not draw yet is
#: drawn as the generic block.
COMPONENT_KIND_DESCRIPTIONS: dict[str, str] = {
    "generic": "unrecognisable body: simple block",
    "ic": "integrated circuit in an unrecognisable package",
    "ic_soic": ("rectangular integrated circuit with gull-wing leads on two opposite sides "
                "(SO, SOIC, SSOP, TSSOP, MSOP)"),
    "ic_sot": ("small plastic package with a few short leads on one or two sides (SOT and "
               "SC-70 family, also with a tab)"),
    "ic_qfn": ("flat integrated circuit without protruding leads, contacts under the edge of "
               "the body (QFN, DFN, LGA)"),
    "ic_qfp": "integrated circuit with gull-wing leads on four sides (QFP, LQFP, TQFP)",
    "ic_bga": "integrated circuit with balls under the body and no terminal visible at the sides (BGA, CSP)",
    "power_tab": ("power package with a metal heat-sinking tab (TO-252/DPAK, "
                  "TO-263/D2PAK, TO-220)"),
    "transformer": "transformer or winding on a core, tall body",
    "module": "sub-module on its own PCB mounted on the board",
    "shield_can": "sheet-metal shield over a group of components",
    "led": "surface-mount LED with a lens",
    "capacitor_tantalum": "moulded capacitor with a polarity stripe (tantalum or polymer)",
    "capacitor": "rectangular chip capacitor with two metal terminations",
    "capacitor_electrolytic": ("cylindrical electrolytic capacitor (can on a plastic base "
                               "or radial), standing on the board"),
    "resistor": "rectangular chip resistor with two metal terminations",
    "inductor": "small chip or wound inductor",
    "inductor_power": ("moulded or shielded power inductor, square or round, with a "
                       "height comparable to its side"),
    "diode": "diode in a flat plastic package with a band (SOD, SMA, SMB, SMC)",
    "diode_melf": "cylindrical glass diode with a band and metal caps, not to be confused with a chip",
    "connector": "connector not otherwise classifiable (USB, coaxial, terminal block, FPC...)",
    "pin_header": "row or matrix of male pins at a regular pitch on a plastic base",
    "socket_header": "row or matrix of female contacts in a closed plastic body",
    "switch": "push button or switch",
    "relay": "relay: closed plastic block, tall",
    "potentiometer": "trimmer or potentiometer with an adjustment screw or shaft",
    "crystal": "flat crystal or oscillator in a ceramic or metal case",
    "crystal_can": "crystal in a tall oval metal can",
    "battery": "battery or battery holder (coin or cylindrical)",
    "display": "flat display (LCD or OLED glass), with or without a frame",
    "buzzer": "buzzer or cylindrical transducer",
}

#: The kinds, in the order the schemas offer them.
COMPONENT_KINDS = tuple(COMPONENT_KIND_DESCRIPTIONS)
MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB, reasonable limit per single API file

_SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif"}

_TOOL_NAME = "report_pin_header_params"

#: The one definition of `body_height_mm`, shared by the schema description,
#: the single-part prompt and the dedicated body reading (adversarial review
#: 2026-11-03: three different texts -- «spessore... vista laterale», «dal
#: piano di appoggio alla sommita', esclusa la sporgenza», «esclusi terminali
#: e distanziale» -- let a compliant model COMPUTE the height from the printed
#: pieces: 19.3 - 18.9 = 0.40 = the printed standoff of the T7N relay, for a
#: printed 16.9). A quote is ONE printed number, transcribed, never summed.
_HEIGHT_RULE = (
    "body_height_mm is ONE SINGLE PRINTED number: in the front or side view of the dimensioned "
    "drawing (group \"Dimensions\", \"Outline\" or similar, also on the same sheet as the pinout) "
    "it is the dimension that measures the body PERPENDICULAR TO THE SEATING PLANE ON THE PCB, "
    "whatever the direction of the terminals on the page (straight under the body, bent sideways "
    "as in a gull-wing SMD, or drawn towards the left or right in a rotated view), from its "
    "top to its bottom (or to the seating plane, if the drawing dimensions it that way). In "
    "leads_point declare where the terminals point in the profile view used (down, up, "
    "left, right): if they point left or right the view is rotated and the height is the "
    "HORIZONTAL dimension, while the vertical dimension is a plan measure; the program assigns "
    "the axes. Transcribe it as it is written: do NOT add or subtract other dimensions (a "
    "standoff or the protrusion of the terminals dimensioned separately are not added), do not "
    "derive it from the proportions of the drawing and do not reuse a number read in plan. In "
    "printed_vertical_quotes list ALL the dimensions printed in the profile view "
    "perpendicular to the seating plane, in the order in which they appear, and for each one "
    "declare in measures what it measures (body_height: the body; plan: a plan measure repeated "
    "in the profile; standoff: the standoff between body and PCB; lead: the protrusion of the "
    "terminals; overall: the total envelope body plus terminals or standoff; other: anything "
    "else): the program looks for the height among them. If no view prints a dimension of the "
    "body in that direction, omit body_height_mm: a height computed from other dimensions is "
    "\"derived\", never \"read\"."
)

#: The one definition of the lead protrusion (`pin_length_3d_mm`, `lead_length`),
#: shared by every schema and prompt (review 2026-11-04, M7: two texts --
#: «sotto il piano di appoggio» and «sotto il corpo» -- defined two different
#: lengths when a standoff is printed).
_LEAD_RULE = ("the PRINTED protrusion of the terminals below the bottom of the body, one single "
              "transcribed number, a mechanical dimension and NOT the hole of the footprint")

#: Roles a printed profile quote may declare (`measures` of
#: `printed_vertical_quotes`): what the number measures, as the reader sees it
#: on the page. "other" is also the role of a bare number sent by an older or
#: cached answer that declared nothing.
PRINTED_QUOTE_ROLES = ("body_height", "plan", "standoff", "lead", "overall", "other")

#: Schema of the printed profile quotes, shared by the single-part tool and
#: the dedicated body reading: one object per printed number with its role.
_PRINTED_QUOTES_SCHEMA = {
    "type": "array",
    "items": {"type": "object", "properties": {
        "value": {"type": "number", "exclusiveMinimum": 0,
                  "description": "The dimension as printed, in the unit of drawing_unit."},
        "measures": {"type": "string", "enum": list(PRINTED_QUOTE_ROLES),
                     "description": ("What it measures: body_height (the body), plan (a plan "
                                     "measure), standoff (body-PCB standoff), lead "
                                     "(protrusion of the terminals), overall (total envelope), "
                                     "other.")},
    }, "required": ["value", "measures"]},
    "description": ("ALL the PRINTED dimensions in the front or side view of the dimensioned "
                    "drawing perpendicular to the seating plane (along the terminals, whatever "
                    "their direction on the page), in the order in which they appear, in the unit "
                    "of drawing_unit, without adding them up, each with the role you declare in "
                    "measures; empty list if no profile view is dimensioned."),
}

#: How a drawing qualifies the plan quotes of the body (`body_quote_kind`):
#: a nominal value, a "MAX" (upper limit) or a lower limit. Only a nominal
#: quote is compared with a chain span as an equality (`_chain_contradictions`).
BODY_QUOTE_KINDS = ("nominal", "max", "min")

#: Where the leads point on the page in the profile view the height was read
#: from (`leads_point`): the program assigns the axes from it, the reader only
#: declares what it sees (review 2026-11-04, M7/M15).
LEADS_POINT = ("down", "up", "left", "right")

_LEADS_POINT_SCHEMA = {
    "type": "string", "enum": list(LEADS_POINT),
    "description": ("Direction of the terminals on the page in the profile view from which you "
                    "read the height: down or up (upright view: vertical height), left or right "
                    "(rotated view: horizontal height)."),
}

#: Length fields of the answer, per level: every one is a length in the
#: declared drawing unit and becomes millimetres after `_canonicalize_answer`.
#: They are also the key dimensions whose provenance the model declares
#: (`value_sources`).
_TOP_LENGTH_KEYS = ("pitch_mm", "row_spacing_mm", "pad_diameter_mm", "drill_mm",
                    "body_length_mm", "body_width_mm", "body_height_mm", "pcb_thickness_mm",
                    "body_offset_x_mm", "body_offset_y_mm", "pin_length_3d_mm",
                    "lead_diameter_mm")
#: Where the leads leave the body of a single through-hole part (`lead_exit`).
#: "bottom": straight out of the underside (potted modules, transformers,
#: relays, connectors), so every lead centre lies inside the body outline;
#: "side": out of the flanks and bent down (DIP, TO-220), so the rows may lie
#: outside the moulding; "unknown": the documents do not show it.
LEAD_EXITS = ("bottom", "side", "unknown")
_SIDE_LENGTH_KEYS = ("pitch_mm", "offset_mm", "pad_length_mm", "pad_width_mm",
                     "half_hole_diameter_mm", "through_hole_drill_mm",
                     "first_contact_from_edge_mm", "last_contact_from_edge_mm")

#: Provenance the model may declare for a value (`value_sources`): "read"
#: written in a document (a dimension, a table), "derived" computed from read
#: values only, "estimated" judged by eye on a photograph, scaled from other
#: dimensions or taken from a design rule because no document gives it.
VALUE_SOURCES = ("read", "derived", "estimated")
#: Provenance recorded for a value the answer carries without a valid source:
#: it is treated as needing review, exactly like an estimated one, so a value
#: whose origin nobody declared never passes for a read one.
UNDECLARED_SOURCE = "undeclared"
#: Provenances that ask for a review before the value is used.
REVIEW_SOURCES = ("estimated", UNDECLARED_SOURCE)
#: Provenance the PROGRAM gives a value it measured on the calibrated photo
#: (edge_lands, HANDOFF 169). Never accepted from the model's own
#: `value_sources` (see _declared_sources): the model reads documents, it
#: does not measure pixels.
MEASURED_SOURCE = "measured"
#: Provenance the PROGRAM gives a value the user chose through a structured
#: question (`apply_answers`): declared, never reviewed as an estimate, and
#: never accepted from the model's own `value_sources`.
USER_SOURCE = "user"


def _value_sources_schema(keys, where: str) -> dict:
    """Schema of a `value_sources` object: one optional provenance per length key."""
    return {
        "type": "object",
        "description": (
            f"Provenance of every dimension you fill in {where}, field by field (key = "
            "field name): \"read\" if it is written in the document (dimension of the drawing, "
            "table); \"derived\" if you compute it ONLY from read dimensions (sum, difference, "
            "half); \"estimated\" if you estimate it: by eye from a photo, in proportion to "
            "other dimensions or with a design rule because the document does not give it. "
            "Declare it for EVERY dimension you fill in: a dimension without provenance is "
            "treated as estimated and flagged to the user."
        ),
        "properties": {key: {"type": "string", "enum": list(VALUE_SOURCES)} for key in keys},
    }


#: Per-pad values whose provenance a pad entry declares in its own
#: `value_sources` (the keys of `core.tht_estimates.SOURCE_KEYS`): the hole,
#: the pad size, the pad shape and the terminal section.
PAD_SOURCE_KEYS = ("drill_mm", "pad_size", "shape", "lead")

#: Schema fragment shared by the explicit pads of the main answer
#: (`package_geometry.pins`) and of the single-part answer (`pads`), 2026-11-10
#: (user: «ok procedi», contract A1-A3 of HANDOFF 10/11): the terminal of each
#: pad, the printed hole callout and the per-pad provenance. The model
#: TRANSCRIBES what is printed; the hole and the land that are not printed
#: are derived by the program from the terminal (`_resolve_pad_terminals`,
#: `core.tht_estimates`). Every field is optional and of a plain type, so the
#: DeepSeek strict conversion (`openrouter.strict_tool`) stays lossless.
_PAD_TERMINAL_PROPERTIES = {
    "lead": {
        "type": "object",
        "description": (
            "Cross-section of the TERMINAL (lead, pin, blade) that enters this hole, as dimensioned "
            "in the drawing, in the unit of drawing_unit: shape \"round\" with diameter_mm for a "
            "round terminal, shape \"flat\" with width_mm and thickness_mm for a blade. It is NOT "
            "the hole. Declare its provenance in value_sources.lead."),
        "properties": {
            "shape": {"type": "string", "enum": ["round", "flat"]},
            "diameter_mm": {"type": "number", "exclusiveMinimum": 0},
            "width_mm": {"type": "number", "exclusiveMinimum": 0},
            "thickness_mm": {"type": "number", "exclusiveMinimum": 0},
        },
    },
    "hole_callout": {
        "type": "string",
        "description": (
            "The callout of the HOLE of this pad as it is PRINTED on the PCB layout or "
            "drilling drawing (e.g. \"3-Ø1.2\", \"Ø1.3+0.1\"). Omit it if the drawing does not "
            "print the hole: then do not estimate it, give the terminal in lead."),
    },
    "drill_length_mm": {
        "type": "number", "exclusiveMinimum": 0,
        "description": (
            "Only for a SLOT printed in the drawing: the side of the slot along Y (the axis of "
            "pad_width_mm), in the unit of drawing_unit; drill_mm is then its side along X "
            "(the axis of pad_length_mm). A horizontal slot has drill_mm greater than "
            "drill_length_mm, a vertical one the opposite. Never estimated."),
    },
    "value_sources": {
        "type": "object",
        "description": (
            "Provenance of the values of THIS pad: drill_mm \"read\" only if the hole is "
            "printed (hole_callout); pad_size \"read\" only if the land is printed (land "
            "pattern); shape for the shape of the land; lead for the terminal. \"estimated\" "
            "for everything that is not printed: the program derives the values not printed."),
        "properties": {key: {"type": "string", "enum": list(VALUE_SOURCES)}
                       for key in PAD_SOURCE_KEYS},
    },
}

#: Closed electrical roles a pinout entry may declare in ``function`` (the
#: relay roles the symbol composer reads, `generators.symbol_relay`): the
#: program never derives them from the free-text ``name``.
PINOUT_FUNCTIONS = ("coil", "common", "nc", "no", "other")


_EXTRACTION_TOOL = {
    "name": _TOOL_NAME,
    "description": (
        "Reports the mechanical parameters of a component extracted from the documents: "
        "contacts (matrix of through-hole pins, sides of a castellated module or "
        "explicit pads in package_geometry), body, pinout, unit of measure and "
        "view of the drawing read. Every free-text value (notes, reasons) is written "
        "in English."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "layout": {
                "type": "string",
                "enum": ["matrix", "castellated"],
                "description": (
                    "How the contacts of the component are arranged.\n"
                    "- \"matrix\": through-hole pins on a regular grid of rows x "
                    "columns (pin headers, DIP-style modules, THT components). Fill in "
                    "rows, cols, pad_diameter_mm, drill_mm, pitch_mm if cols > 1 and "
                    "row_spacing_mm if rows > 1. Use \"matrix\" also when the pads "
                    "are described one by one in package_geometry (SMD components, "
                    "irregular packages): then package_geometry is the footprint and the "
                    "fields of the matrix are optional.\n"
                    "- \"castellated\": module on a PCB with plated contacts on the "
                    "edge (half holes). Fill in castellated_sides and the body_* fields; do NOT "
                    "force the contacts into a matrix, because different sides often have "
                    "a different number of contacts and a different pitch: in that case "
                    "\"matrix\" would give a wrong footprint."
                ),
            },
            "drawing_unit": {
                "type": "string",
                "enum": ["mm", "inch", "mil"],
                "description": (
                    "Unit of measure of the dimensions of the drawing you read: \"mm\" "
                    "millimetres, \"inch\" inches, \"mil\" thousandths of an inch. ALL "
                    "the lengths of this answer (every field with the _mm suffix, "
                    "also inside castellated_sides, package_geometry, components_3d "
                    "and edge_fillets) are given in this unit, with the numbers "
                    "AS YOU READ THEM, without converting them: the program does the "
                    "conversion to millimetres. If the drawing gives two units "
                    "side by side, use millimetres. Without a dimensioned drawing use \"mm\"."
                ),
            },
            "contacts_view": {
                "type": "string",
                "enum": ["top", "bottom", "unknown"],
                "description": (
                    "From which side the drawing (or the photo) is seen from which you read the "
                    "arrangement of the contacts: pin_map, the numbering order of the "
                    "matrix, castellated_sides, body_offset_x_mm and body_offset_y_mm. "
                    "\"bottom\" if it is labelled BOTTOM VIEW or shows the lower "
                    "side, \"top\" if it is a view from above or a photo of the component "
                    "side, \"unknown\" if the document does not allow it to be "
                    "established. Report everything AS YOU SEE IT, without flipping anything: the "
                    "KiCad footprint is drawn as seen from above and the program does the "
                    "left-right mirroring of a bottom view. "
                    "package_geometry declares its view in source_view."
                ),
            },
            "package_geometry": {
                "type": "object",
                "description": (
                    "Explicit pads, one per pin: the way to describe SMD components "
                    "(pads without a hole, pad_type \"smd\") and the packages that cannot be "
                    "represented as a matrix (SOIC, TSSOP, QFN, QFP, irregular DIP, custom "
                    "packages). When present it is the source of the footprint. Local "
                    "coordinates of the centre of every pad, as they appear in the view given "
                    "by source_view (the program mirrors the bottom view). "
                    "AXIS CONVENTION, as the drawing appears on the "
                    "page: X grows to the RIGHT and Y grows DOWNWARDS (a pad lower "
                    "in the drawing has a GREATER y_mm; do not use the mathematical "
                    "convention with Y upwards, which would mirror the footprint and "
                    "reverse the order of the pins). Every angle (orientation_deg, "
                    "rotation_deg) is in degrees COUNTERCLOCKWISE as it appears on the "
                    "page. An exposed/thermal pad is a pin of its own. Do not invent "
                    "coordinates not supported by the document."
                ),
                "properties": {
                    "family": {"type": "string", "enum": [
                        "unknown", "dip", "sip", "soic", "tssop", "qfn", "qfp",
                        "module", "castellated", "custom"]},
                    "body_length_mm": {
                        "type": "number", "exclusiveMinimum": 0,
                        "description": (
                            "Extent of the body along the X axis of these coordinates "
                            "(horizontal in the view of source_view), whatever name the "
                            "datasheet gives it (D, E, E1, length). With the pins on the "
                            "left and right sides (SOIC drawn vertically) it is the "
                            "dimension ACROSS the pin rows, not the one along the "
                            "rows."),
                    },
                    "body_width_mm": {
                        "type": "number", "exclusiveMinimum": 0,
                        "description": (
                            "Extent of the body along the Y axis of these coordinates "
                            "(vertical in the view of source_view), whatever name the "
                            "datasheet gives it. With the pins on the left and right sides it is the "
                            "dimension ALONG the pin rows."),
                    },
                    "body_height_mm": {"type": "number", "exclusiveMinimum": 0},
                    "lead_form": {
                        "type": "string", "enum": ["gull_wing", "j_lead", "unknown"],
                        "description": (
                            "Shape of the terminals as it appears in the side view: "
                            "\"gull_wing\" if they come out of the flanks and bend "
                            "outwards like a gull wing, \"j_lead\" if they bend in a J "
                            "under the edge of the body (the lands lie under the edge), "
                            "\"unknown\" if the drawing does not show the side view. Do not "
                            "deduce it from the name of the package."),
                    },
                    "origin": {"type": "string", "enum": ["body_center", "pin1", "custom"]},
                    "source_view": {"type": "string", "enum": ["top", "bottom"]},
                    "orientation_deg": {
                        "type": "number",
                        "description": (
                            "Rotation to apply to the drawing to obtain the footprint, "
                            "in COUNTERCLOCKWISE degrees as it appears on the page; 0 to draw "
                            "the footprint like the drawing."),
                    },
                    "mirrored": {
                        "type": "boolean",
                        "description": (
                            "true ONLY if the drawing itself is a mirror image of the "
                            "view declared in source_view for a reason OTHER than being "
                            "a bottom view (e.g. labelled «mirror image» or «top view "
                            "seen through»). A BOTTOM VIEW is declared ONLY with "
                            "source_view \"bottom\" and mirrored false: the program already "
                            "mirrors the bottom view, and mirrored true would "
                            "cancel that."),
                    },
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "needs_review": {"type": "boolean"},
                    "pins": {"type": "array", "items": {"type": "object", "properties": {
                        "number": {"type": "string"}, "name": {"type": "string"},
                        "x_mm": {"type": "number"}, "y_mm": {"type": "number"},
                        "pad_length_mm": {"type": "number", "exclusiveMinimum": 0,
                                          "description": "Size of the pad along X, before rotation_deg."},
                        "pad_width_mm": {"type": "number", "exclusiveMinimum": 0,
                                         "description": "Size of the pad along Y, before rotation_deg."},
                        "side": {"type": "string", "enum": ["left", "right", "top", "bottom", "center"]},
                        "pad_type": {"type": "string", "enum": ["smd", "thru_hole", "np_thru_hole", "connect"]},
                        # Only shapes a PinSpec can describe (review 2026-10-27,
                        # G7): a trapezoid needs KiCad's `rect_delta`, which a
                        # pin does not carry, and was written as a rectangle.
                        "shape": {"type": "string", "enum": ["circle", "rect", "oval", "roundrect"]},
                        "rotation_deg": {"type": "number",
                                         "description": "Angle of the pad in COUNTERCLOCKWISE degrees as it appears on the page."},
                        "drill_mm": {"type": "number", "exclusiveMinimum": 0,
                                     "description": ("Hole, only for a pad with a hole: the "
                                                     "PRINTED diameter (hole_callout); "
                                                     "if it is not printed omit it and give lead.")},
                        # Terminal, printed hole and per-pad provenance (2026-11-10).
                        **_PAD_TERMINAL_PROPERTIES,
                    }, "required": ["number", "x_mm", "y_mm", "pad_length_mm", "pad_width_mm"]}},
                },
                "required": ["family", "source_view", "pins"],
            },
            "top_photo_index": {
                "type": "integer", "minimum": -1,
                "description": (
                    "FILE index (0-based, FILE label in the message) of the best top-view "
                    "PHOTO of the side with the mounted components, long axis horizontal. "
                    "Exclude bottom, side and perspective views and drawings. "
                    "Use -1 if no photo is suitable. Do not choose on resolution alone."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "Suggested KiCad name for the footprint, in the style "
                    "PinHeader_<rows>x<columns>_P<pitch>mm_THT"
                ),
            },
            "description": {"type": "string", "description": "Short description of the component, in English"},
            "tags": {"type": "string", "description": "Space-separated tags"},
            "rows": {"type": "integer", "description": "Number of rows of the pin matrix (e.g. 2 for a double-row component)"},
            "cols": {
                "type": "integer",
                "description": (
                    "Number of POSITIONS per row of the regular grid at pitch pitch_mm, "
                    "empty ones included: not the number of physical pins. Two pins of the "
                    "same row a multiple of the pitch apart are as many columns "
                    "further on as there are pitches, and the positions between them count even if "
                    "empty (the empty ones are excluded with populated_pins). A grid "
                    "compressed to the physical pins alone would give a footprint with the pins in the "
                    "wrong place."
                ),
            },
            "pitch_mm": {
                "type": "number",
                "description": (
                    "Pitch between ADJACENT pins on the SAME row, in the unit of "
                    "drawing_unit. Omit it with a single column."
                ),
            },
            "row_spacing_mm": {
                "type": "number",
                "description": (
                    "PERPENDICULAR distance between the centres of two adjacent pin rows, "
                    "in the unit of drawing_unit. Omit it with a single row (rows=1). "
                    "IMPORTANT: it is a dimension distinct from the pitch and in DIP-style "
                    "packages it is normally much greater: read them separately, do not "
                    "reuse one number for the other. "
                    "Also with layout=\"castellated\", if the contacts lie on two opposite "
                    "sides: distance between the CENTRES of the contacts of the two rows, if dimensioned "
                    "or readable on the grid; omit it if you do not know it."
                ),
            },
            "pad_diameter_mm": {
                "type": "number",
                "description": "Diameter of the through-hole pad of the matrix, in the unit of drawing_unit.",
            },
            "drill_mm": {
                "type": "number",
                "description": (
                    "Diameter of the hole (drill) of the pads of the matrix, in the unit of "
                    "drawing_unit: always greater than zero and smaller than pad_diameter_mm. "
                    "SMD pads have no hole: for an SMD component omit this "
                    "field and describe the pads in package_geometry."
                ),
            },
            "lead_diameter_mm": {
                "type": "number",
                "description": (
                    "Diameter of the TERMINAL (lead, pin) dimensioned in the drawing, "
                    "in the unit of drawing_unit (e.g. \"Ø0.5\" next to the pin in the "
                    "side view). It is NOT the hole: the hole must be wider "
                    "than the terminal, and the program checks it. Omit it if not dimensioned."
                ),
            },
            "lead_exit": {
                "type": "string",
                "enum": ["bottom", "side", "unknown"],
                "description": (
                    "From which face of the body the terminals of a single "
                    "through-hole component come out, looking at the side view or the photo: "
                    "\"bottom\" if they come straight out of the bottom (potted modules, "
                    "transformers, relays, connectors: every pin lies inside the outline "
                    "of the body), \"side\" if they come out of the flanks and are bent "
                    "downwards (DIP, TO-220: the rows may lie outside the body), "
                    "\"unknown\" if it cannot be seen. Do not deduce it from the name of the part."
                ),
            },
            "populated_pins": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "ONLY if the pinout of the datasheet shows that not all the positions "
                    "of the rows x cols matrix have a physical pin (partial pinout, "
                    "typical of DIP-style modules): list the pin numbers ACTUALLY "
                    "present according to the numbering of the datasheet. If the matrix is "
                    "fully populated (e.g. an ordinary pin header), OMIT this field."
                ),
            },
            "body_length_mm": {
                "type": "number",
                "description": (
                    "ONLY if the datasheet has a mechanical drawing (\"Outline Dimensions\" "
                    "or similar) with the envelope of the body of the component: extent of the "
                    "body along the X axis of the footprint (horizontal in the view), "
                    "in the unit of drawing_unit, whatever name the datasheet gives it. "
                    "In a pin matrix X is the direction of the columns, hence of the "
                    "pitch; with explicit pads (package_geometry, or pads) X is the X axis of their "
                    "coordinates, even when the pin rows run vertically (pins on the "
                    "left and on the right: it is the dimension across the rows). Omit it if "
                    "the mechanical drawing is not present."
                ),
            },
            "body_width_mm": {
                "type": "number",
                "description": (
                    "Width of the body, extent along the Y axis of the footprint "
                    "(vertical in the view; same convention as body_length_mm), "
                    "from the top or bottom view of the mechanical drawing: it is the "
                    "dimension of the OUTER OUTLINE of the case. In a pin matrix it is NOT "
                    "the distance between the two rows of pins: it is almost always "
                    "a different value, slightly greater than row_spacing_mm, "
                    "because the case is wider than the distance between the pins alone."
                ),
            },
            "pcb_thickness_mm": {
                "type": "number", "exclusiveMinimum": 0,
                "description": "Thickness of the PCB alone, components and pins excluded. Omit it if not dimensioned; do not confuse it with the total height.",
            },
            "body_height_mm": {
                "type": "number",
                "description": (
                    "Height of the body along the Z axis (perpendicular to the PCB), in the unit of "
                    "drawing_unit -- a dimension COMPLETELY DIFFERENT from row_spacing_mm and "
                    "body_width_mm (dimensions of the top view, X/Y axes). " + _HEIGHT_RULE
                ),
            },
            "body_offset_x_mm": {
                "type": "number",
                "description": (
                    "Position of the edge of the body along X relative to pin 1 (which is "
                    "always at the origin x=0 of the footprint). Typically a negative "
                    "value equal to the distance from the left edge of the body to pin 1, "
                    "taken from the dimension of the mechanical drawing (if pin 1 is d away from "
                    "the left edge, body_offset_x_mm = -d). Read it on the SAME "
                    "drawing from which you read the pins, as you see it: if contacts_view is "
                    "\"bottom\" the program flips it together with the columns."
                ),
            },
            "body_offset_y_mm": {
                "type": "number",
                "description": (
                    "Position of the edge of the body along Y relative to the FIRST row of "
                    "pins (y=0), same logic as body_offset_x_mm but for the perpendicular "
                    "axis: negative value equal to the distance between the edge of the "
                    "body on the side of the first row and the first row itself. It is measured "
                    "from the first row, NOT from the centre of the body: with several rows centred "
                    "in the body it is -(body_width_mm - distance between first and last row)/2, "
                    "not -body_width_mm/2. Half the width with a minus sign is right "
                    "only with ONE "
                    "single centred row. All the pin centres must fall inside the "
                    "body."
                ),
            },
            "pin_length_3d_mm": {
                "type": "number",
                "description": ("pin_length_3d_mm is " + _LEAD_RULE
                                + ", in the unit of drawing_unit."),
            },
            "edge_fillets": {
                "type": "array",
                "description": (
                    "If a real photo of the component (not the dimensioned mechanical drawing) "
                    "shows rounded/chamfered edges on the case, list which edges "
                    "of the box appear rounded, with a radius ESTIMATED BY "
                    "EYE. Product photos NEVER give a dimensioned value for "
                    "these radii: it is normal and expected to estimate it visually (e.g. 0.5-1.5mm "
                    "for a moulded plastic case with visibly chamfered edges), "
                    "an explicit dimension is NOT needed to fill in this field -- a reasonable "
                    "estimate is better than omitting it when the rounding is visible. "
                    "If the case in the photo clearly has sharp edges (crisp 90-degree "
                    "corners), or there is no product photo, omit the field."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "edge": {
                            "type": "string",
                            "enum": list(EDGE_NAMES),
                            "description": (
                                "Name of the edge, between two faces of the box. "
                                "Convention, seen from above with the long axis "
                                "horizontal: left/right = left/right sides, front = "
                                "BOTTOM side of the view, back = top side, top = "
                                "upper face, bottom = seating face. The rounded "
                                "corners of the outline SEEN FROM ABOVE (e.g. a board with "
                                "round corners) are the vertical edges front-left, "
                                "front-right, back-left, back-right. top-*/bottom-* are "
                                "instead the horizontal edges of the upper or "
                                "lower face (rounded edge seen in profile): do NOT "
                                "use them for the corners seen from above. On a thin body "
                                "(e.g. a PCB) the fillets of two opposite edges of the "
                                "same face must add up to less than its thickness."
                            ),
                        },
                        "radius_mm": {
                            "type": "number",
                            "description": (
                                "Radius estimated from the photo, in the unit of drawing_unit "
                                "(no dimension "
                                "is available for a visual radius: use a plausible standard "
                                "estimate, e.g. 0.5-1.5mm for a moulded plastic case)."
                            ),
                        },
                    },
                    "required": ["edge", "radius_mm"],
                },
            },
            "pin_map": {
                "type": "array",
                "description": (
                    "Position of every pin in the matrix, EXACTLY AS IT APPEARS IN THE "
                    "DRAWING: do not flip and do not rotate anything, report only what you "
                    "see (the program takes care of any mirroring, based "
                    "on contacts_view). It is needed when the numbering does NOT follow, in the "
                    "drawing read, the column-by-column order (1,2 then 3,4...) "
                    "typical of pin headers: DIP-style packages number in a U (1..N "
                    "along one row, then backwards along the other). If the numbering follows "
                    "the order of the columns you may omit it: contacts_view still applies, "
                    "and a bottom view is mirrored even without pin_map. List ONLY "
                    "the pins whose number is WRITTEN in the drawing next to the position: "
                    "do not extend the numbering to the positions without a number and do not "
                    "apply a convention (pin header or DIP) instead of the reading; "
                    "a position without a number in the drawing does not go in pin_map."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "pin": {"type": "string", "description": (
                            "Number of the pin AS IT IS PRINTED in the drawing or in the "
                            "table, as text: 1, 11, A1, B12, EP.")},
                        "row": {
                            "type": "integer",
                            "description": "Row index, 0 = first row (y=0), 1 = next row",
                        },
                        "col": {
                            "type": "integer",
                            "description": "Column index, 0 = leftmost column IN THE DRAWING",
                        },
                    },
                    "required": ["pin", "row", "col"],
                },
            },
            "pinout": {
                "type": "array",
                "description": (
                    "Pinout table of the datasheet: for every PHYSICALLY PRESENT pin, "
                    "the number and the function. If the datasheet lists several configurations "
                    "(variants in different columns), report the first one and add "
                    "the alternative in brackets only if different. If there is no table but "
                    "the pinout drawing shows the ELECTRICAL SCHEMATIC with the numbered terminals "
                    "(coil between two terminals, contact with common/NC/NO, anode and cathode, "
                    "windings), derive the function from the symbol and write it (e.g. \"Coil\", "
                    "\"Common\", \"NC\", \"NO\"), stating in notes that it is derived from the "
                    "symbol. Omit it only if the document shows neither a table nor a schematic."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "pin": {"type": "string", "description": (
                            "Number of the pin AS IT IS PRINTED in the drawing or in the "
                            "table, as text: 1, 11, A1, B12, EP.")},
                        "name": {
                            "type": "string",
                            "description": (
                                "Function of the pin as the document writes it (NC if "
                                "not connected)."
                            ),
                        },
                        "function": {
                            "type": "string", "enum": list(PINOUT_FUNCTIONS),
                            "description": (
                                "ELECTRICAL role of the terminal ONLY if the document shows it "
                                "(table or electrical schematic next to the pinout): coil = "
                                "terminal of the coil of a relay; common = common of a "
                                "contact; nc = normally CLOSED contact (NOT «not "
                                "connected»: a pin that is not connected is other); no = normally "
                                "open contact; other = any other role or a role not "
                                "shown. Never deduced from the name of the component."
                            ),
                        },
                        "contact": {
                            "type": "string",
                            "description": (
                                "ONLY for common/nc/no of a relay with SEVERAL contacts: the label "
                                "of the contact as the document prints it (1, 2, A, B, ...), the "
                                "same for the common and for its nc/no. With a single contact "
                                "omit it. Never invented: without a printed label omit it."
                            ),
                        },
                    },
                    "required": ["pin", "name"],
                },
            },
            "body_color_hex": {
                "type": "string",
                "description": (
                    "Colour of the case read from the product PHOTO, format '#rrggbb' "
                    "(e.g. '#1a1a1a' for a black case, '#c41230' for a red one). Use the "
                    "colour of the housing, not that of a label applied on it "
                    "-- unless the label in fact covers the whole upper face, "
                    "in which case that is the colour that is seen. "
                    "Omit it if there is no product photo."
                ),
            },
            "text_color_hex": {
                "type": "string",
                "description": (
                    "Colour of the silkscreen/markings on the case in the photo, format "
                    "'#rrggbb' (e.g. '#ffffff' for white markings on a dark case). "
                    "Omit it if there is no photo or no marking can be read."
                ),
            },
            "castellated_sides": {
                "type": "array",
                "description": (
                    "ONLY with layout=\"castellated\": one element for every side of the "
                    "module that carries contacts (up to four), read AS IT "
                    "APPEARS in the drawing or in the photo, without flipping anything: the view "
                    "read is declared in contacts_view and the program mirrors a "
                    "bottom view. Every side has its own number of "
                    "contacts and its own pitch: that is exactly why these "
                    "modules do not fit in a matrix. The origin is the centre of the body."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "edge": {
                            "type": "string",
                            "enum": list(EDGES),
                            "description": (
                                "Side of the module as it appears in the view read: "
                                "\"left\"/\"right\" the vertical sides, \"top\"/\"bottom\" "
                                "the horizontal ones. \"top\" is the upper side in the "
                                "drawing."
                            ),
                        },
                        "count": {
                            "type": "integer",
                            "description": (
                                "Number of PHYSICAL contacts on this side: the plated "
                                "holes cut by the edge (half holes) or the whole holes "
                                "of the edge, counted one by one on the photo and on the "
                                "enlargements of the edges when there are any. The "
                                "silkscreen labels, the labels of the LEDs or of the components and the "
                                "text near the edge are NOT contacts: count the holes, "
                                "not the writing. Check that (count-1) x pitch_mm fits "
                                "in the dimensioned edge."
                            ),
                        },
                        "pitch_mm": {
                            "type": "number",
                            "description": (
                                "Pitch between adjacent contacts ON THIS SIDE, "
                                "in the unit of drawing_unit: it may differ from "
                                "that of the other sides."
                            ),
                        },
                        "first_pin": {
                            "type": "integer",
                            "description": (
                                "Number of the first contact of this side, following the "
                                "edge from top to bottom (vertical sides) or from left "
                                "to right (horizontal sides). It must be the minimum of the "
                                "sequence even with reverse=true. The physical numbers must "
                                "be unique across ALL the sides: do not restart from 1 "
                                "on every side and do not use the GPIO numbers as pad numbers."
                            ),
                        },
                        "reverse": {
                            "type": "boolean",
                            "description": (
                                "true if on this side the numbering grows in the opposite "
                                "direction (from bottom to top, or from right to left)."
                            ),
                        },
                        "offset_mm": {
                            "type": "number",
                            "description": (
                                "Shift of the group of contacts along the edge "
                                "relative to the centred position, in the unit of "
                                "drawing_unit "
                                "(positive downwards or to the right). Omit it or 0 "
                                "if the contacts are centred on the side."
                            ),
                        },
                        "first_contact_from_edge_mm": {
                            "type": "number",
                            "description": (
                                "Distance, along the edge, from the start of the edge (from "
                                "the left for top/bottom, from the top for left/right) to the "
                                "centre of the FIRST contact, in the unit of drawing_unit. "
                                "ONLY if it is a dimension written in the dimensioned drawing; omit it "
                                "otherwise, do not derive it."
                            ),
                        },
                        "last_contact_from_edge_mm": {
                            "type": "number",
                            "description": (
                                "Distance, along the edge, from the end of the edge to the centre "
                                "of the LAST contact, in the unit of drawing_unit. ONLY if "
                                "it is a dimension written in the dimensioned drawing; omit it "
                                "otherwise, do not derive it."
                            ),
                        },
                        "pad_length_mm": {
                            "type": "number",
                            "description": (
                                "Length of the land perpendicular to the edge "
                                "(how far it goes under the module plus how far it protrudes), "
                                "from the \"Recommended PCB land pattern\". Omit it if the "
                                "datasheet does not give it."
                            ),
                        },
                        "pad_width_mm": {
                            "type": "number",
                            "description": (
                                "Width of the land along the edge (normally a little "
                                "more than the diameter of the half hole). Omit it if absent."
                            ),
                        },
                        "contact_style": {
                            "type": "string",
                            "enum": list(CONTACT_STYLES),
                            "description": (
                                "Which plated feature every contact of this side "
                                "carries, LOOKING AT THE PHOTO of the edge (do not deduce it from the "
                                "dimensions): \"half_hole\" if the gold hole is cut by the "
                                "edge of the board (half-moon open towards the outside); "
                                "\"through_hole\" if the hole is a whole closed circle, "
                                "with a complete gold ring and intact board between the "
                                "ring and the edge, which stays straight; "
                                "\"half_and_through\" if the same contact has "
                                "both, the half-moon on the edge and a whole hole a little "
                                "further in. Omit it if the photo does not show the edge or if "
                                "you cannot tell the type: do not deduce it and do not choose a "
                                "usual value, the program will ask the user."
                            ),
                        },
                        "half_hole_diameter_mm": {
                            "type": "number", "exclusiveMinimum": 0,
                            "description": (
                                "Diameter of the plated half hole cut by the edge "
                                "(the whole circle of which the edge shows half), "
                                "in the unit of drawing_unit: from the dimension of the drawing "
                                "or, if not dimensioned, estimated from the photo in proportion "
                                "to the known dimensions (\"estimated\" in value_sources). Omit it "
                                "if the side has no half holes or they cannot be seen."
                            ),
                        },
                        "through_hole_drill_mm": {
                            "type": "number", "exclusiveMinimum": 0,
                            "description": (
                                "Diameter of the whole hole (through hole near the "
                                "edge) of the contacts of this side, in the unit of "
                                "drawing_unit: from the dimension of the drawing or, if not "
                                "dimensioned, estimated from the photo in proportion to the known "
                                "dimensions (\"estimated\" in value_sources). Only if the side "
                                "has whole holes; otherwise omit it."
                            ),
                        },
                        "value_sources": _value_sources_schema(_SIDE_LENGTH_KEYS, "for this side"),
                        "pin_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Silkscreen labels next to the contacts of this side, "
                                "in the order in which they are met along the edge in the "
                                "view read (from top to bottom on the vertical sides, from "
                                "left to right on the horizontal ones). Exactly "
                                "one name per contact counted in count: a label "
                                "printed across two holes (e.g. two letters "
                                "close together) is split between the two contacts, not joined into a "
                                "single name; the labels of nearby LEDs or components are not "
                                "contact names. They document the "
                                "pinout, they do not number the pads."
                            ),
                        },
                    },
                    "required": ["edge", "count", "pitch_mm"],
                },
            },
            "components_3d": {
                "type": "array",
                "description": (
                    "For populated boards, both THT (matrix) and castellated, only if among the "
                    "files there is a PHOTO of the upper side: the visible components, "
                    "approximated as rectangular blocks, to give the 3D model "
                    "the look of the real module instead of a smooth plate. "
                    "List ALL the visible components, one by one, large and small, "
                    "including EVERY single passive, each with its own position and "
                    "its own orientation (length_mm local long axis, width_mm short "
                    "axis, rotation_deg orientation). There is no expected number: "
                    "report the bodies you see, without adding or removing any to "
                    "reach a count. No dimension of these is given by the "
                    "datasheet -- they are visual estimates in proportion to the known "
                    "dimensions of the module, and that is normal. "
                    "Omit the field if there is no top-view photo."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "Type of the body in plain English words, without commercial "
                                "codes: it ends up in the name of the solid."
                            ),
                        },
                        "x_mm": {
                            "type": "number",
                            "description": (
                                "Position of the CENTRE of the component along X relative to the "
                                "centre of the module, in the unit of drawing_unit: negative towards "
                                "the left, positive towards the right in the photo seen "
                                "from above (the module is body_length_mm long)."
                            ),
                        },
                        "y_mm": {
                            "type": "number",
                            "description": (
                                "Position of the CENTRE along Y relative to the centre of the "
                                "module: negative towards the top of the photo, positive "
                                "towards the bottom (the module is body_width_mm wide)."
                            ),
                        },
                        "length_mm": {
                            "type": "number",
                            "description": (
                                "Length of the component along ITS long axis, "
                                "regardless of how it is rotated on the module: "
                                "the orientation goes in rotation_deg."
                            ),
                        },
                        "width_mm": {
                            "type": "number",
                            "description": "Width along the short axis of the component.",
                        },
                        "rotation_deg": {
                            "type": "number",
                            "enum": [0, 90, 180, 270],
                            "description": (
                                "How the component lies on the module seen from above: 0 if "
                                "its long axis is horizontal (parallel to the long side of the "
                                "module, X), 90 if it is vertical (Y). Look at it in the photo: a "
                                "diode or a resistor \"standing up\" is 90. 180/270 only if "
                                "the direction matters (diode band, pin 1) and you see it."
                            ),
                        },
                        "height_mm": {
                            "type": "number",
                            "description": (
                                "Height above the PCB: from a dimensioned side view if "
                                "there is one, otherwise estimated from the type of body (kind) and from "
                                "its plan dimensions; from a single top-view photo it is "
                                "always an estimate. Do not exceed the total height of the "
                                "module minus the thickness of the PCB."
                            ),
                        },
                        "color_hex": {
                            "type": "string",
                            "description": "Colour seen in the photo, '#rrggbb'.",
                        },
                        "kind": {
                            "type": "string",
                            "enum": list(COMPONENT_KINDS),
                            "description": (
                                "Type of body recognised in the photo from its SHAPE "
                                "(not from the electrical function nor from the brand): it decides the "
                                "shape in the 3D model and the plausible height. \"generic\" "
                                "if not recognisable. Values: "
                                + "; ".join(f"{kind} = {text}" for kind, text
                                            in COMPONENT_KIND_DESCRIPTIONS.items()) + "."
                            ),
                        },
                        "marking": {
                            "type": "string",
                            "description": (
                                "Marking printed on the component, ONLY if readable in the "
                                "photo, copied character by character: it is used to look up the "
                                "library model by marking, it is not printed on the "
                                "solid. Do not invent codes."
                            ),
                        },
                    },
                    "required": ["x_mm", "y_mm", "length_mm", "width_mm", "height_mm"],
                    # The model may describe only these fields: internal
                    # provenance and verification flags (`_`-prefixed keys)
                    # are set by the program, never by an answer. The
                    # decoder enforces the same rule (_components_3d).
                    "additionalProperties": False,
                },
            },
            "value_sources": _value_sources_schema(
                _TOP_LENGTH_KEYS, "at this level (the castellated sides have their own)"),
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "How sure you are of the values extracted from the datasheet",
            },
            "notes": {
                "type": "string",
                "description": ("Ambiguities, assumptions made or values not found in the "
                                "datasheet, written in English"),
            },
        },
        # Always required; the matrix fields and the castellated-side fields
        # are required depending on what the answer is (layout, explicit
        # pads, number of rows and columns), and the check is made in
        # _params_from_tool_arguments() where the values are known.
        # Unit and view are required so that the model always declares them;
        # an answer that omits them stays readable (millimetres, unknown view
        # declared as a doubt in the notes).
        "required": ["layout", "confidence", "drawing_unit", "contacts_view"],
    },
}

#: System prompt of the DEDICATED pin-numbering call (user, 2026-10-10:
#: «separa, vediamo se in quel modo funziona»). One perceptual task only: the
#: main call reads every quote well and invents the pin numbering (a zig-zag
#: convention over the six numbers printed on the TEL8 drawing), so the
#: numbering is asked again, alone, with the drawing alone.
DEFAULT_PIN_NUMBERING_PROMPT = (
    "You are a reader of mechanical drawings. You are shown the dimensioned drawing of a "
    "component with its pins arranged on a regular grid. Your ONLY task is to "
    "report, for every PIN NUMBER PRINTED in the drawing next to a position, the "
    "row and the column of that position on the grid. Count rows and columns AS THEY "
    "APPEAR in the drawing: row 0 the topmost row, column 0 the leftmost position "
    "of the whole grid at constant pitch, empty positions included (two pins "
    "a multiple of the pitch apart are as many columns further on as there are pitches). "
    "Do not mirror and do not rotate anything: declare in contacts_view whether the drawing is a "
    "view from below (BOTTOM VIEW) or from above, and the program will do the mirroring. "
    "Report ONLY the numbers you read printed: do not extend the numbering to the positions "
    "without a number and do not apply any convention (pin header, DIP). If you read "
    "no number, answer with an empty list. Write any note in English. Answer ONLY by "
    "calling the tool."
)

_PIN_NUMBERING_TOOL = {
    "name": "report_pin_numbering",
    "description": "Position on the grid of every pin number printed in the drawing.",
    "input_schema": {
        "type": "object",
        "properties": {
            "contacts_view": {
                "type": "string", "enum": ["top", "bottom", "unknown"],
                "description": "From which side the drawing of the pins is seen (title BOTTOM VIEW = bottom).",
            },
            "pins": {
                "type": "array",
                "description": "One element for every pin number PRINTED in the drawing, no other.",
                "items": {
                    "type": "object",
                    "properties": {
                        "pin": {"type": "string", "description": (
                            "Number printed next to the position, as text and as it "
                            "appears: 1, 11, A1, B12, EP.")},
                        "row": {"type": "integer", "description": "Row index: 0 = topmost row in the drawing."},
                        "col": {"type": "integer", "description": "Column index: 0 = leftmost position in the drawing."},
                    },
                    "required": ["pin", "row", "col"],
                },
            },
            "notes": {"type": "string", "description": "Reading doubts, briefly, in English."},
        },
        "required": ["contacts_view", "pins"],
    },
}

#: Roles of the documents the dedicated numbering call looks at: the
#: dimensioned drawing and the pinout drawing, the only places where a pin
#: number is printed beside a position.
NUMBERING_DOCUMENT_ROLES = ("top", "pinout")


def _pin_numbering_tool() -> dict:
    """The dedicated numbering schema in OpenRouter's "function" wrapper."""
    return {"type": "function", "function": {
        "name": _PIN_NUMBERING_TOOL["name"],
        "description": _PIN_NUMBERING_TOOL["description"],
        "parameters": _PIN_NUMBERING_TOOL["input_schema"]}}


def numbering_documents(doc_paths, document_roles) -> list:
    """@brief The documents a pin number can be read from: those declared drawing or pinout.

    @param doc_paths Documents of the analysis.
    @param document_roles Normalized roles (file name -> tuple of keys), or None.
    @return The paths carrying a role in NUMBERING_DOCUMENT_ROLES, images only
        (a PDF cannot be enlarged and is read by the main call); [] without roles.
    """
    if document_roles is None:
        return []
    return [path for path in doc_paths
            if path.suffix.lower() != ".pdf"
            and any(role in NUMBERING_DOCUMENT_ROLES
                    for role in _declared_role_keys(document_roles.get(path.name) or ()))]


def _numbering_only_in_pdf(doc_paths, document_roles) -> bool:
    """True when a drawing or pinout role is declared only on PDF files (no image to enlarge)."""
    if document_roles is None:
        return False
    return (not numbering_documents(doc_paths, document_roles)
            and any(path.suffix.lower() == ".pdf"
                    and any(role in NUMBERING_DOCUMENT_ROLES
                            for role in _declared_role_keys(document_roles.get(path.name) or ()))
                    for path in doc_paths))


def read_pin_numbering(documents, *, rows, cols, model, call, prompt=None,
                       progress=None) -> Optional[dict]:
    """@brief Ask the model, alone, where each printed pin number sits on the rows x cols grid.

    @param documents Image documents of `numbering_documents`.
    @param rows Rows of the grid of the main answer (drawing orientation).
    @param cols Columns of the grid of the main answer.
    @param model Model identifier of the analysis (its provider sizes the image).
    @param call The logged, cancellable call wrapper of the analysis.
    @param prompt System prompt; None means DEFAULT_PIN_NUMBERING_PROMPT.
    @param progress Optional log callback.
    @return None without documents or grid (no call made); otherwise
        {"view": "top"|"bottom"|"unknown", "pins": [{"pin", "row", "col"}, ...],
        "notes": str} with the entries validated against the grid (row < rows,
        col < cols, no pin or position twice); "pins" is empty and "failed"
        (call error) or "discarded" (unusable answer) says why, so the note
        of `apply_dedicated_numbering` names the real outcome. A discard
        because more distinct printed numbers were read than the grid has
        positions also carries "contradicts_grid": True and "grid".
    @throws ExtractionCancelled When the user cancels during the call; every
        other failure (call, unreadable drawing) is returned as "failed".
    @note Best-effort by design: a failure of this paid call must never lose
        the main answer. The result is in DRAWING coordinates, mirrored later
        by `_pin_map_seen_from_top` with the declared view, like pin_map.
    """
    if not documents or not rows or not cols:
        return None
    #: Output budget sized on the grid: about 30 tokens per entry when every
    #: position carries a printed number, plus room for the view and notes.
    max_tokens = int(min(8192, max(1024, 512 + 30 * int(rows) * int(cols))))
    try:
        # The documents are read inside the try: a drawing that vanished or
        # is locked since the main call raises ExtractionError, and this
        # best-effort reading must never lose the paid main answer (M5).
        blocks = []
        for index, path in enumerate(documents):
            blocks.append({"type": "text", "text": f"FILE {index}: {path.name}"})
            blocks.append(_file_to_content_block(path, model))
        blocks.append({"type": "text", "text": (
            f"The pin grid has {int(rows)} rows and {int(cols)} positions per row "
            f"(columns) at constant pitch. Report the position of every pin number printed "
            f"in the drawing, as text and as it appears (1, 11, A1, EP), and call the tool.")})
        # The profile text, or the default's bytes when it is the default
        # (V4-5); the request declares the field it sends (V4-6).
        sent = effective_text("numbering_prompt", {"numbering_prompt": prompt})
        with prompt_sending({"numbering_prompt": sent}):
            data = call(model=model, system_prompt=sent,
                        content_blocks=blocks, tool=_pin_numbering_tool(),
                        max_tokens=max_tokens, has_pdf=False)
    except ExtractionCancelled:
        raise
    except (OpenRouterError, ToolArgumentsError, ExtractionError) as exc:
        if progress:
            progress(tx("Lettura dedicata della numerazione non riuscita ({exc}): resta la "
                        "numerazione della risposta principale.", exc=exc))
        return {"view": "unknown", "pins": [], "notes": "", "failed": str(exc)}
    if not isinstance(data, dict):
        return {"view": "unknown", "pins": [], "notes": "",
                "discarded": tx("risposta non strutturata")}
    view = data.get("contacts_view")
    view = view if view in ("top", "bottom", "unknown") else "unknown"
    raw_pins = data.get("pins") if isinstance(data.get("pins"), (list, tuple)) else []
    printed = {_pin_label(e.get("pin")) for e in raw_pins if isinstance(e, dict)}
    printed.discard(None)
    printed = {_pin_text(pin) for pin in printed}
    if len(printed) > int(rows) * int(cols):
        # More DISTINCT printed numbers than the grid has positions: whatever
        # the reader did with rows and columns, the grid cannot hold them.
        # Evidence against the grid, never a silent discard (user, 2026-10-31,
        # T7N relay: five printed pins on a 2x2 grid). A repeated number or an
        # index out of range alone can be the reader's slip (two images of
        # the same pinout, rows counted from 1) and is discarded as before.
        reason = tx("{count} numeri di pin stampati distinti per {positions} posizioni",
                    count=len(printed), positions=int(rows) * int(cols))
        if progress:
            progress(tx("La lettura dedicata dei numeri di pin non sta nella griglia {rows}x{cols} "
                        "della risposta principale ({exc}).", rows=int(rows), cols=int(cols),
                        exc=reason))
        return {"view": view, "pins": [], "notes": "", "discarded": reason,
                "contradicts_grid": True, "grid": (int(rows), int(cols))}
    try:
        entries = _pin_map_seen_from_top(raw_pins, int(cols), "top", int(rows))
    except ExtractionError as exc:
        if progress:
            progress(tx("Lettura dedicata della numerazione scartata ({exc}): resta la "
                        "numerazione della risposta principale.", exc=exc))
        return {"view": view, "pins": [], "notes": "", "discarded": str(exc)}
    notes = data.get("notes")
    return {"view": view, "pins": entries or [],
            "notes": notes if isinstance(notes, str) else ""}


#: System prompt of the DEDICATED pad-position call of a single part with
#: explicit pads (user, 2026-10-10: two readings of the VE12H-K relay drawing
#: placed the holes differently, both wrong, and nothing noticed). Since
#: 2026-10-30 the model only transcribes the dimension chains as printed and
#: the program adds them up (pads_from_chains): both readings had dropped one
#: quote of a chain while summing it, the same way.
DEFAULT_PAD_POSITIONS_PROMPT = (
    "You are a reader of mechanical drawings. You are shown the dimensioned drawing of the holes or "
    "of the lands of ONE SINGLE component. Do NOT compute coordinates: transcribe the DIMENSION "
    "CHAINS as they are printed, the program does the sums. A chain is a row of "
    "consecutive dimensions along one axis: x horizontal (from left to right) or y vertical "
    "(from top to bottom). For every chain list in `stops` the stops in increasing order "
    "on the page and in `values` the dimensions printed between two consecutive stops (one fewer "
    "than the stops); if instead the dimensions are ORDINATE, that is all measured from the first "
    "stop (0, 5, 10, 15 from one datum), declare kind \"from_first_stop\" and in values the "
    "dimensions as printed, without subtracting them. In values_text copy every dimension as it is "
    "written, with its decimals. A stop is the number of the hole or of the land that the "
    "dimension reaches; if several holes lie on the same dimension line write them together separated "
    "by commas; write \"edge\" for the outline of the BODY, only as the first or last stop; "
    "\"edge:<name>\" for another drawn outline that is not the body (flange, base, "
    "plate); \"ref:<name>\" for a reference point or line (datum) or a mounting hole that is not a "
    "terminal. If a dimension reaches the EDGE of a land and not its centre (typical of "
    "SMD land patterns), declare it in `at` for that stop (near_edge or far_edge): the "
    "program does the conversion to the centre. "
    "Include the dimensions in brackets (reference dimensions) and those towards the outline of the body. "
    "Every numbered hole must appear in at least one x chain and one y chain. Do not skip "
    "any dimension of the chain and do not add two together. CENTRELINE: if on one axis a hole has no "
    "dimension but lies on a centreline (dash-dot) drawn halfway between two stops "
    "(two holes, or the two edges of the body), declare it in midpoints with the axis, the hole and the two "
    "stops (for the outline of the body on one side only declare which in edge_side): the program "
    "takes the mean. Do not invent a dimension that is not printed and do not "
    "write chains with a single stop: a hole without a dimension and without a centreline on an axis "
    "stays out of that axis. Declare in drawing_unit the unit of the "
    "dimensions and in contacts_view whether the drawing is a view from below (BOTTOM VIEW, solder "
    "side) or from above. Do not mirror and do not rotate anything. If a hole has no number and "
    "cannot be recognised from the pinout view, do not include it. PRINTED HOLES: if the drawing "
    "prints the diameter of the holes (a callout such as \"3-Ø1.2\" or \"Ø1.3+0.1\"), report it in "
    "printed_holes with the numbers of the holes it refers to and the text as printed; a hole "
    "whose diameter is not printed must not be estimated. DIMENSIONED TERMINALS: if the drawing "
    "dimensions the CROSS-SECTION of the terminals that enter the holes (leads, pins, blades: a "
    "diameter such as \"Ø0.6\", or width by thickness such as \"1.0x0.4\"), report it in "
    "printed_terminals with the numbers of the holes it refers to: shape \"round\" with diameter, or "
    "shape \"flat\" with width and thickness, and the text as printed. It is the terminal, NOT the "
    "hole; a terminal whose cross-section is not dimensioned stays out, it must not be estimated. "
    "Write any note in English. Answer ONLY by calling the tool."
)

#: Default model of the dedicated pad-position reading (profile field
#: 'pad_model', user, 2026-10-30). Chosen on a bench of real calls on three
#: dimensioned drawings (two relays, one DC-DC converter): it read all 11
#: right, ~0.03 USD each; the default analysis model read 1 in 9 and no
#: transcription format fixed that. A small sample: a capable model lowers
#: the error rate, it does not replace the checks of apply_dedicated_pads.
DEFAULT_PAD_POSITIONS_MODEL = "openai/gpt-5.6-sol"

#: Completion budget of the dedicated pad-position reading. Models that
#: reason before answering spend most of it before the tool call (up to
#: ~3300 tokens measured on the bench); 4096 cut some of them, and a cut
#: answer is paid and lost. Only the tokens used are billed.
PAD_POSITIONS_MAX_TOKENS = 16384

#: How the values of a chain are measured (`kind`): each from the previous
#: stop (a chain of consecutive quotes) or every one from the FIRST stop
#: (ordinate dimensioning: 0, 5, 10, 15 from one datum). Summing an ordinate
#: list as a chain gave 0, 5, 15, 30 (review 2026-11-04, H2).
CHAIN_KINDS = ("consecutive", "from_first_stop")

#: What a quote reaches at a pad stop (`at`, one entry per stop): the pad's
#: centre, or its edge on the side where the axis begins (left for x, top
#: for y: "near_edge") or the opposite edge ("far_edge"). SMD land patterns
#: quote the pad edges; the program converts with the pad size it has.
PAD_EDGE_ANCHORS = ("centre", "near_edge", "far_edge")

#: Sides of the body outline a centreline may run from (`edge_side` of a
#: midpoint whose stop is the bare outline).
EDGE_SIDES = ("low", "high")

#: Prefix of a stop on a drawn outline that is NOT the body (a flange, a
#: base plate, a bracket): "edge:<label>". Kept as a node for relative
#: placement, never compared with the body size.
OUTLINE_STOP_PREFIX = "edge:"
#: Prefix of a stop that is a datum or a mounting hole, not a terminal:
#: "ref:<label>". A node for the sums, never a pad; reported separately.
REFERENCE_STOP_PREFIX = "ref:"


_PAD_POSITIONS_TOOL = {
    "name": "report_pad_positions",
    "description": ("Dimension chains of the drawing of the numbered holes or lands, in the declared "
                    "unit: the program derives the coordinates."),
    "input_schema": {
        "type": "object",
        "properties": {
            "drawing_unit": {"type": "string", "enum": ["mm", "inch", "mil"]},
            "contacts_view": {"type": "string", "enum": ["top", "bottom", "unknown"]},
            "chains": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "axis": {"type": "string", "enum": ["x", "y"],
                             "description": "x: horizontal, to the right; y: vertical, downwards."},
                    "kind": {"type": "string", "enum": list(CHAIN_KINDS),
                             "description": ("consecutive: every dimension goes from the previous stop "
                                             "to the next one; from_first_stop: ordinate dimensions, "
                                             "each measured from the FIRST stop (0, 5, 10, 15).")},
                    "stops": {"type": "array", "items": {"type": "string"},
                              "description": ("Stops in increasing order on the page: numbers of the "
                                              "holes on the same dimension line separated by commas; "
                                              "\"edge\" (outline of the BODY) only at the ends; "
                                              "\"edge:<name>\" for another drawn outline "
                                              "(flange, base, plate) that is not the body; "
                                              "\"ref:<name>\" for a reference point or line (datum) or a mounting "
                                              "hole that is not a terminal.")},
                    "at": {"type": "array", "items": {"type": "string", "enum": list(PAD_EDGE_ANCHORS)},
                           "description": ("One entry per stop: centre if the dimension reaches the "
                                           "centre of the hole or of the land; near_edge if it reaches "
                                           "the edge of the land on the side where the axis begins "
                                           "(left for x, top for y); far_edge the opposite edge. "
                                           "Omit it if all the dimensions go to the centres.")},
                    "values": {"type": "array", "items": {"type": "number"},
                               "description": "Dimensions printed between consecutive stops, in the same order."},
                    "values_text": {"type": "array", "items": {"type": "string"},
                                    "description": ("The same dimensions AS THEY ARE PRINTED, with the "
                                                    "decimals of the drawing (\"12\", \"12.2\", "
                                                    "\"12.20\"): the program derives the precision "
                                                    "from them.")},
                }, "required": ["axis", "stops", "values"]},
            },
            "midpoints": {
                "type": "array",
                "description": ("Holes that on one axis have NO dimension but lie on a "
                                "CENTRELINE drawn halfway between two stops: the program takes the mean. "
                                "No computations and no invented dimensions."),
                "items": {"type": "object", "properties": {
                    "axis": {"type": "string", "enum": ["x", "y"],
                             "description": "Axis on which the hole lies on the centreline."},
                    "pad": {"type": "string", "description": "Number of the hole on the centreline."},
                    "between": {"type": "array", "items": {"type": "string"},
                                "description": ("The two stops between which the centreline runs, in "
                                                "increasing order on the page: hole numbers "
                                                "(also in a group, separated by commas), "
                                                "\"edge:<name>\", \"ref:<name>\", or \"edge\" "
                                                "for the outline of the body (then declare "
                                                "edge_side).")},
                    "edge_side": {"type": "string", "enum": list(EDGE_SIDES),
                                  "description": ("If only one stop is \"edge\": low for the "
                                                  "outline where the axis begins (left or top), "
                                                  "high for the opposite one.")},
                }, "required": ["axis", "pad", "between"]},
            },
            "printed_holes": {
                "type": "array",
                "description": ("Callouts of the HOLES PRINTED on the drawing (e.g. \"3-Ø1.2\" next to the "
                                "holes of the PCB layout): one per callout, with the numbers of the holes "
                                "it refers to. Only what is printed: no estimated hole."),
                "items": {"type": "object", "properties": {
                    "pads": {"type": "string",
                             "description": "Numbers of the holes of the callout, separated by commas."},
                    "diameter": {"type": "number", "exclusiveMinimum": 0,
                                 "description": ("Printed diameter of the hole (for a slot: its "
                                                 "width), in the unit of drawing_unit.")},
                    "slot_length": {"type": "number", "exclusiveMinimum": 0,
                                    "description": "Only for a printed slot: its length."},
                    "text": {"type": "string", "description": "The callout as printed."},
                }, "required": ["pads", "diameter"]},
            },
            # Second, independent transcription of the per-pad terminal
            # (user, 2026-11-11: the main answer alone declared the terminals
            # of a relay wrong and «read», and the holes were derived from
            # them). The same shape as `lead` of the main schema, flat fields
            # so that the strict conversion stays lossless.
            "printed_terminals": {
                "type": "array",
                "description": ("CROSS-SECTION of the terminals (leads, pins, blades) DIMENSIONED in the "
                                "drawing, in the unit of drawing_unit: one entry per dimension, with the "
                                "numbers of the holes those terminals enter. It is NOT the hole. Only what "
                                "is printed: no estimated terminal."),
                "items": {"type": "object", "properties": {
                    "pads": {"type": "string",
                             "description": "Numbers of the holes of these terminals, separated by commas."},
                    "shape": {"type": "string", "enum": ["round", "flat"],
                              "description": "round: round terminal; flat: blade."},
                    "diameter": {"type": "number", "exclusiveMinimum": 0,
                                 "description": "Only for round: the dimensioned diameter of the terminal."},
                    "width": {"type": "number", "exclusiveMinimum": 0,
                              "description": "Only for flat: the dimensioned width of the blade."},
                    "thickness": {"type": "number", "exclusiveMinimum": 0,
                                  "description": "Only for flat: the dimensioned thickness of the blade."},
                    "text": {"type": "string", "description": "The dimension as printed."},
                }, "required": ["pads", "shape"]},
            },
            "notes": {"type": "string", "description": "Reading doubts, in English."},
        },
        "required": ["drawing_unit", "contacts_view", "chains"],
    },
}


#: Dedicated reading of the printed OUTLINE quotes of a single part (user,
#: 2026-11-03, T7N relay: the main model skipped the side view's height in
#: two analyses out of four and invented it -- 18.9 and 19.3 mm for a quoted
#: 16.9 -- in the other two; the «successful» STEP had a false height). The
#: same chain-reading model transcribes the quotes, each from the view that
#: prints it, and the program compares them with the main answer
#: (apply_dedicated_body). Values the drawing does not print are omitted.
_BODY_DIMENSIONS_TOOL = {
    "name": "report_body_dimensions",
    "description": ("PRINTED dimensions of the body envelope of ONE SINGLE component, in the declared "
                    "unit, each from the view that shows it; omit what is not printed."),
    "input_schema": {
        "type": "object",
        "properties": {
            "drawing_unit": {"type": "string", "enum": ["mm", "inch", "mil"]},
            "body_length": {"type": "number", "exclusiveMinimum": 0,
                            "description": "Plan dimension of the outline of the body along X (horizontal)."},
            "body_width": {"type": "number", "exclusiveMinimum": 0,
                           "description": "Plan dimension of the outline of the body along Y (vertical)."},
            "body_height": {"type": "number", "exclusiveMinimum": 0,
                            "description": _HEIGHT_RULE.replace("body_height_mm", "body_height")},
            "standoff": {"type": "number", "exclusiveMinimum": 0,
                         "description": ("Dimensioned distance between the bottom of the body and the PCB, "
                                         "as information only: do NOT subtract it from body_height.")},
            "lead_length": {"type": "number", "exclusiveMinimum": 0,
                            "description": "lead_length is " + _LEAD_RULE + "."},
            "body_quote_kind": {
                "type": "string", "enum": ["nominal", "max", "min"],
                "description": ("How the drawing qualifies the plan dimensions of the body: nominal "
                                "(nominal value), max (\"MAX\", an upper limit), min (a "
                                "lower limit).")},
            "leads_point": _LEADS_POINT_SCHEMA,
            "printed_vertical_quotes": _PRINTED_QUOTES_SCHEMA,
            "notes": {"type": "string",
                      "description": "Which view you used for each value; doubts. In English."},
        },
        "required": ["drawing_unit", "printed_vertical_quotes"],
    },
}

DEFAULT_BODY_DIMENSIONS_PROMPT = (
    "You are a reader of mechanical drawings. You are shown the dimensioned drawing of ONE SINGLE "
    "component. Transcribe ONLY the PRINTED dimensions of the body envelope, without computing or "
    "estimating. body_length and body_width are the two plan dimensions of the outline of the body "
    "(view from above or from below); if the drawing qualifies them as maximum or minimum say so in "
    "body_quote_kind. "
    + _HEIGHT_RULE.replace("body_height_mm", "body_height") + " "
    "standoff is the dimensioned distance between the bottom of the body and the PCB, as information "
    "only, not to be subtracted; lead_length is " + _LEAD_RULE + ". Every value is taken from the view "
    "that shows it: do NOT reuse a plan dimension for the height. If a dimension is not printed, "
    "omit the field: do not guess. Declare in drawing_unit the unit of the dimensions and in notes, "
    "in English, which view you used for each value. Answer ONLY by calling the tool."
)

#: Quotes of the dedicated body reading: (answer key of the reading, key of the
#: main answer, label of the messages). `standoff` is read as information
#: only (no consumer places the body above the PCB yet) and never compared.
_BODY_READING_KEYS = (("body_length", "body_length_mm", "lunghezza"),
                      ("body_width", "body_width_mm", "larghezza"),
                      ("body_height", "body_height_mm", "altezza"),
                      ("lead_length", "pin_length_3d_mm", "sporgenza dei terminali"))

#: Main-answer field of each quote of the dedicated reading.
_BODY_FIELD_OF = {key: field for key, field, _label in _BODY_READING_KEYS}


#: Roles of a printed quote that can never be the body height: a plan size
#: repeated in the profile view, the standoff, the lead protrusion, the
#: overall extent (body plus leads or standoff).
_NOT_A_HEIGHT = frozenset({"plan", "standoff", "lead", "overall"})
#: Roles that can never be the lead protrusion.
_NOT_A_LEAD = frozenset({"plan", "standoff", "body_height", "overall"})
#: Roles whose quotes, added to another, give a printed OVERALL figure a
#: model may list as if it were the body: a sum with such an addend is a
#: computation, not a body height.
_SUM_ADDENDS = frozenset({"standoff", "lead", "overall"})


def printed_quotes(raw, factor: float = 1.0) -> list:
    """@brief Normalise a `printed_vertical_quotes` answer field to (value in mm, role) pairs.

    @param raw The field as the model sent it: a list of objects
        {"value": number, "measures": role} (schema `_PRINTED_QUOTES_SCHEMA`),
        or bare numbers from an older or cached answer, or anything else.
    @param factor The declared drawing unit to millimetres.
    @return [(value_mm, role)] in page order, roles among PRINTED_QUOTE_ROLES;
        a bare number, an object without a known role, or a role outside the
        vocabulary is "other" (nothing declared, never guessed). Entries whose
        value is not a finite positive number are left out.
    """
    quotes = []
    for item in (raw if isinstance(raw, list) else ()):
        if isinstance(item, dict):
            value = _optional_number(item.get("value"), positive=True)
            role = item.get("measures")
            role = role if role in PRINTED_QUOTE_ROLES else "other"
        else:
            value, role = _optional_number(item, positive=True), "other"
        if value is not None:
            quotes.append((round(value * factor, 6), role))
    return quotes


def _roles_declared(quotes) -> bool:
    """True when at least one quote of the list declares a role (not a bare-number list)."""
    return any(role != "other" for _value, role in quotes)


def _height_candidate(value_mm: float, quotes, others, tolerance: float,
                      field: str = "body_height_mm") -> bool:
    """@brief Whether a printed list supports `value_mm` as a body height (or lead length).

    @param value_mm The value to support, mm.
    @param quotes Printed profile quotes of one reading as `printed_quotes`
        gives them: [(value_mm, role)]; bare numbers are accepted too (role
        "other").
    @param others Quotes that are known NOT to be this value (plan sizes,
        standoff, lead length...), mm: used ONLY when the list declares no
        role at all (an older or cached answer), see @note.
    @param tolerance `_reading_tolerance` of the drawing unit, mm.
    @param field "body_height_mm" or "pin_length_3d_mm": which role the
        value is looked for as.
    @return True when some listed quote is within tolerance of `value_mm`
        and its DECLARED role does not rule the field out (`_NOT_A_HEIGHT`,
        `_NOT_A_LEAD`), and `value_mm` is not the SUM of two listed quotes
        one of which is declared a standoff, a lead or an overall extent
        (`_SUM_ADDENDS`: the printed overall figure a model may list as the
        body). A quote declared "body_height" supports the height whatever
        its value: a square-section body (height equal to a plan size) is
        a fact the reader declared, not a coincidence to veto (review
        2026-11-04, H1); a stepped body whose height equals two other
        printed quotes is likewise supported (H3).
    @note Declared fallback for a quote WITHOUT a role ("other"): the
        former value rule applies to that quote -- within tolerance of one of
        `others` it is excluded (a listed width must not pass for a height)
        -- whatever its neighbours declared: a role protects only the quote
        that carries it (generality audit 2026-11-05, M3: a mixed list let an
        undeclared 23 next to a declared 18 pass as a height). The sum veto
        of any two listed quotes applies to lists without any role; with
        roles it applies only through `_SUM_ADDENDS`. Those rules are
        coincidences of value, kept only where no role was declared, never
        over a declaration.
    """
    quotes = [(q, "other") if not isinstance(q, tuple) else q for q in (quotes or ())]
    if not quotes:
        return False
    excluded = _NOT_A_LEAD if field == "pin_length_3d_mm" else _NOT_A_HEIGHT
    declared = _roles_declared(quotes)
    listed = False
    for quote, role in quotes:
        if abs(value_mm - quote) > tolerance or role in excluded:
            continue
        if role == "other" and any(abs(quote - other) <= tolerance for other in others):
            continue
        listed = True
        break
    if not listed:
        return False
    rounding = _sum_tolerance(2)
    for index, (first, first_role) in enumerate(quotes):
        for second, second_role in quotes[index + 1:]:
            if abs(value_mm - (first + second)) > rounding:
                continue
            if not declared or first_role in _SUM_ADDENDS or second_role in _SUM_ADDENDS:
                return False
    return True


#: Quotes of `_BODY_READING_KEYS` no independent witness checks: a vertical
#: quote is seen in no plan view, so the drawn holes (plan) say nothing about
#: it. The plan quotes are bounded by the hole fit (drawing_holes
#: «body_scale»), which is why a disagreement there keeps the main value.
_BODY_VERTICAL_KEYS = ("body_height_mm", "pin_length_3d_mm")


def _body_dimensions_tool() -> dict:
    """The dedicated body-quote schema in OpenRouter's "function" wrapper."""
    return {"type": "function", "function": {
        "name": _BODY_DIMENSIONS_TOOL["name"],
        "description": _BODY_DIMENSIONS_TOOL["description"],
        "parameters": _BODY_DIMENSIONS_TOOL["input_schema"]}}


#: Reading tolerance, in mm, between two readings of the same distance: the
#: rounding of a quote read twice (two decimals in mm, three in inches), not
#: a margin on the part. Beyond it the two readings disagree. The same bound
#: applies to two chains reaching one pad and to a chain closed on the body.
PAD_READING_TOLERANCE_MM = 0.1

#: Stop of a dimension chain on the body outline (only first or last).
CHAIN_EDGE = "edge"

#: Graph nodes of the two outline stops of an axis (never a pad number).
_CHAIN_LOW, _CHAIN_HIGH = "<edge-min>", "<edge-max>"


def _pad_positions_tool() -> dict:
    """The dedicated pad-position schema in OpenRouter's "function" wrapper."""
    return {"type": "function", "function": {
        "name": _PAD_POSITIONS_TOOL["name"],
        "description": _PAD_POSITIONS_TOOL["description"],
        "parameters": _PAD_POSITIONS_TOOL["input_schema"]}}


def _chain_node_name(node: str) -> str:
    """A pad number, «bordo» for the body outline, or the named outline/datum/pad edge, in messages."""
    if node in (_CHAIN_LOW, _CHAIN_HIGH):
        return tx("bordo")
    if node.startswith("<") and node.endswith(">"):
        inner = node[1:-1]
        if inner.startswith(OUTLINE_STOP_PREFIX):
            return tx("contorno {label}", label=inner[len(OUTLINE_STOP_PREFIX):])
        if inner.startswith(REFERENCE_STOP_PREFIX):
            return tx("riferimento {label}", label=inner[len(REFERENCE_STOP_PREFIX):])
        if "@" in inner:
            return tx("bordo del pad {pad}", pad=inner.split("@", 1)[0])
    return node


def _chain_stop_names(stop) -> list:
    """Pad numbers of one chain stop (comma-separated), stripped."""
    return [name.strip() for name in str(stop).split(",") if name.strip()]


#: Rounding of ONE printed quote, in mm, keyed by the drawing unit's factor
#: to mm (_UNIT_TO_MM): half a unit of the coarsest usual precision -- 0.1 mm,
#: 0.01 in, 1 mil. A sum of n quotes may drift by n times this;
#: _sum_tolerance never goes below PAD_READING_TOLERANCE_MM. Used only for a
#: quote whose printed text is unknown (`values_text` absent): the rounding
#: of a transcribed text comes from its decimals (`quote_rounding`).
CHAIN_QUOTE_ROUNDING_MM = {1.0: 0.05, 25.4: 0.127, 0.0254: 0.0127}

def _sum_tolerance(quotes: int, factor: float = 1.0) -> float:
    """Tolerance, mm, of a sum of `quotes` printed quotes whose text is unknown (rounding only)."""
    rounding = CHAIN_QUOTE_ROUNDING_MM.get(factor, CHAIN_QUOTE_ROUNDING_MM[1.0])
    return max(PAD_READING_TOLERANCE_MM, rounding * quotes)


def _reading_tolerance(factor: Optional[float]) -> float:
    """Tolerance, mm, between two readings of one pad offset in a drawing of unit `factor`.

    PAD_READING_TOLERANCE_MM in a millimetre drawing, but never below the
    rounding of ONE printed quote in the drawing's own unit (_sum_tolerance):
    a quote of 0.01 inch rounds by 0.127 mm, more than 0.1 mm, so two correct
    readings of an inch drawing were flagged as disagreeing (review G10,
    2026-11-03). An undeclared unit (None) keeps the millimetre value. This
    is the one place the floor applies to a quote read twice; a sum of
    quotes with a known printed text carries the sum of their roundings
    only (`quote_rounding`).
    """
    return _sum_tolerance(1, factor if factor is not None else 1.0)


_QUOTE_TEXT_RE = re.compile(r"^\s*[+-]?(\d+)(?:[.,](\d+))?")


def quote_rounding(text, value, factor: float) -> Optional[float]:
    """@brief The rounding, in mm, of a printed quote transcribed as text ("12" -> 0.5, "12.20" -> 0.005).

    @param text The quote as printed (`values_text` entry), any type.
    @param value The number the model sent for it (`values` entry), in the
        drawing unit.
    @param factor The drawing unit to millimetres.
    @return Half a unit of the last printed digit, in mm; None when the text
        is not a number or does not state `value` (the text is then ignored
        and the unit's fallback rounding applies). A text is trusted only
        when its number equals the value within its own rounding: a text
        and a value that disagree are a transcription slip, not a precision.
    """
    if not isinstance(text, str):
        return None
    match = _QUOTE_TEXT_RE.match(text)
    if match is None:
        return None
    decimals = len(match.group(2) or "")
    rounding = 0.5 * 10 ** (-decimals)
    number = float(match.group(1) + ("." + match.group(2) if match.group(2) else ""))
    if value is None or abs(number - float(value)) > rounding + 1e-9:
        return None
    return rounding * factor


class _Rounding:
    """Accumulated rounding of the quotes summed along a path of the chain graph.

    `mm` is the sum of the roundings of the quotes summed (from their
    printed text, or the unit's fallback); `declared` whether any of them
    had a printed text; `count` how many quotes were summed. The tolerance
    of the path (`tolerance`) is the unit fallback (`_sum_tolerance`) when
    no text was given; a printed text can only TIGHTEN it, never loosen it,
    and never below PAD_READING_TOLERANCE_MM: a mechanical drawing prints
    nominal values under a general tolerance class (ISO 2768-m: +-0.2 mm
    between 6 and 30 mm), so «12» does not mean 11.5..12.5 and a 1 mm slip
    in a chain of integer quotes stays a contradiction (generality audit
    2026-11-05, M4; the former sum-of-roundings rule weakened the body
    check tenfold). Texts with more decimals than the unit's fallback
    (inch quotes printed to 0.001) do tighten, down to the floor.
    """

    __slots__ = ("mm", "declared", "count")

    def __init__(self, mm: float = 0.0, declared: bool = False, count: int = 0):
        """@brief Record one rounding budget.

        @param mm Rounding in mm.
        @param declared True when it comes from a printed text.
        @param count Number of quotes it covers.
        """
        self.mm, self.declared, self.count = mm, declared, count

    def __add__(self, other: "_Rounding") -> "_Rounding":
        return _Rounding(self.mm + other.mm, self.declared or other.declared,
                         self.count + other.count)

    def half(self) -> "_Rounding":
        """Half the budget (the mean of two sums), rounded up in quotes."""
        return _Rounding(self.mm / 2, self.declared, (self.count + 1) // 2)

    def tolerance(self, factor: float) -> float:
        """The tolerance of a sum carrying this budget, mm (see the class note)."""
        fallback = _sum_tolerance(self.count, factor)
        if not self.declared:
            return fallback
        return max(PAD_READING_TOLERANCE_MM, min(self.mm, fallback))


def _quote_budget(text, value, factor: float) -> _Rounding:
    """The rounding budget of ONE quote: from its text when given and consistent, else the unit's."""
    rounding = quote_rounding(text, value, factor)
    if rounding is not None:
        return _Rounding(rounding, True, 1)
    return _Rounding(CHAIN_QUOTE_ROUNDING_MM.get(factor, CHAIN_QUOTE_ROUNDING_MM[1.0]), False, 1)


def _parse_stop(stop):
    """One chain stop as (kind, payload): ("edge", None), ("outline", label), ("ref", label), ("pads", names), or None when malformed.

    A stop is the text the model wrote: pad numbers sharing a dimension line
    separated by commas, the bare body outline (CHAIN_EDGE), a drawn outline
    that is not the body ("edge:<label>") or a datum/mounting hole
    ("ref:<label>"). A group mixing pads with an outline or a datum is
    malformed.
    """
    names = _chain_stop_names(stop)
    if not names:
        return None
    if len(names) == 1:
        name, lower = names[0], names[0].lower()
        if lower == CHAIN_EDGE:
            return ("edge", None)
        if lower.startswith(OUTLINE_STOP_PREFIX) and len(name) > len(OUTLINE_STOP_PREFIX):
            return ("outline", name[len(OUTLINE_STOP_PREFIX):].strip())
        if lower.startswith(REFERENCE_STOP_PREFIX) and len(name) > len(REFERENCE_STOP_PREFIX):
            return ("ref", name[len(REFERENCE_STOP_PREFIX):].strip())
    # A group on one dimension line: pads, datums ("ref:<label>", their
    # node) and named outlines may share it; the bare body outline cannot.
    nodes = []
    for name in names:
        lower = name.lower()
        if lower == CHAIN_EDGE:
            return None
        if lower.startswith(REFERENCE_STOP_PREFIX) and len(name) > len(REFERENCE_STOP_PREFIX):
            nodes.append(f"<{REFERENCE_STOP_PREFIX}{name[len(REFERENCE_STOP_PREFIX):].strip()}>")
        elif lower.startswith(OUTLINE_STOP_PREFIX) and len(name) > len(OUTLINE_STOP_PREFIX):
            nodes.append(f"<{OUTLINE_STOP_PREFIX}{name[len(OUTLINE_STOP_PREFIX):].strip()}>")
        elif lower.startswith(OUTLINE_STOP_PREFIX) or lower.startswith(REFERENCE_STOP_PREFIX):
            return None
        else:
            nodes.append(name)
    return ("pads", nodes)


def _stop_node(kind: str, payload, position: int, count: int) -> Optional[str]:
    """The graph node of a parsed stop at `position` of `count`, None for a bare edge in the middle.

    A named outline, like the body, has two sides: at the first stop it is
    its low side, at the last its high side (so a chain may run from one
    side of a flange to the other); in the middle it is one line.
    """
    if kind == "edge":
        if 0 < position < count - 1:
            return None
        return _CHAIN_LOW if position == 0 else _CHAIN_HIGH
    if kind == "outline":
        side = ":low" if position == 0 else ":high" if position == count - 1 else ""
        return f"<{OUTLINE_STOP_PREFIX}{payload}{side}>"
    if kind == "ref":
        return f"<{REFERENCE_STOP_PREFIX}{payload}>"
    return payload[0]


def _is_pad_node(node: str) -> bool:
    """True for a pad number node (not an outline, a datum or an edge anchor)."""
    return not (node.startswith("<") and node.endswith(">"))


def _axis_from_chains(chains, axis: str, factor: float, problems: list, malformed: list,
                      midpoints=None, pad_halves=None) -> tuple:
    """Solve one axis: (positions in mm, rounding budgets per node) of the main component.

    Nodes are pad numbers, the two body outline stops, the named outlines
    ("edge:<label>") and datums ("ref:<label>"); a chain gives
    position(next) - position(previous) = value (kind "consecutive") or
    position(stop i) - position(first stop) = value i (kind
    "from_first_stop"), and the pads of one stop share a position. A stop
    whose `at` is a pad EDGE is converted to the pad centre with
    `pad_halves` ({pad: half extent along this axis, mm}); a pad whose size
    is unknown leaves that chain unread and says why. `midpoints`
    (validated entries of this axis, see `_midpoints_by_axis`) place a pad
    half way between two solved stops (`_place_midpoints`). A chain that
    cannot be read (stops and values that do not match, a bare outline stop
    in the middle, a non-positive value, an unknown kind or anchor) goes to
    `malformed`: a transcription slip, not a fact about the drawing. Two
    paths giving one distance differently go to `problems`: the drawing, as
    transcribed, contradicts itself. Pads outside the component holding most
    pads are left out (nothing printed ties them to the others).

    @return (positions {node: mm}, budgets {node: _Rounding}) of the main
        component; the budgets say how many quotes, with which printed
        roundings, were summed to reach each node from the BFS root.
    """
    links = {}

    def link(a, b, value, budget):
        links.setdefault(a, []).append((b, value, budget))
        links.setdefault(b, []).append((a, -value, budget))

    for index, chain in enumerate(chains, start=1):
        if not isinstance(chain, dict) or chain.get("axis") != axis:
            continue
        stops, values = chain.get("stops"), chain.get("values")
        if (not isinstance(stops, list) or not isinstance(values, list) or len(stops) < 2
                or len(values) != len(stops) - 1):
            malformed.append(tx("catena {axis} n. {index}: {stops} fermate e {values} quote",
                                axis=axis, index=index,
                                stops=len(stops) if isinstance(stops, list) else 0,
                                values=len(values) if isinstance(values, list) else 0))
            continue
        kind = chain.get("kind") or CHAIN_KINDS[0]
        anchors = chain.get("at")
        texts = chain.get("values_text")
        valid = (kind in CHAIN_KINDS
                 and (anchors is None or (isinstance(anchors, list) and len(anchors) == len(stops)
                                          and all(a in PAD_EDGE_ANCHORS for a in anchors))))
        if not (isinstance(texts, list) and len(texts) == len(values)):
            texts = [None] * len(values)
        nodes, groups = [], []
        for position, stop in enumerate(stops):
            parsed = _parse_stop(stop) if valid else None
            node = _stop_node(parsed[0], parsed[1], position, len(stops)) if parsed else None
            if node is None:
                valid = False
                break
            anchor = anchors[position] if anchors is not None else PAD_EDGE_ANCHORS[0]
            if anchor != PAD_EDGE_ANCHORS[0] and parsed[0] != "pads":
                valid = False
                break
            nodes.append((node, anchor))
            groups.append(parsed[1] if parsed[0] == "pads" else [])
        lengths = [_optional_number(value, positive=True) for value in values]
        if not valid or any(length is None for length in lengths):
            malformed.append(tx("catena {axis} n. {index}: fermate o quote non valide",
                                axis=axis, index=index))
            continue
        # A quote to a pad's edge is converted to its centre with the pad
        # size the main answer gave; without it the chain cannot be read.
        unsized = [node for node, anchor in nodes
                   if anchor != PAD_EDGE_ANCHORS[0] and (pad_halves or {}).get(node) is None]
        if unsized:
            malformed.append(tx("catena {axis} n. {index}: quota al bordo del pad {pads} senza la "
                                "dimensione del pad, non usata", axis=axis, index=index,
                                pads=", ".join(unsized)))
            continue
        ends = []
        for (node, anchor), names in zip(nodes, groups):
            for other in names[1:]:
                link(names[0], other, 0.0, _Rounding())
            if names and not _is_pad_node(names[0]) and anchor != PAD_EDGE_ANCHORS[0]:
                anchor = PAD_EDGE_ANCHORS[0]
            if anchor == PAD_EDGE_ANCHORS[0]:
                ends.append(node)
                continue
            half = pad_halves[node]
            edge = f"<{node}@{anchor}>"
            # near edge: centre = edge + half; far edge: centre = edge - half.
            link(edge, node, half if anchor == "near_edge" else -half, _Rounding())
            ends.append(edge)
        budgets = [_quote_budget(text, value, factor) for text, value in zip(texts, values)]
        if kind == "from_first_stop":
            for following, length, budget in zip(ends[1:], lengths, budgets):
                link(ends[0], following, length * factor, budget)
        else:
            for previous, following, length, budget in zip(ends, ends[1:], lengths, budgets):
                link(previous, following, length * factor, budget)
    positions, quotes, components, reported = {}, {}, [], set()
    for start in links:
        if start in positions:
            continue
        positions[start], quotes[start] = 0.0, _Rounding()
        component, queue = [start], [start]
        while queue:
            node = queue.pop()
            for other, step, budget in links[node]:
                expected = positions[node] + step
                if other not in positions:
                    positions[other] = expected
                    quotes[other] = quotes[node] + budget
                    component.append(other)
                    queue.append(other)
                    continue
                # The loop closed here sums at most this many printed quotes.
                tolerance = (quotes[node] + quotes[other] + budget).tolerance(factor)
                if (abs(positions[other] - expected) > tolerance
                        and frozenset((node, other)) not in reported):
                    # A distance, not a position: the origin of the sums is arbitrary.
                    reported.add(frozenset((node, other)))
                    problems.append(tx(
                        "fra {a} e {b} le catene {axis} danno {direct:.2f} mm e, per un'altra "
                        "via, {other:.2f} mm", a=_chain_node_name(node), b=_chain_node_name(other),
                        axis=axis, direct=abs(step), other=abs(positions[other] - positions[node])))
        components.append(component)
    by_midpoint = _place_midpoints(midpoints or (), axis, positions, quotes, components, problems,
                                   malformed, factor)
    for pad, lows, highs in midpoints or ():
        low, high = _first_solved(lows, positions), _first_solved(highs, positions)
        # Page order is the contract; between two pads it is observable.
        if (low is not None and high is not None and positions[low] > positions[high]
                and _owner_of(components, low) == _owner_of(components, high)):
            malformed.append(tx("linea d'asse del pad {pad}: fermate {a} e {b} non in ordine "
                                "sulla pagina", pad=pad, a=_chain_node_name(low),
                                b=_chain_node_name(high)))
    if not components:
        return {}, {}
    # Chosen on chain-placed pads only: a centreline is an interpretation
    # and must not decide which group of chains survives.
    main = max(components,
               key=lambda nodes: sum(1 for n in nodes if _is_pad_node(n) and n not in by_midpoint))
    return {node: positions[node] for node in main}, {node: quotes[node] for node in main}


def _first_solved(candidates, positions: dict):
    """The first node of `candidates` that the chains solved, or None."""
    return next((node for node in candidates if node in positions), None)


def _owner_of(components: list, node: str):
    """Index of the chain component holding `node`, or None (private helper)."""
    for index, nodes in enumerate(components):
        if node in nodes:
            return index
    return None


def _place_midpoints(midpoints, axis: str, positions: dict, quotes: dict, components: list,
                     problems: list, malformed: list, factor: float) -> set:
    """Place the pads declared on a drawn centreline half way between two solved stops.

    A drawing often leaves a hole without a quote on one axis because it
    lies on a dash-dot centreline (the symmetry axis of two rows, or of the
    body): a drafting convention, not a missing quote. The model DECLARES
    the centreline it sees (`midpoints`: pad, the two stops it runs
    between, each given as the candidate nodes of `_midpoints_by_axis`);
    the mean is the program's, like every chain sum. Iterated to a fixed
    point (a centreline may run through another centreline pad); a stop
    still unsolved on this axis leaves the pad unplaced, never guessed. The
    two stops must belong to one chain component: a centreline cannot tie
    two groups of pads no quote connects. Two stops solved at one position
    are no centreline (a hole on the LINE of a row is another claim, which
    the schema does not express): reported in `malformed`, the pad left
    unplaced. A pad a chain already places in the SAME component is CHECKED
    against the mean (a difference is a contradiction of the transcription,
    `problems`, as between two chains); a pad placed in ANOTHER component
    (quoted only to pads the stops' chains do not reach) is a constraint,
    not a contradiction: its whole component is moved so the pad sits at
    the mean and merged (generality audit 2026-11-04). The tolerance is the
    rounding budget of the quotes summed (`_Rounding`): the mean of two
    sums carries half their roundings, plus the pad's own.

    @return The pads placed by a centreline (not by a chain), so the main
        component is chosen on chain-placed pads only.
    """
    owner = {node: index for index, nodes in enumerate(components) for node in nodes}
    pending, by_midpoint = list(midpoints), set()
    placed = True
    while pending and placed:
        placed, remaining = False, []
        for pad, lows, highs in pending:
            low, high = _first_solved(lows, positions), _first_solved(highs, positions)
            if low is None or high is None or owner.get(low) != owner.get(high):
                remaining.append((pad, lows, highs))
                continue
            mean = (positions[low] + positions[high]) / 2
            # Half the roundings of the two stops' sums.
            summed = (quotes[low] + quotes[high]).half()
            if abs(positions[high] - positions[low]) <= summed.tolerance(factor):
                malformed.append(tx(
                    "lungo {axis} le fermate {a} e {b} della linea d'asse del pad {pad} "
                    "coincidono", axis=axis, a=_chain_node_name(low), b=_chain_node_name(high),
                    pad=pad))
                continue
            if pad in positions and owner[pad] == owner[low]:
                if abs(positions[pad] - mean) > (summed + quotes[pad]).tolerance(factor):
                    problems.append(tx(
                        "lungo {axis} il pad {pad} dista {off:.2f} mm dalla linea d'asse "
                        "dichiarata fra {a} e {b}", axis=axis, pad=pad,
                        off=abs(positions[pad] - mean), a=_chain_node_name(low),
                        b=_chain_node_name(high)))
                continue
            if pad in positions:
                # Another component hangs on this pad: move it onto the mean.
                shift, source = mean - positions[pad], owner[pad]
                for node in components[source]:
                    positions[node] += shift
                    quotes[node] = quotes[node] + summed
                    owner[node] = owner[low]
                components[owner[low]].extend(components[source])
                components[source] = []
            else:
                positions[pad], quotes[pad] = mean, summed
                components[owner[low]].append(pad)
                owner[pad] = owner[low]
            by_midpoint.add(pad)
            placed = True
        pending = remaining
    return by_midpoint


def _midpoint_stop(stop, side) -> Optional[list]:
    """The candidate nodes of one `between` stop of a centreline, None when malformed.

    A pad group ("1,2") is identified by ANY of its names (the group shares
    one position); the bare outline needs `side` ("low"/"high") unless both
    stops are the outline (then the first is low and the second high);
    "edge:<label>" and "ref:<label>" are their nodes.
    """
    parsed = _parse_stop(stop) if isinstance(stop, str) else None
    if parsed is None:
        return None
    kind, payload = parsed
    if kind == "edge":
        if side not in EDGE_SIDES:
            return None
        return [_CHAIN_LOW if side == "low" else _CHAIN_HIGH]
    if kind == "pads":
        return list(payload)
    return [_stop_node(kind, payload, 1, 3)]


def _midpoints_by_axis(midpoints, malformed: list) -> dict:
    """Validate the declared centrelines: {axis: [(pad, low candidates, high candidates)]} and the malformed ones.

    A valid entry names an axis, one pad number and exactly two DIFFERENT
    stops in page order, each a pad group (any name of the group identifies
    it), a named outline or datum, or the body outline: a bare "edge"
    needs `edge_side` ("low"/"high", which outline it is) unless BOTH stops
    are the outline (then the first is the low one and the second the high
    one). Anything else is a transcription slip reported in `malformed`,
    never read as a position.

    @note The side of the outline is DECLARED (`edge_side`, review 2026-11-04):
        a mean is symmetric, so the program cannot verify it. Two pad stops
        written in the wrong order are caught where observable:
        `_place_midpoints` sees their solved positions, and
        `_axis_from_chains` reports the pair as a slip.
    """
    valid = {"x": [], "y": []}
    for index, entry in enumerate(midpoints if isinstance(midpoints, list) else [], start=1):
        axis = entry.get("axis") if isinstance(entry, dict) else None
        pad = _pad_number_text(entry.get("pad")) if isinstance(entry, dict) else ""
        between = entry.get("between") if isinstance(entry, dict) else None
        stops = ([name.strip() for name in between if isinstance(name, str)]
                 if isinstance(between, list) else [])
        side = entry.get("edge_side") if isinstance(entry, dict) else None
        both_edges = len(stops) == 2 and all(s.lower() == CHAIN_EDGE for s in stops)
        lows = highs = None
        if (axis in valid and pad and pad.lower() != CHAIN_EDGE and len(stops) == 2
                and len(stops) == len(between) and all(stops) and pad not in stops
                and (both_edges or stops[0].lower() != stops[1].lower())):
            lows = _midpoint_stop(stops[0], "low" if both_edges else side)
            highs = _midpoint_stop(stops[1], "high" if both_edges else side)
        if not lows or not highs or (both_edges and side not in (None, "low", "high")):
            malformed.append(tx("linea d'asse n. {index}: voce non valida", index=index))
            continue
        valid[axis].append((pad, lows, highs))
    return valid


def pads_from_chains(chains, factor: float = 1.0, midpoints=None, pad_sizes=None) -> dict:
    """@brief Pad centres added up from the dimension chains the model transcribed.

    @param chains `chains` of the report_pad_positions answer: per chain an
        axis ("x" to the right, "y" down the page), its `kind` (CHAIN_KINDS,
        consecutive by default), its stops in increasing order (pad numbers
        sharing a dimension line, comma-separated; CHAIN_EDGE for the body
        outline at either end; "edge:<label>" for another drawn outline;
        "ref:<label>" for a datum or mounting hole), the printed values
        between consecutive stops (or from the first stop), optionally their
        printed texts (`values_text`, for the rounding) and the anchor of
        each stop (`at`, PAD_EDGE_ANCHORS).
    @param factor Drawing unit to millimetres.
    @param midpoints `midpoints` of the answer, or None: pads that one axis
        does not quote because they lie on a drawn centreline half way
        between two stops ({"axis", "pad", "between": [low, high],
        "edge_side"}, stops as in the chains). The mean is computed here
        (`_place_midpoints`), the model only declares the centreline it sees.
    @param pad_sizes {pad number: (extent along X, extent along Y) in mm} of
        the pads the main answer described, or None: needed only to convert
        a quote to a pad's edge into its centre.
    @return {"pads": {number: (x, y)} in mm from an arbitrary origin,
        "problems": [contradictions], "malformed": [unreadable chains and
        centreline entries], "spans": {"x": mm or None, "y": mm or None,
        "edges": {axis: (low, high)}}, "span_quotes": {axis: quotes summed
        for the span}, "span_rounding": {axis: rounding mm of the span, or
        None when no text gave it}, "references": {label: (x or None,
        y or None)} of the datums, "unplaced": [number]}.
    @note The sums are the program's, never the model's: a model that adds a
        chain up wrong (a quote dropped while summing) cannot pass that error
        on. What remains is the transcription; it is checked where the
        drawing allows it: two chains reaching one pad must agree, a
        centreline pad a chain also places must sit at the mean, and a
        chain from outline to outline gives a span to compare with the body
        (apply_dedicated_pads). A pad that no chain or centreline ties to the
        others on both axes is listed in "unplaced", never guessed.
    @note Limits, declared: these checks see only what changes a SUM. Two
        quotes transposed, two pad labels swapped or a whole axis read in
        reverse keep every distance sum and every span, so the result is
        never trusted over another reading on their strength (see
        apply_dedicated_pads). A centreline the model declares wrongly
        between two stops it does not run between is caught only when a
        chain also reaches that pad, or by the drawn holes
        (apply_dedicated_pads). A quote to a pad's edge needs the pad size
        from the main answer: without it the pad stays unplaced, said why.
    """
    problems, malformed = [], []
    chains = chains if isinstance(chains, list) else []
    centred = _midpoints_by_axis(midpoints, malformed)
    solved, spans, span_quotes, span_rounding = {}, {}, {}, {}
    for index, axis in enumerate(("x", "y")):
        halves = {number: size[index] / 2 for number, size in (pad_sizes or {}).items()
                  if isinstance(size, (tuple, list)) and len(size) == 2
                  and _optional_number(size[index], positive=True) is not None}
        solved[axis], quotes = _axis_from_chains(chains, axis, factor, problems, malformed,
                                                 centred[axis], halves)
        ends = (solved[axis].get(_CHAIN_LOW), solved[axis].get(_CHAIN_HIGH))
        spans[axis] = ends[1] - ends[0] if None not in ends else None
        # Upper bound: the span runs through the BFS root at worst.
        budget = (quotes[_CHAIN_LOW] + quotes[_CHAIN_HIGH]) if None not in ends else _Rounding()
        span_quotes[axis] = budget.count
        span_rounding[axis] = budget.mm if budget.declared else None
    # Where the outline stops are, per axis (low, high; None when no chain
    # reaches that side): one quoted edge places the body on that axis
    # (_write_chain_pads). Carried inside "spans" so every reading tuple
    # that holds the spans holds them too.
    edges = {axis: (solved[axis].get(_CHAIN_LOW), solved[axis].get(_CHAIN_HIGH))
             for axis in ("x", "y")}
    names = {node for axis in ("x", "y") for node in solved[axis] if _is_pad_node(node)}
    mentioned = set()
    for chain in chains:
        if isinstance(chain, dict) and isinstance(chain.get("stops"), list):
            for stop in chain["stops"]:
                parsed = _parse_stop(stop)
                if parsed and parsed[0] == "pads":
                    mentioned.update(name for name in parsed[1] if _is_pad_node(name))
    for entries in centred.values():
        mentioned.update(pad for pad, _lows, _highs in entries)
    pads = {name: (solved["x"][name], solved["y"][name]) for name in names
            if name in solved["x"] and name in solved["y"]}
    prefix = f"<{REFERENCE_STOP_PREFIX}"
    references = {node[len(prefix):-1]: (solved["x"].get(node), solved["y"].get(node))
                  for axis in ("x", "y") for node in solved[axis] if node.startswith(prefix)}
    if pads:
        # Readable origin for the messages (only distances are compared): the
        # outline's low stop when known, else the lowest pad on that axis.
        shift = []
        for index, axis in enumerate(("x", "y")):
            low = solved[axis].get(_CHAIN_LOW)
            shift.append(low if low is not None else min(p[index] for p in pads.values()))
        pads = {name: (x - shift[0], y - shift[1]) for name, (x, y) in pads.items()}
        edges = {axis: tuple(None if e is None else e - shift[index] for e in edges[axis])
                 for index, axis in enumerate(("x", "y"))}
        references = {label: tuple(None if v is None else v - shift[i] for i, v in enumerate(xy))
                      for label, xy in references.items()}
    spans = dict(spans, edges=edges)
    return {"pads": pads, "problems": problems, "malformed": malformed, "spans": spans,
            "span_quotes": span_quotes, "span_rounding": span_rounding,
            "references": references, "unplaced": sorted(mentioned - set(pads))}


def read_pad_positions(documents, *, model, call, prompt=None, progress=None,
                       pad_sizes=None) -> Optional[dict]:
    """@brief Ask the model, alone, for the dimension chains of the numbered pads.

    @param documents Image documents of `numbering_documents`.
    @param model Model identifier of the analysis.
    @param call The logged, cancellable call wrapper of the analysis.
    @param pad_sizes {pad number: (extent along X, extent along Y) in mm} of
        the main answer's pads, or None: only to convert a quote to a pad's
        edge into its centre (`pads_from_chains`).
    @param prompt System prompt; None means DEFAULT_PAD_POSITIONS_PROMPT.
    @param progress Optional log callback.
    @return None without documents; otherwise {"view", "pads": {number: (x, y) in mm},
        "notes"} plus the chain results of pads_from_chains ("problems",
        "spans", "edges", "unplaced"), or "failed"/"discarded" with the reason
        and empty "pads".
    @throws ExtractionCancelled When the user cancels during the call; every
        other failure (call, unreadable drawing) is returned as "failed", and
        an answer without a declared drawing unit as "discarded".
    @note Best-effort: a failure never loses the main answer. The model
        transcribes the chains; the program converts the declared unit and
        adds them up (pads_from_chains). The origin is arbitrary and is never
        compared, only distances are.
    """
    if not documents:
        return None
    try:
        # The documents are read inside the try: a drawing that vanished or
        # is locked since the main call raises ExtractionError, and this
        # best-effort reading must never lose the paid main answer (M5).
        blocks = []
        for index, path in enumerate(documents):
            blocks.append({"type": "text", "text": f"FILE {index}: {path.name}"})
            blocks.append(_file_to_content_block(path, model))
        blocks.append({"type": "text", "text": (
            "Transcribe the dimension chains of the numbered holes or lands of the drawing and call "
            "the tool.")})
        # The profile text, or the default's bytes when it is the default
        # (V4-5); the request declares the field it sends (V4-6).
        sent = effective_text("pad_positions_prompt", {"pad_positions_prompt": prompt})
        with prompt_sending({"pad_positions_prompt": sent}):
            data = call(model=model, system_prompt=sent,
                        content_blocks=blocks, tool=_pad_positions_tool(),
                        max_tokens=PAD_POSITIONS_MAX_TOKENS, has_pdf=False)
    except ExtractionCancelled:
        raise
    except (OpenRouterError, ToolArgumentsError, ExtractionError) as exc:
        if progress:
            progress(tx("Lettura dedicata delle posizioni dei pad non riuscita ({exc}).", exc=exc))
        return {"view": "unknown", "pads": {}, "notes": "", "failed": str(exc)}
    if not isinstance(data, dict):
        return {"view": "unknown", "pads": {}, "notes": "", "discarded": tx("risposta non strutturata")}
    # The program's working state is never taken from the model (review V3-5).
    data = without_private_keys(data)
    view = data.get("contacts_view")
    view = view if view in ("top", "bottom", "unknown") else "unknown"
    factor = _declared_unit_factor(data)
    if factor is None:
        # Chains without a declared unit cannot be put in mm: unverifiable,
        # never read as millimetres (L11).
        reason = _undeclared_unit_reason()
        if progress:
            progress(tx("Lettura dedicata delle posizioni dei pad scartata ({exc}).", exc=reason))
        return {"view": view, "pads": {}, "notes": "", "discarded": reason}
    solved = pads_from_chains(data.get("chains"), factor, data.get("midpoints"), pad_sizes)
    notes = data.get("notes")
    return dict(solved, view=view, notes=notes if isinstance(notes, str) else "",
                holes=printed_holes(data.get("printed_holes"), factor),
                leads=printed_terminals(data.get("printed_terminals"), factor))


def without_private_keys(answer):
    """@brief Return a model answer without the keys the program reserves for its own state.

    The extractor keeps its working state in the answer dictionary under
    keys that begin with «_» (`_dedicated_leads`, `_dedicated_holes`,
    `_pin_map_conflict`, a pin's `_hole_rounding_mm`, a side's
    `_contact_style_evidence`, ...). No tool schema declares such a field,
    so one in a model answer can only be an injection that would pass for a
    conclusion of the program (review V3-5, 2026-11-11): it is dropped at
    every depth, in objects inside lists too.

    @param answer The answer as the provider returned it, any type.
    @return A copy with every «_»-prefixed key of every object removed; a
        value that is neither an object nor a list is returned as it is.
    """
    if isinstance(answer, dict):
        return {key: without_private_keys(value) for key, value in answer.items()
                if not (isinstance(key, str) and key.startswith("_"))}
    if isinstance(answer, list):
        return [without_private_keys(value) for value in answer]
    return answer


def printed_terminals(entries, factor: float) -> dict:
    """@brief The terminal sections a dedicated pad reading transcribed, per pad, in mm.

    @param entries `printed_terminals` of the report_pad_positions answer:
        per printed quote the pad numbers it names ("pads", comma-separated),
        the shape ("round" with `diameter`, "flat" with `width` and
        `thickness`) and the text as printed.
    @param factor Drawing unit to millimetres.
    @return {pad number: {"lead": {"shape": "round", "diameter_mm": d} or
        {"shape": "flat", "width_mm": w, "thickness_mm": t}, "rounding_mm": r,
        "text": t}}, the lead in the contract of `core.tht_estimates`, r the
        coarsest rounding of its transcribed sizes (`_printed_hole_value`).
        A malformed entry (no pad, an unknown shape, a size of that shape
        missing or not a finite number > 0) is left out; a pad named by two
        entries with different terminals keeps neither, with "conflict":
        True -- the reading contradicts itself.
    @note Transcriptions only, the second reading of a pad's terminal
        (`_confirmed_lead_word`): nothing here is estimated or derived.
    """
    fields = {"round": (("diameter", "diameter_mm"),),
              "flat": (("width", "width_mm"), ("thickness", "thickness_mm"))}
    leads = {}
    for entry in entries if isinstance(entries, list) else ():
        # Review V3-3 (2026-11-11): a shape sent as a list or an object is
        # unhashable and made the membership test raise; only a string can
        # name a shape. Pads are named by a string (or a bare number).
        shape = entry.get("shape") if isinstance(entry, dict) else None
        pads = entry.get("pads") if isinstance(entry, dict) else None
        if not isinstance(shape, str) or shape not in fields:
            continue
        if isinstance(pads, bool) or not isinstance(pads, (str, int)):
            continue
        text = str(entry.get("text") or "").strip()
        lead, rounding = {"shape": shape}, 0.0
        for given, key in fields[shape]:
            size = _optional_number(entry.get(given), positive=True)
            size_mm, size_rounding = ((None, None) if size is None
                                      else _printed_hole_value(text, size, factor))
            # Review V3-4: a finite size in the drawing unit may overflow
            # once converted (1e308 inches); a size must stay finite and > 0
            # in millimetres too.
            if size_mm is None or not math.isfinite(size_mm) or size_mm <= 0:
                lead = None
                break
            lead[key] = round(size_mm, 6)
            rounding = max(rounding, size_rounding)
        if lead is None:
            continue
        value = {"lead": lead, "rounding_mm": rounding, "text": text}
        for number in _chain_stop_names(pads):
            previous = leads.get(number)
            if previous is None:
                leads[number] = value
            elif previous.get("lead") != lead:
                leads[number] = {"conflict": True}
    return leads


def printed_holes(entries, factor: float) -> dict:
    """@brief The printed hole callouts of a dedicated pad reading, per pad, in mm.

    @param entries `printed_holes` of the report_pad_positions answer: per
        callout the pad numbers it names ("pads", comma-separated), the printed
        diameter (the slot's width for a slot), an optional printed slot length
        and the text as printed.
    @param factor Drawing unit to millimetres.
    @return {pad number: {"drill_mm": d, "drill_length_mm": l or None, "text": t,
        "rounding_mm": r}}, r the rounding of the transcription (`_printed_hole_value`).
        A malformed callout (no pad, a diameter that is not a finite number > 0)
        is left out; a pad named by two callouts with different values keeps
        neither, with "conflict": True -- the reading contradicts itself, and
        a choice between its two values would be a guess.
    @note Transcriptions only: nothing here is estimated or derived.
    """
    holes = {}
    for entry in entries if isinstance(entries, list) else ():
        if not isinstance(entry, dict):
            continue
        diameter = _optional_number(entry.get("diameter"), positive=True)
        if diameter is None:
            continue
        length = _optional_number(entry.get("slot_length"), positive=True)
        text = str(entry.get("text") or "").strip()
        # Each size checked against the printed text: its rounding in mm,
        # and a value given as nominal plus tolerance brought to the nominal
        # (`_printed_hole_value`, review G6).
        drill_mm, rounding = _printed_hole_value(text, diameter, factor)
        if length is not None:
            length_mm, length_rounding = _printed_hole_value(text, length, factor)
            rounding = max(rounding, length_rounding)
        value = {"drill_mm": round(drill_mm, 6),
                 "drill_length_mm": None if length is None else round(length_mm, 6),
                 "text": text, "rounding_mm": rounding}
        for number in _chain_stop_names(entry.get("pads") or ""):
            previous = holes.get(number)
            if previous is None:
                holes[number] = value
            elif (previous.get("drill_mm"), previous.get("drill_length_mm")) != (
                    value["drill_mm"], value["drill_length_mm"]):
                holes[number] = {"conflict": True}
    return holes


def _pad_sizes_mm(pins, factor: Optional[float]) -> dict:
    """{pad number: (extent along X, extent along Y) in mm} of the pins that give both sizes."""
    sizes = {}
    if factor is None:
        return sizes
    for pin in pins or ():
        if not isinstance(pin, dict):
            continue
        length = _optional_number(pin.get("pad_length_mm"), positive=True)
        width = _optional_number(pin.get("pad_width_mm"), positive=True)
        number = _pad_number_text(pin.get("number"))
        if number and length is not None and width is not None:
            sizes[number] = (length * factor, width * factor)
    return sizes


def _respell_reading(reading: dict, numbers) -> dict:
    """A copy of a pad reading whose pad numbers take the main answer's spelling, case-insensitively.

    Pad numbers are text and compared as such; a reading that writes «a1»
    for the answer's «A1» names the same pad (review 2026-11-04, low
    finding). A reading name is respelled only when exactly one answer
    number matches it ignoring case and no reading name already has that
    spelling; everything else stays as written.
    """
    by_lower = {}
    for number in numbers:
        by_lower.setdefault(str(number).lower(), []).append(str(number))
    pads = reading.get("pads") or {}

    def respelled(name):
        matches = by_lower.get(str(name).lower()) or []
        if len(matches) == 1 and matches[0] != name and matches[0] not in pads:
            return matches[0]
        return name

    copy_of = dict(reading)
    copy_of["pads"] = {respelled(name): xy for name, xy in pads.items()}
    copy_of["unplaced"] = sorted({respelled(name) for name in reading.get("unplaced") or ()})
    return copy_of


#: Fields a structured question may concern (`ExtractedParams.questions`):
#: the side the pads are seen from, the body height between two printed
#: candidates, the pad layout between candidate readings, and whether a
#: height equal to a plan size is a square section or a rotated view.
QUESTION_FIELDS = ("contacts_view", "body_height_mm", "pad_layout", "height_equals_plan")


def question_key(field: str, values) -> str:
    """@brief The stable key of a structured question: its field and its sorted candidate values.

    @param field One of QUESTION_FIELDS.
    @param values The candidate values offered (any JSON-able scalars).
    @return "<field>:<value>|<value>..." with the values as sorted text, so the
        same doubt asked again in a later analysis has the same key and a
        saved answer applies without a new question.
    """
    return field + ":" + "|".join(sorted(str(value) for value in values))


def _answer_identity(field: str, value) -> str:
    """What a chosen option stands for, comparable across questions of one field.

    A "pad_layout" option is "<sources>:<layout digest>": the layout is the
    digest, whichever reading brought it. Any other value is its own text.
    """
    text = str(value)
    return text.rsplit(":", 1)[-1] if field == "pad_layout" else text


#: Separator, in a "contacts_view" question key, of the digests of the
#: layout's mirror images (`_mirror_suffix`): «<numbers and digest>~mirror:<a>,<b>».
#: A key saved before 2026-11-11 has none.
_MIRROR_MARK = "~mirror:"


def _mirror_suffix(pads: dict) -> str:
    """The key suffix naming the digests of a layout mirrored left-right and top-bottom (`_layout_digest`).

    A drawing seen from the other side of the board is the mirror image of
    this one about a vertical or a horizontal page axis (the two differ by a
    half turn, which the digest does not forget), so both are recorded.
    """
    def zero(value):
        # -0.0 + 0.0 is 0.0: a mirrored 0 must spell like the original.
        return value + 0.0

    across = {number: (zero(-x), zero(y)) for number, (x, y) in pads.items()}
    down = {number: (zero(x), zero(-y)) for number, (x, y) in pads.items()}
    return _MIRROR_MARK + ",".join(sorted({_layout_digest(across), _layout_digest(down)}))


def _contacts_view_key_parts(body: str) -> tuple:
    """(numbers-and-digest list body, set of mirror digests or None) of a "contacts_view" key body."""
    head, mark, tail = body.rpartition(_MIRROR_MARK)
    if not mark:
        return body, None
    return head, set(tail.split(","))


def _layout_digest_of(body: str, other: str):
    """The layout digest of key body `body` against `other` (same pads): the one entry `body` has and `other` lacks."""
    from collections import Counter
    extra = Counter(body.split("|")) - Counter(other.split("|"))
    return next(iter(extra)) if sum(extra.values()) == 1 else None


def _same_pads_other_layout(first: str, second: str) -> bool:
    """Whether two "contacts_view" key bodies name the same pad numbers with another layout digest.

    The key of that question is the sorted pad numbers AND the layout digest
    in one "|" list (`_require_declared_view`), so the digest sits somewhere
    among the numbers. Two such lists that differ by exactly one entry on
    each side differ by the digest alone: the digest covers the numbers, so
    one digest can never belong to two different number sets. No pattern is
    matched on the entries themselves.
    """
    from collections import Counter
    a, b = Counter(first.split("|")), Counter(second.split("|"))
    return sum((a - b).values()) == 1 and sum((b - a).values()) == 1


def answers_contradict(key: str, value, other_key: str, other_value) -> bool:
    """@brief Whether two saved answers, under two different question keys, cannot both be true.

    @param key Key of one answered question (`question_key`).
    @param value The option value chosen for it.
    @param other_key Key of another answered question.
    @param other_value The option value chosen for that one.
    @return True when the two answers are about the same thing and disagree:
        - "contacts_view": the same pad numbers (the keys differ only by the
          layout digest). When one layout is the MIRROR image of the other
          (the key of a question asked since 2026-11-11 carries its mirror
          digests, `_mirror_suffix`), the two readings are the two faces of
          one drawing: the answers agree when they name OPPOSITE sides
          («top» for one, «bottom» for its mirror image, review E6) and
          contradict when they name the same side. Otherwise -- layouts that
          are not mirror images, or an older key without mirror digests,
          which cannot say -- two different sides contradict: one part has
          one side from which its drawing is seen, so the question is asked
          again rather than one answer reused;
        - "pad_layout", "body_height_mm": two different candidates were
          chosen and the candidate of one question was ALSO on offer in the
          other, where it was not chosen (the layout or the printed number
          the user called right in one question, wrong in the other).
        False for different fields, for one same key and for any other field.
    @note The saved answers carry no date (user, 2026-11-10: a project held
        «top» and «bottom» for the same five pads under two layout digests,
        and the analysis applied the older one against the newer). So the
        program cannot tell which of two contradicting answers is the newer:
        neither is applied by itself (`contradicting_answers`), the question
        is asked again, and storing the new answer drops the others
        (`remember_answer`).
    """
    field, _sep, body = str(key).partition(":")
    other_field, _sep, other_body = str(other_key).partition(":")
    if field != other_field or key == other_key:
        return False
    if field == "contacts_view":
        body, mirrors = _contacts_view_key_parts(body)
        other_body, other_mirrors = _contacts_view_key_parts(other_body)
        if not _same_pads_other_layout(body, other_body):
            return False
        digest, other_digest = _layout_digest_of(body, other_body), _layout_digest_of(other_body, body)
        mirrored = ((mirrors is not None and other_digest in mirrors)
                    or (other_mirrors is not None and digest in other_mirrors))
        if mirrored:
            return str(value) == str(other_value)
        return str(value) != str(other_value)
    if field in ("pad_layout", "body_height_mm"):
        chosen, other_chosen = _answer_identity(field, value), _answer_identity(field, other_value)
        offered = {_answer_identity(field, option) for option in body.split("|")}
        other_offered = {_answer_identity(field, option) for option in other_body.split("|")}
        return chosen != other_chosen and (chosen in other_offered or other_chosen in offered)
    return False


def contradicting_answers(answers, key: str, value=None) -> list:
    """@brief The keys of the saved answers that contradict the answer to `key`.

    @param answers {question key: chosen value} saved with the project; any
        other type counts as empty.
    @param key Key of the question at hand.
    @param value The answer to test; None (default) tests the answer saved
        under `key`, and without one nothing contradicts.
    @return Sorted keys, other than `key`, whose answers `answers_contradict`
        that one. Not empty means: the saved answer must NOT be applied by
        itself, the question is asked again.
    """
    if not isinstance(answers, dict):
        return []
    if value is None:
        if key not in answers:
            return []
        value = answers[key]
    return sorted(other for other, other_value in answers.items()
                  if isinstance(other, str) and answers_contradict(key, value, other, other_value))


def remember_answer(answers, key: str, value) -> dict:
    """@brief The saved answers with a new one stored and the ones it supersedes dropped.

    @param answers {question key: chosen value} saved so far (not modified);
        any other type counts as empty.
    @param key Key of the question just answered.
    @param value The option value the user chose now.
    @return A new dict holding `value` under `key`, without the answers that
        contradict it (`contradicting_answers`): the answer given now is the
        newest, so an older one never wins over it in a later analysis.
        Answers to other questions, and to other layouts or candidates that
        do not contradict it, are kept.
    """
    kept = dict(answers) if isinstance(answers, dict) else {}
    for other in contradicting_answers(kept, key, value):
        kept.pop(other, None)
    kept[key] = value
    return kept


def _ask(data: dict, field: str, text: str, options: list, evidence=None, candidates=None,
         key_values=None, key_suffix: str = ""):
    """Record a structured question in the answer, or return the answer already known for it.

    `options` is [{"label": str, "value": JSON-able}]; `candidates`, when
    given, maps str(value) -> the payload `apply_answers` needs to apply
    that value without a paid call (kept in data["_candidates"][key]).
    `evidence` is {"document": file name, "crop": [x0, y0, x1, y1] px or
    None} or None. The key is `question_key` of the option values, or of
    `key_values` when the options are generic words (keep/clear) and the
    doubt is about a number; `key_suffix` is appended to the key as given
    (`_MIRROR_MARK` of the "contacts_view" question). When data["_answers"] (the answers saved by
    an earlier analysis, `extract_pin_header_params(answers=...)`) holds
    the key, no question is recorded and that value is returned so the
    caller applies it at once; otherwise None. A saved answer that another
    saved answer contradicts (`contradicting_answers`) is not applied: the
    answers carry no date, so the question is asked again and says why.
    """
    key = question_key(field, key_values if key_values is not None
                       else [option["value"] for option in options]) + key_suffix
    answers = data.get("_answers") if isinstance(data.get("_answers"), dict) else {}
    allowed = {str(option["value"]): option["value"] for option in options}
    # An answer saved under the same key without the suffix (a project saved
    # before the suffix existed) answers the same question.
    saved = key if key in answers or not key_suffix else key[:-len(key_suffix)]
    if saved in answers and str(answers[saved]) in allowed:
        if not contradicting_answers(answers, saved):
            return allowed[str(answers[saved])]
        # Two saved answers that cannot both be true: neither decides.
        text = text + " " + tx("(Domanda riproposta: le risposte salvate per questo progetto si "
                               "contraddicono, vale quella data ora.)")
    questions = data.setdefault("_questions", [])
    if not any(question["key"] == key for question in questions):
        questions.append({"key": key, "field": field, "text": text, "options": list(options),
                          "evidence": evidence})
    if candidates:
        data.setdefault("_candidates", {})[key] = {str(value): payload
                                                   for value, payload in candidates.items()}
    return None


def _question_evidence(data: dict) -> Optional[dict]:
    """The evidence of a question about the drawing: the first drawing document, no crop."""
    names = data.get("_question_documents")
    if isinstance(names, list) and names:
        return {"document": str(names[0]), "crop": None}
    return None


def _ask_height(data: dict, field: str, main_mm: float, dedicated_mm: float, factor: float,
                reason: str, geometry) -> bool:
    """Ask which of two printed heights is the body's, or apply the saved answer.

    @return True when a saved answer was applied (the value is written),
        False when the question was recorded (nothing written).
    """
    options = [{"label": tx("{value:.2f} mm (risposta principale)", value=main_mm),
                "value": round(main_mm, 4)},
               {"label": tx("{value:.2f} mm (lettura dedicata)", value=dedicated_mm),
                "value": round(dedicated_mm, 4)}]
    chosen = _ask(data, "body_height_mm",
                  tx("Altezza del corpo: le letture danno due quote stampate diverse ({reason}). "
                     "Quale misura il corpo perpendicolarmente al piano di appoggio?",
                     reason=reason), options, _question_evidence(data))
    if chosen is None:
        return False
    data[field] = round(float(chosen) / factor, 6)
    if geometry is not None and field != "pin_length_3d_mm":
        geometry[field] = data[field]
    _set_program_source(data, field, USER_SOURCE)
    data.pop("_body_conflict", None)
    return True


def _set_program_source(data: dict, field: str, source: str) -> None:
    """Record a provenance the PROGRAM gives `field` (USER_SOURCE, MEASURED_SOURCE).

    Kept apart from the model's `value_sources`, which `_declared_sources`
    reads with the model's vocabulary only: `_value_sources` applies these
    over the declared ones when the result is built.
    """
    data.setdefault("_program_sources", {})[field] = source


def read_body_dimensions(documents, *, model, call, prompt=None, progress=None,
                         enlarged: bool = False) -> Optional[dict]:
    """@brief Ask the model, alone, for the printed outline quotes of the body (plan sizes, height, leads).

    @param documents Image documents of `numbering_documents` (the dimensioned drawings).
    @param model Model identifier of the reading (the chain-reading model).
    @param call The logged, cancellable call wrapper of the analysis.
    @param prompt System prompt; None means DEFAULT_BODY_DIMENSIONS_PROMPT.
    @param progress Optional log callback.
    @param enlarged True sends every drawing as the whole page plus four
        enlarged tiles (`_enlarged_tile_blocks`): the tie-break reading of
        `apply_dedicated_body`, when the two printed lists disagree and the
        digits may be too small for the reader.
    @return None without documents; otherwise {"values": {key: mm},
        "printed": [mm], "notes"} with the keys of `_BODY_READING_KEYS` (and
        "standoff") the model gave and every vertical quote it listed as
        printed, converted to mm by the DECLARED unit; or "failed"/"discarded"
        with the reason and empty "values".
    @throws ExtractionCancelled When the user cancels during the call; any
        other failure is returned as "failed" (best-effort, the paid main
        answer is never lost), an answer without a known unit as "discarded".
    @note Same contract as `read_pad_positions`: the model transcribes, the
        program converts and compares (`apply_dedicated_body`). A value that
        is not a finite positive number is left out, never repaired.
    """
    if not documents:
        return None
    try:
        blocks = []
        for index, path in enumerate(documents):
            if enlarged:
                blocks.extend(_enlarged_tile_blocks(path, model))
            else:
                blocks.append({"type": "text", "text": f"FILE {index}: {path.name}"})
                blocks.append(_file_to_content_block(path, model))
        blocks.append({"type": "text", "text": (
            "Transcribe the printed dimensions of the body envelope of the component and call the "
            "tool.")})
        # The profile text, or the default's bytes when it is the default
        # (V4-5); the request declares the field it sends (V4-6).
        sent = effective_text("body_dimensions_prompt", {"body_dimensions_prompt": prompt})
        with prompt_sending({"body_dimensions_prompt": sent}):
            data = call(model=model, system_prompt=sent,
                        content_blocks=blocks, tool=_body_dimensions_tool(),
                        max_tokens=PAD_POSITIONS_MAX_TOKENS, has_pdf=False)
    except ExtractionCancelled:
        raise
    except (OpenRouterError, ToolArgumentsError, ExtractionError) as exc:
        if progress:
            progress(tx("Lettura dedicata delle quote del corpo non riuscita ({exc}).", exc=exc))
        return {"values": {}, "notes": "", "failed": str(exc)}
    if not isinstance(data, dict):
        return {"values": {}, "notes": "", "discarded": tx("risposta non strutturata")}
    factor = _declared_unit_factor(data)
    if factor is None:
        reason = _undeclared_unit_reason()
        if progress:
            progress(tx("Lettura dedicata delle quote del corpo scartata ({exc}).", exc=reason))
        return {"values": {}, "notes": "", "discarded": reason}
    values = {}
    for key in [key for key, _field, _label in _BODY_READING_KEYS] + ["standoff"]:
        number = _optional_number(data.get(key), positive=True)
        if number is not None:
            values[key] = round(number * factor, 6)
    printed = [{"value": value, "measures": role}
               for value, role in printed_quotes(data.get("printed_vertical_quotes"), factor)]
    notes = data.get("notes")
    notes = notes if isinstance(notes, str) else ""
    leads_point = data.get("leads_point") if data.get("leads_point") in LEADS_POINT else None
    body_kind = data.get("body_quote_kind")
    body_kind = body_kind if body_kind in BODY_QUOTE_KINDS else None
    if progress:
        # The numbers read, for the log and a bench; `notes` is the model's
        # own sentence about the view it used (project log, as every note).
        progress(tx("Lettura dedicata delle quote del corpo: {values}; quote verticali stampate: "
                    "{printed}; vista: {notes}",
                    values=", ".join(f"{key} {value:.2f} mm" for key, value in values.items()) or "-",
                    printed=_printed_text(printed), notes=notes.replace("\n", " ")[:200] or "-"))
    return {"values": values, "printed": printed, "notes": notes, "leads_point": leads_point,
            "body_quote_kind": body_kind}


def _printed_text(quotes) -> str:
    """'16.90 (body_height), 0.40 (standoff)' for the log lines; '-' for an empty list."""
    return ", ".join(f"{value:.2f} ({role})" for value, role in printed_quotes(quotes)) or "-"


def _dedicated_pair_fits_crossed(main_mm, main_read, own_mm, tolerance: float) -> bool:
    """Whether the dedicated plan pair matches the main pair better with its two sizes exchanged.

    @param main_mm [along X, along Y] of the main answer, mm.
    @param main_read [bool, bool]: True for a size the main answer declares
        read or derived, False for an estimate (or an undeclared provenance).
    @param own_mm [along X, along Y] of the dedicated reading, mm.
    @param tolerance Reading tolerance, mm.
    @return True only when the exchanged pairing is STRICTLY better: first by
        the number of main sizes that are readings and find their value in
        the dedicated pair, then by the number of sizes that find it at all.
        A reading counts before an estimate because an estimate says nothing
        about which printed number lies on its axis: letting it keep the
        printed value of the OTHER axis is how one printed number ended up
        on both (review 2026-11-10, A6). Equal scores keep the order as
        declared, so two pairs that agree as given are never turned round
        and two that agree neither way are compared as before.
    """
    def score(pairing):
        agree = [abs(main_mm[i] - own_mm[j]) <= tolerance for i, j in pairing]
        return (sum(1 for hit, (i, _j) in zip(agree, pairing) if hit and main_read[i]), sum(agree))

    return score(((0, 1), (1, 0))) > score(((0, 0), (1, 1)))


def apply_dedicated_body(data: dict, reading: Optional[dict], progress=None, tie_break=None,
                         same_model: bool = False) -> None:
    """@brief Confront the dedicated body-quote reading with the body quotes of the main answer.

    @param data Main answer, still in its declared drawing unit (before
        `_canonicalize_answer`); `package_geometry` carries the same body
        sizes for a single part.
    @param reading Result of `read_body_dimensions`, or None.
    @param progress Optional log callback.
    @param same_model True when `reading` was made by the SAME model that
        gave the main answer (a blank pad model, a provider not ready, or
        the partial re-reading of the height): its printed list is not an
        independent witness of the main list, so the two lists together
        give a value ONE point at most, never the two that settle it
        (generality audit 2026-11-05, A2, the H6 rule of the pads); the
        note says so. A missing main value is still adopted, said
        unconfirmed, as with one point.
    @param tie_break Optional zero-argument callable returning a THIRD
        `read_body_dimensions` result (the drawing enlarged, read by a model
        OTHER than the main one, see `_validate_main_response`) or None when
        no such reader is available. Called at most once, only when the two
        printed lists do not settle the value -- the same score (user,
        2026-11-03: the main model listed its misread «19,3» as printed, the
        dedicated reading «16,9», one point each) or no value printed twice
        (2026-11-04, HLK module) -- AND some candidate already has a point,
        so the third list can bring it to two: from 0-0 a third list reaches
        1 at most and the call would be paid for nothing (review 2026-11-04,
        M13). The third list adds its points; the value ahead with at least
        two wins, anything else stays a conflict. None: no tie-break.
    @note Per quote of `_BODY_READING_KEYS`, in millimetres, within
        `_reading_tolerance` of the main answer's unit:
        - the main answer has no value: the reading's is ADOPTED (written in
          the answer's own unit, provenance "read"), said to be unconfirmed
          by a second reading -- but when any printed list exists, only a
          value some list SUPPORTS (`_height_candidate`) is written: a
          dedicated height equal to a plan size that no list declares a body
          height is not adopted (review 2026-11-04, H1: the HLK module's STEP
          was built 23 mm high, its plan width);
        - the main value is a declared ESTIMATE (`value_sources` not "read"
          nor "derived"; an undeclared provenance counts as an estimate, as
          `_value_sources` does): the printed quote REPLACES it whatever the
          axis, and an estimate the quote agrees with becomes "read"
          (generality audit 2026-11-03: provenance ranks before value);
        - both read and agreeing: confirmed; a height equal to a plan size
          that no list DECLARES a body height is a doubt, not a confirmation
          (M4: a rotated side view makes two readers take the plan width),
          asked as the structured question "height_equals_plan" (square
          section, keep; rotated view, clear) -- a saved answer is applied;
        - both read and disagreeing: a PLAN quote keeps the main value with a
          warning that claims no check (the hole fit bounds it only when a
          closed outline was found, which this function cannot see); a
          VERTICAL quote (`_BODY_VERTICAL_KEYS`) has no witness at all, so
          NEITHER value is used -- the STEP waits -- and the structured
          question "body_height_mm" offers the two values (a saved answer is
          written with provenance "user"). Readings are never averaged.
        A main height the reading did not find (no side view among the
        images) is said to be unverified, so an invented height never
        passes in silence. A main answer whose unit is undeclared cannot be
        compared: nothing is written, the note says why (L11).
    @note The two PLAN quotes are compared as a pair (review 2026-11-10,
        A6). Their order is a fact only when the drawn outline measured it
        (`_align_body_axes`: an exchange, or a confirmation whose drawn
        outline agrees with the order as given only) or an edge-to-edge span of the
        plan prints it (`_plan_prints_body_axes`): the dedicated pair is then
        compared in that order. Otherwise both orders are the readers'
        inferences and the dedicated pair is turned round when it fits the
        main pair better that way, a size the main answer READ counting
        before an estimate (`_dedicated_pair_fits_crossed`): one printed
        number is never written on both axes, and the note says the
        orientation is unverified. The rules above then apply per quote.
    @note VERTICAL quotes with TWO printed lists (user, 2026-11-03,
        «sgraviamo»: the main model computed 18.9/19.3 for a printed 16.9 and
        labelled them "read"; the dedicated transcription read 16.9). Both
        answers list every quote printed in the profile view
        (`printed_vertical_quotes`, each with the ROLE the reader declares:
        `PRINTED_QUOTE_ROLES`); the lists are independent readings of the
        same page, like the two pad readings. A value is SUPPORTED by a list
        when it is within tolerance of a listed quote whose declared role
        does not rule it out, and is not the printed sum of a quote with a
        standoff, lead or overall quote (`_height_candidate`; lists without
        any role fall back to the former value rules). Each value scores one
        point per list that supports it (its own and the other's): two
        points = a printed number read twice. When the two values disagree,
        the one with two points wins over one with fewer (the loser is
        discarded with a warning); otherwise neither is used, `_body_conflict`
        names both, the question is recorded and the STEP waits. Without any
        list the provenance rules above apply. Generality audit 2026-11-03
        (F1-F3): a reading checked only against its own list is a
        self-witness, so a single list never decides alone.
    """
    # Set by the pad confrontation when the drawn outline measured which plan
    # size runs along X (`_exchange_body_sizes`, or a confirmation as given
    # in `_align_body_axes`); consumed here, whatever follows.
    measured_axes = bool(data.pop("_body_axes_measured", False))
    # Set when a printed edge-to-edge span of the plan equals the size the
    # answer gives on its axis (`_plan_prints_body_axes`).
    printed_axes = bool(data.pop("_body_axes_printed", False))
    if not reading or not isinstance(reading, dict):
        return
    if reading.get("failed") or reading.get("discarded"):
        return
    values = reading.get("values") or {}
    if not values:
        if progress:
            progress(tx("Lettura dedicata delle quote del corpo: nessuna quota stampata trovata."))
        return
    axes_note = None
    factor = _declared_unit_factor(data)
    if factor is None:
        note = tx("Lettura dedicata delle quote del corpo non confrontabile: la risposta "
                  "principale non dichiara l'unita' delle quote.")
        data["notes"] = (str(data.get("notes") or "") + "\n" + note).strip()
        if progress:
            progress(note)
        return
    tolerance = _reading_tolerance(factor)
    geometry = data.get("package_geometry") if isinstance(data.get("package_geometry"), dict) else None
    sources = data.get("value_sources") if isinstance(data.get("value_sources"), dict) else None
    # The two plan sizes are a PAIR. Both readers name them «along X» and
    # «along Y» from the views they looked at, and neither order is a fact
    # unless the plan prints it or the witness measured it (user, 2026-11-10;
    # review A6: 16 read x 12 estimated against a dedicated 12 x 16 gave a
    # 16 x 16 body, the printed 16 used for both axes). So the dedicated pair
    # is turned round before the comparison when:
    # - the order of the main pair is fixed (measured on the drawn outline,
    #   `_align_body_axes`, or printed by an edge-to-edge span of the plan,
    #   `_plan_prints_body_axes`) and the dedicated pair has the larger size
    #   on the other axis (D2);
    # - nothing fixes the order and the pair fits the main one better the
    #   other way round (`_dedicated_pair_fits_crossed`): the order kept is
    #   then the main answer's, said to be unverified.
    main_pair = [_optional_number(data.get(key), positive=True)
                 for key in ("body_length_mm", "body_width_mm")]
    own_pair = [values.get("body_length"), values.get("body_width")]
    if None not in main_pair and None not in own_pair:
        order = (main_pair[0] - main_pair[1]) * (own_pair[0] - own_pair[1])
        if (measured_axes or printed_axes) and order != 0:
            if order < 0:
                values = dict(values, body_length=own_pair[1], body_width=own_pair[0])
                axes_note = (tx("Le due quote in pianta della lettura dedicata sono confrontate "
                                "nell'orientamento del corpo misurato sul contorno disegnato.")
                             if measured_axes else
                             tx("Le due quote in pianta della lettura dedicata sono confrontate "
                                "nell'orientamento del corpo fissato dalle quote da bordo a bordo "
                                "della pianta."))
        elif _dedicated_pair_fits_crossed(
                [size * factor for size in main_pair],
                [(sources or {}).get(key) in ("read", "derived")
                 for key in ("body_length_mm", "body_width_mm")], own_pair, tolerance):
            values = dict(values, body_length=own_pair[1], body_width=own_pair[0])
            axes_note = tx("Le due quote in pianta della lettura dedicata tornano con quelle della "
                           "risposta principale solo nell'ordine opposto: confrontate come coppia. "
                           "Nessuna quota della pianta fissa quale delle due corre lungo X: "
                           "orientamento del corpo da verificare sul disegno.")
    confirmed, adopted, warnings = [], [], []
    dedicated_list = printed_quotes(reading.get("printed"))
    main_list = printed_quotes(data.pop("printed_vertical_quotes", None), factor)
    lists_present = bool(dedicated_list or main_list)
    view = (reading.get("notes") or "").replace("\n", " ").strip()
    # The directions the readers saw the leads point to: a profile view
    # drawn with the leads sideways is the rotated-view case of the
    # height-equals-plan question (M4/M7).
    rotated = [point for point in (data.get("leads_point"), reading.get("leads_point"))
               if point in ("left", "right")]
    # Quotes a height can never be when NO role is declared (declared
    # fallback of _height_candidate): the plan sizes of either answer, the
    # standoff and the lead length.
    plan = [value * factor for value in (
        _optional_number(data.get("body_length_mm"), positive=True),
        _optional_number(data.get("body_width_mm"), positive=True)) if value is not None]
    plan += [values[key] for key in ("body_length", "body_width") if values.get(key) is not None]

    def support(value_mm, quotes, field) -> bool:
        """True when `quotes` prints `value_mm` as a candidate of `field`, not as another quote nor a sum."""
        others = plan + [values[key] for key in ("standoff", "lead_length", "body_height")
                         if values.get(key) is not None and _BODY_FIELD_OF.get(key) != field]
        return _height_candidate(value_mm, quotes, others, tolerance, field)

    def points(value_mm, field) -> int:
        """Printed-list points of `value_mm`: one per independent list, one at most from one model."""
        own = support(value_mm, dedicated_list, field) + support(value_mm, main_list, field)
        return min(own, 1) if same_model else own

    def declared_as(value_mm, role) -> bool:
        """True when some list declares a quote equal to `value_mm` with `role`."""
        return any(abs(value_mm - quote) <= tolerance and quote_role == role
                   for quote, quote_role in dedicated_list + main_list)

    third = {"asked": False, "list": [], "values": {}}

    def third_list() -> list:
        """The tie-break reading's printed list (mm), read once; [] without a tie-break or on failure."""
        if not third["asked"]:
            third["asked"] = True
            result = tie_break() if tie_break is not None else None
            if isinstance(result, dict) and not result.get("failed") and not result.get("discarded"):
                third["list"] = printed_quotes(result.get("printed"))
                third["values"] = dict(result.get("values") or {})
                if progress:
                    progress(tx("Spareggio: terza lettura delle quote del corpo sui riquadri ingranditi: "
                                "{values}; quote stampate: {printed}.",
                                values=", ".join(f"{k} {v:.2f} mm" for k, v in third["values"].items()) or "-",
                                printed=_printed_text(third["list"])))
        return third["list"]

    def write(field, value_mm):
        # In the answer's own unit: _canonicalize_answer converts it once.
        data[field] = round(value_mm / factor, 6)
        if geometry is not None and field != "pin_length_3d_mm":
            geometry[field] = data[field]
        if sources is not None:
            sources[field] = "read"

    def drop(field):
        data[field] = None
        if geometry is not None:
            geometry.pop(field, None)

    def conflict_of(label, main_mm, value_mm, lists):
        """Neither value usable: the warning, the conflict mark and the structured question."""
        text = tx(
            "{label}: le due letture non coincidono ({main:.2f} mm per la risposta "
            "principale, {dedicated:.2f} mm per la lettura dedicata) e le quote stampate "
            "non decidono ({lists}): nessuna delle due viene usata finche' non e' "
            "chiarito sul disegno.", label=tx(label).capitalize(), main=main_mm,
            dedicated=value_mm, lists=lists)
        if field == "body_height_mm":
            data["_body_conflict"] = text
            if _ask_height(data, field, main_mm, value_mm, factor, lists, geometry):
                return tx("{label}: scelta dall'utente fra le due quote lette: {value:.2f} mm.",
                          label=tx(label).capitalize(), value=data[field] * factor)
        return text

    for key, field, label in _BODY_READING_KEYS:
        value_mm = values.get(key)
        main = _optional_number(data.get(field), positive=True)
        if value_mm is None:
            if field == "body_height_mm" and main is not None:
                warnings.append(tx(
                    "Altezza del corpo NON verificata dalla lettura dedicata: nessuna quota di "
                    "altezza trovata nelle immagini (vista laterale assente o non leggibile); "
                    "resta il valore della risposta principale ({main:.2f} mm), da verificare.",
                    main=main * factor))
            continue
        declared_reading = (sources or {}).get(field) in ("read", "derived")
        # The vote runs with any printed list, and also without lists when a
        # tie-break exists (so the log says why no third call is paid); a
        # missing main value with no list at all keeps the adoption below.
        if field in _BODY_VERTICAL_KEYS and (lists_present or tie_break is not None) and not (
                main is None and not lists_present):
            # Two independent printed lists vote (see @note); one model's two
            # lists are one witness (@param same_model).
            dedicated_points = points(value_mm, field)
            lists_text = tx("quote stampate lette: principale {main}; dedicata {dedicated}",
                            main=_printed_text(main_list), dedicated=_printed_text(dedicated_list))
            if same_model:
                lists_text += tx("; lette dallo stesso modello, valgono come una sola lista")
            if main is None:
                if dedicated_points >= 1:
                    # Adopted; with one point the «unconfirmed» line below says so.
                    write(field, value_mm)
                    if dedicated_points == 2:
                        confirmed.append(f"{tx(label)} {value_mm:.2f} mm")
                    else:
                        adopted.append(f"{tx(label)} {value_mm:.2f} mm")
                    continue
                # No list supports the value the reading chose (a plan size
                # listed as such, an overall extent): never written (H1).
                text = tx("{label}: la lettura dedicata ({dedicated:.2f} mm) non e' sostenuta da "
                          "nessuna quota stampata con quel ruolo ({lists}): non usata, da "
                          "chiarire sul disegno.", label=tx(label).capitalize(),
                          dedicated=value_mm, lists=lists_text)
                warnings.append(text)
                if field == "body_height_mm":
                    data["_body_conflict"] = text
                continue
            if abs(main * factor - value_mm) <= tolerance:
                if sources is not None and not declared_reading:
                    sources[field] = "read"
                if (field == "body_height_mm" and plan
                        and any(abs(value_mm - size) <= tolerance for size in plan)
                        and (rotated or not declared_as(value_mm, "body_height"))):
                    # Both took a number equal to a plan size and nobody
                    # declared it the body's: square section or rotated view?
                    # A reader that saw the leads drawn sideways (leads_point
                    # left/right) makes the rotated-view reading of that
                    # number as likely as the role it declared, so the role
                    # does not close the question (generality audit
                    # 2026-11-05, M6: the declared signal was decoration).
                    choice = _ask(data, "height_equals_plan", tx(
                        "Altezza del corpo: entrambe le letture danno {value:.2f} mm, uguale a "
                        "una quota in pianta{rotated}. Il corpo ha sezione quadrata (l'altezza "
                        "e' davvero {value:.2f} mm) oppure la vista di profilo e' ruotata e la "
                        "quota e' una misura in pianta?", value=value_mm,
                        rotated=tx(" (terminali disegnati di lato: vista ruotata)") if rotated else ""),
                        [{"label": tx("Sezione quadrata: altezza {value:.2f} mm", value=value_mm),
                          "value": "keep"},
                         {"label": tx("Vista ruotata: {value:.2f} mm e' in pianta, altezza da leggere",
                                      value=value_mm), "value": "clear"}],
                        _question_evidence(data), key_values=[round(value_mm, 2)])
                    if choice == "clear":
                        drop(field)
                        warnings.append(tx(
                            "{label}: {value:.2f} mm e' una misura in pianta (vista ruotata, scelta "
                            "dell'utente): altezza non usata.", label=tx(label).capitalize(),
                            value=value_mm))
                        continue
                    if choice is None:
                        warnings.append(tx(
                            "{label}: le due letture coincidono su {value:.2f} mm, uguale a una "
                            "quota in pianta e da nessuna lista dichiarata altezza del corpo: "
                            "sezione quadrata o vista ruotata, da chiarire.",
                            label=tx(label).capitalize(), value=value_mm))
                        continue
                confirmed.append(tx(label))
                continue
            main_mm = main * factor
            main_points = points(main_mm, field)
            settled = (max(main_points, dedicated_points) >= 2
                       and main_points != dedicated_points)
            if not settled and tie_break is not None:
                if max(main_points, dedicated_points) >= 1:
                    # A tie, or no value printed twice (one list each, or a
                    # value excluded by its role: HLK module, 2026-11-04, 18
                    # printed once, 23 declared the plan width): a third
                    # reading of the enlarged drawing adds its points, see
                    # @param tie_break.
                    extra = third_list()
                    if extra:
                        main_points += support(main_mm, extra, field)
                        dedicated_points += support(value_mm, extra, field)
                elif progress:
                    progress(tx("Spareggio non richiesto per {label}: nessuna delle due quote e' "
                                "sostenuta da una lista stampata, una terza lista non potrebbe "
                                "portarne una a due punti.", label=tx(label)))
            if dedicated_points > main_points and dedicated_points >= 2:
                write(field, value_mm)
                warnings.append(tx(
                    "{label}: il valore della risposta principale ({main:.2f} mm) non e' una "
                    "quota stampata letta da entrambe le letture ({lists}): scartato; usata la "
                    "quota stampata {dedicated:.2f} mm, letta due volte.",
                    label=tx(label).capitalize(), main=main_mm, lists=lists_text,
                    dedicated=value_mm))
            elif main_points > dedicated_points and main_points >= 2:
                warnings.append(tx(
                    "{label}: la lettura dedicata ({dedicated:.2f} mm) non e' una quota stampata "
                    "letta da entrambe le letture ({lists}): scartata; resta {main:.2f} mm, "
                    "letta due volte.", label=tx(label).capitalize(), dedicated=value_mm,
                    lists=lists_text, main=main_mm))
            else:
                drop(field)
                warnings.append(conflict_of(label, main_mm, value_mm, lists_text))
            continue
        if main is None:
            write(field, value_mm)
            adopted.append(f"{tx(label)} {value_mm:.2f} mm")
        elif abs(main * factor - value_mm) <= tolerance:
            if sources is not None and not declared_reading:
                sources[field] = "read"
            confirmed.append(tx(label))
        elif not declared_reading:
            # A declared estimate against a printed quote: the quote wins.
            estimate = main * factor
            write(field, value_mm)
            warnings.append(tx(
                "{label}: la quota stampata ({dedicated:.2f} mm) sostituisce la stima della "
                "risposta principale ({main:.2f} mm).", label=tx(label).capitalize(),
                dedicated=value_mm, main=estimate))
        elif field in _BODY_VERTICAL_KEYS:
            drop(field)
            warnings.append(conflict_of(label, main * factor, value_mm,
                                        tx("nessuna lista di quote stampate")))
        else:
            warnings.append(tx(
                "Le due letture di {label} non coincidono ({main:.2f} mm contro {dedicated:.2f} mm): "
                "resta il valore della risposta principale; da verificare sul disegno.",
                label=tx(label), main=main * factor, dedicated=value_mm))
    lines = [axes_note] if axes_note else []
    if confirmed:
        lines.append(tx("Quote del corpo confermate dalla lettura dedicata: {labels}.",
                        labels=", ".join(confirmed)))
    if adopted:
        lines.append(tx("Quote del corpo prese dalla lettura dedicata delle quote stampate, assenti "
                        "nella risposta principale: {values}. Non confermate da una seconda lettura: "
                        "da verificare sul disegno.", values="; ".join(adopted)))
    lines.extend(warnings)
    if (adopted or warnings) and view:
        lines.append(tx("Lettura dedicata: {view}", view=view[:300]))
    if values.get("standoff") is not None:
        lines.append(tx("Distanziale stampato fra corpo e PCB: {standoff:.2f} mm (informazione: il "
                        "modello STEP non lo usa ancora).", standoff=values["standoff"]))
    if lines:
        data["notes"] = (str(data.get("notes") or "") + "\n" + "\n".join(lines)).strip()
        if progress:
            for line in lines:
                progress(line)


def _pad_summary(pads: dict) -> str:
    """'1@(2.54,0.00), 4@(0.00,7.62)' for the log and the conflict message."""
    return ", ".join(f"{number}@({x:.2f},{y:.2f})"
                     for number, (x, y) in sorted(pads.items(), key=lambda item: item[0]))


def _offset_disagreements(first: dict, second: dict, common, tolerance: float) -> list:
    """Pairs of `common` pads whose SIGNED offsets (dx, dy) differ between two readings.

    Signed offsets, not distances: both readings describe the drawing as it
    appears on the page (X to the right, Y down), so a mirrored or reversed
    transcription, or two labels swapped on a symmetric layout, changes the
    signs while keeping every distance (adversarial review, 2026-10-30). The
    origins are arbitrary and cancel in the offsets.
    """
    pairs = []
    for i, a in enumerate(common):
        for b in common[i + 1:]:
            da = (first[b][0] - first[a][0], first[b][1] - first[a][1])
            db = (second[b][0] - second[a][0], second[b][1] - second[a][1])
            if abs(da[0] - db[0]) > tolerance or abs(da[1] - db[1]) > tolerance:
                pairs.append((a, b, da, db))
    return pairs


def _offset_pairs(pairs) -> str:
    """'1-4: (0.00, 5.08) mm contro (-2.54, 7.62) mm; ...' for the conflict messages."""
    return "; ".join(
        tx("{a}-{b}: ({mx:.2f}, {my:.2f}) mm contro ({dx:.2f}, {dy:.2f}) mm", a=a, b=b,
           mx=da[0], my=da[1], dx=db[0], dy=db[1]) for a, b, da, db in pairs[:6])


def _write_chain_pads(data: dict, pins: list, candidate: dict, factor: float) -> None:
    """Write the chain sums of `candidate` into `pins`, with the body centre placed axis by axis.

    Per axis (user, 2026-10-31, T7N relay: pin 11 is quoted 2.05 mm from the
    body edge and came out 4.2 mm from it, the body centred on the pins):
    1. both outline stops quoted: the centre is half way between them;
    2. ONE stop quoted and the body size along that axis known (X:
       body_length_mm, Y: body_width_mm of the geometry, as the schema
       defines them): the centre is that edge plus or minus half the size,
       kept only when every pad then lies inside the body;
    3. otherwise the body OUTLINE DRAWN around the holes, measured on the
       drawing (drawing_holes.measured_outline through
       data["_outline_measurer"], set by the extraction when drawing images
       exist): the centre is the middle of the drawn outline on that axis;
       a printed quote stays the stronger source (drawings are often not
       exactly to scale: the T7N page draws 22.0 mm for 22.5);
    4. otherwise the centre of the pad field, a DECLARED ESTIMATE named in
       the result's notes.
    The origin becomes the body centre as soon as one axis is placed by a
    quote or by the drawn outline; with none the former convention stays
    (origin «custom», body centred on the pads by
    PackageGeometry.body_frame_offset).
    """
    spans = candidate["spans"]
    pads = candidate["pads"]
    geometry = data["package_geometry"]
    if candidate["centred"]:
        shift, placed, estimated = (spans["x"] / 2, spans["y"] / 2), True, []
    else:
        edges = spans.get("edges") or {}
        sizes = {"x": _optional_number(geometry.get("body_length_mm"), positive=True),
                 "y": _optional_number(geometry.get("body_width_mm"), positive=True)}
        shift, placed, estimated, drawn = [], False, [], []
        outline = None
        measurer = data.get("_outline_measurer")
        if callable(measurer) and None not in sizes.values():
            try:
                outline = measurer(dict(pads), (sizes["x"] * factor, sizes["y"] * factor))
            except Exception:  # noqa: BLE001 - a local measure never loses a paid answer
                outline = None
        for index, axis in enumerate(("x", "y")):
            values = [p[index] for p in pads.values()]
            low, high = edges.get(axis, (None, None))
            size = sizes[axis] * factor if sizes[axis] is not None else None
            centre = None
            if low is not None and high is not None:
                # Both stops quoted on this axis (audit H2): the centre is
                # half way, whatever the other axis has; the span was
                # already compared with the body (_chain_contradictions).
                centre = (low + high) / 2
                if min(values) < low or max(values) > high:
                    centre = None
            elif size is not None and (low is None) != (high is None):
                centre = low + size / 2 if low is not None else high - size / 2
                if min(values) < centre - size / 2 or max(values) > centre + size / 2:
                    centre = None
            if centre is None and outline is not None:
                a, b = ((outline.left_mm, outline.right_mm) if axis == "x"
                        else (outline.top_mm, outline.bottom_mm))
                if a <= min(values) and max(values) <= b:
                    centre = (a + b) / 2
                    drawn.append(axis.upper())
            if centre is None:
                centre = (min(values) + max(values)) / 2
                estimated.append(axis.upper())
            else:
                placed = True
            shift.append(centre)
        if not placed:
            shift = (0.0, 0.0)
    for pin in pins:
        sx, sy = pads[pin["number"]]
        pin["x_mm"] = round((sx - shift[0]) / factor, 4)
        pin["y_mm"] = round((sy - shift[1]) / factor, 4)
    geometry["origin"] = "body_center" if placed else "custom"
    if not candidate["centred"] and drawn:
        data["notes"] = (str(data.get("notes") or "") + "\n" + tx(
            "Corpo posizionato lungo {axes} sul contorno disegnato attorno ai fori, misurato "
            "sul disegno (nessuna quota dal bordo): da verificare.",
            axes=", ".join(drawn))).strip()
    if placed and estimated:
        data["notes"] = (str(data.get("notes") or "") + "\n" + tx(
            "Lungo {axes} nessuna quota dal bordo ne' contorno disegnato misurabile: corpo "
            "centrato sui pad, STIMA da verificare sul disegno.",
            axes=", ".join(estimated))).strip()


#: Disagreement allowed, mm, between a body centre the main answer declared
#: and the one an edge quote of the dedicated reading implies: the edge quote
#: and half the body size are printed quotes, so the rounding of three quotes
#: (_sum_tolerance), not a value tuned to a part.
EDGE_FRAME_QUOTES = 3


def _edge_frame_clash(geometry: dict, main: dict, dedicated: dict, spans: dict,
                      factor: float):
    """Where the dedicated reading's edge quotes contradict the main answer's body-centre frame.

    @param main Main pads in mm, body centre at (0, 0) (origin «body_center»).
    @param dedicated The agreeing dedicated reading, its own frame.
    @return None when no edge is quoted or every quoted axis agrees within
        _sum_tolerance(EDGE_FRAME_QUOTES); otherwise the block message.
    """
    common = sorted(set(main) & set(dedicated))
    if not common:
        return None
    edges = spans.get("edges") or {}
    sizes = {"x": _optional_number(geometry.get("body_length_mm"), positive=True),
             "y": _optional_number(geometry.get("body_width_mm"), positive=True)}
    tolerance = _sum_tolerance(EDGE_FRAME_QUOTES, factor)
    clashes = []
    for index, axis in enumerate(("x", "y")):
        low, high = edges.get(axis, (None, None))
        size = sizes[axis] * factor if sizes[axis] is not None else None
        if low is not None and high is not None:
            quoted = (low + high) / 2
        elif size is not None and (low is None) != (high is None):
            quoted = low + size / 2 if low is not None else high - size / 2
        else:
            continue
        # The main body centre (0 in its frame) seen in the reading's frame.
        declared = sum(dedicated[n][index] - main[n][index] for n in common) / len(common)
        if abs(quoted - declared) > tolerance:
            clashes.append(tx("lungo {axis} la quota dal bordo mette il centro del corpo a "
                              "{offset:.2f} mm da quello dichiarato dalla risposta principale",
                              axis=axis.upper(), offset=abs(quoted - declared)))
    if not clashes:
        return None
    return tx("Le due letture concordano sulle posizioni dei pad, ma non sulla posizione del "
              "corpo: {clashes}. Controllare sul disegno a quale pad si riferisce la quota dal "
              "bordo.", clashes="; ".join(clashes))


def _extend_chain_conflict(data: dict, extra: str, remark: str, progress) -> None:
    """Keep the main answer's own conflict and append what the dedicated reading added."""
    data["_pin_map_conflict"] = data["_pin_map_conflict"] + " " + extra + remark
    data["_numbering_note"] = data["_pin_map_conflict"]
    if progress:
        progress(data["_pin_map_conflict"])


class _HoleVerdicts:
    """Memoised check of pad layouts against the holes drawn in the documents.

    `check` is the `check_layout` of apply_dedicated_pads (None: no check,
    every layout unverified); `body` the body outline (along page X, along
    page Y) in mm, or None. A layout is checked once per distinct set of
    positions. It also carries what a choice by the holes must respect: the
    view each reading declares, whether two declared views differ, and the
    readings already discarded that still state a scale (_settle_by_holes).

    Which of the two plan sizes of the body runs along page X is a printed
    fact only when a quote of the plan view says so (an edge-to-edge chain,
    `body_axes_known`). Otherwise it is the main answer's inference from the
    elevation views, and one reading's inference must not judge another
    reading (user, 2026-11-10: a main answer with the two sizes exchanged
    made the witness refuse the correct dedicated reading as «body_scale»,
    and the generation was blocked). So, with the axes not known and two
    sizes the fit can tell apart (`_axes_distinguishable`), a layout whose
    pads fit the holes (confirmed, or refused only as «body_scale») is
    checked with BOTH orders, and the drawn outline decides by its
    `body_check` (drawing_holes, round two of 2026-11-10: sizes given the
    other way round are «unverifiable», not a refusal):
    - only the exchanged order AGREES: the confirmation is returned as
      `_ExchangedBodyVerdict`, the MEASURE of the body's orientation
      (`_align_body_axes` exchanges the sizes);
    - only the given order agrees: «measured as given»
      (`measures_as_given`);
    - both or neither: nothing is measured, the verdict as given stands.
    Structural, not tuned: a drawn rectangle cannot agree with both orders
    of two distinguishable sizes at the fitted scale. A witness without
    `body_check` (an older one, a test double) is read the old way: its
    confirmation is the agreement and «body_scale» its refusal.
    """

    def __init__(self, check, body, *, views=None, views_differ=False, discarded=(),
                 measure=None, body_axes_known: bool = True, body_bound: bool = False):
        """@brief Keep the check, the body outline and the context; nothing is checked yet.

        @param check The `check_layout` callable, or None.
        @param body Body outline (page X, page Y) in mm, or None.
        @param body_bound True when the body sizes are printed as a maximum
            or a minimum (`body_quote_kind`): the drawn outline may then only
            bound the body, never contradict it -- a layout refused for the
            body alone is checked again on its pads (review G2).
        @param body_axes_known False when no quote of the plan view fixes
            which body size runs along page X (see the class note): the
            witness may then confirm with the two sizes exchanged. True (the
            default) keeps `body` as an ordered pair.
        @param views {reading key: declared view} ("top", "bottom", "unknown").
        @param views_differ True when two readings declare different views.
        @param discarded Pad layouts of readings left out of the choice (e.g.
            chains contradicting the body), still compared by scale.
        @param measure Optional callable(pads, body) ->
            drawing_holes.MeasuredLayout or None: the pad positions measured
            on the drawn holes with the numbers of a reading (_settle_by_measure).
        """
        self.measure = measure
        self.check, self.body, self.cache = check, body, {}
        #: Whether the drawn outline agreed with the sizes as given and not
        #: exchanged (`measures_as_given`), keyed like `cache`; set by `__call__`.
        self.probes = {}
        self.body_axes_known = bool(body_axes_known)
        #: Body sizes printed as bounds («max»/«min»), not nominal (G2).
        self.body_bound = bool(body_bound)
        #: True once the exchanged sizes were written into the answer
        #: (`_align_body_axes`): the exchange is applied once.
        self.axes_adopted = False
        self.views = dict(views or {})
        self.views_differ = views_differ
        self.discarded = list(discarded)
        #: Reference holes (mounting, datum) a reading declared, in the frame
        #: of ITS layout, keyed like `cache`: forwarded to the witness so no
        #: pad is matched to them (`attach`).
        self.references = {}

    @staticmethod
    def _key(pads: dict):
        return tuple(sorted((number, round(x, 4), round(y, 4)) for number, (x, y) in pads.items()))

    def attach(self, pads: dict, references) -> None:
        """@brief Remember the reference holes declared with `pads` (same frame) for its checks.

        @param pads The layout the references belong to.
        @param references {label: (x, y)} in mm, the frame of `pads`;
            entries with a missing coordinate are left out, None/empty
            attaches nothing (generality audit 2026-11-05, M5: the chain
            reading's `ref:` stops were computed and never forwarded).
        """
        points = [(x, y) for x, y in (references or {}).values()
                  if x is not None and y is not None]
        if points:
            self.references[self._key(pads)] = points

    def context(self, pads: dict) -> dict:
        """The keyword context of a witness call on `pads`: its references, when declared."""
        points = self.references.get(self._key(pads))
        return {"references": points} if points else {}

    def bodies(self) -> list:
        """The body outlines a witness may be given: `body`, then the two sizes exchanged when the axes are not known."""
        if self.body is None or self.body_axes_known or self.body[0] == self.body[1]:
            return [self.body]
        return [self.body, (self.body[1], self.body[0])]

    def _axes_distinguishable(self, verdict) -> bool:
        """Whether the outline of this fit can tell the two body sizes apart (generality audit 2026-11-10, D1).

        A nearly square body refused as given by a hair and accepted the
        other way round by a hair is no measure of its orientation: below
        the check's own separation (drawing_holes.body_axes_distinguishable,
        from the tolerance of this very fit) the refusal stands.
        """
        if len(self.bodies()) < 2:
            return False
        from .drawing_holes import body_axes_distinguishable
        return body_axes_distinguishable(self.body, getattr(verdict, "tolerance_mm", None))

    def measures_as_given(self, pads: dict) -> bool:
        """@brief Whether the drawn outline measured the body as given: it agrees that way and not exchanged.

        @param pads The layout that is kept, in the frame the witness checked.
        @return True when the confirmation is a MEASURE of which plan size
            runs along page X, exactly as an exchange is: the axes are not
            fixed by the plan, the two sizes can be told apart by this fit
            (`_axes_distinguishable`), the confirmation comes from the drawn
            holes (a library footprint compares pads, not the body), the
            outline AGREES with the sizes as given (`_body_agrees`) and does
            not agree with them exchanged. A page whose outline was not found,
            or that agrees with neither order, measured nothing.
        @note The other order is checked when the layout is (`__call__`),
            once and memoised; nothing is deferred.
        """
        verdict = self(pads)
        if (not _holes_confirm(verdict) or getattr(verdict, "body_exchanged", False)
                or getattr(verdict, "source", None) == "library"
                or isinstance(verdict, _WitnessConflict)):
            return False
        return bool(self.probes.get(self._key(pads)))

    def __call__(self, pads: dict):
        key = self._key(pads)
        if key not in self.cache:
            verdict = None
            if self.check is not None:
                context = self.context(pads)
                try:
                    verdict = _call_witness(self.check, dict(pads), self.body, context)
                except Exception as exc:  # noqa: BLE001 - a local check never loses a paid answer
                    # A local check that cannot run (OpenCV included) confirms
                    # nothing; it never stops the analysis.
                    verdict = _HoleFailure(type(exc).__name__)
                if ((_holes_confirm(verdict) or getattr(verdict, "reason", None) == "body_scale")
                        and self._axes_distinguishable(verdict)):
                    # The pads fit the holes undistorted (confirmed, or
                    # refused for the body scale alone): the order of the
                    # two plan sizes, when the plan does not fix it, is
                    # decided by which order the drawn outline AGREES with.
                    # The probe has its own guard (review E7): when it
                    # cannot run, the verdict as given stands, measuring
                    # nothing.
                    try:
                        exchanged = _call_witness(self.check, dict(pads), self.bodies()[1], context)
                    except Exception:  # noqa: BLE001 - a failed probe keeps the first verdict
                        exchanged = None
                    if exchanged is not None:
                        given, other = _body_agrees(verdict), _body_agrees(exchanged)
                        if other and not given:
                            verdict = _ExchangedBodyVerdict(exchanged)
                        self.probes[key] = given and not other
                if self.body_bound and _body_contradicted(verdict):
                    # Body quotes printed as «max»/«min» only BOUND the body:
                    # a drawn outline of other size contradicts nothing
                    # (review G2). The layout is judged on its pads alone.
                    try:
                        verdict = _call_witness(self.check, dict(pads), None, context)
                    except Exception as exc:  # noqa: BLE001 - a local check never loses a paid answer
                        verdict = _HoleFailure(type(exc).__name__)
            self.cache[key] = verdict
        return self.cache[key]

    def forget(self) -> None:
        """@brief Drop every memoised verdict and probe: the body they were checked with changed (review E9)."""
        self.cache.clear()
        self.probes.clear()


def _body_contradicted(verdict) -> bool:
    """Whether a witness verdict refuses a layout for the body alone: outline «contradicted», or refusal «body_scale»."""
    if getattr(verdict, "body_check", None) == "contradicted":
        return True
    return not _holes_confirm(verdict) and getattr(verdict, "reason", None) == "body_scale"


def _body_agrees(verdict) -> bool:
    """Whether a witness verdict ties the reading to the declared body: confirmed with the outline agreeing.

    drawing_holes (2026-11-10) says it in `body_check`: only "agrees" counts
    -- "unverifiable" (no rectangle identified as the body, e.g. the sizes
    given the other way round) and "contradicted" do not. A verdict without
    `body_check` (an older witness, a test double) confirmed only when the
    body fitted, so its confirmation is the agreement.
    """
    if not _holes_confirm(verdict):
        return False
    check = getattr(verdict, "body_check", None)
    return check == "agrees" if isinstance(check, str) else True


class _ExchangedBodyVerdict:
    """A witness confirmation obtained with the two plan sizes of the body exchanged.

    Every attribute is the confirming verdict's; `body_exchanged` marks that
    the drawn outline measured the body's orientation on the page the other
    way round than the answer declares it (`_HoleVerdicts`, `_align_body_axes`).
    """

    body_exchanged = True

    def __init__(self, verdict):
        """@brief Wrap the confirming verdict.

        @param verdict The verdict of the check run with the exchanged sizes.
        """
        self._verdict = verdict

    def __getattr__(self, name):
        """@brief Delegate every other attribute to the wrapped verdict.

        @param name Attribute name.
        @return The wrapped verdict's attribute.
        @throws AttributeError When the wrapped verdict lacks it, and for
            private or special names: `copy` and `pickle` look those up on an
            instance whose `__init__` has not run, where delegating would
            recurse for ever.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._verdict, name)


def _plan_fixes_body_axes(*readings) -> bool:
    """Whether a chain reading closes on the body outline on some axis of the plan view.

    Such an edge-to-edge span is a printed quote of the plan: compared with
    the body sizes (`_chain_contradictions`), it fixes which size runs along
    that axis. Without one, the assignment is the main answer's inference.
    """
    return any((reading.get("spans") or {}).get(axis) is not None
               for reading in readings if isinstance(reading, dict) for axis in ("x", "y"))


def _align_body_axes(data: dict, holes, pads: dict, progress=None) -> None:
    """Write the body sizes the way the drawn outline measured them for the layout about to be kept.

    When the witness confirmed `pads` only with the two plan sizes exchanged
    (`_ExchangedBodyVerdict`), `body_length_mm` (along X) and `body_width_mm`
    (along Y) of the answer and of its `package_geometry` are exchanged, with
    their provenance, BEFORE the pads are written: the body is then placed
    from the edge quotes with the size that really lies on each axis
    (`_write_chain_pads`). Applied once per confrontation; the note says the
    orientation is measured on the drawing, not printed.

    A confirmation whose drawn outline AGREES with the sizes AS GIVEN and
    not with them exchanged (`_HoleVerdicts.measures_as_given`) measures the
    orientation just as much (review 2026-11-10, A6: only an exchange used
    to count). Nothing is exchanged; `data["_body_axes_measured"]` is set
    for `apply_dedicated_body`, exactly as after an exchange. The witness
    answered both orders when the layout was checked (round two,
    2026-11-10: `body_check` says it directly), so no callable is left in
    the answer, which stays JSON-able.

    @param holes The `_HoleVerdicts` of the confrontation (anything else: no-op).
    @param pads The layout that is kept, in the frame the witness checked.
    """
    if getattr(holes, "axes_adopted", True):
        return
    if not getattr(holes(pads), "body_exchanged", False):
        if holes.measures_as_given(pads):
            holes.body_axes_known = holes.axes_adopted = True
            data["_body_axes_measured"] = True
        return
    evidence = _hole_text(holes(pads))
    holes.body = (holes.body[1], holes.body[0])
    holes.body_axes_known = holes.axes_adopted = True
    # Verdicts memoised with the old order describe a body that is no longer
    # the answer's: later notes and checks ask the witness again (E9).
    holes.forget()
    _exchange_body_sizes(data, holes.body, evidence, progress)


def _exchange_body_sizes(data: dict, measured, evidence: str, progress=None) -> None:
    """Exchange `body_length_mm` and `body_width_mm` of an answer, with their provenance and the note.

    @param data Answer in drawing units: the sizes are exchanged in the
        answer, in its `package_geometry` and in `value_sources` (a value
        keeps its own provenance on the axis it moves to).
    @param measured (along X, along Y) in mm AFTER the exchange, for the note.
    @param evidence What the witness said, for the note.
    @note `_body_axes_measured` is left for `apply_dedicated_body`, which
        runs afterwards: the dedicated body reading states its two plan
        sizes without an axis the plan fixes either, so it is compared in
        the measured order and never undoes the exchange (generality audit
        2026-11-10, D2).
    """
    containers = [c for c in (data.get("package_geometry"), data, data.get("value_sources"))
                  if isinstance(c, dict)]
    for container in containers:
        length, width = container.pop("body_length_mm", None), container.pop("body_width_mm", None)
        if width is not None:
            container["body_length_mm"] = width
        if length is not None:
            container["body_width_mm"] = length
    data["_body_axes_measured"] = True
    note = tx("Lunghezza e larghezza del corpo scambiate rispetto alla risposta principale: il "
              "contorno disegnato attorno ai fori misura {x:.2f} mm lungo X e {y:.2f} mm lungo Y "
              "({holes}). Nessuna quota in pianta fissa l'orientamento del corpo: da verificare "
              "sul disegno.", x=measured[0], y=measured[1], holes=evidence)
    data["notes"] = (str(data.get("notes") or "") + "\n" + note).strip()
    if progress:
        progress(note)


def _call_witness(witness, pads: dict, body, context: dict):
    """Call a check/measure callable with its optional context only when its signature takes it.

    The witnesses of the extractor (`_layout_checker`, `_layout_measurer`)
    accept `references=`; a test double or an older callable taking
    (pads, body) alone is called without it, so an attached reference never
    turns into a TypeError that loses a paid answer.
    """
    if context:
        try:
            import inspect
            parameters = inspect.signature(witness).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepted = {name: value for name, value in context.items()
                    if name in parameters
                    or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())}
        if accepted:
            return witness(pads, body, **accepted)
    return witness(pads, body)


class _HoleFailure:
    """A hole check that could not run: unverifiable, with the exception type as reason."""

    status, image, max_error_mm, tolerance_mm, circles = "unverifiable", None, None, None, 0

    def __init__(self, reason):
        """@brief Record why the check could not run.

        @param reason The exception type name.
        """
        self.reason = reason


def _holes_confirm(verdict) -> bool:
    """True only for a hole check that positively confirmed the layout."""
    return getattr(verdict, "status", None) == "confirmed"


def _hole_text(verdict) -> str:
    """One clause saying what the drawn holes said about one layout."""
    if verdict is None:
        return tx("controllo dei fori disegnati non eseguito")
    if isinstance(verdict, _WitnessConflict):
        return tx("i fori disegnati e il footprint KiCad della sigla si contraddicono ({holes}; "
                  "{library})", holes=_hole_text(verdict.holes),
                  library=_hole_text(verdict.library))
    if getattr(verdict, "source", None) == "library":
        if verdict.status == "confirmed":
            return tx("il footprint KiCad {name} ha la stessa disposizione (scarto massimo {error:.2f} "
                      "mm, tolleranza {tolerance:.2f} mm)", name=verdict.image,
                      error=verdict.max_error_mm or 0.0, tolerance=verdict.tolerance_mm or 0.0)
        return tx("i footprint KiCad della sigla ({names}) non hanno questa disposizione",
                  names=verdict.image or "?")
    image = verdict.image or "?"
    reason = verdict.reason
    if verdict.status == "confirmed":
        text = tx("i fori disegnati in {image} coincidono (scarto massimo stimato {error:.2f} "
                  "mm, tolleranza {tolerance:.2f} mm)", image=image,
                  error=verdict.max_error_mm or 0.0, tolerance=verdict.tolerance_mm or 0.0)
        if getattr(verdict, "body_exchanged", False):
            # Said wherever the verdict is quoted (a question, a block): the
            # confirmation holds with the body the other way round (D3).
            text += " " + tx("con lunghezza e larghezza del corpo scambiate")
        # What the confirmation covers (drawing_holes, 2026-11-10): the
        # proportions always, the absolute size only when the drawn body
        # outline agreed with the declared body. A witness that does not say
        # (an older one, a library footprint) adds nothing.
        verified = getattr(verdict, "scale_verified", None)
        if verified is True:
            text += "; " + tx("scala verificata dal contorno del corpo disegnato")
        elif verified is False:
            text += "; " + tx("conferma della sola forma: la scala non e' verificata dal contorno "
                              "del corpo")
        return text
    if reason == "distorted":
        return tx("i fori disegnati in {image} coincidono solo deformando il disegno", image=image)
    if reason == "body_scale":
        return tx("i fori disegnati in {image} coincidono solo con una scala diversa da quella "
                  "del contorno del corpo", image=image)
    if verdict.status == "not_confirmed":
        return tx("i fori disegnati in {image} non coincidono con queste posizioni", image=image)
    if reason == "not_a_drawing":
        return tx("nessuna immagine e' un disegno a linee: i fori non si possono controllare")
    if reason == "no_round_holes":
        return tx("nel disegno non c'e' nessun foro circolare da controllare (piazzole o asole "
                  "soltanto)")
    if reason == "no_match_unobservable":
        return tx("i fori disegnati in {image} non coincidono, ma con questi pad il controllo non "
                  "distingue una lettura sbagliata da una pagina non in scala", image=image)
    if reason == "too_few_circles":
        return tx("nel disegno non si trovano abbastanza fori circolari da controllare")
    # Two "cannot tell" reasons of drawing_holes (2026-11-10): never a
    # contradiction of the reading, each with what would settle it.
    if reason == "page_skewed":
        return tx("i fori disegnati in {image} coincidono solo con la pagina ruotata oltre il "
                  "limite del controllo: raddrizzare la pagina e ripetere", image=image)
    if reason == "unanchored":
        return tx("i fori disegnati in {image} coincidono con cerchi non tutti disegnati come fori: "
                  "puo' essere una coincidenza, non una conferma", image=image)
    if reason == "too_many_pads":
        return tx("troppi pad per il controllo dei fori disegnati")
    if reason == "too_few_pads":
        return tx("meno di due pad: niente da controllare sui fori disegnati")
    if reason == "no_image":
        return tx("nessuna immagine leggibile per il controllo dei fori")
    return tx("controllo dei fori disegnati non riuscito ({reason})", reason=reason)


def _pad_block_remedy(pins) -> str:
    """What the user can do when the pad positions are blocked.

    Only through-hole pads have a manual editor («Piazzole del componente
    THT», shown for single THT parts); for any other pad the way out is a
    better input for the readings.
    """
    if pins and all(isinstance(pin, dict) and pin.get("pad_type", "thru_hole") == "thru_hole"
                    for pin in pins):
        return tx("Inserisci le posizioni a mano in «Piazzole del componente THT» e usa «Rigenera "
                  "footprint», oppure ripeti l'analisi con un modello piu' capace per la lettura "
                  "delle posizioni dei pad.")
    return tx("Aggiungi il disegno quotato delle piazzole come immagine con ruolo pinout o vista "
              "dall'alto, oppure ripeti l'analisi con un modello piu' capace per la lettura delle "
              "posizioni dei pad.")


#: Key of a pad-layout candidate that no reading gave as it stands: the
#: layout the drawn holes confirm where they contradict two agreeing readings
#: (`_refuse_contradicted_agreement`). It appears in the option value and in
#: the payload's `source` of a "pad_layout" question, like a reading's key.
HOLES_SOURCE = "holes"


def _reading_source(key: str) -> str:
    """The name of one reading in the messages."""
    return {"main": tx("coordinate della risposta principale"),
            "chains": tx("catene di quote della risposta principale"),
            "dedicated": tx("lettura dedicata"),
            # Not a reading: a layout the drawn holes confirm, offered beside
            # the readings they contradict (`_hole_backed_layouts`).
            HOLES_SOURCE: tx("disposizione confermata dai fori disegnati")}[key]


def _hole_verdicts(readings, holes) -> str:
    """'<reading>: <what the holes said>; ...' for the block messages."""
    return "; ".join(f"{_reading_source(key)}: {_hole_text(holes(pads))}"
                     for key, pads, _spans, _centred in readings)


#: Readings the program sums from transcribed dimension chains (printed
#: quotes): the dedicated call's and the main answer's own chains.
_CHAIN_READINGS = ("dedicated", "chains")


def _layout_observable(pads: dict) -> bool:
    """Whether a hole check of this layout can tell a wrong reading from a page not to scale.

    The check calls a reading «distorted» (a stretch or shear of the page
    explains it) only when an affine fit is over-determined: enough pads,
    not collinear (drawing_holes._affine_observable, the same rule the
    check applies). With three pads, or pads on one line, a page not to
    scale fails as «no_match» exactly like a wrong reading (review
    2026-11-04, M18/H5), so such a «no_match» is NOT a losing verdict. A
    verdict that carries its own `scale_observable` flag (drawing_holes,
    when it exposes it) is preferred by `_losing_verdict`; when neither the
    flag nor the local rule can be evaluated the layout is held not
    observable (nothing is lost but a choice the witness could not justify).
    """
    try:
        import numpy as np
        from .drawing_holes import _affine_observable
        return bool(_affine_observable(np.array(list(pads.values()), float)))
    except Exception:  # noqa: BLE001 - a local check never loses a paid answer
        return False


def _losing_verdict(verdict, pads: dict, losing) -> bool:
    """True when `verdict` refutes `pads` for a reason in `losing` AND the refutation is observable.

    A "no_match" counts only when the check could have said «distorted»
    instead (`_layout_observable`, or the verdict's own `scale_observable`
    when drawing_holes exposes it); "distorted" is a judgement the check
    already made on an observable layout.
    """
    if getattr(verdict, "status", None) != "not_confirmed":
        return False
    reason = getattr(verdict, "reason", None)
    if reason not in losing:
        return False
    if reason != "no_match":
        return True
    observable = getattr(verdict, "scale_observable", None)
    if observable is None:
        observable = _layout_observable(pads)
    return bool(observable)


def _mirror_layouts(pads: dict) -> list:
    """The mirror images of a layout: drawing_holes.mirror_variants (X flip, Y flip), or the X flip alone.

    One definition shared with the measure (review 2026-11-04, «mirror
    tested only in X»); the hand-written X flip is the fallback when the
    witness module is unavailable.
    """
    try:
        from .drawing_holes import mirror_variants
        variants = mirror_variants(pads)
        if variants:
            return [{str(number): tuple(xy) for number, xy in variant.items()} for variant in variants]
    except Exception:  # noqa: BLE001 - a local helper never loses a paid answer
        pass
    return [{number: (-x, y) for number, (x, y) in pads.items()}]


def _pick_by_holes(readings, holes):
    """The one reading the drawn holes confirm while they confirm none of the others.

    @param readings [(key, pads)] of complete readings (every pin placed).
    @param holes _HoleVerdicts.
    @return (key, pads, verdict) of that reading, or None: none confirmed, or
        more than one (they differ only where the holes cannot see, e.g. two
        pad numbers exchanged on the same positions), so nothing is chosen.
    @note The holes check GEOMETRY, never the numbering: the chosen reading's
        pad numbers are its own and the note says they are unverified.
    @note Two more conditions (adversarial review of drawing_holes,
        2026-10-31): every other reading must fail with "no_match" -- a pad
        landing where no hole is -- and that refutation must be OBSERVABLE
        (`_losing_verdict`: with three pads or a collinear layout a page not
        to scale fails the same way, review 2026-11-04 M18). A reading that
        fails only as "distorted" or "body_scale" may be the right one on a
        drawing not to scale, and an unverifiable one was not checked at
        all: neither lets another reading win. And the chosen layout's
        MIRROR, when it is a different point set, must not fit the same
        holes -- a page printing both a top and a bottom view fits a
        mirrored reading too -- UNLESS the chosen reading DECLARES the side
        it read and no other reading declares a different one (review
        2026-11-04, M9: a plan beside its land pattern always fits the
        mirror, and a declared view is a fact the witness cannot see); a
        chosen reading whose view is unknown is still chosen for its
        geometry, the side is then asked (`_require_declared_view`).
    @note Exception for "distorted" (user, 2026-11-03, T7N relay: the
        chain reading of 5 pads fitted the drawn holes within 0.11 mm while
        the main answer's 4 pads fitted «only by distorting the drawing», and
        nothing was chosen). A CHAIN reading ("dedicated" or "chains") is the
        program's sum of the PRINTED quotes: when it fits the drawn circles
        without distortion, the page is to scale with its own quotes, so the
        «not to scale» excuse no longer holds for a competitor that fits only
        by distorting that same page -- it is wrong. Structural, not tuned:
        a page NOT to scale makes the chain reading itself «distorted», never
        confirmed, and the rule does not fire. "body_scale" and unverifiable
        competitors still refuse the choice. "distorted" also names a fit
        that needs a rotation beyond the check's limit (the same point set
        with the axes swapped): relative to the page as printed, that
        competitor is wrong too. The main answer's own "chains" come from
        the same paid answer as its coordinates: when they fit undistorted
        and the coordinates do not, the sums of the printed quotes win over
        the model's arithmetic, which is the point of transcribing chains.
    """
    confirmed = [(key, pads, holes(pads)) for key, pads in readings
                 if _holes_confirm(holes(pads))]
    if len(confirmed) != 1:
        return None
    key, pads, _verdict = confirmed[0]
    page_to_scale = (key in _CHAIN_READINGS
                     and getattr(confirmed[0][2], "source", None) != "library")
    losing = ("no_match", "distorted") if page_to_scale else ("no_match",)
    for other_key, other in readings:
        if other_key != key and not _losing_verdict(holes(other), other, losing):
            return None
    mirrors = _mirror_layouts(pads)
    mirrored = mirrors[0]
    if getattr(confirmed[0][2], "source", None) == "library":
        # The library compares up to quarter turns, so the side question is
        # asked in the same group (generality audit C3, 2026-10-31): a
        # layout equal to its mirror image under a quarter turn can never
        # tell the side, whatever its orientation on the page.
        if _same_up_to_quarter_turns(list(pads.values()), list(mirrored.values())):
            return None
    if any(not _same_point_set(pads, mirror) and _holes_confirm(holes(mirror))
           for mirror in mirrors):
        view = holes.views.get(key)
        others = {v for k, v in holes.views.items() if k != key and v in ("top", "bottom")}
        if view in ("top", "bottom") and others - {view}:
            # Another reading declares the other side: the page shows both
            # and the readings disagree on which one they read.
            return None
        # The chosen reading may not say its side while another does: the
        # geometry is chosen here, the side is the other reading's claim
        # only, which the witness cannot check -- _settle_by_holes withdraws
        # such a view (_withdraw_view) and the side is asked, never adopted.
    return confirmed[0]


def _same_up_to_quarter_turns(first: list, second: list) -> bool:
    """True when two point lists are one set up to a translation and a quarter turn.

    Centroids aligned, each point paired with the nearest free one, within
    PAD_READING_TOLERANCE_MM: the transformation group of the library witness
    (library_footprints.check_layout).
    """
    if len(first) != len(second) or not first:
        return False

    def centred(points):
        cx = sum(x for x, _y in points) / len(points)
        cy = sum(y for _x, y in points) / len(points)
        return [(x - cx, y - cy) for x, y in points]

    target = centred(second)
    turn = centred(first)
    for _ in range(4):
        free = list(target)
        fits = True
        for x, y in turn:
            index = min(range(len(free)), key=lambda i: math.hypot(free[i][0] - x, free[i][1] - y))
            if math.hypot(free[index][0] - x, free[index][1] - y) > PAD_READING_TOLERANCE_MM:
                fits = False
                break
            free.pop(index)
        if fits:
            return True
        turn = [(-y, x) for x, y in turn]
    return False


def _same_point_set(first: dict, second: dict) -> bool:
    """True when two layouts are the same set of positions up to a translation (numbers ignored)."""
    def shape(pads):
        x0 = min(x for x, _y in pads.values())
        y0 = min(y for _x, y in pads.values())
        return sorted((x - x0, y - y0) for x, y in pads.values())

    if len(first) != len(second) or not first:
        return False
    return all(math.hypot(a[0] - b[0], a[1] - b[1]) <= PAD_READING_TOLERANCE_MM
               for a, b in zip(shape(first), shape(second)))


def _settle(data: dict, note: str, progress) -> None:
    """Lift the block: the pad positions are decided, `note` says how."""
    data.pop("_chain_candidate", None)
    data.pop("_pin_map_conflict", None)
    data["_numbering_note"] = note
    if progress:
        progress(note)


def _block(data: dict, message: str, progress) -> None:
    """Block the automatic generation of the pad positions with `message`: the hard block.

    Kept ONLY when no structured question can close the case
    (`_block_or_ask`): the text says so in one sentence and reports the
    fact; the remedy sentence belongs to the GUI (`_pad_block_remedy` is
    its helper), never repeated here (review 2026-11-04, B3).
    """
    data["_pin_map_conflict"] = (message + " " + tx(
        "Nessuna domanda puo' chiudere il caso: non c'e' una seconda disposizione completa "
        "fra cui scegliere."))
    data["_numbering_note"] = data["_pin_map_conflict"]
    if progress:
        progress(data["_pin_map_conflict"])


def _layout_digest(pads: dict) -> str:
    """A short, stable digest of a layout's signed offsets (numbers and positions to 0.01 mm)."""
    import hashlib
    numbers = sorted(pads)
    if not numbers:
        return "0"
    ox, oy = pads[numbers[0]]
    text = ";".join(f"{n}:{pads[n][0] - ox:.2f},{pads[n][1] - oy:.2f}" for n in numbers)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _candidate_payload(data: dict, keys, pads: dict, spans: dict, centred: bool, view,
                       factor: float, exchange: Optional[dict] = None,
                       main_frame: Optional[bool] = None) -> dict:
    """What a chosen pad-layout candidate writes: the placement the automatic paths would write.

    One writer for every path (review 2026-11-10, A4: a layout chosen by the
    user landed elsewhere than the same layout chosen by the holes, by an
    agreeing reading or by a saved answer). The placement is computed HERE,
    while the body sizes, the edge quotes and the outline measurer of the
    extraction are at hand, by the writers the automatic paths use, and the
    payload carries the result; `_write_candidate` (a saved answer, during
    the extraction) and `apply_answers` (a fresh answer, afterwards) only
    copy it:
    - the main answer's coordinates (alone, or with a twin chain reading
      when the answer declares its body-centre frame, which an agreeing
      reading keeps too): the pins and the declared `origin` stay as they
      are, as `_settle_by_holes` leaves a main reading it chose;
    - a chain reading: `_write_chain_pads` on a scratch copy of the
      geometry, so the body is placed axis by axis from its edge quotes,
      the drawn outline or, declared, the pad field -- with the two body
      sizes exchanged first when the witness confirmed the layout only that
      way (`exchange`), as `_align_body_axes` does before the same writer.

    @param keys Reading keys that describe this one layout (twins).
    @param pads The layout, mm, in its reading's own frame.
    @param spans,centred The reading's outline spans (`_write_chain_pads`).
    @param view The view a reading of `keys` declares, or None.
    @param exchange The "exchange_body" entry of the candidate, or None.
    @param main_frame None (default) applies the rule above to `keys`; True
        says `pads` are already in the main answer's frame and are copied
        with its declared origin, False that they are a chain layout to be
        placed by `_write_chain_pads` -- for a layout that is no reading's
        (`HOLES_SOURCE`) and is given in the frame of the reading it varies.
    @return {"source", "pins" (mm, the frame of `origin`), "origin", "view",
        "notes" (the lines the writer added: estimated axes, drawn outline)}
        plus "exchange_body" when given; JSON-able.
    """
    geometry = data["package_geometry"]
    chains = [key for key in keys if key != "main"]
    keeps_main = ("main" in keys and (not chains or geometry.get("origin") == "body_center")
                  if main_frame is None else bool(main_frame))
    notes = []
    if keeps_main:
        source, placed, origin = "main", dict(pads), geometry.get("origin") or "custom"
    else:
        source = chains[0] if chains else keys[0]
        # A scratch answer: only what the writer reads (body sizes, measurer).
        scratch_geometry = {key: value for key, value in geometry.items() if key != "pins"}
        if exchange:
            length = scratch_geometry.pop("body_length_mm", None)
            width = scratch_geometry.pop("body_width_mm", None)
            if width is not None:
                scratch_geometry["body_length_mm"] = width
            if length is not None:
                scratch_geometry["body_width_mm"] = length
        scratch = {"package_geometry": scratch_geometry, "notes": "",
                   "_outline_measurer": data.get("_outline_measurer")}
        scratch_pins = [{"number": number} for number in pads]
        _write_chain_pads(scratch, scratch_pins, {"pads": pads, "spans": spans or {},
                                                  "centred": bool(centred)}, factor)
        placed = {pin["number"]: (pin["x_mm"] * factor, pin["y_mm"] * factor)
                  for pin in scratch_pins}
        origin = scratch_geometry["origin"]
        notes = [line for line in scratch["notes"].splitlines() if line.strip()]
    payload = {"source": source,
               "pins": [{"number": number, "x_mm": round(x, 4), "y_mm": round(y, 4)}
                        for number, (x, y) in sorted(placed.items(), key=lambda item: item[0])],
               "origin": origin, "view": view if view in ("top", "bottom") else None,
               "notes": notes}
    if exchange:
        payload["exchange_body"] = exchange
    return payload


def _write_candidate(data: dict, pins: list, payload: dict, factor: float) -> None:
    """Write a chosen pad-layout candidate into the answer's pins (drawing units).

    The placement is the payload's (`_candidate_payload`): pins, origin and
    the writer's notes are copied, the body sizes exchanged when the
    candidate says so and nothing exchanged them yet.
    """
    geometry = data["package_geometry"]
    exchange = payload.get("exchange_body")
    if isinstance(exchange, dict) and not data.get("_body_axes_measured"):
        _exchange_body_sizes(data, exchange["measured_mm"], exchange["evidence"])
    positions = {p["number"]: (p["x_mm"], p["y_mm"]) for p in payload["pins"]}
    # A coordinate estimated from the drawing's pixels keeps its provenance
    # (MEASURED_PIXELS_SOURCE), as when `_settle_by_measure` writes it.
    measured = {p["number"]: p["source"] for p in payload["pins"] if p.get("source")}
    for pin in pins:
        if pin.get("number") in positions:
            x, y = positions[pin["number"]]
            pin["x_mm"], pin["y_mm"] = round(x / factor, 4), round(y / factor, 4)
            if pin["number"] in measured:
                pin["source"] = measured[pin["number"]]
    geometry["origin"] = payload.get("origin") or "custom"
    geometry["needs_review"] = True
    for line in payload.get("notes") or ():
        data["notes"] = (str(data.get("notes") or "") + "\n" + str(line)).strip()
    if payload.get("view") in ("top", "bottom"):
        _adopt_view(data, payload["view"], tx("scelta dell'utente"))


def _block_or_ask(data: dict, pins: list, readings: list, holes, factor: float, message: str,
                  progress, remark: str = "", extras=(), contradicted: bool = False) -> None:
    """Ask which complete layout is right when there are at least two, else block.

    `readings` are the (key, pads, spans, centred) tuples at hand; only the
    layouts with exactly the answer's pad numbers can be offered, never one
    the drawn holes refute on an observable page (`_losing_verdict`), and two
    layouts with the same signed offsets are one option. With two or more
    distinct options the structured question "pad_layout" is recorded
    (ExtractedParams.questions) with its candidates, `_pin_map_conflict`
    is lifted and the main pads stay with needs_review until the answer; a
    saved answer (data["_answers"]) is applied at once. With fewer, the
    hard block stands (`_block`): no single fact of the user's can close it.

    `extras` are layouts the drawn holes CONFIRM and no reading gave
    (`_hole_backed_layouts`), each one more option under HOLES_SOURCE with
    its own label. With one of them on offer the readings' layout is offered
    BESIDE it even when the holes refute it, its option saying what the
    holes said: the choice is then between what two readings agree on and
    what the drawing shows, never between wrong answers only (the reason of
    audit A1 below), and the user arbitrates instead of the program adopting
    either (user, 2026-11-11).

    `contradicted` (round 7, `_refuse_contradicted_agreement`): the readings
    AGREE and the drawn holes do not confirm their layout. The witness is
    imperfect, so its refusal neither excludes that layout nor blocks: the
    layout is always offered, with what the holes said about it, and ONE
    option is enough for the question (the user confirms the agreed layout
    after checking the drawing, or goes back). Only pads sharing a number
    still block, since no chosen layout could be written back.
    """
    listed = [_pad_number_text(pin.get("number")) for pin in pins if isinstance(pin, dict)]
    numbers = set(listed)
    tolerance = _reading_tolerance(factor)
    distinct = []
    # Pads sharing a number collapse into one entry of every layout: a
    # chosen candidate could not be written back pad by pad.
    candidates_allowed = len(listed) == len(numbers)
    extras = [extra for extra in extras
              if candidates_allowed and set(extra["pads"]) == numbers and extra["pads"]]
    refuted = []
    for key, pads, spans, centred in readings:
        if not candidates_allowed or set(pads) != numbers or not pads:
            continue
        # A layout the drawn holes REFUTE on an observable page is not a
        # candidate: the witness already said it is wrong, so a question
        # offering it would replace the block with a choice between wrong
        # answers, remembered in the project (generality audit 2026-11-05,
        # A1). An unverifiable or «distorted» verdict refutes nothing.
        if not extras and not contradicted and _losing_verdict(holes(pads), pads, ("no_match",)):
            refuted.append(key)
            continue
        twin = next((entry for entry in distinct
                     if not _offset_disagreements(entry[1], pads, sorted(numbers), tolerance)),
                    None)
        if twin is not None:
            twin[0].append(key)
            if (twin[0][0] == "main" and len(twin[0]) == 2
                    and data["package_geometry"].get("origin") != "body_center"):
                # The main coordinates and a chain reading are one layout:
                # without a body-centre frame declared by the answer,
                # `_candidate_payload` places the CHAIN reading, so the
                # option carries that reading's sums and edge quotes -- the
                # layout an agreement writes (`apply_dedicated_pads`), not
                # the main coordinates with no edge quote. With a declared
                # frame the main pads stay, in their own frame.
                twin[1:] = [pads, spans or {}, bool(centred)]
            continue
        distinct.append([[key], pads, spans or {}, bool(centred)])
    # {index in `distinct`: the hole-backed layout it is}, for the labels and
    # the payloads below; a hole-backed layout equal to a reading's is that
    # reading's option (it cannot happen for a contradicted agreement).
    backed = {}
    for extra in extras:
        if any(not _offset_disagreements(entry[1], extra["pads"], sorted(numbers), tolerance)
               for entry in distinct):
            continue
        backed[len(distinct)] = extra
        distinct.append([[HOLES_SOURCE], extra["pads"], extra["spans"], extra["centred"]])
    if len(distinct) < (1 if contradicted else 2):
        if refuted:
            remark = remark + " " + tx("Disposizioni escluse dalla domanda perche' smentite dai fori "
                                       "disegnati: {sources}.",
                                       sources=", ".join(_reading_source(k) for k in refuted))
        _block(data, message + remark, progress)
        return
    options, candidates = [], {}
    for index, (keys, pads, spans, centred) in enumerate(distinct):
        value = "+".join(keys) + ":" + _layout_digest(pads)
        view = next((holes.views.get(key) for key in keys if holes.views.get(key) in ("top", "bottom")),
                    None)
        extra = backed.get(index)
        if extra is not None:
            # A layout the holes confirm: its option says where it comes
            # from and which pad differs from the readings.
            label = tx("{sources} -- {evidence} -- {pads}", sources=_reading_source(HOLES_SOURCE),
                       evidence=extra["label"], pads=_pad_summary(pads))
        elif backed or contradicted:
            # Beside a hole-backed layout, or when the holes did not confirm
            # an agreement, every reading's option carries what the holes
            # said about it.
            label = tx("{sources} ({holes}): {pads}",
                       sources=", ".join(_reading_source(k) for k in keys),
                       holes=_hole_text(holes(pads)), pads=_pad_summary(pads))
        else:
            label = tx("{sources}: {pads}", sources=", ".join(_reading_source(k) for k in keys),
                       pads=_pad_summary(pads))
        options.append({"label": label, "value": value})
        exchange = None
        if (getattr(holes(pads), "body_exchanged", False)
                and not getattr(holes, "axes_adopted", True)):
            # The witness confirms this layout only with the body the other
            # way round: whoever writes the candidate (here, or apply_answers
            # after the user's choice) exchanges the sizes with it (D3), and
            # the body is placed with the sizes so exchanged.
            exchange = {"measured_mm": [holes.body[1], holes.body[0]],
                        "evidence": _hole_text(holes(pads))}
        if extra is None:
            candidates[value] = _candidate_payload(data, keys, pads, spans, centred, view, factor,
                                                   exchange)
            continue
        # The holes say where, never from which side: no view goes with it.
        payload = _candidate_payload(data, keys, pads, spans, centred, None, factor, exchange,
                                     main_frame=extra["main_frame"])
        payload["source"] = HOLES_SOURCE
        if extra["pixels"]:
            for entry in payload["pins"]:
                if entry["number"] == extra["moved"]:
                    entry["source"] = MEASURED_PIXELS_SOURCE
        candidates[value] = payload
    if len(distinct) == 1:
        # Only with `contradicted`: one layout to confirm, not a choice. The
        # message already opens with «Posizioni dei pad NON confermate»
        # (`_refuse_contradicted_agreement`): no second «Posizioni dei pad:»
        # in front of it (round 7b, F4).
        text = tx("{message} Confermare la disposizione letta dopo averla controllata sul "
                  "disegno?", message=message)
    elif contradicted:
        # The same message, the same reason not to repeat its opening words.
        text = tx("{message} Quale disposizione corrisponde al disegno?", message=message)
    else:
        text = tx("Posizioni dei pad: {message} Quale disposizione corrisponde al disegno?",
                  message=message)
    chosen = _ask(data, "pad_layout", text, options, _question_evidence(data), candidates)
    if chosen is not None:
        _write_candidate(data, pins, candidates[chosen], factor)
        if "exchange_body" not in candidates[chosen]:
            # A saved answer keeps this layout during the extraction: a body
            # reading follows, and a confirmation as given may be a measure
            # of the body's orientation, as on every other path (A6).
            kept = next(pads for keys, pads, _spans, _centred in distinct
                        if "+".join(keys) + ":" + _layout_digest(pads) == chosen)
            _align_body_axes(data, holes, kept)
        _settle(data, tx("Posizioni dei pad scelte dall'utente fra le letture ({source}); da "
                         "verificare sul disegno.",
                         source=_reading_source(candidates[chosen]["source"])) + remark, progress)
        return
    data.pop("_pin_map_conflict", None)
    data["package_geometry"]["needs_review"] = True
    if len(distinct) == 1:
        asked = tx("La conferma della disposizione letta e' chiesta all'utente; fino alla "
                   "risposta restano le coordinate della risposta principale, da verificare.")
    else:
        asked = tx("La scelta fra le {count} disposizioni lette e' chiesta all'utente; fino alla "
                   "risposta restano le coordinate della risposta principale, da verificare.",
                   count=len(distinct))
    data["_numbering_note"] = message + remark + " " + asked
    if progress:
        progress(data["_numbering_note"])


def _settle_by_holes(data: dict, pins: list, readings: list, holes, factor: float,
                     progress, why: str, remark: str):
    """Decide between disagreeing or unconfirmed readings by the drawn holes alone.

    @param readings [(key, pads, spans, centred)]: key "main" (the model's
        coordinates, already in the pins), "chains" (the main answer's own
        chains) or "dedicated"; `spans` and `centred` say whether the
        reading may be written with the body centre as origin
        (_write_chain_pads). Only readings placing every pin count.
    @param why What the readings did not settle, for the note.
    @return The key of the reading chosen and written; "blocked" when the
        chosen reading disagrees in SCALE with another one (the block is
        written here); None when the holes decide nothing.
    @note What the holes cannot see is never decided by them (adversarial
        review, 2026-10-31): the VIEW (the check never mirrors, so a top and
        a bottom reading of one layout fit the same circles) -- declared views
        that differ are never settled here -- and the absolute SCALE (the fit
        scales freely; the body bounds only a ratio taken from the same
        answer) -- a reading of the same shape at another scale blocks. The
        chosen reading's declared view is written with its pads; a view the
        chosen reading did not declare is never taken from a reading the
        holes refuted (review 2026-11-04, C3/M8): the answer's view is then
        withdrawn to unknown and asked.
    """
    if holes.views_differ:
        return None
    numbers = {_pad_number_text(pin.get("number")) for pin in pins if isinstance(pin, dict)}
    # A pad any reading found and the answer lacks is a terminal the answer
    # left out: its pins cannot be written whole, whatever the holes say
    # (final generality audit D-B: a 4-pad answer was confirmed by four of
    # five drawn holes while the dedicated reading had the fifth pin).
    if any(set(pads) - numbers for _key, pads, _spans, _centred in readings):
        return None
    complete = [(key, pads) for key, pads, _spans, _centred in readings
                if numbers <= set(pads)]
    chosen = _pick_by_holes(complete, holes)
    if chosen is None:
        # Measuring is for readings the holes confirm NONE of; a refusal for
        # ambiguity (two confirmed, mirror fitting too, a witness conflict)
        # stands (final generality audit D-A).
        if any(_holes_confirm(holes(pads)) for _key, pads in complete):
            return None
        return _settle_by_measure(data, pins, readings, holes, factor, progress, why, remark)
    key, pads, verdict = chosen
    library = getattr(verdict, "source", None) == "library"
    # A library footprint is compared at its own absolute scale: a reading
    # of the same shape at another scale is already a «no_match» there.
    others = [] if library else [p for k, p, _s, _c in readings if k != key] + holes.discarded
    for other in others:
        ratio = _scale_ratio(pads, other)
        if ratio is not None:
            _block(data, tx("I fori disegnati confermano la forma di una lettura ({source}), ma "
                            "un'altra lettura ha la stessa forma in scala {ratio:.3f}: i fori non "
                            "dicono quale scala e' giusta (unita' di misura o quote lette male).",
                            source=_reading_source(key), ratio=ratio) + remark, progress)
            return "blocked"
    spans, centred = next((s, c) for k, _p, s, c in readings if k == key)
    _align_body_axes(data, holes, pads, progress)
    if key != "main":
        _write_chain_pads(data, pins, {"pads": pads, "spans": spans, "centred": centred}, factor)
    view = holes.views.get(key) or "unknown"
    if view in ("top", "bottom"):
        _adopt_view(data, view, _reading_source(key))
    elif key != "main":
        # The chosen reading did not say its side; the answer's declared
        # view belongs to the main reading, which the holes refuted here.
        _withdraw_view(data, tx("la vista dichiarata dalla risposta principale apparteneva a una "
                                "lettura che i fori disegnati non confermano"))
    if library:
        _settle(data, tx("Posizioni dei pad scelte dal footprint KiCad della sigla. Lettura usata: "
                         "{source} ({holes}); nessun'altra lettura ha la sua disposizione ({why}). "
                         "Il footprint di libreria verifica solo la geometria: numeri e nomi dei "
                         "pad restano quelli del disegno, da controllare.",
                         source=_reading_source(key), holes=_hole_text(verdict), why=why)
                + remark, progress)
        return key
    _settle(data, tx("Posizioni dei pad scelte dal controllo dei fori disegnati. Lettura "
                     "usata: {source} ({holes}); nessun'altra lettura coincide con i fori "
                     "({why}). I fori verificano le posizioni, non i numeri dei pad ne' la vista "
                     "dichiarata ({view}): controllarli sul disegno.",
                     source=_reading_source(key), holes=_hole_text(verdict), why=why,
                     view=view) + remark, progress)
    return key


#: Per-pad provenance of a coordinate the program MEASURED on the drawing's
#: pixels (`_settle_by_measure`): an estimate, shown as such by the GUI.
MEASURED_PIXELS_SOURCE = "measured_pixels"


def _settle_by_measure(data: dict, pins: list, readings: list, holes, factor: float,
                       progress, why: str, remark: str):
    """Pad positions MEASURED on the drawn holes, numbered by one reading.

    Used only when no reading is confirmed as it stands. For each complete
    reading (same pad numbers as the answer), `holes.measure` fits its pads
    to the detected holes (drawing_holes.measured_layout): every pad but at
    most one must sit on a hole, and the one off it is moved onto the only
    free hole left, so geometry comes from the drawing and identity from
    the reading. Written only when exactly one measured layout results --
    readings measuring to the same SIGNED OFFSETS count as one (never
    compared by absolute coordinates: their origins differ, review
    2026-11-04 M10) -- and never when the declared views differ (the holes
    cannot tell the side).

    @return The key of the reading whose numbers were used, or None.
    @note Positions only: the numbers stay the reading's, and the note says
        they are to be checked. The moved pad is named with where it was
        read and carries `source` MEASURED_PIXELS_SOURCE in its pin dict.
    @note The VIEW is adopted only when every reading that declares one
        declares the same (C3): the reading measured here was refuted by the
        witness, so its side alone is not taken; with none, or with
        disagreeing ones, the view is withdrawn to unknown and asked.
    @note Never a way round a refusal (final generality audit D-A): only a
        reading with one pad moved counts (one fitting whole was refused for
        another reason), the completed layout must pass _pick_by_holes
        (combined witnesses, mirror) and the scale check, and only the axis
        on which the moved pad missed its hole is replaced (D-C).
    """
    numbers = {_pad_number_text(pin.get("number")) for pin in pins if isinstance(pin, dict)}
    found = _measured_layout_of(readings, holes, numbers)
    if found is None:
        return None
    key, spans, centred, result, positions = found
    _align_body_axes(data, holes, positions, progress)
    if key == "main":
        # The main answer's own frame and origin: only the coordinates change.
        for pin in pins:
            x, y = positions[pin["number"]]
            pin["x_mm"], pin["y_mm"] = round(x / factor, 4), round(y / factor, 4)
    else:
        # The reading's chains were wrong somewhere (a pad had to be moved):
        # an outline quote in them may hang on the wrong pad, so the body is
        # not placed by it (estimated, _write_chain_pads step 3).
        _write_chain_pads(data, pins, {"pads": positions, "centred": False,
                                       "spans": {"x": None, "y": None, "edges": {}}}, factor)
    for pin in pins:
        if pin.get("number") == str(result.moved):
            pin["source"] = MEASURED_PIXELS_SOURCE
    declared = {v for v in holes.views.values() if v in ("top", "bottom")}
    if len(declared) == 1:
        view = next(iter(declared))
        _adopt_view(data, view, tx("tutte le letture che la dichiarano"))
    else:
        view = "unknown"
        _withdraw_view(data, tx("nessuna lettura confermata dai fori la dichiara"))
    _settle(data, tx("Posizioni dei pad misurate sui fori disegnati in {image} (scarto massimo "
                     "{error:.2f} mm, tolleranza {tolerance:.2f} mm), numeri dalla {source} "
                     "({why}).", image=result.image, error=result.max_error_mm,
                     tolerance=result.tolerance_mm, source=_reading_source(key), why=why)
            + _moved_pad_text(result, positions)
            + " " + tx("I fori danno le posizioni, non i numeri dei pad ne' la vista "
                       "dichiarata ({view}): controllarli sul disegno.", view=view)
            + remark, progress)
    return key


def _moved_pad_text(result, positions: dict) -> str:
    """The sentence naming the pad a measure moved onto the only free hole, where it was read and where it went."""
    return tx(" Il pad {pad} letto in ({x:.2f}, {y:.2f}) mm non cadeva su un foro: spostato "
              "sull'unico foro libero, in ({nx:.2f}, {ny:.2f}) mm; la coordinata corretta e' "
              "STIMATA dai pixel del disegno, da verificare.", pad=result.moved,
              x=result.moved_from_mm[0], y=result.moved_from_mm[1],
              nx=positions[str(result.moved)][0], ny=positions[str(result.moved)][1])


def _measured_layout_of(readings: list, holes, numbers: set):
    """The one layout MEASURED on the drawn holes with a reading's numbers, or None.

    The deduction of `_settle_by_measure`, without writing anything: shared
    with `_hole_backed_layouts`, which only OFFERS the layout.

    @param readings [(key, pads, spans, centred)] at hand.
    @param holes The `_HoleVerdicts` of the confrontation.
    @param numbers The answer's pad numbers: only a reading with exactly
        these is measured.
    @return (key, spans, centred, result, positions): the reading whose
        numbers were used, its spans, the drawing_holes.MeasuredLayout and
        the completed layout {number: (x, y)} mm in that reading's frame;
        None when nothing is measured, the readings measure differently, or
        the completed layout fails what any chosen reading must pass (see
        the notes of `_settle_by_measure`).
    """
    if holes.measure is None or holes.views_differ:
        return None
    measured = []
    for key, pads, spans, centred in readings:
        if set(pads) != numbers:
            continue
        result = None
        try:
            context = holes.context(pads) if hasattr(holes, "context") else {}
            # The measure is bounded by the body like the check: with the
            # axes not fixed by the plan it is tried with either assignment
            # (_HoleVerdicts); the completed layout must still pass
            # _pick_by_holes below, which says which assignment fitted.
            for body in (holes.bodies() if hasattr(holes, "bodies") else [holes.body]):
                result = _call_witness(holes.measure, dict(pads), body, context)
                if result is not None:
                    break
        except Exception:  # noqa: BLE001 - a local measure never loses a paid answer
            result = None
        if result is not None:
            measured.append((key, spans, centred, result))
    # A reading that fits its holes whole (nothing moved) and was still not
    # confirmed was refused for another reason (a witness conflict, a
    # distorted fit): measuring would only undo that refusal.
    measured = [entry for entry in measured if entry[3].moved is not None]
    if not measured:
        return None
    candidates = []
    for key, spans, centred, result in measured:
        positions = {str(n): tuple(p) for n, p in result.positions_mm.items()}
        moved = str(result.moved)
        read = result.moved_from_mm
        # Only the axis on which the reading missed its hole is replaced:
        # a coordinate within the fit's tolerance was read from a printed
        # quote and is more exact than a pixel estimate (audit D-C).
        positions[moved] = tuple(
            r if abs(m - r) <= result.tolerance_mm else m
            for m, r in zip(positions[moved], read))
        candidates.append((key, spans, centred, result, positions))
    first = candidates[0][4]
    for other in candidates[1:]:
        if _offset_disagreements(first, other[4], sorted(numbers), PAD_READING_TOLERANCE_MM):
            return None
    key, spans, centred, result, positions = candidates[0]
    # The completed layout must pass what any chosen reading passes: the
    # combined witnesses confirm it, its mirror does not fit as well, and no
    # other reading has its shape at another scale (audit D-A).
    if _pick_by_holes([(key, positions)], holes) is None:
        return None
    if any(_scale_ratio(positions, other) is not None
           for k, other, _s, _c in readings if k != key) or any(
            _scale_ratio(positions, other) is not None for other in holes.discarded):
        return None
    return key, spans, centred, result, positions


def _holes_alone_confirm(verdict) -> bool:
    """Whether the DRAWN HOLES confirmed a layout on a page that could show its scale.

    A confirmation of the drawing itself -- not a library footprint, not a
    conflict of the two witnesses -- whose pads could show a stretch of the
    page and showed none (`scale_observable`, drawing_holes): the only kind
    that proves the page is drawn to scale with the quotes of the layout.
    """
    return (_holes_confirm(verdict) and not isinstance(verdict, _WitnessConflict)
            and getattr(verdict, "source", None) != "library"
            and getattr(verdict, "scale_observable", None) is True)


def _pads_the_holes_single_out(pads: dict, holes) -> list:
    """The pads without which the rest of a layout is confirmed by the drawn holes, undistorted.

    @param pads A layout the holes did NOT confirm, {number: (x, y)} mm.
    @param holes The `_HoleVerdicts` of the confrontation.
    @return Pad numbers, in pad order: for each, the layout WITHOUT that pad
        is confirmed by the drawing on a page that could show its scale
        (`_holes_alone_confirm`). Empty when no such pad exists.
    @note NOT A PROOF OF SCALE (review V3-1, 2026-11-11): the stretch a fit
        may show (drawing_holes._allowed_stretch) grows as pads are removed,
        so on a stretched page a subset of correct pads passes where the
        whole set does not. Used only to NAME the pads of a «no_match»
        contradiction (which stands on its own) and to try the chain
        reading's unattributed stops; never to turn «distorted» into a
        contradiction (`_agreement_contradicted` asks the witness's own
        `misplaced_pad` for that).
    """
    singled = []
    for number in sorted(pads, key=_pad_number_key):
        rest = {other: xy for other, xy in pads.items() if other != number}
        if _holes_alone_confirm(holes(rest)):
            singled.append(number)
    return singled


def _agreement_contradicted(pads: dict, holes):
    """Whether the drawn holes contradict, OBSERVABLY, a layout that two readings agree on.

    Two models reading one drawing can make the same mistake (user,
    2026-11-11: both put a pad in the column of its neighbours, 2 mm off the
    hole drawn for it, and the footprint was generated on their agreement).
    The holes are evidence independent of both, but an imperfect one: a
    contradiction they can stand behind keeps the agreement from confirming
    and puts the layout to the user (round 7). ONE rule for every place where an
    agreement would confirm (`apply_dedicated_pads`, `_resolve_chain_candidate`
    and, through them, the partial re-read).

    @param pads The layout the readings agree on, in the frame the witness checks.
    @param holes The `_HoleVerdicts` of the confrontation.
    @return None when nothing contradicts it: confirmed, not checked,
        unverifiable (no drawing, a photo, too few circles, a page the fit
        cannot judge) or refused only for the body's scale; otherwise
        {"verdict", "singled", "misplaced", "fitted", "stretch"}:
        - an observable «no_match» (`_losing_verdict`: a pad lands where no
          hole is, on pads that could have shown a page not to scale), with
          or without a decisive fit ("fitted", see the round 7b note);
        - «distorted» with a fit stretch ("stretch") beyond
          drawing_holes.HYPOTHESIS_TOL_SPAN and no single pad named (round
          7b, F2);
        - «distorted», when the witness names the ONE pad no stretch of the
          page explains (`misplaced_pad`, drawing_holes._single_misplaced:
          the whole layout is not an affine image of its holes at the
          per-hole tolerance, and without that pad the others -- enough to
          show a stretch -- fit with no stretch at all). Without that proof
          «distorted» contradicts nothing: mechanical drawings are often not
          drawn to scale with their own quotes, and two agreeing
          transcriptions of the printed quotes are then right (the reason it
          has been a warning since 2026-10-31); the warning stays. Several
          candidate pads, or a layout an affine map fits, are a page that
          may be stretched: warning.
        "singled" lists that pad for «distorted», the pads of
        `_pads_the_holes_single_out` for «no_match» (used only to try the
        chain reading's unattributed stops, never written in a message);
        "misplaced" is the witness's `misplaced_pad` when it is a pad of the
        layout, else None.
    @note Review V3-1 (2026-11-11): «distorted» used to be proven by
        `_pads_the_holes_single_out`, unsound for it: the stretch a fit may
        show grows as pads are removed (drawing_holes._allowed_stretch), so
        on a page stretched 3.5-7 % a subset of CORRECT pads passed where the
        whole set did not, and correct layouts were blocked.
    @note «body_scale» is not a contradiction of the PADS: the body sizes
        compared are the main answer's claim, and the body reading that
        follows judges them.
    @note Review V4-2 (2026-11-11): a «no_match» counts only with a DECISIVE
        fit (`max_error_mm` not None): some assignment of the pads to drawn
        circles was fitted and refused. Without one the witness found no
        fit at all -- stray circles touching the rings can hide every hole
        from the search -- and that measures nothing about the layout: the
        agreement stands with the warning note it had before round 6.
        SUPERSEDED by round 7b (2026-11-11, F1): that made a regression
        against 1266fb7, which refused such an agreement -- three WRONG
        readings on pages crowded with stray circles were confirmed with a
        warning. Since a contradiction now costs a question and never a
        block, a «no_match» without a fit goes to the question too, with a
        text that claims only what was measured: the holes do not coincide
        with the layout and confirm no other ("fitted" False).
    @note Round 7b (2026-11-11, F2): a «distorted» verdict without a named
        pad contradicts the agreement when the stretch of its fit exceeds
        HYPOTHESIS_TOL_SPAN -- a pitch read 7 % wrong by both readings fits
        the holes only that way. Fragile at the border, declared: a WRONG
        reading whose error is just under the span (about 5-6 %) still
        passes with the warning, exactly as on 1266fb7, and a page truly
        drawn more than 6 % off its quotes now costs a question.
    @note Decision of round 7 (coordinator, 2026-11-11): a contradiction is
        never a hard block nor a silent adoption; `_refuse_contradicted_agreement`
        turns it into the "pad_layout" question. "misplaced" carries the
        witness's own `misplaced_pad` (or None), the only pad the message
        may name.
    """
    verdict = holes(pads)
    if getattr(verdict, "status", None) != "not_confirmed" or isinstance(verdict, _WitnessConflict):
        return None
    reason = getattr(verdict, "reason", None)
    named = getattr(verdict, "misplaced_pad", None)
    misplaced = named if named is not None and named in pads else None
    if _losing_verdict(verdict, pads, ("no_match",)):
        # Round 7b (F1): with or without a decisive fit. "fitted" records
        # which: without one the witness found no assignment at all, and
        # the text says only that (`_contradiction_text`).
        fitted = _optional_number(getattr(verdict, "max_error_mm", None)) is not None
        return {"verdict": verdict, "singled": _pads_the_holes_single_out(pads, holes),
                "misplaced": misplaced, "fitted": fitted, "stretch": None}
    if reason == "distorted":
        # Review V3-1 (2026-11-11): the proof is the witness's own, on the
        # circles of this very fit (drawing_holes._single_misplaced): no
        # affine map of the whole layout reaches its holes within the
        # per-hole tolerance, and ONE pad's removal leaves a layout that fits
        # with no stretch at all. A subset confirmed under the stretch
        # allowance proved nothing: that allowance grows as pads are removed.
        if misplaced is not None:
            return {"verdict": verdict, "singled": [misplaced], "misplaced": misplaced,
                    "fitted": True, "stretch": None}
        # Round 7b (F2): no single pad, but a stretch larger than the one
        # the hole search itself attributes to a page not to scale
        # (HYPOTHESIS_TOL_SPAN): a whole axis read wrong by that much and a
        # page drawn that far off its quotes look the same to the witness,
        # so the user decides. Below that span the warning stays: two
        # agreeing transcriptions on a page not drawn to scale are the
        # legitimate case (commit 65aee39).
        stretch = _optional_number(getattr(verdict, "stretch_ratio", None))
        if stretch is not None and stretch > _page_stretch_span():
            return {"verdict": verdict, "singled": [], "misplaced": None, "fitted": True,
                    "stretch": stretch}
    return None


def _page_stretch_span() -> float:
    """The stretch ratio the hole search attributes to a page not to scale (drawing_holes.HYPOTHESIS_TOL_SPAN)."""
    from .drawing_holes import HYPOTHESIS_TOL_SPAN
    return float(HYPOTHESIS_TOL_SPAN)


def _contradiction_text(contradiction: dict, alternatives: bool = False) -> str:
    """Why an agreement is not confirmed, for the question: only what the witness measured.

    Decision of round 7 (2026-11-11): the witness cannot be perfect
    (perspective, stray circles, page stretch), so the text never claims
    what it cannot prove -- no «the page is to scale», no «that pad is not
    where the readings put it». It says the drawn holes do not confirm the
    layout, with the witness's own reason, and names a pad only when the
    witness itself singled one out (`misplaced_pad`).

    @param contradiction Result of `_agreement_contradicted`.
    @param alternatives Whether a layout the holes confirm is offered beside
        the readings' (`_hole_backed_layouts`): without one, a «no_match»
        with no fit at all is said as such -- the holes coincide with this
        layout and confirm no other (round 7b, F1); with one, that would be
        false and the general sentence is used.
    """
    verdict = contradiction["verdict"]
    image = getattr(verdict, "image", None) or "?"
    if contradiction.get("fitted") is False and not alternatives:
        return tx("le due letture concordano, ma i fori disegnati in {image} non coincidono con "
                  "questa disposizione e non ne confermano nessun'altra", image=image)
    stretch = contradiction.get("stretch")
    if stretch is not None:
        # Round 7b (F2): the measured stretch and the span it exceeds.
        return tx("le due letture concordano, ma i fori disegnati in {image} coincidono con questa "
                  "disposizione solo stirando il disegno del {stretch:.1f}%, oltre il {span:.0f}% "
                  "che il controllo attribuisce a una pagina non in scala", image=image,
                  stretch=100.0 * stretch, span=100.0 * _page_stretch_span())
    text = tx("le due letture concordano, ma i fori disegnati non confermano questa "
              "disposizione ({holes})", holes=_hole_text(verdict))
    misplaced = contradiction.get("misplaced")
    if misplaced is not None:
        text += "; " + tx("il pad {pad} e' il piu' lontano dai fori", pad=misplaced)
    return text


def _hole_backed_layouts(readings: list, holes, numbers: set, contradiction: dict,
                         references=None, declared_frame=None) -> list:
    """Layouts the drawn holes CONFIRM where they contradict the layout two readings agree on.

    Offered as candidates of the "pad_layout" question beside the readings'
    layout (`_block_or_ask(extras=)`), never written: the holes verify
    geometry, not numbering, and the numbers of these layouts are the
    readings'. Two sources, both checked by the witness:
    1. the MEASURE (`_measured_layout_of`): every pad but one on its hole,
       that one moved onto the only free hole. Its coordinate is estimated
       from the pixels and carries MEASURED_PIXELS_SOURCE;
    2. a printed quote the chain reading transcribed and attributed to NO
       pad (`references`: a "ref:" stop, a datum line), when the holes single
       out exactly one pad: that pad is tried on the unattributed stop, on
       each axis the stop has, and kept when the holes then confirm the
       whole layout (`_holes_alone_confirm`) and choose it alone
       (`_pick_by_holes`: its mirror image must not fit as well). Every
       other quote of the reading is kept, so its edge quotes still place
       the body; the coordinate is a printed quote, not a pixel estimate.
    Layouts with the same signed offsets are one; two different ones from
    source 2 are an ambiguity and none is offered.

    @param readings [(key, pads, spans, centred)] of the agreeing readings,
        "dedicated" (the chain reading, whose frame `references` share) and
        the others.
    @param numbers The answer's pad numbers.
    @param contradiction Result of `_agreement_contradicted`.
    @param references {label: (x or None, y or None)} mm of the chain
        reading, its frame; None or empty for none.
    @param declared_frame The main answer's pads (mm) when the answer
        DECLARES its body-centre frame, else None: a layout of source 2 is
        then given in that frame, as an agreement keeps it.
    @return [{"pads", "spans", "centred", "main_frame", "label", "moved",
        "pixels"}]: the layout in the frame named by `main_frame` (True: the
        main answer's; False: a chain reading's, with its spans), the text
        saying where it comes from, the pad that differs from the readings
        and whether its coordinate was estimated from the pixels.
    """
    layouts = []
    measured = _measured_layout_of(readings, holes, numbers)
    if measured is not None:
        key, _spans, _centred, result, positions = measured
        layouts.append({
            "pads": positions, "main_frame": key == "main", "moved": str(result.moved),
            "pixels": True, "centred": False,
            # As `_settle_by_measure`: a reading that needed a pad moved may
            # hang an outline quote on the wrong pad, so the body is not
            # placed by its edge quotes.
            "spans": {"x": None, "y": None, "edges": {}},
            "label": tx("posizioni misurate sui fori disegnati in {image} (scarto massimo "
                        "{error:.2f} mm, tolleranza {tolerance:.2f} mm), numeri dalla {source}.",
                        image=result.image, error=result.max_error_mm,
                        tolerance=result.tolerance_mm, source=_reading_source(key))
            + _moved_pad_text(result, positions)})
    chain = next(((pads, spans, centred) for key, pads, spans, centred in readings
                  if key == "dedicated" and set(pads) == numbers), None)
    singled = contradiction["singled"]
    if chain is not None and len(singled) == 1 and not holes.views_differ and not layouts:
        pads, spans, centred = chain
        pad = singled[0]
        variants = []
        for label, stop in sorted((references or {}).items()):
            x, y = stop
            moves = [(x, pads[pad][1])] if x is not None else []
            moves += [(pads[pad][0], y)] if y is not None else []
            moves += [(x, y)] if x is not None and y is not None else []
            for position in moves:
                variant = dict(pads, **{pad: position})
                if not _holes_alone_confirm(holes(variant)):
                    continue
                if any(not _offset_disagreements(variant, other, sorted(numbers),
                                                 PAD_READING_TOLERANCE_MM)
                       for _label, other in variants):
                    continue
                variants.append((label, variant))
        if len(variants) == 1 and _pick_by_holes([(HOLES_SOURCE, variants[0][1])], holes):
            label, variant = variants[0]
            in_main = declared_frame is not None and set(declared_frame) == numbers
            if in_main:
                # The answer declares its body-centre frame, which an
                # agreement keeps: the same move, applied to the main pads.
                shift = (variant[pad][0] - pads[pad][0], variant[pad][1] - pads[pad][1])
                variant = dict(declared_frame, **{pad: (declared_frame[pad][0] + shift[0],
                                                        declared_frame[pad][1] + shift[1])})
            layouts.append({
                "pads": variant, "main_frame": in_main, "moved": pad, "pixels": False,
                "spans": spans or {}, "centred": bool(centred),
                "label": tx("pad {pad} sulla quota stampata che la lettura dedicata non ha "
                            "attribuito a nessun pad («{label}»): {holes}", pad=pad, label=label,
                            holes=_hole_text(holes(variant)))})
    return layouts


def _refuse_contradicted_agreement(data: dict, pins: list, readings: list, holes, factor: float,
                                   progress, remark: str, contradiction: dict, references=None,
                                   prefix: str = "") -> None:
    """Refuse to confirm a layout two readings agree on and the drawn holes contradict: always ask.

    Nothing is written on the strength of the agreement and nothing on the
    strength of the holes alone (user, 2026-11-11). Decision of round 7
    (coordinator, 2026-11-11): the drawn-hole witness cannot be perfect --
    perspective, stray circles touching the rings and page stretch produced
    false blocks or false confirmations in three successive rounds -- so the
    outcome is ALWAYS the "pad_layout" question, never a hard block: the
    readings' layout is offered, with what the holes said about it, and
    beside it the layout the holes confirm (`_hole_backed_layouts`) when one
    exists. The only block left is structural: pads sharing a number, which
    no chosen layout could be written back to (`_block_or_ask`).

    @param readings [(key, pads, spans, centred)] of the readings at hand.
    @param contradiction Result of `_agreement_contradicted`.
    @param references The chain reading's unattributed stops, its frame.
    @param prefix Text put before the message (the main answer's own
        conflict, when it had one).
    """
    numbers = {_pad_number_text(pin.get("number")) for pin in pins if isinstance(pin, dict)}
    declared_frame = None
    if data["package_geometry"].get("origin") == "body_center":
        declared_frame = next((pads for key, pads, _s, _c in readings if key == "main"), None)
    extras = _hole_backed_layouts(readings, holes, numbers, contradiction, references,
                                  declared_frame)
    why = _contradiction_text(contradiction, alternatives=bool(extras))
    _block_or_ask(data, pins, readings, holes, factor,
                  prefix + tx("Posizioni dei pad NON confermate ({why}).", why=why), progress,
                  remark, extras=extras, contradicted=True)


def _adopt_view(data: dict, view, source=None) -> None:
    """Write a declared view ("top"/"bottom") where the pads take it from: `source_view`.

    `source` names who declared it (a reading, the user) for the result's
    note (`_view_source`), so the view actually applied is reported with
    its origin (review 2026-11-04, C3).
    """
    if view in ("top", "bottom"):
        data["contacts_view"] = view
        data["package_geometry"]["source_view"] = view
        data["_view_source"] = source or tx("risposta principale")


def _withdraw_view(data: dict, why: str) -> None:
    """Forget the answer's declared view: the pads are left as read, the side is asked.

    Used when the declared view belongs to a reading the witness refuted
    (C3): `source_view` becomes the "top" frame (pads as read, not
    mirrored) and `contacts_view` unknown, so `_require_declared_view`
    asks the side instead of a refuted reading deciding the mirror.
    """
    if data.get("contacts_view") in ("top", "bottom"):
        data["contacts_view"] = "unknown"
        data["package_geometry"]["source_view"] = "top"
        data.pop("_view_source", None)
        data["notes"] = (str(data.get("notes") or "") + "\n" + tx(
            "Vista dei terminali ritirata: {why}.", why=why)).strip()


def _scale_ratio(chosen: dict, other: dict):
    """The uniform scale between two layouts of the SAME shape, or None.

    None when they share fewer than two pads, differ in shape (beyond
    PAD_READING_TOLERANCE_MM once scaled), or have the same scale. The
    ratio is fitted on the common pads' offsets from their centroid.
    """
    common = sorted(set(chosen) & set(other))
    if len(common) < 2:
        return None
    cx = sum(chosen[n][0] for n in common) / len(common)
    cy = sum(chosen[n][1] for n in common) / len(common)
    ox = sum(other[n][0] for n in common) / len(common)
    oy = sum(other[n][1] for n in common) / len(common)
    a = [(chosen[n][0] - cx, chosen[n][1] - cy) for n in common]
    b = [(other[n][0] - ox, other[n][1] - oy) for n in common]
    norm = sum(x * x + y * y for x, y in a)
    if norm <= 0:
        return None
    ratio = sum(x * u + y * v for (x, y), (u, v) in zip(a, b)) / norm
    shape = max(math.hypot(u - ratio * x, v - ratio * y) for (x, y), (u, v) in zip(a, b))
    extent = max(math.hypot(x, y) for x, y in a)
    if shape > PAD_READING_TOLERANCE_MM or abs(ratio - 1.0) * extent <= PAD_READING_TOLERANCE_MM:
        return None
    return ratio


def _resolve_chain_candidate(data: dict, pins: list, main: dict, reading: dict,
                             views_differ: bool, factor: float, progress, remark: str,
                             holes=None, exchanged: bool = False) -> None:
    """Settle the main answer's own disagreement (its coordinates against its chains) by the dedicated reading.

    The dedicated chains, summed by the program, side with whichever of the
    two describes the same layout by signed offsets over EVERY pin, with the
    same declared view: that one is kept and the conflict is lifted. Siding
    with neither keeps the block, extended with the third reading, unless
    the drawn holes confirm exactly one of the three (_settle_by_holes).

    The closure requirement is deliberately one-sided. Siding with the
    CHAINS needs none: two transcriptions summed by the program agree, and
    coordinates disagreeing with their own chains point at the model's
    arithmetic. Siding with the model's COORDINATES needs the dedicated
    reading closed on the body on both axes and compared with a declared
    body size: two readings never checked against the drawing may share one
    misread quote (the reported failure mode) and must not outvote chains
    the program summed. A drawing with no outline quote, or an answer with
    no body size, can therefore not be auto-confirmed when the model mis-sums:
    a false block at worst. Limit, declared: two calls of one model can
    repeat one transcription slip (a label swap) and agree on it; the result
    stays «needs_review».
    """
    holes = holes if holes is not None else _HoleVerdicts(None, None)
    candidate = data["_chain_candidate"]
    dedicated = reading["pads"]
    numbers = sorted(pin["number"] for pin in pins)
    # The candidate exists only when every pin has coordinates (see
    # _place_pins_from_chains); checked again so a reshaped answer cannot
    # raise here.
    # Same pin SET, not a subset: pads only the dedicated reading found mean
    # the answer leaves terminals out, which its own pads cannot confirm.
    complete = (not views_differ and set(numbers) == set(dedicated)
                and set(numbers) <= set(main))
    spans = reading.get("spans") or {}
    # «Closed» means VERIFIED: a span exists on both axes AND a body size to
    # compare it with (apply_dedicated_pads has already rejected a mismatch).
    # A span with no body size proves nothing (review 5, finding A).
    geometry = data["package_geometry"]
    dedicated_closed = all(
        spans.get(axis) is not None and _optional_number(geometry.get(key), positive=True) is not None
        for axis, key in (("x", "body_length_mm"), ("y", "body_width_mm")))
    summary = _pad_summary(dedicated) or tx("nessuno")
    readings = [("main", main, {}, False),
                ("chains", candidate["pads"], candidate["spans"], candidate["centred"])]
    if not views_differ:
        readings.append(("dedicated", dedicated, spans, _centred(spans, exchanged)))
    tolerance = _reading_tolerance(factor)
    # The layout two of the three readings agree on, if any: the drawn holes
    # may contradict it as they may any agreement (`_agreement_contradicted`).
    agreed = None
    if complete and not _offset_disagreements(candidate["pads"], dedicated, numbers, tolerance):
        agreed = candidate["pads"]
    elif (complete and dedicated_closed
          and not _offset_disagreements(main, dedicated, numbers, tolerance)):
        agreed = main
    contradiction = _agreement_contradicted(agreed, holes) if agreed is not None else None
    if contradiction is not None:
        # Two readings that agree and the drawing that says otherwise: the
        # agreement confirms nothing here either; the main answer's own
        # conflict stays in the message.
        _refuse_contradicted_agreement(
            data, pins, readings, holes, factor, progress, remark, contradiction,
            reading.get("references"), prefix=data["_pin_map_conflict"] + " ")
        return
    if complete and not _offset_disagreements(candidate["pads"], dedicated, numbers, tolerance):
        _align_body_axes(data, holes, candidate["pads"], progress)
        _write_chain_pads(data, pins, candidate, factor)
        note = tx("Posizioni dei pad prese dalle catene di quote della risposta principale: la "
                  "lettura dedicata concorda con esse (scostamenti entro {tolerance} mm) e non "
                  "con le coordinate calcolate dal modello.", tolerance=tolerance)
    elif complete and not _offset_disagreements(main, dedicated, numbers, tolerance):
        if not dedicated_closed:
            # Two readings that were never checked against the drawing may
            # share one misread quote (the reported failure mode): without a
            # closure on the body they cannot outvote chains the program
            # summed. Only a closed dedicated reading, or the drawn holes,
            # may side with the model's own sums.
            extra = tx("Lettura dedicata ({view}): {dedicated}. Concorda con le coordinate "
                       "calcolate dal modello, ma non e' chiusa sul contorno del corpo: non "
                       "basta a preferirle alle catene di quote.", view=reading["view"],
                       dedicated=summary)
            return _block_chain_conflict(data, pins, readings, holes, factor, extra, remark,
                                         progress)
        note = tx("Posizioni dei pad confermate dalla lettura dedicata ({count} pad, scostamenti "
                  "entro {tolerance} mm); le catene di quote della risposta principale, che non "
                  "concordavano, sono state scartate.", count=len(numbers),
                  tolerance=tolerance)
    elif not complete:
        extra = tx("Lettura dedicata ({view}): {dedicated}. Incompleta o con vista diversa "
                   "dalla risposta principale: non basta a decidere.", view=reading["view"],
                   dedicated=summary)
        return _block_chain_conflict(data, pins, readings, holes, factor, extra, remark, progress)
    else:
        extra = tx("Lettura dedicata ({view}): {dedicated}. Non concorda ne' con le coordinate "
                   "del modello ne' con le sue catene di quote.", view=reading["view"],
                   dedicated=summary)
        return _block_chain_conflict(data, pins, readings, holes, factor, extra, remark, progress)
    _settle(data, note + remark, progress)


def _block_chain_conflict(data, pins, readings, holes, factor, extra, remark, progress) -> None:
    """Keep the main answer's chain conflict, unless the drawn holes pick one reading."""
    if _settle_by_holes(data, pins, readings, holes, factor, progress,
                        tx("le letture non concordano"), remark):
        return
    message = (data["_pin_map_conflict"] + " " + extra + " "
               + tx("Controllo dei fori disegnati: {verdicts}.",
                    verdicts=_hole_verdicts(readings, holes)))
    _block_or_ask(data, pins, readings, holes, factor, message, progress, remark)


def _main_pads(pins, factor: float):
    """The main answer's pads in mm ({number: (x, y)}) and the numbers given twice."""
    main, seen, repeated = {}, set(), set()
    for pin in pins:
        if not isinstance(pin, dict):
            continue
        x = _optional_number(pin.get("x_mm"))
        y = _optional_number(pin.get("y_mm"))
        number = _pad_number_text(pin.get("number"))
        if number in seen:
            repeated.add(number)
        seen.add(number)
        if number and x is not None and y is not None:
            main[number] = (x * factor, y * factor)
    return main, repeated


def _unread_main_remark(pins) -> str:
    """The clause naming the main answer's pads left without positions, or "" (L3).

    Appended to a «not confirmed» reason so the message says the cause the
    user can act on: the main answer did not read those positions.
    """
    unread = _pads_without_positions({"pins": pins})
    if not unread:
        return ""
    return "; " + tx("la risposta principale non ha letto le posizioni dei pad {pads}",
                     pads=", ".join(unread))


def _centred(spans: dict, exchanged: bool) -> bool:
    """Whether a chain reading may be written with the body centre as origin (outline on both axes)."""
    return spans.get("x") is not None and spans.get("y") is not None and not exchanged


def _withdraw_dedicated_holes(data: dict) -> None:
    """Drop the printed holes of a dedicated reading the drawing contradicts, saying so when it had some (review E8).

    A reading whose chains do not add up misread at least one quote: its
    hole callouts are no independent transcription either, so
    `_resolve_pad_terminals` must not take a hole from it, nor count it as
    the second reading of a printed hole. The terminal sections it
    transcribed go the same way: they confirm no terminal.
    """
    data.pop("_dedicated_leads", None)
    holes = data.pop("_dedicated_holes", None)
    if holes:
        data["notes"] = (str(data.get("notes") or "") + "\n" + tx(
            "Fori stampati trascritti dalla lettura dedicata non usati: le sue quote non tornano "
            "con il disegno.")).strip()


def _body_quotes_bound(geometry: dict) -> bool:
    """Whether the plan sizes of the body are printed as a maximum or a minimum (`body_quote_kind`), not nominal."""
    kind = geometry.get("body_quote_kind") if isinstance(geometry, dict) else None
    return kind in BODY_QUOTE_KINDS and kind != BODY_QUOTE_KINDS[0]


def _body_on_page(geometry: dict, factor: float, exchanged: bool):
    """Body outline (along page X, along page Y) in mm for the hole check, or None."""
    sizes = [_optional_number(geometry.get(key), positive=True)
             for key in ("body_length_mm", "body_width_mm")]
    if None in sizes:
        return None
    length, width = (size * factor for size in sizes)
    return (width, length) if exchanged else (length, width)


def apply_dedicated_pads(data: dict, reading: Optional[dict], progress=None,
                         check_layout=None, measure_layout=None, same_model: bool = False) -> None:
    """@brief Confront the dedicated pad reading with the explicit pads of the main answer.

    @param data Main answer with `package_geometry` pins, still in drawing
        units and view (before `_canonicalize_answer`).
    @param reading Result of `read_pad_positions`, or None.
    @param progress Optional log callback.
    @param check_layout Optional callable(pads, body) -> drawing_holes.LayoutCheck:
        the local check of a layout ({number: (x, y)} in mm, page
        orientation, any origin) against the holes drawn in the documents,
        `body` the outline (page X, page Y) in mm or None. None checks
        nothing (every layout unverified).
    @param measure_layout Optional callable(pads, body) ->
        drawing_holes.MeasuredLayout or None: when no reading is confirmed,
        the positions measured on the drawn holes with one reading's numbers
        (_settle_by_measure). None measures nothing.
    @param same_model True when the dedicated reading was made by the SAME
        model as the main answer (a blank pad model, or a pad model whose
        provider was not ready and fell back): two readings of one model
        are not independent, so their agreement is ONE reading and confirms
        nothing without a witness (review 2026-11-04, H6); the note says so.
    @note Rules, the same for every part: the two readings choose their own
        origins, so SIGNED OFFSETS between pads of the same number are
        compared, both in millimetres, within _reading_tolerance
        (PAD_READING_TOLERANCE_MM, or one quote's rounding in an inch drawing);
        pad numbers are matched as text ignoring case (`_respell_reading`).
        Confirmed: every common pair agrees, the declared views agree, the
        dedicated reading placed every pin and no other (pads only the
        dedicated reading found are terminals the answer left out), the two
        readings come from different models and the drawn holes do not
        REFUTE the layout. Otherwise the positions are NOT confirmed
        (user, 2026-10-30: a footprint built on an unconfirmed reading came
        out wrong), with these ways out, in order: the holes drawn in the
        documents confirm exactly ONE of the readings at hand and none of
        the others -- that one is written; the holes MEASURE the positions
        with one reading's numbers (`_settle_by_measure`); with two or more
        complete layouts to choose from, the structured question
        "pad_layout" is recorded for the user (`_block_or_ask`); only then
        the generation is BLOCKED (`_pin_map_conflict`). This covers no
        dedicated reading (failed, discarded, no pad), two readings that
        disagree, a partial dedicated reading and a chain reading that
        contradicts itself or the body.
    @note The hole check is independent of both models (drawing_holes: circles
        found by the program, a least-squares fit of each reading). It sees
        GEOMETRY only: two pad numbers exchanged on the same positions pass
        it, so it never decides between such readings, and a note asks to
        check the numbering of the reading it chose. When the two readings
        agree but the holes CONTRADICT them observably
        (`_agreement_contradicted`: an observable «no_match», or «distorted»
        on a page the other pads prove to scale), they are NOT written
        (review 2026-11-04, H6: two readings wrong the same way passed with
        a warning; user, 2026-11-11: a «distorted» pad passed the same way):
        a layout the holes confirm is OFFERED beside the readings' one in
        the "pad_layout" question, never adopted, else the block
        (`_refuse_contradicted_agreement`). A «distorted» verdict on a page
        nothing proves to scale, and «body_scale», are no contradiction (the
        page may not be to scale): the readings stand with a warning.
    @note Body axes (user, 2026-11-10): the witness is given the body as
        (body_length_mm along X, body_width_mm along Y) of the MAIN answer.
        That order is a printed fact only when an edge-to-edge chain of the
        plan view fixes it (`_plan_fixes_body_axes`, `exchanged`); otherwise
        a layout refused only as «body_scale» is checked with the two sizes
        exchanged (`_HoleVerdicts`), so one reading's inference never
        refutes another reading. The layout that is kept then takes the
        sizes as the drawn outline measured them (`_align_body_axes`), with
        a note; a size pair that fits the outline neither way stays
        «body_scale».
    @note Dimension chains (user, 2026-10-30): the dedicated reading is added
        up by the program from the printed chains (pads_from_chains), so it
        can be checked against the drawing itself. Chains that contradict
        each other, or a chain from outline to outline whose sum differs from
        the body (`package_geometry` body_length_mm along X, body_width_mm
        along Y, or the two exchanged) by more than the rounding of the
        quotes summed (_sum_tolerance), discard the dedicated reading EVEN
        when it agrees with the main answer. Chains that cannot be read are
        only named in the note.
    @note A body given as a maximum or a minimum is compared as such
        (`body_quote_kind`, `_chain_contradictions`); a nominal one as an
        equality: a span off by more than the rounding blocks, a false block
        at worst, never a wrong footprint. Pads sharing a number (shield
        tabs, doubled pins) merge into one stop in the chains, so the chain
        checks are skipped for such parts and said so.
    @note The main answer's pad NUMBERS are not always a reading: a grid
        without pin_map is numbered by convention. When its numbering carries
        no information (convention), or is a different scheme (fewer than two
        numbers shared and not a subset of the dedicated ones), a dedicated
        reading confirmed alone by an independent geometry witness replaces
        the pad list (_replace_by_confirmed_reading). Real printed numbers
        that are a subset of the dedicated ones still block (D-B).
    """
    geometry = data.get("package_geometry")
    pins = geometry.get("pins") if isinstance(geometry, dict) else None
    if not isinstance(pins, (list, tuple)) or not pins:
        return
    # Pin numbers are text everywhere below (pads are keyed by text): a model
    # that sends 1 instead of "1" must not raise a KeyError and lose the paid
    # main answer and the paid pad reading (review S1, 2026-11-03).
    _normalise_pin_numbers(pins)
    if isinstance(reading, dict) and not reading.get("failed") and not reading.get("discarded"):
        # The hole callouts the dedicated reading transcribed (mm), for the
        # per-pad rule `_resolve_pad_terminals`: a printed hole read twice is
        # a reading, read once it is named with its reader. The key exists
        # only when the dedicated reading ran, so «it saw no callout» and
        # «it did not run» stay apart.
        data["_dedicated_holes"] = dict(reading.get("holes") or {})
        if not same_model:
            # The terminal sections it transcribed, the SECOND reading of a
            # pad's terminal (`_confirmed_lead_word`). One model reading
            # twice is one reading (H6): its terminals confirm nothing, so
            # the key is left out exactly as when the reading did not run.
            data["_dedicated_leads"] = dict(reading.get("leads") or {})
    factor = _declared_unit_factor(data)
    if factor is None:
        # The main answer's coordinates have no declared unit: nothing can
        # be compared in mm or confirmed, so the positions block (L11).
        _block(data, tx("Posizioni dei pad NON confermate ({why}).",
                        why=_undeclared_unit_reason()), progress)
        return
    main, repeated = _main_pads(pins, factor)
    # Pads sharing a number collapse into one entry: no hole check on them,
    # never an adoption (adversarial review, 2026-10-31).
    check_layout = None if repeated else check_layout
    measure_layout = None if repeated else measure_layout
    main_view = data.get("contacts_view")
    candidate = data.get("_chain_candidate")
    own = ([("chains", candidate["pads"], candidate["spans"], candidate["centred"])]
           if candidate is not None else [])
    if reading and reading.get("pads"):
        reading = _respell_reading(reading, [pin.get("number") for pin in pins])
    if not reading or not reading["pads"]:
        if reading and reading.get("failed"):
            why = tx("chiamata non riuscita: {exc}", exc=reading["failed"])
        elif reading and reading.get("discarded"):
            why = tx("risposta scartata: {exc}", exc=reading["discarded"])
        elif reading is None:
            why = tx("lettura non eseguita")
        elif reading.get("problems") or reading.get("malformed"):
            # Chains too broken to place any pad: say why, not just «none read».
            why = "; ".join((list(reading.get("problems") or [])
                             + list(reading.get("malformed") or []))[:6])
            if reading.get("problems"):
                # Chains that contradict themselves: the reading misread the
                # drawing, its hole callouts are not taken either (E8).
                _withdraw_dedicated_holes(data)
        else:
            why = tx("nessun pad letto")
        if reading and reading.get("skipped"):
            why = reading["skipped"]
        why += _unread_main_remark(pins)
        if _plan_prints_body_axes(geometry, factor, candidate):
            # The main answer's own chains print the order of the plan sizes.
            data["_body_axes_printed"] = True
        holes = _HoleVerdicts(check_layout, _body_on_page(geometry, factor, False),
                              views={"main": main_view, "chains": main_view},
                              measure=measure_layout,
                              body_axes_known=_plan_fixes_body_axes(candidate),
                              body_bound=_body_quotes_bound(geometry))
        _unconfirmed(data, pins, [("main", main, {}, False)] + own, holes, factor,
                     progress, tx("lettura dedicata delle posizioni: {why}", why=why), "")
        return
    dedicated = reading["pads"]
    common = sorted(set(main) & set(dedicated))
    tolerance = _reading_tolerance(factor)
    disagreements = _offset_disagreements(main, dedicated, common, tolerance)
    views_differ = (main_view in ("top", "bottom") and reading["view"] in ("top", "bottom")
                    and main_view != reading["view"])
    problems, exchanged = ([], False) if repeated else _chain_contradictions(geometry, reading,
                                                                              factor)
    holes = _HoleVerdicts(check_layout, _body_on_page(geometry, factor, exchanged),
                          views={"main": main_view, "chains": main_view,
                                 "dedicated": reading["view"]},
                          views_differ=views_differ, discarded=[dedicated] if problems else [],
                          measure=measure_layout,
                          # An edge-to-edge chain of the plan view fixes which
                          # body size lies on which axis (as given, or
                          # `exchanged`); without one the witness measures it.
                          body_axes_known=exchanged or _plan_fixes_body_axes(reading, candidate),
                          # Plan sizes printed «max»/«min» only bound the
                          # drawn outline (review G2).
                          body_bound=_body_quotes_bound(geometry))
    # The datums and mounting holes the chain reading placed are in ITS
    # frame: the witness must not match a pad of that layout to them.
    holes.attach(dedicated, reading.get("references"))
    # A printed span of the plan that equals the size the answer gives on its
    # axis proves the ORDER of the two plan sizes: the body reading that
    # follows is compared in it (`apply_dedicated_body`). A reading whose
    # chains contradict the drawing proves nothing; the main answer's own
    # chains were checked when they became the candidate.
    if _plan_prints_body_axes(geometry, factor, None if problems or exchanged else reading,
                              candidate):
        data["_body_axes_printed"] = True
    if problems:
        # The drawing contradicts the transcribed chains: at least one quote
        # was read or attributed wrongly, so the dedicated reading is out;
        # only the drawn holes can still confirm the main answer. Its hole
        # callouts go with it (review E8).
        _withdraw_dedicated_holes(data)
        why = tx("le quote del disegno non tornano: {problems}. Lettura per catene di quote "
                 "({dedicated_view}): {dedicated}", problems="; ".join(problems[:6]),
                 dedicated_view=reading["view"], dedicated=_pad_summary(dedicated) or tx("nessuno"))
        _unconfirmed(data, pins, [("main", main, {}, False)] + own, holes, factor,
                     progress, why + _unread_main_remark(pins), "")
        return
    remarks = []
    if repeated:
        remarks.append(tx("catene non verificate: piu' pad con lo stesso numero ({numbers})",
                          numbers=", ".join(sorted(repeated))))
    if exchanged:
        remarks.append(_exchanged_body_remark(reading))
    remarks.extend(reading.get("malformed") or [])
    if reading.get("references"):
        remarks.append(tx("riferimenti e fori di fissaggio trascritti, non pad: {labels}",
                          labels=", ".join(sorted(reading["references"]))))
    remark = (" " + tx("Nota sulla lettura per catene: {remarks}.", remarks="; ".join(remarks[:6]))
              if remarks else "")
    if candidate is not None:
        _resolve_chain_candidate(data, pins, main, reading, views_differ, factor, progress,
                                 remark, holes, exchanged)
        return
    readings = [("main", main, {}, False)]
    if not views_differ:
        spans = reading.get("spans") or {}
        readings.append(("dedicated", dedicated, spans, _centred(spans, exchanged)))
    agreeing = (len(common) >= 2 and not disagreements and not views_differ
                and set(main) == set(dedicated))
    if not agreeing and _replace_by_confirmed_reading(data, pins, main, reading, holes, factor,
                                                      progress, remark, views_differ, exchanged):
        return
    unread = _pads_without_positions(geometry)
    if unread:
        # The main answer did not read every position (L3): it is no second
        # reading of the pads it left without, so «fewer than two pads in
        # common» or «pads the main answer lacks» would name the wrong cause.
        # The dedicated reading stands alone: only a witness confirms it.
        _unconfirmed(data, pins, readings, holes, factor, progress, tx(
            "la risposta principale non ha letto le posizioni dei pad {pads}: la lettura dedicata "
            "({read}) resta l'unica lettura, senza una seconda che la confermi",
            pads=", ".join(unread), read=", ".join(sorted(dedicated, key=_pad_number_key))
            or tx("nessuno")), remark)
        return
    if len(common) < 2:
        _unconfirmed(data, pins, readings, holes, factor, progress, tx(
            "la lettura dedicata ha in comune con la risposta principale meno di due pad "
            "numerati ({read})", read=", ".join(sorted(dedicated)) or tx("nessuno")), remark)
        return
    if not disagreements and not views_differ:
        extra = sorted(set(dedicated) - set(main))
        if set(main) <= set(dedicated) and extra:
            # The pads the main answer has agree, but the drawing has more:
            # a grid or a list that leaves out a terminal is not confirmed by
            # the terminals it does have (generality audit, 2026-10-31).
            _unconfirmed(data, pins, readings, holes, factor, progress, tx(
                "la lettura dedicata trova pad che la risposta principale non ha ({numbers})",
                numbers=", ".join(extra)), remark)
            return
        if set(main) <= set(dedicated):
            if same_model:
                # One model read twice: no independent confirmation (H6). The
                # two describe ONE layout, offered to the witnesses as the
                # chain reading (the program's sums, body placed by its edge
                # quotes); without a witness it stays unconfirmed.
                spans = reading.get("spans") or {}
                _unconfirmed(data, pins, [("dedicated", dedicated, spans,
                                           _centred(spans, exchanged))], holes, factor,
                             progress, tx(
                    "le due letture concordano ma vengono dallo stesso modello: valgono come una "
                    "sola lettura, senza conferma indipendente"), remark)
                return
            verdict = holes(main)
            contradiction = _agreement_contradicted(main, holes)
            if contradiction is not None:
                # The drawn holes contradict what both readings say: never
                # written on the strength of their agreement (H6), and a
                # layout the holes confirm is offered, not adopted (user,
                # 2026-11-11).
                _refuse_contradicted_agreement(data, pins, readings, holes, factor, progress,
                                               remark, contradiction, reading.get("references"))
                return
            note = tx("Posizioni dei pad confermate dalla lettura dedicata ({count} pad, "
                      "scostamenti entro {tolerance} mm).", count=len(common),
                      tolerance=tolerance)
            if verdict is not None and verdict.status == "not_confirmed":
                note += " " + tx("Attenzione: {holes}; controllare il disegno.",
                                 holes=_hole_text(verdict))
            # The two readings agree and the drawn outline measured the body
            # the other way round: the sizes follow the drawing before the
            # body is placed from the edge quotes. Done BEFORE the frame
            # check below, which needs the size that really lies on each
            # axis; when that check blocks, the pads (the layout the witness
            # confirmed) stay and so does the measured orientation (D4).
            _align_body_axes(data, holes, main, progress)
            if main_view not in ("top", "bottom") and reading["view"] in ("top", "bottom"):
                # Same positions, and only the dedicated reading says from
                # which side: its view is the one the pads are seen from,
                # said with its source (M8: one reading's claim, the other
                # silent, not two agreeing).
                _adopt_view(data, reading["view"], tx("lettura dedicata, sola a dichiararla"))
                note += " " + tx("Vista dei terminali ({view}) dichiarata dalla sola lettura "
                                 "dedicata.", view=reading["view"])
            spans = reading.get("spans") or {}
            if geometry.get("origin") == "body_center":
                # A frame the main answer declared is kept; the reading's edge
                # quotes only CHECK it (audit M1/M2): one that puts the body
                # elsewhere is a disagreement, never resolved silently.
                clash = _edge_frame_clash(geometry, main, dedicated, spans, factor)
                if clash:
                    _block(data, clash + remark, progress)
                    return
            elif not repeated:
                # The two agree within tolerance: the program's sums of the
                # printed quotes are written (as for the main answer's own
                # chains), so the body is placed axis by axis from the edge
                # quotes or the drawn outline (_write_chain_pads).
                _write_chain_pads(data, pins, {"pads": dedicated, "spans": spans,
                                               "centred": _centred(spans, exchanged)}, factor)
            _settle(data, note + remark, progress)
            return
        _unconfirmed(data, pins, readings, holes, factor, progress, tx(
            "la lettura dedicata conferma solo {read} pad su {total}", read=len(common),
            total=len(main)), remark)
        return
    why = tx("Le due letture delle posizioni dei pad non coincidono{views}. Scostamenti diversi: "
             "{pairs}. Risposta principale ({main_view}): {main}. Lettura dedicata "
             "({dedicated_view}): {dedicated}",
             views=tx(" (e dichiarano viste diverse)") if views_differ else "",
             pairs=_offset_pairs(disagreements) or tx("nessuna"), main_view=main_view or "unknown",
             dedicated_view=reading["view"], main=_pad_summary(main),
             dedicated=_pad_summary(dedicated))
    if _settle_by_holes(data, pins, readings, holes, factor, progress, why, remark):
        return
    # With different declared views the dedicated layout is still a
    # candidate for the user, in its own view.
    offered = readings if not views_differ else readings + [
        ("dedicated", dedicated, reading.get("spans") or {}, False)]
    _block_or_ask(data, pins, offered, holes, factor,
                  why + ". " + tx("Controllo dei fori disegnati: {verdicts}.",
                                  verdicts=_hole_verdicts(readings, holes))
                  + " " + tx("Nessuna delle due e' stata scelta."), progress, remark)


def _unconfirmed(data, pins, readings, holes, factor, progress, why, remark) -> None:
    """Positions no second reading confirms: the drawn holes decide, the user is asked, or the generation is blocked."""
    if _settle_by_holes(data, pins, readings, holes, factor, progress, why, remark):
        return
    own = (data["_pin_map_conflict"] + " ") if data.get("_chain_candidate") else ""
    _block_or_ask(data, pins, readings, holes, factor,
                  own + tx("Posizioni dei pad NON confermate ({why}). Controllo dei fori "
                           "disegnati: {verdicts}.", why=why,
                           verdicts=_hole_verdicts(readings, holes)), progress, remark)


#: Per-pad attributes a replaced pad list inherits from the main answer, only
#: when every pad of the main answer has the same value; a difference blocks
#: (which pad gets which value is unknown). The terminal and a printed slot
#: side are geometry like the hole (review E4, 2026-11-11: they were lost).
_UNIFORM_PAD_FIELDS = ("pad_type", "drill_mm", "drill_length_mm", "pad_length_mm", "pad_width_mm",
                       "shape", "lead")

#: Per-pad attributes inherited only when the same on every pad, whose
#: difference blocks nothing: the printed hole text (one hole may be spelt
#: two ways). The pad's own `value_sources` are merged word by word instead
#: (`_uniform_pad_fields`).
_UNIFORM_PAD_TEXT_FIELDS = ("hole_callout",)


def _pad_number_text(value) -> str:
    """@brief A pad number as stripped text; a missing or null number is the empty text.

    @param value The ``number`` of a pad entry, as the model gave it.
    @return ``""`` for None (M6: ``str(None)`` named a pad «None», which then
        passed the validation as a real pad number), else ``str(value).strip()``.
        The validation rejects the empty number.
    """
    return "" if value is None else str(value).strip()


def _normalise_pin_numbers(pins) -> None:
    """Rewrite in place every pin ``number`` as stripped text (``_pad_number_text``).

    Pads, readings and witnesses are keyed by text; a number the model sent
    as an integer would otherwise miss its key. A pin without a ``number``
    key is left alone so the validation still reports it.
    """
    for pin in pins:
        if isinstance(pin, dict) and "number" in pin:
            pin["number"] = _pad_number_text(pin["number"])


def _pad_number_key(number: str):
    """Natural order of pad numbers: 1, 2, 10, then A1, A2 ('1' < '10', digits first)."""
    parts = re.findall(r"\d+|\D+", number)
    # `isascii()` first: `str.isdigit()` is also true for «²» or «①», which
    # `\d` does not match and `int()` refuses (review 2026-11-11, F9-3).
    return [(0, int(part), "") if part.isascii() and part.isdigit() else (1, 0, part) for part in parts]


def _uniform_pad_fields(pins) -> tuple:
    """The attributes of _UNIFORM_PAD_FIELDS shared by every pin, and the names that differ.

    @return ({field: value} for the uniform fields that have a value, plus
        the _UNIFORM_PAD_TEXT_FIELDS that are the same on every pin and a
        `value_sources` merged word by word -- the word every pin declares
        for a key, "estimated" when the pins disagree or some declare none
        (the replaced pads cannot say which pad the stronger word belonged
        to, review E4); [field of _UNIFORM_PAD_FIELDS] whose values differ).
    """
    import json

    def identity(value):
        """A hashable stand-in of a value: numbers to 1e-6, mappings by their sorted JSON."""
        if isinstance(value, (dict, list, tuple, set)):
            # Containers are unhashable: a list in a scalar field (a broken
            # answer, refused later with a readable error, review F6-8)
            # must not raise TypeError here.
            return json.dumps(value, sort_keys=True, default=str)
        number = _optional_number(value)
        return round(number, 6) if number is not None and not isinstance(value, str) else value

    pin_dicts = [pin if isinstance(pin, dict) else {} for pin in pins]
    uniform, differing = {}, []
    for field in _UNIFORM_PAD_FIELDS + _UNIFORM_PAD_TEXT_FIELDS:
        values = {identity(pin.get(field)): pin.get(field) for pin in pin_dicts}
        if len(values) > 1:
            if field in _UNIFORM_PAD_FIELDS:
                differing.append(field)
        elif values and next(iter(values.values())) is not None:
            uniform[field] = copy.deepcopy(next(iter(values.values())))
    declared = [pin.get("value_sources") if isinstance(pin.get("value_sources"), dict) else {}
                for pin in pin_dicts]
    keys = sorted({key for words in declared for key in words if key in PAD_SOURCE_KEYS})
    if keys:
        merged = {}
        for key in keys:
            words = {words.get(key) for words in declared}
            merged[key] = next(iter(words)) if len(words) == 1 and None not in words else "estimated"
        uniform["value_sources"] = merged
    return uniform, differing


def _replace_by_confirmed_reading(data, pins, main, reading, holes, factor, progress, remark,
                                  views_differ, exchanged) -> bool:
    """Replace the main answer's pad list with a dedicated reading that a geometry witness confirms alone.

    The main answer's pins are the skeleton the other readings are compared
    with by NUMBER; when its numbers carry no information that skeleton can
    only block (user, 2026-10-31, T7N relay: a grid numbered 1..5 by
    convention, the printed names 11, 12, 14, A1, A2 read by the dedicated
    call and confirmed by the drawn holes, generation blocked).
    Eligible only when:
    - the main numbering is a declared convention (`_numbers_by_convention`,
      no printed number read); numbers READ on both sides stay a block, the
      witness sees geometry, not numbering (generality audit S1) -- UNLESS
      the main answer has FEWER pads than the dedicated reading (user,
      2026-11-03, T7N relay: 4 pads read as 1/11/12/14 against 11/12/14/A1/A2
      confirmed by the drawn holes): an answer that leaves a terminal out
      cannot carry the component's numbering, so the witness is not choosing
      between two complete numberings but between a complete layout and an
      incomplete one. FRAGILE SEPARATION, declared (generality audit
      2026-11-03): the witness accepts extra circles, so it cannot tell a
      fifth terminal from a mounting hole or a variant-only pin the reading
      listed as a pad; the pad-reading prompt asks for numbered terminals
      only, and the note says the extra pad is unverified as a terminal. A
      declared stop kind (terminal/hole) in the chain schema would make it
      robust;
    - the dedicated reading has at least as many pads as the answer has
      positions (fewer would drop a terminal: audit H1);
    - the declared views do not differ and the dedicated reading placed
      every pad it names (nothing in "unplaced");
    - _pick_by_holes chooses the dedicated reading against the main one:
      it alone is confirmed, the main layout is a «no_match», its mirror
      image does not fit as well (D-A, side) -- never a measured layout.
      Exception: convention numbering on the SAME point set as the
      dedicated reading (_same_point_set) is one layout, not two: only the
      dedicated reading is put to _pick_by_holes.
    The confirmed reading gives numbers and positions; pad type, drill, pad
    size and shape come from the main answer only when uniform across its
    pads (otherwise the generation is blocked: which pad gets which values
    is unknown). Pin names follow their numbers: a name read for a number
    the reading has is kept, the others are dropped.
    @return True when handled (replaced, or blocked here), False when not
        eligible or not confirmed: the ordinary rules then apply.
    @note The witness checks GEOMETRY: the new numbers are the reading's
        and the note asks to check them on the drawing.
    """
    dedicated = reading["pads"]
    numbers = {_pad_number_text(pin.get("number")) for pin in pins if isinstance(pin, dict)}
    convention = bool(data.get("_numbers_by_convention"))
    if views_differ or reading.get("unplaced") or len(dedicated) < 2:
        return False
    # Only invented numbers are replaced: when both sides READ numbers, the
    # geometry witness would be choosing the numbering scheme, which it
    # cannot see (generality audit S1, 2026-10-31): that stays a block --
    # except against an INCOMPLETE answer (fewer pads: a terminal left out,
    # see the docstring), whose numbering cannot be the complete one.
    incomplete = len(pins) < len(dedicated)
    if not convention and not incomplete:
        return False
    # A reading with fewer pads than the answer has positions leaves a
    # terminal out: the drawn holes accept extra circles, so nothing else
    # would notice (audit H1, the D-B class in reverse).
    if len(pins) > len(dedicated):
        return False
    if convention and main and _same_point_set(main, dedicated):
        # Same positions, numbers only invented by the main answer: the two
        # are not competing layouts, so the witness has to confirm the one
        # shape (mirror guard included) and the numbers come from the
        # reading that read them (the likeliest real case: right grid,
        # convention numbering).
        chosen = _pick_by_holes([("dedicated", dedicated)], holes)
    else:
        chosen = _pick_by_holes([("main", main), ("dedicated", dedicated)] if main
                                else [("dedicated", dedicated)], holes)
    if chosen is None or chosen[0] != "dedicated":
        return False
    verdict = chosen[2]
    why = (tx("era una convenzione, non letta dal disegno") if convention else
           tx("ha meno pad della lettura confermata: le manca un terminale, se il pad in piu' e' "
              "davvero un terminale (il riscontro verifica le posizioni, non la natura di ogni foro)")
           if incomplete else
           tx("non ha almeno due numeri in comune con quella letta"))
    old = ", ".join(sorted(numbers, key=_pad_number_key)) or tx("nessuno")
    new = ", ".join(sorted(dedicated, key=_pad_number_key))
    uniform, differing = _uniform_pad_fields(pins)
    if differing:
        _block(data, tx("La lettura dedicata dei pad ({new}) e' confermata da un riscontro "
                        "geometrico indipendente ({holes}) e la numerazione della risposta "
                        "principale ({old}) {why}, ma i pad della risposta principale non hanno "
                        "attributi uniformi ({fields}): non si sa quali valori dare ai pad letti.",
                        new=new, holes=_hole_text(verdict), old=old, why=why,
                        fields=", ".join(differing)) + remark, progress)
        return True
    geometry = data["package_geometry"]
    # Names read beside the old pads stay with the numbers they were read
    # for (user, 2026-11-03): only names of numbers the reading does not
    # have are dropped.
    names = {_pad_number_text(pin.get("number")): pin.get("name") for pin in pins
             if isinstance(pin, dict) and pin.get("name")}
    # A deep copy per pad: the unit conversion scales each pad's `lead` in
    # place, so a mapping shared by the pads would be scaled once per pad.
    replaced = [dict(copy.deepcopy(uniform), number=number,
                     **({"name": names[number]} if number in names else {}))
                for number in sorted(dedicated, key=_pad_number_key)]
    spans = reading.get("spans") or {}
    _align_body_axes(data, holes, dedicated, progress)
    _write_chain_pads(data, replaced, {"pads": dedicated, "spans": spans,
                                       "centred": _centred(spans, exchanged)}, factor)
    geometry["pins"] = replaced
    geometry["needs_review"] = True
    data.pop("_numbers_by_convention", None)
    view = holes.views.get("dedicated") or reading.get("view")
    if view in ("top", "bottom"):
        _adopt_view(data, view, _reading_source("dedicated"))
    else:
        # The replaced list was the main reading's, refuted by the witness:
        # its declared side does not survive it (C3); the side is asked.
        _withdraw_view(data, tx("la vista dichiarata apparteneva all'elenco di pad sostituito"))
    _settle(data, tx("Elenco dei pad sostituito dalla lettura dedicata ({new}): posizioni "
                     "confermate da un riscontro geometrico indipendente ({holes}); la numerazione "
                     "della risposta principale ({old}) {why}. Tipo, foro, piazzola e forma dei pad "
                     "presi dalla risposta principale, uguali per tutti i pad. Il riscontro "
                     "verifica le posizioni, non i numeri: controllare i numeri dei pad sul "
                     "disegno.", new=new, holes=_hole_text(verdict), old=old, why=why)
            + remark, progress)
    return True


def _mirror_equivalent(pads: list, tolerance: float) -> bool:
    """True when a numbered pad layout equals its mirror image up to a rigid motion.

    @param pads [(number, (x, y))]; numbers may repeat (shield tabs).
    @param tolerance Matching distance, in the units of the coordinates.
    @note Numbers count, not only the point set: a mirrored footprint is
        right only if some rotation brings every pad NUMBER back to its
        place. A reflection composed with a rotation is a reflection (a
        glide cannot fix a finite set), so the layout must be invariant under
        a reflection about an axis through its centroid, each pad landing on
        a pad of the same number. With distinct numbers that means all pads
        on one line; a symmetric point set with distinct numbers (DIP, the
        T7N relay) is NOT equivalent: its mirror swaps pins.
    """
    if len(pads) < 2:
        return True
    cx = sum(p[0] for _n, p in pads) / len(pads)
    cy = sum(p[1] for _n, p in pads) / len(pads)
    centred = [(n, (x - cx, y - cy)) for n, (x, y) in pads]
    directions = []
    for index, (number, (x, y)) in enumerate(centred):
        if math.hypot(x, y) > tolerance:
            directions.append((x, y))       # this pad on the axis
        for other, (u, v) in centred[index + 1:]:
            if other == number and math.hypot(u - x, v - y) > tolerance:
                directions.append((-(v - y), u - x))   # these two swapped
    if not directions:
        return True                          # every pad on the centroid
    for dx, dy in directions:
        norm = math.hypot(dx, dy)
        ux, uy = dx / norm, dy / norm
        free = list(centred)
        fits = True
        for number, (x, y) in centred:
            dot = x * ux + y * uy
            rx, ry = 2 * dot * ux - x, 2 * dot * uy - y
            match = next((i for i, (n, (px, py)) in enumerate(free)
                          if n == number and math.hypot(px - rx, py - ry) <= tolerance), None)
            if match is None:
                fits = False
                break
            free.pop(match)
        if fits:
            return True
    return False


def _require_declared_view(data: dict, progress=None) -> None:
    """Ask the side of a single part whose pad view is still unknown when a wrong guess would mirror it.

    Called after the dedicated readings, which may have adopted a view. The
    pads are written as seen from above when the view is unknown (the
    canonicalisation leaves them as read); for a BOTTOM view drawing that is
    a mirrored footprint, silently (user, 2026-10-31, T7N relay: a cropped
    drawing lost its «Bottom view on solder pins» caption). The view cannot
    be deduced from the layout, so it is ASKED as the structured question
    "contacts_view" (top/bottom; review 2026-11-04, M12: one bit the user
    sees in a second was a hard block) -- a saved answer is applied at once
    -- unless the numbered layout is mirror-equivalent (_mirror_equivalent),
    in which case both views give the same footprint and a note says so.
    Until the answer the pads stay as read, the view unknown; an existing
    pad block keeps its own reason and the question is recorded beside it.
    """
    if data.get("contacts_view") in ("top", "bottom"):
        return
    geometry = data.get("package_geometry")
    pins = geometry.get("pins") if isinstance(geometry, dict) else None
    if not isinstance(pins, (list, tuple)):
        return
    factor = _declared_unit_factor(data)
    pads = []
    for pin in pins:
        x = _optional_number(pin.get("x_mm")) if isinstance(pin, dict) else None
        y = _optional_number(pin.get("y_mm")) if isinstance(pin, dict) else None
        if x is not None and y is not None:
            scale = factor if factor is not None else 1.0
            pads.append((_pad_number_text(pin.get("number")), (x * scale, y * scale)))
    if len(pads) < 2:
        return
    # Without a declared unit the mm tolerance means nothing: no symmetry
    # shortcut, the view is asked (L11).
    if factor is not None and _mirror_equivalent(pads, PAD_READING_TOLERANCE_MM):
        data["notes"] = (str(data.get("notes") or "") + "\n" + tx(
            "Vista dei terminali non dichiarata: la disposizione numerata dei pad e' uguale alla "
            "sua immagine speculare, il footprint e' lo stesso da sopra e da sotto.")).strip()
        return
    chosen = _ask(data, "contacts_view", tx(
        "Il documento non dice se i pad sono visti dall'alto o dal basso e la loro disposizione "
        "numerata non e' simmetrica: se il disegno e' una vista dal basso il footprint risulta "
        "SPECCHIATO. Da quale lato e' disegnata la disposizione dei pad?"),
        [{"label": tx("Dall'alto (lato componente)"), "value": "top"},
         {"label": tx("Dal basso (lato saldature, bottom view)"), "value": "bottom"}],
        # Keyed on the numbered LAYOUT (signed offsets), not the numbers
        # alone: the same project with another drawing of the same pins must
        # ask again, not reuse a side answered for a different layout
        # (generality audit 2026-11-05, M7).
        _question_evidence(data), key_values=[number for number, _xy in pads]
        + [_layout_digest(dict(pads))],
        # The digests of the layout's two mirror images, so that two saved
        # answers for mirror-image readings can be told consistent (E6).
        key_suffix=_mirror_suffix(dict(pads)))
    if chosen is not None:
        _adopt_view(data, chosen, tx("scelta dell'utente"))
        return
    message = tx("Vista dei terminali non dichiarata e disposizione non simmetrica: il lato e' "
                 "chiesto all'utente; fino alla risposta i pad restano come letti.")
    data["notes"] = (str(data.get("notes") or "") + "\n" + message).strip()
    if progress:
        progress(message)


def _layout_checker(documents, library=None, view_of=None, tolerance_mm=None):
    """The `check_layout` of apply_dedicated_pads: the drawn holes and the library footprints.

    @param documents Drawing images whose holes are found (drawing_holes);
        empty checks no holes.
    @param library LibraryFootprint candidates named by the part number
        (_library_witness), or None/empty.
    @param view_of Zero-argument callable returning the view the pads are
        read in ("top", "bottom", other); the library footprints are top
        views, so they are compared only when it is known.
    @param tolerance_mm Largest pad-centre distance the library witness
        accepts (_reading_tolerance of the drawing's unit); None means
        PAD_READING_TOLERANCE_MM.
    @return None when there is nothing to check with; otherwise a callable
        (pads, body) -> verdict. Two independent geometry witnesses, one
        verdict (generality audit C2, 2026-10-31):
        - one confirms and the other says "no_match": a CONFLICT
          (_WitnessConflict), which neither confirms the layout nor counts
          as "no_match", so _pick_by_holes chooses nothing and the block
          names both;
        - one confirms and the other has no contrary evidence: confirmed;
        - otherwise a drawing that met every pad only by stretching
          («distorted», «body_scale»), which _pick_by_holes reads as «may
          still be right», keeps its verdict: a library "no_match" never
          replaces it; a drawing that could not be checked (unverifiable,
          or no drawing) leaves the library's verdict.
    """
    if not documents and not library:
        return None

    def check(pads, body, references=None):
        holes = None
        if documents:
            from .drawing_holes import check_pad_layout
            holes = check_pad_layout(list(documents), pads, body,
                                     **({"reference_holes_mm": references} if references else {}))
        if not library:
            return holes
        from .library_footprints import check_layout
        witness = check_layout(pads, view_of() if view_of else None, library,
                               tolerance_mm if tolerance_mm is not None
                               else PAD_READING_TOLERANCE_MM)
        verdicts = [v for v in (holes, witness) if v is not None]
        confirming = [v for v in verdicts if _holes_confirm(v)]
        contradicting = [v for v in verdicts if getattr(v, "reason", None) == "no_match"]
        if confirming and contradicting:
            return _WitnessConflict(holes, witness)
        if confirming:
            return confirming[0]
        # A drawing that could not be checked says nothing: the library's
        # verdict stands. One that met every pad only by stretching
        # («distorted», «body_scale») may still be right and is kept.
        silent = holes is None or getattr(holes, "status", None) == "unverifiable"
        return witness if silent and witness is not None else holes
    return check


class _WitnessConflict:
    """The drawn holes and the library footprints disagree on one layout: neither verdict stands."""

    status, reason, max_error_mm, tolerance_mm, circles = ("not_confirmed", "witness_conflict",
                                                           None, None, 0)

    def __init__(self, holes, library):
        """@brief Keep both verdicts for the message.

        @param holes Verdict of the drawn holes.
        @param library Verdict of the library footprints.
        """
        self.holes, self.library = holes, library
        self.image = getattr(holes, "image", None)


def _layout_measurer(documents):
    """The `measure_layout` of apply_dedicated_pads: positions measured on the holes drawn in `documents`."""
    if not documents:
        return None

    def measure(pads, body, references=None):
        from .drawing_holes import measured_layout
        return measured_layout(list(documents), pads, body,
                               **({"reference_holes_mm": references} if references else {}))
    return measure


def _outline_measurer(documents):
    """The outline measure of _write_chain_pads: the body outline drawn around the holes in `documents`."""
    if not documents:
        return None

    def measure(pads, body):
        from .drawing_holes import measured_outline
        return measured_outline(list(documents), pads, body)
    return measure


def _library_witness(part_number, component_family, progress) -> list:
    """The library footprints a single part's number names, read once, with a log line.

    @return LibraryFootprint list; empty for a board, a blank part number,
        no KiCad library, or no footprint carrying one of its series codes.
    """
    wanted = (part_number or "").strip()
    if not wanted or component_family not in SINGLE_COMPONENT_FAMILIES:
        return []
    from .library_footprints import find_footprints, read_footprint
    footprints = [fp for fp in (read_footprint(path) for path in find_footprints(wanted)) if fp]
    if progress:
        progress(tx("Footprint KiCad con la sigla «{part}», usati solo per confermare la geometria "
                    "dei pad: {names}.", part=wanted,
                    names=", ".join(fp.name for fp in footprints) or tx("nessuno")))
    return footprints


def _pad_reading_model(pad_model, main_model: str, component_family, progress,
                       openrouter_check) -> str:
    """The model of the dedicated pad-position reading: `pad_model` when its provider is ready.

    Only a single part has that reading; blank means the main model. A
    provider other than the main model's is checked here (key and credit);
    when it is not ready the main model reads, and the log says why: an
    optional, more capable reader never refuses the run.
    """
    chosen = (pad_model or "").strip()
    if component_family not in SINGLE_COMPONENT_FAMILIES or not chosen or chosen == main_model:
        return main_model
    from .providers import PROVIDER_DEEPSEEK, ProviderError, ensure_providers_ready, provider_of
    from .openrouter import deepseek_declared_text_only
    if provider_of(chosen) == PROVIDER_DEEPSEEK and deepseek_declared_text_only(chosen):
        # The reading is made on drawing images: a text-only reader would be
        # refused at the call and the generation blocked for nothing.
        if progress:
            progress(tx("Modello della lettura delle posizioni dei pad {pad_model} non "
                        "utilizzabile ({exc}): legge il modello principale {model}.",
                        pad_model=chosen, exc=tx("legge solo testo"), model=main_model))
        return main_model
    if provider_of(chosen) != provider_of(main_model):
        try:
            ensure_providers_ready([chosen], progress, openrouter_check=openrouter_check)
        except (OpenRouterError, ProviderError) as exc:
            if progress:
                progress(tx("Modello della lettura delle posizioni dei pad {pad_model} non "
                            "utilizzabile ({exc}): legge il modello principale {model}.",
                            pad_model=chosen, exc=exc, model=main_model))
            return main_model
    return chosen


def _exchanged_body_remark(reading: dict) -> str:
    """The note for body sizes matching the chain spans the other way round (a footprint risk)."""
    spans = reading.get("spans") or {}
    return tx("le quote da bordo a bordo ({sx:.2f} x {sy:.2f} mm) corrispondono a lunghezza e "
              "larghezza del corpo scambiate: controllare l'orientamento del corpo",
              sx=spans.get("x") or 0.0, sy=spans.get("y") or 0.0)


def _span_tolerance(reading: dict, axis: str, factor: float) -> float:
    """Allowed difference, mm, between an edge-to-edge span of `reading` along `axis` and a body size.

    The same rule as _Rounding.tolerance: a printed text tightens the unit's
    rounding, never loosens it (generality audit 2026-11-05, M4); the body
    quote's own rounding is added in both cases.
    """
    quotes = int((reading.get("span_quotes") or {}).get(axis) or 0)
    declared = (reading.get("span_rounding") or {}).get(axis)
    if declared is None:
        return _sum_tolerance(quotes + 1, factor)
    unit = CHAIN_QUOTE_ROUNDING_MM.get(factor, CHAIN_QUOTE_ROUNDING_MM[1.0])
    return _Rounding(declared, True, quotes).tolerance(factor) + unit


def _plan_prints_body_axes(geometry: dict, factor: float, *readings) -> bool:
    """Whether a printed edge-to-edge span of the plan EQUALS the body size the answer gives on that axis, and not the other.

    The printed proof that the two plan sizes are in the right order: a
    nominal body size, a span closed on the body outline along X (or Y)
    within the rounding of its quotes, and the other size too far from that
    span to be meant by it. A size quoted as a maximum or a minimum only
    bounds the span and proves no order; neither does a span that fits both
    sizes (a square body) or none.
    """
    kind = geometry.get("body_quote_kind")
    if kind in BODY_QUOTE_KINDS and kind != BODY_QUOTE_KINDS[0]:
        return False
    sizes = {axis: _optional_number(geometry.get(key), positive=True)
             for axis, key in (("x", "body_length_mm"), ("y", "body_width_mm"))}
    if None in sizes.values():
        return False
    for reading in readings:
        if not isinstance(reading, dict):
            continue
        for axis, other in (("x", "y"), ("y", "x")):
            span = (reading.get("spans") or {}).get(axis)
            if span is None:
                continue
            tolerance = _span_tolerance(reading, axis, factor)
            if (abs(span - sizes[axis] * factor) <= tolerance
                    and abs(span - sizes[other] * factor) > tolerance):
                return True
    return False


def _chain_contradictions(geometry: dict, reading: dict, factor: float) -> tuple:
    """Contradictions of the chain reading with itself and with the body, as messages.

    The edge-to-edge spans (the BODY outline, CHAIN_EDGE: a span closed on
    a named outline, "edge:<label>", is not the body and is never compared)
    are compared with body_length_mm (X) and body_width_mm (Y) of
    `geometry`, in drawing units times `factor`, according to the
    qualifier the drawing prints for them (`body_quote_kind`, review
    2026-11-04, A2): a NOMINAL size must equal the span; a "max" size only
    bounds it from above (the span may be shorter: a housing quoted MAX);
    a "min" size from below. When both axes fail as nominal but match the
    two body sizes exchanged, the body sizes were read in another frame
    and nothing is reported. The tolerance is the rounding budget of the
    quotes summed (`span_rounding` from the printed texts, else the unit's
    fallback `_sum_tolerance`), plus the body quote's own rounding.

    @return (problems, exchanged): `exchanged` True when the spans match the
        body sizes the other way round (the pads must then not be centred on
        those sizes).
    """
    problems = list(reading.get("problems") or [])
    spans = reading.get("spans") or {}
    sizes = {axis: _optional_number(geometry.get(key), positive=True)
             for axis, key in (("x", "body_length_mm"), ("y", "body_width_mm"))}
    kind = geometry.get("body_quote_kind")
    kind = kind if kind in BODY_QUOTE_KINDS else BODY_QUOTE_KINDS[0]

    def tolerance(axis):
        return _span_tolerance(reading, axis, factor)

    def off(axis, size, how=kind):
        span = spans.get(axis)
        if span is None or size is None:
            return False
        difference = span - size * factor
        if how == "max":
            return difference > tolerance(axis)
        if how == "min":
            return difference < -tolerance(axis)
        return abs(difference) > tolerance(axis)

    failing = [axis for axis in ("x", "y") if off(axis, sizes[axis])]
    if (kind == BODY_QUOTE_KINDS[0] and failing == ["x", "y"]
            and not off("x", sizes["y"]) and not off("y", sizes["x"])):
        return problems, True
    for axis in failing:
        problems.append(tx(
            "lungo {axis} le quote da bordo a bordo sommano {span:.2f} mm, ma il corpo "
            "misura {body:.2f} mm{kind}", axis=axis.upper(), span=spans[axis],
            body=sizes[axis] * factor,
            kind=tx(" al massimo") if kind == "max" else tx(" al minimo") if kind == "min" else ""))
    return problems, False


def _numbering_summary(entries) -> str:
    """'1@(0,0), 7@(0,6)' for the log and the conflict message."""
    return ", ".join(f"{e['pin']}@({e['row']},{e['col']})"
                     for e in sorted(entries, key=lambda e: _pin_sort_key(e["pin"])))


def apply_dedicated_numbering(data: dict, reading: Optional[dict], progress=None) -> None:
    """@brief Confront the dedicated numbering with the main answer's pin_map, before canonicalisation.

    @param data Main answer, still in drawing coordinates (`_canonicalize_answer`
        mirrors pin_map afterwards according to contacts_view).
    @param reading Result of `read_pin_numbering`, or None.
    @param progress Optional log callback.
    @note Rules, the same for every part: (a) no reading, or a reading with
        no printed number: the main pin_map stands and a note says the
        numbering was not confirmed; (b) the two readings place every pin
        the same way: confirmed; (c) they differ, or the main answer has no
        pin_map: the DEDICATED reading becomes pin_map (one task, read from
        the drawing alone), its view becomes contacts_view when the main
        answer left it unknown, and `_pin_map_conflict` carries both readings
        so the GUI blocks the automatic generation and shows them; (d) the
        dedicated reading's printed numbers do not fit the grid
        ("contradicts_grid"): `_pin_map_conflict` blocks, the grid is in
        doubt. The
        dedicated reading is compared only on the pins it read: pins of the
        main map that the dedicated call did not see are not a disagreement
        (an empty position cannot be read), they are dropped later by the
        populated_pins rule.
    """
    if reading and reading.get("contradicts_grid"):
        # The drawing's printed numbers do not fit the declared grid: the
        # grid is what is in doubt, so the generation is blocked rather than
        # built on the main answer's numbering (user, 2026-10-31, T7N relay:
        # 5 printed pins on a 2x2 grid were discarded and a 4-pad footprint
        # with invented numbers came out).
        data["_pin_map_conflict"] = tx(
            "I numeri di pin stampati nel disegno non stanno nella griglia {rows}x{cols} della "
            "risposta principale ({why}): la griglia non descrive il componente. Ripeti l'analisi "
            "o descrivi i terminali uno per uno.",
            rows=(reading.get("grid") or (data.get("rows"), data.get("cols")))[0],
            cols=(reading.get("grid") or (data.get("rows"), data.get("cols")))[1],
            why=reading.get("discarded") or "")
        data["_numbering_note"] = data["_pin_map_conflict"]
        if progress:
            progress(data["_pin_map_conflict"])
        return
    if not reading or not reading["pins"]:
        if reading and reading.get("failed"):
            why = tx("chiamata non riuscita: {exc}", exc=reading["failed"])
        elif reading and reading.get("discarded"):
            why = tx("risposta scartata: {exc}", exc=reading["discarded"])
        elif reading is None:
            why = tx("lettura non eseguita")
        else:
            why = tx("nessun numero letto")
        data["_numbering_note"] = tx(
            "Numerazione dei pin NON confermata dalla lettura dedicata ({why}): "
            "verificare la posizione dei pin sul disegno.", why=why)
        if progress:
            progress(data["_numbering_note"])
        return
    # Pins are compared as printed labels («1», «A1»), never as ints: an int
    # conversion would drop or crash on an alphanumeric pad (T7NS5D4-12).
    dedicated = {_pin_text(e["pin"]): (int(e["row"]), int(e["col"])) for e in reading["pins"]}
    main_entries = data.get("pin_map") if isinstance(data.get("pin_map"), (list, tuple)) else []
    main = {}
    for entry in main_entries:
        if isinstance(entry, dict):
            label = _pin_label(entry.get("pin"))
            try:
                position = (int(entry["row"]), int(entry["col"]))
            except (KeyError, TypeError, ValueError):
                continue
            if label is not None:
                main[_pin_text(label)] = position
    main_view = data.get("contacts_view")
    views_differ = (main_view in ("top", "bottom") and reading["view"] in ("top", "bottom")
                    and main_view != reading["view"])
    if main_view not in ("top", "bottom") and reading["view"] in ("top", "bottom"):
        data["contacts_view"] = reading["view"]
    agree = all(main.get(pin) == position for pin, position in dedicated.items())
    if main and agree and not views_differ:
        if set(main) <= set(dedicated):
            note = tx("Numerazione dei pin confermata dalla lettura dedicata ({count} numeri "
                      "letti).", count=len(dedicated))
        else:
            note = tx("Numerazione dei pin confermata solo per {read} pin su {total} (i numeri "
                      "stampati); le altre posizioni seguono la risposta principale, non "
                      "verificate: controllarle sul disegno.", read=len(dedicated), total=len(main))
        data["_numbering_note"] = note
        if progress:
            progress(note)
        return
    data["pin_map"] = list(reading["pins"])
    data["_dedicated_map"] = True
    if views_differ:
        # The dedicated call looked at the drawing alone: its view wins, and
        # the disagreement is said, never silently resolved (review A, 2).
        data["contacts_view"] = reading["view"]
    if main or views_differ:
        data["_pin_map_conflict"] = tx(
            "Le due letture della numerazione non coincidono. Risposta principale ({main_view}): "
            "{main}. Lettura dedicata del disegno ({dedicated_view}, usata): {dedicated}. "
            "Controlla il footprint sul disegno: accettala rigenerando footprint e modello STEP, "
            "oppure ripeti l'analisi.",
            main_view=main_view or "unknown", dedicated_view=reading["view"],
            main=_numbering_summary([{"pin": p, "row": r, "col": c} for p, (r, c) in main.items()])
            or tx("nessuna"),
            dedicated=_numbering_summary(reading["pins"]))
        note = data["_pin_map_conflict"]
    else:
        note = tx("Numerazione dei pin letta dalla chiamata dedicata ({count} numeri): la "
                  "risposta principale non ne dava una.", count=len(dedicated))
    data["_numbering_note"] = note
    if progress:
        progress(note)


#: Component families that are ONE part, not a populated board: they get
#: the dedicated prompt and tool below (user, 2026-10-10).
SINGLE_COMPONENT_FAMILIES = ("single_tht", "single_smd")

#: System prompt of the single-component analysis. Short and about one
#: thing: reading the mechanical drawing of one part. No board photo, no
#: component inventory, no castellated sides; the pads are described one by
#: one, the regular grid is only a declared shortcut.
DEFAULT_SINGLE_SYSTEM_PROMPT = (
    "You are a reader of mechanical drawings of ONE SINGLE electronic component (connector, "
    "relay, transformer, converter, integrated circuit, passive): from the datasheet or from the "
    "dimensioned drawing you derive the dimensions needed for the KiCad footprint and for a "
    "simplified 3D solid. Report the dimensions AS YOU READ THEM: declare in drawing_unit the unit of "
    "the drawing (mm, inch or mil) and in contacts_view from which side the arrangement of the "
    "terminals is seen (top, bottom or unknown); the program does the conversion and the mirroring. "
    "TERMINALS: describe them one by one in pads, with the number PRINTED in the drawing, the "
    "type (thru_hole or smd) and the land (pad_length_mm along X and pad_width_mm along Y; "
    "for a circular land the two values are equal). If the positions of the "
    "terminals are dimensioned with dimension chains, transcribe them in pad_chains AS "
    "THEY ARE PRINTED, without computing: axis x to the right or y downwards, stops in increasing "
    "order on the page (numbers of the terminals on the same dimension line separated by "
    "commas, \"edge\" for the outline of the body only as the first or last stop, "
    "\"edge:<name>\" for another drawn outline, \"ref:<name>\" for a reference point or line (datum) or a "
    "mounting hole), values with the dimensions between consecutive stops (or kind "
    "\"from_first_stop\" if they are ordinate dimensions, all from the first stop), values_text with the "
    "dimensions copied with their decimals, and `at` if a dimension reaches the edge of a land "
    "instead of its centre; the program derives the coordinates by adding them up, so do not skip "
    "and do not merge dimensions. With the chains transcribed you may omit x_mm and y_mm; write them, "
    "relative to an origin you choose and declare in pads_origin, only if the drawing dimensions the "
    "coordinates directly or has no chains. A terminal that on one axis has no dimension but "
    "lies on a CENTRELINE (dash-dot) drawn halfway between two stops must be declared in "
    "pad_midpoints (axis, terminal, the two stops, edge_side if only one is the outline of the "
    "body): the program takes the mean, do not invent the dimension. "
    "Use grid ONLY if ALL the "
    "terminals lie on a grid at constant pitch and you declare it: rows and cols are the "
    "positions of the whole grid, empty ones included, and populated_pins lists the occupied "
    "ones. Distances between terminals that are not multiples of one single pitch mean "
    "that the grid does NOT exist: use pads. NUMBERING: only the numbers printed next to the "
    "terminals; do not extend it by convention (pin header, DIP) and do not invent numbers for "
    "positions without a number. HOLES AND TERMINALS, pad by pad: transcribe only what is "
    "PRINTED. If the drawing prints the hole (a callout such as \"3-Ø1.2\" on the PCB layout) "
    "report it in drill_mm, copy the callout into hole_callout and declare drill_mm \"read\" "
    "in the value_sources of the pad; a slot only if printed: drill_mm is its side along X and "
    "drill_length_mm its side along Y (a horizontal slot has drill_mm greater than "
    "drill_length_mm). If the hole is NOT printed do not estimate it: omit drill_mm and describe in "
    "lead the terminal that enters it (round with diameter_mm, or flat with width_mm and "
    "thickness_mm), declaring its provenance in value_sources.lead; the program derives hole and "
    "land. LAND: pad_length_mm and pad_width_mm must always be filled in; if "
    "the drawing prints the land pattern transcribe them and declare pad_size \"read\", otherwise "
    "write your own estimate declared \"estimated\": for a terminal with a hole the program "
    "replaces it with the land it derives from the hole or from the terminal. "
    "If all the terminals are equal you may also give their diameter in "
    "lead_diameter_mm. In grid the same rule applies: grid.drill_mm only if the hole is "
    "printed, otherwise omit it and give the terminal in lead_diameter_mm; "
    "grid.pad_diameter_mm printed or estimated and declared as such. In lead_exit say "
    "from which face the terminals come out (bottom: straight out of the bottom; side: out of the "
    "flanks and bent; unknown). BODY: body_length_mm and body_width_mm are the two plan dimensions "
    "(from the view from above or from below); if the drawing qualifies them as maximum or minimum "
    "say so in body_quote_kind. "
    + _HEIGHT_RULE + " pin_length_3d_mm is " + _LEAD_RULE + ". For every dimension declare in "
    "value_sources whether you read it (\"read\"), "
    "derived it from read dimensions (\"derived\") or estimated it (\"estimated\"); an estimate must "
    "never look like a reading. If there is a photo of the part, derive from it only body_color_hex, "
    "text_color_hex and edge_fillets. Fill in pinout with the function of every terminal if the "
    "document gives it in a table or shows it in an electrical schematic next to the pinout "
    "(coil, common, NC, NO, anode, cathode...): a function derived from the symbol must be written and "
    "flagged in notes as derived. When the document shows the electrical role of a "
    "terminal, declare it in pinout[].function (coil, common, nc, no; other for the rest) and, "
    "if the relay has several contacts, in pinout[].contact the printed label of the contact: the "
    "program draws coil and contacts from those roles, never from the name of the component. Write in "
    "notes, in English, doubts and inconsistencies. Answer ONLY by calling the tool."
)

#: Digests of former single-component defaults, replaced in the profile
#: database when still stored verbatim (see analysis_profiles).
SUPERSEDED_SINGLE_PROMPT_DIGESTS = frozenset({
    # 2026-10-10, first text: the height was asked from a side view only and
    # a front view with the body height quoted (VE12H-K relay) was omitted.
    "7e842bb65b2a4271b9d2700e72e86680984cec8a478bd2642b7ac831f47f1354",
    # 2026-10-30: the pad coordinates were summed by the model alone; the
    # dimension chains (pad_chains) were not asked, and the VE12H-K relay
    # lost one quote of a chain in both readings.
    "05ee7fa78de8f607612c7dd3bd378461ecfa6f5c2a34802aec5c2405cf214173",
    # 2026-11-03: the height was defined by construction (seating plane to
    # top, leads excluded) and the T7N relay's main answer summed the printed
    # pieces (18.9, 19.3 for a printed 16.9) or omitted it; now ONE printed
    # number, transcribed (_HEIGHT_RULE).
    "15a7c81d5ad16c80bb06b91e5d3310faf40882bdb4308a6fc3182caa7ccb6904",
    # 2026-11-03, same night: the first _HEIGHT_RULE still reached «the PCB
    # plane» (a construction again) and asked the printed quotes in notes;
    # now the printed extent of the body and the printed_vertical_quotes field.
    "3b6dfe4de15b890e19bf13c7b920019f420488f3970460010dfd5943cf4ecf7a",
    # 2026-11-03, same night: pin functions drawn as a circuit symbol beside
    # the pinout (relay coil and contact) were not asked, only a table.
    "c1b754dacbb00291e9f836783780f67e03faddca05f7659a6667bebc716038c0",
    # 2026-11-04: a terminal on a drawn centreline without a quote on that
    # axis (SRD relay, pin 4 half way between the two rows) could not be
    # transcribed: the main model invented its coordinate and the dedicated
    # reading left it out. Now pad_midpoints declares the centreline.
    "d026856a4ad691c28eddabc25feddefde585e17e48196d2336d6614bc1830206",
    # 2026-11-04, same day (never released): _HEIGHT_RULE said «the
    # vertical quote»; a side view drawn with the leads pointing sideways
    # (HLK module: height 18 printed horizontal, plan width 23 vertical)
    # made the reader take the plan width for the height. Now the height
    # is the quote along the direction of the leads.
    "a105917ea0ea1ad524f612244e6274fbb4b848f061940f260f5cc8a3b7867c48",
    # 2026-11-04 evening (review of the single-part path): the height was
    # defined «along the direction of the leads» (wrong for gull-wing SMD),
    # the plan sizes could be «the widths of the front and side views» (the
    # rotated-view error), the lead protrusion had two definitions, the
    # printed quotes carried no role, the chains no kind, no pad-edge anchor,
    # no named outline or datum stop, no printed text, and x_mm/y_mm were
    # required beside the chains. Now every one of these is a declared
    # attribute of the schema.
    "76622f74e9bfaad1d004bace9005009b41d2f63bdcefb527f364088e81155a44",
    # 2026-11-05 (user: «un simbolo più adeguato, mettendo la bobina e i
    # contatti»): the pinout entries declare a closed electrical role
    # (pinout[].function) so the program composes the relay symbol from
    # declared roles, never from the part name or the free-text pin names.
    "4702f97841dd1cf611b58004043f0a1d525599a0c7c13b53090c22a2b698e567",
    # 2026-11-10 (user: «ok procedi», contract A1-A3): the model ESTIMATED the
    # hole and the land from the lead diameter, differently at each run, and
    # the estimate lost its mark on explicit pads. Now it transcribes the
    # printed hole callout and the terminal per pad; the program derives the
    # rest (`_resolve_pad_terminals`).
    "ed685f00b13bfc7e7e6702815de750fbc54117b2202bb28fc560202175a062e3",
    # 2026-11-11 (review round 4, F6-4): the text of 2026-11-10 asked the
    # type as «thru_hole con drill_mm» and then to omit drill_mm, called the
    # land «consigliata» while the program derives it, left a grid no way to
    # omit an unprinted hole (grid.drill_mm was required) and gave a slot no
    # axis. Now one rule for holes, lands and slots, pads and grid alike.
    "a8ec8c4c0e846a73fa8f74f93eb2ac111d0eebb093c5d5d91484013d19a9f5ef",
    # 2026-11-12 (user: every prompt in ONE language, English; the model's
    # free-text answers in English too): the Italian text of 2026-11-11,
    # translated rule by rule.
    "f583c8284cfd15465458c77c2e08677adf9a1e55b930de3b3b36dd0bb43fa0ae",
    # 2026-11-12: the first English translation, before «ref:» stops were
    # worded «a reference point or line (datum)» (a database may store it).
    "222bffb5d87f5bc2aed6d2ddb1fab4bc8f792a7fdb2be115398352a36b16b4da",
})
#: Digests of former pin-numbering defaults.
SUPERSEDED_NUMBERING_PROMPT_DIGESTS = frozenset({
    # 2026-11-12 (user: every prompt in English): the first, Italian text.
    "96eb92c3c0237c138a5a4f64b2784636d338392f1b1d5c2be3861a0d2e26cb25",
})


def _single_component_tool() -> dict:
    """@brief The OpenRouter "function" tool of the single-component analysis.

    @return Tool definition `report_single_component`: the pads one by one as
        the primary form, an optional declared grid, body, lead and view
        fields, pinout, provenance. Built from the main schema's shared
        fragments (edge_fillets, pinout, value_sources) so the three stay one.
    """
    main = _EXTRACTION_TOOL["input_schema"]["properties"]
    pad = {
        "type": "object",
        "properties": {
            "number": {"type": "string", "description": "Number PRINTED next to the terminal."},
            "name": {"type": "string", "description": "Function, if the document gives it."},
            "x_mm": {"type": "number", "description": "Centre, X axis (to the RIGHT in the declared "
                                                      "view), in the unit of drawing_unit; "
                                                      "may be omitted if the terminal is in pad_chains."},
            "y_mm": {"type": "number", "description": "Centre, Y axis (downwards in the declared "
                                                      "view); may be omitted if the terminal is in "
                                                      "pad_chains."},
            "pad_type": {"type": "string", "enum": ["thru_hole", "smd"]},
            "drill_mm": {"type": "number", "exclusiveMinimum": 0,
                         "description": ("Hole, only for thru_hole: the diameter PRINTED on the "
                                         "drawing (hole_callout); for a printed slot its "
                                         "side along X. If the hole is not printed omit "
                                         "this field and give the terminal in lead.")},
            "pad_length_mm": {"type": "number", "exclusiveMinimum": 0,
                              "description": ("Land along X; diameter for a circular "
                                              "land. Printed (land pattern, pad_size "
                                              "\"read\") or an estimate declared estimated "
                                              "in value_sources.pad_size, which for a terminal "
                                              "with a hole the program replaces with the one "
                                              "derived from the hole or from the terminal.")},
            "pad_width_mm": {"type": "number", "exclusiveMinimum": 0,
                             "description": "Land along Y; diameter for a circular land."},
            "shape": {"type": "string", "enum": ["circle", "rect", "oval", "roundrect"]},
            # Terminal, printed hole and per-pad provenance (2026-11-10).
            **_PAD_TERMINAL_PROPERTIES,
        },
        # x_mm/y_mm are optional since 2026-11-04 (review M6): with the chains
        # transcribed the program places the pads; requiring the coordinates
        # forced the model to compute them beside the chains it must not sum.
        "required": ["number", "pad_type", "pad_length_mm", "pad_width_mm"],
    }
    grid = {
        "type": "object",
        "description": (
            "ONLY if all the terminals lie on a grid at constant pitch. rows and cols "
            "are the positions of the whole grid, empty ones included; populated_pins the "
            "occupied positions; pin_map the position of every printed number."),
        "properties": {
            "rows": main["rows"], "cols": main["cols"], "pitch_mm": main["pitch_mm"],
            "row_spacing_mm": main["row_spacing_mm"], "pad_diameter_mm": main["pad_diameter_mm"],
            # The grid follows the pads' rule (review F6-4): a hole is given
            # only when printed, so the field is optional here; without it
            # the program derives hole and land from `lead_diameter_mm`.
            "drill_mm": {"type": "number", "exclusiveMinimum": 0,
                         "description": ("Hole of the terminals of the grid, ONLY if printed on the "
                                         "drawing (declare it \"read\" in value_sources.drill_mm). "
                                         "If it is not printed omit this field and give the "
                                         "terminal in lead_diameter_mm.")},
            "populated_pins": main["populated_pins"],
            "pin_map": main["pin_map"],
            "body_offset_x_mm": main["body_offset_x_mm"],
            "body_offset_y_mm": main["body_offset_y_mm"],
        },
        "required": ["rows", "cols", "pad_diameter_mm"],
    }
    properties = {
        "name": main["name"], "description": main["description"], "tags": main["tags"],
        "drawing_unit": main["drawing_unit"], "contacts_view": main["contacts_view"],
        "pads": {"type": "array", "items": pad,
                 "description": "One element per terminal, in the coordinates declared in pads_origin."},
        "pads_origin": {"type": "string", "enum": ["body_center", "pin1", "custom"],
                        "description": "Origin of the coordinates of pads: centre of the body, "
                                       "terminal 1, or a point of the drawing described in notes."},
        "pad_chains": dict(_PAD_POSITIONS_TOOL["input_schema"]["properties"]["chains"],
                           description=("Dimension chains of the terminals as printed in the drawing, "
                                        "in the unit of drawing_unit: the program adds them up.")),
        "pad_midpoints": _PAD_POSITIONS_TOOL["input_schema"]["properties"]["midpoints"],
        "grid": grid,
        "body_length_mm": main["body_length_mm"], "body_width_mm": main["body_width_mm"],
        "body_height_mm": main["body_height_mm"], "pin_length_3d_mm": main["pin_length_3d_mm"],
        "lead_diameter_mm": main["lead_diameter_mm"], "lead_exit": main["lead_exit"],
        "body_color_hex": main["body_color_hex"], "text_color_hex": main["text_color_hex"],
        "edge_fillets": main["edge_fillets"], "pinout": main["pinout"],
        "body_quote_kind": _BODY_DIMENSIONS_TOOL["input_schema"]["properties"]["body_quote_kind"],
        "leads_point": _LEADS_POINT_SCHEMA,
        "printed_vertical_quotes": _PRINTED_QUOTES_SCHEMA,
        "value_sources": main["value_sources"],
        "confidence": main["confidence"], "notes": main["notes"],
    }
    return {"type": "function", "function": {
        "name": "report_single_component",
        "description": ("Dimensions of a single component for footprint and 3D: terminals one by one "
                        "(pads) or, only if declared, a grid at constant pitch (grid). Free-text "
                        "values (notes) are written in English."),
        "parameters": {"type": "object", "properties": properties,
                       "required": ["drawing_unit", "contacts_view", "confidence",
                                    "printed_vertical_quotes"]}}}


def single_answer_to_data(answer: dict) -> dict:
    """@brief Bring an answer of `report_single_component` to the main answer's shape.

    @param answer The tool arguments as returned by the model.
    @return A dict in the shape of `report_pin_header_params` answers, so
        every step after the call (canonicalisation, validation, dedicated
        numbering, ExtractedParams) stays the same: `pads` becomes
        `package_geometry` (family unknown, origin from pads_origin, the
        declared view as source_view), and so does `grid`, converted into
        the pads it describes (_grid_as_pads): a single part never reaches
        the footprint through an unchecked grid.
    @throws AnswerShapeError When the answer has neither pads nor grid, or
        both, or is not an object: nothing to build a footprint from, and
        the same request may come back well formed.
    @note Through-hole pads with one size are circular: pad_length = pad_width
        = the diameter the model gave, shape circle unless it said otherwise.
    @note An unknown view is written as source_view "top" only as a frame:
        _require_declared_view blocks the generation afterwards when that
        guess would mirror the footprint. A grid without pin_map sets
        `_numbers_by_convention` (its numbers were never read); a grid
        lacking the pitch or row spacing it needs sets `_grid_missing` (the
        field names): its pads then have no coordinate on that axis, and
        `_params_from_tool_arguments` drops them naming the missing value.
    """
    if not isinstance(answer, dict):
        raise AnswerShapeError(tx("Risposta AI non strutturata."), raw=answer)
    pads = answer.get("pads")
    grid = answer.get("grid")
    has_pads = isinstance(pads, (list, tuple)) and len(pads) > 0
    has_grid = isinstance(grid, dict) and bool(grid)
    if not has_pads and not has_grid and any(
            key in answer for key in ("rows", "package_geometry", "castellated_sides")):
        # The main answer's shape, which the model may still produce beside
        # the schema (extra properties are not refused): taken as it is.
        return dict(answer)
    if has_pads == has_grid:
        raise AnswerShapeError(tx(
            "La risposta deve descrivere i terminali in pads OPPURE una griglia in grid "
            "(trovati: pads={pads}, grid={grid}).", pads=has_pads, grid=has_grid), raw=answer)
    data = {"layout": "matrix"}
    for key in ("name", "description", "tags", "drawing_unit", "contacts_view", "body_length_mm",
                "body_width_mm", "body_height_mm", "pin_length_3d_mm", "lead_diameter_mm",
                "lead_exit", "body_color_hex", "text_color_hex", "edge_fillets", "pinout",
                "printed_vertical_quotes", "leads_point", "body_quote_kind",
                "value_sources", "confidence", "notes"):
        if key in answer:
            data[key] = answer[key]
    view = answer.get("contacts_view")
    if has_grid:
        # A declared grid is a claim about the pad positions like any other:
        # it becomes the pads it describes, so the dedicated pad reading and
        # the drawn-hole check confirm it or block it (user, 2026-10-31, T7N
        # relay: a 2x2 grid hid a fifth, off-grid pin and skipped every check).
        pins, origin, note, convention = _grid_as_pads(grid, answer)
        data["notes"] = (str(data.get("notes") or "") + "\n" + note).strip()
        if convention:
            # Numbers the drawing never printed: a confirmed reading of the
            # printed numbers may replace them (_replace_by_confirmed_reading).
            data["_numbers_by_convention"] = True
        missing = _grid_missing_spacings(grid)
        if missing:
            # The pads this spacing would place keep no coordinate; the
            # validation names the missing grid value, not «not read» (L1).
            data["_grid_missing"] = missing
        data["package_geometry"] = {
            "family": "unknown", "origin": origin,
            "source_view": view if view in ("top", "bottom") else "top",
            "confidence": answer.get("confidence", "low"), "needs_review": True, "pins": pins}
        for key in ("body_length_mm", "body_width_mm", "body_height_mm"):
            if answer.get(key) is not None:
                data["package_geometry"][key] = answer[key]
        return data
    unnumbered = [index for index, entry in enumerate(pads, 1)
                  if isinstance(entry, dict) and not _pad_number_text(entry.get("number"))]
    if unnumbered:
        # A pad without its printed number cannot be written (PinSpec refuses
        # the empty number) and must not be numbered by the program: the
        # schema requires the number, so this is a broken shape that earns
        # the one corrective retry, never a validation error after the call.
        raise AnswerShapeError(tx(
            "Pad senza numero stampato nella risposta (elementi {indices} di pads): ogni pad "
            "deve avere il suo numero.", indices=", ".join(str(i) for i in unnumbered)), raw=answer)
    pins = []
    for entry in pads:
        if not isinstance(entry, dict):
            continue
        pin = {"number": _pad_number_text(entry.get("number")), "x_mm": entry.get("x_mm"),
               "y_mm": entry.get("y_mm"), "pad_length_mm": entry.get("pad_length_mm"),
               "pad_width_mm": entry.get("pad_width_mm"),
               "pad_type": entry.get("pad_type", "thru_hole")}
        if entry.get("name"):
            pin["name"] = str(entry["name"])
        if entry.get("drill_mm") is not None:
            pin["drill_mm"] = entry["drill_mm"]
        pin["shape"] = entry.get("shape") or ("circle" if pin["pad_type"] == "thru_hole" else "roundrect")
        # The terminal, the printed hole callout, a printed slot side and the
        # pad's own provenance travel raw (drawing units) to the program's
        # rule, `_resolve_pad_terminals`, which converts and judges them.
        for key in ("lead", "hole_callout", "drill_length_mm", "value_sources"):
            if entry.get(key) is not None:
                pin[key] = entry[key]
        pins.append(pin)
    data["package_geometry"] = {
        "family": "unknown",
        "origin": answer.get("pads_origin") or "custom",
        "source_view": view if view in ("top", "bottom") else "top",
        "confidence": answer.get("confidence", "low"),
        "needs_review": True,
        "pins": pins,
    }
    for key in ("body_length_mm", "body_width_mm", "body_height_mm"):
        if answer.get(key) is not None:
            data["package_geometry"][key] = answer[key]
    if answer.get("body_quote_kind") in BODY_QUOTE_KINDS:
        # The qualifier of the plan sizes travels with them: the chain spans
        # are compared with a «MAX» size as a bound (_chain_contradictions).
        data["package_geometry"]["body_quote_kind"] = answer["body_quote_kind"]
    _place_pins_from_chains(data, answer.get("pad_chains"), answer.get("pad_midpoints"))
    return data


def _grid_as_pads(grid: dict, answer: dict) -> tuple:
    """The pads a declared grid of `report_single_component` describes, in drawing units and view.

    Same contract as the former matrix footprint (_footprint_matrix): column
    `col` at col * pitch along X, row `row` at row * row_spacing along Y,
    numbers from the grid's pin_map as read (drawing view) or column by
    column (a convention, said in the note), filtered by populated_pins. The
    pitch is never reused as the row spacing: a missing value needed by the
    grid leaves the coordinate empty (`_grid_missing_spacings` names it), and
    the validation downstream drops the pads naming that value and blocks
    the generation. A grid is through-hole only, as the matrix was: a missing drill is
    left missing, never turned into SMD pads. When the body offsets and both
    body sizes are given, the pads are written with the body centre as origin.

    @return (pins, origin, note, convention): pin dicts in the shape of the
        explicit pads, "body_center" or "pin1", the note for the result, and
        True when the numbers follow the column-by-column convention instead
        of a pin_map read from the drawing.
    """
    rows = _optional_number(grid.get("rows"), positive=True, integer=True)
    cols = _optional_number(grid.get("cols"), positive=True, integer=True)
    pitch = _optional_number(grid.get("pitch_mm"), positive=True)
    row_spacing = _optional_number(grid.get("row_spacing_mm"), positive=True)
    pad = _optional_number(grid.get("pad_diameter_mm"), positive=True)
    drill = _optional_number(grid.get("drill_mm"), positive=True)
    remarks = []
    positions = []
    mapped = None
    if rows and cols:
        try:
            mapped = _pin_map_seen_from_top(grid.get("pin_map"), cols, "top", rows)
        except ExtractionError as exc:
            remarks.append(tx("pin_map della griglia scartata ({exc}): numerazione colonna per "
                              "colonna, da verificare.", exc=exc))
            mapped = None
        if not mapped:
            remarks.append(tx("Numerazione colonna per colonna per convenzione, non letta dal "
                              "disegno: da verificare."))
        positions = ([(e["pin"], e["row"], e["col"]) for e in mapped] if mapped else
                     [(col * rows + row + 1, row, col) for col in range(cols) for row in range(rows)])
        populated = grid.get("populated_pins")
        if isinstance(populated, (list, tuple)) and populated:
            keep = {str(p).strip() for p in populated}
            positions = [p for p in positions if str(p[0]) in keep]
    off_x = _optional_number(grid.get("body_offset_x_mm"))
    off_y = _optional_number(grid.get("body_offset_y_mm"))
    length = _optional_number(answer.get("body_length_mm"), positive=True)
    width = _optional_number(answer.get("body_width_mm"), positive=True)
    centred = None not in (off_x, off_y, length, width)
    cx, cy = (off_x + length / 2, off_y + width / 2) if centred else (0.0, 0.0)
    pins = []
    for number, row, col in sorted(positions, key=lambda p: (p[2], p[1])):
        # Only a coordinate the grid really needs requires its spacing.
        x = 0.0 if col == 0 else (col * pitch if pitch else None)
        y = 0.0 if row == 0 else (row * row_spacing if row_spacing else None)
        pin = {"number": str(number), "x_mm": None if x is None else x - cx,
               "y_mm": None if y is None else y - cy, "pad_length_mm": pad, "pad_width_mm": pad,
               "pad_type": "thru_hole", "shape": "circle"}
        if drill:
            pin["drill_mm"] = drill
        pins.append(pin)
    note = tx("Griglia dichiarata dal modello ({rows}x{cols}) convertita in {count} pad espliciti, "
              "verificati come i pad letti uno per uno.", rows=rows or "?", cols=cols or "?",
              count=len(pins))
    # The grid's own quotes no longer exist as fields of the result: the ones
    # the model declared not read stay visible here.
    sources = answer.get("value_sources") if isinstance(answer.get("value_sources"), dict) else {}
    guessed = sorted(key for key in ("pitch_mm", "row_spacing_mm", "pad_diameter_mm", "drill_mm",
                                     "body_offset_x_mm", "body_offset_y_mm")
                     if grid.get(key) is not None and sources.get(key) != "read")
    if guessed:
        remarks.append(tx("Quote della griglia non dichiarate lette: {fields}. Verificarle.",
                          fields=", ".join(guessed)))
    if remarks:
        note += " " + " ".join(remarks)
    return pins, ("body_center" if centred else "pin1"), note, bool(pins) and not mapped


def _place_pins_from_chains(data: dict, chains, midpoints=None) -> None:
    """Check the model's own pad coordinates against its dimension chains, summed by the program.

    User, 2026-10-30: the model transcribes the dimension chains beside its
    coordinates (pad_chains) and, since 2026-11-04, the drawn centrelines
    (pad_midpoints); the program adds them up (pads_from_chains).
    The chains count only when they place EVERY pin, contradict nothing and
    no two pins share a number (they would merge into one stop); otherwise
    the model's coordinates stay and a note says why. Then:
    * coordinates and chain sums describe the same layout (signed offsets,
      _offset_disagreements, over the pins that HAVE coordinates): the
      program's sums are written, with the body centre as origin when the
      outline is known on both axes and matches the body sizes (not
      exchanged). Pins the model left WITHOUT coordinates (allowed since
      2026-11-04, review M6: the coordinates are optional beside the chains)
      take the sums too, with needs_review and a note naming them;
    * they differ: NOTHING is chosen (adversarial review: a label swap or a
      reversed chain keeps every sum). The chain sums wait in
      `_chain_candidate`, `_pin_map_conflict` blocks the generation, and the
      dedicated reading, if any, settles it (_resolve_chain_candidate).
    A pin with neither coordinates nor a complete chain placement stays
    without coordinates: unless a dedicated reading places it, the
    validation downstream drops the explicit pads with a note and blocks the
    generation (`_params_from_tool_arguments`, «positions not read»).
    """
    if not isinstance(chains, list) or not chains:
        return
    geometry = data["package_geometry"]
    pins = geometry["pins"]
    factor = _declared_unit_factor(data)
    if factor is None:
        # Chain sums need the unit to become mm: without it the model's own
        # coordinates stay, and the note says why (L11).
        note = tx("Coordinate dei pad prese dalle somme del modello, non dalle catene di quote "
                  "({reasons}).", reasons=_undeclared_unit_reason())
        data["notes"] = (str(data.get("notes") or "") + "\n" + note).strip()
        return
    _normalise_pin_numbers(pins)
    numbers = [pin.get("number") for pin in pins]
    solved = _respell_reading(pads_from_chains(chains, factor, midpoints,
                                               _pad_sizes_mm(pins, factor)), numbers)
    missing = sorted(set(numbers) - set(solved["pads"]))
    # Contradictions between chains and with the body, then unreadable chains.
    problems, exchanged = _chain_contradictions(geometry, solved, factor)
    reasons = problems + list(solved["malformed"])
    if len(set(numbers)) != len(numbers):
        reasons.append(tx("piu' pad con lo stesso numero"))
    if missing:
        reasons.append(tx("terminali non raggiunti dalle catene: {numbers}",
                          numbers=", ".join(missing)))
    before = {}
    for pin in pins:
        x, y = _optional_number(pin.get("x_mm")), _optional_number(pin.get("y_mm"))
        if x is not None and y is not None:
            before[pin["number"]] = (x * factor, y * factor)
    unplaced = sorted(set(numbers) - set(before))
    if unplaced and reasons:
        # The chains cannot fill the coordinates in: the validation
        # downstream drops the pads and names the pins left without.
        reasons.append(tx("terminali senza coordinate: {numbers}", numbers=", ".join(unplaced)))
    if exchanged:
        note = _exchanged_body_remark(solved)
        data["notes"] = (str(data.get("notes") or "") + "\n" + note[:1].upper() + note[1:] + ".").strip()
    if reasons:
        note = tx("Coordinate dei pad prese dalle somme del modello, non dalle catene di quote "
                  "({reasons}).", reasons="; ".join(reasons[:6]))
        data["notes"] = (str(data.get("notes") or "") + "\n" + note).strip()
        return
    spans = solved["spans"]
    candidate = {"pads": solved["pads"], "spans": spans,
                 "centred": spans["x"] is not None and spans["y"] is not None and not exchanged}
    disagreements = _offset_disagreements(before, solved["pads"], sorted(before),
                                          _reading_tolerance(factor))
    if not disagreements:
        _write_chain_pads(data, pins, candidate, factor)
        if unplaced:
            geometry["needs_review"] = True
            note = tx("Coordinate dei terminali {numbers} prese dalle catene di quote sommate dal "
                      "programma (il modello non le ha scritte): da verificare sul disegno.",
                      numbers=", ".join(unplaced))
            data["notes"] = (str(data.get("notes") or "") + "\n" + note).strip()
        return
    data["_chain_candidate"] = candidate
    data["_pin_map_conflict"] = tx(
        "Le coordinate dei pad calcolate dal modello non coincidono con le sue catene di quote "
        "sommate dal programma. Scostamenti diversi: {pairs}. Coordinate del modello: {main}. "
        "Somme delle catene: {chains}. Nessuna delle due e' stata scelta: decide la lettura "
        "dedicata, se concorda con una delle due; altrimenti controlla il disegno.",
        pairs=_offset_pairs(disagreements) or tx("nessuna"), main=_pad_summary(before),
        chains=_pad_summary(solved["pads"]))
    data["_numbering_note"] = data["_pin_map_conflict"]


def apply_answers(params: ExtractedParams, answers: dict) -> ExtractedParams:
    """@brief Apply the user's answers to the structured questions of a result, without a paid call.

    @param params A result of `extract_pin_header_params` (millimetres,
        canonical) carrying `questions` and `candidates`.
    @param answers {question key: chosen option value}; keys that match no
        open question, or values that are not among its options, are ignored.
    @return A deep copy of `params` with every answered question applied and
        removed from `questions` (`candidates` of answered keys dropped):
        - "contacts_view": the side the pads are seen from becomes
          `package_geometry["source_view"]` (the generator mirrors a bottom
          view, exactly as a declared view does: the pins stay as read);
        - "body_height_mm": the chosen height (mm) is written with
          provenance USER_SOURCE in `value_sources`, `body_conflict` cleared,
          `package_geometry["body_height_mm"]` aligned;
        - "pad_layout": the chosen candidate's pins (mm, page frame) replace
          the x/y of the pads of the same number, with its origin and, when
          it declared one, its view; `needs_review` True, `pin_map_conflict`
          cleared, a note names the source. When the pads had been dropped
          for unread positions (`unread_pad_geometry`), they are filled from
          the candidate and become `package_geometry` again only if every pad
          is then placed, the lands the program wrote clear their
          neighbours at the chosen positions and every land holds its hole
          (steps 3 and 4 of `_resolve_pad_terminals`, run again: review
          round 5, V1-4) and the geometry validates (`unread_pads` and
          `unread_pad_geometry` cleared). Otherwise nothing is written, the
          question stays open, and a note and `pin_map_conflict` say why the
          chosen layout could not be applied. A layout that would put the
          pads of a valid footprint on each other is refused the same way;
        - "height_equals_plan": "keep" leaves the height, "clear" removes it
          (a rotated view: the agreed number was a plan size).
        The same semantics apply during an extraction given `answers=`
        (`_ask`), so a saved answer and a fresh one lead to one result.
    """
    result = copy.deepcopy(params)
    if not isinstance(answers, dict):
        return result
    remaining, notes = [], []
    candidates = dict(result.candidates or {})
    for question in result.questions or ():
        key = question.get("key")
        if key not in answers:
            remaining.append(question)
            continue
        allowed = {str(option["value"]): option["value"] for option in question.get("options", ())}
        if str(answers[key]) not in allowed:
            remaining.append(question)
            continue
        value = allowed[str(answers[key])]
        field_name = question.get("field")
        geometry = result.package_geometry if isinstance(result.package_geometry, dict) else None
        if field_name == "contacts_view" and geometry is not None:
            geometry["source_view"] = value
            notes.append(tx("Vista dei terminali ({view}) scelta dall'utente.", view=value))
        elif field_name == "body_height_mm":
            height = _optional_number(value, positive=True)
            if height is None:
                remaining.append(question)
                continue
            result.body_height_mm = height
            if geometry is not None:
                geometry["body_height_mm"] = height
            sources = dict(result.value_sources or {})
            sources["body_height_mm"] = USER_SOURCE
            result.value_sources = sources
            result.body_conflict = None
            notes.append(tx("Altezza del corpo scelta dall'utente fra le quote lette: {value:.2f} mm.",
                            value=height))
        elif field_name == "pad_layout" and (geometry is not None
                                             or isinstance(result.unread_pad_geometry, dict)):
            payload = (candidates.get(key) or {}).get(str(value))
            if not isinstance(payload, dict):
                remaining.append(question)
                continue
            # Pads dropped for unread positions (M3) are filled on a copy:
            # they become the footprint again only when every pad is placed
            # and the geometry validates.
            # Both cases work on a copy: a layout that cannot be applied
            # leaves the result as it was, with the question open.
            restoring = geometry is None
            target = copy.deepcopy(result.unread_pad_geometry if restoring else geometry)

            def not_applicable(reasons) -> None:
                """Say why the chosen layout was not applied, in the notes and in the block (V1-4)."""
                line = tx("Disposizione dei pad scelta non applicabile: {exc}", exc="; ".join(reasons))
                notes.append(line)
                # The block stays, and now tells the truth about this
                # choice too: positions alone do not settle these pads.
                if line not in (result.pin_map_conflict or ""):
                    result.pin_map_conflict = (line if not result.pin_map_conflict
                                               else result.pin_map_conflict + "\n" + line)

            positions = {str(p.get("number")): (p.get("x_mm"), p.get("y_mm"))
                         for p in payload.get("pins") or () if isinstance(p, dict)}
            # A coordinate the candidate estimated from the drawing's pixels
            # keeps that provenance on its pad (`_write_candidate` does the same).
            measured = {str(p.get("number")): p["source"] for p in payload.get("pins") or ()
                        if isinstance(p, dict) and isinstance(p.get("source"), str)}
            for pin in target.get("pins") or ():
                number = _pad_number_text(pin.get("number")) if isinstance(pin, dict) else ""
                if number in positions and None not in positions[number]:
                    pin["x_mm"], pin["y_mm"] = positions[number]
                    if number in measured:
                        pin["source"] = measured[number]
            target["origin"] = payload.get("origin") or "custom"
            target["needs_review"] = True
            if payload.get("view") in ("top", "bottom"):
                target["source_view"] = payload["view"]
            exchange = payload.get("exchange_body")
            exchange = exchange if isinstance(exchange, dict) else None
            if exchange:
                # The witness confirmed the chosen layout only with the body
                # the other way round (`_block_or_ask`): the sizes go with it.
                length, width = target.pop("body_length_mm", None), target.pop("body_width_mm", None)
                if width is not None:
                    target["body_length_mm"] = width
                if length is not None:
                    target["body_width_mm"] = length
            if restoring:
                # A chosen layout gives positions, never a hole (E1): pads
                # dropped for an unread hole stay dropped.
                if _pads_without_positions(target) or _pads_without_holes(target):
                    remaining.append(question)
                    continue
                # The pad rules ran while some pads had no position: the
                # lands the program wrote were never kept apart from them.
                # Steps 3 and 4 of `_resolve_pad_terminals` run again on the
                # placed pads, BEFORE the validation (review round 5, V1-4:
                # a restored footprint with a 0.075 mm gap, or a layout that
                # could never be applied). The invariant holds here too.
                record = target.pop(_DERIVED_LANDS_KEY, None)
                unsettled = target.pop("unsettled_pads", None)
                pins = [pin for pin in target.get("pins") or () if isinstance(pin, dict)]
                if not isinstance(record, list) and unsettled:
                    # A dropped geometry without the record (an older
                    # result): what could not be settled then still cannot.
                    remaining.append(question)
                    continue
                derived = {}
                for entry in record if isinstance(record, list) else ():
                    index = entry.get("index") if isinstance(entry, dict) else None
                    all_pins = target.get("pins") or ()
                    if (isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(all_pins)
                            and isinstance(all_pins[index], dict) and isinstance(entry.get("land"), dict)):
                        derived[id(all_pins[index])] = (all_pins[index], entry["land"])
                found = {"land_capped": [], "land_model": [], "thin_ring": []}
                unfit = _settle_lands_and_fit(pins, derived, found)
                if unfit:
                    not_applicable(_grouped_pad_reasons(
                        [(_pad_number_text(pin.get("number")) or "?", unfit[id(pin)])
                         for pin in pins if id(pin) in unfit]))
                    remaining.append(question)
                    continue
                restored_notes: list = _land_notes(found)
                try:
                    mapping, doubts = _validated_explicit_geometry(target, restored_notes)
                except ExtractionError as exc:
                    not_applicable([str(exc)])
                    remaining.append(question)
                    continue
                result.package_geometry = mapping
                result.geometry_doubts = doubts
                result.unread_pads = None
                result.unread_pad_geometry = None
                notes.extend(restored_notes)
            else:
                # A footprint that validated with the positions it had must
                # validate with the chosen ones too: pads the layout puts on
                # each other are not written (the invariant, V1-4).
                clash = _cross_pad_problem(target)
                if clash:
                    not_applicable([clash])
                    remaining.append(question)
                    continue
                result.package_geometry = geometry = target
            if exchange:
                result.body_length_mm, result.body_width_mm = (result.body_width_mm,
                                                               result.body_length_mm)
                sources = dict(result.value_sources or {})
                length, width = sources.pop("body_length_mm", None), sources.pop("body_width_mm", None)
                if width is not None:
                    sources["body_length_mm"] = width
                if length is not None:
                    sources["body_width_mm"] = length
                result.value_sources = sources
                measured = exchange.get("measured_mm") or (0.0, 0.0)
                notes.append(tx(
                    "Lunghezza e larghezza del corpo scambiate rispetto alla risposta principale: il "
                    "contorno disegnato attorno ai fori misura {x:.2f} mm lungo X e {y:.2f} mm lungo Y "
                    "({holes}). Nessuna quota in pianta fissa l'orientamento del corpo: da verificare "
                    "sul disegno.", x=measured[0], y=measured[1], holes=exchange.get("evidence") or ""))
            result.pin_map_conflict = None
            # What the one writer said while placing the body (`_candidate_payload`).
            notes.extend(str(line) for line in payload.get("notes") or ())
            notes.append(tx("Posizioni dei pad scelte dall'utente fra le letture ({source}); da "
                            "verificare sul disegno.",
                            source=_reading_source(payload.get("source") or "main")))
        elif field_name == "height_equals_plan":
            if value == "clear":
                result.body_height_mm = None
                if geometry is not None:
                    geometry.pop("body_height_mm", None)
                sources = dict(result.value_sources or {})
                sources.pop("body_height_mm", None)
                result.value_sources = sources
                notes.append(tx("Altezza del corpo rimossa: la quota uguale alla pianta era una "
                                "misura in pianta (vista ruotata, scelta dell'utente)."))
            else:
                notes.append(tx("Altezza del corpo confermata dall'utente: sezione quadrata."))
        else:
            remaining.append(question)
            continue
        candidates.pop(key, None)
    result.questions = remaining
    result.candidates = candidates or None
    if notes:
        result.notes = (result.notes + "\n" + "\n".join(notes)).strip()
    return result


def _reread_documents(documents) -> list:
    """The drawing images of a partial re-run as Paths; a PDF cannot be re-read (no enlargement)."""
    paths = [Path(document) for document in documents or ()]
    return [path for path in paths if path.suffix.lower() in _SUPPORTED_SUFFIXES
            and path.suffix.lower() != ".pdf"]


#: Top-level «_» keys of the stored main answer (ExtractedParams.raw_main)
#: that are NOT injected state: `single_answer_to_data` derives them from the
#: model's answer before raw_main is copied (a grid numbered by convention,
#: the spacings a grid lacks), and a partial re-read needs them as the full
#: analysis did. Every other «_» key of a stored answer is dropped (V4-7).
_RAW_MAIN_DERIVED_KEYS = ("_numbers_by_convention", "_grid_missing")

#: Top-level key of the stored main answer (ExtractedParams.raw_main) naming
#: the model that actually gave it (the main step's usable model, 2026-11-12,
#: review finding 1). Program state, never a model field: written after
#: `without_private_keys`, so an answer cannot set it. A partial re-read
#: compares its reader with it and keeps it in the `_raw_main` it carries on
#: (so a re-read of a re-read still knows), but never in its working data.
RAW_MAIN_MODEL_KEY = "_main_model"


def _reader_model(reader) -> str:
    """The model name of a dedicated reader: a name, or a zero-argument resolver called now.

    @param reader A model identifier, a callable returning one, or None.
    @return The identifier, "" when there is none.
    """
    if callable(reader):
        reader = reader()
    return (reader or "").strip() if isinstance(reader, str) else ""


def stored_main_model(stored) -> Optional[str]:
    """@brief The model recorded as the producer of a stored main answer, if any.

    @param stored The stored main answer (`ExtractedParams.raw_main`), any type.
    @return The non-blank model identifier under RAW_MAIN_MODEL_KEY, or None
        for an answer saved before 2026-11-12 (or one whose value is not a
        non-blank string: a hand-edited file is not trusted with a model).
    """
    value = stored.get(RAW_MAIN_MODEL_KEY) if isinstance(stored, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _reread_start(stored: dict, progress) -> dict:
    """A working copy of the stored main answer, sanitised as `_validate_main_response` does.

    The stored answer comes from a project file: it may be one saved before
    the main call dropped the «_» keys of the model's answer (review V3-5),
    or a file edited by hand. Every «_» key is the program's working state,
    so it is dropped at every depth (`without_private_keys`, review V4-7,
    2026-11-11) -- marks of an earlier confrontation included -- except the
    top-level keys `single_answer_to_data` itself derives from the model's
    answer (`_RAW_MAIN_DERIVED_KEYS`), which the stored main shape carries.
    """
    if not isinstance(stored, dict):
        raise ExtractionError(tx("Risposta AI non strutturata."))
    clean = without_private_keys(stored)
    for key in _RAW_MAIN_DERIVED_KEYS:
        if key in stored:
            clean[key] = copy.deepcopy(stored[key])
    data = copy.deepcopy(clean)
    data["_raw_main"] = copy.deepcopy(clean)
    producer = stored_main_model(stored)
    if producer is not None:
        # Kept only in the answer carried on, not in the working data: the
        # re-read result's raw_main still names the model that gave it.
        data["_raw_main"][RAW_MAIN_MODEL_KEY] = producer
    _sanitise_printed_quotes(data, progress)
    for key in ("body_length_mm", "body_width_mm", "body_height_mm"):
        data[key] = _optional_number(data.get(key), positive=True)
    return data


def _reread_reader(field: str, stored: dict, step_models: Optional[Mapping[str, str]],
                   model: str, same_model: bool, progress) -> tuple:
    """The reader of a partial re-read and whether it is the model that gave `stored`.

    The comparison base is the model RECORDED in the stored answer
    (RAW_MAIN_MODEL_KEY) when there is one: since 2026-11-12 the main
    reading may run on its own step model, which the caller (the GUI, with
    the single or session model) does not know (review finding 1). An
    answer saved before has no record: `model` is the base, as before.

    @param field The step of the reading ('pad_positions_prompt' or 'body_dimensions_prompt').
    @param stored The stored main answer the re-read starts from.
    @param step_models The step-model table, or None (the caller's `model`
        reads, as before 2026-11-12).
    @param model The caller's model: the main model when a table is given,
        the reader otherwise; also the last fallback of a reader that cannot run.
    @param same_model The caller's claim, used only without a table AND
        without a recorded producer (nothing better to compare with).
    @param progress Optional log callback (the fallback lines of usable_model).
    @return (reader, same_model): the usable reader and True when it is the
        model that gave `stored` (its reading and the stored one are one witness).
    """
    model = model or DEFAULT_MODEL
    producer = stored_main_model(stored)
    if step_models is None:
        # The caller's model reads; with a record the H6 rule is decided by it.
        return model, (model == producer) if producer is not None else same_model
    base = producer or model
    # The step's own model, or (blank step) the model that gave the answer.
    reader = usable_model(field, step_models, base, progress)
    if reader == base and base != model:
        # The recorded producer was not checked by the caller, who knows only
        # `model`: checked like a step model (key, credit, images), falling
        # back to `model` with the log line when it cannot run now.
        reader = usable_model(field, {field: base}, model, progress)
    return reader, reader == base


def reread_body_height(stored: dict, documents, *, model: str, call, progress=None,
                       answers: Optional[dict] = None, same_model: bool = False,
                       prompts: Optional[dict] = None,
                       step_models: Optional[Mapping[str, str]] = None) -> ExtractedParams:
    """@brief Re-run ONLY the dedicated body-quote reading on a stored main answer (no main paid call).

    @param stored The main answer as kept in the result (`ExtractedParams.raw_main`):
        the model's answer brought to the main shape by `single_answer_to_data`,
        in its declared drawing unit, before any validation.
    @param documents The drawing images (paths or path-like) the body quotes
        are read from; PDFs are skipped (they cannot be enlarged).
    @param model Model of the dedicated reading (the pad/chain reader).
    @param call The logged, cancellable call wrapper (`ai_diagnostics.make_logged_call`).
    @param progress Optional log callback.
    @param answers Saved answers to structured questions, as for
        `extract_pin_header_params(answers=)`.
    @param same_model True when `model` is the model that gave `stored`:
        its list and the stored main list are one witness
        (`apply_dedicated_body(same_model=)`). Used only when `stored`
        records no producer (RAW_MAIN_MODEL_KEY) and no table is given.
    @param prompts The profile's prompt texts ({field: text}, see
        `extract_pin_header_params`); the reading sends
        'body_dimensions_prompt', its default when missing or blank.
    @param step_models The step-model table (`extract_pin_header_params`),
        or None (the behaviour before 2026-11-12: `model` reads and
        `same_model` is the caller's). When given, `model` is the MAIN
        model that gave `stored`: the reading uses the model of the step
        'body_dimensions_prompt' (prompt_models.usable_model, falling back
        to the main model with a log line) and `same_model` is recomputed,
        whatever the caller passed.
    @note The «main model» compared with the reader is the one RECORDED in
        `stored` (RAW_MAIN_MODEL_KEY, the main step's model that actually
        answered) when present, else `model`; a blank step reads with that
        recorded model (`_reread_reader`).
    @return A new ExtractedParams built from the stored answer with the body
        quotes confronted by `apply_dedicated_body` (one paid call, no
        tie-break: the GUI runs this when the user asks a re-reading of the
        height alone), everything else as stored. Its `questions` may hold a
        "body_height_mm" or "height_equals_plan" question.
    @note The result is PARTIAL (`partial_reread`, scope "body_height_mm" of
        REREAD_SCOPES): only the vertical quotes were confronted. Everything
        else is the raw main answer -- pads no reading confirmed, plan sizes
        as the model gave them, no view question -- and must not replace
        what the analysis settled: the caller passes it to `merge_reread`
        with the settled result. Alone it never unblocks the pads: explicit
        pads carry a `pin_map_conflict` saying they were not confronted.
    @throws ExtractionError When the stored answer is not usable, or the
        drawings cannot be read.
    @throws ExtractionCancelled When the user cancels during the call.
    """
    data = _reread_start(stored, progress)
    if isinstance(answers, dict):
        data["_answers"] = dict(answers)
    images = _reread_documents(documents)
    data["_question_documents"] = [path.name for path in images]
    if not images:
        raise ExtractionError(tx("Nessun disegno in immagine da rileggere."))
    # Resolved here, before the images are sized for the reader; the
    # comparison base is the model recorded in `stored` when there is one.
    model, same_model = _reread_reader("body_dimensions_prompt", stored, step_models, model,
                                       same_model, progress)
    if progress:
        progress(tx("Rilettura delle quote del corpo su {names} con {model}.",
                    names=", ".join(path.name for path in images), model=model))
    reading = read_body_dimensions(images, model=model, call=call, progress=progress,
                                   prompt=effective_text("body_dimensions_prompt", prompts))
    before = _note_lines(data.get("notes"))
    apply_dedicated_body(data, reading, progress, tie_break=None, same_model=same_model)
    # The notes of this scope are the ones the body confrontation wrote (E5).
    scope_notes = [line for line in _note_lines(data.get("notes")) if line not in before]
    if _has_explicit_pads(data) and not data.get("_pin_map_conflict"):
        # The pads of the stored answer were NOT confronted by this re-read:
        # alone, this result must not pass them as settled (review
        # 2026-11-10, A5). `merge_reread` keeps the settled layout instead.
        data["_pin_map_conflict"] = tx(
            "Rilettura delle sole quote verticali del corpo: le posizioni dei pad di questo "
            "risultato sono quelle della risposta principale, non riconfrontate. Valgono le "
            "posizioni gia' stabilite dall'analisi.")
        data["_numbering_note"] = data["_pin_map_conflict"]
    _canonicalize_answer(data)
    params = _params_from_tool_arguments(data)
    params.partial_reread = {"field": "body_height_mm", "body_axes_measured": False,
                             "notes": scope_notes}
    return params


def reread_pad_positions(stored: dict, documents, *, model: str, call, progress=None,
                         answers: Optional[dict] = None, library=None,
                         same_model: bool = False,
                         prompts: Optional[dict] = None,
                         step_models: Optional[Mapping[str, str]] = None) -> ExtractedParams:
    """@brief Re-run ONLY the dedicated pad-position reading on a stored main answer (no main paid call).

    @param stored The main answer as kept in the result (`ExtractedParams.raw_main`),
        with its explicit pads (`package_geometry`) in the declared drawing
        unit; its own chain candidate and conflict, if any, are confronted
        again.
    @param documents The drawing images the chains are read from; PDFs skipped.
    @param model Model of the dedicated reading (the pad/chain reader).
    @param call The logged, cancellable call wrapper.
    @param progress Optional log callback.
    @param answers Saved answers to structured questions, as for
        `extract_pin_header_params(answers=)`.
    @param library KiCad library footprints of the part number
        (`_library_witness`), one more geometry witness; None or empty for none.
    @param same_model True when `model` is the model that gave `stored`
        (an agreement is then one reading, see `apply_dedicated_pads`).
        Used only when `stored` records no producer (RAW_MAIN_MODEL_KEY)
        and no table is given.
    @param prompts The profile's prompt texts ({field: text}, see
        `extract_pin_header_params`); the reading sends
        'pad_positions_prompt', its default when missing or blank.
    @param step_models The step-model table (`extract_pin_header_params`),
        or None (the behaviour before 2026-11-12: `model` reads and
        `same_model` is the caller's). When given, `model` is the MAIN
        model that gave `stored`: the reading uses the model of the step
        'pad_positions_prompt' (prompt_models.usable_model, falling back to
        the main model with a log line) and `same_model` is recomputed,
        whatever the caller passed.
    @note The «main model» compared with the reader is the one RECORDED in
        `stored` (RAW_MAIN_MODEL_KEY, the main step's model that actually
        answered) when present, else `model`; a blank step reads with that
        recorded model (`_reread_reader`).
    @return A new ExtractedParams: the pads confronted by `apply_dedicated_pads`
        with the drawn holes as witness, the side asked by
        `_require_declared_view` when still unknown, everything else as
        stored. `questions` may hold "pad_layout" or "contacts_view".
    @note The result is PARTIAL (`partial_reread`, scope "pad_layout" of
        REREAD_SCOPES): the body quotes are the raw main answer's, never
        confronted with a body reading, so the caller passes it to
        `merge_reread` with the settled result. Alone, a height it carries
        is held back by a `body_conflict` saying it was not confronted.
    @throws ExtractionError When the stored answer has no explicit pads or
        is not usable.
    @throws ExtractionCancelled When the user cancels during the call.
    """
    data = _reread_start(stored, progress)
    if not _has_explicit_pads(data):
        raise ExtractionError(tx("La risposta memorizzata non ha pad espliciti da rileggere."))
    if isinstance(answers, dict):
        data["_answers"] = dict(answers)
    images = _reread_documents(documents)
    data["_question_documents"] = [path.name for path in images]
    if not images:
        raise ExtractionError(tx("Nessun disegno in immagine da rileggere."))
    # Resolved here, before the images are sized for the reader; the
    # comparison base is the model recorded in `stored` when there is one.
    model, same_model = _reread_reader("pad_positions_prompt", stored, step_models, model,
                                       same_model, progress)
    if progress:
        progress(tx("Rilettura delle posizioni dei pad su {names} con {model}.",
                    names=", ".join(path.name for path in images), model=model))
    factor = _declared_unit_factor(data)
    reading = read_pad_positions(images, model=model, call=call, progress=progress,
                                 prompt=effective_text("pad_positions_prompt", prompts),
                                 pad_sizes=_pad_sizes_mm(data["package_geometry"].get("pins"), factor))

    def view_of():
        view = data.get("contacts_view")
        return view if view in ("top", "bottom") else (reading or {}).get("view")

    data["_outline_measurer"] = _outline_measurer(images)
    # The stored answer's own notes are outside the scope of this re-read (E5).
    before = _note_lines(data.get("notes"))
    try:
        apply_dedicated_pads(data, reading, progress,
                             check_layout=_layout_checker(images, library, view_of,
                                                          _reading_tolerance(factor)),
                             measure_layout=_layout_measurer(images), same_model=same_model)
    finally:
        data.pop("_outline_measurer", None)
        # No body reading follows a partial re-read: the marks left for it
        # (`_align_body_axes`, `_plan_prints_body_axes`) end here.
        axes_measured = bool(data.pop("_body_axes_measured", False))
        data.pop("_body_axes_printed", None)
    # `axes_measured`: whether the drawn outline measured the order of the two
    # plan sizes this result carries, exchanged or as given (`_align_body_axes`):
    # `merge_reread` orders the settled pair by it.
    _require_declared_view(data, progress)
    if _optional_number(data.get("body_height_mm"), positive=True) is not None:
        # The body quotes of the stored answer were NOT confronted by this
        # re-read (no dedicated body reading, no vote of the printed lists):
        # alone, this result must not pass its height as settled (A5).
        # `merge_reread` keeps the settled body quotes instead.
        data["_body_conflict"] = tx(
            "Rilettura delle sole posizioni dei pad: le quote del corpo di questo risultato sono "
            "quelle della risposta principale, non riconfrontate. Valgono le quote gia' stabilite "
            "dall'analisi.")
    _canonicalize_answer(data)
    params = _params_from_tool_arguments(data)
    # The notes of this scope: everything this re-read and the pad rules
    # wrote, not what the stored answer already said (E5).
    params.partial_reread = {"field": "pad_layout", "body_axes_measured": axes_measured,
                             "notes": [line for line in _note_lines(params.notes)
                                       if line not in before]}
    return params


#: What each partial re-read confronts again, by its field: the attributes of
#: ExtractedParams it owns, the `value_sources` keys that go with them, and
#: the question fields it may ask. Everything outside the scope stays as the
#: analysis settled it (`merge_reread`).
REREAD_SCOPES = {
    "body_height_mm": {"attributes": ("body_height_mm", "pin_length_3d_mm", "body_conflict"),
                       "sources": ("body_height_mm", "pin_length_3d_mm"),
                       "questions": ("body_height_mm", "height_equals_plan")},
    "pad_layout": {"attributes": ("package_geometry", "pin_map_conflict", "geometry_doubts",
                                  "unread_pads", "unread_pad_geometry", "candidates"),
                   "sources": (),
                   "questions": ("pad_layout", "contacts_view")},
}

#: Body quotes a package mapping repeats beside the result's own attributes.
_GEOMETRY_BODY_KEYS = ("body_length_mm", "body_width_mm", "body_height_mm")


def _note_lines(text) -> list:
    """The non-empty stripped lines of a notes text."""
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def merge_reread(settled: Optional[ExtractedParams], reread: ExtractedParams) -> ExtractedParams:
    """@brief Write what a partial re-read confronted again into the result the analysis settled.

    @param settled The extraction as the analysis and the user's answers
        left it (the result the fields were filled from), or None when the
        session has none.
    @param reread The result of `reread_body_height` or `reread_pad_positions`.
    @return A deep copy of `settled` in which ONLY the scope of the re-read
        (REREAD_SCOPES) comes from `reread`:
        - the scope's attributes and the provenance of its values;
        - the questions of the scope's fields (the settled questions of
          other fields stay open, the scope's old ones are replaced);
        - the lines of the re-read's SCOPE notes (`partial_reread["notes"]`:
          what its own confrontation wrote, never the stored answer's notes
          nor a mark about what it did not confront, review E5) the settled
          notes do not have; a partial result without that list (an older
          caller) contributes every line of its notes, as before;
        - the body quotes inside `package_geometry`, kept aligned: a re-read
          height is written into the settled mapping, a re-read mapping
          takes the settled body quotes.
        The pad layout, the view, the conflicts and the plan sizes outside
        the scope are the settled ones, whatever the raw main answer said
        (review 2026-11-10, A5: a height re-read returned the main answer's
        unconfirmed pads, unexchanged body, no conflict and no question).
        `partial_reread` is None in the result. `reread` itself is returned
        when it is not a partial result, or when `settled` is None: its own
        marks then block what it did not confront.
    @note A pad re-read whose witness measured the body's orientation on
        the drawn outline (`partial_reread["body_axes_measured"]`) and found
        the larger plan size on the other axis than the settled result has
        it exchanges the settled pair, with its provenance and a note: the
        measure belongs to the layout that is written.
    @note Scope "body_height_mm" (review round 5, V2-1): the scope is
        replaced only by a re-read that DECIDED something about it -- a
        positive height, or a question of the scope's fields. A re-read
        that returns only a `body_conflict` keeps the settled height, its
        provenance and the settled questions of the scope, and records the
        conflict beside them (`body_conflict` and the notes); one that
        returns nothing of the three changes nothing. A `pin_length_3d_mm`
        the re-read carries is taken in both cases.
    """
    scope = REREAD_SCOPES.get((getattr(reread, "partial_reread", None) or {}).get("field"))
    if scope is None or settled is None:
        return reread
    result = copy.deepcopy(settled)
    if scope is REREAD_SCOPES["body_height_mm"]:
        read_height = reread.body_height_mm is not None
        asks = any(isinstance(question, dict) and question.get("field") in scope["questions"]
                   for question in reread.questions or ())
        if not read_height and not asks:
            # The re-read brought neither a height nor a question of its
            # scope (review round 5, V2-1). It decided nothing, so it takes
            # nothing away: the settled height, its provenance and the
            # settled questions of the scope stay -- the free choice of a
            # height survives a paid call that read none.
            result.partial_reread = None
            if reread.pin_length_3d_mm is not None:
                # The other value of the scope, when the re-read does carry
                # it, is still the re-read's, with its provenance.
                result.pin_length_3d_mm = reread.pin_length_3d_mm
                kept = dict(result.value_sources or {})
                kept.pop("pin_length_3d_mm", None)
                if "pin_length_3d_mm" in (reread.value_sources or {}):
                    kept["pin_length_3d_mm"] = reread.value_sources["pin_length_3d_mm"]
                result.value_sources = kept
            conflict = reread.body_conflict if isinstance(reread.body_conflict, str) else None
            # What the re-read's own confrontation wrote is still recorded
            # (its scope notes only, review E5): a note decides nothing.
            scope_notes = reread.partial_reread.get("notes")
            lines = [str(line) for line in (scope_notes if isinstance(scope_notes, list)
                                            else str(reread.notes or "").splitlines())]
            if conflict and conflict.strip():
                # ONLY a conflict: recorded beside what was settled, in the
                # result's `body_conflict` and in its notes. Without one,
                # no value, provenance, question or conflict changes.
                result.body_conflict = conflict
                lines += conflict.splitlines()
            known = {line.strip() for line in str(settled.notes or "").splitlines()}
            added = [line for line in dict.fromkeys(line.strip() for line in lines)
                     if line and line not in known]
            if added:
                result.notes = (str(result.notes or "") + "\n" + "\n".join(added)).strip()
            return result
    for name in scope["attributes"]:
        setattr(result, name, copy.deepcopy(getattr(reread, name)))
    sources = dict(result.value_sources or {})
    for key in scope["sources"]:
        sources.pop(key, None)
        if key in (reread.value_sources or {}):
            sources[key] = reread.value_sources[key]
    notes = []
    if "package_geometry" in scope["attributes"]:
        measured = [reread.body_length_mm, reread.body_width_mm]
        own = [result.body_length_mm, result.body_width_mm]
        if (reread.partial_reread.get("body_axes_measured") and None not in measured + own
                and (measured[0] - measured[1]) * (own[0] - own[1]) < 0):
            result.body_length_mm, result.body_width_mm = own[1], own[0]
            length, width = sources.pop("body_length_mm", None), sources.pop("body_width_mm", None)
            if width is not None:
                sources["body_length_mm"] = width
            if length is not None:
                sources["body_width_mm"] = length
            notes.append(tx("Lunghezza e larghezza del corpo scambiate: la rilettura dei pad ha "
                            "misurato l'orientamento del corpo sul contorno disegnato."))
        if isinstance(result.package_geometry, dict):
            # The re-read mapping repeats the raw main answer's body quotes.
            for key in _GEOMETRY_BODY_KEYS:
                value = getattr(result, key)
                if value is None:
                    result.package_geometry.pop(key, None)
                else:
                    result.package_geometry[key] = value
    elif isinstance(result.package_geometry, dict):
        if result.body_height_mm is None:
            result.package_geometry.pop("body_height_mm", None)
        else:
            result.package_geometry["body_height_mm"] = result.body_height_mm
    result.value_sources = sources
    result.questions = ([question for question in settled.questions or ()
                         if question.get("field") not in scope["questions"]]
                        + [dict(question) for question in reread.questions or ()
                           if question.get("field") in scope["questions"]])
    known = {line.strip() for line in str(settled.notes or "").splitlines()}
    scope_notes = reread.partial_reread.get("notes")
    lines = (scope_notes if isinstance(scope_notes, list)
             else str(reread.notes or "").splitlines())
    notes += [str(line) for line in lines if str(line).strip() and str(line).strip() not in known]
    if notes:
        result.notes = (str(result.notes or "") + "\n" + "\n".join(notes)).strip()
    result.partial_reread = None
    return result


def _openai_tool() -> dict:
    """The same schema in the "function" format required by OpenRouter.

    The JSON schema of the parameters is identical: only the wrapper changes
    (Anthropic uses ``input_schema``, the OpenAI-compatible API of OpenRouter
    uses ``function.parameters``).
    """
    return {
        "type": "function",
        "function": {
            "name": _EXTRACTION_TOOL["name"],
            "description": _EXTRACTION_TOOL["description"],
            "parameters": _EXTRACTION_TOOL["input_schema"],
        },
    }




_SYSTEM_PROMPT = (
    "You are an assistant that reads datasheets of electronic components, connectors and "
    "modules (PDFs or images of dimensioned mechanical drawings) and extracts the geometric "
    "parameters needed to generate a KiCad footprint: a matrix of through-hole "
    "pins, the sides of a castellated module or the explicit pads in "
    "package_geometry (the only way to describe SMD pads, which have no hole). "
    "Report the dimensions AS YOU READ THEM: declare in drawing_unit the unit of the "
    "drawing (mm, inch or mil) and in contacts_view from which side the drawing of the "
    "contacts is seen (top, bottom or unknown); the program does the conversion and the mirroring. "
    "ALWAYS carefully distinguish pitch_mm (pitch between adjacent pins on the same "
    "row) from row_spacing_mm (perpendicular distance between adjacent pin rows): they are "
    "almost always very different values in DIP-style modules, do not use the "
    "same number for both unless the mechanical drawing confirms it "
    "explicitly. If the pinout shows that only some positions of the matrix have "
    "a physical pin, use populated_pins to list them instead of generating a complete "
    "matrix: rows x cols stay those of the WHOLE grid at constant pitch, "
    "empty positions included (two pins a multiple of the pitch apart are as many "
    "columns further on as there are pitches, not adjacent). For every dimension you fill in declare in value_sources (and, for the "
    "castellated sides, in the value_sources of the side) whether you read it (\"read\"), derived "
    "it only from read dimensions (\"derived\") or estimated it (\"estimated\"). If a needed "
    "value is not present in the documents, estimate it from the other dimensions or from the "
    "design rules of the contact type, declare it \"estimated\" and write in "
    "notes what you derived it from: an estimated value must never look read. "
    "For a single through-hole component report in lead_diameter_mm the diameter "
    "of the terminal if it is dimensioned (it is the pin, not the hole) and in lead_exit from which "
    "face the terminals come out (bottom: straight out of the bottom, side: out of the flanks and bent, "
    "unknown): the program uses the first to check the hole and the second to check that "
    "the pins lie inside the body. "
    "If the datasheet includes a mechanical drawing with the envelope of the body (e.g. an "
    "\"Outline Dimensions\" section), also fill in the body_* fields and pin_length_3d_mm; if there "
    "is no mechanical drawing of the 3D envelope at all, omit all the body_* fields and "
    "pin_length_3d_mm rather than guess the dimensions of the body. "
    "WARNING - common mistake to avoid: body_height_mm (thickness of the case, side "
    "view, Z axis) is NOT the same dimension as row_spacing_mm (distance between the rows "
    "of pins, top view, Y axis), even if in the drawing they may appear close "
    "as numbers: read every dimension from the correct view (top view for row_spacing_mm/"
    "body_width_mm, side view for body_height_mm) instead of reusing a value already read "
    "elsewhere. If you are in doubt about which number belongs to which dimension, look at which view "
    "(top vs side) of the drawing shows it, and write the doubt in the notes "
    "instead of silently guessing. "
    "If among the files there is also a real photo of the product (not a technical drawing) and "
    "it shows rounded/chamfered edges, fill in edge_fillets with the visibly rounded "
    "edges and a radius estimated by eye (product photos NEVER have "
    "a dimensioned radius: it is normal to estimate it visually, numerical certainty is not needed "
    "to fill in this field). Omit edge_fillets only if the case clearly has "
    "sharp 90-degree edges, or if there is no product photo at all -- do not omit it "
    "only because the radius is not explicitly dimensioned. "
    "From the same photo also derive body_color_hex (colour of the case) and "
    "text_color_hex (colour of the silkscreen markings on the case): they give "
    "the 3D model the look of the real component. "
    "FIRST OF ALL choose the arrangement of the contacts (layout field): "
    "\"matrix\" for through-hole pins on a regular grid (pin headers, "
    "DIP-style modules) and for the components described pad by pad in "
    "package_geometry, \"castellated\" for modules on a PCB with plated "
    "half holes on the edge. In castellated modules every side often has a "
    "number of contacts and a pitch different from the others: describe them in castellated_sides, one "
    "element per side, instead of compressing them into a rows x columns matrix that "
    "does not represent them and would produce a wrong footprint. "
    "For every castellated side also give contact_style looking at the edge in the "
    "photo: half_hole if the plated hole is cut by the edge (half-moon open "
    "towards the outside), through_hole if the hole is whole, with a closed ring and intact "
    "board between the ring and the edge, half_and_through if the same contact has "
    "both; omit the field if the edge cannot be seen or the type cannot be told "
    "(the program will ask the user). If dimensioned or visible, "
    "also report per side half_hole_diameter_mm and through_hole_drill_mm. "
    "For modules on a PCB, if there is a photo of the upper side, list in "
    "components_3d ALL the visible components, one by one, including the small "
    "passives (resistors, chip capacitors, diodes): position, envelope (long "
    "axis x short axis), orientation in rotation_deg (0 horizontal, 90 "
    "vertical, read from the photo) and colour estimated by eye in proportion to the known dimensions "
    "of the module. They give the 3D model the look of the real part, and "
    "none of those dimensions is given by the datasheet -- it is normal and expected "
    "to estimate them. There is no expected number of components: list those you see, "
    "without adding or removing any to reach a count. Always also fill in "
    "pinout with the function of every contact, reading it from the silkscreen "
    "labels next to the contacts in the photo or from the labels of "
    "a descriptive image of the product. "
    "Write notes and every other free-text value in English. "
    "Answer ONLY by calling the tool provided."
)

# Public alias: the GUI ("Prompt LLM" tab) shows it as the editable starting
# text, and passes the modified version to extract_pin_header_params().
DEFAULT_SYSTEM_PROMPT = _SYSTEM_PROMPT

#: SHA-256 (hex digest of the UTF-8 text) of every earlier built-in system
#: prompt. A profile database whose global system prompt is still exactly one
#: of them receives the current DEFAULT_SYSTEM_PROMPT (see
#: analysis_profiles.ProfileStore); a prompt the user edited never matches and
#: is kept. Digests rather than texts because each old prompt is several kB;
#: the texts are in the git history of this file. The last two: the prompt
#: that described only a through-hole pin matrix, before `drawing_unit`,
#: `contacts_view` and the SMD route through package_geometry; then the one of
#: commit 8970c7e, which still carried numbers and names of the sample board
#: (a 30-50 component count, its silkscreen names, a 2.54 mm drill/pad
#: "standard") and no per-value provenance (replaced 2026-09-26).
SUPERSEDED_SYSTEM_PROMPT_DIGESTS = frozenset({
    # 2026-10-10: lead_diameter_mm and lead_exit added to the default prompt.
    "1b17e4a73222226e3547123f1e4c7b9ade87a100794ba89b0695ea3642885519",
    "c744e556d5e1eba95f41d69b5eb635c2ab8bcc0cf77f5c1d163b18ca3a41feea",
    "920e38029a2f7b6da293539e9a4ec3ea9b2d8465b51f45a49cea33a008671d57",
    "4d9766aa02d962df5d1da6a726280f44e9404247e23e59f1cc1443bbde17ec82",
    "eefd487b98f9217c71c14976fe71ac0095a14dd8ed9b8bd1b5d58e8231c0aafa",
    "31a4c60ce3dedcce3efe236114fa2b963fcbfed652a40f62cbc6609e5e426715",
    "2e420a121ee16fc0877e6d74ac4efdc7e0731f2e3cc29b4c32c0d014364877d0",
    "d00743ee29a91d396eae57cd56e40dbc2e03825d571960dd09f88ba36d0f754f",
    # 2026-11-12 (user: every prompt in ONE language, English; the model's
    # free-text answers in English too): the Italian text, translated rule by rule.
    "b36d2b0ce7572f3e65c599d2a3336420f773bb444200e914b2a945048b602dbe",
})


@dataclass
class ExtractedParams:
    """@brief Extraction result for matrix and castellated contact layouts.

    `layout` says which fields are meaningful: "matrix" fills in
    rows/columns/pitch/pad/drill, "castellated" fills in `castellated_sides`
    (one element for each side of the module) and leaves the matrix fields
    at None, since those components have none.

    @note Every length is in millimetres and every contact position is seen
        from above, whatever unit and view the model read (see
        `_canonicalize_answer`); conversions and doubts are in `notes`.
    @note When `package_geometry` is set (explicit pads, e.g. an SMD part) it
        is the footprint: the matrix fields are informative and may be None,
        and `drill_mm` is None for pads without hole. Otherwise a "matrix"
        result is a through-hole grid with rows, cols, pad and drill always
        set, `pitch_mm` set when cols > 1 and `row_spacing_mm` when rows > 1
        (None otherwise: a single column or row has no such distance).
        Exception: explicit pads whose positions were not read are dropped
        (see `_params_from_tool_arguments`); the matrix fields then stay
        informative and may be None, `pin_map_conflict` is always set and
        `unread_pads` is not None. A consumer must then write NO footprint
        (neither the explicit pads, which are gone, nor a pin grid from the
        matrix fields, which never were the footprint).
    @note Keys of each `castellated_sides` dict: edge, count, pitch_mm,
        first_pin, contact_style, and optionally reverse, offset_mm,
        pad_length_mm, pad_width_mm, pin_names, half_hole_diameter_mm (plated
        half-hole cut by the edge) and through_hole_drill_mm (drill of the full
        hole), all lengths in mm. `contact_style` is always present: one of
        castellated_module.CONTACT_STYLES when the model observed it, None
        when it did not -- never a default, so the caller must ask the user
        (see `unobserved_contact_style_edges`).
    @note `value_sources` maps every key dimension the result carries to its
        provenance: "read", "derived", "estimated" (VALUE_SOURCES) or
        "undeclared" when the model gave none. Keys are the field names of
        this class (e.g. "body_height_mm") and, for a castellated side,
        "castellated_sides.<edge>.<field>" with the edge seen from above
        (e.g. "castellated_sides.left.half_hole_diameter_mm"). A value the
        program itself estimated (e.g. `fit_body_to_pin_field`) is
        "estimated". Absent keys are values the result does not carry.
    @note `geometry_doubts` lists the structural contradictions of
        `package_geometry` (body covering the lead pads, with `swap_clears`
        when swapping body length and width would resolve it; clockwise lead
        numbering; bottom view also declared mirrored). The geometry is kept
        as read; each doubt's message is also in `notes`.
    """

    name: str
    description: str
    tags: str
    confidence: str
    notes: str
    layout: str = "matrix"
    rows: Optional[int] = None
    cols: Optional[int] = None
    pitch_mm: Optional[float] = None
    row_spacing_mm: Optional[float] = None
    pad_diameter_mm: Optional[float] = None
    drill_mm: Optional[float] = None
    castellated_sides: Optional[Sequence[dict]] = None
    components_3d: Optional[Sequence[dict]] = None
    photo_overlay_png: Optional[bytes] = None
    photo_png: Optional[bytes] = None
    photo_source: str = ""
    populated_pins: Optional[Sequence] = None
    body_length_mm: Optional[float] = None
    body_width_mm: Optional[float] = None
    body_height_mm: Optional[float] = None
    pcb_thickness_mm: Optional[float] = None
    body_offset_x_mm: Optional[float] = None
    body_offset_y_mm: Optional[float] = None
    pin_length_3d_mm: Optional[float] = None
    edge_fillets: Optional[Sequence[dict]] = None
    pinout: Optional[Sequence[dict]] = None
    pin_map: Optional[Sequence[dict]] = None
    body_color_hex: Optional[str] = None
    text_color_hex: Optional[str] = None
    package_geometry: Optional[dict] = None
    value_sources: Optional[dict] = None
    #: Lead diameter read from the drawing (mm), None when not given.
    lead_diameter_mm: Optional[float] = None
    #: Both readings of the pin numbering when they disagree (the dedicated
    #: one is in pin_map); None when confirmed or not checked. The GUI blocks
    #: the automatic generation on it.
    pin_map_conflict: Optional[str] = None
    #: Why no body height could be settled (two printed candidates), None
    #: otherwise; the GUI names it when the STEP model is deferred.
    body_conflict: Optional[str] = None
    #: One of LEAD_EXITS, None when the model did not say.
    lead_exit: Optional[str] = None
    #: Structural doubts of `package_geometry` (core.package_geometry.package_doubts),
    #: each as GeometryDoubt.as_dict(): {"code", "message", "pins", "swap_clears"}.
    #: None when there is no package geometry or nothing is contradicted. The
    #: geometry is never corrected: the same messages are in `notes`, and a
    #: GUI can show `swap_clears` / the code to ask the user what is true.
    geometry_doubts: Optional[Sequence[dict]] = None
    #: Structured questions the extraction could not close by itself, each
    #: {"key": str (question_key), "field": one of QUESTION_FIELDS, "text":
    #: Italian text, "options": [{"label": str, "value": JSON-able}],
    #: "evidence": {"document": file name, "crop": [x0, y0, x1, y1] px or
    #: None} or None}. The GUI asks each one once, saves the answer under its
    #: key and applies it with `apply_answers` (no paid call); a later
    #: analysis given the saved answers (`extract_pin_header_params(answers=)`)
    #: applies them during the extraction and asks nothing with a known key.
    #: Until a question is answered the result holds what its text says:
    #: no height (body_height_mm), the main pads with needs_review
    #: (pad_layout), the pads as read with an unknown view (contacts_view),
    #: the agreed height (height_equals_plan).
    questions: Sequence[dict] = field(default_factory=list)
    #: What `apply_answers` needs to apply a chosen option without a paid call:
    #: {question key: {str(option value): payload}}; payloads are JSON-able.
    candidates: Optional[dict] = None
    #: The main answer as the model gave it, brought to the main shape
    #: (`single_answer_to_data`) and deep-copied before any validation or
    #: dedicated reading: the input of `reread_body_height` and
    #: `reread_pad_positions` (partial re-runs without the main paid call).
    #: None for results built outside `extract_pin_header_params`. Its one
    #: program key, RAW_MAIN_MODEL_KEY, names the model that gave it.
    raw_main: Optional[dict] = None
    #: Not None only for the result of a partial re-read (`reread_body_height`,
    #: `reread_pad_positions`): {"field": key of REREAD_SCOPES,
    #: "body_axes_measured": bool, "notes": [the note lines of its scope]}.
    #: Such a result is NOT a settled extraction:
    #: only the attributes of its scope were confronted again, everything else
    #: is the raw main answer. It is meant for `merge_reread`, which writes
    #: the scope into the settled result; alone, it blocks what it did not
    #: confront.
    partial_reread: Optional[dict] = None
    #: Numbers (text) of the explicit pads that are not a footprint, when
    #: those pads were the footprint and were dropped (`package_geometry` is
    #: then None and `pin_map_conflict` set, naming each reason): pads whose
    #: position was not read, holed pads whose hole was not read, and (round
    #: 4, 2026-11-11) pads whose values are well formed but cannot all be
    #: true -- a land that cannot hold its hole, no room at the pitch,
    #: overlapping copper. Not None means: write no
    #: footprint at all, from no field (`footprint_pads_dropped`). None when
    #: nothing was dropped, and for a castellated module (its sides are the
    #: footprint; dropped optional pads are only noted).
    unread_pads: Optional[Sequence[str]] = None
    #: The dropped explicit geometry (canonical mm, view as read), unread
    #: coordinates still null, every other value validated: what
    #: `apply_answers` fills with a chosen "pad_layout" candidate to restore
    #: `package_geometry`. Never a footprint by itself. Its key
    #: `unsettled_pads` (numbers) is present when a land cannot hold its hole
    #: or a hole has no room at the positions read. Its key `derived_lands`
    #: records the lands the program wrote ({"index": pad index, "land": the
    #: land replaced}): `apply_answers` keeps them apart again once a chosen
    #: layout has placed every pad, and restores the footprint only when
    #: every land then holds its hole and clears its neighbours (V1-4).
    unread_pad_geometry: Optional[dict] = None

    def footprint_pads_dropped(self) -> bool:
        """@brief Tell whether the footprint's explicit pads were dropped for unread positions.

        @return True when `unread_pads` is set: the answer declared explicit
            pads as its footprint but not all their positions, so no footprint
            may be written (not even a pin grid from the matrix fields) until
            the positions are confirmed (re-reading, answered question, new
            analysis).
        """
        return bool(self.unread_pads)

    def values_to_review(self) -> list[str]:
        """@brief Return the keys of `value_sources` whose value was not read nor derived.

        @return Sorted keys whose provenance is "estimated" or "undeclared":
            the values to show as estimates and to confirm before use.
        """
        return sorted(key for key, source in (self.value_sources or {}).items()
                      if source in REVIEW_SOURCES)

    def unobserved_contact_style_edges(self) -> list[str]:
        """@brief Return the castellated edges whose contact style nobody observed.

        @return Edges (seen from above, in side order) whose side has
            `contact_style` None: the style must be asked to the user, never
            assumed. Empty for a non-castellated result.
        """
        return [side["edge"] for side in (self.castellated_sides or ())
                if side.get("contact_style") is None]

    def has_3d_body(self) -> bool:
        """@brief Return whether body length, width and height are all known, so a 3D body can be built.

        @return True when none of the three body dimensions (mm) is None.
        """
        return self.body_length_mm is not None and self.body_width_mm is not None and self.body_height_mm is not None

    def is_castellated(self) -> bool:
        """@brief Return whether the contacts use the castellated (edge-by-edge) layout.

        @return True when `layout` is "castellated".
        """
        return self.layout == "castellated"


class ExtractionError(Exception):
    """@brief Application error of the extraction: missing key, invalid file or unexpected answer.

    @note `raw_response`, when set, is the paid main answer that failed the
        validation, kept so a retry can be compared with it (it is also
        written to the AI log as an AI_RESPONSE_REJECTED line).
    """

    raw_response = None


class ExtractionCancelled(ExtractionError, CallCancelled):
    """@brief The user cancelled the extraction before the next paid call.

    @note Subclass of ExtractionError, so existing handlers still stop the
        run; also a CallCancelled, so no optional step (photo inventory,
        tiles, reviews) downgrades it to a note.
    """


class AnswerShapeError(ExtractionError):
    """@brief The model's tool arguments parsed, but their shape contradicts the tool schema.

    @param message Translated diagnostic text.
    @param raw The arguments as the model sent them, for the AI log.
    @note Kept apart from a wrong VALUE: the shape of one answer can come back
        broken at random (deepseek-flash once flattened one pad to the top
        level, pads="1" beside x_mm/y_mm, on a request answered well twice
        out of three; 2026-10-31), so the same request earns the one paid
        retry given to unreadable arguments. A well-formed answer with wrong
        numbers is not this error and is never retried blindly.
    """

    def __init__(self, message: str, *, raw=None):
        """@brief Build the error, keeping the unusable arguments for the log.

        @param message Translated diagnostic text.
        @param raw The arguments as the model sent them (any JSON value).
        """
        super().__init__(message)
        import json
        try:
            self.raw = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=repr)
        except (TypeError, ValueError):
            self.raw = repr(raw)


#: Text appended to the retried request after an AnswerShapeError: it restates
#: the structural rule of `report_single_component` the answer broke, never a
#: value, so it holds for any component.
_SHAPE_CORRECTION = (
    "CORRECTION: the previous answer did not follow the schema of the tool. Call "
    "the tool again with ALL the fields: pads is a LIST with one object for each terminal "
    "(number, x_mm, y_mm, pad_type, pad_length_mm, pad_width_mm, drill_mm if through-hole), never "
    "the fields of a single pad at the top level; every pad with its PRINTED number (a hole "
    "without a printed number must not be included); grid only if the terminals lie on a grid "
    "at constant pitch, and never together with pads.")


#: Bound of the rejected main answer written to the log, in characters: the
#: tool arguments of one component are a few kB, so this keeps every real
#: answer whole while a runaway one cannot flood the log.
_REJECTED_RESPONSE_MAX_CHARS = 200_000


def _log_rejected_response(data, exc: ExtractionError, progress) -> None:
    """Keep the paid answer that failed validation, so a retry is not blind.

    The answer is attached to the exception and written to the AI log as one
    machine line (AI_RESPONSE_REJECTED ...). It is never cached: an invalid
    answer must not be replayed as a success.
    """
    exc.raw_response = data
    if not progress:
        return
    import json
    try:
        text = json.dumps(data, ensure_ascii=False, sort_keys=True, default=repr)
    except (TypeError, ValueError):
        text = repr(data)
    if len(text) > _REJECTED_RESPONSE_MAX_CHARS:
        text = text[:_REJECTED_RESPONSE_MAX_CHARS] + " …[truncated]"
    try:
        progress(tx("Risposta principale scartata dalla validazione: {exc}. Il testo della "
                    "risposta resta nel log AI (riga AI_RESPONSE_REJECTED) per confrontarlo "
                    "con un nuovo tentativo.", exc=exc))
        progress("AI_RESPONSE_REJECTED " + text)
    except Exception:  # noqa: BLE001 - the log must not hide the real error
        pass


def _log_unreadable_arguments(exc, progress, attempt: int) -> None:
    """Write what the model sent when its tool arguments did not parse or broke the schema shape.

    `exc` is a ToolArgumentsError or an AnswerShapeError; both carry `raw`.

    One machine line in the AI log of the project (AI_RESPONSE_UNREADABLE
    attempt=N ...), next to AI_RESPONSE_REJECTED: before, the text was lost
    with the exception and a JSONDecodeError at «char 1874» could not be
    explained (user's run of 2026-10-02 18:20). Never cached.
    """
    if not progress:
        return
    raw = exc.raw if isinstance(exc.raw, str) else repr(exc.raw)
    if len(raw) > _REJECTED_RESPONSE_MAX_CHARS:
        raw = raw[:_REJECTED_RESPONSE_MAX_CHARS] + " …[truncated]"
    try:
        progress(f"AI_RESPONSE_UNREADABLE attempt={attempt} {raw}")
    except Exception:  # noqa: BLE001 - the log must not hide the real error
        pass


def _guess_media_type(path: Path) -> str:
    media_type, _ = mimetypes.guess_type(path.name)
    if media_type:
        return media_type
    if path.suffix.lower() == ".pdf":
        return "application/pdf"
    return "application/octet-stream"


#: EXIF tag of the orientation; 1 means «stored upright».
_EXIF_ORIENTATION = 0x0112

#: JPEG APP0 identifier kept: the JFIF header only declares density and colour
#: space, never the camera or the place.
_JPEG_JFIF_ID = b"JFIF\x00"
#: JPEG APP14 identifier kept: Adobe's segment tells the decoder how to convert
#: CMYK/YCCK colours, so dropping it would change the pixels.
_JPEG_ADOBE_ID = b"Adobe"

#: PNG chunks that describe pixels and are therefore kept; every other chunk
#: (eXIf, tEXt, zTXt, iTXt, iCCP, tIME, C2PA and any private chunk) is dropped.
#: acTL/fcTL/fdAT are the animation chunks of APNG, cICP/mDCv/cLLi the
#: colour-encoding chunks of the third edition: none carries free text.
_PNG_PIXEL_CHUNKS = frozenset({
    b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB",
    b"sBIT", b"bKGD", b"pHYs", b"hIST", b"acTL", b"fcTL", b"fdAT",
    b"cICP", b"mDCv", b"cLLi",
})

#: WebP chunks that describe pixels and are therefore kept (an allow-list,
#: like PNG): lossy and lossless bitstreams, the extended header, alpha and
#: animation. EXIF, XMP, ICCP, C2PA and any private chunk are dropped.
_WEBP_PIXEL_CHUNKS = frozenset({b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANIM", b"ANMF"})
#: VP8X flag bits announcing the metadata chunks (ICC 0x20, EXIF 0x08, XMP
#: 0x04): always cleared, since those chunks are never kept.
_WEBP_METADATA_FLAGS = 0x20 | 0x08 | 0x04


class _UncleanableImage(Exception):
    """An image that cannot be sent safely: its metadata or orientation cannot be handled.

    Private signal of the privacy boundary; `_file_to_content_block` turns it
    into a translated ExtractionError naming the file.
    """

    def __init__(self, reason: str):
        """@brief Record why the image cannot be sent.

        @param reason "metadata" (no cleaner understands the file) or
            "orientation" (a rotation tag that no decoder can apply).
        """
        super().__init__(reason)
        #: "metadata" (no cleaner understands the file) or "orientation"
        #: (a rotation tag that no decoder can apply).
        self.reason = reason


def _jpeg_without_metadata(data: bytes) -> Optional[bytes]:
    """The JPEG with its metadata segments removed, entropy-coded data untouched.

    Lossless: the compressed pixels are copied byte for byte, so quality and
    size do not change (re-encoding would lose quality at every upload).
    Removed: every APPn segment except the JFIF APP0 and the Adobe APP14
    (EXIF with GPS and device, XMP, ICC profile, IPTC, MPF and maker
    segments), every COM comment, and everything after the end-of-image
    marker (phones append there a second, full-metadata picture or a trailer).

    A file cut short (no end-of-image marker, or a scan truncated by a copy
    that did not finish) is cleaned up to the end of its data and closed with
    an end-of-image marker: the cleaning fails closed, it never falls back to
    the raw bytes, whose header still holds the metadata.

    @param data Encoded bytes.
    @return The cleaned bytes, or None when the data is not a JPEG or ends
        before its first scan (no pixels to send).
    """
    size = len(data)
    if size < 4 or data[:2] != b"\xff\xd8":
        return None
    out = bytearray(b"\xff\xd8")
    index = 2
    scanned = False  # True once compressed pixels have been copied

    def closed() -> Optional[bytes]:
        """The cleaned bytes so far, closed, when they hold pixels."""
        return bytes(out) + b"\xff\xd9" if scanned else None

    while index < size:
        if data[index] != 0xFF:
            return closed()
        while index < size and data[index] == 0xFF:  # fill bytes before a marker
            index += 1
        if index >= size:
            return closed()
        marker = data[index]
        index += 1
        if marker == 0xD9:  # end of image: whatever follows is not this picture
            out += b"\xff\xd9"
            return bytes(out)
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:  # markers without a length
            out += bytes((0xFF, marker))
            continue
        if index + 2 > size:
            return closed()
        length = int.from_bytes(data[index:index + 2], "big")
        if length < 2 or index + length > size:
            return closed()
        body = data[index + 2:index + length]
        segment = data[index - 2:index + length]
        index += length
        if marker == 0xFE:
            continue  # COM: free text
        if 0xE0 <= marker <= 0xEF:
            keep = ((marker == 0xE0 and body.startswith(_JPEG_JFIF_ID))
                    or (marker == 0xEE and body.startswith(_JPEG_ADOBE_ID)))
            if not keep:
                continue
        out += segment
        if marker == 0xDA:
            # Start of scan: entropy-coded data up to the next real marker
            # (0xFF followed by neither a stuffed 0x00, a restart marker nor
            # another fill byte). Progressive files have several scans: the
            # outer loop handles the segments between them.
            start = index
            while True:
                index = data.find(b"\xff", index)
                if index < 0 or index + 1 >= size:
                    # Scan cut short: keep the pixels that are there (a
                    # lone trailing 0xFF is half a marker, not data).
                    end = size - 1 if data.endswith(b"\xff") else size
                    out += data[start:end]
                    scanned = True
                    return closed()
                following = data[index + 1]
                if following == 0x00 or 0xD0 <= following <= 0xD7 or following == 0xFF:
                    index += 1
                    continue
                break
            out += data[start:index]
            scanned = True
    return closed()


def _png_without_metadata(data: bytes) -> Optional[bytes]:
    """The PNG with only the chunks that describe pixels, compressed data untouched.

    @param data Encoded bytes.
    @return The cleaned bytes (chunks of `_PNG_PIXEL_CHUNKS`, up to IEND; any
        trailer after IEND dropped), or None when the data is not a PNG whose
        chunk list ends with IEND (a truncated PNG is then decoded and
        re-encoded, or refused).
    """
    signature = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(signature):
        return None
    out = bytearray(signature)
    index, size = len(signature), len(data)
    while index + 12 <= size:
        length = int.from_bytes(data[index:index + 4], "big")
        kind = data[index + 4:index + 8]
        end = index + 12 + length  # length + type + data + CRC
        if end > size:
            return None
        if kind in _PNG_PIXEL_CHUNKS:
            out += data[index:end]
        index = end
        if kind == b"IEND":
            return bytes(out)
    return None


def _webp_without_metadata(data: bytes) -> Optional[bytes]:
    """The WebP with only the chunks that describe pixels, compressed data untouched.

    @param data Encoded bytes.
    @return The cleaned bytes (chunks of `_WEBP_PIXEL_CHUNKS`) with the RIFF
        size and the VP8X metadata flags updated, or None when the data is
        not a well-formed RIFF/WEBP container holding a bitstream.
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunks = []
    index, size = 12, len(data)
    while index + 8 <= size:
        kind = data[index:index + 4]
        length = int.from_bytes(data[index + 4:index + 8], "little")
        if index + 8 + length > size:
            return None
        end = index + 8 + length + (length & 1)  # chunks are padded to an even size
        if kind in _WEBP_PIXEL_CHUNKS:
            chunk = bytearray(data[index:min(end, size)])
            if len(chunk) & 1:
                chunk += b"\x00"  # a last chunk without its padding byte
            chunks.append(chunk)
        index = end
    if not any(chunk[:4] in (b"VP8 ", b"VP8L", b"ANMF") for chunk in chunks):
        return None
    for chunk in chunks:
        if chunk[:4] == b"VP8X" and len(chunk) > 8:
            chunk[8] &= ~_WEBP_METADATA_FLAGS & 0xFF
    body = b"WEBP" + b"".join(bytes(chunk) for chunk in chunks)
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def _reencoded_without_metadata(data: bytes) -> Optional[bytes]:
    """The image decoded and saved again from its bare pixels, for other formats.

    Used for the formats without a lossless cleaner above (GIF), or for a
    container the walkers reject but PIL can decode. The new image is built
    from the pixel buffer alone, so no `info` entry (comment, XMP, ICC, EXIF)
    can reach the encoder whatever the Pillow version copies by default; only
    the palette and the transparency index, which define pixels, are copied.
    An animated GIF becomes its first frame: the models read a still image.

    @param data Encoded bytes.
    @return The re-encoded bytes in the same format, or None when PIL cannot
        decode the data.
    """
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(data)) as image:
            file_format = image.format or "PNG"
            image.load()
            bare = Image.frombytes(image.mode, image.size, image.tobytes())
            if image.mode == "P" and image.palette is not None:
                bare.putpalette(image.getpalette())
            options = {"quality": 95} if file_format.upper() in ("JPEG", "WEBP") else {}
            if "transparency" in image.info and file_format.upper() in ("GIF", "PNG"):
                options["transparency"] = image.info["transparency"]
            out = BytesIO()
            bare.save(out, format=file_format, **options)
            return out.getvalue()
    except Exception:  # noqa: BLE001 - None tells the caller nothing could be cleaned
        return None


def _image_without_metadata(data: bytes) -> bytes:
    """The image bytes with every metadata block removed, pixels unchanged.

    Privacy boundary of every document upload (audit 2026-10-15, security
    M2): a phone photo carries the GPS position, the device make and model
    and capture times in EXIF/XMP, none of which the model needs to read a
    board. JPEG, PNG and WebP are cleaned losslessly by walking their
    container (`_jpeg_without_metadata`, `_png_without_metadata`,
    `_webp_without_metadata`); any other format, or a container those
    walkers reject, is decoded and re-encoded from its bare pixels.

    @param data Encoded image bytes.
    @return Metadata-free bytes in the same format.
    @throws _UncleanableImage ("metadata") When no cleaner applies and PIL
        cannot decode the data either (an HEIC photo renamed .jpg, a damaged
        file): it FAILS CLOSED, because the raw bytes may still carry the
        metadata (review 2026-10-15, M1). The caller refuses the upload.
    @note The ICC profile is dropped too: it can name the device, and the
        models read photos and drawings without colour management.
    """
    for cleaner in (_jpeg_without_metadata, _png_without_metadata,
                    _webp_without_metadata, _reencoded_without_metadata):
        cleaned = cleaner(data)
        if cleaned is not None:
            return cleaned
    raise _UncleanableImage("metadata")


class _UncleanablePdf(Exception):
    """A PDF that cannot be sent safely: the program cannot rebuild it without its metadata.

    Private signal of the privacy boundary for PDFs, the counterpart of
    `_UncleanableImage`; `_file_to_content_block` turns it into a translated
    ExtractionError naming the file.
    """


def _pdf_without_metadata(data: bytes) -> bytes:
    """The PDF rebuilt without its document metadata, pages and content unchanged.

    Privacy boundary of every PDF upload (review 2026-10-27, E1), the same
    one `_image_without_metadata` enforces for photos: a datasheet saved or
    printed by the user carries, in its Information dictionary (/Author,
    /Creator, /Producer, creation and modification dates) and in its XMP
    /Metadata stream, the user's name, the software and the machine's clock,
    none of which the model needs to read a drawing. Incremental updates may
    also keep an earlier metadata dictionary alive in the file: the output
    is written from scratch, so only the objects reachable from the new
    catalogue survive.

    Two passes are needed, not one: pypdf writes every object it cloned,
    including an XMP stream whose reference was removed, so the first pass
    drops the references and the second clones only what is still reachable.
    The result is then READ BACK and checked, so the boundary never depends
    on the library's internals: a PDF that still carries metadata after the
    rebuild is refused.

    @param data PDF bytes as read from the file.
    @return Metadata-free PDF bytes.
    @throws _UncleanablePdf When pypdf cannot read, decrypt or rewrite the
        file, or when the rebuilt file still carries metadata: it FAILS
        CLOSED, exactly like the image path, because the raw bytes may still
        carry the metadata. The caller refuses the upload.
    @note Attached files, page annotations and form fields are kept: they are
        part of the document the user chose to send, not metadata about it.
    """
    from io import BytesIO
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import NameObject

        def rebuilt(source: bytes) -> bytes:
            reader = PdfReader(BytesIO(source))
            if reader.is_encrypted and not reader.decrypt(""):
                raise _UncleanablePdf("encrypted")
            writer = PdfWriter(clone_from=reader)
            # Information dictionary: None removes /Info from the trailer
            # (pypdf would otherwise add its own /Producer).
            writer.metadata = None
            metadata = NameObject("/Metadata")
            if metadata in writer.root_object:
                del writer.root_object[metadata]
            for page in writer.pages:
                if metadata in page:
                    del page[metadata]
            out = BytesIO()
            writer.write(out)
            return out.getvalue()

        cleaned = rebuilt(rebuilt(data))
        check = PdfReader(BytesIO(cleaned))
        if (check.metadata or check.xmp_metadata is not None
                or any("/Metadata" in page for page in check.pages)):
            raise _UncleanablePdf("metadata")
        if len(check.pages) == 0:
            raise _UncleanablePdf("empty")
    except _UncleanablePdf:
        raise
    except Exception as exc:  # noqa: BLE001 - any failure of the rebuild fails closed
        raise _UncleanablePdf("unreadable") from exc
    return cleaned


def _sniffed_extension(data: bytes) -> Optional[str]:
    """OpenCV file extension of the container, read from its magic bytes."""
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _oriented_with_opencv(data: bytes, orientation: int) -> Optional[bytes]:
    """The image rotated by OpenCV when PIL read the tag but cannot decode the pixels.

    A truncated phone photo still has its header: PIL reads the orientation
    but refuses the cut pixels, while OpenCV decodes what is there. The tag
    is applied here explicitly (decoding with IMREAD_IGNORE_ORIENTATION, so
    no decoder version applies it twice) with the same transforms as
    PIL's `ImageOps.exif_transpose`; OpenCV writes no metadata.

    @param data Encoded bytes (JPEG, PNG or WebP).
    @param orientation EXIF orientation 2..8.
    @return Re-encoded upright bytes in the same container (JPEG/WebP at
        quality 95, as the PIL path), or None when OpenCV cannot decode them.
    """
    try:
        import cv2
        import numpy as np
        extension = _sniffed_extension(data)
        if extension is None:
            return None
        pixels = cv2.imdecode(np.frombuffer(data, np.uint8),
                              cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        if pixels is None:
            return None
        transforms = {
            2: lambda a: a[:, ::-1],                         # mirror left-right
            3: lambda a: a[::-1, ::-1],                      # rotate 180
            4: lambda a: a[::-1, :],                         # mirror top-bottom
            5: lambda a: a.transpose(1, 0, 2),               # main diagonal
            6: lambda a: np.rot90(a, -1),                    # rotate 90 clockwise
            7: lambda a: a[::-1, ::-1].transpose(1, 0, 2),   # anti-diagonal
            8: lambda a: np.rot90(a, 1),                     # rotate 90 counter-clockwise
        }
        upright = np.ascontiguousarray(transforms[orientation](pixels))
        params = ([cv2.IMWRITE_JPEG_QUALITY, 95] if extension == ".jpg"
                  else [cv2.IMWRITE_WEBP_QUALITY, 95] if extension == ".webp" else [])
        ok, encoded = cv2.imencode(extension, upright, params)
        return encoded.tobytes() if ok else None
    except Exception:  # noqa: BLE001 - None: the caller refuses the image
        return None


def _upright_image_bytes(data: bytes) -> bytes:
    """The image as the analysis and the preview see it, EXIF orientation applied, without metadata.

    Phones store the pixels one way and the right way in the orientation tag.
    OpenCV (the analysis) and the preview apply it; a provider may not, and
    the model would then read a rotated photo (user, 2026-09-25). A photo
    with a rotation tag is rotated and re-encoded in the same format; every
    photo, rotated or not, then loses its metadata (`_image_without_metadata`):
    sending an unrotated phone photo byte for byte used to leak its GPS
    position and device (audit 2026-10-15).

    When PIL reads a rotation tag but cannot decode the pixels (a truncated
    file), OpenCV rotates them (`_oriented_with_opencv`); when neither can,
    the image is refused rather than sent with its tag stripped and its
    pixels unrotated (review 2026-10-15, L7): the model would read it sideways
    while the analysis and the preview show it upright.

    @param data Encoded image bytes as read from the document file.
    @return Upright, metadata-free bytes in the same format.
    @throws _UncleanableImage "orientation" when a rotation tag cannot be
        applied; "metadata" when the metadata cannot be removed.
    """
    orientation = 1
    try:
        from io import BytesIO
        from PIL import Image, ImageOps
        with Image.open(BytesIO(data)) as image:
            orientation = image.getexif().get(_EXIF_ORIENTATION, 1)
            if orientation not in (None, 1):
                file_format = image.format or "PNG"
                upright = ImageOps.exif_transpose(image)
                # The transposed copy may still hold EXIF/ICC/XMP in `info`;
                # they are removed below from the encoded result.
                out = BytesIO()
                options = {"quality": 95} if file_format.upper() in ("JPEG", "WEBP") else {}
                upright.save(out, format=file_format, **options)
                data = out.getvalue()
                orientation = 1
    except Exception:  # noqa: BLE001 - handled below with the orientation still pending
        pass
    if orientation in range(2, 9):
        # Cleaned first: the JPEG walker closes a truncated file with its
        # end-of-image marker, which OpenCV needs to decode it at all.
        rotated = _oriented_with_opencv(_image_without_metadata(data), orientation)
        if rotated is None:
            raise _UncleanableImage("orientation")
        data = rotated
    return _image_without_metadata(data)


#: Component family whose contacts are the holes cut by the board edge.
CASTELLATED_FAMILY = "aggregate_castellated"


def _edge_strip_blocks(doc_paths, document_roles) -> list:
    """Enlarged strips of the board edges of each declared photograph, as content blocks.

    The model counts the physical contacts of a castellated module in its
    first answer (user decision 2026-09-28, HANDOFF point 142): each strip
    shows one edge from close up, with the labels printed beside the holes,
    so that holes and labels can be told apart. Cut without the board
    dimensions (edge_strips.board_box); a photo where the board cannot be
    found adds nothing. Never fatal: the strips are help, not a measure.
    """
    import cv2
    import numpy as np
    from .edge_strips import edge_strips
    blocks = []
    for index, path in enumerate(doc_paths):
        if PHOTO_ROLE not in _declared_role_keys(document_roles.get(path.name) or ()):
            continue
        try:
            payload = _upright_image_bytes(Path(path).read_bytes())
            image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
            strips = edge_strips(image) if image is not None else []
        except (OSError, ValueError, cv2.error, _UncleanableImage):
            # An image refused by the privacy boundary gives no strips; the
            # upload of the photo itself reports the reason.
            strips = []
        for edge, png in strips:
            blocks.append({"type": "text", "text": (
                f"FILE {index}, enlargement of the {edge} edge of the board (same view "
                "as the photo, board edge towards the outside of the image): count here the plated "
                "holes cut by the edge, one per contact; the writing next to them is "
                "labels, not contacts.")})
            data_b64 = base64.b64encode(png).decode("ascii")
            blocks.append({"type": "image_url",
                           "image_url": {"url": f"data:image/png;base64,{data_b64}"}})
    return blocks


def _enlarged_image_bytes(data: bytes, model: Optional[str]) -> bytes:
    """The image enlarged to what the receiving model can resolve, when that helps.

    A dimensioned drawing cropped from a datasheet is often a few hundred
    pixels wide; a provider with a coarse visual token and an upsampling
    floor (DeepSeek, measured in HANDOFF 144) then reads a dimension label
    with about one token and the numbers come out different at every call
    (TEL8WI, 2026-10-10: seven analyses, seven readings). The crop path
    already enlarges for such providers (`photo_grounding.fit_to_model`);
    this applies the same declared limits to the documents of the analysis.

    @param data Encoded image bytes, already upright.
    @param model Model identifier; its provider decides the limits. None
        (caller without a model) sends the bytes unchanged.
    @return The same bytes when the provider does not gain from enlargement,
        the image already reaches its limits, decoding fails, or the enlarged
        PNG would exceed MAX_FILE_BYTES; otherwise a lossless PNG enlarged
        (bicubic) to the largest size within the provider's limits.
    @note Enlarging adds no information: it makes the provider spend more
        tokens on the same pixels. Never shrinks: shrinking is the provider's job.
    """
    if not model:
        # Caller without a model: the historical bytes, no assumed provider.
        return data
    from .photo_grounding import model_image_limits
    limits = model_image_limits(model)
    if not limits.enlarges:
        return data
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            if width < 1 or height < 1:
                return data
            scale = min(limits.long_side_px / max(width, height),
                        math.sqrt(limits.max_pixels / (width * height)))
            if scale <= 1.0:
                return data
            # A transparent background (RGBA, LA, palette with transparency:
            # the usual export of a vector drawing) would turn black through
            # convert("RGB"); it is composited on white first (review A, 3).
            if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                image = Image.alpha_composite(Image.new("RGBA", rgba.size, "white"), rgba)
            enlarged = image.convert("RGB").resize(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                resample=Image.BICUBIC)
            out = BytesIO()
            enlarged.save(out, format="PNG")
            payload = out.getvalue()
    except Exception:  # noqa: BLE001 - the original file is never worse than failing
        return data
    return payload if len(payload) <= MAX_FILE_BYTES else data


def _file_to_content_block(path: Path, model: Optional[str] = None) -> dict:
    """A documentation file as an OpenRouter content block.

    Images travel as ``image_url`` with a base64 data URL, PDFs as a
    ``file`` block: these are the two formats foreseen by the
    OpenAI-compatible API of OpenRouter.

    @param path Document file (image or PDF).
    @param model Receiving model identifier, or None: an image is enlarged to
        that model's provider limits when the provider resolves more detail
        from an enlarged image (`_enlarged_image_bytes`); a PDF keeps its
        pages and is rebuilt without its document metadata
        (`_pdf_without_metadata`).
    @return The content block; an image always upright and without metadata,
        a PDF always without Information dictionary and XMP stream.
    @throws ExtractionError For an unsupported, unreadable (vanished,
        locked) or too large file, for an image whose metadata cannot be removed or whose rotation tag cannot
        be applied, and for a PDF the program cannot rebuild without its
        metadata: such a file is refused, never sent raw (fail closed).
    @note The 20 MB cap (MAX_FILE_BYTES) applies to the bytes that leave the
        computer: a rebuilt PDF is measured again after cleaning.
    """
    if path.suffix.lower() not in _SUPPORTED_SUFFIXES:
        raise ExtractionError(
            tx("Formato non supportato: {name} (ammessi: {join})",
               name=path.name,
               join=', '.join(sorted(_SUPPORTED_SUFFIXES)))
        )
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ExtractionError(tx("{name} troppo grande ({size:.1f} MB, max {limit} MB)",
                                     name=path.name, size=size / 1_048_576,
                                     limit=MAX_FILE_BYTES // 1_048_576))
        media_type = _guess_media_type(path)
        payload = path.read_bytes()
    except OSError as exc:
        # A document removed, locked or unreadable since it was chosen: an
        # application error naming the file, not a raw OSError the caller
        # would report as «Errore API» (review 2026-10-29, E8). The system
        # reason only, without the absolute path (it names the user).
        raise ExtractionError(tx("{label}: file non leggibile: {error}", label=path.name,
                                 error=exc.strerror or type(exc).__name__)) from exc
    if media_type.startswith("image/"):
        try:
            payload = _upright_image_bytes(payload)
            enlarged = _enlarged_image_bytes(payload, model)
            if enlarged is not payload:
                payload, media_type = enlarged, "image/png"
                # The enlarged PNG is built from a decoded image whose `info`
                # may still carry an ICC profile: the privacy boundary is
                # applied again on the bytes that actually leave the computer.
                payload = _image_without_metadata(payload)
        except _UncleanableImage as exc:
            # Fail closed: nothing that may carry the GPS position or a
            # sideways picture leaves the computer (review 2026-10-15, M1/L7).
            if exc.reason == "orientation":
                raise ExtractionError(tx(
                    "{name}: immagine danneggiata con un'indicazione di rotazione che il "
                    "programma non riesce ad applicare; non viene inviata all'AI, che la "
                    "leggerebbe ruotata. Salvala di nuovo come JPEG o PNG e riprova.",
                    name=path.name)) from exc
            raise ExtractionError(tx(
                "{name}: immagine che il programma non riesce a leggere; non viene inviata "
                "all'AI perche' i suoi metadati (posizione GPS, dispositivo) non si possono "
                "rimuovere. Salvala come JPEG o PNG e riprova.", name=path.name)) from exc
    elif media_type == "application/pdf":
        try:
            payload = _pdf_without_metadata(payload)
        except _UncleanablePdf as exc:
            # Fail closed, as for an image: a PDF whose author, producer and
            # dates cannot be removed never leaves the computer (review
            # 2026-10-27, E1). The message tells the user the program could
            # not read it as a PDF, which is what every failure of the
            # rebuild amounts to; saving it again (e.g. «Print to PDF»)
            # produces a file the program can rebuild.
            raise ExtractionError(
                f"{_log_name(path.name)}: " + tx("Il file non contiene un PDF riconoscibile.")
            ) from exc
        if len(payload) > MAX_FILE_BYTES:
            # Rebuilding can inflate a file that was compressed with object
            # streams: the cap guards what is sent, not what was on disk.
            raise ExtractionError(tx("{name} troppo grande ({size:.1f} MB, max {limit} MB)",
                                     name=path.name, size=len(payload) / 1_048_576,
                                     limit=MAX_FILE_BYTES // 1_048_576))
    data_b64 = base64.b64encode(payload).decode("ascii")
    data_url = f"data:{media_type};base64,{data_b64}"

    if media_type == "application/pdf":
        return {"type": "file", "file": {"filename": path.name, "file_data": data_url}}
    return {"type": "image_url", "image_url": {"url": data_url}}


#: Overlapping tiles of a drawing sent enlarged beside the whole page: a 2 x 2
#: grid whose tiles cover this fraction of each side (half plus an overlap,
#: so a quote cut by the grid stays whole in one tile), each enlarged to the
#: receiving model's limits. On a 977 x 767 px drawing a tile reaches ~2.2x,
#: a 12 px digit ~26 px (adversarial review of the image path, 2026-11-03:
#: the rotated, faint «16,9» of the T7N relay read 8 x 12 px and came back as
#: 19,3 from the main model; the same pixels enlarged are what the photo
#: path already sends for crop readings).
TILE_SIDE_FRACTION = 0.6


def _enlarged_tile_blocks(path: Path, model: Optional[str]) -> list:
    """@brief The drawing `path` as the whole page plus four overlapping tiles, each enlarged to the model's limits.

    @param path Image document (a dimensioned drawing).
    @param model Receiving model identifier: its provider's limits bound the
        enlargement (`photo_grounding.model_image_limits`), tiles are enlarged
        even for a provider that does not gain from a whole-page enlargement
        (`enlarges` False): a tile is a fraction of the page, so enlarging it
        up to the page limit shows the same digits with more pixels.
    @return Content blocks: a text caption and an image block for the whole
        page, then a caption and an image block per tile («tile k of 4»,
        top-left, top-right, bottom-left, bottom-right), captions in English.
    @throws ExtractionError As `_file_to_content_block` for an unreadable or
        uncleanable image.
    @note Every tile is a lossless PNG rebuilt from the decoded page, so it
        carries no metadata; a transparent background is composited on
        white first, as the enlargement path does.
    """
    from io import BytesIO
    from PIL import Image
    from .photo_grounding import model_image_limits
    whole = _file_to_content_block(path, model)
    # Model-facing captions are fixed English (user, 2026-11-12: every prompt
    # in one language), never tx(): the UI language must not reach the model.
    blocks = [{"type": "text", "text": f"FILE {path.name}: whole page"}, whole]
    limits = model_image_limits(model)
    payload = _upright_image_bytes(path.read_bytes())
    with Image.open(BytesIO(payload)) as image:
        if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            image = Image.alpha_composite(Image.new("RGBA", rgba.size, "white"), rgba)
        page = image.convert("RGB")
        width, height = page.size
        tile_w, tile_h = max(1, int(width * TILE_SIDE_FRACTION)), max(1, int(height * TILE_SIDE_FRACTION))
        corners = [(0, 0), (width - tile_w, 0), (0, height - tile_h), (width - tile_w, height - tile_h)]
        names = ("top left", "top right", "bottom left", "bottom right")
        for index, ((left, top), where) in enumerate(zip(corners, names), start=1):
            tile = page.crop((left, top, left + tile_w, top + tile_h))
            scale = min(limits.long_side_px / max(tile_w, tile_h),
                        math.sqrt(limits.max_pixels / (tile_w * tile_h)))
            if scale > 1.0:
                tile = tile.resize((max(1, int(tile_w * scale)), max(1, int(tile_h * scale))),
                                   resample=Image.BICUBIC)
            out = BytesIO()
            tile.save(out, format="PNG")
            data = _image_without_metadata(out.getvalue())
            if len(data) > MAX_FILE_BYTES:
                continue
            blocks.append({"type": "text", "text": (
                f"FILE {path.name}: tile {index} of 4 ({where}), enlarged: a portion of the same "
                "page, to read the small digits; do not count the same dimensions twice.")})
            blocks.append({"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(data).decode("ascii")}})
    return blocks


#: Pin labels accepted from a drawing reading: a letter or digit followed by
#: letters, digits and the separators KiCad pad numbers commonly carry
#: («1», «11», «A1», «B12», «EP», «MP-2»). Spaces, punctuation and longer
#: texts are a sentence or a slip, not a printed pad number.
_PIN_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+\-]{0,15}")


def _pin_label(value):
    """@brief A pin number as printed on the drawing: «1», «11», «A1», «EP».

    @param value The ``pin`` of a reading: text (the schemas declare it a
        string since 2026-11-03), or an integer from older answers and tests.
    @return An int >= 1 for a label made of ASCII digits only (its historical
        type, so numeric grids keep comparing with populated_pins and the
        numbering conventions); the TEXT «0» for pad 0 (a real pad number on
        some packages, accepted by _validated_pinout too: one rule, review
        S7 2026-11-03), so it is a label like «A1» and stays outside the
        automatic 1..rows x cols numbering; the stripped text for any other
        label; None for a malformed value (booleans, fractions, negative
        numbers, empty or non-label text).
    @note Pin numbers are TEXT (T7NS5D4-12 relay, 2026-11-03: a schema
        declaring ``pin`` an integer made the model drop pads A1 and A2).
        Mixed int/str values are compared through `_pin_text`, never as ints.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not _PIN_LABEL_RE.fullmatch(text):
            return None
        if text.isascii() and text.isdigit():
            number = int(text)
            # «01» stays text: the drawing printed it so, and a pad «01» is
            # not pad 1 (generality audit 2026-11-03). Pad «0» stays text
            # too: it has no place in the 1..N automatic numbering.
            return number if number >= 1 and str(number) == text else text
        return text
    number = _optional_number(value, integer=True)
    if number == 0:
        return "0"
    return number if number is not None and number >= 1 else None


def _pin_text(pin) -> str:
    """The identity of a pin for comparisons: its label as stripped text (1 and "1" alike)."""
    return str(pin).strip()


def _pin_sort_key(pin) -> tuple:
    """Sort key for mixed labels: numeric pins in numeric order, then text labels alphabetically."""
    text = _pin_text(pin)
    return (0, int(text), "") if text.isascii() and text.isdigit() else (1, 0, text)


def _pin_map_seen_from_top(
    pin_map: Optional[Sequence[dict]], cols: int, view: Optional[str],
    rows: Optional[int] = None,
) -> Optional[list]:
    """Convert the positions read from the drawing into coordinates seen from above.

    The KiCad footprint is drawn from the component side, while module
    datasheets almost always show a BOTTOM VIEW: without mirroring, the
    footprint would be a mirror image, with holes in positions that do not
    match the real component.

    The transformation is done here and not in the prompt because it is an
    exact geometric reflection: the model reads (a perceptual task), the
    code flips (a deterministic task).

    @param pin_map Entries {"pin", "row", "col"} as read from the drawing,
        or None/empty when the model gave no map.
    @param cols Number of columns of the matrix.
    @param view View the drawing shows ("bottom" mirrors the columns); any
        other value or None keeps the positions as read.
    @param rows Number of rows of the matrix when known, used only to
        validate the entries; None skips that check.
    @return The entries seen from above, or None when `pin_map` is empty.

    @throws ExtractionError When an entry is not an object with a printed
        pin label (`_pin_label`: «1», «A1», «EP», «0» as text; negative
        numbers, booleans and fractions rejected) and integral row >= 0 and col >= 0,
        lies outside the rows x cols matrix (rows checked when given), or
        repeats a pin number or a position. The map is a numbering convention
        as a whole, so one bad entry invalidates it.
    """
    if not pin_map:
        return None
    if not isinstance(pin_map, (list, tuple)):
        raise ExtractionError(tx("pin_map non e' un elenco di posizioni."))
    entries = []
    pins, positions = {}, {}
    for index, raw in enumerate(pin_map, 1):
        values = {}
        if isinstance(raw, dict):
            values = {key: _optional_number(raw.get(key), integer=True) for key in ("row", "col")}
            values["pin"] = _pin_label(raw.get("pin"))
        if (not values or any(v is None for v in values.values())
                or values["row"] < 0 or values["col"] < 0):
            raise ExtractionError(tx("pin_map, voce {index}: servono il numero del pin come "
                                     "stampato (1, 11, A1, EP) e row e col interi da 0.",
                                     index=index))
        pin, row, col = values["pin"], values["row"], values["col"]
        if col >= cols or (rows is not None and row >= rows):
            raise ExtractionError(tx("pin_map: pin {pin} in riga {row}, colonna {col}, fuori "
                                     "dalla matrice {rows}x{cols}.", pin=pin, row=row, col=col,
                                     rows=rows if rows is not None else "?", cols=cols))
        if _pin_text(pin) in pins:
            raise ExtractionError(tx("pin_map: il pin {pin} compare piu' volte.", pin=pin))
        if (row, col) in positions:
            raise ExtractionError(tx("pin_map: i pin {pin} e {other} occupano la stessa "
                                     "posizione.", pin=pin, other=positions[(row, col)]))
        pins[_pin_text(pin)] = True
        positions[(row, col)] = pin
        entries.append({"pin": pin, "row": row, "col": col})
    if view == "bottom":
        for entry in entries:
            entry["col"] = cols - 1 - entry["col"]
    return entries


def _offset_x_seen_from_top(offset, length, cols, pitch, view):
    """Body X offset of a BOTTOM VIEW drawing, taken to the view from above.

    `_pin_map_seen_from_top` mirrors the columns of a bottom view; the body
    must be mirrored with them, or its left and right margins swap and the
    whole body shifts by their difference (audit 25/09, e.g. 3.0 and 5.05 mm
    margins -> 2.05 mm). In the drawing the body spans [offset, offset +
    length] over pins [0, span]; mirrored, its left margin is the old right
    one: length - span + offset. Only when the columns ARE mirrored (a
    pin_map read on a bottom view); otherwise the offset is returned as is.
    """
    if view != "bottom" or offset is None or length is None:
        return offset
    try:
        cols = int(cols)
    except (TypeError, ValueError):
        return offset
    if cols < 1:
        return offset
    # One column: every pin is at x = 0, so the pitch plays no part and the
    # mirror only swaps the body margins. With more columns the span needs it.
    if cols > 1 and not pitch:
        return offset
    span = (cols - 1) * pitch if cols > 1 else 0.0
    return round(-(length - span + offset), 3)


#: Millimetres per unit, for the drawing units the schema accepts
#: (`drawing_unit`). Exact by definition: 1 inch = 25.4 mm, 1 mil = 0.001 inch.
_UNIT_TO_MM = {"mm": 1.0, "inch": 25.4, "mil": 0.0254}


def _declared_unit_factor(data) -> Optional[float]:
    """@brief Millimetres per drawing unit as the answer DECLARES it, never assumed.

    @param data A model answer carrying ``drawing_unit``.
    @return The factor of a declared, known unit; None when the unit is
        missing or unknown (L11: a missing unit silently meant mm, so a
        drawing in inches would have been written 25.4 times too small).
    @note The dedicated position readings treat None as unverifiable: the
        reading is discarded or the positions blocked, with the reason
        `_undeclared_unit_reason`.
    """
    unit = data.get("drawing_unit") if isinstance(data, dict) else None
    return _UNIT_TO_MM.get(unit) if isinstance(unit, str) else None


def _undeclared_unit_reason() -> str:
    """The reason of a reading set aside because its drawing unit is not declared."""
    return tx("unita' non dichiarata")

_PACKAGE_LENGTH_KEYS = ("body_length_mm", "body_width_mm", "body_height_mm")
_PACKAGE_PIN_LENGTH_KEYS = ("x_mm", "y_mm", "pad_length_mm", "pad_width_mm", "drill_mm",
                            "drill_length_mm")
#: Length fields of a pad's declared terminal section (`lead` of a pad).
_LEAD_LENGTH_KEYS = ("diameter_mm", "width_mm", "thickness_mm")
_COMPONENT_LENGTH_KEYS = ("x_mm", "y_mm", "length_mm", "width_mm", "height_mm")
_FILLET_LENGTH_KEYS = ("radius_mm",)

#: Views a contact drawing can be read from (`contacts_view`).
_CONTACT_VIEWS = ("top", "bottom")

#: Heuristic threshold, in mm, below which a pitch declared in millimetres is
#: reported as a probable unit error. DECLARED HEURISTIC, not a rule: the
#: finest centre-to-centre pitch of production packages is about 0.3 mm
#: (wafer-level chip-scale packages), so a smaller pitch describes no real
#: part, while an inch drawing read as millimetres gives 0.1, 0.05 or
#: 0.0197 for 2.54, 1.27 and 0.5 mm. The margin below 0.3 mm keeps every
#: real pitch clear of it. A body/pitch ratio would not help: it is
#: dimensionless, identical in both units. It never converts anything, it
#: only writes a doubt; a declared `drawing_unit` is what makes it
#: superfluous, and it would miss an inch drawing whose pitch is 0.3 inch or
#: more (7.62 mm), which reads as a plausible millimetre pitch.
_UNIT_DOUBT_PITCH_MM = 0.25


def _scale_lengths(entry, keys, factor: float) -> None:
    """Multiply the numeric length fields of one dict by `factor`, in place.

    Values that are not numbers are left untouched, so the validation that
    follows still sees (and reports) them exactly as the model sent them.
    """
    if not isinstance(entry, dict):
        return
    for key in keys:
        number = _optional_number(entry.get(key))
        if number is not None:
            entry[key] = round(number * factor, 6)


def _convert_units(data: dict, factor: float) -> None:
    """Convert every length of the answer to millimetres, in place."""
    _scale_lengths(data, _TOP_LENGTH_KEYS, factor)
    if isinstance(data.get("castellated_sides"), (list, tuple)):
        for side in data["castellated_sides"]:
            _scale_lengths(side, _SIDE_LENGTH_KEYS, factor)
    geometry = data.get("package_geometry")
    if isinstance(geometry, dict):
        _scale_lengths(geometry, _PACKAGE_LENGTH_KEYS, factor)
        if isinstance(geometry.get("pins"), (list, tuple)):
            for pin in geometry["pins"]:
                _scale_lengths(pin, _PACKAGE_PIN_LENGTH_KEYS, factor)
                if isinstance(pin, dict):
                    # The terminal is quoted in the drawing's unit like the pad.
                    _scale_lengths(pin.get("lead"), _LEAD_LENGTH_KEYS, factor)
    for key, keys in (("components_3d", _COMPONENT_LENGTH_KEYS),
                      ("edge_fillets", _FILLET_LENGTH_KEYS)):
        if isinstance(data.get(key), (list, tuple)):
            for entry in data[key]:
                _scale_lengths(entry, keys, factor)


def _declared_pitches(data: dict, layout: str) -> list:
    """(label, pitch in mm) of every pitch the answer declares, for the unit doubt.

    Matrix pitch (with more than one column), per-side castellated pitches
    (with more than one contact) and the smallest centre distance between the
    explicit pads of package_geometry: all are centre-to-centre distances of
    real contacts, the quantity whose physical floor the heuristic uses.
    """
    pitches = []
    if layout == "matrix":
        cols = _optional_number(data.get("cols"), positive=True, integer=True)
        pitch = _optional_number(data.get("pitch_mm"), positive=True)
        if cols and cols > 1 and pitch:
            pitches.append(("pitch_mm", pitch))
    sides = data.get("castellated_sides")
    if layout == "castellated" and isinstance(sides, (list, tuple)):
        for side in sides:
            if not isinstance(side, dict):
                continue
            count = _optional_number(side.get("count"), positive=True, integer=True)
            pitch = _optional_number(side.get("pitch_mm"), positive=True)
            if count and count > 1 and pitch:
                pitches.append((f"castellated_sides[{side.get('edge')}].pitch_mm", pitch))
    geometry = data.get("package_geometry")
    if isinstance(geometry, dict) and isinstance(geometry.get("pins"), (list, tuple)):
        centres = []
        for pin in geometry["pins"]:
            if isinstance(pin, dict):
                x, y = _optional_number(pin.get("x_mm")), _optional_number(pin.get("y_mm"))
                if x is not None and y is not None:
                    centres.append((x, y))
        distances = [math.dist(a, b) for i, a in enumerate(centres) for b in centres[i + 1:]]
        distances = [d for d in distances if d > 0]
        if distances:
            pitches.append(("package_geometry", min(distances)))
    return pitches


def _unit_doubt(data: dict, layout: str) -> Optional[str]:
    """A doubt note when a pitch read as millimetres is below any real package.

    See `_UNIT_DOUBT_PITCH_MM` for why this is a declared heuristic.
    """
    suspicious = [(label, pitch) for label, pitch in _declared_pitches(data, layout)
                  if pitch < _UNIT_DOUBT_PITCH_MM]
    if not suspicious:
        return None
    label, pitch = min(suspicious, key=lambda item: item[1])
    return tx("DUBBIO unita' di misura: {label} vale {pitch:g} mm, meno del passo piu' fine "
              "dei package reali (circa 0.3 mm). Se il disegno e' in pollici varrebbe "
              "{inch:.4g} mm: verificare l'unita' del disegno e ripetere l'analisi. Nessuna "
              "conversione applicata.", label=label, pitch=pitch, inch=pitch * 25.4)


def _mirror_castellated_sides(sides) -> None:
    """Take castellated sides read on a BOTTOM VIEW to the view from above, in place.

    The bottom view is the part turned over about the vertical axis of the
    drawing: left and right swap, top and bottom stay. Along a vertical edge
    the order from top to bottom is unchanged; along a horizontal edge the
    order from left to right is reversed, so the numbering direction flips,
    the offset (positive to the right) changes sign and the silkscreen names,
    listed in the order met along the edge, are reversed with the contacts.
    """
    if not isinstance(sides, (list, tuple)):
        return
    for side in sides:
        if not isinstance(side, dict):
            continue
        edge = str(side.get("edge") or "").strip().lower()
        if edge in ("left", "right"):
            side["edge"] = "right" if edge == "left" else "left"
            continue
        if edge not in ("top", "bottom"):
            continue
        side["reverse"] = side.get("reverse") is not True
        offset = _optional_number(side.get("offset_mm"))
        if offset is not None:
            side["offset_mm"] = -offset + 0.0
        if isinstance(side.get("pin_names"), list):
            side["pin_names"] = list(reversed(side["pin_names"]))
        # The first contact met along the edge becomes the last: the two
        # end quotes swap with the direction.
        first, last = side.get("first_contact_from_edge_mm"), side.get("last_contact_from_edge_mm")
        if first is not None or last is not None:
            side["first_contact_from_edge_mm"], side["last_contact_from_edge_mm"] = last, first


def _mirrored_column_order(rows: int, cols: int) -> list:
    """Column-order numbering (pin_header.pin_positions) read on a bottom view, seen from top."""
    return [{"pin": col * rows + row + 1, "row": row, "col": cols - 1 - col}
            for col in range(cols) for row in range(rows)]


def _has_explicit_pads(data: dict) -> bool:
    """True when package_geometry carries pads, i.e. it is the footprint source."""
    geometry = data.get("package_geometry")
    return isinstance(geometry, dict) and bool(geometry.get("pins"))


def _canonicalize_answer(data: dict) -> None:
    """Bring a tool answer to millimetres and to the view from above, in place.

    The model reports the numbers and the contact layout as it reads them
    (`drawing_unit`, `contacts_view`); every conversion is done here, once,
    because it is deterministic geometry, not perception. Afterwards the
    answer says `drawing_unit="mm"` and, when the view was known,
    `contacts_view="top"`; the legacy `pin_map_view` is folded in and
    removed. What was done, and every doubt, is kept in `_canonical_notes`;
    `_canonical` marks the answer so that a second call does nothing: the
    photo stage and `_params_from_tool_arguments` read the same answer again.
    Invalid values are left for the validation to reject or report.
    """
    if data.get("_canonical"):
        return
    # A raw answer never carries the program's own measurement block: the
    # photo stage writes it AFTER this conversion (HANDOFF 169), and an
    # answer already converted keeps it (the early return above).
    data.pop("_measured_lands", None)
    data.pop("_measured_holes", None)
    notes: list[str] = []
    layout = data.get("layout") or "matrix"

    unit = data.get("drawing_unit")
    if unit is None:
        # The schema requires the unit; a missing one is read as mm, but
        # SAID, like an unknown one, and its provenance is kept as
        # undeclared for _value_sources (review S6, 2026-11-03).
        notes.append(tx("DUBBIO unita' di misura non dichiarata: quote trattate come "
                        "millimetri, da verificare."))
        data["_unit_undeclared"] = True
        unit = "mm"
    elif not isinstance(unit, str) or unit not in _UNIT_TO_MM:
        # A unit that is not text (a list, an object: unhashable, V1-10) is
        # an unknown unit like any other, never a TypeError.
        notes.append(tx("DUBBIO unita' di misura {unit!r} non riconosciuta: quote trattate come "
                        "millimetri, da verificare.", unit=unit))
        data["_unit_undeclared"] = True
        unit = "mm"
    # The precision of each transcribed pad hole is known only in the
    # drawing's own unit and digits: recorded before the conversion (G6).
    _record_pad_hole_precision(data, _UNIT_TO_MM[unit], notes)
    if unit != "mm":
        _convert_units(data, _UNIT_TO_MM[unit])
        notes.append(tx("Quote lette in {unit} e convertite in millimetri (x {factor:g}).",
                        unit=unit, factor=_UNIT_TO_MM[unit]))
    else:
        doubt = _unit_doubt(data, layout)
        if doubt:
            notes.append(doubt)
    data["drawing_unit"] = "mm"

    view = data.get("contacts_view")
    if view is None:
        view = data.get("pin_map_view")
    view = view if view in _CONTACT_VIEWS else "unknown"
    data.pop("pin_map_view", None)
    reads_contacts = layout == "castellated" or (layout == "matrix" and not _has_explicit_pads(data))
    if view == "unknown" and reads_contacts:
        notes.append(tx("DUBBIO vista: il documento non dice se i contatti sono visti dall'alto o "
                        "dal basso; interpretati come vista dall'alto. Se il disegno era una "
                        "BOTTOM VIEW il footprint risulta specchiato: verificarlo sul disegno."))
    if layout == "castellated" and view == "bottom":
        _mirror_castellated_sides(data.get("castellated_sides"))
        notes.append(tx("Lati del modulo letti su una vista dal basso: specchiati nella vista "
                        "dall'alto del footprint."))
    if layout == "matrix":
        rows = _optional_number(data.get("rows"), positive=True, integer=True)
        cols = _optional_number(data.get("cols"), positive=True, integer=True)
        pitch = _optional_number(data.get("pitch_mm"), positive=True)
        if data.get("pin_map"):
            if cols is None:
                notes.append(tx("pin_map ignorata: la matrice non ha un numero di colonne valido."))
                data["pin_map"] = None
            else:
                try:
                    data["pin_map"] = _pin_map_seen_from_top(
                        data["pin_map"], cols, "bottom" if view == "bottom" else "top", rows)
                except ExtractionError as exc:
                    # Optional field: the paid response survives, but automatic
                    # numbering replaces the map, so the note must say it loudly.
                    notes.append(tx("ATTENZIONE pin_map scartata ({exc}): numerazione automatica "
                                    "colonna per colonna, da verificare sul pinout.", exc=exc))
                    data["pin_map"] = None
        if view == "bottom" and cols is not None:
            if not data.get("pin_map") and rows is not None:
                # Column order read on a bottom view is NOT column order seen
                # from above: the mirrored numbering needs an explicit map.
                data["pin_map"] = _mirrored_column_order(rows, cols)
            offset = _optional_number(data.get("body_offset_x_mm"))
            length = _optional_number(data.get("body_length_mm"), positive=True)
            mirrored = _offset_x_seen_from_top(offset, length, cols, pitch, "bottom")
            if offset is not None and mirrored is not None:
                data["body_offset_x_mm"] = mirrored
            notes.append(tx("Pin letti su una vista dal basso: colonne e spostamento del corpo "
                            "specchiati nella vista dall'alto del footprint."))
    if view in _CONTACT_VIEWS:
        data["contacts_view"] = "top"
    else:
        data["contacts_view"] = "unknown"
    data["_canonical_notes"] = notes
    data["_canonical"] = True


def _merge_photo_components(groups):
    """Compatibility entry for callers that use the inventory in millimetres.

    The groups are inventories built by the program, so their internal flags
    are kept (trusted).
    """
    from .photo_inventory import merge_components
    return merge_components([_components_3d(g, trusted=True) or [] for g in groups])


#: Maximum number of characters with which a file name is shown in the
#: progress line. It is a PRESENTATION fallback, not a rule on documents: it
#: does not depend on extension, content or brand.
#:
#: The modal now puts each document on its own line
#: (`analysis_overlay.enumeration_lines`), so a long name no longer pushes the
#: following names out: it shortens only ITSELF. The limit stays because a
#: card line holds about eighty-eight characters (the width the card is sized
#: for), and an entry is «nome — ruolo» (name — role): with the forty
#: characters here plus the separator and the longest role label among those
#: declared in `document_roles.ROLES` (thirty characters) the line stays below
#: that width, i.e. the role is always readable. Beyond the limit the card
#: would shorten the line anyway, but by cutting the TAIL, hence the role:
#: here the name is shortened in the middle, and the extension survives.
_LOG_NAME_CHARS = 40


def _log_name(name: str) -> str:
    """@brief Shorten one file name so a multi-file progress line stays readable.

    @param name File name as the user sees it, without any directory part.
    @return The name unchanged when short enough, otherwise its head and tail
        joined by an ellipsis, so both the prefix and the extension stay
        visible and the document remains recognizable.
    @note Presentation only: the name sent to the model is never shortened.
    """
    if len(name) <= _LOG_NAME_CHARS:
        return name
    tail = _LOG_NAME_CHARS // 3
    return name[:_LOG_NAME_CHARS - 1 - tail] + '…' + name[-tail:]


#: Roles a document may carry (document manifest version 2). One file may
#: carry several at once: a datasheet page is often pinout, dimensioned top
#: view and land pattern together.
DOCUMENT_ROLE_KEYS = ("pinout", "top", "hires", "land_pattern", "side_view", "datasheet")
#: Views a complete manufacturer datasheet may contain. When a file is
#: declared `datasheet`, these views, if not declared on another file, are
#: not "missing": the model looks for them in the datasheet and reports the
#: sections it does not find. The photograph is never one of them.
DATASHEET_VIEWS = ("pinout", "top", "land_pattern", "side_view")
#: Views a dimensioned drawing («top», one sheet) may print beside the plan
#: view: looked for in it when not declared on another file.
DRAWING_VIEWS = ("side_view",)
#: The role of the photographs of the real part: only files carrying it go to
#: the photo inventory (component recognition).
PHOTO_ROLE = "hires"

#: How each role is presented to the model: (model-facing name, instruction).
#: Prompt text in English, like every prompt (user, 2026-11-12), not a
#: user-visible message. The NAME is a fixed English name keyed by the role id;
#: the user reads the Italian-source label of document_roles.ROLES, translated
#: by the catalog (`_role_label`), so the UI language never reaches the model
#: (tests/test_extraction_prompts.py checks that every role has both).
_ROLE_PROMPTS = {
    "pinout": ("Pinout / contact arrangement",
               "use it for contacts, numbering, pin functions and contact dimensions. "
               "Do not assume that the drawing is seen from above: declare in contacts_view "
               "the view that the document indicates."),
    "top": ("Dimensioned image",
            "dimensioned drawing: in plan use it for the mechanical dimensions -- length and width "
            "of the body, position of the contacts and of the rows, offsets of the body relative to the "
            "contacts; if the same sheet prints a front or side view, read from that one "
            "the vertical dimensions (body_height_mm, pin_length_3d_mm)."),
    "hires": ("High-resolution photo",
              "photograph of the real part seen from above: use it for what can be seen -- "
              "contact type on the edge, colours, silkscreen labels, rounded edges, "
              "mounted components. It is not a dimensioned drawing: a measure derived from here is "
              "an estimate (\"estimated\" in value_sources)."),
    "land_pattern": ("Recommended land pattern",
                     "recommended lands (land pattern) on the board that hosts the "
                     "component: use it for "
                     "sizes and positions of lands and holes (pad_diameter_mm, drill_mm, "
                     "pad_length_mm, pad_width_mm, package_geometry). The lands are not the "
                     "body: do not derive the body_* dimensions from them."),
    "side_view": ("Side view / heights",
                  "profile view: use it for the height dimensions -- body_height_mm, "
                  "pcb_thickness_mm, pin_length_3d_mm and heights of the components. Plan lengths, "
                  "widths and pitches are not read from this view."),
    "datasheet": ("Manufacturer datasheet",
                  "complete document of the manufacturer: it may contain the pin table and pinout, "
                  "dimensioned drawings seen from above and in profile, recommended lands (land "
                  "pattern). Look for each of these sections and use it for its purpose, as if "
                  "it were declared separately; declare in contacts_view the view of the drawing of the "
                  "contacts. A section that is not there is flagged in notes, not reconstructed. "
                  "It is not a photo of the part: do not derive components_3d or colours from it."),
}


def _role_label(role: str) -> str:
    """User-visible label of a role: the document catalogue's, else the model-facing name."""
    from .document_roles import ROLES
    return ROLES.get(role) or _ROLE_PROMPTS[role][0]


def _declared_role_keys(value) -> tuple:
    """The known role keys in one declared value (a key or a collection of keys), tolerantly.

    Used where an unknown role must read as «not declared» instead of failing
    (the log line); `normalize_document_roles` is the strict version.
    """
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(key for key in DOCUMENT_ROLE_KEYS if key in value)


def normalize_document_roles(document_roles, file_names) -> Optional[dict[str, tuple]]:
    """@brief Validate the user-declared document roles and give every file its tuple of roles.

    @param document_roles None (no roles declared), or a mapping file name ->
        role key (legacy form, one role per file, as in manifest version 1)
        or file name -> list/tuple/set of role keys (manifest version 2).
        Role keys are those of DOCUMENT_ROLE_KEYS.
    @param file_names Names of the documents sent, without directories.
    @return None when `document_roles` is None; otherwise file name -> tuple
        of role keys, without repetitions, in DOCUMENT_ROLE_KEYS order.
    @throws ExtractionError When the mapping does not name exactly the files
        sent, a file has no role or an unknown one, or -- legacy form only --
        two files declare the same role (a version 1 manifest has one file
        per role).
    @note Legacy form only: when no file is declared a photograph ('hires'),
        the files declared 'top' also become photographs. That was the
        behaviour of version 1, where the dimensioned top view stood in for a
        missing photo, and it is the same migration the manifest applies
        (version 1 -> 2), so an old caller sees the same result. A version 2
        mapping is taken as declared: only files marked 'hires' are photos.
    """
    if document_roles is None:
        return None
    invalid = ExtractionError(tx("Ruoli documenti incompleti, duplicati o non validi."))
    names = list(file_names)
    if not isinstance(document_roles, dict) or set(document_roles) != set(names):
        raise invalid
    legacy = all(isinstance(value, str) for value in document_roles.values())
    if legacy and len(set(document_roles.values())) != len(names):
        raise invalid
    roles = {}
    for name, value in document_roles.items():
        keys = (value,) if isinstance(value, str) else value
        if (not isinstance(keys, (list, tuple, set, frozenset)) or not keys
                or any(not isinstance(key, str) or key not in DOCUMENT_ROLE_KEYS
                       for key in keys)):
            raise invalid
        roles[name] = _declared_role_keys(keys)
    if legacy and not any(PHOTO_ROLE in keys for keys in roles.values()):
        roles = {name: _declared_role_keys(keys + (PHOTO_ROLE,)) if "top" in keys else keys
                 for name, keys in roles.items()}
    return roles


def photo_document_indices(doc_paths, document_roles) -> Optional[list[int]]:
    """@brief Indices of the documents declared photographs, the only inputs of the photo inventory.

    @param doc_paths Documents in request order.
    @param document_roles Declared roles, in any form `normalize_document_roles`
        accepts, or None.
    @return None when no roles are declared (the model's `top_photo_index`
        decides); otherwise the indices of the files carrying PHOTO_ROLE, in
        request order, or [-1] when none does (no other document is used in
        their place).
    @throws ExtractionError When the roles are invalid (see
        `normalize_document_roles`).
    """
    names = [Path(path).name for path in doc_paths]
    roles = normalize_document_roles(document_roles, names)
    if roles is None:
        return None
    return [index for index, name in enumerate(names) if PHOTO_ROLE in roles[name]] or [-1]


def role_instructions(prompts=None) -> dict:
    """@brief The model-facing instruction of every document role, from the profile or the code.

    @param prompts Mapping profile field -> text (prompt_catalog.profile_prompts),
        or None. The field of a role is «role_prompt_<role>».
    @return {role key: instruction} for every key of _ROLE_PROMPTS: the
        profile's text when it is not blank, else the second item of
        _ROLE_PROMPTS (the factory text).
    @note Only the INSTRUCTION is a profile prompt; the role NAME (first item
        of _ROLE_PROMPTS) is the fixed English name the model reads and stays
        in the code; the user reads the label of document_roles.ROLES.
    """
    return {key: effective_text(f"role_prompt_{key}", prompts) for key in _ROLE_PROMPTS}


def _role_description(keys, instructions=None) -> str:
    """How one file and its declared roles are introduced to the model.

    `instructions` maps a role key to its instruction (`role_instructions`);
    None, or a key missing from it, uses the factory text of _ROLE_PROMPTS.
    """
    instructions = instructions or {}
    names = " + ".join(_ROLE_PROMPTS[key][0] for key in keys)
    lead = "ROLE DECLARED BY THE USER" if len(keys) == 1 else "ROLES DECLARED BY THE USER"
    lines = [f"{lead}: {names}."]
    lines += [f"- {_ROLE_PROMPTS[key][0]}: {instructions.get(key) or _ROLE_PROMPTS[key][1]}"
              for key in keys]
    if len(keys) > 1:
        lines.append("The same file applies to all these roles: read from every part of the "
                     "document the information of the corresponding role.")
    if "pinout" in keys and "top" not in keys and "datasheet" not in keys:
        # Only when the SAME file is not also the dimensioned top view: a
        # datasheet page that is both must be read for both.
        lines.append("This file is not declared a dimensioned top-view drawing: do not use it "
                     "for the envelope of the body.")
    return "\n".join(lines)


def _files_sent_message(doc_paths, document_roles: Optional[dict] = None) -> str:
    """@brief Name the documents about to travel in the paid analysis request.

    @param doc_paths Documents in request order; only their file names are
        reported, because the directories say nothing about what is being sent
        and would take the room the names and the roles need.
    @param document_roles Optional file name -> role key, or file name ->
        collection of role keys, as declared by the user. A name missing from
        it, or without any known role, is reported as undeclared: the role is
        never guessed from the file name. Several roles of one file are listed
        joined by « + ».
    @return One readable line listing every document and, where known, the
        role it is sent as.
    @note The count and the noun agree for one document as well as for many,
        so the line never reads as a plural applied to a single file.
    @note Punctuation is part of the contract, not taste, and now for two
        readers at once.

        The modal hides the lines a machine writes for itself, recognizing them
        by an UPPERCASE_UNDERSCORE token in FIRST position (`analysis_overlay.
        _MACHINE_MARKER`). A file name is user data and is never rewritten, so
        the line is built so that no name ever opens it: the lead-in does. That
        is what keeps a document called «MOD_REV_B» from hiding the one line
        that says what is being paid for; the old requirement that no bare
        space follow a name dated from an unanchored marker rule and no longer
        applies. A name that carries the separators below inside itself is
        still beyond this line's reach.

        Three signs are read by the card (`analysis_overlay`), while the line
        written to the log stays ONE line with one timestamp, as every other
        log entry:

        - the colon closes the lead-in and every middle dot starts a new line
          (`enumeration_lines`): the documents are listed one per line;
        - " — " (space, em dash, space) separates each file name from its role
          (`enumeration_term`): the card shows the name in bold, set apart from
          the role, as the user asked («i nomi dei file non sono evidenziati,
          troppo attaccati al testo», the file names are not highlighted, too
          close to the text). It replaced the comma, which the card
          could not tell apart from a comma inside a file name without guessing
          what a file name looks like.

        Replacing the middle dot with a semicolon, dropping the colon, or going
        back to the comma would silently take the list or the highlighting away
        without breaking anything else -- hence this note. The role labels
        never contain " — ", so the first occurrence in an item always ends the
        name unless the name itself contains the sign.
    """
    if not doc_paths:
        # Defensive invariant of the helper: the line describes a submission,
        # and without documents there is no submission to describe. An
        # «Invio a AI 0 file» (sending 0 files to the AI) would suggest a call
        # that went out empty, hence paid. The extraction caller rejects the
        # empty list earlier («Nessun file selezionato.» (no file selected)),
        # so this is not an expected case in production: it is the defined
        # behaviour of the helper for whoever reuses it, so the branch stays
        # testable and does not produce a wrong sentence if a second caller arrives.
        return tx("Nessun documento da inviare a AI: layout, quote e pinout non sono "
                  "estraibili.")
    described = []
    for path in doc_paths:
        name = Path(path).name
        keys = _declared_role_keys((document_roles or {}).get(name))
        label = ' + '.join(tx(_role_label(key)) for key in keys)
        described.append(f'{_log_name(name)} — {label}' if label
                         else tx("{log_name} — ruolo non dichiarato", log_name=_log_name(name)))
    count = len(described)
    # Singular and plural are two whole sentences: each language inflects
    # them its own way, a pasted «o»/«i» suffix only works for Italian.
    lead = (tx("Invio a AI 1 documento per layout, quote e pinout: ") if count == 1 else
            tx("Invio a AI {count} documenti per layout, quote e pinout: ", count=count))
    return lead + ' · '.join(described) + '.'


def extract_pin_header_params(
    doc_paths: list[Path],
    model: Optional[str] = None,
    system_prompt: Optional[str] = None,
    extra_instructions: Optional[str] = None,
    include_nc_pins: bool = True,
    progress: Optional[Callable[[str], None]] = None,
    vision_model: Optional[str] = None,
    photo_cache_dir: Optional[Path] = None,
    grounding_prompt: Optional[str] = None,
    verification_prompt: Optional[str] = None,
    verification_model: Optional[str] = None,
    terminal_prompt: Optional[str] = None,
    crop_prompt: Optional[str] = None,
    crop_model: Optional[str] = None,
    document_roles: Optional[dict] = None,
    component_family: Optional[str] = None,
    photo_detector: str = "ai",
    yolo_model_path: Optional[Path] = None,
    smd_model_path: Optional[Path] = None,
    yolo_confidence: Optional[float] = None,
    yolo_settings: Optional[dict] = None,
    phase: Optional[Callable[..., None]] = None,
    review_proposals: Optional[Callable[..., object]] = None,
    cancel: Optional[object] = None,
    numbering_prompt: Optional[str] = None,
    pad_model: Optional[str] = None,
    part_number: Optional[str] = None,
    answers: Optional[dict] = None,
    prompts: Optional[dict] = None,
    step_models: Optional[Mapping[str, str]] = None,
) -> ExtractedParams:
    """@brief Extract dimensions and pinout, optionally using local YOLO photo proposals.

    @param doc_paths Documentation files to send (PDF or images), at least one
        and at most MAX_FILES; each is checked for format and size before any
        paid call.
    @param model Provider model identifier of the main document call (e.g.
        the OpenRouter id "anthropic/claude-sonnet-5"): it allows a more
        capable model on difficult datasheets without touching the global
        default. None means DEFAULT_MODEL.
    @param system_prompt Override of DEFAULT_SYSTEM_PROMPT (or of
        DEFAULT_SINGLE_SYSTEM_PROMPT for a single-component family): the
        GLOBAL setting (the «Configurazione» menu of the GUI), valid for every component.
        None keeps the default. The tool schema stays fixed regardless: it is
        what guarantees that the answer is always valid structured JSON.
    @param extra_instructions Notes specific to THIS component ("the case is
        metal", "ignore the side connector"), appended to the user message
        together with the documents and forwarded to the photo inventory.
        None or blank adds nothing.
    @param include_nc_pins Whether the pins marked NC in the pinout are
        generated anyway (default True): they are physically present on the
        component, and without their pad the part would not fit the board.
        False asks the model to leave them out of the populated pins.
    @param progress Optional callable(str) receiving the log lines (documents
        sent, warnings, cost); None discards them. A failing callback never
        aborts the extraction.
    @param vision_model Model of the paid localization calls on the board
        photograph (photo inventory and terminal observation); None means
        photo_grounding.DEFAULT_VISION_MODEL.
    @param photo_cache_dir Folder of the photo response cache and of the
        saved photo evidence; None writes nothing and caches nothing.
    @param grounding_prompt Custom notes of the photo localization call
        (profile field), forwarded to the photo inventory; None sends its
        factory text.
    @param verification_prompt Custom notes of the photo verification call
        (profile field), forwarded to the photo inventory; None sends its
        factory text.
    @param verification_model Model of the paid verification/classification
        call on the photograph; None falls back to `model`, then
        DEFAULT_MODEL.
    @param terminal_prompt System prompt of the terminal observation
        (photo_terminals.observe_terminals), used only on the legacy 'ai'
        detector path; None or blank keeps that module's default.
    @param component_family Declared family of the chosen component type
        (e.g. CASTELLATED_FAMILY, "aggregate_tht", one of
        SINGLE_COMPONENT_FAMILIES): it selects the system prompt, the edge
        enlargements of the photo and the pin-field check
        (`fit_body_to_pin_field`). None means no family is declared.
    @param review_proposals Optional callable(photo_png, proposals,
        length_mm, width_mm) -> proposals kept, run between the local
        detector and the paid reconciliation (see
        photo_inventory.extract_inventory); returning None aborts the
        inventory. None skips the review.
    @param numbering_prompt System prompt of the dedicated pin-numbering call
        (`read_pin_numbering`); None means DEFAULT_PIN_NUMBERING_PROMPT.
    @param pad_model Model of the dedicated pad-position reading of a single
        part (profile field 'pad_model', e.g. DEFAULT_PAD_POSITIONS_MODEL);
        None or blank uses `model`. When its provider has no key or credit
        the reading falls back to `model` and the log says so: the run is
        not refused for an optional, more capable reader.
    @param part_number The component's part number typed by the user (GUI
        field «Sigla»), single parts only: the KiCad library footprints it
        names (library_footprints) become one more GEOMETRY witness of the pad
        positions, beside the drawn holes; numbers and names never come from
        them. None or blank searches nothing.
    @param answers The answers the user gave to the structured questions of
        an earlier analysis of this component ({question key: chosen option
        value}, see ExtractedParams.questions and `apply_answers`), or None.
        A doubt whose key is known is settled with the saved answer during
        the extraction and asked no more; the key names the candidates, so a
        different doubt is asked afresh.
    @param prompts The profile's prompt texts, {profile field: text}
        (prompt_catalog.profile_prompts of ProfileStore.resolve), or None.
        Read here for the prompts that have no parameter of their own:
        'pad_positions_prompt', 'body_dimensions_prompt',
        'contact_style_prompt', the six 'role_prompt_<role>' and, through the
        photo inventory, 'mechanical_evidence_prompt', the three
        'inventory_*' prompts and 'marking_prompt'. A missing or blank text
        sends the registry default (prompt_catalog.effective_text). When not
        None, every request of the run also goes through
        prompt_catalog.SentPromptLog, which writes one log line per prompt
        sent (title, status, digest; never the text). None sends the
        defaults and writes no such line.
    @param step_models The model of every analysis step, {profile prompt
        field: model identifier or "" for the single model}
        (ProfileStore.step_models, see prompt_models), or None. None keeps
        the behaviour before 2026-11-12: every call uses `model`, the
        dedicated pad and body readings `pad_model`. When given, `pad_model`
        is ignored and each call resolves its own step's model AT THE CALL
        SITE, before its images are sized and before any cache key
        (prompt_models.usable_model: a step model that cannot run leaves the
        step to `model`, with a log line): the main reading
        ('system_prompt_single' or 'system_prompt', whichever is sent; the
        castellated recounts and the numbering repair reuse it), the
        dedicated numbering ('numbering_prompt'), the dedicated pad and body
        readings of a single part ('pad_positions_prompt',
        'body_dimensions_prompt', each falling back to the main reading's
        model), the contact-style recheck ('contact_style_prompt'); the
        mapping is forwarded to the photo stage (`step_models=` of
        photo_inventory.extract_inventory and photo_terminals.observe_terminals).

    @param cancel Optional cancellation channel: a threading.Event (or any
        object with is_set()) or a zero-argument callable returning True when
        the user asked to stop. It is checked before EVERY paid call, retries
        and the photo inventory included, and during retry backoff waits.
        When set, ExtractionCancelled is raised and no further call is paid.
        None (default) keeps the previous behaviour.
    @param document_roles None, or the roles the user declared for each file:
        file name -> list of role keys among DOCUMENT_ROLE_KEYS ('pinout',
        'top', 'hires', 'land_pattern', 'side_view'; one file may carry
        several), or the legacy file name -> single role key. Validated
        before any paid call (see `normalize_document_roles`); only the files
        carrying 'hires' go to the photo inventory (`photo_document_indices`).

    @param crop_prompt Instructions of the per-body crop reading (profile
        field 'crop_prompt'). Used only with photo_detector 'yolo' or 'vlm',
        the paths that run photo_crop_reading.read_bodies; None or blank sends
        its factory default.
    @param crop_model Model of that per-body crop reading (profile field
        'crop_model', HANDOFF 150), same paths as `crop_prompt`. None or
        blank reads the crops with the model of the paid call on the
        photograph (`verification_model`, else `model`); a model set here is
        used for the crop readings only, and its provider is checked for a
        key and credit before the run like the others.
    @param photo_detector Photo localization backend: ai (legacy), yolo (local
        proposals plus one AI reconciliation) or vlm (no local detector: the
        vision model enumerates the bodies in that same single request).
    @param yolo_model_path Optional compatible ONNX file; validated before paid calls.
    @param smd_model_path Optional checkpoint of the local SMD network
        (photo_detector 'yolo_smd'); None uses models/smd/smd_classifier.pt.
        Validated before paid calls.
    @param yolo_confidence Operating confidence threshold of the local YOLO
        detector; None keeps its default (photo_yolo.operating_confidence).
    @param yolo_settings Other local-detector settings (photo_yolo.yolo_settings);
        None keeps the defaults. Validated before any paid call.
    @param phase Optional callable(tool, detail) telling the caller WHICH tool
        is working, so a progress modal can distinguish a paid remote call
        ('llm' for the documents, 'llm_inventory' for the board photo) from
        local detection ('yolo'). It is forwarded to the photo
        inventory, which announces the local and the paid steps as they start.
        None disables the notification; a failing callback never aborts the
        extraction, exactly like `progress`.
    @return The validated ExtractedParams, with the photo inventory in
        `components_3d` when a photograph was available, and every
        conversion, doubt and dropped value written in `notes`.
    @throws ExtractionError For any application problem: missing key or
        credit, unsupported or too large files, too many files, unknown photo
        detector, local YOLO not available, network/API errors, an answer
        without the tool call or unreadable even after the one retry, or an
        answer rejected by the validation. A main answer rejected by the
        validation keeps its content in `ExtractionError.raw_response` and in
        the AI log.
    @throws ExtractionCancelled When `cancel` is set before a paid call: no
        further call is paid.
    @note Two levels of instructions, both optional: `system_prompt` is the
        GLOBAL setting valid for every component, `extra_instructions` are
        the notes of the SINGLE component.
    @note YOLO localization is local; datasheet, package and terminal review still use AI.
    """
    # The cost tally concerns this run: reset it BEFORE any check, so that
    # even a validation rejection (YOLO, roles, files) does not show the cost
    # of the previous run as if it were its own.
    reset_usage()
    if not doc_paths:
        raise ExtractionError(tx("Nessun file selezionato."))
    if photo_detector not in ("ai", "yolo", "vlm", "yolo_smd"):
        raise ExtractionError(tx("Rilevatore fotografico non supportato."))
    if photo_detector in ("yolo", "yolo_smd"):
        if photo_detector == "yolo_smd" and yolo_model_path is None:
            # The shared rule (review 2026-10-29, P3): the checkpoint when it
            # is there, else its ONNX export -- the installed program has
            # only the export, and forcing the checkpoint refused the run.
            from ..app_paths import default_yolo_smd_model
            yolo_model_path = default_yolo_smd_model()
        from .photo_yolo import operating_confidence, validate_yolo_setup
        from .photo_yolo import yolo_settings as check_yolo_settings
        try:
            # Threshold and parameters are also checked here, before paying.
            operating_confidence(yolo_confidence)
            check_yolo_settings(yolo_settings)
            validate_yolo_setup(yolo_model_path)
            if photo_detector == "yolo_smd":
                from .smd_component_classifier import validate_smd_setup
                validate_smd_setup(smd_model_path)
        except (ValueError, OSError, ImportError, RuntimeError) as exc:
            raise ExtractionError(tx("YOLO locale non disponibile: {exc}", exc=exc)) from exc
    from .ai_diagnostics import make_logged_call
    # The application analysis must be auditable: the persistent log keeps
    # the exact text of the prompts/tools and the image hashes (never the bytes).
    cancelled = _cancel_predicate(cancel)

    def check_cancel():
        """Raise ExtractionCancelled when the user asked to stop."""
        if cancelled is not None and cancelled():
            raise ExtractionCancelled(tx("Estrazione annullata dall'utente: nessuna altra "
                                         "chiamata a pagamento."))

    diagnostic_call = make_logged_call(call_tool, progress, log_prompt=True,
                                       cancel=check_cancel if cancelled is not None else None)
    if prompts is not None:
        # One log line per profile prompt the run really sends (status and
        # digest, never the text): user request of 2026-11-11, point 4.
        from .prompt_catalog import SentPromptLog
        diagnostic_call = SentPromptLog(prompts, progress).wrap(diagnostic_call)
    if len(doc_paths) > MAX_FILES:
        raise ExtractionError(tx("Troppi file selezionati (max {MAX_FILES} per chiamata).", MAX_FILES=MAX_FILES))
    # Legacy strings and lists alike become tuples of roles, checked before
    # anything is paid; from here on only the normalized form travels.
    document_roles = normalize_document_roles(document_roles, [p.name for p in doc_paths])

    check_cancel()
    try:
        # The credit must be checked here, before the first content block:
        # the extraction is a chain of dozens of calls (main analysis, crops,
        # verification, terminals) and running dry halfway means having spent
        # without getting a component. Every provider of the models used is
        # checked (OpenRouter and/or DeepSeek direct, 27/09).
        # The EFFECTIVE models, defaults applied exactly as the run applies
        # them below: a DeepSeek main model with no explicit vision model
        # still calls the default vision model, whose provider must be ready.
        from .photo_grounding import DEFAULT_VISION_MODEL
        from .providers import ensure_providers_ready

        def openrouter_ready(report):
            """OpenRouter key and credit, through this module's references."""
            api_key()
            check_credit(progress=report)

        ensure_providers_ready(
            [model or DEFAULT_MODEL, vision_model or DEFAULT_VISION_MODEL,
             verification_model or model or DEFAULT_MODEL,
             # Blank = the photo model above; an empty entry is skipped.
             (crop_model or '').strip()],
            progress, openrouter_check=openrouter_ready)
    except OpenRouterError as exc:
        raise ExtractionError(str(exc)) from exc

    # The model of every step, resolved HERE, at the call site, before the
    # images are sized for it (user request of 2026-11-12, prompt_models).
    # The check above covers `model`, every step's fallback; a step model on
    # another provider is checked by usable_model when it is resolved, and
    # one that cannot run leaves its step to the fallback with a log line:
    # an optional model never stops the run. Without step_models every call
    # uses `model` as before.
    main_field = ("system_prompt_single" if component_family in SINGLE_COMPONENT_FAMILIES
                  else "system_prompt")
    main_model = (usable_model(main_field, step_models, model or DEFAULT_MODEL, progress,
                               openrouter_ready)
                  if step_models is not None else model or DEFAULT_MODEL)
    resolved_steps: dict = {}
    #: {(requested model, step sends images): usable model} of this run.
    resolved_models: dict = {}

    def step_model(field):
        """The model of the step `field` (falling back to the main reading's), resolved once per run.

        Resolved on first use: a step the answer never reaches (the
        numbering of a castellated board) writes no fallback line.
        Without step_models it is the main model, as before 2026-11-12.
        """
        if step_models is None:
            return main_model
        if field not in resolved_steps:
            # Memoised per requested model too (finding 5, 2026-11-12): two
            # steps asking the same model (the pad positions and the body
            # quotes on the legacy «Posizioni pad» model) check it once and
            # log at most one fallback line. Keyed with whether the step
            # sends images, the one step property usable_model checks.
            try:
                images = prompt_step(field).images
            except KeyError:
                images = True
            key = (model_for(field, step_models, main_model), images)
            if key not in resolved_models:
                resolved_models[key] = usable_model(field, step_models, main_model, progress,
                                                    openrouter_ready)
            resolved_steps[field] = resolved_models[key]
        return resolved_steps[field]

    def announce(tool, detail):
        """Reporting the phase is informational: a broken modal must not lose a
        paid processing, so the error is logged and the run goes on -- the
        same treatment given to `progress`."""
        if phase is None:
            return
        try:
            phase(tool, detail)
        except Exception as exc:  # noqa: BLE001 - the GUI must not be able to abort the analysis
            if progress:
                progress(tx("Segnalazione della fase non riuscita ({exc}); elaborazione proseguita.",
                            exc=exc))

    # Which tool is working cannot be inferred from the progress messages,
    # and the difference matters: this is a remote call that is paid for,
    # local detection is not.
    announce("llm", main_model)
    if progress:
        # The user must be able to check WHICH of their documents went into
        # the paid call, and with which declared role.
        progress(_files_sent_message(doc_paths, document_roles))
    content_blocks = []
    # Where each document's block sits in `content_blocks`: a later call by
    # another step model gets the same request with the images sized for
    # ITS model (`blocks_for`).
    file_slots = []
    # The role instructions of the profile (fields 'role_prompt_<role>'),
    # blank or missing ones replaced by the factory texts.
    roles_text = role_instructions(prompts)
    for index, path in enumerate(doc_paths):
        description = f"FILE {index}: {path.name}"
        if document_roles is not None:
            description += "\n" + _role_description(document_roles[path.name], roles_text)
        content_blocks.append({"type": "text", "text": description})
        file_slots.append((len(content_blocks), path))
        content_blocks.append(_file_to_content_block(path, main_model))
    if component_family == CASTELLATED_FAMILY and document_roles is not None:
        # The AI recognises the number of contacts in THIS call (user,
        # 28/09, point 142): the photo edges enlarged, not a correction afterwards.
        strips = _edge_strip_blocks(doc_paths, document_roles)
        content_blocks.extend(strips)
        if strips and progress:
            progress(tx("Allegati all'AI gli ingrandimenti dei bordi della foto, per contare i "
                        "contatti fisici: {count} immagini.",
                        count=sum(1 for block in strips if block.get("type") == "image_url")))
    request = ("Extract the mechanical parameters of the component from these documents and call the "
               "tool. Write every free-text value (notes, reasons) in English.")
    if document_roles is not None:
        declared = {key for keys in document_roles.values() for key in keys}
        # A datasheet is a container: the views it may hold are looked for in
        # it, not declared missing. The datasheet role itself is optional.
        in_datasheet = set(DATASHEET_VIEWS) if "datasheet" in declared else set()
        # A dimensioned drawing routinely prints the plan and the profile
        # views on one sheet: an undeclared side view is looked for in it,
        # never declared absent (adversarial review 2026-11-03: «Viste non
        # fornite: Vista laterale» plus «se nessuna vista mostra il corpo di
        # profilo, ometti» made a compliant model drop the printed height).
        in_drawing = set(DRAWING_VIEWS) if "top" in declared else set()
        missing = [_ROLE_PROMPTS[key][0] for key in DOCUMENT_ROLE_KEYS
                   if key not in declared and key not in in_datasheet and key not in in_drawing
                   and key != "datasheet"]
        to_search = [_ROLE_PROMPTS[key][0] for key in DATASHEET_VIEWS
                     if key in in_datasheet and key not in declared]
        in_drawings = [_ROLE_PROMPTS[key][0] for key in DRAWING_VIEWS
                       if key in in_drawing and key not in declared and key not in in_datasheet]
        request += ('\nThe roles of the files are explicit: respect them, do not deduce them from the original names. '
                    'A file with several roles applies to each of them. '
                    'Flag missing data and inconsistencies, without inventing dimensions to complete the absent views.')
        if missing:
            request += '\nViews not provided: ' + ', '.join(missing) + '.'
        if to_search:
            request += ('\nViews to look for in the datasheet (not declared in other files): '
                        + ', '.join(to_search) + '.')
        if in_drawings:
            request += ('\nViews to look for in the dimensioned drawings provided (not declared separately): '
                        + ', '.join(in_drawings) + '.')
    if include_nc_pins:
        request += (
            "\n\nNC pins: the pins marked NC (Not Connected) in the pinout table are "
            "nevertheless PHYSICALLY PRESENT on the component -- they are not connected "
            "electrically, but the pin is there. So include them in populated_pins: "
            "without their pad the footprint would not have the hole and the component would not "
            "fit into the board."
        )
    else:
        request += (
            "\n\nNC pins: exclude from populated_pins the pins marked NC (Not Connected), "
            "even if physically present."
        )
    if extra_instructions and extra_instructions.strip():
        request += (
            "\n\nSpecific notes on THIS component, provided by the user "
            "(they take precedence if they contradict what you deduce from the documents):\n"
            + extra_instructions.strip()
        )
    content_blocks.append({"type": "text", "text": request})

    def blocks_for(step):
        """The request of the main reading with each document sized for the model `step`.

        The same list when `step` is the main model; otherwise a copy whose
        document blocks are rebuilt for `step` (an image enlarged to ITS
        provider's limits, `_file_to_content_block`).
        """
        if step == main_model:
            return content_blocks
        blocks = list(content_blocks)
        for slot, path in file_slots:
            blocks[slot] = _file_to_content_block(path, step)
        return blocks

    has_pdf = any(p.suffix.lower() == ".pdf" for p in doc_paths)

    single_part = component_family in SINGLE_COMPONENT_FAMILIES

    def main_call(correction=None):
        """The paid reading of the documents (same request on a retry).

        A single part gets its own prompt and tool (pads one by one); the
        answer is brought to the main shape right here, so a retry and
        every later step see one shape only. `correction`, when given, is
        one more text block at the end of the request telling the model
        which part of the schema its previous answer broke.
        """
        blocks = content_blocks if correction is None else [
            *content_blocks, {"type": "text", "text": correction}]
        # The profile field of the main prompt; a text equal to its default
        # apart from blanks and line ends sends the default's bytes (V4-5),
        # and the request declares the field it sends (V4-6).
        # (`main_field` is resolved above, with the model of its step.)
        sent = effective_text(main_field, {main_field: system_prompt})
        with prompt_sending({main_field: sent}):
            answer = diagnostic_call(
                model=main_model,
                system_prompt=sent,
                content_blocks=blocks,
                tool=_single_component_tool() if single_part else _openai_tool(),
                max_tokens=16384,
                has_pdf=has_pdf,
            )
        # Keys beginning with «_» are the program's working state; the
        # model must not be able to set them (review V3-5).
        answer = without_private_keys(answer)
        return single_answer_to_data(answer) if single_part else answer
    try:
        try:
            data = main_call()
        except (ToolArgumentsError, AnswerShapeError) as exc:
            # The model answered with the right tool, a complete answer (a
            # truncated one does not get here), but unreadable arguments or
            # arguments whose shape contradicts the schema: the SAME request
            # may come back well formed. A single new paid attempt, like the
            # global inventory (HANDOFF 137; user, 2026-10-02: «ok procedi»
            # (ok, go ahead)). Refusals, network and credit do not pass here.
            _log_unreadable_arguments(exc, progress, 1)
            check_cancel()
            if progress:
                progress(tx("Analisi dei documenti: la risposta del modello non era un JSON "
                            "leggibile (testo nel log AI, riga AI_RESPONSE_UNREADABLE); un solo "
                            "nuovo tentativo a pagamento con la stessa richiesta.")
                         if isinstance(exc, ToolArgumentsError) else
                         tx("Analisi dei documenti: la risposta del modello non aveva la forma "
                            "richiesta ({exc}; testo nel log AI, riga AI_RESPONSE_UNREADABLE); un "
                            "solo nuovo tentativo a pagamento, con la richiesta e l'indicazione "
                            "dell'errore.", exc=exc))
            try:
                # A broken shape is repeated as often as not when the SAME
                # request is sent again (deepseek-flash, real calls of
                # 2026-10-31): the retry names the schema rule it broke.
                data = main_call(_SHAPE_CORRECTION if isinstance(exc, AnswerShapeError) else None)
            except ToolArgumentsError as second:
                _log_unreadable_arguments(second, progress, 2)
                raise ExtractionError(
                    tx("Anche il nuovo tentativo ha restituito argomenti illeggibili; il testo "
                       "delle due risposte e' nel log AI (righe AI_RESPONSE_UNREADABLE). {exc}",
                       exc=second)) from second
            except AnswerShapeError as second:
                _log_unreadable_arguments(second, progress, 2)
                raise ExtractionError(
                    tx("Anche il nuovo tentativo ha restituito una risposta senza la forma "
                       "richiesta; il testo delle due risposte e' nel log AI (righe "
                       "AI_RESPONSE_UNREADABLE). {exc}", exc=second)) from second
            if progress:
                progress(tx("Analisi dei documenti: il nuovo tentativo e' leggibile; si usa quello."))
    except OpenRouterError as exc:
        raise ExtractionError(str(exc)) from exc

    if not isinstance(data, dict):
        error = ExtractionError(tx("Risposta AI non strutturata."))
        _log_rejected_response(data, error, progress)
        raise error
    raw_main = copy.deepcopy(data)
    # The main answer as given travels into the result (ExtractedParams.raw_main)
    # for the partial re-runs (reread_body_height, reread_pad_positions).
    data["_raw_main"] = copy.deepcopy(raw_main)
    # The model that actually gave this answer (the main step's usable model,
    # not necessarily the single or session model the GUI knows): a partial
    # re-read compares its reader with it («one model reading twice is one
    # reading», H6). Written after `without_private_keys`, so a model answer
    # can never set it (RAW_MAIN_MODEL_KEY).
    data["_raw_main"][RAW_MAIN_MODEL_KEY] = main_model
    if step_models is None:
        # Before 2026-11-12: one dedicated reader, the profile's pad_model,
        # for both the pad positions and the body quotes.
        pad_reader = _pad_reading_model(pad_model, main_model, component_family, progress,
                                        openrouter_ready)
        readers = {"pad_model": pad_reader, "pad_model_requested": (pad_model or "").strip(),
                   # No note about the body reader: the pad note says it.
                   "body_model": pad_reader, "body_model_requested": ""}
    elif component_family in SINGLE_COMPONENT_FAMILIES:
        # Each dedicated reading of a single part has its own step; the
        # requested model is kept so the result can say it was replaced.
        # The readers are resolvers, called only when the dedicated reading
        # runs (finding 5, 2026-11-12): a part whose answer never reaches it
        # writes no fallback line and pays no credit check.
        readers = {"pad_model": lambda: step_model("pad_positions_prompt"),
                   "pad_model_requested": model_for("pad_positions_prompt", step_models,
                                                    main_model),
                   "body_model": lambda: step_model("body_dimensions_prompt"),
                   "body_model_requested": model_for("body_dimensions_prompt", step_models,
                                                     main_model)}
    else:
        # Only a single part has the dedicated pad and body readings.
        readers = {"pad_model": main_model, "pad_model_requested": "",
                   "body_model": main_model, "body_model_requested": ""}
    try:
        _validate_main_response(data, content_blocks, diagnostic_call, main_model, system_prompt,
                                has_pdf, progress,
                                numbering={"documents": numbering_documents(doc_paths, document_roles),
                                           "pdf_only": _numbering_only_in_pdf(doc_paths, document_roles),
                                           "component_family": component_family,
                                           "announce": announce, "check_cancel": check_cancel,
                                           "prompt": numbering_prompt,
                                           # The model of the dedicated numbering,
                                           # resolved when the call is made.
                                           "step_model": step_model,
                                           **readers,
                                           "library": _library_witness(
                                               part_number, component_family, progress),
                                           "answers": answers,
                                           # Profile texts of the dedicated pad and
                                           # body readings (None: defaults).
                                           "prompts": prompts})
    except ExtractionCancelled:
        raise
    except ExtractionError as exc:
        _log_rejected_response(raw_main, exc, progress)
        raise
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        # A shape no rule above anticipated: still an application error with
        # the answer preserved, never a raw exception out of the extraction.
        error = ExtractionError(tx("Risposta AI non valida: {exc}", exc=exc))
        _log_rejected_response(raw_main, error, progress)
        raise error from exc

    check_cancel()
    # The recheck runs only on a castellated answer with sides of unknown
    # style: its step model is resolved (and its fallback logged) only then.
    contact_model = (step_model("contact_style_prompt")
                     if _needs_contact_style_recheck(data, content_blocks, has_pdf) else main_model)
    _recheck_missing_contact_styles(data, blocks_for(contact_model), diagnostic_call,
                                   contact_model, has_pdf, progress,
                                   prompt=(prompts or {}).get("contact_style_prompt"))
    check_cancel()
    if progress:
        progress(
            tx("Analisi principale completata: layout={get}, componenti "
               "iniziali={n_items}.",
               get=data.get('layout', 'matrix'),
               n_items=len(data.get('components_3d') or []))
        )
    return _extract_photo_stage(
        data, doc_paths, progress=progress, announce=announce, check_cancel=check_cancel,
        cancelled=cancelled,
        document_roles=document_roles, component_family=component_family,
        photo_detector=photo_detector, vision_model=vision_model, model=model,
        verification_model=verification_model, diagnostic_call=diagnostic_call,
        extra_instructions=extra_instructions, photo_cache_dir=photo_cache_dir,
        grounding_prompt=grounding_prompt, verification_prompt=verification_prompt,
        terminal_prompt=terminal_prompt, crop_prompt=crop_prompt, crop_model=crop_model,
        yolo_model_path=yolo_model_path, smd_model_path=smd_model_path,
        review_proposals=review_proposals, yolo_confidence=yolo_confidence,
        yolo_settings=yolo_settings,
        recount_contacts=lambda disagreements: _recount_sides_from_photo(
            data, disagreements, content_blocks, diagnostic_call, main_model, system_prompt,
            has_pdf, progress),
        prompts=prompts, step_models=step_models)


#: System prompt of the optional contact-style recheck of a castellated board
#: (`_recheck_missing_contact_styles`, tool observe_contact_styles). Lifted out
#: of the function on 2026-11-11 (step P2, byte for byte: a test compares it
#: with the text of the previous commit) so that it is a profile prompt
#: ('contact_style_prompt', schema 9) the user sees and edits.
CONTACT_STYLE_PROMPT = (
    "Inspect only physical contact construction in the supplied photographs or drawings. "
    "Return edge names in canonical TOP VIEW (not the underside), matching the supplied sides. "
    "A half hole is a metallized opening cut by the board edge. A through hole is a "
    "complete enclosed plated hole with board material between it and the edge. "
    "Both can exist at the same contact. For each feature use true only if clearly visible; "
    "false only if its absence is clearly visible; otherwise null. If the edge is obscured, "
    "orientation uncertain, or contacts on that side differ, use null and low confidence. "
    "Do not infer style from component family, pin count, pad colour or previous defaults. "
    "Give high confidence only for clear evidence; cite the document and visible feature. "
    "Do not change counts, pitch, numbering or dimensions."
)


def _sides_missing_style(data) -> dict:
    """The castellated sides of `data` whose contact style is unknown, {edge: side}; empty otherwise."""
    if data.get("layout") != "castellated":
        return {}
    sides = data.get("castellated_sides") or []
    return {_side_edge(side): side for side in sides
            if isinstance(side, dict) and side.get("contact_style") is None
            and _side_edge(side) in ("top", "bottom", "left", "right")}


def _needs_contact_style_recheck(data, content_blocks, has_pdf) -> bool:
    """Whether `_recheck_missing_contact_styles` would make its paid call on `data`.

    True for a castellated answer with sides of unknown style and a request
    carrying visual evidence (a PDF or an image): a text-only request cannot
    provide new visual evidence. Lets the caller resolve the step's model
    only for a call that is made.
    """
    if not _sides_missing_style(data):
        return False
    return has_pdf or any(block.get("type") == "image_url" for block in content_blocks)


def _recheck_missing_contact_styles(data, content_blocks, diagnostic_call, model,
                                   has_pdf=False, progress=None, prompt=None):
    """One optional visual check; uncertain edges still require manual choice.

    Only missing styles can change. Counts, geometry, numbering and already
    observed styles are preserved. Replies use canonical top-view edge names.
    `prompt` is the profile's system prompt of the check (field
    'contact_style_prompt'); None or blank sends CONTACT_STYLE_PROMPT.
    `model` is the model of that step (the main reading's when no step
    model is chosen) and `content_blocks` the request sized for it.
    """
    if not _needs_contact_style_recheck(data, content_blocks, has_pdf):
        return
    missing = _sides_missing_style(data)
    message = "Ricontrollo AI del tipo di contatto sui lati non riconosciuti: " + ", ".join(missing)
    if progress:
        progress(tx("Ricontrollo AI del tipo di contatto sui lati non riconosciuti: {sides}",
                    sides=", ".join(missing)))
    properties = {
        "edge": {"type": "string", "enum": list(missing)},
        "half_hole_visible": {"type": ["boolean", "null"]},
        "through_hole_visible": {"type": ["boolean", "null"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "evidence": {"type": "string"},
    }
    tool = {"type": "function", "function": {
        "name": "observe_contact_styles",
        "description": "Observe physical board contact construction, without changing geometry.",
        "parameters": {"type": "object", "properties": {
            "sides": {"type": "array", "items": {"type": "object",
                "properties": properties, "required": list(properties),
                "additionalProperties": False}}}, "required": ["sides"],
            "additionalProperties": False}}}
    # The profile's text (field 'contact_style_prompt'); missing or blank
    # sends the factory text CONTACT_STYLE_PROMPT.
    # The profile text, or the default's bytes when blank or equal to the
    # default (V4-5); the request declares the field it sends (V4-6).
    prompt = effective_text("contact_style_prompt", {"contact_style_prompt": prompt})
    blocks = list(content_blocks) + [{"type": "text", "text":
        "Target sides in top view: " + repr(list(missing.values())) +
        ". Only return observe_contact_styles observations for these sides."}]
    try:
        with prompt_sending({"contact_style_prompt": prompt}):
            reply = diagnostic_call(model=model or DEFAULT_MODEL, system_prompt=prompt,
                                    content_blocks=blocks, tool=tool, max_tokens=1800,
                                    has_pdf=has_pdf)
    except OpenRouterError:
        if progress:
            progress(tx("Ricontrollo tipo di contatto non riuscito: resta la scelta manuale."))
        return
    observations = reply.get("sides") if isinstance(reply, dict) else None
    if not isinstance(observations, list):
        return
    grouped = {}
    for row in observations:
        if isinstance(row, dict):
            grouped.setdefault(_side_edge(row), []).append(row)
    for edge, side in missing.items():
        rows = grouped.get(edge, [])
        if len(rows) != 1:
            continue
        row = rows[0]
        half, through = row.get("half_hole_visible"), row.get("through_hole_visible")
        evidence = row.get("evidence")
        if (row.get("confidence") != "high" or not isinstance(evidence, str)
                or not evidence.strip() or type(half) is not bool or type(through) is not bool
                or not (half or through)):
            continue
        style = "half_and_through" if half and through else "half_hole" if half else "through_hole"
        side["contact_style"] = style
        side["_contact_style_evidence"] = evidence.strip()
        note = f"Tipo di contatto ricontrollato: {edge} = {style}. {evidence.strip()}"
        data["notes"] = (str(data.get("notes") or "") + "\n" + note).strip()
        if progress:
            progress(note)


#: Fields of a castellated side a photo recount may change: the number of its
#: contacts and how they are named and numbered. Pitch, offset, contact style
#: and pad sizes stay those of the main answer: the recount is asked about the
#: COUNT (HANDOFF point 139, G2 a), and a geometry changed on the way would be
#: adopted unchecked. (value_sources cover only lengths, so the provenance of
#: the side stays valid.)
_RECOUNT_FIELDS = ("count", "pin_names", "first_pin", "reverse")


#: Private mark set on each side of a recount reply before canonicalization:
#: True when the model wrote `reverse` itself (a boolean). The mirror of a
#: bottom view writes `reverse` on every horizontal side, so after
#: canonicalization its presence no longer says whether the model stated it.
_REVERSE_STATED = "_reverse_stated"


def _recounted_side(old: dict, reply: dict, count: int) -> dict:
    """The declared side with only the _RECOUNT_FIELDS taken from the reply.

    `first_pin` stays the old one when the reply gives none; `reverse` is the
    reply's only when the model stated it (_REVERSE_STATED), otherwise the
    declared one is kept: a reply that omits it says nothing about the
    numbering direction (HANDOFF point 139, R-M1); `pin_names` are the
    reply's, or none.
    """
    side = copy.deepcopy(old)
    side["count"] = count
    names = reply.get("pin_names")
    if isinstance(names, (list, tuple)) and names:
        side["pin_names"] = [str(name) for name in names]
    else:
        side.pop("pin_names", None)
    first = _optional_number(reply.get("first_pin"), positive=True, integer=True)
    if first is not None:
        side["first_pin"] = first
    if reply.get(_REVERSE_STATED) and isinstance(reply.get("reverse"), bool):
        if reply["reverse"]:
            side["reverse"] = True
        else:
            side.pop("reverse", None)
    return side


def _recount_refusal(side: dict, old: dict, reply: dict, item, data: dict) -> Optional[str]:
    """Why a recounted side cannot replace the declared one, or None when it can.

    Structural checks, each for a failure reproduced in the audit (HANDOFF
    points 138-139): names of another length than the count (they would be
    dropped in silence later), a side whose names the recount dropped, a side
    contradicting itself (_edge_contradictions), and -- the check that breaks
    the circle «the model now says what the photo said» -- contact centres
    that do not fall on the notch centres measured on the photo.

    The centres come from the geometry the footprint uses
    (contact_zones.contact_positions: the new count with the DECLARED pitch
    and offset). They are compared RELATIVELY (HANDOFF point 139, R-G2): the
    photo measures from the crop box, whose edge may sit off the board corner
    by the tilt find_board accepts, so the whole row may be shifted by the
    calibration alone. One common shift is allowed, up to
    EdgeCount.shift_bound_mm (edge_notches.row_shift_bound_mm, derived from
    the calibration contract; None = no shift allowed); after removing it,
    every centre must match within the photo's own tolerance
    (EdgeCount.tolerance_mm), so a row of another spacing is refused. A count
    that equals the photo's but whose row sits a notch off (half a pitch or
    more, e.g. the model dropped a contact hidden at an end of the row) is
    refused because its shift exceeds the bound.
    """
    count = side["count"]
    names = reply.get("pin_names")
    if isinstance(names, (list, tuple)) and names and len(names) != count:
        return tx("{n_names} nomi per {count} contatti", n_names=len(names), count=count)
    if old.get("pin_names") and not side.get("pin_names"):
        return tx("il ricontrollo non elenca i nomi dei contatti")
    length = _optional_number(data.get("body_length_mm"), positive=True)
    width = _optional_number(data.get("body_width_mm"), positive=True)
    edge = _side_edge(side)
    if any(problem_edge == edge for problem_edge, _ in _edge_contradictions([side], length, width)):
        return tx("il lato ricontato si contraddice")
    if length is None or width is None:
        return tx("quote della scheda mancanti")
    from .contact_zones import contact_positions
    from .edge_notches import row_offset_from
    along = "y_mm" if edge in ("left", "right") else "x_mm"
    start = width / 2 if edge in ("left", "right") else length / 2
    centres = sorted(row[along] + start
                     for row in contact_positions({"castellated_sides": [side]}, length, width)
                     if row.get("edge") == edge)
    measured = sorted(float(c) for c in (getattr(item, "centres_mm", None) or ()))

    def positive(value):
        if isinstance(value, bool) or not isinstance(value, (int, float))                 or not math.isfinite(value) or not value > 0:
            return 0.0
        return float(value)

    tolerance = positive(getattr(item, "tolerance_mm", None))
    bound = positive(getattr(item, "shift_bound_mm", None))
    if len(centres) != len(measured) or row_offset_from(centres, measured, tolerance,
                                                        float("inf")) is None:
        return tx("i contatti ricontati non cadono sui semi-fori misurati sulla foto "
                  "(centri {declared} mm contro {measured} mm)",
                  declared=", ".join(f"{c:.2f}" for c in centres),
                  measured=", ".join(f"{c:.2f}" for c in measured))
    shift = row_offset_from(centres, measured, tolerance, float("inf"))
    if abs(shift) > bound:
        return tx("la fila ricontata e' spostata di {shift:.2f} mm rispetto ai semi-fori "
                  "misurati, piu' di quanto la calibrazione della foto possa spostarla "
                  "({bound:.2f} mm)", shift=shift, bound=bound)
    return None


def _recount_sides_from_photo(data, disagreements, content_blocks, diagnostic_call, model,
                              system_prompt, has_pdf, progress) -> bool:
    """One paid recount of the sides whose half-holes the photo counts differently.

    User decision of 2026-09-28 (HANDOFF point 137): when the calibrated top
    photo shows another number of half-holes than a declared side, the model
    is asked once, with the same documents, to recount THOSE sides, and is
    told what the photo measured (count and notch centres).

    Adoption (HANDOFF point 139, G2), per disagreeing side:
    - only the recount fields change (_RECOUNT_FIELDS): count, pin_names,
      first_pin, reverse; pitch, offset, contact style, pad sizes and their
      provenance stay those of the main answer;
    - the recounted count must equal the photo's and the recounted contacts
      must sit on the measured notch centres (see _recount_refusal);
    - with that side replaced, the pad numbering of ALL sides must stay valid
      and contiguous (_castellated_sides, _numbering_is_contiguous);
    - sides the photo did not dispute are never changed, whatever the reply
      says about them.
    A side that fails keeps its declared contacts, the photo check keeps
    reporting the disagreement, and the log says why.

    @param data Validated main answer, in millimetres and seen from above
        (after `_canonicalize_answer`); updated in place.
    @param disagreements edge_notches.EdgeCount list (edge, found, declared,
        centres_mm, tolerance_mm).
    @return True when at least one side was replaced.
    @note The recount answer is canonicalized like the main one, so a model
        that answers in another unit or view is converted, not misread; the
        notes of that conversion are logged and, when a side is adopted, kept
        with the answer's own (`_canonical_notes`). A reply that does not say
        its view is read as the top view the request asks for, so no «DUBBIO
        vista» note is made up for it; a view it states but that is not
        recognized still gives that note.
    @note Failures are contained (HANDOFF point 139, minors): any exception of
        the call or of reading the reply (OpenRouterError, a malformed reply
        raising KeyError/ValueError, a truncated transfer...) is logged and
        the answer is kept unchanged -- the photo inventory goes on. Only a
        cancellation (CallCancelled, hence ExtractionCancelled) propagates.
    """
    say = progress or (lambda _message: None)
    try:
        return _recount_sides_attempt(data, disagreements, content_blocks, diagnostic_call,
                                      model, system_prompt, has_pdf, say)
    except CallCancelled:
        raise
    except Exception as exc:  # contenuto: vedi la @note sopra
        detail = str(exc) if isinstance(exc, OpenRouterError) else f"{type(exc).__name__}: {exc}"
        say(tx("Ricontrollo dei contatti non riuscito ({exc}): restano quelli dell'analisi.",
               exc=detail))
        return False


def _exclude_declared_sides(data, items, say) -> bool:
    """Drop declared castellated sides the photo says are not there (HANDOFF point 164).

    @param data Validated main answer, updated in place.
    @param items EdgeCount list of the sides to drop; `reason` says why.
    @param say Progress callback; the exclusion is reported as a WARNING.
    @return True when at least one side was dropped.
    @note The remaining sides keep their own numbering when it stays
        contiguous; otherwise they are renumbered in order of their first
        pin, and the note says so, because a footprint with a gap in the pad
        numbers is refused downstream. The excluded sides are named in
        `notes`, so whoever reads the result can put one back by hand when
        the photo, not the model, is wrong.
    """
    from .messages import as_warning
    edges = {item.edge: item for item in items}
    remaining = [entry for entry in (data.get("castellated_sides") or ())
                 if _side_edge(entry) not in edges]
    dropped = [entry for entry in (data.get("castellated_sides") or ())
               if _side_edge(entry) in edges]
    if not dropped:
        return False
    for entry in dropped:
        item = edges[_side_edge(entry)]
        say(as_warning(tx(
            "Lato {label}: l'analisi dichiarava {declared} contatti, ma sulla foto il bordo "
            "non ha nessuna tacca ({reason}): lato escluso. Se la foto inganna, "
            "aggiungilo a mano nella tabella dei lati.",
            label=EDGE_LABELS.get(item.edge, item.edge), declared=entry.get("count"),
            reason=item.reason or tx("nessuna tacca visibile sul bordo"))))
    renumbered = False
    if remaining and not _numbering_is_contiguous(copy.deepcopy(remaining)):
        ordered = sorted(remaining, key=lambda entry: (
            _optional_number(entry.get("first_pin"), positive=True, integer=True) or 0))
        next_pin = 1
        for entry in ordered:
            count = _optional_number(entry.get("count"), positive=True, integer=True)
            if count is None:
                continue
            entry["first_pin"] = next_pin
            next_pin += int(count)
        renumbered = True
    data["castellated_sides"] = remaining
    note = tx("Lati esclusi dopo il confronto con la foto (bordo senza tacche): {edges}.",
              edges=", ".join(EDGE_LABELS.get(edge, edge) for edge in edges))
    if renumbered:
        note += " " + tx("I pad dei lati rimasti sono stati rinumerati di seguito: verificare "
                         "la tabella pinout.")
    data["notes"] = (str(data.get("notes") or "") + "\n" + note).strip()
    say(note)
    return True


def _keep_clean_sides_with_copper(data, items, say) -> bool:
    """Keep the declared sides of a clean edge along which the photo shows a row of lands.

    @param data Validated main answer, updated in place (notes only).
    @param items EdgeCount list of clean edges with `copper_found` > 0.
    @param say Progress callback; reported as a WARNING to verify.
    @return True when a note was added.
    @note The outline count has a blind spot: a shallow half-hole whose
        plated barrel hides the background shows no notch (ESP32 devkit,
        2026-10-08: 22 contacts per edge dropped as «no notch» all day). A
        periodic row of identical lands on the edge (edge_lands.
        count_edge_copper) is the presence of contacts, so the side stays as
        declared; style and count are left to a person, because the photo
        could not read the holes. Nothing is adopted from the copper count.
    """
    from .messages import as_warning
    changed = False
    for item in items:
        for entry in data.get("castellated_sides") or ():
            if _side_edge(entry) != item.edge:
                continue
            message = tx("Lato {label}: la foto non mostra tacche sul bordo, ma una fila "
                         "periodica di {found} elementi (piazzole e spazi) al passo dichiarato "
                         "({declared} contatti dichiarati): lato tenuto, tipo di contatto e "
                         "conteggio da verificare a mano.",
                         label=EDGE_LABELS.get(item.edge, item.edge), found=item.copper_found,
                         declared=entry.get("count"))
            say(as_warning(message))
            data["notes"] = (str(data.get("notes") or "") + "\n" + message).strip()
            changed = True
    return changed


def _copper_count(item) -> Optional[int]:
    """The copper lands counted along a clean edge: an int >= 0, or None when not measured.

    A boolean or a non-integral value is not a count and reads as None, so
    it can never exclude a side.
    """
    value = getattr(item, "copper_found", None)
    if isinstance(value, bool) or not isinstance(value, numbers.Integral) or value < 0:
        return None
    return int(value)


def _keep_clean_sides_unmeasured(data, items, say) -> bool:
    """@brief Keep the declared sides of a clean edge whose copper strip could not be measured.

    @param data Validated main answer, updated in place (notes only).
    @param items EdgeCount list of clean edges with `copper_found` None.
    @param say Progress callback; each kept side is reported as a WARNING.
    @return True when at least one declared side was flagged.
    @note A straight outline alone is not proof of absence (a shallow
        half-hole shows no notch), and an unmeasurable copper strip is no
        evidence either way (H4): excluding the side would drop real
        contacts on missing data. Only a measured empty strip
        (`copper_found` == 0) excludes; here the side stays as declared and
        a person checks it, told why the photo could not decide.
    """
    from .messages import as_warning
    changed = False
    for item in items:
        for entry in data.get("castellated_sides") or ():
            if _side_edge(entry) != item.edge:
                continue
            message = tx("Lato {label}: la foto non mostra tacche sul bordo e il rame lungo il "
                         "bordo non si puo' misurare ({reason}): lato tenuto con i {declared} "
                         "contatti dichiarati, da verificare a mano.",
                         label=EDGE_LABELS.get(item.edge, item.edge),
                         reason=item.copper_reason or tx("striscia del bordo non misurabile"),
                         declared=entry.get("count"))
            say(as_warning(message))
            data["notes"] = (str(data.get("notes") or "") + "\n" + message).strip()
            changed = True
    return changed


def _recount_sides_attempt(data, disagreements, content_blocks, diagnostic_call, model,
                           system_prompt, has_pdf, say) -> bool:
    """Body of _recount_sides_from_photo; `data` is changed only at the very end.

    Three kinds of EdgeCount arrive (edge_notches.count_disagreements):
    * a CLEAN edge (`no_notch`): with no paid call, the declared side is
      excluded when the copper strip along it was measured empty
      (`copper_found` == 0), kept when a periodic row of lands is there, and
      kept flagged for review when the strip could not be measured (None);
    * an edge the photo cannot count (`found` None): the model is asked to
      look at the enlargement of THAT edge and to answer count 0 (or leave
      the side out) when it sees no plated hole cut by the edge; 0 excludes
      the side, any other count keeps the declared side with a warning that
      the photo could not verify it (the recount never adopts a number the
      photo cannot check);
    * another number of notches: the recount of HANDOFF point 137.
    """
    clean = [item for item in disagreements if item.no_notch]
    unverifiable = [item for item in disagreements if item.found is None and not item.no_notch]
    disagreements = [item for item in disagreements
                     if item.found is not None and not item.no_notch]
    # A straight outline is not proof of absence: a shallow half-hole whose
    # barrel hides the background shows no notch (ESP32 devkit, 2026-10-08:
    # 22 contacts per edge dropped as «no notch»). A periodic row of lands
    # on the edge is presence: such a side stays, flagged; the others go.
    # Only a MEASURED absence excludes a side: copper_found == 0 is a strip
    # read and found empty. None means the strip could not be measured (a
    # row too short to confirm, a strip off the photo): that is no evidence
    # either way, so the declared side stays, flagged for review (H4).
    absent = [item for item in clean if _copper_count(item) == 0]
    unmeasured = [item for item in clean if _copper_count(item) is None]
    for item in absent:
        if item.copper_reason:
            say(tx("Lato {label}: rame sul bordo: {reason}.",
                   label=EDGE_LABELS.get(item.edge, item.edge), reason=item.copper_reason))
    changed = _exclude_declared_sides(data, absent, say) if absent else False
    changed = _keep_clean_sides_with_copper(
        data, [item for item in clean if (_copper_count(item) or 0) > 0], say) or changed
    changed = _keep_clean_sides_unmeasured(data, unmeasured, say) or changed
    if not disagreements and not unverifiable:
        return changed
    facts = "; ".join(
        f"side {item.edge}: the calibrated photo seen from above shows {item.found} half holes "
        f"open on the edge, with the centres at {', '.join(f'{c:.2f}' for c in item.centres_mm)} mm "
        f"from the start of the edge (from the left for top/bottom, from the top for left/right); "
        f"the answer declared {item.declared}"
        for item in disagreements)
    doubts = "; ".join(
        f"side {item.edge}: the calibrated photo does not allow the half holes to be counted "
        f"({item.model_reason or 'not countable'}); the answer declared {item.declared}. "
        f"Look at the enlargement "
        f"of the {item.edge} edge: if you do NOT see plated holes cut by the edge, answer "
        f"count 0 for this side (or do not list it)"
        for item in unverifiable)
    summary = ", ".join(
        [f"{EDGE_LABELS.get(item.edge, item.edge)} {item.declared} -> {item.found}"
         for item in disagreements]
        + [f"{EDGE_LABELS.get(item.edge, item.edge)} {item.declared} -> ?"
           for item in unverifiable])
    say(tx("Contatti: la foto non conferma l'analisi ({summary}). Richiesta di ricontarli sugli "
           "stessi documenti: una chiamata in piu'.", summary=summary))
    comparison = "Comparison with the photograph: " + "; ".join(part for part in (facts, doubts) if part)
    blocks = list(content_blocks) + [{"type": "text", "text": (
        comparison + ". Recount on the documents (pinout, dimensioned "
        "drawing, photo) the PHYSICAL contacts of these sides: count is their number, pin_names "
        "lists exactly one per contact in the order along the edge; the labels of the LEDs "
        "or of other components are not contacts. The photo can be wrong only if a half hole "
        "is covered: if the documents show another number, keep it and explain it in notes, in English. "
        "Return the complete answer with the sides seen from above (contacts_view=\"top\") "
        "and the dimensions in millimetres (drawing_unit=\"mm\"). Current sides, seen from above and in "
        f"mm: {data.get('castellated_sides')!r}")}]
    repaired = diagnostic_call(model=model or DEFAULT_MODEL,
                               system_prompt=system_prompt or _SYSTEM_PROMPT,
                               content_blocks=blocks, tool=_openai_tool(),
                               max_tokens=16384, has_pdf=has_pdf)
    if not isinstance(repaired, dict) or not isinstance(repaired.get("castellated_sides"), list):
        say(tx("Il ricontrollo dei contatti non ha restituito lati utilizzabili: restano "
               "quelli dell'analisi."))
        return changed
    repaired = copy.deepcopy(repaired)
    repaired["layout"] = "castellated"
    # Private keys: a model answer is never already converted.
    repaired.pop("_canonical", None)
    repaired.pop("_canonical_notes", None)
    if repaired.get("contacts_view") is None and repaired.get("pin_map_view") is None:
        # The request asks for the top view: an answer that does not state
        # the view is read that way, without the DOUBT of the main answer.
        repaired["contacts_view"] = "top"
    for entry in repaired["castellated_sides"]:
        if isinstance(entry, dict):
            entry[_REVERSE_STATED] = isinstance(entry.get("reverse"), bool)
    _canonicalize_answer(repaired)
    conversion = [str(note) for note in repaired.get("_canonical_notes") or ()]
    for note in conversion:
        say(tx("Ricontrollo dei contatti: {note}", note=note))
    edges = [_side_edge(entry) for entry in repaired["castellated_sides"] if _side_edge(entry)]
    # A side listed twice does not say which count applies: it is not adopted.
    recounted = {_side_edge(entry): entry for entry in repaired["castellated_sides"]
                 if _side_edge(entry) and edges.count(_side_edge(entry)) == 1}
    merged = list(data.get("castellated_sides") or ())
    index = {_side_edge(entry): i for i, entry in enumerate(merged) if _side_edge(entry)}
    length = _optional_number(data.get("body_length_mm"), positive=True)
    width = _optional_number(data.get("body_width_mm"), positive=True)
    # Sides not verifiable on the photo: zero (or a missing side) excludes
    # them, a number leaves them declared with the warning. A new number is never adopted.
    from .messages import as_warning
    not_there = []
    for item in unverifiable:
        reply = recounted.get(item.edge)
        count = (_optional_number(reply.get("count"), integer=True)
                 if isinstance(reply, dict) else None)
        if reply is None or count == 0:
            not_there.append(EdgeCount(item.edge, 0, item.declared, [], reason=tx(
                "ricontrollo AI sull'ingrandimento del bordo: nessun foro tagliato dal bordo")))
        else:
            say(as_warning(tx(
                "Lato {label}: {declared} contatti dichiarati; la foto non permette di "
                "contarli ({reason}) e il ricontrollo AI li conferma: lato da verificare a "
                "mano.", label=EDGE_LABELS.get(item.edge, item.edge), declared=item.declared,
                reason=item.reason)))
    if not_there:
        changed = _exclude_declared_sides(data, not_there, say) or changed
        merged = list(data.get("castellated_sides") or ())
        index = {_side_edge(entry): i for i, entry in enumerate(merged) if _side_edge(entry)}
    adopted, confirmed = [], []
    for item in disagreements:
        reply, position = recounted.get(item.edge), index.get(item.edge)
        count = (_optional_number(reply.get("count"), positive=True, integer=True)
                 if isinstance(reply, dict) else None)
        if position is None or count != item.found:
            confirmed.append(item.edge)
            continue
        old = merged[position]
        side = _recounted_side(old, reply, count)
        reason = _recount_refusal(side, old, reply, item, data)
        if reason is None:
            trial = merged[:position] + [side] + merged[position + 1:]
            try:
                _castellated_sides(copy.deepcopy(trial), body_length=length, body_width=width)
            except ExtractionError as exc:
                reason = tx("numerazione dei pad incoerente ({exc})", exc=exc)
            else:
                if not _numbering_is_contiguous(copy.deepcopy(trial)):
                    reason = tx("la numerazione dei pad di tutti i lati avrebbe un buco")
        if reason is not None:
            say(tx("Ricontrollo dei contatti non adottato per il lato {label}: {reason}; "
                   "restano quelli dell'analisi.",
                   label=EDGE_LABELS.get(item.edge, item.edge), reason=reason))
            continue
        merged[position] = side
        adopted.append(item.edge)
    if confirmed and not adopted:
        say(tx("Ricontrollo dei contatti: l'AI conferma i contatti dichiarati; il disaccordo "
               "con la foto resta da verificare."))
    if not adopted:
        return changed
    data["castellated_sides"] = merged
    if conversion:
        data["_canonical_notes"] = list(data.get("_canonical_notes") or ()) + [
            tx("Ricontrollo dei contatti: {note}", note=note) for note in conversion]
    note = tx("Contatti ricontati dopo il confronto con la foto: {edges}. Verificare la "
              "tabella pinout prima dell'uso.",
              edges=", ".join(EDGE_LABELS.get(edge, edge) for edge in adopted))
    data["notes"] = (str(data.get("notes") or "") + "\n" + note).strip()
    say(note)
    return True


def _side_contradictions(raw, body_length, body_width) -> list[str]:
    """Self-contradictions of the castellated sides of ONE answer, for the model.

    Structural, not tuned: a side cannot hold a different number of contacts
    than the names it lists for them, and (count - 1) * pitch contact centres
    cannot run past the edge they sit on. Both sides of each comparison come
    from the same answer, in its own drawing unit. Sides the normalization
    would discard are left to it.
    """
    return [problem for _edge, problem in _edge_contradictions(raw, body_length, body_width)]


def _edge_contradictions(raw, body_length, body_width) -> list[tuple[str, str]]:
    """Same checks as _side_contradictions, each problem paired with its edge (English, for the model)."""
    return [(edge, model_text) for edge, model_text, _ui_text
            in _edge_problems(raw, body_length, body_width)]


def _edge_problems(raw, body_length, body_width) -> list[tuple[str, str, str]]:
    """The self-contradictions of the castellated sides as (edge, model text, log text).

    The model text is fixed English (user, 2026-11-12: every text sent to a
    model in English); the log text keeps the Italian wording the progress
    line has always shown inside its translated frame.
    """
    try:
        sides = _normalized_castellated_sides(raw)
    except ExtractionError:
        return []
    names, ends = {}, {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        edge = str(entry.get("edge") or "").strip().lower()
        if isinstance(entry.get("pin_names"), (list, tuple)) and entry["pin_names"]:
            names[edge] = len(entry["pin_names"])
        first = _optional_number(entry.get("first_contact_from_edge_mm"), positive=False)
        last = _optional_number(entry.get("last_contact_from_edge_mm"), positive=False)
        if first is not None and last is not None and first >= 0 and last >= 0:
            ends[edge] = (first, last)
    problems = []
    for side in sides:
        edge, count = side["edge"], side["count"]
        if names.get(edge, count) != count:
            problems.append((edge, f"side {edge}: count={count} but pin_names has "
                                   f"{names[edge]} entries",
                             f"lato {edge}: count={count} ma pin_names ha "
                             f"{names[edge]} voci"))
        edge_length = body_width if edge in ("left", "right") else body_length
        span = (count - 1) * side["pitch_mm"]
        if edge_length is not None and span > edge_length + _PIN_FIELD_TOLERANCE_MM:
            formula = f"(count-1) x pitch_mm = ({count}-1) x {side['pitch_mm']:g} = {span:.2f}"
            problems.append((edge, f"side {edge}: {formula}, more than the edge, which is "
                                   f"{edge_length:g} long",
                             f"lato {edge}: {formula}, piu' del bordo lungo {edge_length:g}"))
        # A row of N contacts at pitch P spans (N-1)*P from centre to centre;
        # a drawing that quotes the first and the last contact from the two
        # ends of the edge (a, b) fixes that span to L-a-b. When the nearest
        # integer N to that span is not the declared count, the answer
        # contradicts its own quotes (ESP32 devkit, 2026-10-08: 21 declared
        # against 63.3-5.05-4.91 = 21 x 2.54, i.e. 22). Rounding boundary,
        # not a threshold: beyond half a pitch another integer is nearer.
        # Without both quotes nothing is checked, nothing is inferred.
        if edge in ends and edge_length is not None and side["pitch_mm"] > 0:
            first, last = ends[edge]
            quoted = edge_length - first - last
            if quoted < 0:
                problems.append((edge, f"side {edge}: the dimensions of the first and last contact "
                                       f"from the edges ({first:g} + {last:g}) exceed the edge, which "
                                       f"is {edge_length:g} long",
                                 f"lato {edge}: le quote del primo e dell'ultimo contatto "
                                 f"dai bordi ({first:g} + {last:g}) superano il bordo lungo "
                                 f"{edge_length:g}"))
            elif abs(quoted - span) > side["pitch_mm"] / 2:
                expected = round(quoted / side["pitch_mm"]) + 1
                formula = (f"({edge_length:g} - {first:g} - {last:g}) / {side['pitch_mm']:g} "
                           f"+ 1 = {expected}")
                problems.append((edge, f"side {edge}: the dimensions of the drawing give "
                                       f"{formula} contacts, but count={count}",
                                 f"lato {edge}: le quote del disegno danno "
                                 f"{formula} contatti, ma count={count}"))
    return problems


def _numbering_is_contiguous(raw) -> bool:
    """True when the sides, numbered exactly as the extraction will number them, leave no gap.

    Judged on the output of _castellated_sides (discarded entries dropped,
    missing first_pin assigned), so what is checked is what will be drawn.
    A list that numbering refuses (overlap, repeated edge, nothing usable) is
    not contiguous. Ranges are compared as intervals, never materialized.
    """
    try:
        sides = _castellated_sides(raw)
    except ExtractionError:
        return False
    ranges = sorted((int(side["first_pin"]), int(side["first_pin"]) + side["count"])
                    for side in sides)
    return all(stop == start for (_a, stop), (start, _b) in zip(ranges, ranges[1:]))


def _side_edge(entry) -> str:
    """Edge name of one raw castellated side, as _side_contradictions keys it."""
    return str(entry.get("edge") or "").strip().lower() if isinstance(entry, dict) else ""


def _recount_contradictory_sides(data, content_blocks, diagnostic_call, model, system_prompt,
                                 has_pdf, progress, dims) -> None:
    """One paid recount of castellated sides that contradict themselves (user, 2026-09-26).

    The model is not deterministic: the same documents once gave 22 contacts
    per side, once 28 with 22 names on a 63.3 mm edge. Failing there wasted the
    whole analysis; guessing the count from the names would not be safer (the
    two sides listed 22 and 23). The contradiction is sent back with the same
    documents, its exact numbers and the edge lengths; the recounted sides
    replace the old ones, provenance included. Whatever is still wrong is
    handled as before: contacts that do not fit are an error, names of the
    wrong length are dropped with a note. At most one extra call.

    Only the CONTRADICTED edges are replaced. The recount returns every side,
    and a side that was coherent is not made more reliable by being answered
    twice: on a real run one edge failed by 0.04 mm and the recount also
    changed the coherent opposite edge from 6 to 5 contacts, unchecked.
    Changes the recount proposes for coherent edges are reported, not adopted
    -- unless the merged pad numbering would have a gap or an overlap
    (first_pin of one edge depends on the counts of the others): then the
    recount is adopted whole, as before, and the warning says so. A recount
    that lists an edge twice is also passed whole, so the normalization
    refuses it as it always did.
    """
    contradicted = _edge_problems(data.get("castellated_sides"), **dims)
    if not contradicted:
        return
    wrong_edges = {edge for edge, _model_text, _ui_text in contradicted}
    # The model reads the English wording, the log the wording it always
    # showed (user, 2026-11-12: prompts in English, UI unchanged).
    summary = "; ".join(model_text for _edge, model_text, _ui_text in contradicted)
    log_summary = "; ".join(ui_text for _edge, _model_text, ui_text in contradicted)
    if progress:
        progress(tx("Risposta AI incoerente sui contatti ({summary}). Richiesta di ricontarli "
                    "sugli stessi documenti: una chiamata in piu'.", summary=log_summary))
    unit = data.get("drawing_unit") or "mm"
    repair_blocks = list(content_blocks) + [{"type": "text", "text": (
        "The previous answer contradicts itself on the contacts of the castellated sides: "
        f"{summary}. Recount on the documents (pinout and dimensioned drawing) the PHYSICAL contacts of "
        "every side: count is their number, pin_names lists exactly one per "
        "contact in the order along the edge, and (count-1) x pitch_mm must fit in the edge "
        f"(body_length_mm={dims['body_length']} for the top and bottom sides, "
        f"body_width_mm={dims['body_width']} for left and right). If the drawing dimensions the "
        "distances of the first and last contact from the edges (first_contact_from_edge_mm, "
        "last_contact_from_edge_mm), then count = (edge - first - last) / pitch_mm + 1: "
        "report them and respect them. Use the same unit as the "
        f"previous answer (drawing_unit={unit}). Do not invent values that the documents "
        "do not give: if you cannot establish them, omit the names and explain it in notes, in "
        "English. Return "
        f"the complete answer. Previous sides: {data.get('castellated_sides')!r}"
    )}]
    try:
        repaired = diagnostic_call(model=model or DEFAULT_MODEL,
                                   system_prompt=system_prompt or _SYSTEM_PROMPT,
                                   content_blocks=repair_blocks, tool=_openai_tool(),
                                   max_tokens=16384, has_pdf=has_pdf)
    except OpenRouterError as error:
        raise ExtractionError(tx("Ricontrollo dei contatti non riuscito; analisi foto non "
                                 "avviata.")) from error
    sides = repaired.get("castellated_sides") if isinstance(repaired, dict) else None
    repaired_unit = repaired.get("drawing_unit") if isinstance(repaired, dict) else None
    if not isinstance(sides, list) or not sides or repaired_unit not in (None, unit):
        # Sides missing or in another unit: not comparable with the body and
        # pitch of the main answer, which stay as they are.
        raise ExtractionError(tx("Il ricontrollo dei contatti non ha restituito lati "
                                 "utilizzabili; analisi foto non avviata."))
    recounted_edges = [_side_edge(entry) for entry in sides if _side_edge(entry)]
    recounted = {_side_edge(entry): entry for entry in sides if _side_edge(entry)}
    previous = data.get("castellated_sides")
    previous = list(previous) if isinstance(previous, list) else []
    before = {_side_edge(entry): entry for entry in previous if _side_edge(entry)}
    # Coherent edges the recount would change: normally said, not adopted.
    # A coherent edge is one the first answer DESCRIBED without contradiction.
    # An edge the recount adds was not described at all: it is part of the
    # repair, not a change to something coherent (ESP32 devkit, 2026-10-08:
    # the first answer put the two 22-contact rows on left/right, where
    # 53.34 mm cannot fit in 25.4; the recount moved them to top/bottom and
    # was refused as «changing coherent sides», so the extraction failed).
    changed = sorted(edge for edge, entry in recounted.items()
                     if edge not in wrong_edges and edge in before and any(
                         entry.get(key) != before[edge].get(key)
                         for key in ("count", "pitch_mm", "first_pin", "reverse")))
    # A contradicted edge the recount no longer lists does not exist: the
    # recount is the answer to that contradiction, and keeping the impossible
    # row would only fail validation again.
    merged = [recounted[_side_edge(entry)] if _side_edge(entry) in wrong_edges
              else entry for entry in previous
              if _side_edge(entry) not in wrong_edges or _side_edge(entry) in recounted]
    # Edges absent from the first answer can only come from the recount.
    merged.extend(entry for edge, entry in recounted.items() if edge not in before)
    whole = (len(recounted_edges) != len(set(recounted_edges))
             or not _numbering_is_contiguous(merged))
    # The recount as a whole, as before this rule, in two cases: it lists an
    # edge twice (the normalization then refuses it with its own message,
    # instead of a silent «last one wins»), or the pad numbering of the merged
    # sides has a gap or an overlap -- first_pin of one edge depends on the
    # counts of the others, so mixing two answers can silently lose a pad.
    data["castellated_sides"] = sides if whole else merged
    warning = tx("Contatti dei lati ricontati dopo una risposta AI incoerente ({summary}); "
                 "verificare conteggi e nomi prima dell'utilizzo.", summary=log_summary)
    if changed and whole:
        warning += " " + tx("Il ricontrollo cambiava anche lati che erano coerenti ({edges}) e la "
                            "numerazione dei contatti dipende da tutti i lati: adottato il "
                            "riconteggio intero, verificare anche quei lati.",
                            edges=", ".join(changed))
    elif changed:
        warning += " " + tx("Il ricontrollo cambiava anche lati che erano coerenti ({edges}): "
                            "modifica non adottata, restano quelli della prima risposta.",
                            edges=", ".join(changed))
    data["notes"] = (str(data.get("notes") or "") + "\n" + warning).strip()
    if progress:
        progress(warning)


def _cancel_predicate(cancel):
    """Normalize the cancellation channel to a zero-argument predicate, or None."""
    if cancel is None:
        return None
    if hasattr(cancel, "is_set"):
        return cancel.is_set
    if callable(cancel):
        return lambda: bool(cancel())
    raise ExtractionError(tx("Canale di annullamento non valido."))


def _sanitise_printed_quotes(data: dict, progress) -> None:
    """Keep only the valid printed profile quotes of the main answer (value and role), and log them.

    The list stays in the answer's own unit as [{"value", "measures"}]
    (`printed_quotes` normalises objects and bare numbers alike); the body
    numbers with their declared provenance go to the log before any check
    (bench evidence).
    """
    data["printed_vertical_quotes"] = [
        {"value": value, "measures": role}
        for value, role in printed_quotes(data.get("printed_vertical_quotes"))]
    if progress:
        sources = data.get("value_sources") if isinstance(data.get("value_sources"), dict) else {}
        progress(tx("Quote del corpo nella risposta principale: {values}; quote verticali "
                    "stampate elencate: {printed}.", values=", ".join(
            f"{key} {data.get(key)} ({sources.get(key) or 'senza provenienza'})"
            for key in ("body_length_mm", "body_width_mm", "body_height_mm", "pin_length_3d_mm")
            if data.get(key) is not None) or "-",
            printed=", ".join(f"{q['value']:g} ({q['measures']})"
                              for q in data["printed_vertical_quotes"]) or "-"))


def _validate_main_response(data, content_blocks, diagnostic_call, model, system_prompt,
                            has_pdf, progress, numbering=None) -> None:
    """Sanitize and validate the main answer in place; ExtractionError when unusable.

    `numbering`: None, or the inputs of the dedicated pin-numbering call
    (`read_pin_numbering`): documents, component_family, announce,
    check_cancel, prompt. The call is made for a matrix answer of a single
    part when there is a drawing to read.

    Body dimensions are sanitized with a note; castellated sides that
    contradict themselves (count against pin_names, contacts longer than
    their edge) get one paid recount, numbering conflicts get one paid
    repair, and the complete answer must then build ExtractedParams.
    """
    if numbering:
        # What the structured questions need: the drawing names as evidence,
        # the answers saved by an earlier analysis (asked nothing twice).
        data["_question_documents"] = [p.name for p in numbering.get("documents") or ()]
        if isinstance(numbering.get("answers"), dict):
            data["_answers"] = dict(numbering["answers"])
    # Validate geometry before it can be used to calibrate a photograph.
    _sanitise_printed_quotes(data, progress)
    for key in ("body_length_mm", "body_width_mm", "body_height_mm"):
        original = data.get(key)
        data[key] = _optional_number(original, positive=True)
        if original is not None and data[key] is None:
            warning = tx("{key} scartato: dimensione non valida (serve un numero positivo "
                         "finito, non booleano).",
                         key=key)
            data["notes"] = (str(data.get("notes") or "") + "\n" + warning).strip()
            if progress:
                progress(warning)
    dims = dict(body_length=data.get("body_length_mm"), body_width=data.get("body_width_mm"))
    if data.get("layout") == "castellated":
        _recount_contradictory_sides(data, content_blocks, diagnostic_call, model,
                                     system_prompt, has_pdf, progress, dims)
        try:
            _castellated_sides(data.get("castellated_sides"), **dims)
        except _PinNumberConflict as exc:
            if progress:
                progress(tx("Numerazione contatti incoerente: {exc}. Richiesta correzione prima "
                            "dell'analisi foto.",
                            exc=exc))
            repair_blocks = list(content_blocks) + [{"type": "text", "text": (
                "Correct only the physical numbering of the contacts of the previous answer. "
                "Compare the original pinout: GPIOs and electrical labels are pin_names, not pad numbers. "
                "first_pin is the minimum of the sequence; reverse inverts its spatial order. "
                "Do not change sides, counts, geometry or the order of the labels. "
                "If the documents do not establish the numbering, omit first_pin on ALL the sides "
                "to adopt a progressive convention explicitly to be verified. "
                # The English twin of the conflict, never the UI-language
                # message (user, 2026-11-12).
                f"Conflict: {end_sentence(getattr(exc, 'model_text', '') or str(exc))} "
                f"Previous sides: {data.get('castellated_sides')!r}"
            )}]
            try:
                repaired = diagnostic_call(model=model or DEFAULT_MODEL,
                    system_prompt=system_prompt or _SYSTEM_PROMPT,
                    content_blocks=repair_blocks, tool=_openai_tool(),
                    max_tokens=16384, has_pdf=has_pdf)
                corrected = _castellated_sides(repaired.get("castellated_sides"), **dims)
                # Normalized against normalized: the raw original may hold
                # entries the normalization drops, or edges written
                # differently, which are not changes made by the correction.
                original = _normalized_castellated_sides(data["castellated_sides"])
                if len(corrected) != len(original) or any(
                    row["edge"] != old["edge"] or row["count"] != old["count"]
                    for row, old in zip(corrected, original)
                ):
                    raise ExtractionError(tx("La correzione della numerazione ha cambiato i lati o i conteggi."))
                # The normalized sides drop the declared provenance: carry it
                # over by edge (edges are unique), or every value of the
                # repaired sides would read as undeclared.
                provenance = {str(entry.get("edge") or "").strip().lower(): entry["value_sources"]
                              for entry in data["castellated_sides"]
                              if isinstance(entry, dict) and "value_sources" in entry}
                data["castellated_sides"] = [
                    dict(old, first_pin=row["first_pin"], reverse=row.get("reverse", False),
                         **({"value_sources": provenance[old["edge"]]}
                            if old["edge"] in provenance else {}))
                    for old, row in zip(original, corrected)]
                warning = tx("Numerazione contatti corretta dopo conflitto AI; verificare la "
                             "tabella pinout prima dell'utilizzo.")
                data["notes"] = (str(data.get("notes") or "") + "\n" + warning).strip()
                if progress:
                    progress(warning)
            except (OpenRouterError, AttributeError) as repair_error:
                raise ExtractionError(tx("Correzione numerazione non riuscita; analisi foto non avviata.")) from repair_error
    if (numbering and numbering.get("documents")
            and (data.get("layout") or "matrix") == "matrix" and not data.get("package_geometry")
            and numbering.get("component_family") != "aggregate_tht"):
        # Second, dedicated reading of the pin numbering (user, 2026-10-10):
        # the drawing alone, one question. Before the canonicalisation, so
        # both readings are in the drawing's own view and get mirrored once.
        documents = numbering["documents"]
        rows = _optional_number(data.get("rows"), positive=True, integer=True)
        cols = _optional_number(data.get("cols"), positive=True, integer=True)
        if rows and cols:
            numbering["check_cancel"]()
            # The model of the numbering step (2026-11-12), resolved now that
            # the call is made; without a resolver, the main model as before.
            resolve = numbering.get("step_model")
            reader = resolve("numbering_prompt") if resolve else model or DEFAULT_MODEL
            numbering["announce"]("llm", reader)
            if progress:
                progress(tx("Lettura dedicata della numerazione dei pin su {names}.",
                            names=", ".join(p.name for p in documents)))
            reading = read_pin_numbering(
                documents, rows=rows, cols=cols, model=reader,
                call=diagnostic_call, prompt=numbering.get("prompt"), progress=progress)
        else:
            reading = {"view": "unknown", "pins": [], "notes": "",
                       "discarded": tx("griglia righe x colonne non valida, nessuna chiamata")}
        apply_dedicated_numbering(data, reading, progress)
    elif (numbering and numbering.get("documents") and _has_explicit_pads(data)
            and numbering.get("component_family") in SINGLE_COMPONENT_FAMILIES):
        # Explicit pads of a single part: a second, dedicated reading of the
        # hole positions, compared by distances (user, 2026-10-10, VE12H-K).
        documents = numbering["documents"]
        # A reader is a model name or a resolver called here, when the
        # reading runs (finding 5, 2026-11-12: no eager fallback lines).
        reader = _reader_model(numbering.get("pad_model")) or model or DEFAULT_MODEL
        # The body quotes have their own step since 2026-11-12 (the pad
        # reader before): absent, the pad reader as before.
        body_reader = _reader_model(numbering.get("body_model")) or reader
        # One model reading twice is one reading (H6): said to the pad
        # confrontation (pad reader) and to the height tie-break (body reader).
        same_model = reader == (model or DEFAULT_MODEL)
        body_same_model = body_reader == (model or DEFAULT_MODEL)
        numbering["check_cancel"]()
        numbering["announce"]("llm", reader)
        if progress:
            progress(tx("Lettura dedicata delle posizioni dei pad su {names} con {model}.",
                        names=", ".join(p.name for p in documents), model=reader))
        # The profile's texts of the two dedicated readings (blank: defaults).
        pad_prompt = effective_text("pad_positions_prompt", numbering.get("prompts"))
        body_prompt = effective_text("body_dimensions_prompt", numbering.get("prompts"))
        reading = read_pad_positions(
            documents, model=reader, call=diagnostic_call, progress=progress, prompt=pad_prompt,
            pad_sizes=_pad_sizes_mm(data["package_geometry"].get("pins"),
                                    _declared_unit_factor(data)))

        def view_of():
            # The main answer's view, or the dedicated one when the main left
            # it unknown: the readings compared share it (apply_dedicated_pads
            # leaves the dedicated reading out when the two views differ).
            view = data.get("contacts_view")
            return view if view in ("top", "bottom") else (reading or {}).get("view")
        # The body outline drawn around the holes places the body on an axis
        # with no outline quote (_write_chain_pads, step 3); a callable in the
        # answer for the writers, removed right after.
        data["_outline_measurer"] = _outline_measurer(documents)
        try:
            apply_dedicated_pads(data, reading, progress, check_layout=_layout_checker(
                documents, numbering.get("library"), view_of,
                _reading_tolerance(_declared_unit_factor(data))),
                measure_layout=_layout_measurer(documents), same_model=same_model)
        finally:
            data.pop("_outline_measurer", None)
        if reader != (numbering.get("pad_model_requested") or reader):
            # Not two different models any more: the result says so, not only the log.
            data["notes"] = (str(data.get("notes") or "") + "\n" + tx(
                "Posizioni dei pad lette dal modello principale {model} al posto di "
                "{requested}, non utilizzabile.", model=reader,
                requested=numbering["pad_model_requested"])).strip()
        # The body quotes get the same second reading (user, 2026-11-03): the
        # outline sizes and the height from the view that prints them,
        # compared with the main answer by apply_dedicated_body.
        numbering["check_cancel"]()
        numbering["announce"]("llm", body_reader)
        if progress:
            progress(tx("Lettura dedicata delle quote del corpo su {names} con {model}.",
                        names=", ".join(p.name for p in documents), model=body_reader))
        def tie_break():
            # A model OTHER than the main one (review 2026-11-04, M13): the
            # dedicated reader on the enlarged page is a third, independent
            # transcription; when that reader IS the main model (blank pad
            # model, or a provider not ready) the third witness does not
            # exist and no call is paid for a re-reading by the same model.
            if body_same_model:
                if progress:
                    progress(tx("Spareggio sull'altezza non disponibile: il lettore dedicato e' il "
                                "modello principale {model}, una sua terza lettura non sarebbe "
                                "indipendente.", model=body_reader))
                return None
            numbering["check_cancel"]()
            numbering["announce"]("llm", body_reader)
            if progress:
                progress(tx("Spareggio sull'altezza: terza lettura delle quote del corpo su {names} a "
                            "riquadri ingranditi con {model}.",
                            names=", ".join(p.name for p in documents), model=body_reader))
            return read_body_dimensions(documents, model=body_reader, call=diagnostic_call,
                                        prompt=body_prompt, progress=progress, enlarged=True)

        apply_dedicated_body(data, read_body_dimensions(documents, model=body_reader,
                                                        call=diagnostic_call,
                                                        prompt=body_prompt, progress=progress),
                             progress, tie_break=tie_break)
        body_requested = numbering.get("body_model_requested") or body_reader
        pad_note_says_it = (body_requested == numbering.get("pad_model_requested")
                            and body_reader == reader)
        if body_reader != body_requested and not pad_note_says_it:
            # As for the pads: the result says the body quotes were not read
            # by the model chosen for their step. Not when both steps asked
            # the same model and both fell back to the same reader: the pad
            # note above already says it (finding 5, 2026-11-12).
            data["notes"] = (str(data.get("notes") or "") + "\n" + tx(
                "Quote del corpo lette dal modello principale {model} al posto di "
                "{requested}, non utilizzabile.", model=body_reader,
                requested=numbering["body_model_requested"])).strip()
    elif (numbering and _has_explicit_pads(data)
            and numbering.get("component_family") in SINGLE_COMPONENT_FAMILIES):
        # No drawing image to read the positions from (only a PDF, or no
        # drawing role): nothing confirms them, so they block like any
        # unconfirmed reading (user, 2026-10-30; adversarial review G1).
        # No drawn holes to check either; the library footprints, when the
        # part number names some, can still confirm the main answer's geometry.
        apply_dedicated_pads(data, {"view": "unknown", "pads": {}, "notes": "", "skipped": tx(
            "nessun disegno in immagine con ruolo pinout o vista dall'alto: la lettura dedicata "
            "non si puo' fare (un PDF non si ingrandisce)")}, progress,
            check_layout=_layout_checker([], numbering.get("library"),
                                         lambda: data.get("contacts_view"),
                                         _reading_tolerance(_declared_unit_factor(data))))
        # No body reading follows on this path: its marks end here.
        for mark in ("_body_axes_measured", "_body_axes_printed"):
            data.pop(mark, None)
    if (numbering and _has_explicit_pads(data)
            and numbering.get("component_family") in SINGLE_COMPONENT_FAMILIES):
        # After the readings, which may have adopted a view: an unknown view
        # that would mirror the footprint is asked, never assumed.
        _require_declared_view(data, progress)
    elif (numbering and numbering.get("pdf_only")
            and (data.get("layout") or "matrix") == "matrix" and not data.get("package_geometry")
            and numbering.get("component_family") != "aggregate_tht"):
        data["_numbering_note"] = tx(
            "Numerazione dei pin NON confermata da una lettura dedicata: il disegno e' "
            "disponibile solo in PDF, che non si puo' ingrandire. Verificare la posizione dei "
            "pin sul disegno.")
        if progress:
            progress(data["_numbering_note"])
    if (data.get("layout") or "matrix") in ("matrix", "castellated"):
        # After the numbering repair, which compares sides in the drawing's own
        # unit and view, and before the photo stage, which calibrates on the
        # body dimensions and masks the declared contacts: both must read
        # millimetres seen from above.
        _canonicalize_answer(data)
        if progress:
            for note in data["_canonical_notes"]:
                progress(note)
    # After the possible correction, so the lines describe the final sides:
    # unobserved contact style and estimated values go into the log at once,
    # and stay in the notes of the result (_params_from_tool_arguments).
    params = _params_from_tool_arguments(data)
    if progress:
        for line in _review_notes(params.castellated_sides, params.value_sources):
            progress(line)


def _extract_photo_stage(data, doc_paths, *, progress, announce, check_cancel, document_roles,
                         component_family, photo_detector, vision_model, model,
                         verification_model, diagnostic_call, extra_instructions,
                         photo_cache_dir, grounding_prompt, verification_prompt,
                         terminal_prompt, yolo_model_path, review_proposals,
                         yolo_confidence, yolo_settings,
                         recount_contacts=None, crop_prompt=None,
                         crop_model=None, cancelled=None,
                         smd_model_path=None, prompts=None,
                         step_models=None) -> ExtractedParams:
    """Optional photo inventory and final assembly of a validated main answer.

    `cancelled` is the zero-argument cancellation predicate of extract_params
    (None: no cancellation channel); the SMD network polls it between crops.
    `prompts` is the profile's {field: text} of extract_pin_header_params,
    forwarded to extract_inventory only when given (None: the defaults, and
    an inventory function that predates the keyword keeps working).
    `step_models` is the step-model table of extract_pin_header_params,
    forwarded the same way (only when given) to extract_inventory and
    observe_terminals, which resolve the model of each photo step at their
    own call sites.
    """
    # The photo needs a separate pass: in the global extraction the model
    # must also read dimensions, pinout and layout, and tends to sacrifice the
    # small passives. The crops are best-effort: if they fail, the global result stays.
    photo_report = None
    top_index = data.get('top_photo_index')
    if document_roles is not None:
        # User-assigned roles are authoritative. Never calibrate the wrong view.
        # Only the files declared photographs (PHOTO_ROLE) go to the
        # inventory, all of them: it picks the most detailed one it can
        # calibrate. A dimensioned drawing is not a photograph, whatever
        # other role the same file carries (user decision 2026-09-26).
        top_index = photo_document_indices(doc_paths, document_roles)
        if progress:
            chosen = ', '.join(f'{doc_paths[i].name} ({tx(_role_label(PHOTO_ROLE))})'
                               for i in top_index if i >= 0)
            progress(tx('Viste dall’alto disponibili: ') + (
                chosen + tx(". Verrà usata la più dettagliata fra quelle calibrabili.")
                if chosen else tx("nessuna; nessun’altra vista verrà usata al suo posto.")))
    populated_board = data.get("layout") == "castellated" or component_family == "aggregate_tht"
    if (populated_board and data.get("body_length_mm")
            and data.get("body_width_mm")):
        from .photo_inventory import ReviewCancelled, extract_inventory
        from .photo_grounding import DEFAULT_VISION_MODEL
        # The local detector phase is NOT announced here: the contract of
        # `extract_inventory` (see photo_inventory.extract_inventory, @param
        # phase) is that 'yolo' arrives immediately before the detector, and
        # here we are before the calibration. Announcing it now showed a phase
        # that might never start -- without a calibratable view the inventory
        # exits before loading the ONNX -- and the modal stayed on the local
        # detector for the rest of the run.
        # Nor is the paid call to the vision model ('ai', 'vlm') announced
        # here (point 139, C1): first comes the OpenCV calibration, which
        # `extract_inventory` announces itself, and the early announcement
        # stayed as an empty «✓» phase immediately covered by OpenCV.
        # 'llm_inventory' is announced by `extract_inventory` when the photo starts.
        check_cancel()
        try:
            photo_report = extract_inventory(
                doc_paths, vision_model or DEFAULT_VISION_MODEL, data["body_length_mm"],
                data["body_width_mm"], COMPONENT_KINDS, diagnostic_call,
                progress=progress, phase=announce, top_photo_index=top_index,
                **({'top_photo_source': 'utente'} if document_roles is not None else {}),
                extra_instructions=extra_instructions,
                cache_dir=photo_cache_dir,
                classifier_model=verification_model or model or DEFAULT_MODEL,
                grounding_prompt=grounding_prompt, verification_prompt=verification_prompt,
                **({'detector': photo_detector, 'yolo_model_path': yolo_model_path,
                    'review_proposals': review_proposals, 'declared': data,
                    'yolo_confidence': yolo_confidence, 'yolo_settings': yolo_settings,
                    'recount_contacts': recount_contacts, 'crop_prompt': crop_prompt,
                    'crop_model': crop_model}
                   if photo_detector in ('yolo', 'vlm', 'yolo_smd') else {}),
                **({'cancel': cancelled} if photo_detector == 'yolo_smd' and cancelled is not None
                   else {}),
                **({'smd_model_path': smd_model_path}
                   if photo_detector == 'yolo_smd' and smd_model_path is not None else {}),
                **({'prompts': prompts} if prompts is not None else {}),
                **({'step_models': step_models} if step_models is not None else {}),
            )
        except ReviewCancelled as exc:
            # A cancellation is not an inventory failure: continuing here
            # would mean paying the reconciliation on the rejected proposals.
            raise ExtractionError(str(exc)) from exc
        except ExtractionCancelled:
            raise
        except CallCancelled as exc:
            # Cancellation raised outside a paid call (SMD network, KiCad
            # matching): same documented type as every other cancellation.
            raise ExtractionCancelled(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - the inventory is optional
            # Any inventory failure (even KeyError/TypeError from a malformed
            # answer) must not lose the main answer, already paid and valid:
            # it becomes a note, with the full traceback in the log for
            # whoever has to fix it.
            # The error is swallowed into the notes and processing continues
            # without inventory: the announced phase must be closed, otherwise
            # the modal stays for the whole rest of the run on the last tool
            # announced by the inventory (the local detector, if the ONNX file
            # vanished between validation and detection) while that tool is no
            # longer working. Empty tool = no phase claimed, not a wrong phase.
            announce("", "")
            if progress:
                import traceback
                from ..crash_report import scrub
                # The AI log may be attached to a public issue: home folder,
                # user name and keys are masked as in a crash report.
                progress("AI_PHOTO_INVENTORY_TRACEBACK " + scrub("".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()))
            data["notes"] = (str(data.get("notes") or "") + "\n"
                             + tx("Inventario fotografico fallito: {exc}", exc=exc)).strip()
        if photo_report and photo_report.calibration.get('inventory_strategy') == 'review-kept-none':
            # An explicit empty review is authoritative, unlike a failed or
            # unavailable inventory: do not resurrect the initial AI bodies.
            data["components_3d"] = []
        if photo_report and photo_report.components:
            # The global YOLO reconciliation already reports terminal evidence.
            # Further paid crop calls are an explicit optional UI action only.
            if photo_detector not in ('yolo', 'vlm', 'yolo_smd') and isinstance(getattr(photo_report, "photo_png", None), bytes):
                from .photo_terminals import observe_terminals
                check_cancel()
                try:
                    photo_report.components = observe_terminals(
                        photo_report.photo_png, photo_report.components, vision_model or DEFAULT_VISION_MODEL,
                        diagnostic_call, progress or (lambda message: None),
                        photo_cache_dir,
                        **({'system_prompt': terminal_prompt} if terminal_prompt else {}),
                        **({'step_models': step_models} if step_models is not None else {}),
                    )
                except (OpenRouterError, ValueError, OSError) as exc:
                    photo_report.warnings.append(tx("Dettaglio terminali non disponibile: {exc}", exc=exc))
            data["components_3d"] = photo_report.components
            old_notes = str(data.get("notes") or "").strip()
            detailed = sum(bool(c.get("terminal_geometry")) for c in photo_report.components)
            suffix = (tx("Inventario fotografico: {n_components} ingombri su {photo}; "
                         "{detailed} con terminali osservati. Modello meccanico semplificato, "
                         "senza marcature. XY stimati dalla foto calibrata; altezze indicative, "
                         "non misure certificate: verificare con una misura diretta.",
                         n_components=len(photo_report.components),
                         photo=photo_report.photo,
                         detailed=detailed))
            data["notes"] = f"{old_notes} {suffix}".strip()
        if photo_report and photo_report.warnings:
            details = "\n".join(photo_report.warnings[:6])
            if len(photo_report.warnings) > 6:
                details += tx("\nAltri {value} avvisi nel log AI.", value=len(photo_report.warnings) - 6)
            data["notes"] = (str(data.get("notes") or "") + "\n" + details).strip()
    elif progress and populated_board:
        progress(tx("Secondo passaggio fotografico non avviato: mancano "
                    "body_length/body_width."))

    # Only the inventory built by the program is trusted with internal flags;
    # without it, data["components_3d"] is still the raw AI answer.
    params = _params_from_tool_arguments(
        data, trusted_components=bool(photo_report and photo_report.components))
    if populated_board and not (photo_report and photo_report.components):
        for index, component in enumerate(params.components_3d or [], 1):
            component.update(kind="generic", name=f"Ingombro {index}", marking="",
                             confidence="low", _uncertain=True,
                             _height_source="global_visual_estimate_not_measured")
        if params.components_3d:
            params.notes = (str(params.notes or "") + tx("\nIngombri dall'estrazione globale, senza verifica fotografica e senza "
                                                         "marcature; altezze indicative, non misure certificate.")).strip()
    if photo_report and photo_report.components:
        params.photo_overlay_png = photo_report.overlay_png
        params.photo_png = photo_report.photo_png
        params.photo_source = photo_report.photo
    for message in fit_body_to_pin_field(params, component_family):
        if progress:
            progress(message)
    if progress:
        progress(usage_totals().describe())
    return params


#: Tolerance, in mm, with which a pin centre on the edge counts as inside:
#: numerical rounding only, not a physical margin.
_PIN_FIELD_TOLERANCE_MM = 1e-6


def _pin_field_positions(params) -> list:
    """Positions (x, y) in mm of the pins that exist, as the footprint draws them.

    Same function as the footprint and the 3D pins (`pin_header.pin_positions`),
    so populated_pins and an explicit pin_map count exactly as they will be
    drawn. Empty when the grid is not usable.
    """
    from ..generators.pin_header import PinHeaderParams, pin_positions
    if int(params.rows) > 1 and not params.row_spacing_mm:
        # Without a declared row spacing the rows would sit one pitch apart:
        # a guess, and `fit_body_to_pin_field` must not correct the body from
        # it (review 2026-11-03, pitch never reused as row spacing).
        return []
    mapping = None
    if params.pin_map:
        # Keyed on the printed label: pin_header compares pins as text, so
        # «A1» and 1 are both valid keys (an int() here dropped every
        # alphanumeric map).
        mapping = {entry["pin"]: (int(entry["row"]), int(entry["col"]))
                   for entry in params.pin_map}
    grid = PinHeaderParams(
        # With one column every pin is at x = 0 whatever the pitch, and a single
        # column declares none: 1.0 is a placeholder with no geometric effect.
        rows=int(params.rows), cols=int(params.cols),
        pitch=float(params.pitch_mm) if params.pitch_mm else 1.0,
        row_spacing=params.row_spacing_mm or None,
        populated_pins=set(params.populated_pins) if params.populated_pins else None,
        pin_map=mapping)
    try:
        return [(x, y) for _, x, y in pin_positions(grid)]
    except (TypeError, ValueError, KeyError):
        return []


def fit_body_to_pin_field(params, component_family) -> list[str]:
    """@brief Keep every pin of a populated through-hole board inside the board.

    @param params ExtractedParams of «matrix» layout; corrected in place.
    @param component_family Family of the chosen component type (a declared
        attribute, not the name of the part).
    @return The messages to show in the log: empty when there was nothing to say.
    @note Only for `aggregate_tht`: on a populated through-hole board the
        pins go through the holes OF the board, so every pin centre lies
        inside its outline. This is a physical constraint, not an estimate. A
        single THT component may instead have legs outside its body, and is
        left untouched.
    @note Case seen by the user on 2026-09-24 (TEST4): the AI gave
        `body_offset_y_mm = -12.7`, half the width, measuring from the centre
        of the board instead of from the first row, and the second row ended
        up outside. When the pin field leaves the board but would fit in it,
        the offset of THAT axis is re-centred: it is an ESTIMATE (the edge was
        not dimensioned consistently) and is written in the notes, in the log
        and in `params.value_sources` ("estimated"). When the board is
        smaller than the pin field no value is right: no correction, only the
        warning about inconsistent dimensions.
    """
    if component_family != "aggregate_tht" or params.is_castellated():
        return []
    rows, cols, pitch = params.rows, params.cols, params.pitch_mm
    length, width = params.body_length_mm, params.body_width_mm
    if not all(isinstance(v, (int, float)) and v > 0 for v in (rows, cols, length, width)):
        return []
    if cols > 1 and not (isinstance(pitch, (int, float)) and pitch > 0):
        return []
    positions = _pin_field_positions(params)
    if not positions:
        return []
    messages = []
    for axis, index, extent, key in (("X", 0, length, "body_offset_x_mm"),
                                     ("Y", 1, width, "body_offset_y_mm")):
        offset = getattr(params, key)
        if offset is None:
            continue
        # Field of the PRESENT pins (populated_pins, pin_map), not of the
        # declared matrix: an empty column at the end is not a pin off the
        # board (audit 25/09: a correct offset was being «corretto»
        # (corrected)). The pins occupy [low, high] on the axis; the board
        # [offset, offset + extent].
        low = min(point[index] for point in positions)
        high = max(point[index] for point in positions)
        span = high - low
        if (offset <= low + _PIN_FIELD_TOLERANCE_MM
                and offset + extent >= high - _PIN_FIELD_TOLERANCE_MM):
            continue
        if extent + _PIN_FIELD_TOLERANCE_MM < span:
            messages.append(tx(
                "ATTENZIONE quote incoerenti sull'asse {axis}: i pin occupano {span:.2f} mm "
                "ma la scheda misura {extent:.2f} mm. Nessuna correzione automatica: "
                "verificare le quote.", axis=axis, span=span, extent=extent))
            continue
        centred = round(low - (extent - span) / 2, 3)
        setattr(params, key, centred)
        # The program chose this value: it is an estimate, whatever the
        # model declared for the value it replaces.
        params.value_sources = dict(params.value_sources or {}, **{key: "estimated"})
        messages.append(tx(
            "ATTENZIONE spostamento del corpo sull'asse {axis} corretto da {old:.2f} a "
            "{new:.2f} mm: con il valore dell'analisi una fila di pin cadeva fuori dalla "
            "scheda. Valore STIMATO (campo dei pin centrato sulla scheda): verificarlo "
            "con una quota.", axis=axis, old=offset, new=centred))
    if messages:
        params.notes = (str(params.notes or "") + "\n" + "\n".join(messages)).strip()
    return messages


def _real_number(value) -> Optional[float]:
    """A finite real number as float, or None; numeric TEXT is never a number.

    Delegates to the shared `finite_number` (review D4: "12" used to pass as a
    coordinate). Real scalars that are not Python int/float, such as the
    numpy values the program's own photo pipeline may carry into trusted
    components, are converted first: they are numbers, unlike text.
    """
    if (isinstance(value, numbers.Real) and not isinstance(value, (bool, int, float))):
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
    return finite_number(value)


#: Name given to an extracted package whose AI-proposed name is not a valid
#: file name. Never kept: the window replaces every package name with the
#: project folder's (the folder is the component's identity).
EXTRACTED_PACKAGE_PLACEHOLDER_NAME = "Package"

#: Pad shapes an older schema offered that PinSpec no longer accepts, mapped to
#: what KiCad actually drew for them (review 2026-10-27, G7: a trapezoid needs
#: `rect_delta`, which the AI never gave, so it was always written as a rectangle).
LEGACY_EXTRACTED_PAD_SHAPES = {"trapezoid": "rect"}


def _pads_without_positions(package_geometry) -> list[str]:
    """The pads of an explicit geometry whose position was not read, as their number text.

    A pad is unread when `x_mm` or `y_mm` is null or absent: the way a model
    says it did not read the value (every field is nullable in DeepSeek's
    strict mode). A value that IS present is not judged here: text, booleans
    and non-finite numbers stay for `package_from_mapping` to reject, as any
    malformed pad. Entries that are not objects are left to it too.

    @return The numbers ("?" for a pad without one), in answer order; empty
        when every pad has both coordinates or there is no pad list.
    """
    pins = package_geometry.get("pins") if isinstance(package_geometry, dict) else None
    if not isinstance(pins, (list, tuple)):
        return []
    return [_pad_number_text(pin.get("number")) or "?" for pin in pins
            if isinstance(pin, dict) and (pin.get("x_mm") is None or pin.get("y_mm") is None)]


def _pads_without_holes(package_geometry) -> list[str]:
    """The holed pads of an explicit geometry left without a hole, as their number text.

    After `_resolve_pad_terminals` a through-hole (or NPTH) pad has no
    `drill_mm` only when the drawing printed no hole and no terminal was
    declared, neither by the pad nor for the part (review E1): an unread
    value, never invented, so the pads are UNREAD geometry exactly like
    pads without a position.

    @return The numbers ("?" for a pad without one), in answer order.
    """
    pins = package_geometry.get("pins") if isinstance(package_geometry, dict) else None
    if not isinstance(pins, (list, tuple)):
        return []
    return [_pad_number_text(pin.get("number")) or "?" for pin in pins
            if isinstance(pin, dict) and pin.get("pad_type", "smd") in _HOLED_PAD_TYPES
            and pin.get("drill_mm") is None]


#: Pad sizes an answer may carry: each one present must be a positive length.
_PAD_SIZE_KEYS = ("pad_length_mm", "pad_width_mm", "drill_mm", "drill_length_mm")

#: Pad fields that ARE objects by contract (judged by their own rule).
_PAD_OBJECT_KEYS = ("lead", "value_sources", "sources")

#: The SCALAR pad fields the pad rules and the footprint read: position,
#: sizes, hole, slot, number, shape, type and rotation. A list or an object
#: in one of them is a malformed value the rules would have to read: an
#: error of the answer. Every other pad field is outside this contract
#: (`_pads_with_the_fields_read`).
_PAD_READ_SCALAR_KEYS = ("number", "x_mm", "y_mm", "pad_length_mm", "pad_width_mm", "drill_mm",
                         "drill_length_mm", "shape", "pad_type", "rotation_deg")

#: The fields of the geometry itself that `package_from_mapping` reads and
#: validates (a malformed value there is an error of the answer).
_GEOMETRY_READ_KEYS = ("name", "family", "body_length_mm", "body_width_mm", "body_height_mm",
                       "origin", "source_view", "orientation_deg", "mirrored", "confidence",
                       "needs_review", "pins", "lead_form")

#: Informative pad fields: text the footprint only carries along (`name`,
#: `side`) or the program consumes as text (`hole_callout`). Never geometry.
_PAD_TEXT_KEYS = ("name", "side", "hole_callout")

#: Largest length, mm, a terminal section may have: KiCad's own coordinate
#: range (`core.footprint.KICAD_COORD_LIMIT_MM`, the bound the footprint
#: applies to every pad). Not a threshold on parts: a lead wider than what
#: KiCad can represent describes no pad, and its estimate overflows a float.
_MAX_LEAD_MM = KICAD_COORD_LIMIT_MM

#: Shape of a pad whose answer gives none: the default of the footprint
#: itself (`PinSpec.shape`, the one `package_from_mapping` applies), read
#: from there so that the clearance rules and the footprint never assume
#: two different pads (review round 5, V1-7).
_DEFAULT_PAD_SHAPE = PinSpec.__dataclass_fields__["shape"].default


def _malformed_geometry(reason: str) -> "ExtractionError":
    """The readable «Geometria package non valida» error of a malformed explicit geometry."""
    return ExtractionError(tx("Geometria package non valida: {exc}", exc=reason))


def _json_safe(value) -> bool:
    """Whether a value survives `json.dumps(allow_nan=False)`: text, booleans, finite numbers, lists and str-keyed objects of them."""
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, (list, tuple)):
        return all(_json_safe(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_safe(item) for key, item in value.items())
    return False


def _pad_text(value) -> Optional[str]:
    """An informative pad field as text, or None when it carries none.

    Text stays; a finite number becomes its text; a list of such values
    (a model that wraps one name in a list) becomes them joined by «, ».
    An object, a boolean, a non-finite number or a nested list is no text.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value) if math.isfinite(value) else None
    if isinstance(value, (list, tuple)) and value:
        parts = [_pad_text(item) for item in value]
        if all(isinstance(item, (str, int, float)) and not isinstance(item, bool) for item in value) \
                and None not in parts:
            return ", ".join(parts)
    return None


def _pads_with_the_fields_read(package_geometry, notes: list):
    """A copy of an explicit geometry whose pads carry nothing the rules do not read in a shape that could harm (V1-2).

    A paid answer is never lost to a field the pad rules do not read
    (review round 5, V1-2: «layers», «nets», an unknown key holding a list
    used to refuse the whole answer). Per pad:
    - the fields of `_PAD_READ_SCALAR_KEYS` and `_PAD_OBJECT_KEYS` are left
      exactly as answered (`_reject_malformed_pad_sizes` judged them);
    - an informative field (`_PAD_TEXT_KEYS`) that is not text is brought
      to text (`_pad_text`) or dropped;
    - any other field stays only when it is a plain scalar a project file
      can store (text, boolean, finite number): a list, an object or a
      non-finite number there is dropped.
    The same holds for the fields of the geometry itself that are not read
    by `package_from_mapping`. ONE note names what was dropped or rewritten.

    @param package_geometry The explicit geometry (already through
        `_reject_malformed_pad_sizes`), or None.
    @param notes Receives the one note.
    @return A new geometry; `package_geometry` itself when it is not one.
    """
    if not isinstance(package_geometry, dict) or not isinstance(package_geometry.get("pins"), (list, tuple)):
        return package_geometry
    geometry = dict(package_geometry)
    changed: list = []
    for key in [key for key in geometry if key not in _GEOMETRY_READ_KEYS]:
        value = geometry[key]
        if isinstance(value, (list, tuple, dict)) or not _json_safe(value):
            del geometry[key]
            changed.append(str(key))
    pins = []
    for pin in geometry["pins"]:
        if not isinstance(pin, dict):
            pins.append(pin)
            continue
        clean = {}
        for key, value in pin.items():
            if (key in _PAD_READ_SCALAR_KEYS or key in _PAD_OBJECT_KEYS
                    or key in ("_hole_rounding_mm", "_lead_rounding_mm")):
                clean[key] = value
            elif key in _PAD_TEXT_KEYS:
                text = value if value is None else _pad_text(value)
                if text is not None or value is None:
                    clean[key] = text
                if text is not value:
                    changed.append(str(key))
            elif isinstance(value, (list, tuple, dict)) or not _json_safe(value):
                changed.append(str(key))
            else:
                clean[key] = value
        pins.append(clean)
    geometry["pins"] = pins
    if changed:
        notes.append(tx("Campi dei pad fuori contratto ignorati o ridotti a testo ({fields}): non "
                        "sono fra i valori che il programma legge.",
                        fields=", ".join(dict.fromkeys(changed))))
    return geometry


def _reject_malformed_pad_sizes(package_geometry) -> None:
    """Refuse an explicit geometry of a malformed SHAPE or with a malformed size, before any size is derived.

    The program's one rule (`_resolve_pad_terminals`) walks the pads and may
    replace a land or a hole the model gave with one derived from the
    terminal. Checked here, first, so that neither a malformed container
    reaches that walk as an AttributeError/TypeError (review F7-5, F6-8) nor
    a malformed value vanishes behind an estimate:
    - `package_geometry` must be an object and its `pins` a list of objects;
    - no scalar field the rules READ (`_PAD_READ_SCALAR_KEYS`) may hold a
      list or an object; the terminal and the provenance maps
      (`_PAD_OBJECT_KEYS`) are judged by their own rule, and every OTHER
      field is not judged at all (review round 5, V1-2: a list in «layers»
      or in an unknown key refused a paid answer) --
      `_pads_with_the_fields_read` drops or rewrites it afterwards;
    - a pad `number` is text or a finite number, never a boolean or a
      non-finite number (it would name the pad «nan»);
    - every size present must be a finite number > 0. Absent (None) values
      are not checked: a missing size is what the rule exists to fill. A
      `drill_mm` or `drill_length_mm` of 0 is how an answer says «no hole»
      or «no slot» (review F7-3, V1-6), on any pad type: it is absent, and
      `_resolve_one_pad` drops it. A negative, non-finite, boolean or text
      value anywhere stays an error.

    @param package_geometry The explicit geometry as answered, or None.
    @throws ExtractionError «Geometria package non valida» naming the pad and
        the field, the same error the footprint validation gives.
    """
    if package_geometry is None:
        return
    if not isinstance(package_geometry, dict):
        raise _malformed_geometry(tx("package_geometry deve essere un oggetto, non {kind}",
                                     kind=type(package_geometry).__name__))
    pins = package_geometry.get("pins")
    if not isinstance(pins, (list, tuple)):
        raise _malformed_geometry(tx("pins deve essere una lista di pad, non {kind}",
                                     kind=type(pins).__name__))
    for index, pin in enumerate(pins, 1):
        if not isinstance(pin, dict):
            raise _malformed_geometry(tx("l'elemento {index} di pins non e' un oggetto", index=index))
        raw_number = pin.get("number")
        number = "?" if isinstance(raw_number, (list, tuple, dict, set)) or raw_number is None else raw_number
        for key in _PAD_READ_SCALAR_KEYS:
            if isinstance(pin.get(key), (list, tuple, dict, set)):
                raise _malformed_geometry(tx(
                    "pin {number}: {name} deve essere un valore semplice, non una lista o un oggetto",
                    number=number, name=key))
        if isinstance(raw_number, bool) or (isinstance(raw_number, float) and not math.isfinite(raw_number)):
            raise _malformed_geometry(tx("pin {number}: {name} deve essere un testo o un numero finito",
                                         number="?", name="number"))
        shape = pin.get("shape")
        if shape is not None and shape not in PAD_SHAPES and shape not in LEGACY_EXTRACTED_PAD_SHAPES:
            # The rule may replace the shape of a land it derives: an unknown
            # shape is refused here, with the footprint's own message.
            raise _malformed_geometry(tx("shape non valida: {shape}", shape=shape))
        for key in _PAD_SIZE_KEYS:
            value = pin.get(key)
            if value is None or (key.startswith("drill") and _is_zero(value)):
                continue
            if _optional_number(value, positive=True) is None:
                raise _malformed_geometry(tx("pin {number}: {name} deve essere un numero maggiore di zero",
                                             number=number, name=key))


def _validate_pads_alone(package_geometry: dict, unsettled=()) -> None:
    """Validate every value an explicit geometry carries, pad by pad, apart from what is merely UNREAD.

    The well-formedness half of the final validation (audit 2026-11-09, M1;
    review round 4): a malformed number, coordinate, size, pad type, shape,
    terminal or package field is an error of the answer whatever else the
    geometry lacks. Each pad is validated alone, as a footprint of one pad,
    with stand-ins for what is unread and never for what is malformed: a
    missing coordinate is 0; a holed pad left without a hole
    (`_pads_without_holes`), or whose land cannot hold its hole
    (`unsettled`, the problems of `_resolve_pad_terminals`), is checked
    with a hole of half its smaller side. The stand-ins never leave this
    check. What pads do to EACH OTHER (overlaps) is not judged here.

    @param package_geometry The mapping (`_package_mapping_for_validation`).
    @param unsettled Numbers of the pads whose hole does not fit their land.
    @throws KeyError, TypeError, ValueError As `package_from_mapping`.
    """
    for pin in package_geometry.get("pins") or []:
        if isinstance(pin, dict):
            pin = dict(pin)
            for key in ("x_mm", "y_mm"):
                if pin.get(key) is None:
                    pin[key] = 0.0
            sizes = [_optional_number(pin.get(key), positive=True) for key in ("pad_length_mm", "pad_width_mm")]
            if (pin.get("pad_type", "smd") in _HOLED_PAD_TYPES and None not in sizes
                    and (pin.get("drill_mm") is None
                         or (_pad_number_text(pin.get("number")) or "?") in unsettled)):
                pin["drill_mm"] = min(sizes) / 2
                pin.pop("drill_length_mm", None)
        package_from_mapping({**package_geometry, "pins": [pin]})


def _cross_pad_problem(package_geometry: dict, skip=()) -> Optional[str]:
    """What the placed, settled pads of a geometry do to each other that a footprint refuses, or None.

    The consistency half of the final validation: the pads with both
    coordinates and a hole that fits (numbers not in `skip`), each already
    valid alone (`_validate_pads_alone`), validated TOGETHER as the footprint
    (`package_from_mapping`: overlapping copper, pads of one number with
    different names). Such a geometry is made of well-formed values that
    cannot all be true: it is returned as a reason, for the caller to drop
    the pads as unread geometry with a block, never as an exception that
    would lose the paid answer.

    @return The validator's message, or None when the pads validate.
    """
    pins = [pin for pin in package_geometry.get("pins") or []
            if isinstance(pin, dict) and pin.get("x_mm") is not None and pin.get("y_mm") is not None
            and not (pin.get("pad_type", "smd") in _HOLED_PAD_TYPES and pin.get("drill_mm") is None)
            and (_pad_number_text(pin.get("number")) or "?") not in skip]
    if not pins:
        return None
    try:
        package_from_mapping({**package_geometry, "pins": pins})
    except (KeyError, TypeError, ValueError) as exc:
        return str(exc)
    return None


def _validated_explicit_geometry(package_geometry: dict, notes: list) -> tuple:
    """Validate an explicit pad geometry as the footprint; return (mapping, doubts).

    @param package_geometry The explicit geometry (canonical mm), every pad
        with both coordinates.
    @param notes Receives the label notes and the structural doubts.
    @return (the mapping as validated, `geometry_doubts` list or None).
    @throws ExtractionError When the geometry does not validate.
    """
    mapping = _package_mapping_for_validation(package_geometry, notes)
    try:
        # Validate now, while the source response and its warnings are still
        # available; the GUI must never receive malformed pad coordinates.
        geometry = package_from_mapping(mapping)
    except (KeyError, TypeError, ValueError) as exc:
        raise ExtractionError(tx("Geometria package non valida: {exc}", exc=exc)) from exc
    # Structural contradictions (body over the pads, clockwise numbering,
    # bottom view also declared mirrored) are exposed, never repaired:
    # which reading is true is a question for the user (audit 2026-10-15).
    doubts = [doubt.as_dict() for doubt in package_doubts(geometry)] or None
    notes.extend(doubt["message"] for doubt in doubts or ())
    return mapping, doubts


def _kept_after_pad_drop(params) -> str:
    """What of the answer still stands once its explicit pads were dropped, as one phrase.

    Built from the fields the result really carries (audit 2026-11-09, M2):
    a single-part answer keeps drill, pad size and pitch inside its pads,
    so those fall with them and are not named.
    """
    kept = []
    if any(getattr(params, key) is not None
           for key in ("body_length_mm", "body_width_mm", "body_height_mm")):
        kept.append(tx("corpo"))
    if params.castellated_sides:
        kept.append(tx("lati del modulo"))
    if params.drill_mm is not None:
        kept.append(tx("foro"))
    if params.pad_diameter_mm is not None:
        kept.append(tx("diametro del pad"))
    if params.pitch_mm is not None:
        kept.append(tx("passo"))
    if params.row_spacing_mm is not None:
        kept.append(tx("interasse fra le file"))
    if params.pinout:
        kept.append(tx("pinout"))
    return ", ".join(kept) if kept else tx("nient'altro")


def _grid_missing_spacings(grid: dict) -> list:
    """The spacing fields a declared grid needs to place its pads and does not give.

    `pitch_mm` is needed with more than one column, `row_spacing_mm` with
    more than one row (`_grid_as_pads`); the pitch is never reused as the
    row spacing, so either one missing leaves pads without a coordinate.
    """
    rows = _optional_number(grid.get("rows"), positive=True, integer=True)
    cols = _optional_number(grid.get("cols"), positive=True, integer=True)
    missing = []
    if cols and cols > 1 and _optional_number(grid.get("pitch_mm"), positive=True) is None:
        missing.append("pitch_mm")
    if rows and rows > 1 and _optional_number(grid.get("row_spacing_mm"), positive=True) is None:
        missing.append("row_spacing_mm")
    return missing


def _package_mapping_for_validation(package_geometry, notes: list):
    """Copy of an extracted package mapping that cannot discard a paid answer for a label.

    Two fields of an AI answer are not geometry and must not make
    `package_from_mapping` reject the whole extraction: the package NAME
    (validated as a file name since 2026-10-27, while the AI writes part
    numbers such as «LM358/LM2904»; the window replaces it with the project
    folder anyway) and a legacy pad SHAPE still present in cached answers.
    The name is swapped for a placeholder silently (it was never shown); each
    legacy shape is mapped and named in one note. Everything else is
    validated as before; the caller's mapping is never modified.
    """
    from ..core.output_names import is_valid_output_name

    if not isinstance(package_geometry, dict):
        return package_geometry
    mapping = dict(package_geometry)
    name = mapping.get("name")
    if not (isinstance(name, str) and is_valid_output_name(name)):
        mapping["name"] = EXTRACTED_PACKAGE_PLACEHOLDER_NAME
    pins = mapping.get("pins")
    if isinstance(pins, list):
        legacy = []
        migrated = []
        for pin in pins:
            if isinstance(pin, dict) and pin.get("shape") in LEGACY_EXTRACTED_PAD_SHAPES:
                legacy.append(str(pin.get("number", "?")))
                pin = {**pin, "shape": LEGACY_EXTRACTED_PAD_SHAPES[pin["shape"]]}
            migrated.append(pin)
        if legacy:
            mapping["pins"] = migrated
            notes.append(tx("Pad {pads}: forma «trapezio» proposta senza la geometria che KiCad "
                            "richiede; usati come rettangoli, come KiCad li avrebbe disegnati.",
                            pads=", ".join(legacy)))
    return mapping


def _optional_number(value, *, positive=False, integer=False):
    """Discard malformed optional numerics; signed offsets remain valid.

    Strings, booleans, non-finite values and integers too large for a float
    are malformed (see `_real_number`).
    """
    number = _real_number(value)
    if number is None or (positive and number <= 0):
        return None
    if integer and number != int(number):
        return None
    return int(number) if integer else number


class _PinNumberConflict(ExtractionError):
    """@brief Reject overlapping physical pad ranges before photographic analysis.

    @note The message is the user's (tx); `model_text` is the same fact in
        fixed English, the wording the numbering correction sends to the model
        (user, 2026-11-12: the UI language never reaches a model).
    """

    def __init__(self, message: str, model_text: str = "") -> None:
        """@brief Keep the user message and its English model-facing twin.

        @param message Translated message shown to the user.
        @param model_text English wording for the model; the message when empty.
        """
        super().__init__(message)
        self.model_text = model_text or message


#: Optional per-side hole sizes, in mm, as observed on the board: the diameter
#: of the plated half-hole cut by the edge and the drill of the full through
#: hole. Which of the two a contact carries is `contact_style`.
_SIDE_HOLE_KEYS = ("half_hole_diameter_mm", "through_hole_drill_mm")

#: Contact styles that carry each hole (see castellated_module.CONTACT_STYLES).
_STYLES_WITH_HOLE = {"half_hole_diameter_mm": ("half_hole", "half_and_through"),
                     "through_hole_drill_mm": ("through_hole", "half_and_through")}


def _add_hole_sizes(side: dict, entry: dict, notes: Optional[list]) -> None:
    """Copy the optional hole diameters of one castellated side, validated.

    A diameter must be a finite number > 0 and, with more than one contact,
    smaller than the pitch: two holes of that diameter at that pitch would
    merge into one slot, which is not a row of contacts (physical bound, not a
    tuned limit). An invalid value is dropped with a note; a hole the declared
    contact style does not carry is kept, with a note, because the two
    observations contradict each other and nothing says which one is wrong.
    """
    label = EDGE_LABELS.get(side["edge"], side["edge"])
    for key in _SIDE_HOLE_KEYS:
        raw = entry.get(key)
        if raw is None:
            continue
        value = _optional_number(raw, positive=True)
        if value is None:
            if notes is not None:
                notes.append(tx("Lato {label}: {key} scartato, valore non valido ({value!r}).",
                                label=label, key=key, value=raw))
            continue
        if side["count"] > 1 and value >= side["pitch_mm"]:
            if notes is not None:
                notes.append(tx("Lato {label}: {key} {value:g} mm non minore del passo "
                                "{pitch:g} mm, i fori adiacenti si unirebbero; valore scartato.",
                                label=label, key=key, value=value, pitch=side["pitch_mm"]))
            continue
        side[key] = value
        style = side.get("contact_style")
        if style is not None and style not in _STYLES_WITH_HOLE[key] and notes is not None:
            notes.append(tx("Lato {label}: {key} indicato ma il tipo di contatto osservato "
                            "({style}) non ha quel foro; valore conservato, da verificare.",
                            label=label, key=key, style=style))


def _normalized_castellated_sides(raw, notes: Optional[list] = None) -> list[dict]:
    """Per-side normalization of castellated sides, WITHOUT pad numbering.

    Separated from the numbering so that a numbering repair can compare the
    normalized original with the normalized correction: comparing the raw list
    with the normalized one rejected valid corrections whenever the raw list
    held a discarded entry or a differently written edge.
    """
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ExtractionError(
            tx("Il modello ha indicato un modulo con piazzole a mezzo foro ma non ha "
               "descritto alcun lato. Riprova, eventualmente con un modello piu' "
               "capace.")
        )

    sides = []
    for index, entry in enumerate(raw, 1):
        if not isinstance(entry, dict):
            if notes is not None:
                notes.append(tx("Lato {index} scartato: servono lato, numero di contatti e "
                                "passo validi.", index=index))
            continue
        edge = str(entry.get("edge") or "").strip().lower()
        count = _optional_number(entry.get("count"), positive=True, integer=True)
        pitch = _optional_number(entry.get("pitch_mm", 0))
        if edge not in EDGES or count is None or pitch is None or pitch < 0 or (count > 1 and pitch == 0):
            if notes is not None:
                notes.append(tx("Lato {index} scartato: servono lato, numero di contatti e "
                                "passo validi.", index=index))
            continue
        side = {"edge": edge, "count": count, "pitch_mm": pitch}
        for key in ("first_pin", "offset_mm", "pad_length_mm", "pad_width_mm"):
            value = _optional_number(entry.get(key), positive=key != "offset_mm", integer=key == "first_pin")
            if value is not None:
                side[key] = value
        if entry.get("reverse") is True:
            side["reverse"] = True
        if isinstance(entry.get("pin_names"), (list, tuple)) and entry["pin_names"]:
            names = [str(n) for n in entry["pin_names"]]
            if len(names) == count:
                side["pin_names"] = names
            elif notes is not None:
                # A name list of the wrong length cannot be aligned with the
                # pads: any alignment would put a name on the wrong contact.
                notes.append(tx("Lato {label}: {n_names} nomi per {count} contatti; nomi "
                                "scartati per non assegnarli ai pad sbagliati.",
                                label=EDGE_LABELS.get(edge, edge), n_names=len(names),
                                count=count))
        # The contact type is seen in the photo and cannot be derived from
        # the dimensions: a value outside the list is neither corrected nor
        # silently discarded, because any substitution would be exactly the
        # deduction the field exists to avoid. Missing becomes None, i.e.
        # «sconosciuto» (unknown): no default type (user decision 26/09, it was the
        # sample board's), the user chooses it (see
        # _unobserved_contact_style_notice).
        side["contact_style"] = None
        if "contact_style" in entry and entry["contact_style"] is not None:
            style = entry["contact_style"]
            if style not in CONTACT_STYLES:
                raise ExtractionError(
                    tx("Lato {get}: tipo di contatto {style!r} non riconosciuto (ammessi: "
                       "{join}). La risposta non viene adattata: ripeti l'estrazione o scegli "
                       "il tipo a mano.",
                       get=EDGE_LABELS.get(edge, edge),
                       style=style,
                       join=', '.join(CONTACT_STYLES))
                )
            side["contact_style"] = style
        _add_hole_sizes(side, entry, notes)
        sides.append(side)

    if not sides:
        raise ExtractionError(
            tx("Nessun lato valido tra quelli restituiti dal modello (servono lato, "
               "numero di contatti e passo).")
        )
    # One row of contacts per edge: two descriptions of the same edge would
    # put two pad rows on top of each other, and nothing says which is right.
    seen = set()
    for side in sides:
        if side["edge"] in seen:
            raise ExtractionError(
                tx("Il lato {label} compare piu' volte nella risposta: non si puo' stabilire "
                   "quale descrizione sia quella giusta.",
                   label=EDGE_LABELS.get(side["edge"], side["edge"])))
        seen.add(side["edge"])
    return sides


def _check_sides_fit_edges(sides, body_length, body_width) -> None:
    """Reject a side whose contact centres cannot fit along its board edge.

    Physical bound, not a tuned limit: the centres of `count` contacts at
    `pitch` span (count - 1) * pitch, which cannot exceed the edge the row sits
    on (left/right edges run along the board width, top/bottom along its
    length). Skipped when the board dimensions are unknown; the generator
    repeats a stricter check that includes the pad land.
    """
    for side in sides:
        edge_length = body_width if side["edge"] in ("left", "right") else body_length
        if edge_length is None:
            continue
        span = (side["count"] - 1) * side["pitch_mm"]
        if span > edge_length + _PIN_FIELD_TOLERANCE_MM:
            raise ExtractionError(
                tx("Lato {label}: {count} contatti a passo {pitch} mm occupano {span:.2f} mm, "
                   "ma il bordo e' lungo {edge:.2f} mm.",
                   label=EDGE_LABELS.get(side["edge"], side["edge"]), count=side["count"],
                   pitch=side["pitch_mm"], span=span, edge=edge_length))


def _castellated_sides(raw: Optional[Sequence[dict]], *, body_length: Optional[float] = None,
                       body_width: Optional[float] = None,
                       notes: Optional[list] = None) -> list[dict]:
    """Normalize the sides of a castellated module, discarding the unusable ones.

    The model may return a side without pitch or with zero contacts: it is
    better to ignore it and say so (in `notes`, when given) than to
    propagate a side that would make the generation fail with an obscure
    geometric error. Two sides on the same edge, or a side whose contacts
    cannot fit along the known board edge (`body_length`/`body_width`, mm),
    raise ExtractionError; a pad number shared between sides raises
    _PinNumberConflict.

    Pad ranges are compared as intervals and never materialized: a huge
    declared count must not allocate memory proportional to it.
    """
    sides = _normalized_castellated_sides(raw, notes)
    _check_sides_fit_edges(sides, body_length, body_width)

    # Progressive numbering on the sides without first_pin, in the order in
    # which they were listed: better a consistent numbering to fix by hand
    # than pads without a number.
    used: list[tuple[int, int]] = []  # half-open [start, stop) pad ranges
    for side in sides:
        if "first_pin" in side:
            start, stop = side["first_pin"], side["first_pin"] + side["count"]
            shared = [max(start, a) for a, b in used if max(start, a) < min(stop, b)]
            if shared:
                raise _PinNumberConflict(
                    tx("Numero pad duplicato tra lati: {min}.", min=min(shared)),
                    model_text=f"Duplicate pad number between sides: {min(shared)}.")
            used.append((start, stop))
    next_pin = 1
    for side in sides:
        if "first_pin" not in side:
            while True:
                clash = [b for a, b in used
                         if max(next_pin, a) < min(next_pin + side["count"], b)]
                if not clash:
                    break
                # Jump past the blocking range instead of stepping by one pad.
                next_pin = max(clash)
            side["first_pin"] = next_pin
            used.append((next_pin, next_pin + side["count"]))
        next_pin = int(side["first_pin"]) + side["count"]
    return sides


def _unobserved_contact_style_notice(sides: Sequence[dict]) -> Optional[str]:
    """@brief Say which castellated sides came back without a contact style.

    @param sides Normalized sides, as returned by _castellated_sides.
    @return One Italian log line naming those sides and saying that no style
        is assumed for them, or None when every side declares its own style.
    @note The style of such a side is None (unknown) in the extracted data:
        whether a contact is a half-hole, a full hole or both is seen on the
        board edge and cannot be deduced from dimensions, so no default is
        substituted (user decision 2026-09-26; the former default was the
        style of the sample board). The GUI asks the user for it
        (ExtractedParams.unobserved_contact_style_edges).
    """
    missing = [EDGE_LABELS.get(side["edge"], side["edge"])
               for side in sides if side.get("contact_style") is None]
    if not missing:
        return None
    return tx("Lato {join}: tipo di contatto non osservato (contact_style vuoto). Nessun tipo "
              "assunto: va scelto a mano fra solo semi-foro, solo foro passante o entrambi.",
              join=', '.join(missing))


def _declared_sources(raw, keys, notes: list, label: str) -> dict:
    """Valid provenances declared at one level of the answer: {field: source}.

    A value outside VALUE_SOURCES, or a field that is not a key dimension of
    that level, is ignored: the field then counts as undeclared, which is
    reviewed exactly like an estimate, so nothing is lost by ignoring it. A
    `value_sources` that is not an object is dropped with a note.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        notes.append(tx("{key} scartato: valore non valido ({value!r}).", key=label, value=raw))
        return {}
    return {key: source for key, source in raw.items()
            if key in keys and isinstance(source, str) and source in VALUE_SOURCES}


def _value_sources(data: dict, params: "ExtractedParams", sides, notes: list) -> dict:
    """Provenance of every key dimension the validated result carries.

    @param data Canonical answer (millimetres, contacts seen from above): its
        castellated sides were mirrored in place, together with their nested
        `value_sources`, so the edges here are those of the result.
    @param params The result built from `data`; only the values it carries
        get a provenance.
    @param sides Normalized castellated sides, or None.
    @param notes Notes of the result, for a malformed `value_sources`.
    @return {field: source} with the key scheme of ExtractedParams.value_sources;
        a value without a valid declaration is UNDECLARED_SOURCE, never "read".
        ``drawing_unit`` is UNDECLARED_SOURCE when the answer gave no known
        unit (`_canonicalize_answer` read its lengths as mm).
    """
    sources = {}
    if data.get("_unit_undeclared"):
        # Every length was read in a unit the answer did not declare (or
        # declared unknown) and taken as mm: the unit itself is a value
        # without provenance, listed by _review_notes.
        sources["drawing_unit"] = UNDECLARED_SOURCE
    declared = _declared_sources(data.get("value_sources"), _TOP_LENGTH_KEYS, notes,
                                 "value_sources")
    program = data.get("_program_sources") if isinstance(data.get("_program_sources"), dict) else {}
    for key in _TOP_LENGTH_KEYS:
        if getattr(params, key) is not None:
            # A provenance the program gave (a user's choice, a measure)
            # ranks over the model's declaration for the same field.
            sources[key] = program.get(key) or declared.get(key, UNDECLARED_SOURCE)
    if not sides:
        return sources
    raw_by_edge = {}
    for entry in data.get("castellated_sides") or ():
        if isinstance(entry, dict) and "value_sources" in entry:
            raw_by_edge[str(entry.get("edge") or "").strip().lower()] = entry["value_sources"]
    for side in sides:
        edge = side["edge"]
        declared = _declared_sources(raw_by_edge.get(edge), _SIDE_LENGTH_KEYS, notes,
                                     f"castellated_sides[{edge}].value_sources")
        for key in _SIDE_LENGTH_KEYS:
            # A single contact has no pitch: its placeholder is not a value.
            if side.get(key) is None or (key == "pitch_mm" and side["count"] < 2):
                continue
            sources[f"castellated_sides.{edge}.{key}"] = declared.get(key, UNDECLARED_SOURCE)
    return sources


def _review_notes(sides, value_sources, pins=None) -> list[str]:
    """@brief Lines telling what the user must decide or check before using a result.

    @param sides Normalized castellated sides, or None.
    @param value_sources Provenance map (ExtractedParams.value_sources), or None.
    @param pins Pin dicts of the explicit geometry (with their per-pad
        `sources`), or None.
    @return Italian log lines: sides whose contact style was not observed,
        values estimated by the model and values whose provenance it did not
        declare, and the explicit pads whose hole, size or shape was not read
        from the drawing (pad by pad, 2026-11-10: the «estimated» of explicit
        pads used to be dropped). Empty when there is nothing to review.
    """
    lines = []
    notice = _unobserved_contact_style_notice(sides) if sides else None
    if notice:
        lines.append(notice)
    sources = value_sources or {}
    estimated = sorted(key for key, source in sources.items() if source == "estimated")
    undeclared = sorted(key for key, source in sources.items() if source == UNDECLARED_SOURCE)
    if estimated:
        lines.append(tx("Valori STIMATI, non letti dai documenti: {fields}. Verificarli prima "
                        "dell'uso.", fields=", ".join(estimated)))
    if undeclared:
        lines.append(tx("Valori senza provenienza dichiarata dal modello, trattati come stime "
                        "da verificare: {fields}.", fields=", ".join(undeclared)))
    names = {"drill_mm": tx("foro"), "pad_size": tx("dimensione"), "shape": tx("forma")}
    unverified = []
    for pin in pins or ():
        if not isinstance(pin, dict) or not isinstance(pin.get("sources"), dict):
            continue
        keys = unverified_values(pin["sources"])
        if keys:
            unverified.append(f"{_pad_number_text(pin.get('number')) or '?'} "
                              f"({', '.join(names[key] for key in keys)})")
    if unverified:
        lines.append(tx("Valori STIMATI o ricavati dal programma nei pad, non letti dal disegno: "
                        "{pads}. Verificarli prima dell'uso.", pads="; ".join(unverified)))
    return lines


#: Verification and provenance fields of a component that only the program's
#: own photo pipeline may set: confidences, counts declared complete, the
#: inventory's identifiers and class, and `object_type` (whose presence lets a
#: body named like a PCB feature through). A raw AI answer loses them, together
#: with every `_`-prefixed internal flag (`_yolo_manual_box`,
#: `_height_estimated`, `_global_inventory_status`, ...). Descriptive fields
#: (name, kind, package, mating, colour, ...) are kept.
_TRUSTED_ONLY_FIELDS = frozenset({
    "model_match", "footprint", "footprint_candidates",
    "object_type", "confidence", "presence_confidence", "class_confidence", "class",
    "component_id", "source_candidate_ids", "pin_count_confidence", "pin_count_source",
    "terminal_count_basis", "lead_count_complete", "lead_count_confidence",
    "landmark_confidence",
})
#: `_height_source` of a height taken from a raw AI answer: the schema asks for
#: a visual estimate from a top photo, never a measurement.
AI_ANSWER_HEIGHT_SOURCE = "ai_answer_visual_estimate_not_measured"


def _components_3d(raw: Optional[Sequence[dict]],
                   notes: Optional[list] = None, *,
                   trusted: bool = False) -> Optional[list[dict]]:
    """@brief Components read from the photo, dropping those without a real footprint.

    These are visual estimates: a block with a zero or negative dimension is
    not a badly measured component, it is noise, and in the 3D solid it would
    raise an error. A `kind` outside COMPONENT_KINDS becomes "generic", with
    a line in `notes` when given.

    @param raw Entries of components_3d.
    @param notes Optional list that receives the review lines.
    @param trusted True only for components built by the program (photo
        inventory, saved projects): their internal flags are kept. False
        (default) for a raw AI answer: every `_`-prefixed key and every field
        of _TRUSTED_ONLY_FIELDS is dropped first, so a text in a document can
        never mark a body as drawn by hand, verified or measured, and every
        height is marked as an estimate (`_height_estimated` True,
        AI_ANSWER_HEIGHT_SOURCE).
    @return The valid components, or None when there is none.
    """
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    components = []
    for entry in raw:
        if not isinstance(entry, dict):
            # With long lists the model sometimes
            # derails from the schema and slips in an entry as a plain string
            # instead of an object: that entry is discarded, not the whole
            # extraction -- the other components stay valid.
            continue
        if not trusted:
            entry = {key: value for key, value in entry.items()
                     if not (isinstance(key, str) and key.startswith("_"))
                     and key not in _TRUSTED_ONLY_FIELDS}
        # Booleans, numeric strings, non-finite and overflowing values are not
        # measures (review D4): the entry is dropped, the others stay.
        geometry = {key: _real_number(entry.get(key)) for key in
                    ("x_mm", "y_mm", "length_mm", "width_mm", "height_mm")}
        if any(value is None for value in geometry.values()):
            continue
        component = {"name": str(entry.get("name") or "Componente"), **geometry}
        # The model also sees PCB elements that are not mounted components:
        # pads, vias, half-holes and edge contacts. If they get through the
        # schema, removing them here keeps them from ending up in the 3D table
        # as boxes. Real components must have a package above the board.
        label = component["name"].strip().lower()
        # Global component IDs are arbitrary display labels; retained detector
        # proposals are physical hypotheses, even when their class is unknown.
        # Preserve explicit PCB-feature rejection and the geometry checks below.
        physical_hypothesis = (
            entry.get("_global_inventory_status") in ("validated_hypotheses", "partial")
            or (isinstance(entry.get("_yolo_proposal_id"), str)
                and bool(entry["_yolo_proposal_id"].strip())
                and entry.get("_yolo_origin") in ("operating_threshold", "low_confidence_review")))
        if entry.get("object_type") == "pcb_feature":
            # The declared criterion: the schema asks for `object_type`, and
            # a declared PCB feature is never a body.
            continue
        if (not physical_hypothesis and "object_type" not in entry and re.match(
                r"^(?:via|pad|piazzola|terminal pad|contatto|castellated|semiforo|serigrafia)\b", label)):
            # DECLARED HEURISTIC (review 2026-10-27, E8), not a rule: when
            # `object_type` is absent -- which is always the case for a raw
            # AI answer, since `_TRUSTED_ONLY_FIELDS` strips it so that a
            # document cannot vouch for itself -- the only information left
            # is the display name, and a name that opens with the word of a
            # PCB feature is taken as one. It was tuned on the inventory
            # labels the models actually produce («pad 3», «via», «contatto
            # castellated»); a component the model chose to name «Pad di
            # test IC» is lost by it. It would become superfluous if the
            # answer carried a provenance the program could trust. It is
            # never silent: every time it fires a line is written, so the
            # user can bring the body back from the review.
            if notes is not None:
                notes.append(tx("{name}: scartato dai corpi 3D perche' il nome descrive un "
                                "elemento del PCB (via, pad, contatto, serigrafia) e il tipo "
                                "di oggetto non e' dichiarato; se e' un componente, "
                                "rinominalo o rimettilo nella revisione.",
                                name=component["name"]))
            continue
        if not all(math.isfinite(component[k]) for k in
                   ("x_mm", "y_mm", "length_mm", "width_mm", "height_mm")):
            continue
        if min(component["length_mm"], component["width_mm"], component["height_mm"]) <= 0:
            continue
        if entry.get("color_hex"):
            component["color_hex"] = str(entry["color_hex"])
        kind = str(entry.get("kind") or "generic")
        if kind not in COMPONENT_KINDS:
            # A kind outside the vocabulary has no shape nor height rule: the
            # body stays a generic block, and the note says so instead of
            # letting the change pass unseen.
            if notes is not None:
                notes.append(tx("{name}: tipo di corpo {kind!r} non previsto, trattato come "
                                "generico (blocco semplice).", name=component["name"], kind=kind))
            kind = "generic"
        component["kind"] = kind
        if entry.get("marking"):
            marking = str(entry["marking"]).strip()
            # Normalize only whole-field AI absence/illegibility sentinels;
            # literal substrings and the source observation remain untouched.
            component["marking"] = "" if marking.casefold() in ("none", "?") else marking
        component["rotation_deg"] = (_optional_number(entry.get("rotation_deg")) or 0.0) % 360
        # Mating direction declared by the model looking at the photo.
        # Without it, the 3D shape would have to guess it from the dimensions
        # alone, which for a connector are not enough. Other values are ignored.
        if entry.get("mating") in ("edge", "top", "none"):
            component["mating"] = entry["mating"]
        # Keep the complete description: the matcher extracts declared tokens,
        # which may occur after dimensions or beyond a short display label.
        if entry.get("package"):
            component["package"] = str(entry["package"]).strip()
        if entry.get("bbox_basis") in ("body", "leads", "pads", "unknown"):
            component["bbox_basis"] = entry["bbox_basis"]
        if isinstance(entry.get("package_evidence"), str):
            component["package_evidence"] = entry["package_evidence"][:500]
        for key in ("_box", "_id", "_candidate_id", "_source", "_uncertain", "_review_reason",
                    "_detected_kind", "terminal_geometry",
                    "_height_source", "_grounding_kind", "_alternative_kind", "confidence"):
            if key in entry:
                component[key] = entry[key]
        for key in ('_yolo_class', '_yolo_confidence', '_yolo_original_box', '_geometry_source',
                    '_smd_prediction', '_smd_class', '_smd_rotation_deg', '_smd_rotation_confidence',
                    '_smd_ai_recheck', '_smd_accepted', '_smd_body', '_smd_rotation_kind',
                    '_smd_views_agree', '_smd_terminal_count', '_smd_superseded',
                    'model_match', 'footprint', 'footprint_candidates', '_presence_reviewed_by_user',
                    'presence_confidence', 'class_confidence', '_yolo_proposal_id',
                    '_yolo_class_ambiguous', '_yolo_review_only', '_yolo_origin',
                    # Box drawn or retouched by a person: it is the reviewer's
                    # presence decision and must reach the final component,
                    # not stop at the submitted candidates.
                    '_yolo_manual_box',
                    # Body added by the model alone, excluded until the user
                    # keeps it (point 141): without this, the first extraction
                    # delivered it like any other body, into the 3D.
                    '_ai_only_excluded',
                    '_package_verified', '_orientation_verified', 'pin_count_confidence', 'pin_count_source',
                    'class', 'component_id', 'source_candidate_ids', 'terminal_count', 'terminal_layout',
                    'terminal_count_basis', 'lead_count', 'lead_count_complete', 'lead_count_confidence',
                    '_reported_rotation_deg', '_height_estimated', '_global_inventory_status',
                    # Measurement from the edges, estimated-height rule and the
                    # user's choice on weak proposals: the GUI shows them.
                    '_edges_measured', '_height_basis', '_yolo_low_confidence', '_yolo_user_kept',
                    # Detector's own weak/strong verdict before the reviewer
                    # changed `_yolo_review_only` (photo_yolo.set_review_only).
                    '_detector_review_only',
                    '_global_inventory_errors', '_global_inventory_uncertainties',
                    '_global_inventory_prompt_version', '_global_inventory_prompt_hash',
                    # The asymmetric reference lets the 3D builder establish
                    # the orientation of the native model. Without this line
                    # it was discarded here, and every component reached the
                    # matching declaring "no observed reference" even when
                    # the model had seen one with high confidence.
                    'landmark_feature', 'landmark_side', 'landmark_confidence',
                    # Crop re-reading (HANDOFF 144): outcome, the two readings
                    # and the replaced inventory values must reach the review,
                    # otherwise a «da rivedere» (to review) row would no longer
                    # say why.
                    'package_confidence', '_crop_reading', '_crop_readings',
                    '_crop_observations', '_crop_class_status', '_crop_class_decided',
                    '_crop_terminal_review_required', '_crop_image_evidence',
                    '_category_source', '_category_status', '_ai_class_proposal',
                    '_ai_class_confidence', '_terminal_status',
                    # Local detector evidence of the source proposal and
                    # provenance of a derived package (HANDOFF 149).
                    '_detector_evidence', 'package_source',
                    '_crop_reading_version', '_crop_reading_prompt_digest',
                    '_package_inventory', '_class_inventory',
                    '_lead_count_inventory'):
            if key in entry:
                component[key] = entry[key]
        if isinstance(entry.get("pin_count"), int) and not isinstance(entry.get("pin_count"), bool) and entry["pin_count"] > 0:
            component["pin_count"] = entry["pin_count"]
        if isinstance(entry.get("pins_by_side"), dict):
            sides = {k: v for k, v in entry["pins_by_side"].items()
                     if k in {"left", "right", "top", "bottom"}
                     and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
            if sides and sum(sides.values()) > 0:
                component["pins_by_side"] = sides
        if not trusted:
            component["_height_estimated"] = True
            component["_height_source"] = AI_ANSWER_HEIGHT_SOURCE
        components.append(component)
    return components or None


#: Matrix fields that are integer counts; every one must be >= 1.
_MATRIX_INT_FIELDS = ("rows", "cols")
#: Matrix fields that are physical lengths in mm; every one must be > 0.
_MATRIX_LENGTH_FIELDS = ("pitch_mm", "row_spacing_mm", "pad_diameter_mm", "drill_mm")
#: Optional signed offsets in mm: any finite number is meaningful.
_SIGNED_OPTIONAL_FIELDS = ("body_offset_x_mm", "body_offset_y_mm")
#: Optional physical lengths in mm: only finite values > 0 are meaningful.
_POSITIVE_OPTIONAL_FIELDS = ("body_length_mm", "body_width_mm", "body_height_mm",
                             "pin_length_3d_mm", "pcb_thickness_mm", "lead_diameter_mm")


def _required_matrix_value(data: dict, key: str):
    """One mandatory matrix field, validated; ExtractionError when unusable.

    Counts must be integral (2.0 accepted, 2.7 and True rejected) and >= 1;
    lengths must be finite and > 0. Nothing is truncated or coerced silently.
    """
    raw = data.get(key)
    if raw is None:
        raise ExtractionError(
            tx("La risposta del modello non contiene il campo obbligatorio '{key}'. "
               "Riprova, eventualmente con un modello piu' capace.",
               key=key)
        )
    if key in _MATRIX_INT_FIELDS:
        value = _optional_number(raw, positive=True, integer=True)
        if value is None:
            raise ExtractionError(tx("Campo obbligatorio '{key}' non valido: {value!r} (serve un "
                                     "intero maggiore di zero, non booleano).",
                                     key=key, value=raw))
        return value
    value = _optional_number(raw, positive=True)
    if value is None:
        raise ExtractionError(tx("Campo obbligatorio '{key}' non valido: {value!r} (serve un "
                                 "numero finito maggiore di zero, non booleano).",
                                 key=key, value=raw))
    return value


def _numbering_conventions(rows: int, cols: int) -> dict:
    """@brief The standard pin-numbering conventions of a rows x cols grid, seen from above.

    @param rows Rows of the grid.
    @param cols Columns of the grid.
    @return {name: {pin: (row, col)}} covering every position once:
        "column" (pin headers: 1,2 down the first column, then the next) and
        its mirror image "column_mirrored" (the same convention read on a
        bottom view), and for two rows "u" (DIP-style: 1..cols along row 0,
        then back along row 1) and "u_mirrored". A single row has the two
        reading directions only.
    @note These are the conventions a drawing implies when it prints only
        some numbers (a header prints «1», a DIP prints «1» and «N»); they
        are declared here, not tuned: completing a map with one of them is
        done only when exactly one fits every printed number.
    """
    column = {col * rows + row + 1: (row, col) for col in range(cols) for row in range(rows)}
    conventions = {
        "column": column,
        "column_mirrored": {pin: (row, cols - 1 - col) for pin, (row, col) in column.items()},
    }
    if rows == 2:
        u = {col + 1: (0, col) for col in range(cols)}
        u.update({2 * cols - col: (1, col) for col in range(cols)})
        conventions["u"] = u
        conventions["u_mirrored"] = {pin: (row, cols - 1 - col) for pin, (row, col) in u.items()}
    return conventions


def _resolve_pin_numbering(pin_map, raw_populated, rows, cols, max_pin, *, dedicated: bool,
                           notes: list):
    """@brief Decide the pin map and the populated pins of a matrix answer, map complete or not.

    @param pin_map Validated entries seen from above, or None.
    @param raw_populated The populated_pins of the answer, unvalidated.
    @param rows Rows of the grid (None when unknown).
    @param cols Columns of the grid (None when unknown).
    @param max_pin rows x cols, or None.
    @param dedicated True when the map comes from the dedicated numbering
        call (every entry is a number printed beside a position).
    @param notes Notes of the result.
    @return (pin_map, populated_pins, block_text): block_text is None, or the
        reason the automatic generation must stop (a partial map that no
        single convention completes).
    @note Rules, the same for every part (review A of 2026-10-10, finding 1:
        the prompts now ask for the PRINTED numbers only, so a map may be
        partial -- a header prints «1», a DIP «1» and «N» -- and a partial
        map must never define which pins exist):
        - COMPLETE map (it places every populated pin, or every position
          when no populated_pins is given): the map decides which numbers
          exist, whatever rows x cols (a U-numbered DIP keeps pin 16 on a
          2x3 grid of populated positions). Entries of a MAIN map for pins
          absent from populated_pins are a convention, not a reading, and
          are dropped with a note; entries of a DEDICATED map are printed
          numbers, so the pins are added to populated_pins with a note.
        - PARTIAL map (anchors): populated_pins is validated against
          1..rows x cols; the anchors are matched against the standard
          conventions (`_numbering_conventions`); exactly one fitting every
          anchor completes the map, declared «derived» in the notes; none or
          several: no map, automatic numbering, and a block text.
    """
    loose = _validated_populated_pins(raw_populated, None, [], None) or []
    mapped = {entry["pin"] for entry in pin_map} if pin_map else set()
    if pin_map:
        if loose:
            complete = set(loose) <= mapped
        else:
            complete = max_pin is not None and len(mapped) >= max_pin
    else:
        complete = False
    if pin_map and complete:
        populated = _validated_populated_pins(raw_populated, max_pin, notes,
                                              mapped=sorted(mapped, key=_pin_sort_key))
        if populated:
            extra = sorted((pin for pin in mapped if pin not in populated), key=_pin_sort_key)
            if extra and dedicated:
                populated = sorted(set(populated) | set(extra), key=_pin_sort_key)
                notes.append(tx("pin_map: i pin {pins} hanno un numero stampato nel disegno ma "
                                "mancavano da populated_pins: aggiunti ai pin presenti. "
                                "Verificare sul disegno.", pins=", ".join(str(p) for p in extra)))
            elif extra:
                pin_map = [entry for entry in pin_map if entry["pin"] in populated]
                notes.append(tx("pin_map: scartate le posizioni dei pin {pins}, assenti da "
                                "populated_pins: nel disegno non c'e' un pin da leggere li', "
                                "quindi erano una convenzione e non una lettura. Verificare "
                                "sul disegno la posizione dei pin presenti.",
                                pins=", ".join(str(p) for p in extra)))
        return pin_map, populated, None
    populated = _validated_populated_pins(raw_populated, max_pin, notes, None)
    if not pin_map:
        return None, populated, None
    if not all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in (rows, cols)):
        # A partial map without a known grid (rows or cols missing, e.g. an
        # answer whose footprint comes from package_geometry and whose matrix
        # is informative only): no convention can complete the anchors, so
        # the map is set aside with a note, never a crash that loses the
        # paid answer (review S2, 2026-11-03).
        notes.append(tx("pin_map parziale ignorata: righe o colonne della griglia non note, "
                        "nessuna convenzione puo' completarla. Verificare la numerazione sul "
                        "disegno."))
        return None, populated, None
    expected = len(populated) if populated else (max_pin or 0)
    anchors = {entry["pin"]: (entry["row"], entry["col"]) for entry in pin_map}
    fitting = [name for name, convention in _numbering_conventions(rows, cols).items()
               if all(convention.get(pin) == position for pin, position in anchors.items())]
    summary = ", ".join(f"{pin}@({r},{c})" for pin, (r, c)
                        in sorted(anchors.items(), key=lambda item: _pin_sort_key(item[0])))
    if len(fitting) == 1:
        convention = _numbering_conventions(rows, cols)[fitting[0]]
        completed = [{"pin": pin, "row": row, "col": col}
                     for pin, (row, col) in sorted(convention.items())]
        if populated:
            missing = sorted((pin for pin in populated if pin not in convention),
                             key=_pin_sort_key)
            if missing:
                return None, populated, tx(
                    "Numerazione letta per {read} pin su {expected} ({anchors}); la convenzione "
                    "{name} che li completa non prevede i pin {missing}: numerazione da "
                    "verificare sul disegno prima di generare.",
                    read=len(anchors), expected=expected, anchors=summary, name=fitting[0],
                    missing=", ".join(str(p) for p in missing))
        notes.append(tx("Numerazione letta per {read} pin su {expected} ({anchors}): completata "
                        "con la convenzione {name}, l'unica coerente con i numeri stampati. "
                        "Valore DERIVATO, da verificare sul disegno.",
                        read=len(anchors), expected=expected, anchors=summary, name=fitting[0]))
        return completed, populated, None
    reason = (tx("nessuna convenzione standard e' coerente con i numeri stampati")
              if not fitting else
              tx("piu' convenzioni sono coerenti con i numeri stampati ({names})",
                 names=", ".join(fitting)))
    return None, populated, tx(
        "Numerazione letta per {read} pin su {expected} ({anchors}) e {reason}: le altre "
        "posizioni non hanno un numero stampato. Numerazione automatica colonna per colonna "
        "nei campi, da verificare sul disegno prima di generare.",
        read=len(anchors), expected=expected, anchors=summary, reason=reason)


def _validated_populated_pins(raw, max_pin: Optional[int], notes: list,
                              mapped: Optional[Sequence] = None) -> Optional[list]:
    """Optional populated_pins: keep the valid, in-range pin numbers once each.

    Each entry is a printed label (`_pin_label`): «3» and 3 are pin 3, «A1»
    is pin A1. A text label is in range only when the map places it, or when
    neither a map nor a grid bounds the numbers: the automatic
    column-by-column numbering knows only 1..rows x cols.

    Invalid entries (booleans, fractions, non-numbers, out of range) are
    dropped and reported in `notes`, like repeated numbers; the valid ones are
    kept, as for any partial response. None when nothing valid remains.

    @param max_pin Highest number of the automatic column-by-column numbering
        (rows x cols); None when the grid is unknown.
    @param mapped Pin numbers of an explicit, validated pin_map. With a map the
        numbering is a convention of the drawing (a DIP numbered in a U, empty
        positions), so a number is in range when the map places it, whatever
        rows x cols is: 1..max_pin only applies to the automatic numbering.
    """
    if raw is None or (isinstance(raw, (list, tuple)) and not raw):
        return None
    if not isinstance(raw, (list, tuple)):
        notes.append(tx("populated_pins scartato: valore non valido ({value!r}).", value=raw))
        return None
    allowed = {_pin_text(pin) for pin in mapped} if mapped else None
    kept, rejected, repeated = [], [], []
    for item in raw:
        number = _pin_label(item)
        if number is None:
            rejected.append(item)
        elif allowed is not None and _pin_text(number) not in allowed:
            rejected.append(item)
        elif allowed is None and max_pin is not None and (
                not isinstance(number, int) or number > max_pin):
            rejected.append(item)
        elif number in kept:
            repeated.append(number)
        else:
            kept.append(number)
    if rejected:
        notes.append(tx("populated_pins: scartate le voci non valide o fuori matrice {values!r}; "
                        "verificare i pin presenti.", values=rejected))
    if repeated:
        notes.append(tx("populated_pins: rimossi i numeri ripetuti {values!r}.", values=repeated))
    return kept or None


def _informative_matrix(data: dict, notes: list) -> dict:
    """Matrix fields that do not define the footprint: kept when valid, else noted.

    Used for a castellated module and for a part whose explicit pads
    (package_geometry) are the footprint. A drill of 0 is not an error here:
    it is how a pad without hole (SMD) is written, so it becomes None silently.
    """
    matrix = {}
    for key in _MATRIX_INT_FIELDS:
        raw = data.get(key)
        matrix[key] = _optional_number(raw, positive=True, integer=True)
        if raw is not None and matrix[key] is None:
            notes.append(tx("{key} scartato: valore non valido ({value!r}).", key=key, value=raw))
    for key in _MATRIX_LENGTH_FIELDS:
        raw = data.get(key)
        matrix[key] = _optional_number(raw, positive=True)
        if key == "drill_mm" and _optional_number(raw) == 0:
            continue
        if raw is not None and matrix[key] is None:
            notes.append(tx("{key} scartato: valore non valido ({value!r}).", key=key, value=raw))
    return matrix


def _footprint_matrix(data: dict, notes: list) -> dict:
    """Matrix fields of a through-hole pin grid that IS the footprint.

    Required fields follow from what the grid structurally is: rows, cols,
    pad and drill always; the pitch only with more than one column and the
    row spacing only with more than one row, because a single column or row
    has no such distance (it is left None, never borrowed from the other
    quantity). A given but invalid optional distance is dropped with a note.

    @throws ExtractionError For a missing or invalid required field, for a
        drill not smaller than the pad, and for a pad without hole: the grid
        generator draws only drilled pads, and a pad without hole needs its
        own shape, which only package_geometry describes.
    """
    matrix = {key: _required_matrix_value(data, key) for key in _MATRIX_INT_FIELDS}
    for key, count in (("pitch_mm", matrix["cols"]), ("row_spacing_mm", matrix["rows"])):
        if count > 1:
            matrix[key] = _required_matrix_value(data, key)
            continue
        raw = data.get(key)
        matrix[key] = _optional_number(raw, positive=True)
        if raw is not None and matrix[key] is None and _optional_number(raw) != 0:
            notes.append(tx("{key} scartato: valore non valido ({value!r}).", key=key, value=raw))
    matrix["pad_diameter_mm"] = _required_matrix_value(data, "pad_diameter_mm")
    raw_drill = data.get("drill_mm")
    if raw_drill is None or _optional_number(raw_drill) == 0:
        raise ExtractionError(tx(
            "Pad senza foro (drill_mm assente o 0) e nessun pad esplicito in "
            "package_geometry: la matrice di pin genera solo pad a foro passante, e un "
            "componente SMD richiede i pad descritti uno per uno. Riprova, eventualmente "
            "con un modello piu' capace."))
    matrix["drill_mm"] = _required_matrix_value(data, "drill_mm")
    if matrix["drill_mm"] >= matrix["pad_diameter_mm"]:
        raise ExtractionError(tx("Foratura non valida: drill_mm {drill} deve essere minore di "
                                 "pad_diameter_mm {pad}.", drill=matrix["drill_mm"],
                                 pad=matrix["pad_diameter_mm"]))
    return matrix


def _params_from_tool_arguments(data: dict, *, trusted_components: bool = False) -> ExtractedParams:
    """@brief Build ExtractedParams from the tool arguments, without any network access.

    Kept separate from the network call so that it can be tested without
    the API.

    @param data Tool arguments as the model returned them, or as
        `_validate_main_response` canonicalized them. The input is never
        modified: a raw answer is canonicalized on a copy
        (`_canonicalize_answer`: lengths to millimetres from `drawing_unit`,
        contacts to the view from above from `contacts_view`).
    @param trusted_components True only when `data["components_3d"]` is the
        photo inventory built by the program; False (default) treats it as the
        raw AI answer, whose internal flags are dropped (see `_components_3d`).
    @return The validated parameters, with every conversion, doubt and
        dropped optional value written in `notes`.
    @note What is required depends on what the answer structurally is. Valid
        explicit pads (package_geometry) are the footprint: the matrix fields
        become informative, and a drill of 0 or none means pads without hole.
        Without them, a "matrix" answer is a through-hole pin grid (see
        `_footprint_matrix`); a "castellated" one is its edge sides.
    @note Explicit pads whose positions were NOT READ -- at least one pad
        with `x_mm` or `y_mm` null or absent -- are not a footprint: the
        program never invents a coordinate, and the pads that have one alone
        would be a footprint missing terminals. The explicit geometry is
        dropped with a note naming those pads and what of the answer stays
        (built from the surviving fields: a single-part answer's drill and
        pad size live in its pads and fall with them), the matrix fields stay
        informative, and `pin_map_conflict` blocks the generation until the
        positions are confirmed (user, 2026-11-09, VE12H5-K run: «Sì,
        correggi così»); `unread_pads` names the pads and
        `unread_pad_geometry` keeps them for `apply_answers`. The raw answer
        in `raw_main` keeps the pads, so the dedicated re-reading of the
        positions can still settle them. Every value that IS present is
        validated first (unread coordinates replaced by far stand-ins): a
        malformed coordinate, number, size or pad type (text, boolean,
        non-finite) is a broken answer, not an unread value, and remains an
        error even beside a null coordinate. A declared grid lacking its
        pitch or row spacing is named as such (`_grid_missing`).
    @note INVARIANT of the explicit pads (review round 4, 2026-11-11): once
        `_resolve_pad_terminals` has run, nothing changes a pad; the geometry
        is validated pad by pad (`_validate_pads_alone`: a malformed value
        is an error) and then as a whole (`_cross_pad_problem`). It either
        validates as a footprint or is returned as UNREAD geometry -- pads
        without a position, holed pads without a hole, pads whose land
        cannot hold the hole or has no room at the pitch, pads whose copper
        overlaps -- with a `pin_map_conflict` naming the pads, the values
        in conflict and what to verify. No exception is raised for values
        that are well formed and only cannot all be true.
    @note Castellated layout: the edge sides are the footprint, so explicit
        pads with unread positions are optional data: dropped with a note,
        no block, `unread_pads` None (before 2026-11-09 the whole answer was
        rejected).
    @throws ExtractionError For any unusable MANDATORY field (missing,
        boolean, non-finite, non-integral count, non-positive length, drill
        not smaller than the pad, pad without hole and without explicit pads),
        for invalid explicit pads and for invalid castellated sides. Invalid
        OPTIONAL fields never fail the paid response: they are dropped and
        the reason is appended to `notes`.
    """
    if not isinstance(data, dict):
        raise ExtractionError(tx("Risposta AI non strutturata."))
    layout = data.get("layout") or "matrix"
    if layout not in ("matrix", "castellated"):
        raise ExtractionError(tx("Disposizione dei contatti sconosciuta: {layout!r}.", layout=layout))
    if not data.get("_canonical"):
        data = copy.deepcopy(data)
        _canonicalize_answer(data)
    notes: list[str] = list(data.get("_canonical_notes") or ())

    def _opt_float(key: str) -> Optional[float]:
        """Optional numeric field; an invalid value is dropped with a note."""
        raw = data.get(key)
        value = _optional_number(raw, positive=key in _POSITIVE_OPTIONAL_FIELDS)
        if raw is not None and value is None:
            notes.append(tx("{key} scartato: valore non valido ({value!r}).", key=key, value=raw))
        return value

    package_geometry = data.get("package_geometry")
    if not package_geometry or (isinstance(package_geometry, dict) and not package_geometry.get("pins")):
        # Optional field that came back empty (null, {} or «pins»: []): no
        # explicit pad, hence no package geometry. It is not an error of the
        # answer -- the schema imposes no minimum of pins -- and rejecting it
        # would throw away a paid extraction with valid matrix or module data.
        # The rule «senza pad non c'e' footprint» (no pads, no footprint)
        # stays in PackageGeometry.validate, where it is generated.
        if package_geometry:
            notes.append(tx("Geometria package senza pad espliciti ignorata: si usano i "
                            "parametri a matrice o del modulo."))
        package_geometry = None
    # The shape of the geometry and every size the answer DOES carry are
    # checked before the program's rule walks the pads and may replace a
    # value with a derived one (review 2026-11-11, F7-3, F7-5, F6-8): a
    # malformed value is an error of the answer, never hidden by an estimate
    # and never an AttributeError.
    _reject_malformed_pad_sizes(package_geometry)
    # Fields the rules do not read never fail the answer, on any layout
    # (review round 5, V1-2): dropped or brought to text, with one note (a
    # new geometry: `data` is untouched).
    package_geometry = _pads_with_the_fields_read(package_geometry, notes)
    # Holes, lands and their per-pad provenance settled by the program's one
    # rule before anything is validated (a new geometry: `data` is untouched).
    # NOTHING changes a pad after this call (invariant of round 4).
    package_geometry, pad_problems = _resolve_pad_terminals(
        package_geometry, data, notes, lift=layout != "castellated")
    unread_positions = _pads_without_positions(package_geometry)
    # Holed pads left without a hole: no printed hole and no terminal, so no
    # value the program may derive (review E1). Unread geometry like an
    # unread position: dropped and blocked, never an error after paid calls.
    unread_holes = [number for number in _pads_without_holes(package_geometry)
                    if number not in unread_positions]
    # Pads whose values are well formed but cannot all be true: a land that
    # cannot hold its hole, no room at the pitch (`pad_problems`), copper of
    # two pads overlapping (`_cross_pad_problem`). Unread geometry too.
    # The reasons name the pads that share one reason and one pair of values
    # together, bounded (V1-9).
    unfit_pads = [number for number, _reason in pad_problems]
    unfit_reasons = _grouped_pad_reasons(pad_problems)
    # Explicit pads dropped for their unread positions, holes or fit, kept
    # apart so that a chosen pad layout can still be written into them
    # (apply_answers).
    unread_geometry = None
    geometry_doubts = None
    mapping = None
    if package_geometry is not None:
        mapping = _package_mapping_for_validation(package_geometry, notes)
        try:
            # Everything present is validated first (M1): a malformed
            # coordinate, number, size or pad type stays an error even
            # beside an unread value.
            _validate_pads_alone(mapping, unfit_pads)
        except (KeyError, TypeError, ValueError) as exc:
            raise ExtractionError(tx("Geometria package non valida: {exc}", exc=exc)) from exc
        clash = _cross_pad_problem(mapping, unfit_pads)
        if clash:
            unfit_reasons.append(clash)
            unfit_pads += [number for pin in mapping.get("pins") or () if isinstance(pin, dict)
                           and pin.get("x_mm") is not None and pin.get("y_mm") is not None
                           and (number := _pad_number_text(pin.get("number")) or "?") not in unfit_pads]
    unread_fit = [number for number in dict.fromkeys(unfit_pads)
                  if number not in unread_positions and number not in unread_holes]
    unread = unread_positions + unread_holes + unread_fit
    if unread:
        # Only the pad geometry is dropped, never the paid answer; the
        # generation waits for a confirmed reading instead of a guessed one.
        unread_geometry = copy.deepcopy(mapping)
        if pad_problems:
            # Pads no choice of positions can settle (a land that cannot
            # hold its hole, no room at the pitch): `apply_answers` must not
            # turn them back into a footprint.
            unread_geometry["unsettled_pads"] = [number for number, _reason in pad_problems]
        package_geometry = None
    elif mapping is not None:
        # The LAST validation: the pads are valid alone and together (the
        # two checks above), so this only builds the object for the doubts;
        # an error here would be a defect of those checks, reported as the
        # answer's like any other.
        try:
            validated = package_from_mapping(mapping)
        except (KeyError, TypeError, ValueError) as exc:
            raise ExtractionError(tx("Geometria package non valida: {exc}", exc=exc)) from exc
        # The record of the lands the program wrote serves only a dropped
        # geometry (`apply_answers`): a footprint does not carry it.
        mapping.pop(_DERIVED_LANDS_KEY, None)
        package_geometry = mapping
        # Structural contradictions (body over the pads, clockwise numbering,
        # bottom view also declared mirrored) are exposed, never repaired:
        # which reading is true is a question for the user (audit 2026-10-15).
        geometry_doubts = [doubt.as_dict() for doubt in package_doubts(validated)] or None
        notes.extend(doubt["message"] for doubt in geometry_doubts or ())
    # Spacing fields a declared grid lacked: the reason its pads have no
    # coordinate, named instead of «not read» (L1).
    grid_missing = [key for key in data.get("_grid_missing") or ()
                    if key in ("pitch_mm", "row_spacing_mm")] if unread_positions else []

    if layout == "castellated":
        sides = _castellated_sides(
            data.get("castellated_sides"),
            body_length=_optional_number(data.get("body_length_mm"), positive=True),
            body_width=_optional_number(data.get("body_width_mm"), positive=True),
            notes=notes)
        # A castellated module has no matrix: counts or lengths it still sends
        # are informative only and dropped with a note when malformed.
        matrix = _informative_matrix(data, notes)
    elif package_geometry is not None or unread:
        # Pads with unread positions are still what the answer described (the
        # schema: explicit pads, when present, ARE the footprint source): a
        # grid it may also send is not promoted to the footprint in their place.
        sides = None
        matrix = _informative_matrix(data, notes)
    else:
        sides = None
        matrix = _footprint_matrix(data, notes)

    rows, cols = matrix["rows"], matrix["cols"]

    pin_map = None
    # The mirroring of pin_map has already been done (_canonicalize_answer):
    # here the map is seen from the top and is only checked again. For
    # castellated modules the positions come from the sides, not from rows/columns.
    if layout == "matrix" and data.get("pin_map") and cols:
        try:
            pin_map = _pin_map_seen_from_top(data.get("pin_map"), cols, "top", rows)
        except ExtractionError as exc:
            notes.append(tx("ATTENZIONE pin_map scartata ({exc}): numerazione automatica colonna "
                            "per colonna, da verificare sul pinout.", exc=exc))

    max_pin = rows * cols if layout == "matrix" and rows and cols else None
    pin_map_conflict = (data.get("_pin_map_conflict")
                        if isinstance(data.get("_pin_map_conflict"), str) else None)
    pin_map, populated, completion = _resolve_pin_numbering(
        pin_map, data.get("populated_pins"), rows, cols, max_pin,
        dedicated=bool(data.get("_dedicated_map")), notes=notes)
    if completion:
        # A partial map that no single convention completes: the GUI blocks
        # the automatic generation with this text, the user checks.
        pin_map_conflict = completion if not pin_map_conflict else pin_map_conflict + "\n" + completion
    # A castellated module's footprint is its edge sides (validated above):
    # its optional explicit pads, dropped, block nothing (L2). Otherwise the
    # dropped pads WERE the footprint and the generation must wait.
    pads_footprint_dropped = bool(unread) and layout != "castellated"
    if layout == "castellated":
        unread_geometry = None
    if pads_footprint_dropped and unread_holes:
        # No reading of the positions can supply a hole: the block names it,
        # beside any block of the positions (review E1).
        hole_block = tx("Foro dei pad {pads} non stampato e terminale non dichiarato: nessun "
                        "footprint finche' il foro non e' letto dal disegno.",
                        pads=", ".join(unread_holes))
        pin_map_conflict = hole_block if not pin_map_conflict else pin_map_conflict + "\n" + hole_block
    if pads_footprint_dropped and unread_positions and not pin_map_conflict:
        # The dedicated pad reading normally blocks unconfirmed positions
        # already (apply_dedicated_pads); this covers every path that did not
        # run it, so dropped pads never leave the generation unblocked.
        pin_map_conflict = (tx(
            "Griglia dei pad senza {fields}: posizioni dei pad {pads} non calcolabili, nessun "
            "footprint finche' le posizioni non sono confermate.",
            fields=", ".join(grid_missing), pads=", ".join(unread_positions))
            if grid_missing else tx(
            "Posizioni dei pad NON lette (pad {pads} senza coordinate): nessun footprint "
            "finche' le posizioni non sono confermate.", pads=", ".join(unread_positions)))
    if pads_footprint_dropped and unfit_reasons:
        # Values that cannot all be true (review round 4): the block names
        # each pad with the values in conflict and what to verify; no reading
        # of the positions alone settles a land that cannot hold its hole.
        # Added after the block of the positions, so that one never hides
        # the other.
        fit_block = _fit_block(unfit_reasons)
        pin_map_conflict = fit_block if not pin_map_conflict else pin_map_conflict + "\n" + fit_block

    body_length = _opt_float("body_length_mm")
    lead_diameter = _opt_float("lead_diameter_mm")
    if lead_diameter is not None and lead_diameter > _MAX_LEAD_MM:
        # The same bound the pad rules applied (`_part_lead`): a terminal
        # KiCad cannot represent is dropped, said, never an overflow (V1-10).
        notes.append(tx("{key} scartato: valore non valido ({value!r}).", key="lead_diameter_mm",
                        value=lead_diameter))
        lead_diameter = None
    confidence = data.get("confidence", "low")
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    base_notes = data.get("notes", "")
    base_notes = base_notes if isinstance(base_notes, str) else str(base_notes or "")

    def _text(key: str) -> str:
        value = data.get(key)
        return value if isinstance(value, str) else ""

    params = ExtractedParams(
        name=_text("name"),
        description=_text("description"),
        tags=_text("tags"),
        layout=layout,
        rows=rows,
        cols=cols,
        pitch_mm=matrix["pitch_mm"],
        row_spacing_mm=matrix["row_spacing_mm"],
        pad_diameter_mm=matrix["pad_diameter_mm"],
        drill_mm=matrix["drill_mm"],
        castellated_sides=sides,
        components_3d=_components_3d(data.get("components_3d"), notes,
                                     trusted=trusted_components),
        confidence=confidence,
        notes=base_notes,
        populated_pins=populated,
        body_length_mm=body_length,
        body_width_mm=_opt_float("body_width_mm"),
        body_height_mm=_opt_float("body_height_mm"),
        pcb_thickness_mm=_opt_float("pcb_thickness_mm"),
        body_offset_y_mm=_opt_float("body_offset_y_mm"),
        pin_length_3d_mm=_opt_float("pin_length_3d_mm"),
        edge_fillets=_validated_edge_fillets(data.get("edge_fillets"), notes),
        pinout=_validated_pinout(data.get("pinout"), notes),
        pin_map=pin_map,
        body_offset_x_mm=_opt_float("body_offset_x_mm"),
        body_color_hex=data.get("body_color_hex") or None,
        text_color_hex=data.get("text_color_hex") or None,
        package_geometry=package_geometry,
        lead_diameter_mm=lead_diameter,
        lead_exit=_validated_lead_exit(data.get("lead_exit"), notes),
        pin_map_conflict=pin_map_conflict,
        body_conflict=data.get("_body_conflict") if isinstance(data.get("_body_conflict"), str) else None,
        geometry_doubts=geometry_doubts,
        questions=[dict(question) for question in data.get("_questions") or ()
                   if isinstance(question, dict)],
        candidates=copy.deepcopy(data["_candidates"]) if isinstance(data.get("_candidates"), dict)
        else None,
        raw_main=copy.deepcopy(data["_raw_main"]) if isinstance(data.get("_raw_main"), dict) else None,
        unread_pads=list(unread) if pads_footprint_dropped else None,
        unread_pad_geometry=unread_geometry,
    )
    if isinstance(data.get("_numbering_note"), str) and data["_numbering_note"]:
        notes.append(data["_numbering_note"])
    if isinstance(data.get("_view_source"), str) and package_geometry is not None:
        # The side actually applied to the pads and who declared it (C3).
        notes.append(tx("Vista dei terminali applicata: {view} (fonte: {source}).",
                        view=package_geometry.get("source_view", "top"),
                        source=data["_view_source"]))
    params.value_sources = _value_sources(data, params, sides, notes)
    if layout == "castellated":
        _drop_unread_row_spacing(params, notes)
        _apply_measured_lands(data, params, notes)
        _apply_measured_holes(data, params, notes)
        _downgrade_sizes_wider_than_the_pitch(params, notes)
    else:
        _lift_drill_above_the_lead(params, notes)
    if unread_holes:
        # Said apart from the positions: here the hole is what was not read.
        notes.append(tx("Geometria dei pad scartata: per i pad {pads} il disegno non stampa il "
                        "foro e nessun terminale e' dichiarato, ne' per il pad ne' per il "
                        "componente. Si tengono: {kept}; il foro e' da leggere sul disegno.",
                        pads=", ".join(unread_holes), kept=_kept_after_pad_drop(params)))
    if unfit_reasons:
        # Said apart again: these pads have every value, but values that
        # cannot all be true (review round 4).
        notes.append(tx("Geometria dei pad scartata: {reasons}. Si tengono: {kept}; foro, "
                        "terminale, piazzola e posizioni dei pad sono da verificare sul disegno.",
                        reasons="; ".join(unfit_reasons), kept=_kept_after_pad_drop(params)))
    if unread_positions:
        # Named after the fields that survived (M2), once every adjustment
        # above has run; the grid case names the spacing it lacked (L1).
        reason = (tx("griglia senza {fields}", fields=", ".join(grid_missing)) if grid_missing
                  else tx("coordinate assenti"))
        if pads_footprint_dropped:
            notes.append(tx("Geometria dei pad scartata: posizioni non lette per i pad {pads} "
                            "({reason}). Si tengono: {kept}; le posizioni dei pad sono da "
                            "rivedere.", pads=", ".join(unread_positions), reason=reason,
                            kept=_kept_after_pad_drop(params)))
        else:
            notes.append(tx("Pad espliciti facoltativi scartati: posizioni non lette per i pad "
                            "{pads} ({reason}). Il footprint resta quello dei lati del modulo; si "
                            "tengono: {kept}.", pads=", ".join(unread_positions), reason=reason,
                            kept=_kept_after_pad_drop(params)))
    notes.extend(_review_notes(sides, params.value_sources,
                               (params.package_geometry or params.unread_pad_geometry or {}).get("pins")))
    if notes:
        params.notes = (params.notes + "\n" + "\n".join(notes)).strip()
    return params


def _validated_edge_fillets(raw, notes: list) -> Optional[list[dict]]:
    """Optional edge_fillets: only ``{"edge": <EDGE_NAMES>, "radius_mm": > 0}`` entries.

    A malformed entry is dropped with a note and the valid ones stay (review
    D2): the consumer reads ``entry["edge"]`` and ``float(entry["radius_mm"])``
    without checks, so nothing else may reach it. The radius must be a real
    finite number: numeric text is malformed like everywhere else.
    @return The valid fillets with only those two keys, or None when none is.
    """
    if raw is None or raw == []:
        return None
    entries = raw if isinstance(raw, (list, tuple)) else [raw]
    valid = []
    for index, entry in enumerate(entries):
        edge = entry.get("edge") if isinstance(entry, dict) else None
        radius = (_optional_number(entry.get("radius_mm"), positive=True)
                  if isinstance(entry, dict) else None)
        if isinstance(edge, str) and edge in EDGE_NAMES and radius is not None:
            valid.append({"edge": edge, "radius_mm": radius})
        else:
            notes.append(tx("{key} scartato: valore non valido ({value!r}).",
                            key=f"edge_fillets[{index}]", value=entry))
    return valid or None


def _validated_pinout(raw, notes: list) -> Optional[list[dict]]:
    """@brief Optional pinout: ``{"pin": <number or label>, "name": <text>[, "function": <role>]}`` entries.

    @param raw The ``pinout`` value of the model's answer.
    @param notes Extraction notes; every dropped or repaired entry adds one.
    @return The kept entries, each with the keys the schema declares (the
        ``function`` only when it is one of PINOUT_FUNCTIONS: any other
        value is dropped with a note, never mapped; the ``contact`` label
        only as non-blank text), in the source order; None when none is
        kept.
    @note ``pin`` is a nonnegative integer (an integral float such as 3.0
        included; 0 is a real pad number on some packages and the symbol
        and pinout tables accept any pin text -- `_pin_label` accepts it
        too, as the text «0», for pin maps and populated pins) or a nonempty
        label such as "A2" for grid packages. An entry without a usable pin
        id is dropped with a note: there is no pin to keep (review D2).
    @note A missing ``name``, or one that is not text, does not drop the pin: the symbol
        must keep every pin of the source pinout, so the pin stays with an
        empty name and a note names the discarded ``pinout[i].name`` (review G5).
    """
    if raw is None or raw == []:
        return None
    entries = raw if isinstance(raw, (list, tuple)) else [raw]
    valid = []
    for index, entry in enumerate(entries):
        pin = None
        if isinstance(entry, dict):
            raw_pin = entry.get("pin")
            if isinstance(raw_pin, str):
                pin = raw_pin.strip() or None
            else:
                pin = _optional_number(raw_pin, integer=True)
                if pin is not None and pin < 0:
                    pin = None
        if pin is None:
            notes.append(tx("{key} scartato: valore non valido ({value!r}).",
                            key=f"pinout[{index}]", value=entry))
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            # Only the name is discarded (the note names the field): the pin stays.
            notes.append(tx("{key} scartato: valore non valido ({value!r}).",
                            key=f"pinout[{index}].name", value=name))
            name = ""
        entry_out = {"pin": pin, "name": name}
        function = entry.get("function")
        if function is not None:
            if isinstance(function, str) and function.strip().lower() in PINOUT_FUNCTIONS:
                entry_out["function"] = function.strip().lower()
            else:
                notes.append(tx("{key} scartato: valore non valido ({value!r}).",
                                key=f"pinout[{index}].function", value=function))
        contact = entry.get("contact")
        if contact is not None:
            if isinstance(contact, str) and contact.strip():
                entry_out["contact"] = contact.strip()
            else:
                notes.append(tx("{key} scartato: valore non valido ({value!r}).",
                                key=f"pinout[{index}].contact", value=contact))
        valid.append(entry_out)
    return valid or None


def _validated_lead_exit(raw, notes: list) -> Optional[str]:
    """Optional lead_exit: one of LEAD_EXITS, else None with a note ("unknown" is None too)."""
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip().lower() in LEAD_EXITS:
        value = raw.strip().lower()
        return None if value == "unknown" else value
    notes.append(tx("lead_exit scartato: valore non valido ({value!r}).", value=raw))
    return None


#: Floor, in mm, of the agreement between two transcriptions of ONE printed
#: hole: the program's own conversion noise (values are kept to 1e-6 mm). Two
#: transcriptions agree when they differ by less than half a unit of the last
#: printed digit of the coarser one (`_printed_hole_value`, review G6
#: 2026-11-11): «Ø1.02» and «.040» (in) are one hole, 1.2 and 1.3 are two.
_PRINTED_HOLE_TOLERANCE_MM = 1e-4

#: Pad types that have a hole (`PinSpec`): only they carry `drill_mm`.
_HOLED_PAD_TYPES = ("thru_hole", "np_thru_hole")

#: A number as printed in a callout: integer part and optional decimals, or
#: a decimal written without its leading zero («.040», the inch habit).
_PRINTED_NUMBER_RE = re.compile(r"(?<![\d.,])(?:(\d+)(?:[.,](\d+))?|[.,](\d+))")
#: A printed nominal followed by its tolerance («1.3+0.1», «1.3 ±0.05»,
#: «1.3+/-0.1», «.040 ±.003»): nominal, sign, tolerance.
_PRINTED_TOLERANCE_RE = re.compile(
    r"(?<![\d.,])(\d+(?:[.,]\d+)?|[.,]\d+)\s*(\+/-|±|\+|-)\s*(\d+(?:[.,]\d+)?|[.,]\d+)")
#: What follows a COUNT of holes in a callout: a hyphen or a times sign and
#: then the hole symbol («2-Ø1.6», «4 x Ø1.0»), or a times sign that no
#: number follows («2x», «(5x)», «4X PLCS»).
_COUNT_BEFORE_SYMBOL_RE = re.compile(r"\s*[-xX×*]\s*[Øø⌀Φφ]")
_COUNT_TIMES_RE = re.compile(r"\s*[xX×](?!\s*[\d.,Øø⌀Φφ])")
#: A hyphen and then a number: after the FIRST integer of a callout this is
#: the count idiom too («4-1.0», «2-0.8x2.0»).
_COUNT_HYPHEN_RE = re.compile(r"\s*-\s*[\d.,]")


def _transcribed_decimals(value: float) -> int:
    """Digits after the decimal point of a number as the model transcribed it (shortest repr)."""
    import decimal
    exponent = decimal.Decimal(repr(float(value))).normalize().as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def _printed_numbers(text: str) -> list:
    """The numbers of a hole callout as (value, decimals, is_count), in print order.

    A COUNT of holes is not a size (review F6-3, F7-8): an integer followed
    by a hyphen or a times sign and the hole symbol, an integer followed by
    a times sign with no number after it, or the first integer of the
    callout followed by a hyphen and a number. DECLARED HEURISTIC, built on
    how callouts are written, not on a part: «3 x 1.5» stays ambiguous (three
    holes of 1.5, or a 3 x 1.5 slot) and is NOT taken for a count; a count
    written in words («4 holes») is not recognised.
    """
    found = []
    for index, match in enumerate(_PRINTED_NUMBER_RE.finditer(text)):
        whole, decimals = match.group(1), match.group(2) or match.group(3) or ""
        value = float((whole or "0") + ("." + decimals if decimals else ""))
        rest = text[match.end():]
        count = not decimals and bool(
            _COUNT_BEFORE_SYMBOL_RE.match(rest) or _COUNT_TIMES_RE.match(rest)
            or (index == 0 and _COUNT_HYPHEN_RE.match(rest)))
        found.append((value, len(decimals), count))
    return found


def _printed_hole_value(text, value, factor: float) -> tuple:
    """@brief One transcribed hole size as (value, rounding), both in mm, checked against its printed text.

    @param text The callout as printed (`hole_callout`, or the dedicated
        reading's `text`), any type.
    @param value The number the reading sent for it, in the drawing unit.
    @param factor The drawing unit to millimetres.
    @return (value_mm, rounding_mm):
        - a SIZE of the text (never a count of holes, `_printed_numbers`)
          that equals `value` within its own last digit: `value`, rounded
          by half a unit of that printed digit (the finest such number when
          several do);
        - else a printed nominal with a tolerance whose range holds `value`
          (a reading that added the tolerance): the NOMINAL, rounded by its
          own last digit -- the hole is the nominal. The range follows the
          printed sign: «+t» is [nominal, nominal + t], «-t» is
          [nominal - t, nominal], «±t» both;
        - else (no text, or a text that does not state the value): `value`,
          rounded by half a unit of its own last transcribed digit.
        The rounding is never coarser than the unit's usual printed
        precision (CHAIN_QUOTE_ROUNDING_MM), in every case.
    @note Why the cap (review F6-3, F7-8): a drill callout printed without
        decimals («Ø1») names a drill size, it is not a quote rounded to the
        millimetre; half a unit of that digit (0.5 mm) made «Ø1» agree with
        a second reading of 1.4. The cap is the precision already assumed
        for a quote whose text is unknown, not a value tuned to a part.
    @note «a-b» is a tolerance only when b is smaller than a («1.3-0.1»);
        «1.0-1.2» is a RANGE between two sizes and names no nominal, and a
        count before a size («2-0.8») is neither: both fall to the value as
        transcribed (review F6-6).
    @note Derived from the transcriptions, not tuned: the agreement margin of
        two readings (`_same_printed_hole`) is the coarser of their roundings.
    """
    number = float(value)
    cap = CHAIN_QUOTE_ROUNDING_MM.get(factor, CHAIN_QUOTE_ROUNDING_MM[1.0])
    if isinstance(text, str):
        # The finest printed SIZE that states the value: in «2-0.8x2.0» the
        # size 2.0 is «2.0» (a tenth), never the count «2».
        roundings = []
        for printed, decimals, count in _printed_numbers(text):
            rounding = min(0.5 * 10 ** (-decimals) * factor, cap)
            if not count and abs(printed - number) * factor <= rounding + 1e-9:
                roundings.append(rounding)
        if roundings:
            return number * factor, min(roundings)
        counts = {match.start(): is_count for match, (_v, _d, is_count)
                  in zip(_PRINTED_NUMBER_RE.finditer(text), _printed_numbers(text))}
        for match in _PRINTED_TOLERANCE_RE.finditer(text):
            nominal_text, sign, tolerance_text = (group.replace(",", ".") for group in match.groups())
            nominal, tolerance = float("0" + nominal_text if nominal_text.startswith(".") else nominal_text), \
                float("0" + tolerance_text if tolerance_text.startswith(".") else tolerance_text)
            if counts.get(match.start()) or (sign == "-" and tolerance >= nominal):
                # A count before a size, or a range between two sizes.
                continue
            low = nominal - tolerance if sign in ("-", "±", "+/-") else nominal
            high = nominal + tolerance if sign in ("+", "±", "+/-") else nominal
            if abs(number - nominal) > 1e-9 and low - 1e-9 <= number <= high + 1e-9:
                decimals = len(nominal_text.partition(".")[2])
                return nominal * factor, min(0.5 * 10 ** (-decimals) * factor, cap)
    return number * factor, min(0.5 * 10 ** (-_transcribed_decimals(number)) * factor, cap)


def _record_pad_hole_precision(data: dict, factor: float, notes: list) -> None:
    """Record, before the unit conversion, the rounding of every explicit pad's transcribed hole.

    Each pad with a `drill_mm` gets `_hole_rounding_mm` (mm, from
    `_printed_hole_value` with its own `hole_callout`); a drill transcribed
    as nominal plus tolerance is brought to the printed nominal, and `notes`
    (the canonical notes) says so. A pad with a `lead` gets
    `_lead_rounding_mm` (mm, the coarsest rounding of its transcribed
    sizes). `_resolve_one_pad` consumes both marks; they never reach the
    validated geometry.
    """
    geometry = data.get("package_geometry")
    pins = geometry.get("pins") if isinstance(geometry, dict) else None
    nominal = []
    for pin in pins if isinstance(pins, list) else ():
        if not isinstance(pin, dict):
            continue
        lead = pin.get("lead")
        sizes = [size for key in _LEAD_LENGTH_KEYS
                 if (size := _optional_number(lead.get(key), positive=True)) is not None
                 ] if isinstance(lead, dict) else []
        if sizes:
            # The terminal's transcription has its own last digit, known
            # only here: the margin within which a second reading of it
            # agrees (`_confirmed_lead_word`).
            pin["_lead_rounding_mm"] = max(_printed_hole_value(None, size, factor)[1]
                                           for size in sizes)
        drill = _optional_number(pin.get("drill_mm"), positive=True)
        if drill is None:
            continue
        value_mm, rounding_mm = _printed_hole_value(pin.get("hole_callout"), drill, factor)
        pin["_hole_rounding_mm"] = rounding_mm
        if abs(value_mm / factor - drill) > 1e-9:
            # Still in the drawing unit: the conversion follows.
            pin["drill_mm"] = value_mm / factor
            nominal.append(f"{_pad_number_text(pin.get('number')) or '?'} "
                           f"(«{str(pin.get('hole_callout')).strip()}»)")
    if nominal:
        notes.append(tx(
            "Fori stampati con tolleranza presi al valore nominale: pad {pads}.",
            pads=", ".join(nominal)))


def _slot_along_pad(pin: dict, first: float, second: float):
    """Orient a slot read without axes (width and length) to its pad, or None when the pad cannot say.

    Contract of `PinSpec` and `core.tht_estimates.lead_passes`: `drill_mm`
    runs along the pad's LENGTH (`pad_length_mm`), `drill_length_mm` along
    its WIDTH. The slot's longer side goes on the pad's longer side: a slot
    across an elongated pad cannot hold a ring on its short sides. A pad
    with two equal sides (round or square), or without both sizes, gives no
    orientation; neither does a pad the oriented slot does not fit inside.

    @return (drill_mm, drill_length_mm) along the pad's axes, or None.
    """
    length = _optional_number(pin.get("pad_length_mm"), positive=True)
    width = _optional_number(pin.get("pad_width_mm"), positive=True)
    if length is None or width is None or abs(length - width) <= _PRINTED_HOLE_TOLERANCE_MM:
        return None
    long_side, short_side = max(first, second), min(first, second)
    along, across = (long_side, short_side) if length > width else (short_side, long_side)
    if along >= length or across >= width:
        return None
    return along, across


def _round_hole_loose_on_blade(lead, drill: float) -> bool:
    """Whether an estimated ROUND hole leaves a flat lead more free hole across its thickness than the land has copper.

    Criterion (declared, derived from the estimate's own rule, review G5
    2026-11-11): a round hole clears a blade's DIAGONAL, so across the
    blade's thickness the gap on each side is (hole - thickness) / 2. The
    estimated land gives the joint HOLE_TO_PAD_ANNULUS_MM / 2 of copper per
    side to wet. When the air the solder must bridge across the thickness is
    wider than that copper, the round hole is the wrong shape for the blade
    and a printed slot is needed. A round lead never trips it (its gap is
    LEAD_TO_HOLE_CLEARANCE_MM / 2). It depends on HOLE_TO_PAD_ANNULUS_MM, a
    declared heuristic itself; a printed slot or land makes it moot.
    """
    if not isinstance(lead, dict) or lead.get("shape") != "flat":
        return False
    thickness = _optional_number(lead.get("thickness_mm"), positive=True)
    width = _optional_number(lead.get("width_mm"), positive=True)
    if thickness is None or width is None:
        return False
    return (drill - min(thickness, width)) / 2 > HOLE_TO_PAD_ANNULUS_MM / 2 + 1e-9


def _pad_word(raw, key: str) -> Optional[str]:
    """The provenance a pad entry's own `value_sources` declares for `key`, or None."""
    word = raw.get(key) if isinstance(raw, dict) else None
    return word if isinstance(word, str) and word in VALUE_SOURCES else None


def _dedicated_hole(dedicated, number: str):
    """The dedicated reading's printed hole of pad `number`: exact name, else the one name equal ignoring case."""
    if not isinstance(dedicated, dict) or not number:
        return None
    if number in dedicated:
        return dedicated[number]
    matches = [key for key in dedicated if str(key).lower() == number.lower()]
    return dedicated[matches[0]] if len(matches) == 1 else None


def _same_printed_hole(drill, length, other: dict, rounding_mm: float = 0.0) -> bool:
    """Whether a hole (drill, slot side or None) is the printed hole `other` of the other reading.

    A round hole compares its diameter; a slot compares its two sides as an
    unordered pair (the dedicated reading gives width and length, the pad
    gives the sides along its own axes). Two transcriptions agree when they
    differ by less than the coarser of their two roundings -- `rounding_mm`
    of this one, `other["rounding_mm"]` of the other: half a unit of the last
    printed digit in each reading's own unit (`_printed_hole_value`, review
    G6) -- or by the conversion noise alone (_PRINTED_HOLE_TOLERANCE_MM).
    """
    first = sorted(v for v in (drill, length) if v is not None)
    second = sorted(v for v in (other.get("drill_mm"), other.get("drill_length_mm")) if v is not None)
    if len(first) == 2 and first[0] == first[1]:
        first = first[:1]
    if len(second) == 2 and second[0] == second[1]:
        second = second[:1]
    margin = max(float(rounding_mm or 0.0), float(other.get("rounding_mm") or 0.0))
    # Never wider than the coarsest printed precision of any unit: a
    # rounding that did not come from `_printed_hole_value` (a stored or
    # foreign value) must not make two different holes agree (review F7-8).
    margin = min(margin, max(CHAIN_QUOTE_ROUNDING_MM.values()))
    return len(first) == len(second) and all(
        abs(a - b) <= _PRINTED_HOLE_TOLERANCE_MM or abs(a - b) < margin
        for a, b in zip(first, second))


def _hole_text_mm(drill, length) -> str:
    """A hole for the notes: «1.20 mm» or «1.20 x 2.00 mm» for a slot."""
    if drill is None:
        return "?"
    if length is None or abs(length - drill) <= _PRINTED_HOLE_TOLERANCE_MM:
        return f"{drill:.2f} mm"
    return f"{drill:.2f} x {length:.2f} mm"


def _part_lead(data: dict):
    """The part-level terminal of an answer as a round lead and its provenance word, or None.

    `lead_diameter_mm` (mm once the answer is canonical) is the one lead the
    answer declares for the whole part; its provenance is the answer-level
    `value_sources.lead_diameter_mm`, "estimated" when undeclared.
    """
    diameter = _optional_number(data.get("lead_diameter_mm"), positive=True)
    if diameter is None or diameter > _MAX_LEAD_MM:
        # Beyond KiCad's range it is no terminal (V1-10): the result drops
        # the field with a note (`_params_from_tool_arguments`).
        return None
    top = data.get("value_sources") if isinstance(data.get("value_sources"), dict) else {}
    return {"shape": "round", "diameter_mm": diameter}, _pad_word(top, "lead_diameter_mm") or "estimated"


def _resolve_pad_terminals(package_geometry, data: dict, notes: list, *, lift: bool = True) -> tuple:
    """Settle the hole, the land and their provenance of every explicit pad, in ONE place, before any validation.

    INVARIANT (review round 4, 2026-11-11): every change the program makes
    to a pad's hole or land happens here, in this order, and nothing changes
    a pad afterwards:
    1. per pad, the hole and its provenance, then the land that follows it
       (`_resolve_one_pad`);
    2. the holes judged against the terminals, per pad and part level
       (`_judge_holes_against_leads`; `lift` False for a castellated module);
    3. the lands the program wrote kept apart from every neighbour
       (`_keep_derived_lands_apart`);
    4. every through-hole pad checked: its land must hold its hole
       (`_pad_unfit_reason`).
    A pad the rules cannot settle -- a READ land that cannot hold the hole,
    no room at the pitch for the hole with its ring -- is returned as a
    problem, never repaired with an invented value and never left to raise:
    `_params_from_tool_arguments` returns the pads as UNREAD geometry with
    a `pin_map_conflict` naming them.

    The ONE rule of the program for the values a drawing does not print
    (user, 2026-11-10, «ok procedi», contract A1-A3 of the handoff): the
    model transcribes, the program derives. Per pad with a hole:
    - hole PRINTED (the pad's `value_sources.drill_mm` is "read", or, for a
      pad that declares nothing, the answer-level `value_sources.drill_mm`
      -- extended only when every holed pad has the same drill, or when the
      pad's drill is one the dedicated reading transcribed: with mixed holes
      one printed callout must not make the invented ones «read», review
      G3): kept, source "read". The dedicated pad reading transcribes the
      printed callouts too (`printed_holes`): the same value in both (within
      the coarser rounding of the two, `_same_printed_hole`) is "read"; two
      different values are flagged for review -- the main value stays, its
      source is "estimated" and the note names both, never an average and
      never silently one of them; a value only one reading saw is kept as
      "read" with a note naming that reading. A slot only the dedicated
      reading saw has no axes: it is oriented to the pad
      (`_slot_along_pad`), or left out with a note when the pad cannot say.
      A land not printed around a printed hole is derived from the hole,
      source "derived": the round pad `estimated_pad_diameter(hole)` for a
      round hole, the slot plus HOLE_TO_PAD_ANNULUS_MM on both axes for a
      slot (`_write_slot_land`); its shape stays the declared one when the
      pad says its shape was read (review E3). A printed land is never
      changed: when it cannot hold the hole the pad is a problem.
    - hole not printed, terminal declared by the pad (`lead`), or else the
      part-level `lead_diameter_mm` as a round lead (review E1; applied to a
      pad that carries a model drill only when every holed pad has one
      drill): hole and ROUND pad from `core.tht_estimates.estimated_round_pad`,
      sources "derived" when the terminal was read (or derived) and
      "estimated" otherwise; a printed land (`value_sources.pad_size`
      "read") is kept with its shape. A terminal is «read» only when TWO
      independent readings transcribed the same one for the pad
      (`_confirmed_lead_word`, user 2026-11-11): read once, or read
      differently by the two, it is "estimated" and so are the hole and
      the land derived from it. An estimated round hole too loose
      across a flat lead is named (`_round_hole_loose_on_blade`, review G5).
    - no terminal, a model drill: the model's values stay, marked
      "estimated", and the note says so.
    - no terminal and no drill at all: nothing is invented; the pad keeps no
      hole and `_params_from_tool_arguments` drops the pad geometry as
      UNREAD (`_pads_without_holes`), the generation blocked with its remedy.
    A slot is used only when printed. A pad without a hole carrying a drill
    (an SMD pad with `drill_mm`, which `PinSpec` refuses) loses the drill
    with a note naming it: the type or the hole is wrong, and which one is
    for the user to check; a drill of 0 there is the usual way to say «no
    hole» and is dropped without a word (review F7-3). Pads without a hole
    keep only the provenance they declare. On a pad WITH a hole a zero
    `drill_length_mm` is «no slot» and a zero `drill_mm` is «hole not
    printed» (review round 5, V1-6): both absent, the hole then derived
    from the terminal or, without one, unread; a note names the pads. A
    printed slot keeps its transcribed axes; only a READ land that holds it
    no other way turns it, and the hole is then "derived"
    (`_slot_on_pad_axes`, V1-3). A pad without a `shape` gets the
    footprint's default written, never marked "read" (V1-7).

    @param package_geometry The explicit geometry, canonical (millimetres).
    @param data The canonical answer: its `value_sources` (answer level),
        `lead_diameter_mm` and `_dedicated_holes` (the printed holes of the
        dedicated reading, set by apply_dedicated_pads only when that
        reading ran and the drawing did not contradict it) and
        `_dedicated_leads` (the terminals it transcribed, set only when it
        was also made by another model than the main answer).
    @param notes Receives one line per kind of outcome.
    @param lift False keeps step 2 off (a castellated module: its explicit
        pads are optional data and were never judged against the lead).
    @return (geometry, problems). `geometry` is a NEW geometry whose pins
        carry `lead` (mm, validated) and `sources` (`core.tht_estimates`
        contract); the model's raw `lead`, `hole_callout`, `value_sources`
        and `sources` are not passed on. `problems` lists (pad number,
        translated reason, which does not name the pad:
        `_grouped_pad_reasons` does) for every pad the rules could not
        settle, in pad order; empty when the pads are settled. The geometry
        carries `_DERIVED_LANDS_KEY` when the program wrote lands (for
        `apply_answers`; dropped from a validated footprint). Anything that is not a
        geometry with a pin list is returned as is, with no problem.
    @note Not tuned: every value comes from the declared provenance and
        from the published rule of `core.tht_estimates` (clearance and
        annulus are declared there with their sources and limits).
    """
    if not isinstance(package_geometry, dict) or not isinstance(package_geometry.get("pins"), list):
        return package_geometry, []
    geometry = copy.deepcopy(package_geometry)
    top = data.get("value_sources") if isinstance(data.get("value_sources"), dict) else {}
    dedicated = data.get("_dedicated_holes") if isinstance(data.get("_dedicated_holes"), dict) else None
    # The terminals an INDEPENDENT dedicated reading transcribed; None when
    # there is none (it did not run, the same model made it, the drawing
    # contradicted it, an answer saved before it existed).
    dedicated_leads = (data.get("_dedicated_leads")
                       if isinstance(data.get("_dedicated_leads"), dict) else None)
    pins = [pin for pin in geometry["pins"] if isinstance(pin, dict)]
    # One drill on every holed pad: the answer-level words and the one
    # part-level lead may stand for all of them. Different drills mean
    # different terminals, which one answer-level word cannot describe.
    drills = {round(drill, 6) for pin in pins if pin.get("pad_type", "smd") in _HOLED_PAD_TYPES
              and (drill := _optional_number(pin.get("drill_mm"), positive=True)) is not None}
    uniform = len(drills) <= 1
    printed = [hole for hole in (dedicated or {}).values()
               if isinstance(hole, dict) and hole.get("drill_mm") is not None]
    part_lead = _part_lead(data)
    found = {name: [] for name in (
        "smd_drill", "lead_dropped", "slot_dropped", "only_main", "only_dedicated", "disagree",
        "unfounded", "withheld", "part_lead", "lead_withheld", "slot_unoriented", "loose_blade",
        "land_capped", "land_model", "slot_turned", "thin_ring", "lead_contradicted",
        "lead_lifted", "widened", "part_narrow", "part_protected", "part_lifted", "part_widened",
        "zero_hole", "lead_once", "lead_disagree")}
    # The lands the program wrote, {id(pin): (pin, the land it replaced)}:
    # the only ones `_keep_derived_lands_apart` may take back or reduce.
    derived: dict = {}
    for pin in pins:
        drill_word = _pad_word(top, "drill_mm")
        if (drill_word == "read" and not uniform and pin.get("pad_type", "smd") in _HOLED_PAD_TYPES
                and _pad_word(pin.get("value_sources"), "drill_mm") is None):
            drill = _optional_number(pin.get("drill_mm"), positive=True)
            length = _optional_number(pin.get("drill_length_mm"), positive=True)
            rounding = _optional_number(pin.get("_hole_rounding_mm"), positive=True) or 0.0
            if drill is not None and not any(_same_printed_hole(drill, length, hole, rounding)
                                             for hole in printed):
                drill_word = None
                found["withheld"].append(_pad_number_text(pin.get("number")) or "?")
        fallback = {"drill_mm": drill_word, "pad_size": _pad_word(top, "pad_diameter_mm")}
        _resolve_one_pad(pin, fallback, dedicated, found, part_lead, uniform, derived,
                         dedicated_leads)
    if lift:
        _judge_holes_against_leads(pins, part_lead, _part_lead_word(data, part_lead), found, derived)
    # {id(pin): reason} of the pads no rule could settle (steps 3 and 4).
    unfit = _settle_lands_and_fit(pins, derived, found)
    if found["zero_hole"]:
        notes.append(tx("Pad {pads}: foro 0 nella risposta inteso come «foro non stampato»: "
                        "ricavato dal terminale quando e' dichiarato, altrimenti non letto.",
                        pads=_bounded_pad_list(found["zero_hole"])))
    if found["smd_drill"]:
        notes.append(tx("Pad {pads}: dichiarati senza foro ma con un foro nella risposta: foro "
                        "scartato, tipo del pad da verificare sul disegno.",
                        pads=", ".join(found["smd_drill"])))
    if found["lead_dropped"]:
        notes.append(tx("Pad {pads}: terminale dichiarato non valido, ignorato.",
                        pads=", ".join(found["lead_dropped"])))
    if found["slot_dropped"]:
        notes.append(tx("Pad {pads}: asola non stampata sul disegno, ignorata: un'asola si usa "
                        "solo se stampata.", pads=", ".join(found["slot_dropped"])))
    if found["withheld"]:
        notes.append(tx("Pad {pads}: la provenienza «read» del foro dichiarata per tutta la "
                        "risposta non vale per loro: i pad hanno fori diversi e il loro foro non "
                        "e' fra quelli trascritti dalla lettura dedicata.",
                        pads=", ".join(found["withheld"])))
    if found["disagree"]:
        notes.append(tx("ATTENZIONE foro stampato letto diversamente dalle due letture: {details}. "
                        "Nessuno dei due valori e' adottato come letto: resta quello della "
                        "risposta principale, segnato come stimato, da verificare sul disegno.",
                        details="; ".join(found["disagree"])))
    if found["only_main"]:
        notes.append(tx("Pad {pads}: foro stampato letto solo dalla risposta principale (la "
                        "lettura dedicata non lo riporta).", pads=", ".join(found["only_main"])))
    if found["only_dedicated"]:
        notes.append(tx("Pad {pads}: foro stampato letto solo dalla lettura dedicata, usato.",
                        pads=", ".join(found["only_dedicated"])))
    if found["slot_unoriented"]:
        notes.append(tx("Pad {pads}: asola stampata letta solo dalla lettura dedicata, senza assi: "
                        "il pad non ne indica l'orientamento (lati uguali, o asola piu' grande del "
                        "pad). Asola non usata, da verificare sul disegno.",
                        pads=", ".join(found["slot_unoriented"])))
    if found["slot_turned"]:
        notes.append(tx("Pad {pads}: asola stampata messa col lato lungo sul lato lungo della "
                        "piazzola: orientamento da verificare sul disegno.",
                        pads=", ".join(found["slot_turned"])))
    if found["part_lead"]:
        lead, word = part_lead
        notes.append(tx("Pad {pads}: foro non stampato e terminale del pad non dichiarato: foro e "
                        "piazzola ricavati dal terminale dichiarato per il componente (Ø{lead:.2f} "
                        "mm, provenienza {source}).", pads=", ".join(found["part_lead"]),
                        lead=lead["diameter_mm"], source=word))
    if found["lead_withheld"]:
        notes.append(tx("Pad {pads}: foro non stampato e terminale del pad non dichiarato; il "
                        "terminale del componente (Ø{lead:.2f} mm) non e' applicato perche' i pad "
                        "hanno fori diversi: foro e piazzola sono quelli del modello, STIMATI.",
                        pads=", ".join(found["lead_withheld"]),
                        lead=part_lead[0]["diameter_mm"]))
    if found["unfounded"]:
        notes.append(tx("Pad {pads}: ne' foro stampato ne' terminale dichiarato: foro e piazzola "
                        "sono quelli del modello, STIMATI.", pads=", ".join(found["unfounded"])))
    if found["lead_disagree"]:
        notes.append(tx("ATTENZIONE terminale letto diversamente dalle due letture: {details}. "
                        "Nessuno dei due valori e' adottato come letto: per la stima resta quello "
                        "della risposta principale, da verificare sul disegno.",
                        details="; ".join(found["lead_disagree"])))
    if found["lead_once"]:
        notes.append(tx("Pad {pads}: terminale letto una sola volta, senza una seconda lettura "
                        "indipendente che lo confermi: considerato STIMATO, come il foro e la "
                        "piazzola che il programma ne ricava. Da verificare sul disegno.",
                        pads=_bounded_pad_list(found["lead_once"])))
    if found["lead_contradicted"]:
        notes.append(tx("ATTENZIONE pad {numbers}: il terminale dichiarato non passa nel foro letto "
                        "dal disegno. Foro non modificato: terminale o foro letti male, da "
                        "verificare sul disegno.", numbers=", ".join(found["lead_contradicted"])))
    if found["lead_lifted"]:
        notes.append(tx("ATTENZIONE pad {numbers}: il foro non lasciava passare il terminale "
                        "dichiarato dal pad: foro ricavato dal terminale, valore STIMATO da "
                        "verificare.", numbers=", ".join(found["lead_lifted"])))
    if found["widened"]:
        notes.append(tx("Piazzole dei pad {numbers} allargate attorno al foro ricavato, "
                        "conservandone l'orientamento: valori STIMATI da verificare.",
                        numbers=", ".join(found["widened"])))
    if part_lead is not None and (found["part_narrow"] or found["part_protected"] or found["part_lifted"]):
        lead = part_lead[0]["diameter_mm"]
        lifted = estimated_hole(part_lead[0])
        if found["part_narrow"]:
            notes.append(tx("ATTENZIONE fori dei pad {numbers} non maggiori del terminale "
                            "{lead:.2f} mm dichiarato; i pad hanno fori diversi, quindi il "
                            "terminale puo' riguardarne solo alcuni: fori non modificati, da "
                            "verificare.", numbers=", ".join(found["part_narrow"]), lead=lead))
        if found["part_protected"]:
            notes.append(tx("ATTENZIONE fori letti dei pad {numbers} non maggiori del terminale "
                            "{lead:.2f} mm, che non e' letto dal disegno: fori non modificati, da "
                            "verificare.", numbers=", ".join(found["part_protected"]), lead=lead))
        if found["part_lifted"] and found["part_widened"]:
            notes.append(tx(
                "ATTENZIONE fori dei pad {numbers} non maggiori del terminale {lead:.2f} mm: un "
                "terminale non passa in un foro del suo diametro. Fori portati a {lifted:.2f} mm "
                "(terminale + {clearance:.2f} mm), piazzole troppo strette allargate: valori STIMATI "
                "da verificare.", numbers=", ".join(found["part_lifted"]), lead=lead, lifted=lifted,
                clearance=LEAD_TO_HOLE_CLEARANCE_MM))
        elif found["part_lifted"]:
            notes.append(tx(
                "ATTENZIONE fori dei pad {numbers} non maggiori del terminale {lead:.2f} mm: un "
                "terminale non passa in un foro del suo diametro. Fori portati a {lifted:.2f} mm "
                "(terminale + {clearance:.2f} mm): valori STIMATI da verificare.",
                numbers=", ".join(found["part_lifted"]), lead=lead, lifted=lifted,
                clearance=LEAD_TO_HOLE_CLEARANCE_MM))
    if found["loose_blade"]:
        notes.append(_loose_blade_note(found["loose_blade"]))
    notes.extend(_land_notes(found))
    problems = [(_pad_number_text(pin.get("number")) or "?", unfit[id(pin)])
                for pin in pins if id(pin) in unfit]
    if derived:
        # Kept for `apply_answers` (review round 5, V1-4): the lands the
        # program wrote, by pad index, each with the land it replaced, so
        # that steps 3 and 4 can run again once a chosen layout has placed
        # the pads. Dropped from a geometry that becomes the footprint.
        geometry[_DERIVED_LANDS_KEY] = [
            {"index": index, "land": derived[id(pin)][1]}
            for index, pin in enumerate(geometry["pins"]) if isinstance(pin, dict) and id(pin) in derived]
    return geometry, problems


#: Key of an explicit geometry under which `_resolve_pad_terminals` records
#: the lands the program wrote (`apply_answers` reads it; it never reaches a
#: validated footprint).
_DERIVED_LANDS_KEY = "derived_lands"


def _settle_lands_and_fit(pins: list, derived: dict, found: dict) -> dict:
    """Steps 3 and 4 of `_resolve_pad_terminals`: lands kept apart, then every land checked against its hole.

    @param pins The pin dicts of the geometry (changed in place by step 3).
    @param derived {id(pin): (pin, the land the program replaced)}.
    @param found Outcome lists (`land_capped`, `land_model`, `thin_ring`).
    @return {id(pin): reason} of the pads no rule could settle; the reason
        does not name the pad (`_grouped_pad_reasons` does).
    """
    unfit: dict = {}
    _keep_derived_lands_apart(pins, derived, found, unfit)
    for pin in pins:
        if id(pin) not in unfit:
            reason = _pad_unfit_reason(pin, found)
            if reason:
                unfit[id(pin)] = reason
    return unfit


def _land_notes(found: dict) -> list:
    """The notes of what steps 3 and 4 did to the lands (`_settle_lands_and_fit`), one per kind of outcome."""
    notes = []
    if found.get("land_capped"):
        notes.append(tx("Pad {pads}: piazzola ricavata dal foro ridotta per lasciare {gap:.2f} mm "
                        "di rame libero dal pad vicino: valore STIMATO, da verificare.",
                        pads=", ".join(found["land_capped"]), gap=MIN_PAD_GAP_MM))
    if found.get("land_model"):
        notes.append(tx("Pad {pads}: la piazzola ricavata dal foro toccherebbe il pad vicino: "
                        "tenuta quella della risposta, STIMATA, da verificare.",
                        pads=", ".join(found["land_model"])))
    if found.get("thin_ring"):
        notes.append(tx("ATTENZIONE pad {pads}: la piazzola letta dal disegno lascia meno di "
                        "{ring:.2f} mm di corona attorno al foro che il programma ha ricavato: "
                        "piazzola non modificata, foro e piazzola da verificare.",
                        pads=", ".join(found["thin_ring"]), ring=MIN_ANNULAR_RING_MM))
    return notes


#: How many groups of pads (one reason, one pair of values each) a block
#: names before it counts the rest. A presentation bound like
#: `generators.package.ESTIMATED_PADS_LISTED`, which it reuses, not a
#: geometric threshold: every pad stays in `unread_pads` and in the preview.
_PAD_REASON_GROUPS_LISTED = ESTIMATED_PADS_LISTED

#: The tail `estimated_pad_list` writes when it leaves pads out (fixed file
#: language there), recognised to be said in the interface language here.
_PAD_LIST_MORE_RE = re.compile(
    r"\s*" + re.escape(ESTIMATED_PADS_MORE).replace(r"\{count\}", r"(\d+)") + r"$")


def _bounded_pad_list(numbers) -> str:
    """Pad numbers for a message: unique, runs compressed, bounded (`generators.package.estimated_pad_list`).

    The list a block or a note prints must not grow with the pad count
    (review round 5, V1-9). The compression and the bound are the
    footprint note's own; only its Italian tail is said through the catalog.
    """
    text = estimated_pad_list(numbers)
    match = _PAD_LIST_MORE_RE.search(text)
    if match:
        text = text[:match.start()] + " " + tx("e altri {count}", count=int(match.group(1)))
    return text


def _grouped_pad_reasons(problems) -> list:
    """The reasons of unsettled pads as bounded text: one entry per (reason, values), naming its pads.

    @param problems (pad number, reason without the pad) pairs, in pad order.
    @return Entries «pad 1-3, 7: reason», in order of first appearance: pads
        that share one reason and one pair of values share one entry, so a
        part with 200 identical pads writes one line, not 200 (review round
        5, V1-9: a 24,000-character block). At most
        `_PAD_REASON_GROUPS_LISTED` entries; the pads of the others are
        counted in a last entry.
    """
    groups: dict = {}
    for number, reason in problems:
        groups.setdefault(reason, []).append(number)
    entries = [tx("pad {pads}: {reason}", pads=_bounded_pad_list(numbers), reason=reason)
               for reason, numbers in list(groups.items())[:_PAD_REASON_GROUPS_LISTED]]
    left = sum(len(numbers) for numbers in list(groups.values())[_PAD_REASON_GROUPS_LISTED:])
    if left:
        entries.append(tx("e altri {count} pad", count=left))
    return entries


def _fit_block(reasons) -> str:
    """The block of pads whose values cannot all be true, naming them with the values and what to verify."""
    return tx("Fori, piazzole o posizioni dei pad incompatibili fra loro ({reasons}): "
              "nessun footprint finche' foro, terminale, piazzola e posizioni di quei pad "
              "non sono verificati sul disegno; poi ripetere l'analisi o correggere i "
              "documenti.", reasons="; ".join(reasons))


def _loose_blade_note(numbers) -> str:
    """The note naming pads whose estimated round hole is too loose on their flat lead (`_round_hole_loose_on_blade`)."""
    return tx("Pad {pads}: foro TONDO stimato per un terminale piatto: attraverso lo spessore "
              "resta piu' foro libero che rame di piazzola. Serve un'asola stampata sul disegno o "
              "una verifica.", pads=", ".join(numbers))


def _write_derived_land(pin: dict, diameter: float, declared_shape) -> None:
    """Write a land the program derived: both sides `diameter`; the shape too unless the pad's shape was read (E3)."""
    pin["pad_length_mm"] = pin["pad_width_mm"] = diameter
    if declared_shape != "read":
        pin["shape"] = "circle"


def _write_slot_land(pin: dict, drill: float, length: float, declared_shape) -> None:
    """Write the land the program derives around a PRINTED slot (review F6-1).

    The slot's two sides lie along the pad's own axes (`PinSpec`: `drill`
    along the pad length, `length` along the pad width); the land exceeds
    the slot by HOLE_TO_PAD_ANNULUS_MM on BOTH axes -- the same ring per
    side a round hole gets from `estimated_pad_diameter` -- so its long
    side is the slot's long side. The shape becomes "oval" (a stadium, the
    slot's own outline grown by the ring) unless the pad says its shape was
    read and that shape can have two sides.
    """
    pin["pad_length_mm"] = round(drill + HOLE_TO_PAD_ANNULUS_MM, 6)
    pin["pad_width_mm"] = round(length + HOLE_TO_PAD_ANNULUS_MM, 6)
    if declared_shape != "read" or pin.get("shape") not in ("rect", "roundrect", "oval"):
        pin["shape"] = "oval"


def _slot_on_pad_axes(pin: dict, drill: float, length: float, land_read: bool) -> tuple:
    """A printed slot's two sides on the pad's axes; turned only when a READ land holds it no other way.

    The answer gives the slot along the pad's own axes: the schema and the
    prompt define them (`drill_mm` along X, the pad length; `drill_length_mm`
    along Y, the pad width).
    - A land that was NOT read is only the model's guess of the pad: it
      decides nothing (review round 5, V1-3: a printed horizontal slot was
      turned by 90 degrees on the strength of an estimated land, and still
      called «read»). The slot stays as transcribed and the land is derived
      around it.
    - A READ land keeps the slot as transcribed whenever that fits; the slot
      is turned only when it does not fit as transcribed and fits turned:
      two readings that agree once the axes are exchanged. The caller names
      the pad and no longer calls the hole «read» (the program changed it).
    A round or square pad says nothing: the slot stays as transcribed.
    DECLARED HEURISTIC: the turn assumes the axes were exchanged in the
    transcription rather than the land or the slot misread; the pads are
    named in a note to verify. A slot angle declared in the schema would
    make it superfluous.

    @return (drill_mm, drill_length_mm) along the pad's length and width.
    """
    sizes = [_optional_number(pin.get(key), positive=True) for key in ("pad_length_mm", "pad_width_mm")]
    if not land_read or None in sizes:
        return drill, length
    fits_as_read = drill < sizes[0] and length < sizes[1]
    fits_turned = length < sizes[0] and drill < sizes[1]
    return (length, drill) if fits_turned and not fits_as_read else (drill, length)


def _is_zero(value) -> bool:
    """Whether a value is the NUMBER zero (0 or 0.0): how an answer says «no hole»; never True for a boolean."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0


def _same_lead(lead: dict, rounding_mm: float, other: dict) -> bool:
    """Whether a pad's terminal is the terminal `other` the dedicated reading transcribed for it.

    Same shape; a round lead compares its diameter, a flat one its two
    sides as an UNORDERED pair (which side a reading calls width is its own
    choice, as `core.tht_estimates.lead_passes` tries both). Two
    transcriptions agree by the rule of the printed holes
    (`_same_printed_hole`): within the coarser of their two roundings, never
    wider than the coarsest printed precision of any unit, or by the
    conversion noise alone.
    """
    theirs = other.get("lead") if isinstance(other, dict) else None
    if not isinstance(theirs, dict) or theirs.get("shape") != lead.get("shape"):
        return False
    first = sorted(value for key, value in lead.items() if key != "shape")
    second = sorted(value for key, value in theirs.items() if key != "shape")
    margin = min(max(float(rounding_mm or 0.0), float(other.get("rounding_mm") or 0.0)),
                 max(CHAIN_QUOTE_ROUNDING_MM.values()))
    return len(first) == len(second) and all(
        abs(a - b) <= _PRINTED_HOLE_TOLERANCE_MM or abs(a - b) < margin
        for a, b in zip(first, second))


def _lead_text_mm(lead: dict) -> str:
    """A terminal for the notes: «Ø0.60 mm» or «1.00 x 0.40 mm» for a blade."""
    if lead.get("shape") == "round":
        return f"Ø{lead['diameter_mm']:.2f} mm"
    return f"{lead['width_mm']:.2f} x {lead['thickness_mm']:.2f} mm"


def _confirmed_lead_word(lead: dict, word: str, rounding_mm: float, dedicated_leads, number: str,
                         found: dict) -> str:
    """The provenance of a pad's terminal once the second reading has been heard.

    A terminal read ONCE is not a measure (user, 2026-11-11: one answer
    declared the terminals of a relay wrong and «read», and the program
    derived holes and lands from them as from a measure). The dedicated pad
    reading transcribes the terminal sections too (`printed_terminals`):
    - both readings give the same terminal for this pad (`_same_lead`): the
      declared word stands;
    - the dedicated reading gives ANOTHER terminal, or two of its own that
      differ: "estimated", both values named (`found["lead_disagree"]`);
      the main value stays for the estimate, never an average;
    - only the main answer gives it -- the dedicated reading did not
      transcribe it, did not run, was made by the same model or was
      contradicted by the drawing (`dedicated_leads` None), as in every
      answer saved before the second transcription existed: "estimated",
      the pad named as read once (`found["lead_once"]`).
    A terminal the model itself declared "estimated" stays so, unnamed: it
    was never passed off as read. Structural, not tuned: the rule looks at
    who transcribed the terminal, never at its size or at the part.

    @param lead The pad's terminal, validated, mm.
    @param word The provenance declared for it ("read", "derived", "estimated").
    @param rounding_mm Rounding of the main transcription (`_lead_rounding_mm`).
    @param dedicated_leads `printed_terminals` of the dedicated reading, or
        None when there is no independent second reading.
    @param number The pad's number.
    @param found Outcome lists of `_resolve_pad_terminals`.
    @return The word the pad's `sources.lead` gets; the hole and the land
        derived from the terminal follow it (`_resolve_one_pad`).
    """
    if word == "estimated":
        return word
    other = _dedicated_hole(dedicated_leads, number)
    if isinstance(other, dict) and other.get("lead") is not None and _same_lead(lead, rounding_mm, other):
        return word
    if isinstance(other, dict) and (other.get("lead") is not None or other.get("conflict")):
        seen = (_lead_text_mm(other["lead"]) if other.get("lead") is not None
                else tx("due valori diversi"))
        found["lead_disagree"].append(tx("pad {pad}: risposta principale {main}, lettura dedicata "
                                         "{dedicated}", pad=number or "?", main=_lead_text_mm(lead),
                                         dedicated=seen))
    elif (number or "?") not in found["lead_once"]:
        found["lead_once"].append(number or "?")
    return "estimated"


def _resolve_one_pad(pin: dict, fallback: dict, dedicated, found: dict, part_lead=None,
                     lead_on_model_drill: bool = True, derived: Optional[dict] = None,
                     dedicated_leads=None) -> None:
    """Apply step 1 of `_resolve_pad_terminals` to one pin dict, in place; `found` collects the pads per outcome.

    `part_lead` is `_part_lead(data)`; `lead_on_model_drill` False keeps it
    off a pad that carries a model drill (mixed drills); `derived` receives
    {id(pin): (pin, the land the program replaced)} for every land the
    program wrote; `dedicated_leads` are the terminals the dedicated reading
    transcribed (`_confirmed_lead_word`), None without an independent one.
    """
    number = _pad_number_text(pin.get("number"))
    raw = pin.pop("value_sources", None)
    raw = raw if isinstance(raw, dict) else {}
    pin.pop("hole_callout", None)
    rounding = _optional_number(pin.pop("_hole_rounding_mm", None), positive=True) or 0.0
    lead_rounding = _optional_number(pin.pop("_lead_rounding_mm", None), positive=True) or 0.0
    raw_lead = pin.pop("lead", None)
    if isinstance(raw_lead, dict) and all(value is None for value in raw_lead.values()):
        # An empty lead, or one with every field null (how a strict-mode
        # answer leaves an optional object out): absent, not invalid (F6-7).
        raw_lead = None
    # Provenance is the program's verdict, never copied from the model.
    pin.pop("sources", None)
    # Same default as `package_from_mapping`: a pin without a type is SMD.
    pad_type = pin.get("pad_type", "smd")
    if pad_type not in _HOLED_PAD_TYPES:
        hole = [pin.pop(key, None) for key in ("drill_mm", "drill_length_mm")]
        if any(value is not None and not _is_zero(value) for value in hole):
            # A real hole on a pad without one is named; a zero is «no
            # hole» said with a number and goes without a word (F7-3).
            found["smd_drill"].append(f"{number or '?'} ({pad_type})")
        own = {key: word for key in ("pad_size", "shape") if (word := _pad_word(raw, key))}
        if pin.get("shape") is None:
            # A word about a shape the answer does not give states nothing (V1-7).
            own.pop("shape", None)
        if own:
            pin["sources"] = own
        return
    derived = derived if derived is not None else {}
    zero = [key for key in ("drill_mm", "drill_length_mm") if _is_zero(pin.get(key))]
    for key in zero:
        # A zero is «not printed» said with a number (review round 5, V1-6):
        # a zero slot side is no slot, a zero hole is a hole to derive from
        # the terminal -- or, without one, an unread hole. Never an error.
        del pin[key]
    if "drill_mm" in zero:
        found["zero_hole"].append(number or "?")
    lead = None
    if raw_lead is not None and pad_type == "thru_hole":
        try:
            lead = normalised_lead(raw_lead, number)
            if max(value for key, value in lead.items() if key != "shape") > _MAX_LEAD_MM:
                # Beyond what KiCad can represent: no terminal (V1-10).
                raise ValueError("lead")
        except (ValueError, OverflowError):
            lead = None
            found["lead_dropped"].append(number or "?")
    lead_word = (_pad_word(raw, "lead") or "estimated") if lead else None
    if lead is not None:
        # A terminal read once is not a measure: the word the model declares
        # stands only when the dedicated reading transcribed the same one.
        lead_word = _confirmed_lead_word(lead, lead_word, lead_rounding, dedicated_leads, number,
                                         found)
    # A land is printed when the pad says so, or, for a pad that says
    # nothing, when the answer says its pad size was read.
    size_word = _pad_word(raw, "pad_size") or fallback["pad_size"] or "estimated"
    # A word about a shape the answer does not give states nothing (review
    # round 5, V1-7): an absent shape is never «read».
    shapeless = pin.get("shape") is None
    declared_shape = None if shapeless else _pad_word(raw, "shape")
    shape_word = declared_shape or ("read" if size_word == "read" and not shapeless else "estimated")
    drill = _optional_number(pin.get("drill_mm"), positive=True)
    length = _optional_number(pin.get("drill_length_mm"), positive=True)
    drill_word = _pad_word(raw, "drill_mm") or fallback["drill_mm"]
    main_printed = drill is not None and drill_word == "read"
    other = _dedicated_hole(dedicated, number)
    other_printed = isinstance(other, dict) and other.get("drill_mm") is not None
    model_land = {key: pin.get(key) for key in ("pad_length_mm", "pad_width_mm", "shape")}
    sources = {}
    if main_printed and (other_printed or (isinstance(other, dict) and other.get("conflict"))):
        if other_printed and _same_printed_hole(drill, length, other, rounding):
            sources["drill_mm"] = "read"
        else:
            sources["drill_mm"] = "estimated"
            seen = (_hole_text_mm(other["drill_mm"], other.get("drill_length_mm"))
                    if other_printed else tx("due valori diversi"))
            found["disagree"].append(tx("pad {pad}: risposta principale {main}, lettura dedicata "
                                        "{dedicated}", pad=number, main=_hole_text_mm(drill, length),
                                        dedicated=seen))
    elif main_printed:
        sources["drill_mm"] = "read"
        if dedicated is not None:
            found["only_main"].append(number)
    else:
        printed_hole = None
        if other_printed:
            first, second = other["drill_mm"], other.get("drill_length_mm")
            if second is None or abs(second - first) <= _PRINTED_HOLE_TOLERANCE_MM:
                printed_hole = (first, None)
            else:
                # The dedicated reading gives a slot's width and length, not
                # its axes on the pad (review E2): oriented to the pad, or
                # left out when the pad cannot say.
                printed_hole = _slot_along_pad(pin, first, second)
                if printed_hole is None:
                    found["slot_unoriented"].append(f"{number} ({_hole_text_mm(first, second)})")
        if printed_hole is not None:
            drill, length = printed_hole
            pin["drill_mm"] = drill
            if length is None:
                pin.pop("drill_length_mm", None)
            else:
                pin["drill_length_mm"] = length
            sources["drill_mm"] = "read"
            found["only_dedicated"].append(number)
        else:
            if pin.get("drill_length_mm") is not None:
                # A slot is never an estimate (contract: only when printed).
                pin.pop("drill_length_mm", None)
                length = None
                found["slot_dropped"].append(number)
            terminal, terminal_word = lead, lead_word
            if (terminal is None and part_lead is not None and pad_type == "thru_hole"
                    and (drill is None or lead_on_model_drill)):
                # The part-level lead stands for a pad that declares none of
                # its own (review E1): the same rule, its own provenance.
                terminal, terminal_word = part_lead
                # One declaration for the whole part is one reading too: on
                # this pad it stands as declared only when the dedicated
                # reading transcribed the same terminal for it.
                terminal_word = _confirmed_lead_word(terminal, terminal_word, 0.0,
                                                     dedicated_leads, number, found)
                lead, lead_word = terminal, terminal_word
                found["part_lead"].append(number)
            if terminal is not None:
                estimate = estimated_round_pad(terminal)
                word = "derived" if terminal_word in ("read", "derived") else "estimated"
                pin["drill_mm"] = drill = estimate.drill_mm
                sources["drill_mm"] = word
                if size_word != "read":
                    _write_derived_land(pin, estimate.pad_diameter_mm, declared_shape)
                    size_word = word
                    shape_word = "read" if declared_shape == "read" else word
                    derived.setdefault(id(pin), (pin, model_land))
                if _round_hole_loose_on_blade(terminal, drill):
                    found["loose_blade"].append(number)
            elif drill is not None:
                sources["drill_mm"] = "estimated"
                found["lead_withheld" if part_lead is not None and pad_type == "thru_hole"
                      else "unfounded"].append(number)
            # Neither a hole nor a terminal: nothing is invented, the pad
            # stays without a hole (`_pads_without_holes`).
    if sources.get("drill_mm") == "read" and pad_type == "thru_hole" and drill is not None:
        slot = length is not None and abs(length - drill) > _PRINTED_HOLE_TOLERANCE_MM
        if slot:
            # A printed slot keeps its transcribed axes (review F6-1, V1-3);
            # only a READ land it fits no other way turns it.
            turned = _slot_on_pad_axes(pin, drill, length, size_word == "read")
            if turned != (drill, length):
                drill, length = turned
                pin["drill_mm"], pin["drill_length_mm"] = drill, length
                # The two sides were read, their axes are the program's: a
                # value the program changed is never «read».
                sources["drill_mm"] = "derived"
                found["slot_turned"].append(number)
        if size_word != "read":
            # A printed hole without a printed land: the land follows the
            # hole by the one rule, instead of the model's guess -- a round
            # pad for a round hole, the slot plus the ring for a slot; a
            # shape the pad says was read stays (review E3).
            if slot:
                _write_slot_land(pin, drill, length, declared_shape)
            else:
                _write_derived_land(pin, estimated_pad_diameter(drill), declared_shape)
            size_word = "derived"
            shape_word = "read" if declared_shape == "read" else "derived"
            derived.setdefault(id(pin), (pin, model_land))
    if pin.get("shape") is None:
        # The shape the footprint would assume is written, so that every
        # rule after this one (clearance, fit, the footprint) sees the same
        # pad; it is the program's choice, never «read» (V1-7).
        pin["shape"] = _DEFAULT_PAD_SHAPE
        shape_word = "estimated"
    sources["pad_size"] = size_word
    sources["shape"] = shape_word
    if lead is not None:
        pin["lead"] = lead
        sources["lead"] = lead_word
    pin["sources"] = sources


def _part_lead_word(data: dict, part_lead) -> Optional[str]:
    """The provenance of the part-level lead as the RESULT states it, or None without one.

    The program's own provenance for `lead_diameter_mm` (`_program_sources`:
    a user's choice, a measure) ranks over the model's declaration, exactly
    as `_value_sources` does; only "read" and "derived" may overrule a hole
    read from the drawing (`_judge_holes_against_leads`).
    """
    if part_lead is None:
        return None
    program = data.get("_program_sources") if isinstance(data.get("_program_sources"), dict) else {}
    word = program.get("lead_diameter_mm")
    return word if isinstance(word, str) and word else part_lead[1]


def _lift_pad_hole(pin: dict, hole: float, derived: dict) -> bool:
    """Replace a pad's hole by the ROUND hole estimated from its terminal and bring the land along.

    The hole becomes "estimated". The land follows by provenance:
    - a READ land is never changed (`_pad_unfit_reason` then judges whether
      it holds the new hole: a contradiction of two readings is a problem
      of the pad, not something the program repairs);
    - a land the program had derived from the old hole is derived again
      from the new one (`estimated_pad_diameter`);
    - the model's own land is widened on both axes by one amount when it no
      longer keeps the ring (`_widen_pad_around`, review G4: an oval stays
      an oval), and from then on it is a land the program wrote.

    @return True when the land was changed.
    """
    pin["drill_mm"] = hole
    pin.pop("drill_length_mm", None)
    _mark_pad_estimated(pin, "drill_mm")
    sources = pin.get("sources") or {}
    if sources.get("pad_size") == "read":
        return False
    if id(pin) in derived:
        _write_derived_land(pin, estimated_pad_diameter(hole), sources.get("shape"))
        _mark_pad_estimated(pin, "pad_size")
        return True
    before = {key: pin.get(key) for key in ("pad_length_mm", "pad_width_mm", "shape")}
    if _widen_pad_around(pin, hole):
        _mark_pad_estimated(pin, "pad_size")
        derived[id(pin)] = (pin, before)
        return True
    return False


def _judge_holes_against_leads(pins: list, part_lead, part_word, found: dict, derived: dict) -> None:
    """Step 2 of `_resolve_pad_terminals`: every through-hole pad's hole judged against the terminal it must take.

    Structural, not a threshold: a lead cannot pass through a hole of its
    own section or smaller (`core.tht_estimates.lead_passes`).

    A pad that declares its OWN terminal is judged against it (it supersedes
    the part-level lead for that pad). A hole the terminal does not pass:
    - read from the drawing: never changed by the program (provenance
      before value), but a hole its terminal cannot pass is not a measure
      either: its source becomes "estimated" and the note names the pad
      (review F7-7: the same rule the matrix drill has);
    - not read: replaced by the hole estimated from that terminal
      (`_lift_pad_hole`).

    The pads WITHOUT a terminal of their own are judged against the
    part-level lead, only when all of them have one hole (one lead for
    all); with different holes a note names the narrow ones and nothing
    changes (audit D-D). A hole not wider than the lead becomes the
    estimated hole of that lead -- unless the hole was read and the lead
    was not (`part_word` neither "read" nor "derived"): then the hole keeps
    its value, loses its "read" (F7-7) and is named.

    @param pins The pin dicts of the geometry, already through `_resolve_one_pad`.
    @param part_lead `_part_lead(data)`, or None.
    @param part_word `_part_lead_word(data, part_lead)`.
    @param found Outcome lists of `_resolve_pad_terminals`.
    @param derived The lands the program wrote, updated when a land follows a lifted hole.
    """
    thru = [pin for pin in pins if pin.get("pad_type") == "thru_hole"]
    for pin in thru:
        lead = pin.get("lead")
        drill = _optional_number(pin.get("drill_mm"), positive=True)
        if not isinstance(lead, dict) or drill is None:
            continue
        length = _optional_number(pin.get("drill_length_mm"), positive=True)
        try:
            if lead_passes(lead, drill, length):
                continue
        except ValueError:
            continue
        number = _pad_number_text(pin.get("number")) or "?"
        if (pin.get("sources") or {}).get("drill_mm") == "read":
            found["lead_contradicted"].append(number)
            _mark_pad_estimated(pin, "drill_mm")
            continue
        hole = estimated_round_pad(lead).drill_mm
        found["lead_lifted"].append(number)
        if _lift_pad_hole(pin, hole, derived):
            found["widened"].append(number)
        if _round_hole_loose_on_blade(lead, hole):
            found["loose_blade"].append(number)
    if part_lead is None:
        return
    lead = part_lead[0]["diameter_mm"]
    rest = [(pin, drill) for pin in thru if not isinstance(pin.get("lead"), dict)
            and (drill := _optional_number(pin.get("drill_mm"), positive=True)) is not None]
    if len({round(drill, 6) for _pin, drill in rest}) > 1:
        # Pads with different holes have different leads: the one lead
        # diameter of the answer may belong to some of them only.
        found["part_narrow"].extend(sorted(_pad_number_text(pin.get("number")) or "?"
                                           for pin, drill in rest if drill <= lead))
        return
    lifted = estimated_hole(part_lead[0])
    for pin, drill in rest:
        if drill > lead:
            continue
        number = _pad_number_text(pin.get("number")) or "?"
        if (pin.get("sources") or {}).get("drill_mm") == "read" and part_word not in ("read", "derived"):
            found["part_protected"].append(number)
            _mark_pad_estimated(pin, "drill_mm")
            continue
        found["part_lifted"].append(number)
        if _lift_pad_hole(pin, lifted, derived):
            found["part_widened"].append(number)


def _hole_sides(pin: dict):
    """A pad's hole as (side along the pad length, side along the pad width), mm, or None without a hole."""
    drill = _optional_number(pin.get("drill_mm"), positive=True)
    if drill is None:
        return None
    length = _optional_number(pin.get("drill_length_mm"), positive=True)
    return drill, drill if length is None else length


def _land_text_mm(land: dict) -> str:
    """A land for the notes: «1.60 mm» when its two sides are equal, else «2.40 x 1.60 mm»."""
    sizes = [_optional_number(land.get(key), positive=True) for key in ("pad_length_mm", "pad_width_mm")]
    if None in sizes:
        return "?"
    if abs(sizes[0] - sizes[1]) <= _PRINTED_HOLE_TOLERANCE_MM:
        return f"{sizes[0]:.2f} mm"
    return f"{sizes[0]:.2f} x {sizes[1]:.2f} mm"


def _land_holds_hole(land: dict, sides) -> bool:
    """Whether a land (sizes) keeps MIN_ANNULAR_RING_MM around a hole given by its two `sides` (`_hole_sides`)."""
    sizes = [_optional_number(land.get(key), positive=True) for key in ("pad_length_mm", "pad_width_mm")]
    return (None not in sizes and sides is not None
            and all(size >= side + 2 * MIN_ANNULAR_RING_MM - 1e-9 for size, side in zip(sizes, sides)))


def _pad_unfit_reason(pin: dict, found: dict) -> Optional[str]:
    """Step 4 of `_resolve_pad_terminals`: why a through-hole pad's land cannot hold its hole, or None when it does.

    «Holds» is the footprint's own rule (`PinSpec.validate`): a round hole
    smaller than both pad sides, a slot smaller than the pad on each axis.
    A pad that fails it would make `package_from_mapping` refuse the whole
    geometry; here it is named with both values instead, and the caller
    returns the pads as unread geometry. A pad without a hole or without
    its sizes is not judged (other rules name it). A READ land that holds a
    hole the program derived or estimated with less than MIN_ANNULAR_RING_MM
    of ring is kept (provenance before value) and named in `found`.

    @return The reason with both values and WITHOUT the pad's number: pads
        that share it are named together by `_grouped_pad_reasons` (V1-9).
    """
    if pin.get("pad_type") != "thru_hole":
        return None
    sides = _hole_sides(pin)
    sizes = [_optional_number(pin.get(key), positive=True) for key in ("pad_length_mm", "pad_width_mm")]
    if sides is None or None in sizes:
        return None
    number = _pad_number_text(pin.get("number")) or "?"
    sources = pin.get("sources") or {}
    round_hole = abs(sides[0] - sides[1]) <= _PRINTED_HOLE_TOLERANCE_MM
    holds = sides[0] < min(sizes) if round_hole else (sides[0] < sizes[0] and sides[1] < sizes[1])
    if holds:
        if (sources.get("pad_size") == "read" and sources.get("drill_mm") != "read"
                and not _land_holds_hole(pin, sides)):
            found["thin_ring"].append(number)
        return None
    hole = _hole_text_mm(sides[0], None if round_hole else sides[1])
    if sources.get("pad_size") == "read":
        # Two readings that cannot both be true: which one is wrong is on
        # the drawing, not in the program.
        return tx("la piazzola letta dal disegno ({land}) non contiene il foro {hole} "
                  "(provenienza del foro: {source})", land=_land_text_mm(pin), hole=hole,
                  source=sources.get("drill_mm") or "estimated")
    return tx("la piazzola {land} non contiene il foro {hole}", land=_land_text_mm(pin), hole=hole)


def _keep_derived_lands_apart(pins: list, derived: dict, found: dict, unfit: dict) -> None:
    """Step 3 of `_resolve_pad_terminals`: no land the program wrote may overlap or crowd a neighbour (review G1).

    A derived land (hole + HOLE_TO_PAD_ANNULUS_MM) ignores the pitch: Ø0.65
    at 1.27 mm gives 1.45 mm pads that overlap. Each land the program wrote
    must keep MIN_PAD_GAP_MM of free copper from every other pad (the pin
    header's minimum: KiCad's default clearance, above the usual fabrication
    minimum; measured on the copper as KiCad draws it,
    `package_geometry.copper_separation`). A land that does not:
    1. takes the model's own land back when that keeps MIN_ANNULAR_RING_MM
       around the hole and clears every neighbour (neighbours in the same
       case tried with their model land too);
    2. else is reduced, both sides by one amount, never below the hole plus
       MIN_ANNULAR_RING_MM per side (review F7-6: a 0.025 mm ring used to
       pass). The room is shared by the RING: every land reduced here is
       tried with the same ring as its reduced neighbours, so two pads with
       different holes end with the same copper around each hole, whichever
       is judged first (symmetric by construction);
    3. else there is no room at this pitch for the hole with its minimum
       ring: the pad is a problem (`unfit`), never a thinner ring and never
       the model's land put back although it cannot hold the hole (F7-4).
    Every land changed here is marked "estimated" and named in the notes; a
    reduction below KiCad's 1 nm is not a change and is not named. Pads
    without both coordinates or sizes are not measured.

    @param pins The pin dicts of the geometry.
    @param derived {id(pin): (pin, the land the program replaced)}.
    @param found Outcome lists of `_resolve_pad_terminals`.
    @param unfit Receives {id(pin): reason} for the pads of case 3; the
        reason does not name the pad (`_grouped_pad_reasons` does).
    @note A land without a `shape` is measured with the footprint's own
        default (`_DEFAULT_PAD_SHAPE`), the shape it will be written with.
    """
    from types import SimpleNamespace

    from ..core.package_geometry import copper_separation, pad_copper_shape

    gap = MIN_PAD_GAP_MM
    floor = 2 * MIN_ANNULAR_RING_MM

    def land_of(pin):
        return {key: pin.get(key) for key in ("pad_length_mm", "pad_width_mm", "shape")}

    def copper(pin, land):
        """The copper (core, radius) of `pin` with `land`, or None when a value is missing."""
        values = [_optional_number(pin.get("x_mm")), _optional_number(pin.get("y_mm")),
                  _optional_number(land.get("pad_length_mm"), positive=True),
                  _optional_number(land.get("pad_width_mm"), positive=True)]
        if None in values:
            return None
        rotation = _optional_number(pin.get("rotation_deg")) or 0.0
        return pad_copper_shape(SimpleNamespace(
            x_mm=values[0], y_mm=values[1], pad_length_mm=values[2], pad_width_mm=values[3],
            # An absent shape is the footprint's own default, never another
            # one (V1-7: a circle assumed here, a roundrect written).
            shape=str(land.get("shape") or _DEFAULT_PAD_SHAPE), rotation_deg=rotation))

    placed = [pin for pin in pins if copper(pin, land_of(pin)) is not None]
    lands = {id(pin): land_of(pin) for pin in placed}

    def clears(pin, land, now) -> bool:
        own = copper(pin, land)
        number = _pad_number_text(pin.get("number"))
        for other in placed:
            # Pads sharing a number are one pin: their copper may touch.
            if other is pin or (number and _pad_number_text(other.get("number")) == number):
                continue
            shape = copper(other, now[id(other)])
            if shape is not None and copper_separation(own, shape) < gap - 1e-9:
                return False
        return True

    entries = [(pin, model) for pin, model in derived.values()
               if id(pin) in lands and _hole_sides(pin) is not None]
    failing = [entry for entry in entries if not clears(entry[0], lands[id(entry[0])], lands)]
    if not failing:
        return
    trial = dict(lands)
    for pin, model in failing:
        if _land_holds_hole(model, _hole_sides(pin)) and copper(pin, model) is not None:
            trial[id(pin)] = model
    # A model land that crowds a neighbour goes back to the derived land; the
    # lands only grow, so this settles.
    changed = True
    while changed:
        changed = False
        for pin, model in failing:
            if trial[id(pin)] is model and not clears(pin, model, trial):
                trial[id(pin)] = lands[id(pin)]
                changed = True
    capping = [pin for pin, model in failing if trial[id(pin)] is not model]
    for pin, model in failing:
        if trial[id(pin)] is model:
            pin.update({key: value for key, value in model.items() if value is not None})
            # The model's land comes back with the shape it was tried with:
            # its own, or the footprint's default when it gave none (V1-7).
            pin["shape"] = model.get("shape") or _DEFAULT_PAD_SHAPE
            _mark_pad_estimated(pin, "pad_size", "shape")
            found["land_model"].append(_pad_number_text(pin.get("number")) or "?")
    # The ring each land may give up: its smaller excess over the hole,
    # 0.8 mm for a land derived by the one rule.
    slack = {}
    for pin in capping:
        sides = _hole_sides(pin)
        land = lands[id(pin)]
        slack[id(pin)] = min(float(land["pad_length_mm"]) - sides[0], float(land["pad_width_mm"]) - sides[1])

    def at(pin, margin):
        """The land of `pin` reduced on both sides until its smaller excess over the hole is `margin`."""
        cut = slack[id(pin)] - min(slack[id(pin)], margin)
        land = lands[id(pin)]
        return {**land, "pad_length_mm": float(land["pad_length_mm"]) - cut,
                "pad_width_mm": float(land["pad_width_mm"]) - cut}

    def fits(pin, margin) -> bool:
        now = dict(trial)
        for other in capping:
            if other is not pin:
                now[id(other)] = at(other, margin)
        return clears(pin, at(pin, margin), now)

    for pin in capping:
        number = _pad_number_text(pin.get("number")) or "?"
        top = slack[id(pin)]
        if top < floor - 1e-9 or not fits(pin, floor):
            sides = _hole_sides(pin)
            hole = _hole_text_mm(sides[0], None if abs(sides[0] - sides[1]) <= _PRINTED_HOLE_TOLERANCE_MM
                                 else sides[1])
            unfit[id(pin)] = tx(
                "a questo passo non c'e' posto per il foro {hole} con {ring:.2f} mm di "
                "corona e {gap:.2f} mm di rame libero dal pad vicino", hole=hole,
                ring=MIN_ANNULAR_RING_MM, gap=gap)
            continue
        low, high = floor, top
        for _step in range(40):
            middle = (low + high) / 2
            if fits(pin, middle):
                low = middle
            else:
                high = middle
        # Down to KiCad's 1 nm so the clearance is kept, never rounded away;
        # never below the ring that `fits` has just accepted.
        margin = max(floor, math.floor(low * 1e6) / 1e6)
        if top - margin < 1e-6:
            # Not a reduction KiCad could write: the land is as derived.
            continue
        land = at(pin, margin)
        pin["pad_length_mm"] = round(land["pad_length_mm"], 6)
        pin["pad_width_mm"] = round(land["pad_width_mm"], 6)
        _mark_pad_estimated(pin, "pad_size")
        found["land_capped"].append(f"{number} ({_land_text_mm(pin)})")


def _widen_pad_around(pin: dict, drill: float) -> bool:
    """Grow a pad on both axes by ONE amount until its smaller side keeps the ring around `drill`.

    The pad keeps its orientation and its elongation (an oval stays oval, its
    long axis where it was): only when the smaller side leaves less than
    MIN_ANNULAR_RING_MM per side, both sides grow by the amount that brings
    the smaller one to the estimated pad of that hole
    (`estimated_pad_diameter`, the one rule of `core.tht_estimates`, review
    G4). Returns True when the pad was widened.
    """
    sizes = [_optional_number(pin.get(key), positive=True) for key in ("pad_length_mm", "pad_width_mm")]
    if None in sizes:
        return False
    smaller = min(sizes)
    if smaller >= drill + 2 * MIN_ANNULAR_RING_MM:
        return False
    grow = estimated_pad_diameter(drill) - smaller
    pin["pad_length_mm"] = round(sizes[0] + grow, 3)
    pin["pad_width_mm"] = round(sizes[1] + grow, 3)
    return True


def _mark_pad_estimated(pin: dict, *keys) -> None:
    """Mark `keys` of a pin's `sources` "estimated" (a pin without `sources` gains them)."""
    sources = dict(pin.get("sources") or {})
    for key in keys:
        sources[key] = "estimated"
    pin["sources"] = sources


def _lift_drill_above_the_lead(params: "ExtractedParams", notes: list) -> None:
    """A MATRIX hole not wider than the lead is not a measurement: it becomes an estimate that fits.

    Structural, not a threshold: a lead cannot pass through a hole of its
    own diameter or smaller, whatever the part. When the drawing quotes the
    lead (`lead_diameter_mm`) and the matrix drill is not larger (TEL8 run of
    2026-10-10, 11:38: lead Ø0.5 and drill 0.5 «estimated»), the drill
    becomes the hole the one rule estimates for that lead
    (`core.tht_estimates.estimated_hole`: lead + LEAD_TO_HOLE_CLEARANCE_MM
    rounded up to its step, review G4), marked «estimated» in
    `value_sources` -- a «read» hole smaller than a READ lead was read wrong,
    and the note says so. A pad that no longer clears the lifted drill is
    lifted too (`estimated_pad_diameter`) and marked «estimated».
    Nothing happens without a lead diameter.

    Provenance before value (2026-11-10): a lead that is itself an estimate
    (or undeclared) never overrules a hole read from the drawing; the
    conflict is only named.

    @param params Result under construction; the matrix drill, pad diameter
        and their sources are updated.
    @param notes Notes of the result.
    @note Only the matrix fields: the explicit pads are judged pad by pad
        BEFORE they are validated (`_judge_holes_against_leads`, step 2 of
        `_resolve_pad_terminals`), and nothing changes a pad after its
        validation (review F7-2: holes lifted and lands widened here used to
        leave overlapping copper in the result).
    """
    lead, drill = params.lead_diameter_mm, params.drill_mm
    if not (isinstance(lead, (int, float)) and isinstance(drill, (int, float))):
        return
    if drill > lead:
        return
    sources = dict(params.value_sources or {})
    if sources.get("drill_mm") == "read" and sources.get("lead_diameter_mm") not in ("read", "derived"):
        # The two values cannot both be true and nothing tells which one is
        # wrong: the unread lead does not move the hole, but a hole the lead
        # cannot pass is no longer reported as a measure (review 2026-11-11):
        # it joins the values to verify instead of passing as read.
        notes.append(tx("ATTENZIONE foro letto {drill:.2f} mm non maggiore del terminale {lead:.2f} "
                        "mm, che non e' letto dal disegno: foro non modificato, da verificare.",
                        drill=drill, lead=lead))
        sources["drill_mm"] = "estimated"
        params.value_sources = sources
        return
    lifted = estimated_hole({"shape": "round", "diameter_mm": lead})
    notes.append(tx(
        "ATTENZIONE foro {drill:.2f} mm non maggiore del terminale {lead:.2f} mm "
        "(provenienza dichiarata: {source}): un terminale non passa in un foro del suo "
        "diametro. Foro portato a {lifted:.2f} mm (terminale + {clearance:.2f} mm), valore "
        "STIMATO da verificare.", drill=drill, lead=lead,
        source=sources.get("drill_mm", UNDECLARED_SOURCE), lifted=lifted,
        clearance=LEAD_TO_HOLE_CLEARANCE_MM))
    params.drill_mm = lifted
    sources["drill_mm"] = "estimated"
    pad = params.pad_diameter_mm
    if isinstance(pad, (int, float)) and pad < lifted + 2 * MIN_ANNULAR_RING_MM:
        params.pad_diameter_mm = estimated_pad_diameter(lifted)
        sources["pad_diameter_mm"] = "estimated"
        notes.append(tx("Piazzola {pad:.2f} mm non piu' larga del foro corretto: portata a "
                        "{new:.2f} mm, valore STIMATO da verificare.",
                        pad=pad, new=params.pad_diameter_mm))
    params.value_sources = sources


def _drop_unread_row_spacing(params: "ExtractedParams", notes: list) -> None:
    """Keep a castellated row spacing only when the model READ it in a document.

    The row spacing is the one number that moves the through-holes of a
    castellated module across the board: the generator places them at
    (extent - spacing) / 2 from the edge. A value the model derived or
    estimated is not that measurement -- on the RS485 run of 2026-09-30 it
    reported the pitch of one side as `row_spacing_mm` (its own words:
    «derivato, coincidente col passo, non e' una distanza fra file», derived,
    equal to the pitch, not a distance between rows) and the holes
    landed 18.86 mm in, with copper lands across half the board (HANDOFF
    point 167). Provenance is a declared attribute of the answer, not a
    threshold: only "read" keeps the value; derived, estimated and
    undeclared drop it, with a note that says the value, its provenance and
    where it would have put the holes. The generator then falls back to the
    minimum inset behind the half-hole (or to an offset typed by hand).

    @param params Result under construction; `row_spacing_mm` and its
        provenance are removed when not read.
    @param notes Notes of the result.
    """
    spacing = params.row_spacing_mm
    if spacing is None:
        return
    sources = dict(params.value_sources or {})
    source = sources.get("row_spacing_mm", UNDECLARED_SOURCE)
    if source == "read":
        return
    from ..generators.castellated_module import opposite_row_edges, through_hole_inset_from_rows
    edges = [side["edge"] for side in (params.castellated_sides or ()) if isinstance(side, dict)]
    pair = opposite_row_edges(edges)
    extent = None
    if pair == ("left", "right"):
        extent = params.body_length_mm
    elif pair == ("top", "bottom"):
        extent = params.body_width_mm
    inset = through_hole_inset_from_rows(extent, spacing) if extent else None
    where = (tx(" (avrebbe messo i fori passanti a {inset:.2f} mm dal bordo)", inset=inset)
             if inset is not None else "")
    notes.append(tx(
        "Distanza fra le file {spacing} mm riportata dal modello come «{source}», non letta in "
        "un documento: non usata per posizionare i fori passanti dei contatti castellated{where}. "
        "Restano la stima minima dietro al semi-foro o la distanza dal bordo indicata a mano.",
        spacing=spacing, source=_SOURCE_LABELS.get(source, source), where=where))
    params.row_spacing_mm = None
    sources.pop("row_spacing_mm", None)
    params.value_sources = sources


def _apply_measured_lands(data: dict, params: "ExtractedParams", notes: list) -> None:
    """Adopt the land widths the photo measured (`data["_measured_lands"]`, edge_lands).

    Per edge with a measured width: a `pad_width_mm` the model READ in a
    document is kept (a document measure outranks a photo measure) and the
    two are compared in a note; any other provenance -- derived, estimated,
    undeclared, or no value at all -- is replaced by the measured width with
    provenance MEASURED_SOURCE, which the GUI treats as declared. The inward
    extent is recorded on the side as `land_inward_mm` (informative: the
    footprint's pad length includes the overhang beyond the edge, which no
    photo of the module shows). The measurement block is a private key the
    photo stage writes; a raw answer never carries it (_canonicalize_answer
    drops it).

    @param data Answer the photo stage annotated.
    @param params Result under construction; sides and value_sources updated.
    @param notes Notes of the result.
    """
    lands = data.get("_measured_lands")
    if not isinstance(lands, dict) or not lands:
        return
    sources = dict(params.value_sources or {})
    for side in params.castellated_sides or ():
        if not isinstance(side, dict):
            continue
        edge = side.get("edge")
        land = lands.get(edge)
        if not isinstance(land, dict):
            continue
        width = _optional_number(land.get("width_mm"), positive=True)
        if width is None:
            continue
        label = EDGE_LABELS.get(edge, edge)
        tolerance = _optional_number(land.get("tolerance_mm")) or 0.0
        inward = _optional_number(land.get("inward_mm"), positive=True)
        if inward is not None:
            side["land_inward_mm"] = round(float(inward), 3)
        key = f"castellated_sides.{edge}.pad_width_mm"
        declared = _optional_number(side.get("pad_width_mm"), positive=True)
        if sources.get(key) == "read" and declared is not None:
            agreement = (tx("coerente") if abs(declared - width) <= tolerance + 1e-9
                         else tx("DIVERSA: verificare il disegno"))
            notes.append(tx(
                "Lato {label}: larghezza delle piazzole letta nei documenti {declared} mm, "
                "misurata sulla foto {width:.2f} mm (±{tolerance:.2f}): si tiene la lettura, "
                "{agreement}.", label=label, declared=declared, width=width,
                tolerance=tolerance, agreement=agreement))
            continue
        previous = (tx("stima del modello {value} mm", value=declared) if declared is not None
                    else tx("nessun valore del modello"))
        side["pad_width_mm"] = round(float(width), 3)
        sources[key] = MEASURED_SOURCE
        notes.append(tx(
            "Lato {label}: larghezza delle piazzole misurata sulla foto: {width:.2f} mm "
            "(±{tolerance:.2f}), al posto di: {previous}.",
            label=label, width=width, tolerance=tolerance, previous=previous))
    params.value_sources = sources


def _apply_measured_holes(data: dict, params: "ExtractedParams", notes: list) -> None:
    """Adopt the contact style the photo read (`data["_measured_holes"]`, edge_lands).

    Per edge with a verdict: when the style the photo read differs from the
    declared one, the side takes the photo's style, with a note that says
    the evidence (how many lands agreed, the opening and the hole widths);
    an equal style is confirmed silently. Only the STYLE is adopted: the hole
    width read on the photo is the visible barrel, not the drill, and is
    reported in the note for a person, never written as a size. The
    measurement block is a private key the photo stage writes; a raw answer
    never carries it (_canonicalize_answer drops it).

    @param data Answer the photo stage annotated.
    @param params Result under construction; sides updated.
    @param notes Notes of the result.
    """
    holes = data.get("_measured_holes")
    if not isinstance(holes, dict) or not holes:
        return
    for side in params.castellated_sides or ():
        if not isinstance(side, dict):
            continue
        edge = side.get("edge")
        reading = holes.get(edge)
        if not isinstance(reading, dict) or reading.get("style") not in CONTACT_STYLES:
            continue
        declared = side.get("contact_style")
        if declared == reading["style"]:
            continue
        label = EDGE_LABELS.get(edge, edge)
        side["contact_style"] = reading["style"]
        side["_contact_style_evidence"] = tx(
            "letto sulla foto: {agreeing} piazzole su {contacts} concordi, foro {drill:.2f} mm, "
            "apertura sul bordo {chord:.2f} mm",
            agreeing=reading.get("agreeing", 0), contacts=reading.get("contacts", 0),
            drill=float(reading.get("drill_mm") or 0.0), chord=float(reading.get("chord_mm") or 0.0))
        notes.append(tx(
            "Lato {label}: tipo di contatto {measured} letto sulla foto ({evidence}), al posto di "
            "«{declared}» dichiarato dal modello.",
            label=label, measured=reading["style"], evidence=side["_contact_style_evidence"],
            declared=declared or "?"))


#: Per-side sizes measured ALONG the edge, across the pitch: two adjacent
#: contacts cannot share copper, so the pad width (the along-edge size on
#: every edge, see castellated_module.pad_positions) cannot reach the pitch.
#: The hole diameters have the same physical bound and are already DROPPED
#: by _add_hole_sizes when they reach it; the pad width is kept as an
#: estimate instead, because the generator knows how to shrink a land.
_ACROSS_PITCH_KEYS = ("pad_width_mm",)


def _downgrade_sizes_wider_than_the_pitch(params: "ExtractedParams", notes: list) -> None:
    """A side size that reaches the pitch is not a measurement: keep it, as an estimate.

    Structural, not a threshold: on a side with more than one contact, a pad
    width equal to or wider than the pitch means two adjacent contacts
    sharing copper -- no drawing shows that (the hole diameters, with the
    same bound, are dropped earlier by _add_hole_sizes). When
    the model still declares such a value «read» or «derived» (the RS485 run
    of 2026-09-30: «larghezza piazzola 2.54 mm derivata dal foro stimato»,
    on a 2.54 mm pitch, HANDOFF point 168), the value stays for the
    generator to reduce (an AssumedSize shrinks to the largest that fits,
    and says so), but its provenance becomes «estimated»: a measure is never
    touched, an impossible «measure» is not one. A value that fits the pitch
    keeps whatever provenance it has, including one the generator will
    refuse for the fabrication gap: that is a real disagreement to show.

    @param params Result under construction; `value_sources` is updated.
    @param notes Notes of the result.
    """
    sources = dict(params.value_sources or {})
    changed = False
    for side in params.castellated_sides or ():
        if not isinstance(side, dict):
            continue
        edge, count, pitch = side.get("edge"), side.get("count"), side.get("pitch_mm")
        if not (isinstance(count, int) and count > 1) or not isinstance(pitch, (int, float)):
            continue
        for key in _ACROSS_PITCH_KEYS:
            value = side.get(key)
            source_key = f"castellated_sides.{edge}.{key}"
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and value >= float(pitch) - 1e-9
                    and sources.get(source_key) in ("read", "derived")):
                notes.append(tx(
                    "Lato {label}: {field} = {value} mm dichiarato «{source}» ma pari o superiore "
                    "al passo di {pitch} mm dello stesso lato: due contatti adiacenti si "
                    "sovrapporrebbero, quindi non e' una misura. Trattato come stima, che il "
                    "generatore riduce e annota.",
                    label=EDGE_LABELS.get(edge, edge), field=key, value=value,
                    source=_SOURCE_LABELS.get(sources[source_key], sources[source_key]),
                    pitch=pitch))
                sources[source_key] = "estimated"
                changed = True
    if changed:
        params.value_sources = sources


#: Italian words for the provenances, for the notes.
_SOURCE_LABELS = {"read": "letta", "derived": "derivata", "estimated": "stimata",
                  UNDECLARED_SOURCE: "senza provenienza", USER_SOURCE: "scelta dall'utente"}
