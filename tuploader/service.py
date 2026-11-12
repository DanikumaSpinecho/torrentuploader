# -*- coding: utf-8 -*-
"""Service local : instance unique et point d'entrée des clients.

Un petit serveur HTTP écoute sur la boucle locale. Il remplit deux rôles :

* verrou d'instance unique — un second lancement de l'application y transmet
  ses arguments puis se termine ;
* porte d'entrée pour l'hôte de messagerie native (extension de navigateur) et,
  en secours, pour une extension configurée à la main avec le jeton.

Toute requête doit présenter le jeton : sans lui, rien n'est accepté.
"""

from __future__ import annotations

import base64
import hmac
import http.server
import json
import os
import re
import secrets
import socket
import socketserver
import threading
import time
from pathlib import Path

from . import APP_NAME, __version__
from .paths import inbox_dir, log_error, token_path
from .torrentfile import parse_magnet, parse_torrent

IPC_HOST = "127.0.0.1"
IPC_PORT = 38471

MAX_BODY = 16 * 1024 * 1024          # corps JSON (un .torrent en base64 y tient largement)
MAX_TORRENT = 8 * 1024 * 1024        # taille d'un .torrent décodé
MAX_ITEMS = 200


def _read_token(path) -> str:
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return existing if len(existing) >= 16 else ""


