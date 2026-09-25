"""@brief Parametric generator for modules with castellated (half-hole) edge contacts.

Generatore parametrico: modulo con piazzole a mezzo foro (castellated).

I moduli su PCB (RS485/TTL isolati, WiFi/BLE tipo ESP32, nRF...) portano i
contatti come semi-fori metallizzati sul bordo della schedina e si saldano
sulla scheda madre come componenti SMD: sotto ogni semi-foro va una
piazzola rettangolare.

Perche' non basta il generatore a matrice (`pin_header`): li' tutti i pad
condividono un solo `pitch` e stanno su una griglia righe x colonne. Nei
moduli reali ogni lato fa storia a se' -- per esempio un convertitore
RS485/TTL con 3 contatti a passo 5.08mm sul lato sinistro e 5 contatti a
passo 2.54mm sul lato destro non e' rappresentabile da nessuna matrice.
Qui invece ogni lato ha il suo numero di pad, il suo passo e la sua
numerazione.

Non tutti i moduli hanno pero' i semi-fori: ogni lato dichiara il proprio
tipo di contatto (`CastellatedSide.contact_style`) -- solo semi-foro, solo
foro passante arretrato dal bordo (schede di sviluppo con strip a passo 2.54),
oppure entrambi. Il tipo si vede nella fotografia e viene chiesto al modello;
il default e' il comportamento storico (entrambi).

Convenzioni geometriche:

* l'origine del footprint e' il **centro del corpo** del modulo (standard
  per i componenti SMD in KiCad), non il pin 1 come nel pin header;
* l'asse X e' la lunghezza del corpo, l'asse Y la larghezza, con Y che
  cresce verso il basso come in KiCad: il lato "top" e' quindi a Y
  negativo;
* ogni pad e' lungo `pad_length` in direzione perpendicolare al bordo e
  largo `pad_width` lungo il bordo, e sporge di `pad_overhang` oltre il
  bordo del modulo, cosi' che il menisco di saldatura resti ispezionabile
  a modulo montato;
* un contatto half_and_through con foro passante dichiarato non ha due
  piazzole ma UNA sola, continua, a stadio (`continuous_pad`): dallo stesso
  sbalzo esterno fino oltre la corona del foro, con il foro decentrato al suo
  interno. E' quello che mostra la foto di una scheda reale (decisione
  dell'utente del 2026-09-23) ed e' la stessa forma che il 3D disegna.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, NamedTuple, Optional, Sequence

from ..core.footprint import Footprint, FpCircle, FpRect, FpText, Pad
from ..core.silk import keepout_box, pin_one, pin_one_marker, silk_rectangle
from ..core.sexpr import SExpr, sx
from ..services.messages import tx

#: Parete di rame minima fra il semi-foro sul bordo e il foro passante. Due
#: cilindri passanti sottratti allo stesso solido restano distinti solo se la
#: distanza fra gli assi supera la somma dei raggi: sotto quella soglia non si
#: ottengono due feature ma un unico buco di serratura aperto sul bordo, che e'
#: esattamente il difetto trovato misurando il solido generato (44 pareti da
#: 164,7 gradi invece di 44 fori chiusi).
MIN_HOLE_WALL_MM = 0.2

#: Lati del modulo su cui possono stare i contatti.
EDGES = ("left", "right", "top", "bottom")

#: Tipo di contatto di un lato: solo il semi-foro tagliato dal bordo (modulo
#: castellated puro, si salda per rifusione sulle piazzole di bordo).
CONTACT_HALF_HOLE = "half_hole"
#: Solo un foro passante metallizzato INTERO, con la sua corona, arretrato dal
#: bordo: fra corona e bordo resta materiale della scheda e il bordo e' dritto.
#: E' il caso delle schede di sviluppo con gli strip a passo 2.54.
CONTACT_THROUGH_HOLE = "through_hole"
#: Entrambe le feature sullo stesso contatto: il semi-foro sul bordo e un foro
#: passante poco piu' dentro, uniti da UNA piazzola di rame continua a stadio
#: (come nelle schede reali: il rame parte dal bordo, dove si apre il
#: semi-foro, e prosegue senza interruzioni fino oltre la corona del foro).
#: E' il comportamento predefinito del generatore.
CONTACT_HALF_AND_THROUGH = "half_and_through"
#: Valori ammessi per `CastellatedSide.contact_style`. Quale sia quello giusto
#: si vede nella fotografia (il bordo taglia il foro oppure no) e va chiesto al
#: modello, non dedotto da una soglia dimensionale: vedi datasheet_extractor.
CONTACT_STYLES = (CONTACT_HALF_HOLE, CONTACT_THROUGH_HOLE, CONTACT_HALF_AND_THROUGH)

#: Frase aggiunta alla descrizione di un footprint dichiarato SMD che porta
#: pad forati. KiCad considera incoerente il tipo (controllo DRC
#: `footprint_type_mismatch`, spento di serie in KiCad 10) e di norma esporta
#: nel file di posizionamento solo i footprint SMD. Il tipo resta SMD per
#: scelta dell'utente (2026-09-24): il modulo si posa e si salda a rifusione,
#: quindi deve restare in quel file; la frase lo dice a chi apre la libreria.
SMD_WITH_DRILLED_PADS_NOTE = (
    "Tipo SMD di proposito anche con piazzole forate: il modulo si posa e si "
    "salda a rifusione, i fori non ospitano pin."
)

#: Nomi leggibili dei tipi di contatto, per i messaggi d'errore e i log.
CONTACT_STYLE_NAMES = {
    CONTACT_HALF_HOLE: "mezzo foro sul bordo",
    CONTACT_THROUGH_HOLE: "foro passante",
    CONTACT_HALF_AND_THROUGH: "mezzo foro + foro passante",
}


def style_has_half_hole(style: str) -> bool:
    """@brief Tell whether a contact style cuts a plated half-hole into the edge.

    @param style One of CONTACT_STYLES.
    @return True for CONTACT_HALF_HOLE and CONTACT_HALF_AND_THROUGH.
    """
    return style in (CONTACT_HALF_HOLE, CONTACT_HALF_AND_THROUGH)


def style_has_through_hole(style: str) -> bool:
    """@brief Tell whether a contact style carries a closed plated through-hole.

    @param style One of CONTACT_STYLES.
    @return True for CONTACT_THROUGH_HOLE and CONTACT_HALF_AND_THROUGH.
    """
    return style in (CONTACT_THROUGH_HOLE, CONTACT_HALF_AND_THROUGH)


#: Le due coppie di lati opposti: la distanza fra le file si misura attraverso
#: il corpo, quindi lungo X per sinistra/destra e lungo Y per sopra/sotto.
OPPOSITE_EDGE_PAIRS = (("left", "right"), ("top", "bottom"))


def opposite_row_edges(edges) -> Optional[tuple]:
    """@brief The single pair of opposite edges a declared row spacing refers to.

    @param edges Edges that carry contacts (any iterable of EDGES values).
    @return ("left", "right") or ("top", "bottom") when exactly one opposite
        pair carries contacts on both of its edges, otherwise None.
    @note One declared row spacing is one number: it can describe the distance
        between two facing rows, not between rows on both axes at once. With
        contacts on all four edges it cannot be attributed, and with a single
        row there is nothing for it to measure; in both cases it is not used,
        rather than being applied to an axis it may not belong to.
    """
    present = set(edges)
    pairs = [pair for pair in OPPOSITE_EDGE_PAIRS if set(pair) <= present]
    return pairs[0] if len(pairs) == 1 else None


def through_hole_inset_from_rows(extent_mm, row_spacing_mm) -> Optional[float]:
    """@brief Distance from the board edge of the through-hole centres, in mm.

    @param extent_mm Board extent across the two rows (body length for
        left/right rows, body width for top/bottom rows).
    @param row_spacing_mm Declared centre-to-centre distance between the rows.
    @return (extent - row_spacing) / 2, or None when either quantity is missing
        or not a positive finite number, or when the spacing is not smaller
        than the extent (the rows would not fit inside the board).
    @note Assumes the two rows are symmetric about the body centre, which is
        the same assumption the footprint makes: its origin is the body centre
        and every side is centred along its edge unless an offset is declared.
        Example from a real run: 25.4 mm across, rows 22.86 mm apart, so the
        centres sit 1.27 mm in from each edge. Nothing here is tuned to it.
    """
    try:
        extent = float(extent_mm)
        spacing = float(row_spacing_mm)
    except (TypeError, ValueError):
        return None
    if isinstance(extent_mm, bool) or isinstance(row_spacing_mm, bool):
        return None
    if not (math.isfinite(extent) and math.isfinite(spacing)) or extent <= 0 or spacing <= 0:
        return None
    if spacing >= extent:
        return None
    return round((extent - spacing) / 2, 4)


def fallback_through_hole_inset(pad_diameter_mm: float) -> float:
    """@brief Heuristic distance of a through-hole centre from the edge, in mm.

    @param pad_diameter_mm Diameter of the annular pad around the hole.
    @return pad radius + MIN_HOLE_WALL_MM.
    @note HEURISTIC, used only when neither an explicit offset nor a row
        spacing that can be attributed to the side is available. It is the
        SMALLEST inset compatible with what the style declares -- a closed
        ring with board material between it and the edge -- using the same
        minimum wall this module already requires between two plated
        features. It was not fitted to any board and it is a lower bound: a
        real board usually sets its rows further in (the 2.54 mm grid dev kit
        that motivated the through-hole style has 1.27 mm, this gives 1.05 mm
        with the default 1.7 mm pad). Declaring the row spacing, or the offset
        explicitly, makes it unnecessary; the 3D generator reports its use.
    """
    return round(pad_diameter_mm / 2 + MIN_HOLE_WALL_MM, 4)

#: Etichette leggibili (GUI, messaggi d'errore).
EDGE_LABELS = {
    "left": "sinistro",
    "right": "destro",
    "top": "superiore",
    "bottom": "inferiore",
}


class PadPlacement(NamedTuple):
    """@brief One placed contact land: number, centre and oriented size, in mm."""

    number: str
    x: float
    y: float
    width: float
    height: float
    edge: str
    # Tipo di contatto del lato: decide QUALE piazzola e' questa. Per
    # CONTACT_THROUGH_HOLE il centro e' quello del foro passante e le misure
    # sono il diametro della corona, perche' e' l'unica piazzola che il pezzo
    # offre; per gli altri due e' la piazzola rettangolare di bordo di sempre.
    style: str = CONTACT_HALF_AND_THROUGH


#: Versore verso l'interno del corpo per ogni lato, in coordinate footprint
#: (Y verso il basso): il lato "top" sta a Y negativo, quindi entra verso +Y.
INWARD_FOOTPRINT = {"left": (1.0, 0.0), "right": (-1.0, 0.0), "top": (0.0, 1.0), "bottom": (0.0, -1.0)}


def _clean(value: float) -> float:
    """Round float noise away and turn -0.0 into 0.0, so S-expressions stay stable."""
    return round(float(value), 6) + 0.0


class ContactPad(NamedTuple):
    """@brief The ONE continuous copper land of a half_and_through contact.

    A stadium (KiCad "oval") whose long axis is perpendicular to the board
    edge: it starts at the outer end of the edge land (pad_overhang beyond the
    edge, where the solder fillet of the castellation is inspected) and runs
    inward, past the ring of the through-hole, without interruption. All
    values are in mm, footprint coordinates (origin at the body centre, Y down).

    @note The drill is NOT at the stadium centre: it sits where
        CastellatedModuleParams.thru_hole_centre puts it. In KiCad a pad's
        `at` is the HOLE position and `(drill D (offset dx dy))` moves the
        COPPER away from it, so the footprint pad is written at the drill
        centre with offset = copper centre - drill centre (see `drill_offset`).
    """

    number: str
    edge: str
    #: Centre of the stadium copper (the KiCad pad `at` plus its drill offset).
    x: float
    y: float
    #: Oriented size along X and Y (the KiCad pad `size`, no rotation).
    width: float
    height: float
    #: Through-hole drill diameter and its absolute centre.
    drill: float
    drill_x: float
    drill_y: float
    #: Centre (inside the board) and diameter of the plated half-hole at the edge.
    half_hole_x: float
    half_hole_y: float
    half_hole_diameter: float
    #: Distance from the board edge of the inner (rounded) end, measured inward.
    inner_reach: float
    #: Distance of the outer end beyond the board edge (= pad_overhang).
    outer_reach: float
    #: Stadium width, measured along the edge (the diameter of its round ends).
    across: float

    @property
    def drill_offset(self) -> tuple:
        """@brief KiCad drill offset: copper centre relative to the hole, in mm.

        @return (dx, dy) = stadium centre - drill centre, in footprint
            coordinates; the pad is never rotated, so pad-local and footprint
            axes coincide.
        @note KiCad semantics, verified with pcbnew 10: for `(at X Y)` with
            `(drill D (offset dx dy))` the hole is at (X, Y) (GetPosition) and
            the copper shape is centred at (X + dx, Y + dy) (ShapePos). Until
            2026-09-25 this returned drill - centre and the writer put `at` on
            the copper centre, which swapped hole and copper in KiCad.
        """
        return (_clean(self.x - self.drill_x), _clean(self.y - self.drill_y))

    @property
    def length(self) -> float:
        """@brief Stadium length perpendicular to the edge, in mm (outer_reach + inner_reach)."""
        return self.outer_reach + self.inner_reach

    def axis(self) -> tuple:
        """@brief Segment joining the centres of the two round ends.

        @return ((x0, y0), (x1, y1)) in mm, outer end first. A point belongs to
            the stadium when its distance from this segment is <= across / 2.
        """
        nx, ny = INWARD_FOOTPRINT[self.edge]
        half = max(self.length / 2 - self.across / 2, 0.0)
        return ((self.x - nx * half, self.y - ny * half), (self.x + nx * half, self.y + ny * half))

    def contains(self, px: float, py: float, tolerance: float = 1e-9) -> bool:
        """@brief Tell whether a point lies on the copper of this stadium.

        @param px Point X, mm (footprint coordinates).
        @param py Point Y, mm.
        @param tolerance Numeric slack, mm.
        @return True when the distance from the stadium axis is <= across / 2.
        """
        (ax, ay), (bx, by) = self.axis()
        return _point_segment_distance(px, py, ax, ay, bx, by) <= self.across / 2 + tolerance


def _point_segment_distance(px, py, ax, ay, bx, by) -> float:
    """Euclidean distance from point P to segment AB, in mm."""
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


class OffsetDrillPad(Pad):
    """@brief A KiCad pad whose drill sits off the pad centre.

    KiCad writes it as `(drill D (offset dx dy))`, dx/dy in mm in pad-local
    axes; it is how a single plated pad can carry one hole away from the
    middle of its copper (KiCad 6 and later, unchanged in the 8/9/10 formats).
    The pad `at` is the HOLE position and the offset moves the COPPER shape:
    copper centre = at + offset (pcbnew GetPosition() is the hole, ShapePos()
    the copper centre).
    Kept here rather than in core.footprint so that every other generator's
    output stays byte-identical.
    """

    def __init__(self, *args, drill_offset=(0.0, 0.0), **kwargs):
        """@brief Build the pad.

        @param drill_offset (dx, dy) of the copper centre from the hole
            (the pad `at`), mm, as KiCad defines the drill offset.
        @param args Forwarded to Pad.
        @param kwargs Forwarded to Pad.
        """
        super().__init__(*args, **kwargs)
        self.drill_offset = (_clean(drill_offset[0]), _clean(drill_offset[1]))

    def to_sexpr(self) -> SExpr:
        """@brief Serialize like Pad, adding `(offset dx dy)` inside `(drill ...)` when non-zero."""
        node = super().to_sexpr()
        if self.drill_offset != (0.0, 0.0):
            for child in node.args:
                if isinstance(child, SExpr) and child.name == "drill":
                    child.add(sx("offset", *self.drill_offset))
        return node


@dataclass
class ComponentBox:
    """@brief Component mounted on the module, approximated as a box for the 3D model only.

    Un componente montato sul modulo, approssimato a parallelepipedo.

    Serve solo al modello 3D: un modulo reale non e' una lastra liscia, e
    riconoscerlo a colpo d'occhio (il trasformatore, gli integrati, i LED)
    aiuta a capire da che lato va montato e quanto ingombra davvero.

    Le coordinate sono quelle del footprint: origine al centro del modulo,
    X verso destra, Y verso il basso, Z dal piano del PCB verso l'alto.
    `length` x `width` e' l'ingombro del componente nel SUO sistema (asse
    lungo x asse corto); `rotation` lo orienta sul modulo.
    """

    x: float
    y: float
    length: float
    width: float
    height: float
    name: str = "Componente"
    color_hex: str = "#303030"
    # Tipo di package: decide la forma nel 3D (vedi cad3d/freecad_parts.py).
    kind: str = "generic"
    # Scritta stampata sul componente (es. "SP3485", "VPT"), se leggibile.
    marking: str = ""
    # Orientamento nella foto: `length` e' l'asse LUNGO del componente e
    # `width` quello corto, sempre; rotation dice come sta sul modulo
    # (0 = asse lungo parallelo a X, 90 = verticale). Esplicito, non dedotto
    # dalle proporzioni: con stime quasi quadrate la deduzione sbaglia.
    rotation: float = 0.0
    # Conteggio opzionale dei terminali osservati per un IC; serve al modello 3D.
    pin_count: Optional[int] = None
    pins_by_side: Optional[Dict[str, int]] = None
    detected_kind: Optional[str] = None
    terminal_geometry: Optional[dict] = None
    package: str = ""
    mating: str = ""
    bbox_basis: str = "unknown"
    package_evidence: str = ""
    # Confidence/source belong to the observed contact count, not package identity.
    pin_count_confidence: str = ""
    pin_count_source: str = ""
    # Reviewed native-model placement overrides, not inferred photo pin-1.
    model_rotation_deg: Optional[float] = None
    model_z_offset_mm: float = 0.0
    # Explicit local user choice; absence never establishes a verified identity.
    model_selection: Optional[dict] = None
    model_match: Optional[dict] = None
    # Photographic evidence for mechanical matching, never placement overrides.
    match_evidence: Optional[dict] = None
    # Scelta esplicita dell'utente sul solido: None lascia decidere
    # l'automatismo, False toglie dal 3D un falso positivo del rilevatore,
    # True rimette un pezzo che l'automatismo aveva omesso. La riga resta
    # comunque in tabella: l'esclusione riguarda il modello, non l'inventario.
    render_3d_override: Optional[bool] = None
    # Correzione manuale dell'orientamento del solido, in gradi, SOMMATA
    # all'angolo che il costruttore deduce dal modello nativo. Un delta
    # resta valido anche se il modello scelto cambia; un angolo assoluto no.
    model_rotation_delta_deg: float = 0.0

    def is_usable(self) -> bool:
        """@brief Tell whether the box can become a 3D body.

        @return True when position, sizes and rotation are finite numbers and
            the three sizes are positive. NaN or infinity would otherwise be
            turned into invented coordinates downstream (audit 2026-09-25, E6).
        """
        values = (self.x, self.y, self.length, self.width, self.height, self.rotation)
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   and math.isfinite(value) for value in values):
            return False
        return self.length > 0 and self.width > 0 and self.height > 0


@dataclass
class CastellatedSide:
    """@brief Contacts on one module edge: count, pitch, offset and pin numbering.

    I contatti su un lato del modulo.

    `offset` sposta il gruppo di pad lungo il bordo rispetto alla posizione
    centrata: serve per i moduli in cui i contatti non sono simmetrici
    rispetto al centro del corpo.

    `first_pin` e `reverse` descrivono la numerazione: i pad sono numerati
    in sequenza a partire da `first_pin` seguendo il bordo (per i lati
    verticali dall'alto in basso, per quelli orizzontali da sinistra a
    destra), oppure nel verso opposto con `reverse=True`. Con
    `pin_numbers` si danno invece i numeri uno per uno, quando la
    sequenza non e' regolare.

    `contact_style` (uno di CONTACT_STYLES) dice quali feature porta ogni
    contatto del lato: decide le piazzole del footprint e i fori del 3D, mai
    la numerazione.
    """

    edge: str
    count: int
    pitch: float
    first_pin: int = 1
    reverse: bool = False
    offset: float = 0.0
    pad_length: Optional[float] = None
    pad_width: Optional[float] = None
    pin_numbers: Optional[Sequence[str]] = None
    # Quali feature porta ogni contatto di questo lato (vedi CONTACT_STYLES).
    # Si vede nella fotografia e va chiesto al modello; il default e' il
    # comportamento storico, cosi' ogni progetto e scelta gia' salvati
    # continuano a produrre esattamente cio' che producevano.
    contact_style: str = CONTACT_HALF_AND_THROUGH

    def numbers(self) -> List[str]:
        """@brief Return the pad numbers in the order they are met along the edge.

        Numeri dei pad nell'ordine in cui si incontrano lungo il bordo.
        """
        if self.pin_numbers is not None:
            return [str(n) for n in self.pin_numbers]
        numbers = [str(self.first_pin + i) for i in range(self.count)]
        return list(reversed(numbers)) if self.reverse else numbers


@dataclass
class CastellatedModuleParams:
    """@brief Parameters of a castellated module: body size plus contacts per edge.

    Parametri di un modulo castellated: corpo + contatti per lato.
    """

    name: str = "CastellatedModule"
    description: str = ""
    tags: str = "castellated module SMD"
    body_length: float = 42.8
    body_width: float = 15.2
    sides: List[CastellatedSide] = field(default_factory=list)
    # Dimensioni di default dei pad, sovrascrivibili lato per lato.
    # Land pattern di default: 3.6 x 1.8 mm con 1.4 mm fuori dal bordo, cioe'
    # il pad centrato 0.4 mm fuori dal modulo -- e' il pattern gia' verificato
    # sul convertitore RS485/TTL, un buon punto di partenza per moduli simili.
    pad_length: float = 3.6
    pad_width: float = 1.8
    pad_overhang: float = 1.4
    # Diametro del semi-foro metallizzato sul bordo: non serve al footprint
    # (i pad sono rettangolari) ma al modello 3D, dove i semi-fori vanno
    # tagliati nel solido perche' il pezzo somigli a quello reale.
    hole_diameter: float = 1.7
    # Distanza del centro del semi-foro dal bordo (None = 0.76 * raggio): un
    # castellated e' un foro normale forato vicino al bordo e poi tagliato.
    hole_inset: Optional[float] = None
    # Foro passante CHIUSO: quota comune a tutti i lati che lo portano.
    # Quante feature abbia un contatto (solo semi-foro, solo foro passante,
    # entrambi) non si decide qui: e' `CastellatedSide.contact_style`, letto
    # dalla fotografia. Mettere il diametro a zero resta il modo storico di
    # dire "solo semi-fori" per i lati half_and_through.
    thru_hole_drill: float = 1.0
    thru_pad_diameter: float = 1.7
    # Distanza del centro del foro chiuso dal bordo. A zero la calcola il
    # programma: per half_and_through dal semi-foro (i due fori non devono
    # toccarsi), per through_hole dalle file dichiarate (vedi
    # through_hole_inset). Un numero esplicito viene rispettato, ma controllato.
    thru_hole_offset: float = 0.0
    # Distanza dichiarata fra i centri di due file di contatti su lati
    # opposti (ExtractedParams.row_spacing_mm). Serve solo ai lati
    # through_hole: con l'ingombro del corpo da' la distanza dal bordo dei
    # fori senza inventarla. None = non dichiarata.
    row_spacing: Optional[float] = None
    # Il modulo non e' un blocco pieno: e' un PCB sottile con sopra i
    # componenti. Il modello 3D lo costruisce come due solidi, perche' un
    # parallelepipedo unico alto quanto il componente piu' alto non
    # somiglia al pezzo reale ne' serve a valutare gli ingombri.
    pcb_thickness: float = 1.0
    component_inset: float = 1.0
    component_color_hex: str = "#303030"
    # Aspetto del PCB nel 3D: colore del substrato visibile di taglio, sigle
    # serigrafate accanto ai contatti (numero pad -> testo) e loro colore.
    substrate_color_hex: str = "#2f3f1f"
    silk_labels: Dict[str, str] = field(default_factory=dict)
    silk_color_hex: str = "#e8e8e0"
    show_silk_labels_3d: bool = False
    # Componenti letti dalla foto del modulo: se ci sono, il 3D li disegna
    # uno per uno invece del blocco unico ricavato da component_inset.
    components: List[ComponentBox] = field(default_factory=list)
    margin: float = 0.25

    @property
    def half_hole_inset(self) -> float:
        """@brief Distanza dal bordo del centro del semi-foro, in mm.

        @return Il valore dichiarato, oppure 0.76 raggi come da convenzione.
        @note Un castellated e' un foro normale forato vicino al bordo e poi
            tagliato: il centro sta DENTRO la scheda. La stessa formula era
            scritta nel generatore 3D e non era visibile da qui, quindi la
            validazione non poteva tenerne conto -- ed e' per questo che due
            fori sovrapposti passavano senza che nessuno se ne accorgesse.
        """
        return (self.hole_inset if self.hole_inset is not None
                else round(self.hole_diameter / 2 * 0.76, 3))

    @property
    def thru_hole_centre_offset(self) -> float:
        """@brief Distanza dal bordo del centro del foro passante, in mm.

        @return Il valore dichiarato, oppure il minimo che tiene separati i
            due fori lasciando MIN_HOLE_WALL_MM di parete fra loro.
        @note Non e' una quota scelta su una scheda: e' la distanza sotto la
            quale le due cavita' si fondono, piu' una parete. Derivarla invece
            di fissarla la rende giusta con qualunque semi-foro.
        """
        if self.thru_hole_offset > 0:
            return self.thru_hole_offset
        return round(self.half_hole_inset + self.hole_diameter / 2
                     + self.thru_hole_drill / 2 + MIN_HOLE_WALL_MM, 3)

    @property
    def has_thru_holes(self) -> bool:
        """@brief True quando ogni contatto porta anche un foro passante chiuso.

        @return True se sia il foro sia la sua piazzola sono dichiarati.
        @note Mettere il diametro a zero e' il modo di dire "questo modulo ha
            solo i semi-fori": non esiste un foro di diametro nullo, quindi lo
            zero non puo' essere confuso con una quota.
        """
        return self.thru_hole_drill > 0 and self.thru_pad_diameter > 0

    def edge_extent(self, edge: str) -> float:
        """@brief Board extent across a contact row on `edge`, in mm.

        @param edge One of EDGES.
        @return body_length for left/right rows (measured along X), body_width
            for top/bottom rows (measured along Y).
        """
        return self.body_length if edge in ("left", "right") else self.body_width

    def _row_spacing_edges(self) -> Optional[tuple]:
        """The opposite pair the declared row spacing belongs to, if any."""
        if self.row_spacing is None:
            return None
        return opposite_row_edges(side.edge for side in self.sides)

    def through_hole_inset_source(self, edge: str) -> str:
        """@brief Where the through-hole inset of a CONTACT_THROUGH_HOLE side comes from.

        @param edge Edge of the side.
        @return "explicit" (thru_hole_offset), "row_spacing" (body extent and
            declared row spacing) or "fallback" (declared heuristic, see
            fallback_through_hole_inset).
        """
        if self.thru_hole_offset > 0:
            return "explicit"
        pair = self._row_spacing_edges()
        if (pair is not None and edge in pair
                and through_hole_inset_from_rows(self.edge_extent(edge), self.row_spacing) is not None):
            return "row_spacing"
        return "fallback"

    def through_hole_inset(self, edge: str) -> float:
        """@brief Distance from the edge of the through-hole centres of a through_hole side.

        @param edge Edge of the side.
        @return In mm: the explicit thru_hole_offset when given; otherwise
            (extent - row_spacing) / 2 when a row spacing is declared for this
            edge's opposite pair; otherwise the declared heuristic
            fallback_through_hole_inset().
        @note Distinct from thru_hole_centre_offset, which places the hole of a
            half_and_through contact behind its half-hole: with no half-hole
            that derivation has nothing to refer to.
        """
        source = self.through_hole_inset_source(edge)
        if source == "explicit":
            return self.thru_hole_offset
        if source == "row_spacing":
            return through_hole_inset_from_rows(self.edge_extent(edge), self.row_spacing)
        return fallback_through_hole_inset(self.thru_pad_diameter)

    def needs_half_holes(self) -> bool:
        """@brief True when at least one side cuts half-holes into the edge."""
        return any(style_has_half_hole(side.contact_style) for side in self.sides)

    def thru_hole_centre(self, placement: "PadPlacement") -> tuple:
        """@brief Centro del foro chiuso di un contatto, in coordinate footprint.

        @param placement Contatto gia' posizionato sul suo bordo.
        @return (x, y) in mm: il foro sta sulla stessa riga del contatto, ma
            spostato verso l'interno del corpo. Per un contatto through_hole la
            piazzola E' la corona del foro, quindi il suo centro e' gia' quello.
        """
        if placement.style == CONTACT_THROUGH_HOLE:
            return (placement.x, placement.y)
        offset = self.thru_hole_centre_offset
        if placement.edge == "left":
            return (-self.body_length / 2 + offset, placement.y)
        if placement.edge == "right":
            return (self.body_length / 2 - offset, placement.y)
        if placement.edge == "top":
            return (placement.x, -self.body_width / 2 + offset)
        return (placement.x, self.body_width / 2 - offset)

    def side_pad_length(self, side: CastellatedSide) -> float:
        """@brief Return the pad length in mm for `side`, falling back to the module default."""
        return side.pad_length if side.pad_length is not None else self.pad_length

    def side_pad_width(self, side: CastellatedSide) -> float:
        """@brief Return the pad width in mm for `side`, falling back to the module default."""
        return side.pad_width if side.pad_width is not None else self.pad_width

    def edge_length(self, edge: str) -> float:
        """@brief Return the body edge length in mm along which the pads of `edge` are spread.

        Lunghezza del bordo lungo cui si distribuiscono i pad.
        """
        return self.body_width if edge in ("left", "right") else self.body_length

    def validate(self) -> None:
        """@brief Check that the module can be drawn as a manufacturable footprint.

        @throws ValueError With a localized message on the first violation.
        @note Contract: every dimension is a finite number; every contact row
            fits inside its edge, offset included; the copper of two different
            pins never touches (strictly positive gap, every pair of contacts,
            any side, any contact style); a pad keeps part of its land under
            the module (0 <= pad_overhang < pad_length); every pin number is
            non-empty and unique.
        """
        self._validate_finite_numbers()
        if not self.name.strip():
            raise ValueError(tx("Il nome del footprint non puo' essere vuoto."))
        if self.body_length <= 0 or self.body_width <= 0:
            raise ValueError(tx("Le dimensioni del corpo devono essere > 0."))
        if not self.sides:
            raise ValueError(tx("Serve almeno un lato con dei contatti."))
        # Un valore sconosciuto si rifiuta, non si rimappa: indovinare quale
        # dei tre si intendesse sarebbe esattamente la deduzione che il campo
        # esiste per evitare.
        for side in self.sides:
            if side.contact_style not in CONTACT_STYLES:
                raise ValueError(
                    tx("Lato {get}: tipo di contatto {contact_style!r} non riconosciuto "
                       "(ammessi: {join}).",
                       get=EDGE_LABELS.get(side.edge, side.edge),
                       contact_style=side.contact_style,
                       join=', '.join(CONTACT_STYLES))
                )
        # Il semi-foro serve solo ai lati che lo tagliano davvero.
        if self.needs_half_holes() and self.hole_diameter <= 0:
            raise ValueError(tx("Il diametro del semi-foro deve essere > 0."))
        if self.pcb_thickness <= 0:
            raise ValueError(tx("Lo spessore del PCB deve essere > 0."))
        if self.thru_hole_drill < 0 or self.thru_pad_diameter < 0:
            raise ValueError(tx("Le quote del foro passante non possono essere negative."))
        if self.pad_overhang < 0:
            raise ValueError(
                tx("Lo sbalzo dei pad oltre il bordo ({pad_overhang}mm) non puo' essere negativo.",
                   pad_overhang=self.pad_overhang)
            )
        self._validate_through_hole_sides()
        # Il controllo storico del foro dietro al semi-foro vale solo dove
        # entrambi esistono: su un lato through_hole non c'e' semi-foro con cui
        # fondersi, su un lato half_hole non c'e' foro.
        if self.has_thru_holes and any(side.contact_style == CONTACT_HALF_AND_THROUGH
                                       for side in self.sides):
            if self.thru_pad_diameter <= self.thru_hole_drill:
                raise ValueError(
                    tx("La piazzola del foro passante ({thru_pad_diameter}mm) deve essere "
                       "piu' larga del foro ({thru_hole_drill}mm): senza corona non c'e' rame "
                       "su cui saldare.",
                       thru_pad_diameter=self.thru_pad_diameter,
                       thru_hole_drill=self.thru_hole_drill)
                )
            # Un foro il cui bordo esce dal corpo non e' un foro chiuso: e' un
            # secondo semi-foro, e allora tanto vale non dichiararlo.
            gap = (self.thru_hole_centre_offset - self.half_hole_inset
                   - self.hole_diameter / 2 - self.thru_hole_drill / 2)
            if gap < 0:
                minimum = (self.half_hole_inset + self.hole_diameter / 2
                           + self.thru_hole_drill / 2 + MIN_HOLE_WALL_MM)
                raise ValueError(
                    tx("Il foro passante e il semi-foro si toccano: i loro assi distano "
                       "{value:.2f}mm ma la somma dei raggi e' {value2:.2f}mm, quindi si "
                       "fonderebbero in un'unica cavita' aperta sul bordo invece di restare "
                       "due fori. Porta la distanza dal bordo ad almeno {minimum:.2f}mm, "
                       "oppure riduci il semi-foro.",
                       value=self.thru_hole_centre_offset - self.half_hole_inset,
                       value2=self.hole_diameter / 2 + self.thru_hole_drill / 2,
                       minimum=minimum)
                )
            if self.thru_hole_centre_offset < self.thru_pad_diameter / 2:
                raise ValueError(
                    tx("Il centro del foro passante dista {thru_hole_centre_offset}mm dal "
                       "bordo, meno del raggio della sua piazzola ({value}mm): resterebbe "
                       "aperto sul bordo invece di essere chiuso.",
                       thru_hole_centre_offset=self.thru_hole_centre_offset,
                       value=self.thru_pad_diameter / 2)
                )
            # Solo le coppie di lati opposti che portano DAVVERO fori passanti
            # half_and_through si contendono l'ingombro: un modulo 30x4 con
            # contatti solo a sinistra e destra misura 30 mm fra le due file,
            # non 4. Un lato senza dirimpettaio e' gia' coperto dai controlli
            # sopra (il foro resta chiuso dentro il corpo).
            carrying = {side.edge for side in self.sides
                        if side.contact_style == CONTACT_HALF_AND_THROUGH}
            for pair in OPPOSITE_EDGE_PAIRS:
                if not set(pair) <= carrying:
                    continue
                shortest = self.edge_extent(pair[0])
                if 2 * self.thru_hole_centre_offset + self.thru_pad_diameter > shortest + 1e-6:
                    raise ValueError(
                        tx("I fori passanti dei due lati opposti non stanno in {shortest}mm: "
                           "servono almeno {value:.2f}mm.",
                           shortest=shortest,
                           value=2 * self.thru_hole_centre_offset + self.thru_pad_diameter)
                    )

        seen_numbers: dict[str, str] = {}
        for side in self.sides:
            label = EDGE_LABELS.get(side.edge, side.edge)
            if side.edge not in EDGES:
                raise ValueError(
                    tx("Lato non valido: {edge!r} (attesi: {join}).",
                       edge=side.edge,
                       join=', '.join(EDGES))
                )
            if side.count < 1:
                raise ValueError(tx("Lato {label}: servono almeno 1 pad.", label=label))
            if side.pitch <= 0 and side.count > 1:
                raise ValueError(tx("Lato {label}: il passo deve essere > 0.", label=label))

            pad_length = self.side_pad_length(side)
            pad_width = self.side_pad_width(side)
            if pad_length <= 0 or pad_width <= 0:
                raise ValueError(tx("Lato {label}: le dimensioni dei pad devono essere > 0.", label=label))
            # Una piazzola tutta fuori dal bordo non sta sotto il modulo: non
            # c'e' nulla da saldare. Vale per i lati con piazzola di bordo.
            if side.contact_style != CONTACT_THROUGH_HOLE and self.pad_overhang >= pad_length:
                raise ValueError(
                    tx("Lato {label}: lo sbalzo dei pad oltre il bordo ({pad_overhang}mm) non e' "
                       "minore della loro lunghezza ({pad_length}mm): la piazzola starebbe tutta "
                       "fuori dal modulo.",
                       label=label,
                       pad_overhang=self.pad_overhang,
                       pad_length=pad_length)
                )
            # La piazzola che si allinea lungo il bordo e' quella che il lato
            # offre davvero: la corona del foro per through_hole, la piazzola
            # rettangolare di bordo per gli altri due tipi.
            if side.contact_style == CONTACT_THROUGH_HOLE:
                land = self.thru_pad_diameter
                if side.count > 1 and side.pitch < land:
                    raise ValueError(
                        tx("Lato {label}: passo {pitch}mm minore del diametro della corona "
                           "{land}mm -- le piazzole dei fori passanti si sovrapporrebbero.",
                           label=label,
                           pitch=side.pitch,
                           land=land)
                    )
            else:
                land = pad_width
                if side.count > 1 and side.pitch < pad_width:
                    raise ValueError(
                        tx("Lato {label}: passo {pitch}mm minore della larghezza pad "
                           "{pad_width}mm -- i pad si sovrapporrebbero.",
                           label=label,
                           pitch=side.pitch,
                           pad_width=pad_width)
                    )
                if has_continuous_pad(self, side.contact_style):
                    # La piazzola continua contiene anche la corona del foro:
                    # e' larga quanto la maggiore delle due, e il passo deve
                    # lasciarle separate.
                    land = continuous_pad_across(self, pad_width)
                    if side.count > 1 and side.pitch < land:
                        raise ValueError(
                            tx("Lato {label}: passo {pitch}mm minore della larghezza della piazzola "
                               "continua {land}mm (la maggiore fra pad {pad_width}mm e corona "
                               "{thru_pad_diameter}mm) -- i pad si sovrapporrebbero.",
                               label=label,
                               pitch=side.pitch,
                               land=land,
                               pad_width=pad_width,
                               thru_pad_diameter=self.thru_pad_diameter)
                        )

            span = (side.count - 1) * side.pitch + land
            available = self.edge_length(side.edge)
            if span - 1e-6 > available:
                raise ValueError(
                    tx("Lato {label}: i {count} pad occupano {span:.2f}mm ma il bordo e' "
                       "lungo {available:.2f}mm.",
                       label=label,
                       count=side.count,
                       span=span,
                       available=available)
                )
            # Il gruppo e' centrato e poi spostato di `offset`: anche spostato
            # deve restare entro il bordo, altrimenti i pad finiscono oltre
            # l'angolo del modulo.
            if abs(side.offset) + span / 2 > available / 2 + 1e-6:
                raise ValueError(
                    tx("Lato {label}: spostati di {offset}mm, i pad escono dal bordo: "
                       "occupano fino a {reach:.2f}mm dal centro, il bordo arriva a "
                       "{half:.2f}mm.",
                       label=label,
                       offset=side.offset,
                       reach=abs(side.offset) + span / 2,
                       half=available / 2)
                )
            if side.pin_numbers is not None and any(
                    n is None or not str(n).strip() for n in side.pin_numbers):
                raise ValueError(tx("Lato {label}: un numero di pin e' vuoto.", label=label))

            numbers = side.numbers()
            if len(numbers) != side.count:
                raise ValueError(
                    tx("Lato {label}: {n_numbers} numeri di pin per {count} pad.",
                       label=label,
                       n_numbers=len(numbers),
                       count=side.count)
                )
            for number in numbers:
                if number in seen_numbers:
                    raise ValueError(
                        tx("Il pin {number} compare sia sul lato {seen_numbers} sia su quello "
                           "{label}.",
                           number=number,
                           seen_numbers=seen_numbers[number],
                           label=label)
                    )
                seen_numbers[number] = label

        self._validate_copper_clearance()

    def _validate_finite_numbers(self) -> None:
        """Refuse NaN, infinities, booleans and non-numbers before any comparison.

        A NaN compares False with everything, so every `<= 0` check below
        would silently accept it and the footprint writer would emit "nan".
        """
        def check(value, name, optional=False):
            if value is None and optional:
                return
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(tx("{name} deve essere un numero finito (mm).", name=name))

        for name in ("body_length", "body_width", "pad_length", "pad_width", "pad_overhang",
                     "hole_diameter", "thru_hole_drill", "thru_pad_diameter", "thru_hole_offset",
                     "pcb_thickness", "margin"):
            check(getattr(self, name), name)
        # row_spacing ha il suo controllo, con il suo messaggio, dove serve
        # davvero (lati through_hole): altrove non entra nella geometria.
        check(self.hole_inset, "hole_inset", optional=True)
        for side in self.sides:
            label = EDGE_LABELS.get(side.edge, side.edge)
            for name in ("pitch", "offset"):
                check(getattr(side, name), f"{name} ({label})")
            for name in ("pad_length", "pad_width"):
                check(getattr(side, name), f"{name} ({label})", optional=True)
            for name in ("count", "first_pin"):
                value = getattr(side, name)
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(tx("Lato {label}: {name} deve essere un numero intero.",
                                        label=label, name=name))

    def _validate_copper_clearance(self) -> None:
        """Refuse any two contacts of different pins whose copper touches or overlaps.

        One rule for every pair (same side, adjacent sides near a corner,
        facing rows of a narrow body) and every contact style: the copper of
        each contact is the envelope from _copper_outline() and the gap
        between two envelopes must be strictly positive. Copper that merely
        touches is a short on the host board, so it is refused too (until
        2026-09-25 touching was accepted and only pairs involving a continuous
        pad were checked). Contacts with the same number may share copper.
        """
        placements = list(pad_positions(self))
        outlines = [(placement, _copper_outline(self, placement)) for placement in placements]
        for i, (first, a) in enumerate(outlines):
            for second, b in outlines[i + 1:]:
                if first.number == second.number:
                    continue
                if _box_distance(a, b) - a[4] - b[4] > 1e-6:
                    continue
                if has_continuous_pad(self, first.style) or has_continuous_pad(self, second.style):
                    stadium = first if has_continuous_pad(self, first.style) else second
                    pad = continuous_pad(self, stadium)
                    raise ValueError(
                        tx("La piazzola continua del contatto {number} (lato {get}), che dal "
                           "bordo arriva {inner_reach:.2f}mm dentro il corpo fino oltre il foro "
                           "passante, tocca il rame del contatto {value} (lato {get2}): due pin "
                           "diversi sarebbero in corto. Avvicina il foro passante al bordo, "
                           "accorcia la piazzola o riduci la corona.",
                           number=stadium.number,
                           get=EDGE_LABELS.get(stadium.edge, stadium.edge),
                           inner_reach=pad.inner_reach,
                           value=second.number if stadium is first else first.number,
                           get2=EDGE_LABELS.get((second if stadium is first else first).edge, ''))
                    )
                raise ValueError(
                    tx("Il rame del contatto {first} (lato {first_edge}) tocca quello del "
                       "contatto {second} (lato {second_edge}): due pin diversi sarebbero in "
                       "corto. Aumenta il passo, sposta i contatti o riduci le piazzole.",
                       first=first.number,
                       first_edge=EDGE_LABELS.get(first.edge, first.edge),
                       second=second.number,
                       second_edge=EDGE_LABELS.get(second.edge, second.edge))
                )

    def _validate_through_hole_sides(self) -> None:
        """Checks for sides whose only feature is a full through-hole."""
        # Un lato con bordo non valido viene rifiutato piu' avanti, con il suo
        # messaggio: qui non ha una geometria da controllare.
        through = [side for side in self.sides
                   if side.contact_style == CONTACT_THROUGH_HOLE and side.edge in EDGES]
        if not through:
            return
        labels = ", ".join(EDGE_LABELS.get(side.edge, side.edge) for side in through)
        if self.thru_hole_drill <= 0:
            raise ValueError(
                tx("Lato {labels}: i contatti sono fori passanti ma il diametro del foro "
                   "e' zero. Indica il foro, oppure scegli il tipo «mezzo foro sul bordo».",
                   labels=labels)
            )
        if self.thru_pad_diameter <= self.thru_hole_drill:
            raise ValueError(
                tx("Lato {labels}: la piazzola del foro passante ({thru_pad_diameter}mm) "
                   "deve essere piu' larga del foro ({thru_hole_drill}mm): senza corona "
                   "non c'e' rame su cui saldare.",
                   labels=labels,
                   thru_pad_diameter=self.thru_pad_diameter,
                   thru_hole_drill=self.thru_hole_drill)
            )
        spacing = self.row_spacing
        if spacing is not None and (
                isinstance(spacing, bool) or not isinstance(spacing, (int, float))
                or not math.isfinite(spacing) or spacing <= 0):
            raise ValueError(
                tx("La distanza fra le file ({row_spacing!r}) deve essere un numero > 0.",
                   row_spacing=self.row_spacing)
            )
        for side in through:
            label = EDGE_LABELS.get(side.edge, side.edge)
            source = self.through_hole_inset_source(side.edge)
            inset = self.through_hole_inset(side.edge)
            radius = self.thru_pad_diameter / 2
            # Un contatto through_hole ha per definizione il bordo intatto:
            # una corona che esce dal corpo sarebbe un semi-foro.
            if inset < radius:
                if source == "row_spacing":
                    why = (tx("le file dichiarate distano {row_spacing}mm su un corpo di "
                              "{edge_extent}mm, quindi i fori starebbero a {inset}mm dal bordo",
                              row_spacing=self.row_spacing,
                              edge_extent=self.edge_extent(side.edge),
                              inset=inset))
                else:
                    why = tx("il centro del foro dista {inset}mm dal bordo", inset=inset)
                raise ValueError(
                    tx("Lato {label}: {why}, meno del raggio della corona ({radius}mm): il "
                       "foro resterebbe aperto sul bordo, cioe' sarebbe un semi-foro e non un "
                       "foro passante.",
                       label=label,
                       why=why,
                       radius=radius)
                )
            extent = self.edge_extent(side.edge)
            opposite = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}[side.edge]
            facing = any(other.edge == opposite for other in self.sides)
            if facing and 2 * inset + self.thru_pad_diameter > extent + 1e-6:
                raise ValueError(
                    tx("Lato {label}: le corone dei fori passanti di due file opposte non "
                       "stanno in {extent}mm: servono almeno {value:.2f}mm.",
                       label=label,
                       extent=extent,
                       value=2 * inset + self.thru_pad_diameter)
                )
            if inset + radius > extent + 1e-6:
                raise ValueError(
                    tx("Lato {label}: il foro passante a {inset}mm dal bordo esce dal lato "
                       "opposto del corpo ({extent}mm).",
                       label=label,
                       inset=inset,
                       extent=extent)
                )


def pad_positions(params: CastellatedModuleParams) -> Iterator[PadPlacement]:
    """@brief Yield the land of every contact, already placed and oriented.

    @param params Module description.
    @return Iterator of PadPlacement in footprint coordinates (mm, Y down).
    @note Single source of truth for the geometry: the 2D footprint, the 3D
        payload and the GUI preview all read it, so there are no two
        computations to keep aligned. The along-edge coordinate and the pin
        numbering depend only on the side, never on its contact style; the
        style decides WHICH land is yielded -- the rectangular edge pad for
        half_hole and half_and_through, the annular ring of the through-hole
        (centre at through_hole_inset, size = ring diameter) for through_hole,
        because that ring is the only land such a part offers.
    @note For half_and_through with a declared through-hole the edge pad
        yielded here is the REFERENCE the continuous copper is built from
        (same outer end, same along-edge position): the copper actually drawn
        is continuous_pad(params, placement), which replaces it.
    """
    half_length = params.body_length / 2
    half_width = params.body_width / 2

    for side in params.sides:
        pad_length = params.side_pad_length(side)
        pad_width = params.side_pad_width(side)
        style = side.contact_style

        # Coordinata lungo il bordo: gruppo centrato, poi spostato di offset.
        start = -(side.count - 1) * side.pitch / 2 + side.offset
        outer = (half_length if side.edge in ("left", "right") else half_width)
        if style == CONTACT_THROUGH_HOLE:
            # La corona sta arretrata dal bordo: fra lei e il bordo c'e'
            # scheda intatta, quindi nessuna parte sporge.
            center_offset = outer - params.through_hole_inset(side.edge)
            pad_length = pad_width = params.thru_pad_diameter
        else:
            # Il pad sporge di pad_overhang oltre il bordo, il resto della sua
            # lunghezza entra sotto il modulo.
            center_offset = outer + params.pad_overhang - pad_length / 2

        for index, number in enumerate(side.numbers()):
            along = start + index * side.pitch
            if side.edge == "left":
                yield PadPlacement(number, -center_offset, along, pad_length, pad_width, side.edge, style)
            elif side.edge == "right":
                yield PadPlacement(number, center_offset, along, pad_length, pad_width, side.edge, style)
            elif side.edge == "top":
                yield PadPlacement(number, along, -center_offset, pad_width, pad_length, side.edge, style)
            else:  # bottom
                yield PadPlacement(number, along, center_offset, pad_width, pad_length, side.edge, style)


def has_continuous_pad(params: CastellatedModuleParams, style: str) -> bool:
    """@brief Tell whether contacts of `style` are drawn as one continuous stadium pad.

    @param params Module description.
    @param style One of CONTACT_STYLES.
    @return True for CONTACT_HALF_AND_THROUGH when the module declares a closed
        through-hole (params.has_thru_holes). With a zero drill such a side is
        "half-holes only" and keeps its historical rectangular edge land.
    """
    return style == CONTACT_HALF_AND_THROUGH and params.has_thru_holes


def continuous_pad_across(params: CastellatedModuleParams, land_width: float) -> float:
    """@brief Width (along the edge) of the continuous pad of a half_and_through contact, mm.

    @param params Module description (for the through-hole ring diameter).
    @param land_width Declared width of the edge land (side pad_width), mm.
    @return max(land_width, thru_pad_diameter).
    @note Not a tuned value: the copper must contain BOTH declared lands, the
        edge land (land_width wide) and the full annular ring of the
        through-hole (thru_pad_diameter), so the narrowest stadium doing so is
        as wide as the larger of the two. This keeps the declared annular ring
        (thru_pad_diameter - drill) / 2 all around the drill.
    """
    return max(land_width, params.thru_pad_diameter)


def continuous_pad(params: CastellatedModuleParams, placement: PadPlacement) -> Optional[ContactPad]:
    """@brief The single continuous copper land of a half_and_through contact.

    @param params Module description.
    @param placement The contact's edge land, from pad_positions().
    @return ContactPad, or None when the contact is not drawn as one
        continuous pad (see has_continuous_pad): half_hole and through_hole
        contacts, and half_and_through ones without a declared through-hole,
        keep exactly their historical lands.
    @note Rule (user decision 2026-09-23, from the photo of a real board:
        each contact is ONE elongated gold pad, no bare board between the
        castellation and the through-hole). Every dimension comes from declared
        parameters:
        * outer end: pad_overhang beyond the board edge, like the edge land;
        * inner end: the farthest of the edge land's inner end
          (pad_length - pad_overhang) and the far side of the through-hole
          ring (thru_hole_centre_offset + thru_pad_diameter / 2);
        * width: continuous_pad_across();
        * geometric floor: a stadium is never shorter than it is wide (else
          its round ends would turn along the edge), and its inner end is at
          least half a width inside the edge (else the 3D copper, which starts
          at the edge, would have no room for its round end). Both only
          lengthen the pad inward, never move the outer end.
        However far the through-hole sits, the pad stays continuous: that is
        what the style declares physically. The limits are physical and are
        enforced by validate(): the pad must not reach another contact's copper.
    """
    if not has_continuous_pad(params, placement.style):
        return None
    vertical_edge = placement.edge in ("left", "right")
    land_length = placement.width if vertical_edge else placement.height
    land_width = placement.height if vertical_edge else placement.width
    across = continuous_pad_across(params, land_width)
    overhang = params.pad_overhang
    inner = max(land_length - overhang,
                params.thru_hole_centre_offset + params.thru_pad_diameter / 2,
                across - overhang,
                across / 2)
    length = overhang + inner
    nx, ny = INWARD_FOOTPRINT[placement.edge]
    # Punto del bordo sull'asse del contatto: da li' si misura tutto.
    if placement.edge == "left":
        edge_x, edge_y = -params.body_length / 2, placement.y
    elif placement.edge == "right":
        edge_x, edge_y = params.body_length / 2, placement.y
    elif placement.edge == "top":
        edge_x, edge_y = placement.x, -params.body_width / 2
    else:
        edge_x, edge_y = placement.x, params.body_width / 2
    centre_depth = (inner - overhang) / 2
    drill_x, drill_y = params.thru_hole_centre(placement)
    size = (length, across) if vertical_edge else (across, length)
    return ContactPad(
        number=placement.number, edge=placement.edge,
        x=_clean(edge_x + nx * centre_depth), y=_clean(edge_y + ny * centre_depth),
        width=_clean(size[0]), height=_clean(size[1]),
        drill=params.thru_hole_drill, drill_x=_clean(drill_x), drill_y=_clean(drill_y),
        half_hole_x=_clean(edge_x + nx * params.half_hole_inset),
        half_hole_y=_clean(edge_y + ny * params.half_hole_inset),
        half_hole_diameter=params.hole_diameter,
        inner_reach=_clean(inner), outer_reach=_clean(overhang), across=_clean(across),
    )


def continuous_pads(params: CastellatedModuleParams) -> Iterator[ContactPad]:
    """@brief Yield the continuous pad of every contact that has one, in pad_positions() order.

    @param params Module description.
    @return Iterator of ContactPad (half_and_through contacts with a through-hole only).
    @note The GUI preview can draw each one as a rounded rectangle of size
        (width, height) centred at (x, y) with corner radius across / 2, plus
        the drill circle at (drill_x, drill_y); the edge land of the same
        number from pad_positions() is then entirely replaced by it.
    """
    for placement in pad_positions(params):
        pad = continuous_pad(params, placement)
        if pad is not None:
            yield pad


def _copper_outline(params: CastellatedModuleParams, placement: PadPlacement) -> tuple:
    """Copper of one contact as (core box x0, y0, x1, y1, radius), footprint mm.

    The copper is the set of points within `radius` of the (possibly
    degenerate) axis-aligned core box: a stadium is its axis segment with
    radius across/2, a ring is its centre with radius ring/2, a rectangular
    edge land is its own box with radius 0 (a conservative bound for a
    roundrect).
    """
    pad = continuous_pad(params, placement)
    if pad is not None:
        (ax, ay), (bx, by) = pad.axis()
        return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by), pad.across / 2)
    if placement.style == CONTACT_THROUGH_HOLE:
        return (placement.x, placement.y, placement.x, placement.y, params.thru_pad_diameter / 2)
    return (placement.x - placement.width / 2, placement.y - placement.height / 2,
            placement.x + placement.width / 2, placement.y + placement.height / 2, 0.0)


def _copper_outline_bbox(params: CastellatedModuleParams, placement: PadPlacement) -> tuple:
    """Axis-aligned bounding box (x0, y0, x1, y1) of one contact's copper, mm."""
    x0, y0, x1, y1, radius = _copper_outline(params, placement)
    return (x0 - radius, y0 - radius, x1 + radius, y1 + radius)


