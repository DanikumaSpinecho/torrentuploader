# -*- coding: utf-8 -*-
"""Intégration à Windows et aux navigateurs.

Trois enregistrements, tous dans HKEY_CURRENT_USER (aucun droit administrateur) :

* les fichiers .torrent et les liens magnet: s'ouvrent avec l'application ;
* l'application apparaît dans « Applications par défaut » et « Ouvrir avec » ;
* un hôte de messagerie native permet à l'extension de navigateur de joindre
  l'application — et de la lancer si elle est fermée — sans jeton ni port.

Tout ce que register() écrit, unregister() le retire.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

from . import APP_NAME
from .paths import APP_DIR, ASSETS_DIR, ENV_DATA_DIR, is_sandboxed, native_host_dir

try:
    import winreg
except ImportError:                      # hors Windows : le module reste importable
    winreg = None


PROGID_FILE = "TorrentUploader.torrent"
PROGID_MAGNET = "TorrentUploader.magnet"
CLASSES = r"Software\Classes"
APP_KEY = r"Software\TorrentUploader"
REGISTERED_APPS = r"Software\RegisteredApplications"
REGISTERED_NAME = "TorrentUploader"

# Hôte de messagerie native : le nom et l'identifiant de l'extension forment le
# contrat avec le projet de l'extension (son manifeste porte la clé correspondante).
HOST_NAME = "com.torrentuploader.host"
EXTENSION_IDS = ("ejbkdabahogcncpmabcgmenfmefokgjj",)

# (clé du navigateur, sous-clé des hôtes) — on n'écrit que si le navigateur existe.
BROWSERS = (
    ("Chrome", r"Software\Google\Chrome"),
    ("Edge", r"Software\Microsoft\Edge"),
    ("Brave", r"Software\BraveSoftware\Brave-Browser"),
    ("Chromium", r"Software\Chromium"),
)

FILEEXTS_USERCHOICE = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.torrent\UserChoice"
)
MAGNET_USERCHOICE = (
    r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\magnet\UserChoice"
)


class IntegrationError(Exception):
    pass


# --------------------------------------------------------------------------- #
#  Interpréteur, scripts et commandes
# --------------------------------------------------------------------------- #

def _is_usable_exe(path) -> bool:
    """Écarte l'alias du Microsoft Store (fichier de 0 octet qui ouvre le Store)."""
    if not path:
        return False
    p = Path(path)
    try:
        if not p.is_file() or p.stat().st_size == 0:
            return False
    except OSError:
        return False
    return "\\microsoft\\windowsapps\\" not in str(p).lower()


def _find_python(name: str) -> str:
    candidates = []
    if sys.executable:
        candidates.append(Path(sys.executable).with_name(name))
    found = shutil.which(name)
    if found:
        candidates.append(Path(found))
    for cand in candidates:
        if _is_usable_exe(cand):
            return str(cand)
    raise IntegrationError(
        "%s est introuvable. Installez Python depuis python.org "
        "(l'alias du Microsoft Store ne convient pas)." % name
    )


def python_launcher() -> str:
    """pythonw.exe : lance l'interface sans fenêtre de console."""
    return _find_python("pythonw.exe")


def python_console() -> str:
    """python.exe : requis par l'hôte natif, qui dialogue sur stdin/stdout."""
    return _find_python("python.exe")


def script_path() -> str:
    return str(APP_DIR / "torrent_uploader.py")


def host_script_path() -> str:
    return str(APP_DIR / "native_host.py")


def icon_path() -> str:
    return str(ASSETS_DIR / "icon.ico")


def commands() -> tuple[str, str]:
    """(commande fichier, commande URI). %L = nom long du fichier ; %1 = URI."""
    pyw, script = python_launcher(), script_path()
    return ('"%s" "%s" "%%L"' % (pyw, script), '"%s" "%s" "%%1"' % (pyw, script))


def _script_in_command(command) -> str:
    """Chemin du script cité dans une commande enregistrée (2e élément entre guillemets)."""
    parts = re.findall(r'"([^"]*)"', command or "")
    return parts[1] if len(parts) >= 2 else ""


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


