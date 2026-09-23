"""Generatore parametrico: modulo con piazzole a mezzo foro (castellated).

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
  a modulo montato.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, NamedTuple, Optional, Sequence

from ..core.footprint import Footprint, FpCircle, FpRect, FpText, Pad

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
#: passante poco piu' dentro. E' il comportamento storico del generatore.
CONTACT_HALF_AND_THROUGH = "half_and_through"
#: Valori ammessi per `CastellatedSide.contact_style`. Quale sia quello giusto
#: si vede nella fotografia (il bordo taglia il foro oppure no) e va chiesto al
#: modello, non dedotto da una soglia dimensionale: vedi datasheet_extractor.
CONTACT_STYLES = (CONTACT_HALF_HOLE, CONTACT_THROUGH_HOLE, CONTACT_HALF_AND_THROUGH)

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


@dataclass
class ComponentBox:
    """Un componente montato sul modulo, approssimato a parallelepipedo.

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
        """@brief Provide the is usable operation for this workflow."""
        return self.length > 0 and self.width > 0 and self.height > 0


@dataclass
class CastellatedSide:
    """I contatti su un lato del modulo.

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
        """Numeri dei pad nell'ordine in cui si incontrano lungo il bordo."""
        if self.pin_numbers is not None:
            return [str(n) for n in self.pin_numbers]
        numbers = [str(self.first_pin + i) for i in range(self.count)]
        return list(reversed(numbers)) if self.reverse else numbers


