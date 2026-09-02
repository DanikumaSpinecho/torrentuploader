# -*- coding: utf-8 -*-
"""
Integration Windows pour Torrent Uploader.

Enregistre l'application comme gestionnaire des fichiers .torrent et des liens
magnet:, uniquement dans HKEY_CURRENT_USER (aucun droit administrateur requis).

Toutes les ecritures sont reversibles via unregister().
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
from ctypes import wintypes
from pathlib import Path

try:
    import winreg
except ImportError:  # non-Windows : le module reste importable
    winreg = None


PROGID_FILE = "TorrentUploader.torrent"
PROGID_MAGNET = "TorrentUploader.magnet"
APP_NAME = "Torrent Uploader"
APP_KEY = r"Software\TorrentUploader"
CAP_KEY = APP_KEY + r"\Capabilities"
REGISTERED_APPS = r"Software\RegisteredApplications"
REGISTERED_NAME = "TorrentUploader"

FILEEXTS_USERCHOICE = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.torrent\UserChoice"
)
MAGNET_USERCHOICE = (
    r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\magnet\UserChoice"
)


class IntegrationError(Exception):
    pass


# --------------------------------------------------------------------------- #
#  Interpreteur et commandes
# --------------------------------------------------------------------------- #

def _is_usable_exe(path) -> bool:
    """Ecarte l'alias Microsoft Store (fichier de 0 octet qui ouvre le Store)."""
    if not path:
        return False
    p = Path(path)
    try:
        if not p.is_file() or p.stat().st_size == 0:
            return False
    except OSError:
        return False
    return "\\microsoft\\windowsapps\\" not in str(p).lower()


def python_launcher() -> str:
    """Chemin de pythonw.exe (lancement sans fenetre de console)."""
    candidates = []
    exe = Path(sys.executable) if sys.executable else None
    if exe:
        candidates.append(exe.with_name("pythonw.exe"))
        candidates.append(exe)
    for name in ("pythonw", "pythonw.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for cand in candidates:
        if _is_usable_exe(cand):
            return str(cand)
    raise IntegrationError(
        "pythonw.exe est introuvable. Installez Python depuis python.org "
        "(l'alias du Microsoft Store ne convient pas)."
    )


def script_path() -> str:
    return str((Path(__file__).resolve().parent / "torrent_uploader.py"))


def commands() -> tuple[str, str]:
    """(commande fichier, commande URI). %L = nom long ; %1 = URI."""
    pyw = python_launcher()
    script = script_path()
    return (
        '"%s" "%s" "%%L"' % (pyw, script),
        '"%s" "%s" "%%1"' % (pyw, script),
    )


# --------------------------------------------------------------------------- #
#  Aides registre
# --------------------------------------------------------------------------- #

def _require_windows():
    if winreg is None or os.name != "nt":
        raise IntegrationError("Cette fonction n'est disponible que sous Windows.")


def _set(path: str, value: str, name: str = ""):
    _require_windows()
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_WRITE)
    try:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    finally:
        winreg.CloseKey(key)


def _get(path: str, name: str = ""):
    _require_windows()
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ)
    except OSError:
        return None
    try:
        value, _ = winreg.QueryValueEx(key, name)
        return value
    except OSError:
        return None
    finally:
        winreg.CloseKey(key)


def _delete_value(path: str, name: str) -> bool:
    _require_windows()
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE)
    except OSError:
        return False
    try:
        winreg.DeleteValue(key, name)
        return True
    except OSError:
        return False
    finally:
        winreg.CloseKey(key)


def _delete_tree(path: str) -> bool:
    """Supprime une cle et toute sa descendance (DeleteKey refuse les cles peuplees)."""
    _require_windows()
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                             winreg.KEY_READ | winreg.KEY_WRITE)
    except OSError:
        return False
    try:
        while True:
            try:
                sub = winreg.EnumKey(key, 0)   # toujours l'index 0 : la liste se decale
            except OSError:
                break
            _delete_tree(path + "\\" + sub)
    finally:
        winreg.CloseKey(key)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        return True
    except OSError:
        return False


def notify_shell():
    """Previent l'explorateur qu'une association a change (SHCNE_ASSOCCHANGED)."""
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  Verification par l'API du shell
# --------------------------------------------------------------------------- #

ASSOCF_NONE = 0
ASSOCSTR_COMMAND = 1