def _exists(path: str) -> bool:
    _require_windows()
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ))
        return True
    except OSError:
        return False


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
    """Supprime une clé et toute sa descendance (DeleteKey refuse les clés peuplées)."""
    _require_windows()
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                             winreg.KEY_READ | winreg.KEY_WRITE)
    except OSError:
        return False
    try:
        while True:
            try:
                sub = winreg.EnumKey(key, 0)   # toujours l'index 0 : la liste se décale
            except OSError:
                break
            if not _delete_tree(path + "\\" + sub):
                return False               # sous-clé protégée : on s'arrête, sans boucler
    finally:
        winreg.CloseKey(key)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        return True
    except OSError:
        return False


def _is_empty(path: str) -> bool:
    """Vrai si la clé n'a ni sous-clé ni valeur (y compris sa valeur par défaut)."""
    _require_windows()
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ)
    except OSError:
        return False
    try:
        subkeys, values, _ = winreg.QueryInfoKey(key)
        return subkeys == 0 and values == 0
    finally:
        winreg.CloseKey(key)


def _prune(path: str, stop: str) -> None:
    """Supprime `path` puis ses parents tant qu'ils sont vides, sans remonter au-delà de `stop`."""
    while path and len(path) > len(stop) and path.lower().startswith(stop.lower()):
        if not _is_empty(path) or not _delete_tree(path):
            return
        path = path.rsplit("\\", 1)[0]


def _delete_value_if(path: str, expected, name: str = "") -> bool:
    """Supprime une valeur seulement si elle vaut ce que nous y avons écrit."""
    if _get(path, name) == expected:
        return _delete_value(path, name)
    return False


def notify_shell():
    """Prévient l'explorateur qu'une association a changé (SHCNE_ASSOCCHANGED)."""
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  API du shell
# --------------------------------------------------------------------------- #

ASSOCF_NONE = 0
ASSOCSTR_COMMAND = 1


def assoc_command(assoc: str) -> str | None:
    """Commande que le shell exécuterait réellement pour cette extension ou ce schéma."""
    if os.name != "nt":
        return None
    try:
        func = ctypes.windll.shlwapi.AssocQueryStringW
    except Exception:
        return None
    size = wintypes.DWORD(0)
    func(ASSOCF_NONE, ASSOCSTR_COMMAND, assoc, None, None, ctypes.byref(size))
    if not size.value:
        return None
    buf = ctypes.create_unicode_buffer(size.value)
    hr = func(ASSOCF_NONE, ASSOCSTR_COMMAND, assoc, None, buf, ctypes.byref(size))
    if hr != 0:
        return None
    return buf.value or None


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _known_folder(d1, d2, d3, d4) -> Path | None:
    """SHGetKnownFolderPath : résolu par le shell, donc juste même si le dossier est redirigé."""
    if os.name != "nt":
        return None
    folder_id = _GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))
    out = ctypes.c_wchar_p()
    try:
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(out))
        if hr == 0 and out.value:
            path = Path(out.value)
            ctypes.windll.ole32.CoTaskMemFree(out)
            return path
    except Exception:
        pass
    return None


