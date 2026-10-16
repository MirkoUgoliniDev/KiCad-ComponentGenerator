"""@brief Compare the same PCB crops and KiCad projections with the configured vision API.

Explicit --run-paid opt-in is required. Responses are cached by model, prompt,
inputs and schema. The final HTML embeds images so it can be shared alone.
"""
import argparse
import base64
import hashlib
import html
import json
from pathlib import Path
import sys

import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

PROMPT="""Compare a real PCB component photograph with orthographic top projections of KiCad models.
Image 1 is a 6 x 6 mm region centered on the TARGET component. Ignore neighbouring components.
Image 2 contains 3 candidate models, each at 0,90,180,270 degrees, row labels identify models.
All images use the same physical scale before display resizing. Allow mild photographic measurement
error, but do not resize away a large package mismatch. Compare body and VISIBLE lead arrangement.
A solder fillet/pad/lead of the same contact is ONE terminal; silkscreen and neighbouring pads are not leads.
First count visible target leads on each photo side. Then compare templates, especially the missing
central lead that distinguishes a 5-pin from a 6-pin package. Do not infer hidden terminals from a
candidate name. Body function and printed marking are not needed to compare package geometry.
Distinguish incomplete visibility from incompatible geometry. You may answer none or ambiguous;
never force a match. Symmetry can make absolute polarity/pin-1 orientation unresolved: list equivalent
angles, never invent a pin-1 landmark. These renders approximate material appearance using height.
Return concise reasons IN ITALIAN, grounded in visible geometry, and rank all three candidates.
Do not assume any reference has already been accepted. No OpenCV result is provided to you."""

TOOL={'type':'function','function':{'name':'compare_geometry','description':'Report visual package comparison, with uncertainty.',
'parameters':{'type':'object','properties':{
'visible_leads':{'type':'object','properties':{k:{'type':['integer','null']} for k in ('left','right','top','bottom')},'required':['left','right','top','bottom']},
'visibility':{'type':'string','enum':['complete_visible_perimeter','incomplete','uncertain']},
'best_model':{'type':'string','enum':['SOT-23','SOT-23-5','SOT-23-6','none','ambiguous']},
'confidence':{'type':'string','enum':['low','medium','high']},
'reason':{'type':'string'},
'ranking':{'type':'array','items':{'type':'object','properties':{
'model':{'type':'string','enum':['SOT-23','SOT-23-5','SOT-23-6']},
'compatible':{'type':'boolean'},'angles':{'type':'array','items':{'type':'integer','enum':[0,90,180,270]}},
'reason':{'type':'string'}},'required':['model','compatible','angles','reason']}}},
'required':['visible_leads','visibility','best_model','confidence','reason','ranking']}}}


def reference_sheet(folder):
    """@brief Create labelled rotations at the photo pixel scale, without adjusting candidates.

    @param folder Folder holding the `<model>-<angle>.png` renders and
        receiving `references.png`.
    @return Path of the written reference sheet.
    """
    sheet=np.full((3*(384+45),4*384,3),240,np.uint8)
    for row,model in enumerate(('SOT-23','SOT-23-5','SOT-23-6')):
        for col,angle in enumerate((0,90,180,270)):
            y=row*429
            cv2.putText(sheet,f'{model} / {angle} deg',(col*384+10,y+29),cv2.FONT_HERSHEY_SIMPLEX,.7,(20,20,20),2)
            sheet[y+45:y+429,col*384:(col+1)*384]=cv2.imread(str(folder/f'{model}-{angle}.png'))
    target=folder/'references.png'
    cv2.imwrite(str(target),sheet)
    return target


def embedded(path):
    """@brief Embed a PNG into a portable HTML document.

    @param path Path of the PNG file.
    @return `data:image/png;base64,...` URI of the file content.
    """
    return 'data:image/png;base64,'+base64.b64encode(path.read_bytes()).decode('ascii')


