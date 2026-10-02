# Classificatore SMD locale

L'unico file trasferito da TorchObjectDetection e' `models\smd_classifier.pt`
(v3). La funzione `classify_photo_boxes` ricostruisce la rete ResNet18 dalle
informazioni e dai pesi contenuti in questo checkpoint. Classi e orientamenti
sono gia' nel file; non serve il progetto di addestramento o il suo catalogo.

Footprint e STEP vengono cercati dal sistema gia' presente in KiCadGen,
`kicadgen.cad3d.kicad_models`. Le associazioni fra footprint e STEP vengono
lette dalle librerie KiCad installate. Alternative e scelte da rivedere
rimangono esplicite nel risultato.

## Runtime

Nella cartella KiCad-ComponentGenerator:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-smd.txt
```

Il runtime PyTorch e il checkpoint SMD non sono inclusi nell'attuale pacchetto
Windows della GUI: in quel pacchetto la modalita' SMD e' disabilitata e ne
viene indicato il motivo. Per usarla, avvia il programma dai sorgenti con il
checkpoint in `models/smd_classifier.pt` e il runtime indicato sopra.

## Uso con la foto RS485 di questo progetto

Questo comando usa il YOLO locale, le quote RS485 di 42,8 x 15,2 mm e la
funzione unica foto + riquadri. Il callback di `prepare_photo` conserva la
foto completa nel medesimo sistema di coordinate del PCB rilevato. Questo
evita di tagliare il margine dei componenti vicini al bordo della scheda.

```powershell
Set-Location 'C:\Lavori\Python\KiCad-ComponentGenerator'
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
    Path('generated/RS485/docs/AltaRisoluzione.jpg'),
    length_mm, width_mm, frame_callback=remember_frame,
)
if board is None:
    raise SystemExit('Foto non calibrabile')
detections = detect_yolo_components(
    board, length_mm, width_mm,
    model_path=Path('models/pcb_component_detector_yolov8m_best.pt'),
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

Le quote dell'esempio sono della RS485. Per un'altra scheda si applicano le
sue quote e la sua foto, usando la calibrazione gia' disponibile nel progetto.

## Ritagli e risultati

La rete riceve il riquadro con margine del 10% per lato, mantenendo i pixel e
le proporzioni della foto. Il ritaglio viene ridimensionato con interpolazione
bicubica in un quadrato 224 x 224, con riempimento RGB (114, 114, 114), poi
normalizzato con media e deviazione ImageNet. La TTA usa quattro rotazioni:
0, 90, 180 e 270 gradi. Le probabilita' della rotazione vengono riportate
all'orientamento iniziale prima della media.

I ritagli context/detail preparati per l'AI hanno margini, correzioni della
scala e annotazioni diversi: alla rete SMD si passa la foto completa e i
riquadri, lasciando la preparazione a `classify_photo_boxes`.

`box_frame_px` e' il riquadro del PCB nella foto completa. Quando viene passato,
i box in ingresso si riferiscono a quel PCB; i box restituiti in `box_px` si
riferiscono alla foto completa. Il margine viene limitato dai bordi della
foto completa, come in TorchObjectDetection. L'eventuale raddrizzamento del
PCB effettuato da KiCadGen e' condiviso tra rilevatore e classificatore;
la funzione non aggiunge correzioni delle proporzioni.

Ogni risultato contiene `class`, `kind`, `class_confidence`, `rotation_deg`,
`size_mm`, `footprint`, `footprint_candidates`, `step_model`, `step_candidates`
e `model_match`. La rotazione e' antioraria nella convenzione KiCad. `_other`
e le risposte sotto la soglia (almeno 0,5) non ricevono footprint o rotazione.
Senza `mm_per_pixel` si restituiscono soltanto classificazione e rotazione.
L'abbinamento dimensionale conserva gli stati di revisione del programma;
un modello STEP condiviso da piu' footprint lascia i footprint come alternative.

## Uso nella GUI

Dai sorgenti, **YOLO + rete SMD locale (v3)** e' la modalita' predefinita di
**Rilevamento foto** (nel binario frozen resta YOLO, perche' manca PyTorch). Se il campo
del modello YOLO e' vuoto, questa modalita' usa
`models/pcb_component_detector_yolov8m_best.pt`, lo stesso verificato nel
progetto TorchObjectDetection. Un modello scelto esplicitamente ha precedenza.
Le altre modalita' restano disponibili.

Dopo la revisione dei riquadri, la rete classifica i corpi tenuti e il programma
cerca footprint e STEP nella libreria KiCad. In **Parametri componente** trovi
classe, confidenza, rotazione KiCad proposta e stato del footprint. Le risposte
`_other` o sotto soglia restano corpi generici da rivedere. Le altezze restano
stime: la rete non le misura.

La classificazione dei ritagli non chiama un modello remoto. Solo i corpi che
la rete NON risolve (classe `_other` o probabilita' sotto la soglia del
checkpoint) passano alla lettura AI dei ritagli gia' usata dagli altri percorsi
(`photo_crop_reading.read_bodies`, due letture per corpo, si adotta solo cio'
su cui concordano); quelli risolti non generano chiamate. La modale mostra la
fase «Rete SMD locale» e, se serve, «Lettura ritagli» col numero dei corpi
rimandati; in **Parametri componente** la riga «Esito rete SMD» dice chi ha
deciso il tipo. Un errore della rete ad alta confidenza NON viene rimandato
all'AI. L'analisi iniziale dei documenti e delle immagini selezionate continua
a usare il modello AI configurato. La rotazione KiCad della rete viene conservata separatamente
dall'angolo dell'ingombro; non forza l'orientamento del STEP nativo, che puo'
avere una trasformazione diversa nel footprint e va confermato nel selettore.

## Identita' dei pesi

SHA-256 del file SMD v3 copiato:
`3CEEEADC9B03C2CC04E0F59FF04FAE6ADAD9EF11BA021CBFA0DBAE89D0FE1A4B`.

I due pesi YOLO `pcb_component_detector_yolov8m_best.pt` sono identici:
`27AF3B3D1E17251B124C9DE860B024833A46F77DB04AB8767FC2554A3B3A4607`.

Per ripetere il controllo:

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath 'C:\Lavori\Python\KiCad-ComponentGenerator\models\smd_classifier.pt', 'C:\Lavori\Python\TorchObjectDetection\models\smd_classifier.pt', 'C:\Lavori\Python\KiCad-ComponentGenerator\models\pcb_component_detector_yolov8m_best.pt', 'C:\Lavori\Python\TorchObjectDetection\models\pcb_component_detector_yolov8m_best.pt' | Format-List Path, Hash
```

Confronto effettuato su 65 riquadri delle schede di prova originali: pixel dei
ritagli, letterbox, tensori normalizzati, classi, rotazioni e probabilita' sono
identici fra il classificatore originale e questa funzione. Il controllo
non implica che due configurazioni diverse di YOLO producano gli stessi box.
La foto RS485 di KiCadGen conserva i pixel originali nella calibrazione;
il margine oltre il bordo del PCB e' mantenuto usando la foto completa.

Le regressioni locali si eseguono con:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_smd_component_classifier.py -q
```
