"""Prova esplicita su una foto superiore: 4 localizzazioni + verifica a gruppi.

Salva JSON e confronto PNG sotto generated/photo-review per misurare e
ispezionare la localizzazione senza rigenerare il modello FreeCAD.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from dotenv import load_dotenv
from kicadgen.services.datasheet_extractor import COMPONENT_KINDS
from kicadgen.services.openrouter import call_tool, DEFAULT_MODEL
from kicadgen.services.photo_grounding import DEFAULT_VISION_MODEL
from kicadgen.services.photo_inventory import extract_inventory


def main():
    """@brief Run the public entry point for this workflow."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("photo", type=Path)
    parser.add_argument("--length",type=float,required=True)
    parser.add_argument("--width",type=float,required=True)
    parser.add_argument("--model")
    parser.add_argument("--classifier-model")
    parser.add_argument("--output",type=Path,default=ROOT/"generated"/"photo-review")
    args = parser.parse_args()
    load_dotenv(ROOT/".env")
    config_path = ROOT/"config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    messages = []
    def log(message):
        messages.append(message)
        print(message,flush=True)
    model = args.model or config.get("vision_model") or DEFAULT_VISION_MODEL
    result = extract_inventory([args.photo],model,
                               args.length,args.width,COMPONENT_KINDS,call_tool,
                               progress=log,top_photo_index=0,cache_dir=ROOT/"generated"/"photo-cache",
                               classifier_model=args.classifier_model or config.get("model") or DEFAULT_MODEL)
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/"inventory.json").write_text(json.dumps({
        "photo":result.photo,"model":model,"body_length_mm":args.length,"body_width_mm":args.width,
        "components":result.components,"warnings":result.warnings,
        **result.calibration,
    },ensure_ascii=False,indent=2),encoding="utf-8")
    if result.overlay_png:
        (args.output/"comparison.png").write_bytes(result.overlay_png)
    if result.photo_png:
        (args.output/"calibrated-photo.png").write_bytes(result.photo_png)
    (args.output/"analysis.log").write_text("\n".join(messages),encoding="utf-8")
    if not result.components:
        raise SystemExit("Nessun componente rilevato: controllare i messaggi diagnostici.")


if __name__ == "__main__":
    main()
