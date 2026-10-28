"""@brief Where desktop programs such as FreeCAD and KiCad may be installed on Windows.

@details Two sources, used by the FreeCAD and KiCad finders alike:

- the program roots: the per-machine Program Files folders as the
  environment declares them (another drive or language is possible), and the
  per-user root ``%LOCALAPPDATA%\\Programs`` used by installers run «for the
  current user only»;
- the folders declared by the installers themselves in the «Uninstall»
  registry entries (per machine, 64 and 32 bit, and per user), which cover
  any location the user chose.

Nothing here starts a program or writes anything.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import sys
from typing import Iterable, Mapping

#: Locations used when the environment does not declare them (a stripped
#: environment, e.g. a service or a test): the default Windows folders.
_FALLBACK_ROOTS = (r"C:\Program Files", r"C:\Program Files (x86)")

#: Registry subkey of the entries listed in Settings > Apps.
_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"

#: Values read from each entry, in order of trust for the install folder.
_ENTRY_VALUES = ("DisplayName", "InstallLocation", "DisplayIcon", "UninstallString")


def program_roots() -> list[Path]:
    """@brief Folders under which installers put one subfolder per program.

    @return Existing-or-not roots, without duplicates (compared without case),
        environment first: ``%ProgramFiles%``, ``%ProgramW6432%``,
        ``%ProgramFiles(x86)%``, ``%LOCALAPPDATA%\\Programs``, then the default
        ``C:\\`` folders.
    @note Callers glob below these roots, so a root that does not exist simply
        yields no match; off Windows the variables are absent and the
        ``C:\\`` folders match nothing.
    """
    declared = [os.environ.get(name) for name in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")]
    local = os.environ.get("LOCALAPPDATA")
    declared.append(str(Path(local) / "Programs") if local else None)
    roots: list[Path] = []
    seen: set[str] = set()
    for root in (*declared, *_FALLBACK_ROOTS):
        if not root:
            continue
        key = os.path.normcase(os.path.normpath(root))
        if key not in seen:
            seen.add(key)
            roots.append(Path(root))
    return roots


def _executable_of(command: str | None) -> str | None:
    """Return the file path at the start of a registry command line.

    ``DisplayIcon`` is ``path[,index]`` and ``UninstallString`` is a command
    with arguments, quoted or not; both start with the path of a file.
    """
    if not command:
        return None
    text = command.strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        return text[1:end] if end > 1 else None
    # Unquoted: the path ends at the icon index or at the first argument that
    # follows the executable's extension.
    text = text.split(",", 1)[0].strip()
    match = re.match(r"(.+?\.exe)\b", text, re.IGNORECASE)
    return match.group(1) if match else text or None


def install_dir_from_entry(entry: Mapping[str, str | None]) -> Path | None:
    """@brief Folder an «Uninstall» registry entry says the program lives in.

    @param entry Values of one entry (``InstallLocation``, ``DisplayIcon``,
        ``UninstallString``); missing or empty values are skipped.
    @return ``InstallLocation`` when declared, otherwise the folder of the
        ``DisplayIcon`` file, otherwise the folder of the uninstaller; None
        when the entry declares no path.
    @note Some installers leave ``InstallLocation`` empty (FreeCAD 1.1 does):
        the icon and the uninstaller still name files inside the installation.
        The returned folder may be a subfolder such as ``bin``; callers look
        for their executable both in it and in its ``bin`` child.
    @note Safety invariant (review 2026-10-27, E2): only an ABSOLUTE folder
        with a drive anchor is returned. A relative value (``.`` from an
        ``UninstallString`` such as ``MsiExec.exe /X{GUID}``, a bare ``C:``,
        a ``\\folder`` without drive) would make `executables_in` probe the
        current working directory, and the executable found there would
        later be RUN: such entries are treated as declaring no path.
    """
    location = (entry.get("InstallLocation") or "").strip().strip('"')
    if location:
        return _absolute_folder(location)
    for name in ("DisplayIcon", "UninstallString"):
        executable = _executable_of(entry.get(name))
        if executable:
            folder = _absolute_folder(str(Path(executable).parent))
            if folder is not None:
                return folder
    return None


def _absolute_folder(text: str) -> Path | None:
    """The folder named by ``text`` when it is absolute with a drive, else None.

    ``Path.is_absolute`` is not enough on Windows: ``C:`` (drive-relative) and
    ``\\folder`` (root-relative) both resolve against the current directory
    of the process. Both a drive and a root are required, so the folder is
    the same whatever the working directory. A UNC path (``\\\\server\\share``)
    has both and is accepted. Off Windows the drive is empty for every path,
    so the check is deliberately performed with the Windows flavour.
    """
    from pathlib import PureWindowsPath
    folder = PureWindowsPath(text.strip())
    if not folder.drive or not folder.root:
        return None
    return Path(text.strip())


def _uninstall_entries() -> list[dict[str, str | None]]:
    """Read every «Uninstall» registry entry visible to this user.

    Per machine in both registry views (64 and 32 bit programs) and per user.
    Unreadable keys are skipped; an empty list off Windows.
    """
    if sys.platform != "win32":
        return []
    import winreg

    sources = (
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, 0),
    )
    entries: list[dict[str, str | None]] = []
    for hive, view in sources:
        try:
            root = winreg.OpenKey(hive, _UNINSTALL_KEY, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, name) as key:
                        entry = {}
                        for value in _ENTRY_VALUES:
                            try:
                                data, _kind = winreg.QueryValueEx(key, value)
                            except OSError:
                                data = None
                            entry[value] = data if isinstance(data, str) else None
                except OSError:
                    continue
                entries.append(entry)
    return entries


def registered_install_dirs(display_name: str,
                            entries: Iterable[Mapping[str, str | None]] | None = None) -> list[Path]:
    """@brief Install folders of the programs whose registered name matches.

    @param display_name Regular expression matched (case-insensitive) at the
        start of each entry's ``DisplayName``, e.g. ``r"FreeCAD\\b"``.
    @param entries Registry entries to search; None reads this machine's.
    @return Declared folders, without duplicates, in registry order. They are
        not checked for existence: callers check the executable they need.
    """
    pattern = re.compile(display_name, re.IGNORECASE)
    folders: list[Path] = []
    seen: set[str] = set()
    for entry in _uninstall_entries() if entries is None else entries:
        if not pattern.match((entry.get("DisplayName") or "").strip()):
            continue
        folder = install_dir_from_entry(entry)
        if folder is None:
            continue
        key = os.path.normcase(os.path.normpath(str(folder)))
        if key not in seen:
            seen.add(key)
            folders.append(folder)
    return folders


def executables_in(folders: Iterable[Path], filename: str) -> list[str]:
    """@brief Paths of ``filename`` found directly in, or in ``bin`` below, each folder.

    @param folders Candidate install folders (from `registered_install_dirs`).
    @param filename Executable file name, e.g. ``freecadcmd.exe``.
    @return Paths of the files that exist, in folder order.
    """
    found = []
    for folder in folders:
        for candidate in (folder / filename, folder / "bin" / filename):
            try:
                if candidate.is_file():
                    found.append(str(candidate))
                    break
            except OSError:
                continue
    return found


def unique_paths(paths: Iterable[str]) -> list[str]:
    """@brief Drop paths that name the same file twice (case, separators, ``..``).

    @param paths Candidate paths, possibly found through several sources.
    @return The first spelling of each path, in input order.
    """
    result = []
    seen: set[str] = set()
    for path in paths:
        key = os.path.normcase(os.path.normpath(path))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result
