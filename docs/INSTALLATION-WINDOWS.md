# Installing KiCadGen on Windows

## 1. Before you start

KiCadGen uses two free programs, installed separately:

- **FreeCAD** (freecad.org) — builds the 3D models of the components.
- **KiCad** (kicad.org) — to check the result and install the libraries.

Install them before or after KiCadGen: it finds them in `C:\Program Files`.
The installer tells you on its last page if one of them is missing.

## 2. Install

1. Download `KiCadGen-X.Y.Z.msi` from the project's GitHub Releases.
2. Open it. Windows may show «Windows protected your PC» (the installer is
   not digitally signed): choose *More info* > *Run anyway*.
3. Accept the license, choose the folder (default `C:\Program Files\KiCadGen`)
   and confirm. Administrator rights are required: the program is installed
   for all the users of the computer.
4. Start KiCadGen from the Start menu or the desktop.

## 3. OpenRouter API key (AI analysis)

The AI analysis needs an OpenRouter API key. For now it is set in a text file:

1. Open the folder `%LOCALAPPDATA%\KiCadGen` (paste it in the File Explorer
   address bar). It exists after KiCadGen has been started once.
2. Create a file named `.env` containing one line:

   ```
   OPENROUTER_API_KEY=sk-or-...
   ```

3. Restart KiCadGen. The «AI credit» line in the Project tab shows the
   balance when the key works.

The key stays on your computer, in your user folder.

## 4. Where your files are

| What | Where |
|---|---|
| Your projects | `Documents\KiCadGen` (you can choose another workspace in the program) |
| Settings, AI profiles, accepted models, API key | `%LOCALAPPDATA%\KiCadGen` |
| The program | `C:\Program Files\KiCadGen` |

## 5. Update and uninstall

- **Update**: open the newer MSI; it replaces the installed version and keeps
  your projects and settings.
- **Uninstall**: Settings > Apps > KiCadGen > Uninstall. Your projects and
  `%LOCALAPPDATA%\KiCadGen` are **not** removed; delete them yourself if you
  no longer need them.