@dataclass
class CastellatedModuleParams:
    """Parametri di un modulo castellated: corpo + contatti per lato."""

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
        """@brief Provide the side pad length operation for this workflow."""
        return side.pad_length if side.pad_length is not None else self.pad_length

    def side_pad_width(self, side: CastellatedSide) -> float:
        """@brief Provide the side pad width operation for this workflow."""
        return side.pad_width if side.pad_width is not None else self.pad_width

    def edge_length(self, edge: str) -> float:
        """Lunghezza del bordo lungo cui si distribuiscono i pad."""
        return self.body_width if edge in ("left", "right") else self.body_length

    def validate(self) -> None:
        """@brief Provide the validate operation for this workflow."""
        if not self.name.strip():
            raise ValueError("Il nome del footprint non puo' essere vuoto.")
        if self.body_length <= 0 or self.body_width <= 0:
            raise ValueError("Le dimensioni del corpo devono essere > 0.")
        if not self.sides:
            raise ValueError("Serve almeno un lato con dei contatti.")
        # Un valore sconosciuto si rifiuta, non si rimappa: indovinare quale
        # dei tre si intendesse sarebbe esattamente la deduzione che il campo
        # esiste per evitare.
        for side in self.sides:
            if side.contact_style not in CONTACT_STYLES:
                raise ValueError(
                    f"Lato {EDGE_LABELS.get(side.edge, side.edge)}: tipo di contatto "
                    f"{side.contact_style!r} non riconosciuto (ammessi: "
                    f"{', '.join(CONTACT_STYLES)})."
                )
        # Il semi-foro serve solo ai lati che lo tagliano davvero.
        if self.needs_half_holes() and self.hole_diameter <= 0:
            raise ValueError("Il diametro del semi-foro deve essere > 0.")
        if self.pcb_thickness <= 0:
            raise ValueError("Lo spessore del PCB deve essere > 0.")
        if self.thru_hole_drill < 0 or self.thru_pad_diameter < 0:
            raise ValueError("Le quote del foro passante non possono essere negative.")
        self._validate_through_hole_sides()
        # Il controllo storico del foro dietro al semi-foro vale solo dove
        # entrambi esistono: su un lato through_hole non c'e' semi-foro con cui
        # fondersi, su un lato half_hole non c'e' foro.
        if self.has_thru_holes and any(side.contact_style == CONTACT_HALF_AND_THROUGH
                                       for side in self.sides):
            if self.thru_pad_diameter <= self.thru_hole_drill:
                raise ValueError(
                    f"La piazzola del foro passante ({self.thru_pad_diameter}mm) deve essere "
                    f"piu' larga del foro ({self.thru_hole_drill}mm): senza corona non c'e' "
                    "rame su cui saldare."
                )
            # Un foro il cui bordo esce dal corpo non e' un foro chiuso: e' un
            # secondo semi-foro, e allora tanto vale non dichiararlo.
            gap = (self.thru_hole_centre_offset - self.half_hole_inset
                   - self.hole_diameter / 2 - self.thru_hole_drill / 2)
            if gap < 0:
                minimum = (self.half_hole_inset + self.hole_diameter / 2
                           + self.thru_hole_drill / 2 + MIN_HOLE_WALL_MM)
                raise ValueError(
                    f"Il foro passante e il semi-foro si toccano: i loro assi distano "
                    f"{self.thru_hole_centre_offset - self.half_hole_inset:.2f}mm ma la somma "
                    f"dei raggi e' {self.hole_diameter / 2 + self.thru_hole_drill / 2:.2f}mm, "
                    f"quindi si fonderebbero in un'unica cavita' aperta sul bordo invece di "
                    f"restare due fori. Porta la distanza dal bordo ad almeno "
                    f"{minimum:.2f}mm, oppure riduci il semi-foro."
                )
            if self.thru_hole_centre_offset < self.thru_pad_diameter / 2:
                raise ValueError(
                    f"Il centro del foro passante dista {self.thru_hole_centre_offset}mm dal bordo, "
                    f"meno del raggio della sua piazzola ({self.thru_pad_diameter / 2}mm): "
                    "resterebbe aperto sul bordo invece di essere chiuso."
                )
            shortest = min(self.body_length, self.body_width)
            if 2 * self.thru_hole_centre_offset + self.thru_pad_diameter > shortest + 1e-6:
                raise ValueError(
                    f"I fori passanti dei due lati opposti non stanno in {shortest}mm: "
                    f"servono almeno {2 * self.thru_hole_centre_offset + self.thru_pad_diameter:.2f}mm."
                )

        seen_numbers: dict[str, str] = {}
        for side in self.sides:
            label = EDGE_LABELS.get(side.edge, side.edge)
            if side.edge not in EDGES:
                raise ValueError(
                    f"Lato non valido: {side.edge!r} (attesi: {', '.join(EDGES)})."
                )
            if side.count < 1:
                raise ValueError(f"Lato {label}: servono almeno 1 pad.")
            if side.pitch <= 0 and side.count > 1:
                raise ValueError(f"Lato {label}: il passo deve essere > 0.")

            pad_length = self.side_pad_length(side)
            pad_width = self.side_pad_width(side)
            if pad_length <= 0 or pad_width <= 0:
                raise ValueError(f"Lato {label}: le dimensioni dei pad devono essere > 0.")
            # La piazzola che si allinea lungo il bordo e' quella che il lato
            # offre davvero: la corona del foro per through_hole, la piazzola
            # rettangolare di bordo per gli altri due tipi.
            if side.contact_style == CONTACT_THROUGH_HOLE:
                land = self.thru_pad_diameter
                if side.count > 1 and side.pitch < land:
                    raise ValueError(
                        f"Lato {label}: passo {side.pitch}mm minore del diametro della "
                        f"corona {land}mm -- le piazzole dei fori passanti si "
                        "sovrapporrebbero."
                    )
            else:
                land = pad_width
                if side.count > 1 and side.pitch < pad_width:
                    raise ValueError(
                        f"Lato {label}: passo {side.pitch}mm minore della larghezza pad "
                        f"{pad_width}mm -- i pad si sovrapporrebbero."
                    )

            span = (side.count - 1) * side.pitch + land
            available = self.edge_length(side.edge)
            if span - 1e-6 > available:
                raise ValueError(
                    f"Lato {label}: i {side.count} pad occupano {span:.2f}mm ma il "
                    f"bordo e' lungo {available:.2f}mm."
                )

            numbers = side.numbers()
            if len(numbers) != side.count:
                raise ValueError(
                    f"Lato {label}: {len(numbers)} numeri di pin per {side.count} pad."
                )
            for number in numbers:
                if number in seen_numbers:
                    raise ValueError(
                        f"Il pin {number} compare sia sul lato "
                        f"{seen_numbers[number]} sia su quello {label}."
                    )
                seen_numbers[number] = label


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
                f"Lato {labels}: i contatti sono fori passanti ma il diametro del foro e' "
                "zero. Indica il foro, oppure scegli il tipo «mezzo foro sul bordo»."
            )
        if self.thru_pad_diameter <= self.thru_hole_drill:
            raise ValueError(
                f"Lato {labels}: la piazzola del foro passante ({self.thru_pad_diameter}mm) "
                f"deve essere piu' larga del foro ({self.thru_hole_drill}mm): senza corona "
                "non c'e' rame su cui saldare."
            )
        spacing = self.row_spacing
        if spacing is not None and (
                isinstance(spacing, bool) or not isinstance(spacing, (int, float))
                or not math.isfinite(spacing) or spacing <= 0):
            raise ValueError(
                f"La distanza fra le file ({self.row_spacing!r}) deve essere un numero > 0."
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
                    why = (f"le file dichiarate distano {self.row_spacing}mm su un corpo di "
                           f"{self.edge_extent(side.edge)}mm, quindi i fori starebbero a "
                           f"{inset}mm dal bordo")
                else:
                    why = f"il centro del foro dista {inset}mm dal bordo"
                raise ValueError(
                    f"Lato {label}: {why}, meno del raggio della corona ({radius}mm): "
                    "il foro resterebbe aperto sul bordo, cioe' sarebbe un semi-foro e "
                    "non un foro passante."
                )
            extent = self.edge_extent(side.edge)
            opposite = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}[side.edge]
            facing = any(other.edge == opposite for other in self.sides)
            if facing and 2 * inset + self.thru_pad_diameter > extent + 1e-6:
                raise ValueError(
                    f"Lato {label}: le corone dei fori passanti di due file opposte non "
                    f"stanno in {extent}mm: servono almeno "
                    f"{2 * inset + self.thru_pad_diameter:.2f}mm."
                )
            if inset + radius > extent + 1e-6:
                raise ValueError(
                    f"Lato {label}: il foro passante a {inset}mm dal bordo esce dal lato "
                    f"opposto del corpo ({extent}mm)."
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


def build_castellated_module(params: CastellatedModuleParams) -> Footprint:
    """@brief Build the KiCad footprint of the module described by `params`.

    @param params Module description; validated first.
    @return Footprint with, per contact, exactly the lands the part offers:
        an SMD edge pad for half_hole, an SMD edge pad plus a THT pad for
        half_and_through (as before), a THT pad only for through_hole.
    @throws ValueError When `params` does not validate.
    @note A through_hole side emits no edge pad: a board without
        castellations cannot be reflowed onto edge pads, so such a footprint
        could not be soldered to the part. The footprint attribute follows the
        lands: "smd" when any SMD pad exists (unchanged for existing modules),
        "through_hole" when every contact is a through-hole.
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

    # Contorno del corpo: e' il modulo vero e proprio, quindi va sia in
    # serigrafia sia su F.Fab (dove serve al disegno di assemblaggio).
    for layer in ("F.SilkS", "F.Fab"):
        fp.add_graphic(FpRect(
            start=(-half_length, -half_width),
            end=(half_length, half_width),
            layer=layer,
        ))

    placements = list(pad_positions(params))

    # Courtyard: ingombro reale, corpo piu' i pad che sporgono dal bordo.
    extent_x = max([half_length] + [abs(p.x) + p.width / 2 for p in placements])
    extent_y = max([half_width] + [abs(p.y) + p.height / 2 for p in placements])
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

    first_number = placements[0].number if placements else None
    for placement in placements:
        if placement.style != CONTACT_THROUGH_HOLE:
            fp.add_pad(Pad(
                number=placement.number,
                pad_type="smd",
                shape="roundrect",
                at=(placement.x, placement.y),
                size=(placement.width, placement.height),
                layers=["F.Cu", "F.Paste", "F.Mask"],
                roundrect_rratio=0.25,
            ))
        if placement.style == CONTACT_THROUGH_HOLE or (
                placement.style == CONTACT_HALF_AND_THROUGH and params.has_thru_holes):
            # Stesso numero del pad SMD: sono due appigli dello stesso
            # contatto, non due contatti. KiCad li tratta come una sola rete.
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
            # Marcatore del pin 1, affiancato al suo pad lungo il bordo
            # (non verso l'esterno, dove finirebbe sotto il pad stesso).
            if placement.edge in ("left", "right"):
                center = (placement.x, placement.y - placement.height / 2 - 0.5)
            else:
                center = (placement.x - placement.width / 2 - 0.5, placement.y)
            fp.add_graphic(FpCircle(
                center=center, radius=0.25, layer="F.SilkS", fill=True,
            ))

        # Sigla del contatto (PE, B-, A+, GND...) su F.Fab, oltre il bordo
        # interno del pad: leggibile nel disegno di assemblaggio senza
        # dover consultare a parte la tabella pinout. Solo se l'estrazione
        # (o l'utente) l'ha fornita per questo numero di pin.
        label = params.silk_labels.get(placement.number, "").strip()
        if label:
            gap = 0.6
            if placement.edge == "left":
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

    return fp
