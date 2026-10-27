# Installing KiCadGen on Windows

## 1. Before you start

KiCadGen uses two free programs, installed separately:

- **FreeCAD** (freecad.org) — builds the STEP models of the components.
  Without it the footprint and the symbol are still produced.
- **KiCad 10 or newer** (kicad.org) — to test the result in an isolated
  project (its PCB Editor), run «Check component for KiCad» (`kicad-cli`
  adds the real loading check; without it that check is skipped) and install
  the libraries (`kicad-cli` is needed for the global library). The
  global library installation is refused until KiCad has been started once
  and has created its global footprint library table: start KiCad, complete
  its initial library setup, then retry. The generated files use the KiCad 10
  formats, which KiCad 8 and 9 cannot read.

Install them before or after KiCadGen, for all users or for the current user
only. KiCad is found through the `KICAD_CLI_PATH` variable, on PATH, or where
it is installed; FreeCAD is **not** looked up on PATH: it is found through the
`FREECAD_CMD_PATH` variable (full path of `freecadcmd.exe`) or where it is
installed. «Where it is installed» means the Program Files folders, the
per-user folder `%LOCALAPPDATA%\Programs` and any folder declared by the
program's entry in Settings > Apps (its «Uninstall» registry entry), so a
custom folder or another drive is found too. The variables remain for a
portable copy that has no such entry.
The installer itself only looks in Program Files: on its last page it says
which of them it **may not** have found; KiCadGen searches further when it
starts.

## 2. Install

1. Download `KiCadGen-X.Y.Z.msi` from the project's GitHub Releases.
2. Open it. Windows may show «Windows protected your PC» (the installer is
   not digitally signed): choose *More info* > *Run anyway*.
3. Accept the license, choose the folder (default `C:\Program Files\KiCadGen`)
   and confirm. Administrator rights are required: the program is installed
   for all the users of the computer.
4. Start KiCadGen from the Start menu or the desktop. The interface language
   (Italian, English, French, German) is chosen from the selector above the
   tabs.

## 3. AI account (API keys)

The document analysis is paid and needs an API key of one of the two
supported providers: **OpenRouter** (one key for many vendors; the default
model of the installed program, `anthropic/claude-opus-5`, goes through it) or
**DeepSeek's direct API** (models `deepseek-api/...`; running from the sources
the default model is `deepseek-api/deepseek-flash`, from the profile
database). Choose the model from the interface, in «Configuration» →
«General AI defaults...», tab «Model». Set the key from the program:

1. Open **Configuration → AI account...**. The window lists every provider
   with where its key in use comes from (environment variable, Windows
   Credential Manager, `.env` file or none) and its current balance, read
   with the providers' free status endpoints.
2. Select the provider, paste the key and press **Verify**: the check reads
   your balance and spends nothing. A key the provider refuses cannot be
   saved; when the provider cannot be reached, saving is still possible after
   an explicit confirmation.
3. Press **Save**. The key is stored in the Windows Credential Manager of
   your user account (generic credentials `KiCadGen/OpenRouter` and
   `KiCadGen/DeepSeek`), not in a file. **Remove key** deletes that
   entry only; keys in the environment or in a `.env` file are never touched.

The «AI credit» line in the Project tab shows the balance of the provider of
the chosen model, and the analysis window shows the cost of the current
analysis and of the project. If no key is set, KiCadGen says so before
starting an analysis and offers to open «AI account...».

A text file still works as an alternative: create
`%LOCALAPPDATA%\KiCadGen\.env` (paste the folder in the File Explorer address
bar; it exists after KiCadGen has been started once; **Tools → Open tool
project folder** opens it) with the lines you need,
`OPENROUTER_API_KEY=sk-or-...` and/or `DEEPSEEK_API_KEY=...`, then restart
KiCadGen. The key is looked up in this order: environment variable,
Credential Manager, `.env` file. The template `.env.example` is in the
program folder, `C:\Program Files\KiCadGen\_internal\.env.example`: open it
with a text editor, copy the lines you need into your
`%LOCALAPPDATA%\KiCadGen\.env`, and never edit the copy in Program Files.

## 4. Local detectors in the installed program

The installed program ships ONNX Runtime: the local YOLO detector works with
`.onnx` weights, chosen in the «YOLO model» field (the weights are not
included, see `PACKAGING-WINDOWS-MSI.md`). PyTorch is not shipped, so `.pt`
weights and the «YOLO + local SMD network (v3)» route are unavailable there:
the entry is disabled with the reason in its tooltip, and the default
detector is «Local YOLO + AI verification». To use them, run the program from
the sources (see `README.md`).

## 5. Where your files are

| What | Where |
|---|---|
| Your projects | `Documents\KiCadGen` (you can choose another workspace in the Project tab) |
| Settings (`config.json`), AI profiles, accepted models, model caches, `.env` key file (if used) | `%LOCALAPPDATA%\KiCadGen` (**Tools → Open tool project folder**) |
| Crash reports | `%LOCALAPPDATA%\KiCadGen\logs\crash-reports` (only the newest 20 are kept) |
| Analysis and FreeCAD logs | `analisi-ai.log` and `freecad.log` in each project folder (**Help → Open log folder**) |
| API keys saved with «AI account...» | Windows Credential Manager (`KiCadGen/OpenRouter`, `KiCadGen/DeepSeek`) |
| The program | `C:\Program Files\KiCadGen` (read-only) |

`KICADGEN_DATA_DIR`, set before starting the program, moves the data folder
and the projects (`<dir>\generated`).

## 6. Help, problems and updates

- **Help → User guide** (the menu is **?** in Italian) opens the HTML guide
  shipped with the program, written in Italian.
- **Help → Report a problem...** opens a new GitHub issue in the browser,
  prefilled with the program and system versions only, and opens the log
  folder: attach the files you want after reading them (they may contain
  your folder names). Nothing is sent until you submit the issue.
- **Help → Check for updates...** compares this version with the latest
  GitHub release. The check runs only when you ask. At start-up the program
  makes one network call only: it reads the balance of the AI provider
  (OpenRouter or DeepSeek) whose API key is configured; without a key nothing
  is sent. No other call is made at start-up.

## 7. Update and uninstall

- **Update**: open the newer MSI (or a rebuilt MSI of the same version); it
  replaces the installed version and keeps your projects and settings. The
  folder you chose at install is proposed again.
- **Uninstall**: Settings > Apps > KiCadGen > Uninstall. Your projects and
  `%LOCALAPPDATA%\KiCadGen` are **not** removed; delete them yourself if you
  no longer need them. Keys saved with «AI account...» also stay: remove them
  first with «Remove key», or later from the Windows Credential Manager.
