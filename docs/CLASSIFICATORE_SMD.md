# Classificatore SMD locale

Rete neurale locale che classifica i corpi SMD tenuti nella revisione dei
rilevamenti, senza chiamate remote. È il rilevatore predefinito avviando il
programma dai sorgenti (**Rilevamento foto → YOLO + rete SMD locale (v3)**);
nel programma Windows installato la voce è disabilitata, con il motivo nel
suggerimento, perché il runtime PyTorch e il checkpoint non sono inclusi.

L'unico file necessario è `models/smd/smd_classifier.pt` (v3, prodotto dal progetto di
addestramento, non distribuito con i sorgenti). La funzione
`kicadgen.services.smd_component_classifier.classify_photo_boxes`
ricostruisce la rete ResNet18 dalle informazioni e dai pesi contenuti nel
checkpoint: classi e orientamenti sono già nel file, non serve il progetto di
addestramento né il suo catalogo. Footprint e STEP vengono cercati dal
sistema già presente in KiCadGen (`kicadgen.cad3d.kicad_models`), leggendo le
librerie KiCad installate; alternative e scelte da rivedere restano esplicite
nel risultato.

## Runtime

Nella cartella del repository:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements/smd.txt
```

Se il runtime manca, il programma lo dice con il comando da eseguire. Il
rilevatore YOLO di questa modalità usa, se il campo **Modello YOLO** è vuoto,
`models/yolo/pcb_component_detector_yolov8m_best.pt` (lo stesso verificato
nel progetto di origine) e richiede quindi anche `requirements/yolo-pt.txt`;
un modello scelto esplicitamente ha precedenza.

I pesi (`models/smd/smd_classifier.pt` e
`models/yolo/pcb_component_detector_yolov8m_best.pt`) **non sono distribuiti
con i sorgenti** e un checkout nuovo non ha le cartelle `models/yolo/` e
`models/smd/`: vanno create quando si aggiungono i file. Poiché questa è la
modalità predefinita dai sorgenti, senza pesi o senza i pacchetti di
`requirements/smd.txt` e `requirements/yolo-pt.txt` l'analisi di una scheda
si ferma con un avviso prima di qualunque chiamata a pagamento (controlli
`validate_yolo_setup` e `validate_smd_setup`). Per lavorare senza questi pesi
basta scegliere un'altra voce in **Rilevamento foto**, nel riquadro
**Strumenti AI** (per esempio *AI (attuale)*).

## Uso nella GUI

1. Tipologia **Scheda con componenti**, foto con il ruolo **Alta
   risoluzione**, rilevatore **YOLO + rete SMD locale (v3)**. La riga
   **Modello rete SMD** mostra il checkpoint in uso; il pulsante accanto apre
   **Scegli la rete SMD**: a sinistra i file `.pt` di `models/smd` (più quello
   in uso e i file aggiunti con «Altro file…»), a destra dimensione, data,
   SHA-256 e ciò che il checkpoint dichiara di sé (architettura, data ed
   epoche di addestramento, classi, tipi di rotazione, soglia di
   accettazione, metriche di validazione). Il checkpoint è letto con
   `weights_only=True`: nessun codice del file viene eseguito.
2. **Estrai parametri con AI**. Dopo la revisione dei riquadri del
   rilevatore (prima fase della finestra di revisione), la rete classifica i
   corpi tenuti — fase **Rete SMD locale** nella finestra di avanzamento — e il
   programma cerca footprint e STEP nella libreria KiCad.
3. Solo i corpi che la rete **non** risolve (classe `_other` o probabilità
   sotto la soglia del checkpoint) passano alla **Lettura ritagli** AI usata
   dagli altri percorsi (`photo_crop_reading`: due letture per corpo, si
   adotta solo ciò su cui concordano); la finestra di avanzamento mostra il
   numero dei corpi rimandati. I corpi risolti non generano chiamate. Un
   errore della rete ad alta confidenza NON viene rimandato all'AI: va corretto
   nella revisione.
4. Nella finestra di revisione, **Parametri…** mostra per ogni corpo classe,
   confidenza, rotazione KiCad proposta e stato del footprint; la riga
   **Esito rete SMD** dice chi ha deciso il tipo. Le risposte `_other` o sotto
   soglia restano corpi generici da rivedere. Le altezze restano stime: la
   rete non le misura.

L'analisi iniziale dei documenti continua a usare il modello AI
configurato. La rotazione KiCad della rete viene conservata separatamente
dall'angolo dell'ingombro e non forza l'orientamento del STEP nativo, che può
avere una trasformazione diversa nel footprint e va confermato nel selettore
dei modelli (`interactive-kicad-models.md`).

## Uso da riga di comando

Questo esempio usa il YOLO locale, le quote della scheda RS485 di esempio
(42,8 × 15,2 mm) e la funzione unica foto + riquadri. Il callback di
`prepare_photo` conserva la foto completa nel sistema di coordinate del PCB
rilevato, così il margine dei componenti vicini al bordo non viene tagliato.

Dalla radice del repository:

```powershell
@'
import json
from pathlib import Path
from kicadgen.services.photo_inventory import prepare_photo
from kicadgen.services.photo_yolo import detect_yolo_components
from kicadgen.services.smd_component_classifier import classify_photo_boxes