def assoc_command(assoc: str) -> str | None:
    """Commande que le shell executerait reellement pour cette extension/ce schema."""
    if os.name != "nt":
        return None
    try:
        func = ctypes.windll.shlwapi.AssocQueryStringW
    except Exception:
        return None
    size = wintypes.DWORD(0)
    # 1er appel : longueur necessaire
    func(ASSOCF_NONE, ASSOCSTR_COMMAND, assoc, None, None, ctypes.byref(size))
    if not size.value:
        return None
    buf = ctypes.create_unicode_buffer(size.value)
    hr = func(ASSOCF_NONE, ASSOCSTR_COMMAND, assoc, None, buf, ctypes.byref(size))
    if hr != 0:
        return None
    return buf.value or None


# --------------------------------------------------------------------------- #
#  Dossier Telechargements (resolu par le shell, jamais devine)
# --------------------------------------------------------------------------- #

class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def downloads_folder() -> Path:
    """FOLDERID_Downloads via SHGetKnownFolderPath (gere la redirection de dossier)."""
    if os.name == "nt":
        folder_id = _GUID(
            0x374DE290, 0x123F, 0x4565,
            (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B),
        )
        out = ctypes.c_wchar_p()
        try:
            hr = ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(folder_id), 0, None, ctypes.byref(out)
            )
            if hr == 0 and out.value:
                path = Path(out.value)
                ctypes.windll.ole32.CoTaskMemFree(out)
                return path
        except Exception:
            pass
        raw = _get(r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
                   "{374DE290-123F-4565-9164-39C4925E467B}")
        if raw:
            return Path(os.path.expandvars(raw))
    return Path.home() / "Downloads"


# --------------------------------------------------------------------------- #
#  Enregistrement / desenregistrement
# --------------------------------------------------------------------------- #

def register(classes: str = r"Software\Classes", app_key: str = APP_KEY,
             registered_apps: str = REGISTERED_APPS) -> list[str]:
    """Enregistre .torrent et magnet: dans HKCU. Retourne le journal des actions.

    Les racines sont parametrables uniquement pour permettre les tests en bac a sable.
    """
    _require_windows()
    cmd_file, cmd_uri = commands()
    cap_key = app_key + r"\Capabilities"
    log: list[str] = []

    # --- ProgId fichier ---
    _set(classes + "\\" + PROGID_FILE, "Fichier torrent")
    _set(classes + "\\" + PROGID_FILE + r"\shell\open\command", cmd_file)
    log.append("ProgId %s -> %s" % (PROGID_FILE, cmd_file))

    # --- extension .torrent ---
    current = _get(classes + r"\.torrent")
    if not current or current == PROGID_FILE:
        _set(classes + r"\.torrent", PROGID_FILE)
        log.append(".torrent associe a %s" % PROGID_FILE)
    else:
        log.append(".torrent appartient deja a « %s » : association par defaut non modifiee "
                   "(utilisez « Ouvrir avec »)." % current)
    # toujours, meme si un concurrent detient le defaut : place l'appli dans « Ouvrir avec »
    _set(classes + r"\.torrent\OpenWithProgids", "", name=PROGID_FILE)
    log.append("Ajoute a la liste « Ouvrir avec »")

    # --- schema magnet: ---
    for key in (classes + r"\magnet", classes + "\\" + PROGID_MAGNET):
        _set(key, "URL:Magnet Link")
        _set(key, "", name="URL Protocol")      # valeur vide OBLIGATOIRE, sinon pas de lancement
        _set(key + r"\shell\open\command", cmd_uri)
    log.append("magnet: -> %s" % cmd_uri)

    # --- Capabilities : fait apparaitre l'appli dans Parametres > Applications par defaut ---
    _set(cap_key, APP_NAME, name="ApplicationName")
    _set(cap_key, "Envoie les torrents et les liens magnet vers une seedbox ruTorrent.",
         name="ApplicationDescription")
    _set(cap_key + r"\FileAssociations", PROGID_FILE, name=".torrent")
    _set(cap_key + r"\UrlAssociations", PROGID_MAGNET, name="magnet")
    _set(registered_apps, cap_key, name=REGISTERED_NAME)
    log.append("Declare dans les Applications par defaut de Windows")

    notify_shell()
    return log