def main():
    """@brief Run at most one paid request for each uncached crop and build the comparison page.

    @throws SystemExit When a crop has no cached answer and `--run-paid`
        was not given.
    """
    parser=argparse.ArgumentParser()
    parser.add_argument('--folder',type=Path,required=True)
    parser.add_argument('--model',default='deepseek-api/deepseek-flash')
    parser.add_argument('--run-paid',action='store_true')
    args=parser.parse_args()
    from kicadgen.services.openrouter import call_tool,usage_totals,OpenRouterError
    from kicadgen.services.photo_inventory import image_block
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT/'.env')
    except ImportError:
        pass
    data=json.loads((args.folder/'results.json').read_text())
    references=reference_sheet(args.folder)
    cache=args.folder/'ai-cache';cache.mkdir(exist_ok=True)
    outcomes=[]
    for result in data['results']:
        cid=result['component'].split()[0]
        photo=args.folder/f'{cid}-photo.png'
        request_hash=hashlib.sha256((args.model+PROMPT+json.dumps(TOOL,sort_keys=True)).encode()+photo.read_bytes()+references.read_bytes()).hexdigest()
        target=cache/f'{request_hash}.json'
        if target.exists():
            answer=json.loads(target.read_text(encoding='utf-8'))
        elif args.run_paid:
            print('AI start',cid,flush=True)
            try:
                answer=call_tool(model=args.model,system_prompt=PROMPT,
                                 content_blocks=[{'type':'text','text':'Image 1: target at photo center; Image 2: labelled candidate projections. Compare the geometry.'},
                                                 image_block(photo.read_bytes()),image_block(references.read_bytes())],
                                 tool=TOOL,max_tokens=2400,has_pdf=False)
            except OpenRouterError as exc:
                answer=dict(best_model='errore risposta',confidence='non disponibile',visible_leads={},
                            visibility='non disponibile',reason=str(exc),ranking=[],valid_response=False)
            target.write_text(json.dumps(answer,ensure_ascii=False,indent=2),encoding='utf-8')
        else:
            raise SystemExit('Missing cache; --run-paid required.')
        counts=answer.get('visible_leads',{})
        expected={'SOT-23':3,'SOT-23-5':5,'SOT-23-6':6}.get(answer.get('best_model'))
        values=list(counts.values())
        consistency=('Il conteggio dichiarato dall?AI non coincide con i terminali del modello scelto.'
                     if expected and len(values)==4 and all(type(v) is int for v in values)
                     and sum(values)!=expected else '')
        outcomes.append(dict(component=result['component'],opencv=result,ai=answer,consistency_warning=consistency))
        print(cid,answer['best_model'],answer['confidence'],answer['visible_leads'],flush=True)
    usage=vars(usage_totals())
    prior=args.folder/'comparison-ai-opencv.json'
    if not usage['calls'] and prior.exists():
        usage=json.loads(prior.read_text(encoding='utf-8')).get('usage_this_run',usage)
    output={'model':args.model,'usage_this_run':usage,'results':outcomes}
    (args.folder/'comparison-ai-opencv.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    doc=['<!doctype html><html lang="it"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PCB e KiCad ? OpenCV / AI</title><style>body{background:#181c28;color:#eef2fc;font:16px system-ui;max-width:1500px;margin:30px auto;padding:0 24px}h1,h2{color:#9ce2d4}section{background:#242b3c;padding:20px;margin:24px 0;border-radius:14px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}img{width:100%;border-radius:8px}td,th{padding:12px;text-align:left;border-bottom:1px solid #46516a}table{width:100%;border-collapse:collapse}.note{color:#b7c8e1}strong{color:#ffd49a}details{margin:16px 0}@media(max-width:850px){.grid{grid-template-columns:repeat(2,1fr)}}</style>',
         '<h1>Ritagli PCB contro modelli KiCad</h1><p>Esperimento su 6 componenti: OpenCV e AI ricevono le stesse foto e le stesse proiezioni dei modelli. Nessuna assegnazione del progetto o voce dei consolidati viene modificata.</p>',
         '<p class="note">OpenCV confronta metallo, corpo e bordi; AI confronta visivamente foto e riferimenti senza conoscere la classifica OpenCV. Il punteggio OpenCV non ? una probabilit?. La confidenza AI ? autodichiarata. Accordo fra metodi non equivale a verifica.</p>',
         f'<p>Modello AI: <strong>{html.escape(args.model)}</strong>. Costo parziale delle nuove chiamate di questa esecuzione (escluse le prove precedenti): {output["usage_this_run"]["cost_usd"]:.6f} USD; chiamate: {output["usage_this_run"]["calls"]}.</p>',
         '<table><tr><th>Componente</th><th>OpenCV: primo candidato</th><th>AI</th><th>Confronto</th></tr>']
    for row in outcomes:
        cv=row['opencv']['ranked'][0];ai=row['ai']
        doc.append(f'<tr><td><a href="#{row["component"].split()[0]}" style="color:#9ce2d4">{html.escape(row["component"])}</a></td><td>{cv["model"]} ? {cv["score"]:.3f}</td><td>{html.escape(ai["best_model"])} ? {html.escape(ai["confidence"])}</td><td>{"Concordano" if cv["model"]==ai["best_model"] else "Discordano / incerto"}</td></tr>')
    doc.append('</table><section><h2>Esito della prova</h2><p><strong>Non pronto per assegnazioni automatiche.</strong> OpenCV preferisce SOT-23 sui quattro ritagli C27/C36/C37/C39, ma confonde SOT-23-5 e SOT-23-6 su C33. AI sceglie SOT-23-6 su C33, ma lo propone anche su C27, dove la foto mostra un terminale a sinistra e due a destra. Su C36 e C37 il numero di terminali dichiarato dall?AI non coincide con il modello scelto.</p><p>Due richieste iniziali hanno restituito JSON non valido e sono state ripetute; le sei risposte valide sono riportate integralmente nei risultati. Il costo indicato ? parziale, perch? non include le prime due esecuzioni interrotte.</p><p>Prossimo controllo utile: isolamento dei terminali e tolleranza alle saldature per OpenCV; per AI, lettura della sola foto prima di mostrare i modelli, cos? da misurare l?influenza dei riferimenti sulla conta.</p></section><p><strong>Limiti:</strong> solo tre package candidati, una sola scheda, nessun campione indipendente. Il catalogo non comprende SC-70/SOT-353. Le proiezioni distinguono corpo e terminali tramite altezza del solido fuso, senza materiali STEP. Rotazioni equivalenti non determinano la polarit?. Non pronto per assegnazioni automatiche.</p>')
    for row in outcomes:
        cid=row['component'].split()[0];ai=row['ai'];cv=row['opencv']
        doc.append(f'<section id="{cid}"><h2>{html.escape(row["component"])}</h2><div class="grid"><div><h3>Foto reale</h3><img src="{embedded(args.folder/f"{cid}-photo.png")}"></div>')
        for index,r in enumerate(cv['ranked']):
            doc.append(f'<div><h3>{r["model"]} ? {r["angle"]}?</h3><img src="{embedded(args.folder/f"{cid}-{index}.png")}"><p>OpenCV {r["score"]:.3f}; scala {r["scale"]:.1f}</p><small>Terminali {r["parts"]["metal"]:.3f} ? corpo {r["parts"]["body"]:.3f} ? bordi {r["parts"]["edge"]:.3f}</small></div>')
        doc.append(f'</div><p class="note">Giallo: sagoma prevista. Azzurro: terminali previsti. Margine OpenCV fra primo e secondo: {cv["margin"]:.3f}.</p><h3>AI: {html.escape(ai["best_model"])} ? {html.escape(ai["confidence"])}</h3><p>{html.escape(ai["reason"])}</p><p>Terminali visibili secondo AI: {html.escape(str(ai["visible_leads"]))}; visibilit?: {html.escape(ai["visibility"])}.</p><ul>')
        if row['consistency_warning']:
            doc.append('<p><strong>'+html.escape(row['consistency_warning'])+'</strong></p>')
        for r in ai['ranking']:
            doc.append(f'<li><b>{html.escape(r["model"])}</b>: {"compatibile" if r["compatible"] else "non compatibile"}; angoli {html.escape(str(r["angles"]))}?. {html.escape(r["reason"])}</li>')
        doc.append('</ul></section>')
    doc.append(f'<details><summary>Le viste KiCad fornite all?AI (0?, 90?, 180?, 270?)</summary><img src="{embedded(references)}"></details></html>')
    (args.folder/'confronto-opencv-ai.html').write_text(''.join(doc),encoding='utf-8')
    print('Report:',args.folder/'confronto-opencv-ai.html',flush=True)


if __name__=='__main__':
    main()
