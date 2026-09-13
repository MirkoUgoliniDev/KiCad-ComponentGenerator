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

from dataclasses import dataclass, field
from typing import Dict, Iterator, List, NamedTuple, Optional, Sequence

from ..core.footprint import Footprint, FpCircle, FpRect, FpText, Pad

#: Lati del modulo su cui possono stare i contatti.
EDGES = ("left", "right", "top", "bottom")

#: Etichette leggibili (GUI, messaggi d'errore).
EDGE_LABELS = {
    "left": "sinistro",
    "right": "destro",
    "top": "superiore",
    "bottom": "inferiore",
}


class PadPlacement(NamedTuple):
    """Un pad piazzato: numero, centro e dimensioni gia' orientate."""

    number: str
    x: float
    y: float
    width: float
    height: float
    edge: str


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

    def is_usable(self) -> bool:
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
    # Componenti letti dalla foto del modulo: se ci sono, il 3D li disegna
    # uno per uno invece del blocco unico ricavato da component_inset.
    components: List[ComponentBox] = field(default_factory=list)
    margin: float = 0.25

    def side_pad_length(self, side: CastellatedSide) -> float:
        return side.pad_length if side.pad_length is not None else self.pad_length

    def side_pad_width(self, side: CastellatedSide) -> float:
        return side.pad_width if side.pad_width is not None else self.pad_width

    def edge_length(self, edge: str) -> float:
        """Lunghezza del bordo lungo cui si distribuiscono i pad."""
        return self.body_width if edge in ("left", "right") else self.body_length

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("Il nome del footprint non puo' essere vuoto.")
        if self.body_length <= 0 or self.body_width <= 0:
            raise ValueError("Le dimensioni del corpo devono essere > 0.")
        if not self.sides:
            raise ValueError("Serve almeno un lato con dei contatti.")
        if self.hole_diameter <= 0:
            raise ValueError("Il diametro del semi-foro deve essere > 0.")
        if self.pcb_thickness <= 0:
            raise ValueError("Lo spessore del PCB deve essere > 0.")

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
            if side.count > 1 and side.pitch < pad_width:
                raise ValueError(
                    f"Lato {label}: passo {side.pitch}mm minore della larghezza pad "
                    f"{pad_width}mm -- i pad si sovrapporrebbero."
                )

            span = (side.count - 1) * side.pitch + pad_width
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


def pad_positions(params: CastellatedModuleParams) -> Iterator[PadPlacement]:
    """Genera i pad di tutti i lati, gia' posizionati e orientati.

    Unica fonte di verita' della geometria: la usano sia il footprint 2D
    sia l'anteprima, cosi' non esistono due calcoli da tenere allineati.
    """
    half_length = params.body_length / 2
    half_width = params.body_width / 2

    for side in params.sides:
        pad_length = params.side_pad_length(side)
        pad_width = params.side_pad_width(side)

        # Coordinata lungo il bordo: gruppo centrato, poi spostato di offset.
        start = -(side.count - 1) * side.pitch / 2 + side.offset
        # Coordinata perpendicolare: il pad sporge di pad_overhang oltre il
        # bordo, il resto della sua lunghezza entra sotto il modulo.
        outer = (half_length if side.edge in ("left", "right") else half_width)
        center_offset = outer + params.pad_overhang - pad_length / 2

        for index, number in enumerate(side.numbers()):
            along = start + index * side.pitch
            if side.edge == "left":
                yield PadPlacement(number, -center_offset, along, pad_length, pad_width, side.edge)
            elif side.edge == "right":
                yield PadPlacement(number, center_offset, along, pad_length, pad_width, side.edge)
            elif side.edge == "top":
                yield PadPlacement(number, along, -center_offset, pad_width, pad_length, side.edge)
            else:  # bottom
                yield PadPlacement(number, along, center_offset, pad_width, pad_length, side.edge)


def build_castellated_module(params: CastellatedModuleParams) -> Footprint:
    """Costruisce il Footprint SMD del modulo descritto da `params`."""
    params.validate()

    total_pads = sum(side.count for side in params.sides)
    fp = Footprint(
        name=params.name,
        description=params.description or (
            f"Modulo castellated {params.body_length}x{params.body_width}mm, "
            f"{total_pads} contatti su {len(params.sides)} lati, "
            "generato con kicadgen_tool"
        ),
        tags=params.tags,
        attr="smd",
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
        fp.add_pad(Pad(
            number=placement.number,
            pad_type="smd",
            shape="roundrect",
            at=(placement.x, placement.y),
            size=(placement.width, placement.height),
            layers=["F.Cu", "F.Paste", "F.Mask"],
            roundrect_rratio=0.25,
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