def downloads_folder() -> Path:
    found = _known_folder(0x374DE290, 0x123F, 0x4565,
                          (0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
    return found or (Path.home() / "Downloads")


def desktop_folder() -> Path:
    found = _known_folder(0xB4BFCC3A, 0xDB2C, 0x424C,
                          (0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))
    return found or (Path.home() / "Desktop")


# --------------------------------------------------------------------------- #
#  Hôte de messagerie native
# --------------------------------------------------------------------------- #

def _host_files() -> tuple[Path, Path]:
    folder = native_host_dir()
    return folder / (HOST_NAME + ".json"), folder / "torrent-uploader-host.bat"


def _host_bat_content() -> str:
    lines = ["@echo off", "chcp 65001 >nul"]
    if is_sandboxed():                   # tests : l'hôte doit viser les mêmes données
        lines.append('set "%s=%s"' % (ENV_DATA_DIR, os.environ[ENV_DATA_DIR]))
    lines.append('"%s" "%s" %%*' % (python_console(), host_script_path()))
    return "\r\n".join(lines) + "\r\n"


def _host_manifest(bat: Path) -> dict:
    return {
        "name": HOST_NAME,
        "description": "Torrent Uploader - relais entre le navigateur et l'application",
        "path": str(bat),
        "type": "stdio",
        "allowed_origins": ["chrome-extension://%s/" % ext for ext in EXTENSION_IDS],
    }


def _host_roots(force_all: bool = False) -> list[tuple[str, str]]:
    """Navigateurs présents sur ce poste (clé d'éditeur existante dans HKCU)."""
    return [(name, base + "\\NativeMessagingHosts\\" + HOST_NAME)
            for name, base in BROWSERS if force_all or _exists(base)]


def register_native_host(roots=None) -> list[str]:
    """Écrit le manifeste de l'hôte natif et le déclare aux navigateurs installés."""
    _require_windows()
    manifest, bat = _host_files()
    bat.write_text(_host_bat_content(), encoding="utf-8", newline="")
    manifest.write_text(json.dumps(_host_manifest(bat), indent=2), encoding="utf-8")
    log = []
    for name, key in (_host_roots() if roots is None else roots):
        _set(key, str(manifest))
        log.append("Extension de navigateur : hôte déclaré pour %s" % name)
    if not log:
        log.append("Extension de navigateur : aucun navigateur compatible détecté")
    return log


def unregister_native_host(roots=None) -> list[str]:
    _require_windows()
    log = []
    for name, key in (_host_roots(force_all=True) if roots is None else roots):
        value = _get(key)
        if value is not None and _delete_tree(key):
            log.append("Hôte de l'extension retiré pour %s" % name)
    manifest, bat = _host_files()
    for path in (manifest, bat):
        try:
            path.unlink()
        except OSError:
            pass
    return log


def native_host_status(roots=None) -> dict:
    """État de l'hôte natif : déclaré, et pointant bien sur cette installation ?"""
    manifest, bat = _host_files()
    roots = _host_roots() if roots is None else roots
    declared = [name for name, key in roots if _get(key) == str(manifest)]
    try:
        files_ok = (json.loads(manifest.read_text(encoding="utf-8")) == _host_manifest(bat)
                    and bat.read_bytes() == _host_bat_content().encode("utf-8"))
    except Exception:
        files_ok = False
    return {
        "browsers": [name for name, _ in roots],
        "declared": declared,
        "ok": bool(roots) and len(declared) == len(roots) and files_ok,
        "applicable": bool(roots),
    }


# --------------------------------------------------------------------------- #
#  Enregistrement / désenregistrement
# --------------------------------------------------------------------------- #

def register(classes: str = CLASSES, app_key: str = APP_KEY,
             registered_apps: str = REGISTERED_APPS, native_roots=None) -> list[str]:
    """Enregistre .torrent, magnet: et l'hôte natif. Retourne le journal des actions.

    Les racines sont paramétrables uniquement pour permettre les tests en bac à sable.
    """
    _require_windows()
    cmd_file, cmd_uri = commands()
    cap_key = app_key + r"\Capabilities"
    icon = icon_path() if Path(icon_path()).is_file() else ""
    log: list[str] = []

    # --- type de fichier ---
    progid = classes + "\\" + PROGID_FILE
    _set(progid, "Fichier torrent")
    _set(progid + r"\shell\open\command", cmd_file)
    if icon:
        _set(progid + r"\DefaultIcon", '"%s",0' % icon)
    log.append("Fichiers .torrent : commande d'ouverture enregistrée")

    current = _get(classes + r"\.torrent")
    if not current or current == PROGID_FILE:
        _set(classes + r"\.torrent", PROGID_FILE)
        log.append("Fichiers .torrent : associés à %s" % APP_NAME)
    else:
        log.append("Fichiers .torrent : déjà gérés par « %s », association par défaut "
                   "non modifiée (utilisez « Ouvrir avec »)" % current)
    # toujours, même si un autre programme détient le défaut : place l'appli dans « Ouvrir avec »
    _set(classes + r"\.torrent\OpenWithProgids", "", name=PROGID_FILE)

    # --- schéma magnet: ---
    for key in (classes + r"\magnet", classes + "\\" + PROGID_MAGNET):
        _set(key, "URL:Magnet Link")
        _set(key, "", name="URL Protocol")      # valeur vide OBLIGATOIRE, sinon pas de lancement
        _set(key + r"\shell\open\command", cmd_uri)
        if icon:
            _set(key + r"\DefaultIcon", '"%s",0' % icon)
    log.append("Liens magnet : associés à %s" % APP_NAME)

    # --- Capabilities : fait apparaître l'appli dans Paramètres > Applications par défaut ---
    _set(cap_key, APP_NAME, name="ApplicationName")
    _set(cap_key, "Envoie les torrents et les liens magnet vers une seedbox ruTorrent.",
         name="ApplicationDescription")
    if icon:
        _set(cap_key, icon, name="ApplicationIcon")
    _set(cap_key + r"\FileAssociations", PROGID_FILE, name=".torrent")
    _set(cap_key + r"\UrlAssociations", PROGID_MAGNET, name="magnet")
    _set(registered_apps, cap_key, name=REGISTERED_NAME)
    log.append("Application déclarée dans les « Applications par défaut » de Windows")

    log.extend(register_native_host(native_roots))
    notify_shell()
    return log


def unregister(classes: str = CLASSES, app_key: str = APP_KEY,
               registered_apps: str = REGISTERED_APPS, native_roots=None) -> list[str]:
    """Retire tout ce que register() a écrit — et rien d'autre."""
    _require_windows()
    log: list[str] = []

    # .torrent : notre valeur par défaut et notre entrée « Ouvrir avec », rien d'autre
    ext = classes + r"\.torrent"
    owned = _delete_value_if(ext, PROGID_FILE)
    listed = _delete_value(ext + r"\OpenWithProgids", PROGID_FILE)
    if owned:
        log.append("Association des fichiers .torrent retirée")
    elif listed:
        log.append("Retirée de la liste « Ouvrir avec »")
    _prune(ext + r"\OpenWithProgids", classes)
    _prune(ext, classes)

    # nos ProgId nous appartiennent en entier
    for key in (classes + "\\" + PROGID_FILE, classes + "\\" + PROGID_MAGNET):
        _delete_tree(key)

    # schéma magnet : seulement s'il pointe encore vers nous, et seulement nos valeurs
    magnet = classes + r"\magnet"
    cmd = _get(magnet + r"\shell\open\command")
    if cmd and _is_our_command(cmd):
        _delete_value(magnet + r"\shell\open\command", "")
        _prune(magnet + r"\shell\open\command", magnet)
        icon = icon_path()
        _delete_value_if(magnet + r"\DefaultIcon", '"%s",0' % icon)
        _prune(magnet + r"\DefaultIcon", magnet)
        if not _has_subkeys(magnet):
            # plus aucun autre programme ne s'y appuie : le schéma entier peut partir
            _delete_value_if(magnet, "URL:Magnet Link")
            _delete_value_if(magnet, "", name="URL Protocol")
            _prune(magnet, classes)
        log.append("Association des liens magnet retirée")

    _delete_value(registered_apps, REGISTERED_NAME)
    _delete_tree(app_key)
    log.append("Déclaration « Applications par défaut » retirée")

    log.extend(unregister_native_host(native_roots))
    notify_shell()
    return log


def _has_subkeys(path: str) -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ)
    except OSError:
        return False
    try:
        return winreg.QueryInfoKey(key)[0] > 0
    finally:
        winreg.CloseKey(key)


def _is_our_command(command: str) -> bool:
    """La commande lance-t-elle CETTE application (quel que soit l'interpréteur) ?"""
    script = _script_in_command(command)
    return bool(script) and os.path.basename(script).lower() == "torrent_uploader.py"


def status(classes: str = CLASSES, native_roots=None) -> dict:
    """État courant, pour affichage dans l'interface."""
    if os.name != "nt":
        return {"supported": False}
    try:
        cmd_file, cmd_uri = commands()
    except IntegrationError as exc:
        return {"supported": True, "error": str(exc)}

    ours_file = _get(classes + "\\" + PROGID_FILE + r"\shell\open\command")
    torrent_owner = _get(classes + r"\.torrent")
    magnet_cmd = _get(classes + r"\magnet\shell\open\command")
    user_choice = _get(FILEEXTS_USERCHOICE, "ProgId")
    magnet_choice = _get(MAGNET_USERCHOICE, "ProgId")
    icon = icon_path() if Path(icon_path()).is_file() else ""
    icon_ok = (not icon) or _get(classes + "\\" + PROGID_FILE + r"\DefaultIcon") == '"%s",0' % icon

    installed = bool(ours_file)
    registered = installed and ours_file == cmd_file
    other_script = _script_in_command(ours_file)
    # une AUTRE copie de l'application, toujours présente sur le disque, tient l'enregistrement
    foreign = (installed and not registered and bool(other_script)
               and os.path.normcase(other_script) != os.path.normcase(script_path())
               and Path(other_script).is_file())
    host = native_host_status(native_roots)
    # UserChoice, quand il existe, l'emporte sur tout le reste dans Windows.
    if user_choice:
        torrent_active = registered and user_choice == PROGID_FILE
    else:
        torrent_active = registered and torrent_owner == PROGID_FILE
    if magnet_choice:
        magnet_active = registered and (magnet_choice == PROGID_MAGNET
                                        or (magnet_choice == "magnet" and magnet_cmd == cmd_uri))
    else:
        magnet_active = registered and magnet_cmd == cmd_uri
    host_ok = host["ok"] or not host["applicable"]

    return {
        "supported": True,
        "installed": installed,
        "registered": registered,
        "foreign": foreign,
        "foreign_script": other_script if foreign else "",
        "stale": installed and not registered and not foreign,
        "torrent_active": torrent_active,
        "magnet_active": magnet_active,
        "torrent_owner": torrent_owner,
        "user_choice": user_choice,
        "magnet_user_choice": magnet_choice,
        "open_with_listed": _get(classes + r"\.torrent\OpenWithProgids", PROGID_FILE) is not None,
        "native_host": host,
        "icon_ok": icon_ok,
        "up_to_date": registered and host_ok and icon_ok
                      and (magnet_cmd == cmd_uri or bool(magnet_choice)),
        "effective_command": assoc_command(".torrent"),
        "effective_magnet_command": assoc_command("magnet"),
        "expected_command": cmd_file,
        "expected_magnet_command": cmd_uri,
        "launcher": python_launcher(),
        "script": script_path(),
    }


def ensure_current() -> list[str]:
    """Remet l'enregistrement à niveau si l'utilisateur l'avait déjà activé.

    Cas visés : mise à jour de l'application (nouvel hôte natif, icône), Python
    réinstallé ailleurs, dossier de l'application déplacé. On ne fait RIEN si
    l'intégration n'a jamais été activée, ni si une autre copie de l'application,
    encore présente, la détient.
    """
    if os.name != "nt" or is_sandboxed():
        return []
    try:
        st = status()
        if st.get("error") or not st.get("installed") or st.get("foreign") or st.get("up_to_date"):
            return []
        return register()
    except Exception:
        return []


def open_default_apps_settings():
    """Ouvre Paramètres > Applications par défaut sur la fiche de l'application."""
    if os.name == "nt":
        os.startfile("ms-settings:defaultapps?registeredAppUser=" + REGISTERED_NAME)


def create_desktop_shortcut() -> Path:
    """Crée « Torrent Uploader » sur le Bureau. On peut y déposer des .torrent."""
    _require_windows()
    link = desktop_folder() / (APP_NAME + ".lnk")

    def q(text) -> str:                   # littéral PowerShell entre apostrophes
        return "'" + str(text).replace("'", "''") + "'"

    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(%s); "
        "$s.TargetPath = %s; $s.Arguments = %s; $s.WorkingDirectory = %s; "
        "$s.Description = %s; %s $s.Save()"
        % (q(link), q(python_launcher()), q('"%s"' % script_path()), q(APP_DIR),
           q("Envoyer des torrents vers la seedbox"),
           ("$s.IconLocation = %s;" % q(icon_path())) if Path(icon_path()).is_file() else "")
    )
    done = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        capture_output=True, timeout=40,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if done.returncode != 0 or not link.exists():
        raise IntegrationError("Impossible de créer le raccourci : %s"
                               % (done.stderr.decode("utf-8", "replace").strip()[:200] or "erreur inconnue"))
    return link


def _owner(st: dict) -> str:
    """Programme qui gère les .torrent à notre place, s'il est connu."""
    owner = st.get("user_choice") or st.get("torrent_owner") or ""
    return "" if owner == PROGID_FILE else str(owner)


def describe(st: dict) -> str:
    """Résumé d'une ligne de status()."""
    if not st.get("supported"):
        return "Intégration disponible uniquement sous Windows."
    if st.get("error"):
        return st["error"]
    if st.get("foreign"):
        return "Une autre copie de l'application est enregistrée (%s)." % st["foreign_script"]
    if not st.get("installed"):
        return "Non activée."
    if st.get("stale"):
        return "Enregistrement à mettre à jour : cliquez sur « Activer »."
    parts = [
        "Fichiers .torrent : " + ("actif" if st["torrent_active"] else
                                  "inactif (géré par « %s »)" % _owner(st)
                                  if _owner(st) else "inactif (aucun programme associé)"),
        "liens magnet : " + ("actif" if st["magnet_active"] else "inactif"),
    ]
    host = st.get("native_host") or {}
    if host.get("applicable"):
        parts.append("extension de navigateur : " + ("prête" if host.get("ok") else "à activer"))
    return " · ".join(parts)
