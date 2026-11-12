# -*- coding: utf-8 -*-
"""Emplacements des fichiers, journal d'erreurs et migration depuis la v1.

Les données de l'utilisateur (identifiants, jeton, journal) vivent dans son
profil Windows, pas à côté du programme : le dossier de l'application peut ainsi
être copié, partagé ou mis à jour sans jamais emporter un mot de passe.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import traceback
from pathlib import Path

# Dossier qui contient torrent_uploader.py
APP_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = Path(__file__).resolve().parent / "assets"

# Permet aux tests (et à un usage « portable ») de rediriger toutes les données.
ENV_DATA_DIR = "TORRENT_UPLOADER_DATA_DIR"

_LOG_MAX_BYTES = 512 * 1024


def is_sandboxed() -> bool:
    """Vrai si les données sont redirigées (tests) : on ne touche alors à rien de réel."""
    return bool(os.environ.get(ENV_DATA_DIR))


def data_dir() -> Path:
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        folder = Path(override)
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        folder = Path(base) / "TorrentUploader"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        folder = Path(base) / "torrent-uploader"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def config_path() -> Path:
    return data_dir() / "config.json"


def token_path() -> Path:
    return data_dir() / "ipc_token.txt"


def log_path() -> Path:
    return data_dir() / "erreurs.log"


def inbox_dir() -> Path:
    """Copies des .torrent reçus d'un navigateur, conservées jusqu'à l'envoi."""
    folder = data_dir() / "recus"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def native_host_dir() -> Path:
    folder = data_dir() / "native-host"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def migrate_legacy() -> list[str]:
    """Déplace config.json, ipc_token.txt et erreurs.log de la v1 vers le profil.

    La v1 les rangeait à côté du script, donc au milieu du code source.
    """
    moved: list[str] = []
    if is_sandboxed():
        return moved
    for name in ("config.json", "ipc_token.txt", "erreurs.log"):
        old, new = APP_DIR / name, data_dir() / name
        if old.is_file() and not new.exists():
            try:
                shutil.move(str(old), str(new))
                moved.append(name)
            except OSError:
                pass
    return moved


def purge_inbox(max_age_days: float = 7.0) -> None:
    """Supprime les copies de .torrent reçues il y a plus d'une semaine."""
    limit = time.time() - max_age_days * 86400
    try:
        for entry in inbox_dir().iterdir():
            if entry.is_file() and entry.stat().st_mtime < limit:
                entry.unlink()
    except OSError:
        pass


def ensure_std_streams() -> None:
    """Sous pythonw.exe, stdout/stderr/stdin valent None : toute écriture planterait."""
    if getattr(sys, "stdout", None) is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if getattr(sys, "stderr", None) is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if getattr(sys, "stdin", None) is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8")


def log_error(context: str, exc: BaseException | None = None) -> None:
    """Journalise dans un fichier : lancée par l'explorateur, l'application n'a pas de console."""
    try:
        path = log_path()
        try:
            if path.stat().st_size > _LOG_MAX_BYTES:      # on garde la moitié la plus récente
                data = path.read_bytes()[-_LOG_MAX_BYTES // 2:]
                path.write_bytes(data[data.find(b"\n[") + 1:])
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), context))
            if exc is not None:
                fh.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        pass
