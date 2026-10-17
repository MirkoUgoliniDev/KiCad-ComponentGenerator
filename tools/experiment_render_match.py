"""@brief Offline comparison of PCB crops with top projections of KiCad STEP meshes.

Run after exporting model-meshes.json with FreeCAD. Produces a self-contained
HTML report and PNGs; does not alter projects, accepted entries or model choices.
Scores are experimental geometric similarities, never confidence probabilities.
"""
import argparse
import html
import json
from pathlib import Path

import cv2
import numpy as np

SIZE = 384
SCALE = 64.0  # pixels per mm; resampling adds no source detail


def render(solids):
    """@brief Rasterize top-facing geometry in physical units with depth ordering.

    @param solids Solids as dicts with `vertices` (x, y, z in mm),
        `triangles` and a `label` (1 body, 2 exposed lead).
    @return SIZE x SIZE label image (uint8) at SCALE px per mm.
    """
    all_vertices = np.array([v for s in solids for v in s['vertices']])
    center = (all_vertices[:, :2].min(axis=0) + all_vertices[:, :2].max(axis=0)) / 2
    low, high = all_vertices[:, 2].min(), all_vertices[:, 2].max()
    polygons = []
    for solid in solids:
        points = np.array(solid['vertices'])
        for tri in solid['triangles']:
            xyz = points[list(tri)]
            xy = (xyz[:, :2] - center) * [SCALE, -SCALE] + SIZE / 2
            polygons.append((float(xyz[:, 2].mean()), np.round(xy).astype(np.int32)))
    labels = np.zeros((SIZE, SIZE), np.uint8)
    for z, polygon in sorted(polygons, key=lambda p:p[0]):
        # STEP solids are fused. Height separates exposed low leads from the body.
        cv2.fillConvexPoly(labels, polygon, 2 if z < low + .45 * (high-low) else 1)
    return labels


def transform(labels, angle, scale=1, dx=0, dy=0):
    """@brief Rotate and shift a projection without changing its physical scale metadata.

    @param labels Label image produced by render.
    @param angle Rotation in degrees, counter-clockwise.
    @param scale Uniform scale factor applied about the image centre.
    @param dx Horizontal shift in px.
    @param dy Vertical shift in px.
    @return Transformed label image, nearest-neighbour resampled.
    """
    matrix = cv2.getRotationMatrix2D((SIZE/2, SIZE/2), angle, scale)
    matrix[:,2] += [dx,dy]
    return cv2.warpAffine(labels, matrix, (SIZE,SIZE), flags=cv2.INTER_NEAREST)


def colour(labels):
    """@brief Display body in dark grey and exposed leads in light grey.

    @param labels Label image produced by render.
    @return BGR image of the same size.
    """
    image=np.full((SIZE,SIZE,3), (55,65,55), np.uint8)
    image[labels==1]=(42,42,42)
    image[labels==2]=(220,220,220)
    return image


def compare(crop, labels):
    """@brief Compare metal occupancy, body darkness and silhouette-edge proximity.

    @param crop BGR photo crop at the same scale as the label image.
    @param labels Label image produced by render (possibly transformed).
    @return (score, parts) where parts holds the metal, body and edge terms;
        (0, zeros) when the projection has no body or no lead.
    """
    hsv=cv2.cvtColor(crop,cv2.COLOR_BGR2HSV)
    grey=cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY)
    metal=((hsv[:,:,1]<105)&(hsv[:,:,2]>110)).astype(np.uint8)
    dark=((hsv[:,:,2]<150)&(hsv[:,:,1]<170)).astype(np.uint8)
    expected=(labels==2).astype(np.uint8)
    body=(labels==1).astype(np.uint8)
    if not expected.any() or not body.any():
        return 0,dict(metal=0,body=0,edge=0)
    kernel=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(15,15))
    tolerance=cv2.dilate(expected,kernel)>0
    observed=cv2.dilate(metal,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(5,5)))
    recall=float(observed[expected>0].mean())
    # Penalize extra terminals around the body, preventing a 3-pin subset from
    # automatically beating a 6-pin template. Neighbour solder remains a confounder.
    ring=(cv2.dilate(body,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(55,55)))>0)&(body==0)
    seen=(metal>0)&ring
    precision=float((seen&tolerance).sum()/max(1,seen.sum()))
    terminal=2*recall*precision/max(1e-9,recall+precision)
    body_score=float(dark[body>0].mean())
    edges=cv2.Canny(grey,60,140)
    distance=cv2.distanceTransform(255-edges,cv2.DIST_L2,3)
    boundary=cv2.morphologyEx((labels>0).astype(np.uint8),cv2.MORPH_GRADIENT,np.ones((3,3),np.uint8))>0
    edge_score=float(np.exp(-distance[boundary]/6).mean())
    return .60*terminal+.20*body_score+.20*edge_score,dict(metal=terminal,body=body_score,edge=edge_score)