frame = {}
def remember_frame(photo, box):
    frame.update(photo=photo, box=box)

length_mm, width_mm = 42.8, 15.2
board = prepare_photo(
    Path('docs/RS485/AltaRisoluzione.jpg'),
    length_mm, width_mm, frame_callback=remember_frame,
)
if board is None:
    raise SystemExit('Foto non calibrabile')
detections = detect_yolo_components(
    board, length_mm, width_mm,
    model_path=Path('models/yolo/pcb_component_detector_yolov8m_best.pt'),
)
results = classify_photo_boxes(
    frame['photo'],
    [item['_box'] for item in detections],
    box_format='normalized',
    box_frame_px=frame['box'],
    mm_per_pixel=(length_mm / board.shape[1], width_mm / board.shape[0]),
)
print(json.dumps(results, ensure_ascii=False, indent=2))
'@ | .\.venv\Scripts\python.exe -
```

Per un'altra scheda si usano le sue quote e la sua foto.

## Ritagli e risultati

La rete riceve il riquadro con margine del 10 % per lato, mantenendo i pixel
e le proporzioni della foto. Il ritaglio viene ridimensionato con
interpolazione bicubica in un quadrato 224 × 224, con riempimento RGB
(114, 114, 114), poi normalizzato con media e deviazione ImageNet. La TTA usa
quattro rotazioni (0, 90, 180, 270 gradi); le probabilità della rotazione
vengono riportate all'orientamento iniziale prima della media.

I ritagli context/detail preparati per l'AI hanno margini, correzioni della
scala e annotazioni diversi: alla rete SMD si passano la foto completa e i
riquadri, lasciando la preparazione a `classify_photo_boxes`.

`box_frame_px` è il riquadro del PCB nella foto completa. Quando viene
passato, i box in ingresso si riferiscono a quel PCB e i box restituiti in
`box_px` alla foto completa; il margine viene limitato dai bordi della foto.
L'eventuale raddrizzamento del PCB fatto da KiCadGen è condiviso tra
rilevatore e classificatore; la funzione non aggiunge correzioni delle
proporzioni.

Ogni risultato contiene `class`, `kind`, `class_confidence`, `rotation_deg`,
`size_mm`, `footprint`, `footprint_candidates`, `step_model`,
`step_candidates` e `model_match`. La rotazione è antioraria nella
convenzione KiCad. `_other` e le risposte sotto la soglia (almeno 0,5) non
ricevono footprint o rotazione. Senza `mm_per_pixel` si restituiscono solo
classificazione e rotazione. L'abbinamento dimensionale conserva gli stati di
revisione del programma; un modello STEP condiviso da più footprint lascia i
footprint come alternative.

## Identità dei pesi

SHA-256 del file SMD v3 copiato:
`3CEEEADC9B03C2CC04E0F59FF04FAE6ADAD9EF11BA021CBFA0DBAE89D0FE1A4B`.

Il peso YOLO `pcb_component_detector_yolov8m_best.pt` usato qui è identico a
quello prodotto dall'addestramento da cui è stato copiato:
`27AF3B3D1E17251B124C9DE860B024833A46F77DB04AB8767FC2554A3B3A4607`.

Per ripetere il controllo, dalla radice del repository (per confrontarli con
gli originali, aggiungere alla lista i percorsi dei file prodotti
dall'addestramento):

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath 'models\smd\smd_classifier.pt', 'models\yolo\pcb_component_detector_yolov8m_best.pt' | Format-List Path, Hash
```

Confronto effettuato su 65 riquadri delle schede di prova originali: pixel
dei ritagli, letterbox, tensori normalizzati, classi, rotazioni e probabilità
sono identici fra il classificatore originale e questa funzione. Il controllo
non implica che due configurazioni diverse di YOLO producano gli stessi box.

Le regressioni locali:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_smd_component_classifier.py -q
```
