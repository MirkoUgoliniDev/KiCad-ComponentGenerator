"""
KiCadGen-Tool - GUI

Run:  python main.py

Legge OPENROUTER_API_KEY (e altre variabili) da un file .env nella
cartella del progetto, se presente (vedi .env.example). Se python-dotenv
non e' installato, si prosegue usando solo le variabili d'ambiente reali.
"""

from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from kicadgen.gui.main_window import main

if __name__ == "__main__":
    main()
