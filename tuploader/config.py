# -*- coding: utf-8 -*-
"""Configuration de l'utilisateur (config.json)."""

from __future__ import annotations

import copy
import json
import os

from .paths import config_path, log_error

DEFAULT_CONFIG = {
    "url": "",
    "username": "",
    "password": "",
    "verify_ssl": True,
    "timeout": 60,
    "last_dir": "",
    "last_label": "",
    "start_torrents": True,
    "favorite_dirs": [],
    "favorite_labels": [],
    "auto_refresh": True,
    "clear_after_upload": False,
    "watch_downloads": False,
    "watch_folder": "",
    "watch_auto_send": False,
    "watch_interval": 3,
    "ipc_port": 38471,
    "theme": "system",            # system | light | dark
    # dernières listes lues sur la seedbox (affichées même si elle est injoignable)
    "cache_labels": [],
    "cache_dirs": [],
    "cache_time": "",
}


def _same_kind(default, value) -> bool:
    """La valeur lue a-t-elle le type attendu ? (un entier vaut pour un nombre)"""
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, list):
        return isinstance(value, list) and all(isinstance(x, str) for x in value)
    return isinstance(value, type(default))


def load_config() -> dict:
    # copie profonde : les listes par défaut ne doivent jamais être partagées
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    path = config_path()
    if path.exists():
        try:
            # « utf-8-sig » : accepte un fichier enregistré avec BOM (Bloc-notes...)
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            log_error("config.json illisible, valeurs par défaut utilisées", exc)
            data = None
        if isinstance(data, dict):
            for key, value in data.items():
                if key in DEFAULT_CONFIG and _same_kind(DEFAULT_CONFIG[key], value):
                    cfg[key] = value
    return cfg


def save_config(cfg: dict) -> None:
    """Écriture atomique : une coupure en cours d'écriture ne corrompt pas le fichier."""
    path = config_path()
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        log_error("enregistrement de la configuration", exc)
        try:
            tmp.unlink()
        except OSError:
            pass