def get_token() -> str:
    """Secret partagé, créé au premier lancement. Sans lui, aucune requête n'est acceptée.

    Création exclusive : l'Explorateur lance un processus par fichier sélectionné, et
    deux premiers lancements simultanés doivent aboutir au même jeton.
    """
    path = token_path()
    existing = _read_token(path)
    if existing:
        return existing
    token = secrets.token_hex(16)
    tmp = path.with_name("%s.%s.tmp" % (path.name, secrets.token_hex(4)))
    try:
        tmp.write_text(token, encoding="utf-8")
        try:
            os.link(tmp, path)              # échoue si un autre processus l'a créé entre-temps
            return token
        except FileExistsError:
            pass
        except OSError:                     # système de fichiers sans liens physiques
            try:
                with open(path, "x", encoding="utf-8") as fh:
                    fh.write(token)
                return token
            except FileExistsError:
                pass
    except OSError as exc:
        log_error("écriture du jeton", exc)
        return token
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    for _ in range(25):                     # un autre processus finit peut-être d'écrire
        existing = _read_token(path)
        if existing:
            return existing
        time.sleep(0.02)
    # fichier présent mais durablement invalide (trop court, tronqué) : on le remplace
    try:
        tmp.write_text(token, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log_error("remplacement du jeton", exc)
    return token


def resolve_port(cfg: dict | None = None) -> int:
    """Port du service local : configurable (clé « ipc_port »), défaut 38471."""
    try:
        port = int((cfg or {}).get("ipc_port") or IPC_PORT)
    except (TypeError, ValueError):
        return IPC_PORT
    return port if 1024 <= port <= 65535 else IPC_PORT


# --------------------------------------------------------------------------- #
#  Validation de ce qui arrive de l'extérieur
# --------------------------------------------------------------------------- #

def _safe_name(raw) -> str:
    """Nom de fichier sûr à partir d'un nom fourni par une source externe."""
    name = os.path.basename(str(raw or "recu").replace("\\", "/"))
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    name = re.sub(r"\.{2,}", ".", name).strip("._") or "recu"
    name = name[:80]
    if not name.lower().endswith(".torrent"):
        name += ".torrent"
    return name


def _store(raw: bytes, name) -> str:
    """Enregistre un .torrent reçu dans le dossier « recus » et vérifie qu'il est valide."""
    if len(raw) > MAX_TORRENT or not raw.startswith(b"d"):
        raise ValueError("contenu .torrent invalide")
    dest = inbox_dir() / ("%s_%s" % (secrets.token_hex(4), _safe_name(name)))
    dest.write_bytes(raw)
    try:
        parse_torrent(dest)               # écarte une page HTML déguisée en .torrent
    except Exception:
        dest.unlink(missing_ok=True)
        raise ValueError("contenu .torrent invalide")
    return str(dest)


def materialize(entry) -> str:
    """Transforme un élément reçu en chemin local, lien magnet ou URL.

    Formes acceptées :
      "magnet:?xt=urn:btih:..."                    lien magnet
      "https://site/x.torrent"                     URL (la seedbox la téléchargera)
      "C:\\dossier\\x.torrent"                      fichier local
      {"path": "C:\\...\\x.torrent", "consume": true}  fichier local ; « consume »
                                                    en fait une copie, l'original
                                                    pouvant ensuite être supprimé
      {"name": "x.torrent", "data_b64": "..."}     contenu du fichier
    """
    if isinstance(entry, dict) and entry.get("data_b64"):
        try:
            raw = base64.b64decode(entry["data_b64"], validate=True)
        except Exception:
            raise ValueError("contenu .torrent invalide (base64)")
        return _store(raw, entry.get("name"))

    consume = False
    if isinstance(entry, dict) and entry.get("path"):
        consume = bool(entry.get("consume"))
        entry = entry["path"]

    if not isinstance(entry, str):
        raise ValueError("élément non reconnu")

    text = entry.strip()
    if text.lower().startswith("magnet:"):
        parse_magnet(text)                # valide avant d'aller plus loin
        return text
    if re.match(r"^https?://", text, re.I):
        return text

    path = Path(text)
    if path.suffix.lower() != ".torrent" or not path.is_file():
        raise ValueError("élément refusé : %s" % text[:80])
    if not consume:
        return str(path)
    if path.stat().st_size > MAX_TORRENT:
        raise ValueError("fichier trop volumineux pour un .torrent")
    return _store(path.read_bytes(), path.name)


# --------------------------------------------------------------------------- #
#  Serveur
# --------------------------------------------------------------------------- #

class _LocalServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False          # SO_REUSEADDR casserait le verrou sous Windows

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except (AttributeError, OSError):
            pass                          # hors Windows : allow_reuse_address=False suffit
        # On évite HTTPServer.server_bind : son socket.getfqdn() peut déclencher une
        # résolution DNS et allonger la fenêtre entre bind() et listen(), pendant
        # laquelle une instance concurrente se ferait refuser la connexion.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

    def handle_error(self, request, client_address):
        pass                              # pas de trace sur stderr (inexistant sous pythonw)


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "TorrentUploader/" + __version__
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _extension_origin(self):
        """N'autorise la lecture des réponses qu'à une extension de navigateur.

        Une page web ordinaire ne doit rien pouvoir lire ici, même si elle devine
        l'adresse du service.
        """
        origin = self.headers.get("Origin") or ""
        return origin if origin.startswith("chrome-extension://") else None

    def _host_ok(self) -> bool:
        """Rejette une requête dont l'en-tête Host n'est pas la boucle locale.

        Parade au « DNS rebinding » : un nom de domaine contrôlé par un tiers qui
        résoudrait vers 127.0.0.1 arriverait ici avec son propre Host.
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1", "")

    def _send(self, code: int, payload: dict, close: bool = False):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if close:                         # corps non lu : la connexion ne peut pas resservir
            self.close_connection = True
            self.send_header("Connection", "close")
        origin = self._extension_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        origin = self._extension_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if not self._host_ok():
            self._send(403, {"ok": False, "error": "hôte non autorisé"}, close=True)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send(400, {"ok": False, "error": "corps de requête invalide"}, close=True)
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
            if not isinstance(payload, dict):
                raise ValueError
        except Exception:
            self._send(400, {"ok": False, "error": "JSON invalide"})
            return

        given = str(payload.get("token") or "")
        if not hmac.compare_digest(given.encode("utf-8"), self.server.token.encode("utf-8")):
            self._send(403, {"ok": False, "error": "jeton invalide"})
            return

        route = self.path.rstrip("/")
        if route == "/ping":
            self._send(200, {"ok": True, "app": APP_NAME, "version": __version__})
        elif route == "/show":
            self._notify(self.server.on_show)
            self._send(200, {"ok": True})
        elif route == "/add":
            self._add(payload)
        else:
            self._send(404, {"ok": False, "error": "route inconnue"})

    def _notify(self, callback, *args):
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as exc:
            log_error("notification de l'interface", exc)

    def _add(self, payload: dict):
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            self._send(400, {"ok": False, "error": "aucun élément"})
            return
        accepted, errors = [], []
        for entry in items[:MAX_ITEMS]:
            try:
                accepted.append(materialize(entry))
            except Exception as exc:
                errors.append(str(exc))
        errors.extend("élément %d ignoré : %d au plus par envoi" % (n + 1, MAX_ITEMS)
                      for n in range(MAX_ITEMS, len(items)))
        if accepted:
            self._notify(self.server.on_items, accepted)
        self._send(200, {"ok": bool(accepted), "accepted": len(accepted), "errors": errors})


def start_local_server(on_items, token: str, port: int | None = None, on_show=None):
    """Tente de prendre le port. Retourne le serveur, ou None s'il est déjà pris."""
    port = IPC_PORT if port is None else port
    try:
        server = _LocalServer((IPC_HOST, port), _Handler)
    except OSError:
        return None
    server.token = token
    server.on_items = on_items
    server.on_show = on_show
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --------------------------------------------------------------------------- #
#  Client (second lancement de l'application, hôte natif)
# --------------------------------------------------------------------------- #

def post_local(route: str, payload: dict, port: int, timeout: float = 0.6):
    """Requête HTTP minimale sur la boucle locale. Retourne (statut, corps JSON ou None).

    urllib ne permet pas de borner la phase de connexion : sous Windows, un port
    fermé met environ deux secondes à refuser, ce qui figerait le démarrage.
    """
    body = json.dumps(payload).encode("utf-8")
    head = (
        "POST %s HTTP/1.1\r\nHost: %s:%d\r\n"
        "Content-Type: application/json\r\nContent-Length: %d\r\n"
        "Connection: close\r\n\r\n" % (route, IPC_HOST, port, len(body))
    ).encode("ascii")
    sock = socket.create_connection((IPC_HOST, port), timeout=timeout)
    try:
        sock.settimeout(max(timeout, 5.0))      # une fois connecté, on laisse le temps de répondre
        sock.sendall(head + body)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        sock.close()
    data = b"".join(chunks)
    header, _, content = data.partition(b"\r\n\r\n")
    parts = header.split(b"\r\n", 1)[0].split(b" ")
    if len(parts) < 2 or not parts[1].isdigit():
        raise OSError("réponse HTTP illisible")
    try:
        parsed = json.loads(content.decode("utf-8", "replace"))
    except ValueError:
        parsed = None
    return int(parts[1]), parsed if isinstance(parsed, dict) else None


def ask_running_instance(route: str, token: str, port: int | None = None, items=None,
                         retries: int = 8, delay: float = 0.15, timeout: float = 0.6):
    """Interroge l'instance ouverte. Retourne sa réponse, ou None si personne ne répond.

    L'explorateur lance un processus par fichier sélectionné : un perdant peut
    frapper entre le bind() et le listen() du gagnant, d'où les tentatives.
    Une réponse autre que 200 (jeton refusé...) est retournée avec "ok": False :
    quelqu'un écoute, mais ce n'est pas une instance compatible.
    """
    port = IPC_PORT if port is None else port
    payload = {"token": token}
    if items is not None:
        payload["items"] = list(items)
    for _ in range(retries):
        try:
            status, body = post_local(route, payload, port, timeout)
        except Exception:
            time.sleep(delay)
            continue
        if body is None:
            return {"ok": False, "status": status, "incompatible": True,
                    "error": "un autre programme occupe le port de l'application"}
        body = dict(body)
        body["status"] = status
        if status != 200:
            body["ok"] = False
        return body
    return None


def forward_to_running_instance(items, token: str, port: int | None = None,
                                retries: int = 8, delay: float = 0.15,
                                timeout: float = 0.6) -> bool:
    """Transmet des éléments à l'instance ouverte (ou la fait passer au premier plan).

    Retourne True si une instance compatible a répondu.
    """
    items = list(items or [])
    answer = ask_running_instance("/add" if items else "/show", token, port, items or None,
                                  retries, delay, timeout)
    return bool(answer) and answer.get("status") == 200 and not answer.get("incompatible")