def unregister(classes: str = r"Software\Classes", app_key: str = APP_KEY,
               registered_apps: str = REGISTERED_APPS) -> list[str]:
    """Retire tout ce que register() a ecrit."""
    _require_windows()
    log: list[str] = []

    if _get(classes + r"\.torrent") == PROGID_FILE:
        _delete_tree(classes + r"\.torrent")
        log.append("Association .torrent retiree")
    else:
        if _delete_value(classes + r"\.torrent\OpenWithProgids", PROGID_FILE):
            log.append("Retire de la liste « Ouvrir avec »")

    for key in (classes + "\\" + PROGID_FILE, classes + "\\" + PROGID_MAGNET):
        if _delete_tree(key):
            log.append("Supprime %s" % key)

    # ne supprime le schema magnet que s'il pointe encore vers nous
    cmd = _get(classes + r"\magnet\shell\open\command")
    if cmd and "torrent_uploader.py" in cmd:
        _delete_tree(classes + r"\magnet")
        log.append("Schema magnet: retire")

    _delete_value(registered_apps, REGISTERED_NAME)
    _delete_tree(app_key)
    log.append("Declaration Applications par defaut retiree")

    notify_shell()
    return log


def status(classes: str = r"Software\Classes") -> dict:
    """Etat courant, pour affichage dans l'interface."""
    if os.name != "nt":
        return {"supported": False}
    cmd_file, cmd_uri = "", ""
    try:
        cmd_file, cmd_uri = commands()
    except IntegrationError as exc:
        return {"supported": True, "error": str(exc)}

    ours_file = _get(classes + "\\" + PROGID_FILE + r"\shell\open\command")
    ours_magnet = _get(classes + "\\" + PROGID_MAGNET + r"\shell\open\command")
    torrent_owner = _get(classes + r"\.torrent")
    magnet_cmd = _get(classes + r"\magnet\shell\open\command")
    user_choice = _get(FILEEXTS_USERCHOICE, "ProgId")
    magnet_choice = _get(MAGNET_USERCHOICE, "ProgId")
    open_with = _get(classes + r"\.torrent\OpenWithProgids", PROGID_FILE)

    effective = assoc_command(".torrent")
    effective_magnet = assoc_command("magnet")

    registered = bool(ours_file) and ours_file == cmd_file
    torrent_active = (torrent_owner == PROGID_FILE) and not user_choice
    magnet_active = bool(magnet_cmd) and magnet_cmd == cmd_uri and not magnet_choice

    return {
        "supported": True,
        "registered": registered,
        "stale": bool(ours_file) and ours_file != cmd_file,
        "torrent_active": torrent_active,
        "magnet_active": magnet_active,
        "torrent_owner": torrent_owner,
        "user_choice": user_choice,
        "magnet_user_choice": magnet_choice,
        "open_with_listed": open_with is not None,
        "effective_command": effective,
        "effective_magnet_command": effective_magnet,
        "expected_command": cmd_file,
        "expected_magnet_command": cmd_uri,
        "launcher": python_launcher(),
        "script": script_path(),
    }


def open_default_apps_settings():
    """Ouvre Parametres > Applications par defaut sur notre fiche."""
    if os.name != "nt":
        return
    os.startfile("ms-settings:defaultapps?registeredAppUser=" + REGISTERED_NAME)


def describe(st: dict) -> str:
    """Resume lisible de status()."""
    if not st.get("supported"):
        return "Integration disponible uniquement sous Windows."
    if st.get("error"):
        return st["error"]
    if not st.get("registered"):
        return ("Non enregistre." if not st.get("stale")
                else "Enregistre avec un ancien chemin : cliquez sur « Activer » pour corriger.")
    parts = []
    parts.append("Fichiers .torrent : " +
                 ("actif" if st["torrent_active"] else
                  "inactif - detenu par « %s »" % (st.get("user_choice") or st.get("torrent_owner"))))
    parts.append("Liens magnet : " + ("actif" if st["magnet_active"] else "inactif"))
    if st.get("open_with_listed"):
        parts.append("present dans « Ouvrir avec »")
    return " | ".join(parts)


# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "register":
        for line in register():
            print("  +", line)
        print("\nEnregistrement termine.")
    elif action == "unregister":
        for line in unregister():
            print("  -", line)
        print("\nDesenregistrement termine.")
    else:
        st = status()
        for k, v in st.items():
            print("%-26s %s" % (k, v))
        print("\n=>", describe(st))