def _box_distance(a: tuple, b: tuple) -> float:
    """Distance between two axis-aligned boxes (x0, y0, x1, y1, ...), mm; 0 when they meet."""
    dx = max(0.0, a[0] - b[2], b[0] - a[2])
    dy = max(0.0, a[1] - b[3], b[1] - a[3])
    return math.hypot(dx, dy)


def build_castellated_module(params: CastellatedModuleParams) -> Footprint:
    """@brief Build the KiCad footprint of the module described by `params`.

    @param params Module description; validated first.
    @return Footprint with, per contact, exactly the lands the part offers:
        an SMD edge pad for half_hole; for half_and_through ONE plated pad
        whose copper is a stadium ("oval") running from the outer end of the
        edge land to beyond the through-hole ring, with the drill offset inside
        it (see continuous_pad), or the SMD edge pad alone when no
        through-hole is declared; a THT pad only for through_hole.
    @throws ValueError When `params` does not validate.
    @note A through_hole side emits no edge pad: a board without
        castellations cannot be reflowed onto edge pads, so such a footprint
        could not be soldered to the part. The footprint attribute stays
        "smd" whenever some side has castellations (the module is reflowed),
        "through_hole" when every contact is a through-hole. An "smd"
        footprint that carries drilled pads (continuous pads, or a side of
        through-holes next to castellated sides) gets
        SMD_WITH_DRILLED_PADS_NOTE appended to its description: KiCad flags
        that combination and leaves non-SMD footprints out of the default
        position file, so the reason for keeping "smd" is written where the
        library user reads it (user decision 2026-09-24; confirmed the same
        day for modules mixing half_hole and through_hole sides, which are
        reflowed too, with no pins in the holes).
    @note The continuous pad carries F.Paste so the castellations can still be
        reflowed (the old SMD edge pad carried it). KiCad applies the paste to
        the whole copper, drill mouth included: pin-in-paste, which also fills
        the module's through-hole above. Kept on purpose (user decision
        2026-09-24), knowing it is more paste than the old edge land and can
        bridge contacts on a tight pitch; no paste-only pad is added. It
        carries no `pad_prop_castellated`: this footprint is the land on the HOST board, which is not cut by any
        board edge; the castellation belongs to the module and is in the 3D.
    @note half_hole and through_hole contacts are emitted exactly as before
        (byte-identical output).
    """
    params.validate()

    total_pads = sum(side.count for side in params.sides)
    has_edge_pads = any(side.contact_style != CONTACT_THROUGH_HOLE for side in params.sides)
    kind = "castellated" if params.needs_half_holes() else "a fori passanti"
    fp = Footprint(
        name=params.name,
        description=params.description or (
            f"Modulo {kind} {params.body_length}x{params.body_width}mm, "
            f"{total_pads} contatti su {len(params.sides)} lati, "
            "generato con kicadgen_tool"
        ),
        tags=params.tags,
        attr="smd" if has_edge_pads else "through_hole",
    )

    half_length = params.body_length / 2
    half_width = params.body_width / 2

    placements = list(pad_positions(params))
    # Piazzola continua (half_and_through con foro): sostituisce sia la
    # piazzola di bordo sia la corona separata dello stesso contatto.
    stadiums = [continuous_pad(params, placement) for placement in placements]
    # Rame esposto di ogni contatto, allargato della distanza serigrafia-pad:
    # la serigrafia non deve mai coprire una piazzola (i pad di bordo
    # sporgono oltre il contorno del corpo e lo attraversano).
    keepouts = [keepout_box(*_copper_outline_bbox(params, placement), line_width=0.12)
                for placement in placements]

    # Contorno del corpo: e' il modulo vero e proprio, quindi va sia in
    # serigrafia (interrotta dove passano i pad) sia su F.Fab, intero (serve
    # al disegno di assemblaggio e non va in produzione).
    for graphic in silk_rectangle((-half_length, -half_width), (half_length, half_width), keepouts):
        fp.add_graphic(graphic)
    fp.add_graphic(FpRect(
        start=(-half_length, -half_width),
        end=(half_length, half_width),
        layer="F.Fab",
    ))

    # Courtyard: ingombro reale, corpo piu' i pad che sporgono dal bordo.
    lands = placements + [pad for pad in stadiums if pad is not None]
    extent_x = max([half_length] + [abs(p.x) + p.width / 2 for p in lands])
    extent_y = max([half_width] + [abs(p.y) + p.height / 2 for p in lands])
    fp.add_graphic(FpRect(
        start=(-extent_x - params.margin, -extent_y - params.margin),
        end=(extent_x + params.margin, extent_y + params.margin),
        layer="F.CrtYd",
        width=0.05,
    ))

    # Riferimento al centro del corpo su F.Fab (KLC F5.2): la serigrafia
    # finisce coperta a modulo montato, F.Fab resta leggibile nei disegni
    # di assemblaggio.
    fp.add_graphic(FpText(kind="user", text="${REFERENCE}", at=(0, 0), layer="F.Fab"))

    # Il marcatore indica il pad numerato "1" (o il numero piu' basso), non il
    # primo pad prodotto: con reverse=True o con i lati elencati a partire da
    # destra il primo prodotto e' un altro pin.
    first_number = pin_one(placement.number for placement in placements)
    for placement, stadium in zip(placements, stadiums):
        if stadium is not None:
            # UN solo pad per contatto, come sulla scheda reale: rame continuo
            # dal bordo fino oltre la corona, foro passante decentrato dentro.
            # KiCad: `at` e' il FORO, l'offset sposta il RAME (verificato con
            # pcbnew 10). Scrivere il centro del rame in `at` scambiava foro e
            # rame sulla scheda.
            fp.add_pad(OffsetDrillPad(
                number=placement.number,
                pad_type="thru_hole",
                shape="oval",
                at=(stadium.drill_x, stadium.drill_y),
                size=(stadium.width, stadium.height),
                layers=["*.Cu", "*.Mask", "F.Paste"],
                drill=params.thru_hole_drill,
                drill_offset=stadium.drill_offset,
            ))
        elif placement.style != CONTACT_THROUGH_HOLE:
            fp.add_pad(Pad(
                number=placement.number,
                pad_type="smd",
                shape="roundrect",
                at=(placement.x, placement.y),
                size=(placement.width, placement.height),
                layers=["F.Cu", "F.Paste", "F.Mask"],
                roundrect_rratio=0.25,
            ))
        if placement.style == CONTACT_THROUGH_HOLE:
            fp.add_pad(Pad(
                number=placement.number,
                pad_type="thru_hole",
                shape="circle",
                at=params.thru_hole_centre(placement),
                size=(params.thru_pad_diameter, params.thru_pad_diameter),
                layers=["*.Cu", "*.Mask"],
                drill=params.thru_hole_drill,
            ))
        if placement.number == first_number:
            first_number = None   # un solo marcatore anche se il numero si ripete
            # Marcatore del pin 1, affiancato al suo pad lungo il bordo
            # (non verso l'esterno, dove finirebbe sotto il pad stesso).
            # Distanza misurata dal rame vero: la piazzola continua puo'
            # essere piu' larga della piazzola di bordo (contiene la corona).
            # Il lato scelto e' quello opposto agli altri pad della stessa
            # fila, cosi' il marcatore non finisce fra il pin 1 e il suo vicino
            # (numerazione inversa); con un pad solo resta verso l'inizio.
            half_along = (stadium.across / 2 if stadium is not None
                          else (placement.height if placement.edge in ("left", "right")
                                else placement.width) / 2)
            vertical = placement.edge in ("left", "right")
            along = placement.y if vertical else placement.x
            row = [(p.y if vertical else p.x) for p in placements
                   if p.edge == placement.edge and p.number != placement.number]
            sign = 1.0 if row and sum(row) / len(row) < along else -1.0
            offset = sign * (half_along + 0.5)
            if vertical:
                center, direction = (placement.x, placement.y + offset), (0.0, sign)
            else:
                center, direction = (placement.x + offset, placement.y), (sign, 0.0)
            # Il disco e' pieno: la distanza si misura dal suo bordo, senza la
            # mezza larghezza del tratto usata per il contorno.
            marker_keepouts = [keepout_box(*_copper_outline_bbox(params, p), line_width=0.0)
                               for p in placements]
            fp.add_graphic(pin_one_marker(center, marker_keepouts, direction))

        # Sigla del contatto (PE, B-, A+, GND...) su F.Fab, oltre il bordo
        # interno del pad: leggibile nel disegno di assemblaggio senza
        # dover consultare a parte la tabella pinout. Solo se l'estrazione
        # (o l'utente) l'ha fornita per questo numero di pin.
        label = params.silk_labels.get(placement.number, "").strip()
        if label:
            gap = 0.6
            if stadium is not None:
                # Oltre la fine interna del rame continuo, non sopra di esso.
                nx, ny = INWARD_FOOTPRINT[placement.edge]
                reach = (stadium.length / 2 + gap)
                label_at = (_clean(stadium.x + nx * reach), _clean(stadium.y + ny * reach))
            elif placement.edge == "left":
                label_at = (placement.x + placement.width / 2 + gap, placement.y)
            elif placement.edge == "right":
                label_at = (placement.x - placement.width / 2 - gap, placement.y)
            elif placement.edge == "top":
                label_at = (placement.x, placement.y + placement.height / 2 + gap)
            else:
                label_at = (placement.x, placement.y - placement.height / 2 - gap)
            fp.add_graphic(FpText(
                kind="user", text=label, at=label_at, layer="F.Fab",
                size=0.8, thickness=0.12,
            ))

    if fp.attr == "smd" and any(pad.pad_type == "thru_hole" for pad in fp.pads):
        # Il criterio e' la combinazione che KiCad segnala (tipo SMD con pad
        # forati), non il tipo di contatto: vale per le piazzole a stadio e
        # per un modulo che mescola lati half_hole e through_hole. L'estensione
        # ai moduli misti e' confermata dall'utente (2026-09-24, scelta «A»):
        # anche quelli si posano e si saldano a rifusione, senza pin nei fori.
        fp.description = f"{fp.description} {SMD_WITH_DRILLED_PADS_NOTE}".strip()
    return fp