def main():
    """@brief Generate an offline report for selected saved PCB components."""
    parser=argparse.ArgumentParser()
    parser.add_argument('--project',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--meshes',type=Path,required=True)
    parser.add_argument('--ids',nargs='+',default=['C33','C27','C36','C37','C39','C4'])
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    state=json.loads((args.project/'project_config.json').read_text(encoding='utf-8'))['state']
    metadata=json.loads((args.project/'photo-review/proposals.json').read_text(encoding='utf-8'))
    length,width=metadata['body_length_mm'],metadata['body_width_mm']
    photo=cv2.imread(str(args.project/'photo-review/photo.png'))
    photo=cv2.resize(photo,(round(length*SCALE),round(width*SCALE)))
    models={name:render(solids) for name,solids in json.loads(args.meshes.read_text()).items()}
    for name,labels in models.items():
        for angle in (0,90,180,270):
            cv2.imwrite(str(args.output/f'{name}-{angle}.png'),colour(transform(labels,angle)))
    results=[]
    for component in state['components']:
        cid=component['name'].split()[0]
        if cid not in args.ids:
            continue
        center=((component['x_mm']+length/2)*SCALE,(component['y_mm']+width/2)*SCALE)
        crop=cv2.getRectSubPix(photo,(SIZE,SIZE),center)
        cv2.imwrite(str(args.output/f'{cid}-photo.png'),crop)
        ranked=[]
        for name,labels in models.items():
            best=None
            for angle in (0,90,180,270):
                # Bounded registration, identical freedom for all candidate models.
                for scale in (.9,1.,1.1):
                    for dx,dy in ((0,0),(-8,0),(8,0),(0,-8),(0,8)):
                        moved=transform(labels,angle,scale,dx,dy)
                        score,parts=compare(crop,moved)
                        row=dict(model=name,angle=angle,scale=scale,shift_px=[dx,dy],score=score,parts=parts)
                        if best is None or score>best['score']:
                            best=row
            ranked.append(best)
        ranked.sort(key=lambda r:r['score'],reverse=True)
        for index,row in enumerate(ranked):
            labels=transform(models[row['model']],row['angle'],row['scale'],*row['shift_px'])
            overlay=crop.copy()
            contours,_=cv2.findContours((labels>0).astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(overlay,contours,-1,(0,255,255),1)
            overlay[labels==2]=(overlay[labels==2]*.45+np.array([255,160,0])*.55).astype(np.uint8)
            cv2.imwrite(str(args.output/f'{cid}-{index}.png'),overlay)
        results.append(dict(component=component['name'],ranked=ranked,margin=ranked[0]['score']-ranked[1]['score']))
    (args.output/'results.json').write_text(json.dumps(dict(method='experimental top geometry and metal occupancy',pixel_per_mm=SCALE,results=results),indent=2),encoding='utf-8')
    document=['<!doctype html><meta charset="utf-8"><title>Confronto PCB / KiCad</title><style>body{background:#191b25;color:#eee;font:16px system-ui;margin:32px}section{margin:30px 0;padding:20px;background:#252836}article{display:inline-block;width:23%;vertical-align:top;margin-right:1%}img{width:100%}small{color:#bec8d4}h2{color:#9ddbd0}</style><h1>Esperimento: ritagli PCB contro proiezioni KiCad</h1><p>Confronto locale OpenCV, nessuna AI. Punteggi sperimentali: non probabilit?. Giallo: sagoma modello; azzurro: terminali previsti.</p><p>Rotazioni 0/90/180/270?, scala ?10%, traslazione ?0,125 mm. Classificazione corpo/terminali del rendering ricavata dall?altezza, da verificare. Le rotazioni simmetriche non identificano il pin 1.</p>']
    for result in results:
        cid=result['component'].split()[0]
        document.append(f'<section><h2>{html.escape(result["component"])} ? margine tra primi due: {result["margin"]:.3f}</h2><article><h3>Foto corretta per scala</h3><img src="{cid}-photo.png"></article>')
        for index,row in enumerate(result['ranked']):
            document.append(f'<article><h3>{html.escape(row["model"])} ? {row["angle"]}?</h3><img src="{cid}-{index}.png"><p>Punteggio {row["score"]:.3f}</p><small>Terminali {row["parts"]["metal"]:.3f} ? corpo {row["parts"]["body"]:.3f} ? bordi {row["parts"]["edge"]:.3f}</small></article>')
        document.append('</section>')
    document.append('<h2>Proiezioni dei modelli alla scala di confronto</h2>')
    for name in models:
        document.append(f'<section><h3>{name}</h3>')
        for angle in (0,90,180,270):document.append(f'<article><p>{angle}?</p><img src="{name}-{angle}.png"></article>')
        document.append('</section>')
    (args.output/'report.html').write_text(''.join(document),encoding='utf-8')
    for result in results:
        print(result['component'],[(r['model'],round(r['score'],3),r['angle']) for r in result['ranked']])


if __name__=='__main__':
    main()
