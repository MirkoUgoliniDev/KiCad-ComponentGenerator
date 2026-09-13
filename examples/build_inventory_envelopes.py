"""Build a mechanical-only STEP from a photo inventory; no API calls.

Example: python examples/build_inventory_envelopes.py inventory.json --side left:3:5.08 --side right:4:2.54
Contact spacing is supplied explicitly, never inferred from package boxes.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from kicadgen.generators.castellated_module import CastellatedModuleParams, CastellatedSide, ComponentBox
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.cad3d.step_generator import generate_castellated_3d_model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inventory',type=Path)
    parser.add_argument('--side',action='append',required=True,help='edge:count:pitch_mm')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--observed-packages',action='store_true',
                        help='Use observed terminals and geometric hints, not only envelopes')
    parser.add_argument('--pcb-thickness',type=float,default=1.0,help='Estimated unless measured, default 1mm')
    args = parser.parse_args()
    data = json.loads(args.inventory.read_text(encoding='utf-8'))
    sides,pin = [],1
    for specification in args.side:
        edge,count,pitch = specification.split(':')
        sides.append(CastellatedSide(edge=edge,count=int(count),pitch=float(pitch),first_pin=pin))
        pin += int(count)
    components = [ComponentBox(x=c['x_mm'],y=c['y_mm'],length=c['length_mm'],width=c['width_mm'],
        height=c['height_mm'],name=c['name'],kind='generic',marking='',rotation=c['rotation_deg'],
        color_hex=c.get('color_hex','#303030'),
        detected_kind=c.get('_detected_kind') if args.observed_packages else None,
        terminal_geometry=c.get('terminal_geometry') if args.observed_packages else None)
        for c in data['components']]
    params = CastellatedModuleParams(name='PhotoMechanicalEnvelope',body_length=data['body_length_mm'],
        body_width=data['body_width_mm'],sides=sides,components=components,pcb_thickness=args.pcb_thickness)
    body = Body3DParams(params.body_length,params.body_width,args.pcb_thickness+max(c.height for c in components),
                        offset_x_mm=-params.body_length/2,offset_y_mm=-params.body_width/2,
                        color_hex='#181818',pin_color_hex='#cba42e')
    output = args.output or args.inventory.parent/'model'
    result = generate_castellated_3d_model(params,body,output)
    (output/'build-report.json').write_text(json.dumps(result,default=str,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,default=str,ensure_ascii=False,indent=2))
    # Offline rendering of the actual exported mesh, not an idealized drawing.
    if result.get('mesh_path'):
        import numpy as np
        import pyvista as pv
        mesh_data = json.loads(Path(result['mesh_path']).read_text(encoding='utf-8'))
        plot = pv.Plotter(off_screen=True,window_size=(1440,650))
        plot.set_background('#eeeeee')
        for part in mesh_data.get('parts',[]):
            vertices = np.asarray(part.get('vertices',[]))
            triangles = np.asarray(part.get('triangles',[]))
            if not len(vertices) or not len(triangles):
                continue
            faces = np.hstack([np.full((len(triangles),1),3),triangles]).ravel()
            plot.add_mesh(pv.PolyData(vertices,faces),color=part.get('color','#808080'),ambient=.4,specular=.2)
        plot.camera_position = [(25,-38,65),(0,0,0),(0,1,0)]
        plot.enable_parallel_projection()
        plot.reset_camera()
        plot.screenshot(str(output/'mechanical-3d.png'))
        plot.view_xy()
        plot.reset_camera()
        plot.camera.parallel_scale = max(params.body_width / 2,
                                         params.body_length / (2 * 1440 / 650)) * 1.12
        plot.render()
        plot.screenshot(str(output/'mechanical-top.png'))
        plot.close()


if __name__ == '__main__':
    main()
