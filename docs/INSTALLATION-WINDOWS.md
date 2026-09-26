# Installing KiCadGen on Windows

## 1. Before you start

KiCadGen uses two free programs, installed separately:

- **FreeCAD** (freecad.org) — builds the 3D models of the components.
- **KiCad** (kicad.org) — to check the result and install the libraries.

Install them before or after KiCadGen: it finds them in `C:\Program Files`
(or `Program Files (x86)`), on PATH, or through the `FREECAD_CMD_PATH` and
`KICAD_CLI_PATH` variables. The installer only looks in Program Files: on its
last page it says which of them it **may not** have found. If they are
installed somewhere else, KiCadGen can still find them.

## 2. Install

1. Download `KiCadGen-X.Y.Z.msi` from the project's GitHub Releases.
2. Open it. Windows may show «Windows protected your PC» (the installer is
   not digitally signed): choose *More info* > *Run anyway*.
3. Accept the license, choose the folder (default `C:\Program Files\KiCadGen`)
   and confirm. Administrator rights are required: the program is installed
   for all the users of the computer.
4. Start KiCadGen from the Start menu or the desktop.

## 3. OpenRouter API key (AI analysis)

The AI analysis needs an OpenRouter API key. Set it from the program:

1. Open Configuration → «AI account...».
2. Paste the key and press «Verify»: the check reads your credit and spends
   nothing.
3. Press «Save». The key is stored in the Windows Credential Manager of your
   user account, not in a file. «Remove key» deletes it again.

The «AI credit» line in the Project tab shows the balance when the key works.
If no key is set, KiCadGen says so before starting an analysis and offers to
open «AI account...».

A text file still works as an alternative, as in earlier versions: create
`%LOCALAPPDATA%\KiCadGen\.env` (paste the folder in the File Explorer address
bar; it exists after KiCadGen has been started once) with the line
`OPENROUTER_API_KEY=sk-or-...`, then restart KiCadGen. A key saved with
«AI account...» takes precedence over the file.

The program's messages may refer to `.env.example`: it is the commented
template of that file. In the installed program it is in the program folder,
`C:\Program Files\KiCadGen\_internal\.env.example` (or `_internal` under the
folder you chose); open it with a text editor, copy the lines you need into
your `%LOCALAPPDATA%\KiCadGen\.env`, and never edit the copy in Program
Files.

## 4. Where your files are

| What | Where |
|---|---|
| Your projects | `Documents\KiCadGen` (you can choose another workspace in the program) |
| Settings, AI profiles, accepted models, `.env` key file (if used), logs | `%LOCALAPPDATA%\KiCadGen` |
| API key saved with «AI account...» | Windows Credential Manager (generic credential `KiCadGen/OpenRouter`) |
| The program | `C:\Program Files\KiCadGen` |

## 5. Update and uninstall

- **Update**: open the newer MSI (or a rebuilt MSI of the same version); it
  replaces the installed version and keeps your projects and settings. The
  folder you chose at install is proposed again (from the versions that
  remember it onwards).
- **Uninstall**: Settings > Apps > KiCadGen > Uninstall. Your projects and
  `%LOCALAPPDATA%\KiCadGen` are **not** removed; delete them yourself if you
  no longer need them. A key saved with «AI account...» also stays: remove it
  first with «Remove key», or later from the Windows Credential Manager.
