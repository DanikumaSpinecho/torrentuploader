# -*- coding: utf-8 -*-
"""Hôte de messagerie native : relais entre l'extension de navigateur et l'application.

Le navigateur lance ce programme à chaque message de l'extension et dialogue
avec lui sur stdin/stdout (longueur sur 4 octets, puis JSON en UTF-8). Windows
garantit que seul le navigateur, pour l'extension déclarée, peut l'invoquer :
aucun jeton à recopier, aucun port à ouvrir vers l'extérieur.

Messages compris :
    {"action": "ping"}                      -> état de l'application
    {"action": "show"}                      -> ouvre / met au premier plan
    {"action": "add", "items": [...]}       -> ajoute des torrents à la file
                                               (mêmes formes que le service local)

Si l'application est fermée, elle est lancée, puis le message lui est transmis.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import time

from . import APP_NAME, __version__
from .config import load_config
from .paths import log_error
from .service import ask_running_instance, get_token, resolve_port

MAX_MESSAGE = 64 * 1024 * 1024
LAUNCH_TIMEOUT = 25.0            # démarrage de Python + tkinter sur une machine lente


def read_message(stream):
    """Lit un message. Retourne None en fin de flux."""
    header = stream.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("<I", header)
    if length > MAX_MESSAGE:
        raise ValueError("message trop volumineux")
    data = stream.read(length)
    if len(data) < length:
        return None
    return json.loads(data.decode("utf-8"))


def write_message(stream, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    stream.write(struct.pack("<I", len(data)))
    stream.write(data)
    stream.flush()


def launch_app() -> bool:
    """Démarre l'application, détachée du navigateur qui nous a lancés."""
    from . import winintegration as wi
    from .paths import EXE_PATH, FROZEN
    env = None
    if FROZEN:
        command = [str(EXE_PATH)]
        # nouvelle instance INDÉPENDANTE : sinon elle réutiliserait le dossier
        # temporaire de l'hôte, effacé dès que celui-ci se termine
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    else:
        try:
            command = [wi.python_launcher(), wi.script_path()]
        except Exception as exc:
            log_error("hôte natif : interpréteur introuvable", exc)
            return False
    detached = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    breakaway = 0x01000000                       # CREATE_BREAKAWAY_FROM_JOB
    options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, close_fds=True, env=env)
    # Le navigateur peut placer ses hôtes dans un « job » : on tente d'en sortir
    # pour que l'application survive à la fin de l'hôte, sinon on lance tel quel.
    for flags in ((detached | breakaway, detached) if os.name == "nt" else (0,)):
        try:
            subprocess.Popen(command, creationflags=flags, **options)
            return True
        except OSError:
            continue
    log_error("hôte natif : impossible de lancer l'application")
    return False


def _ask(route: str, items=None, wait: float = 0.0):
    """Interroge l'application ; `wait` laisse le temps à un démarrage en cours."""
    token, port = get_token(), resolve_port(load_config())
    deadline = time.time() + wait
    while True:
        answer = ask_running_instance(route, token, port, items, retries=1, timeout=0.5)
        if answer is not None or time.time() >= deadline:
            return answer
        time.sleep(0.3)


def handle(message) -> dict:
    """Traite un message de l'extension et retourne la réponse."""
    if not isinstance(message, dict):
        return {"ok": False, "error": "message invalide"}
    action = message.get("action")
    base = {"app": APP_NAME, "version": __version__}

    if action == "ping":
        answer = _ask("/ping")
        if answer is None:
            return {**base, "ok": True, "running": False}
        if answer.get("status") != 200 or answer.get("ok") is not True:
            return {**base, "ok": False, "running": False,
                    "error": answer.get("error") or "l'application a refusé la connexion"}
        return {**base, "ok": True, "running": True}

    if action not in ("show", "add"):
        return {**base, "ok": False, "error": "action inconnue"}

    items = None
    if action == "add":
        items = message.get("items")
        if not isinstance(items, list) or not items:
            return {**base, "ok": False, "error": "aucun élément"}
    route = "/add" if action == "add" else "/show"

    launched = False
    answer = _ask(route, items)
    if answer is None:
        launched = launch_app()
        if not launched:
            return {**base, "ok": False, "launched": False,
                    "error": "impossible de lancer l'application"}
        answer = _ask(route, items, wait=LAUNCH_TIMEOUT)
    if answer is None:
        return {**base, "ok": False, "launched": launched,
                "error": "l'application ne répond pas"}
    if answer.get("status") != 200:
        return {**base, "ok": False, "launched": launched,
                "error": answer.get("error") or "requête refusée par l'application"}
    ok = answer.get("ok") is True and (action == "show" or answer.get("accepted", 0) > 0)
    result = {**base, "ok": ok, "launched": launched,
              "accepted": answer.get("accepted", 0), "errors": answer.get("errors", [])}
    if not ok and not result["errors"]:
        result["error"] = "requête refusée par l'application"
    return result


def main() -> int:
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    try:
        message = read_message(stdin)
        if message is None:
            return 0
        write_message(stdout, handle(message))
    except Exception as exc:
        log_error("hôte natif", exc)
        try:
            write_message(stdout, {"ok": False, "error": "erreur interne de l'hôte"})
        except Exception:
            pass
        return 1
    return 0
