"""Reobserve visible terminals in a saved calibrated photo inventory (API call)."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kicadgen.services.openrouter import call_tool
from kicadgen.services.photo_grounding import DEFAULT_VISION_MODEL
from kicadgen.services.photo_terminals import observe_terminals


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inventory', type=Path)
    parser.add_argument('photo', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--model', default=DEFAULT_VISION_MODEL)
    parser.add_argument('--indices', type=int, nargs='+',
                        help='Optional 1-based component indices for a focused diagnostic; others stay unchanged')
    args = parser.parse_args()
    data = json.loads(args.inventory.read_text(encoding='utf-8'))
    messages = []
    def log(message):
        messages.append(message)
        print(message, flush=True)
    indices = [i-1 for i in args.indices] if args.indices else list(range(len(data['components'])))
    if len(indices) != len(set(indices)) or any(i < 0 or i >= len(data['components']) for i in indices):
        parser.error('indices must be unique, existing 1-based component indices')
    selected = [data['components'][i] for i in indices]
    refined = observe_terminals(args.photo.read_bytes(), selected,
        args.model, call_tool, log, ROOT / 'generated' / 'photo-cache')
    for index, component in zip(indices, refined):
        data['components'][index] = component
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'inventory.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    (args.output / 'analysis.log').write_text('\n'.join(messages), encoding='utf-8')
    # Local visual audit: place accepted rectangles back on original photo pixels.
    import cv2
    import numpy as np
    from kicadgen.services.photo_terminals import _local_box
    audit = cv2.imdecode(np.frombuffer(args.photo.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    h, w = audit.shape[:2]
    for component in data['components']:
        geometry = component.get('terminal_geometry')
        if not geometry:
            continue
        x0,y0,x1,y1 = component['_box']
        rectangles = [(geometry['body_bbox'], (0,255,0))]
        rectangles += [(t['bbox'], (255,255,0)) for t in geometry['terminals']]
        for box, color in rectangles:
            left,top,right,bottom = _local_box(box, -component.get('rotation_deg',0))
            cv2.rectangle(audit, (round((x0+left*(x1-x0))*w), round((y0+top*(y1-y0))*h)),
                          (round((x0+right*(x1-x0))*w), round((y0+bottom*(y1-y0))*h)), color, 1)
    cv2.imwrite(str(args.output / 'terminal-audit.png'), audit)


if __name__ == '__main__':
    main()
