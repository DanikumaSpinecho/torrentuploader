#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Torrent Uploader - envoie des fichiers .torrent vers une seedbox ruTorrent / rTorrent.

Usage :
    python torrent_uploader.py                       -> ouvre l'interface
    python torrent_uploader.py fichier1.torrent ...  -> pre-remplit la file d'attente
    (ou glissez-deposez des .torrent sur le raccourci "Torrent Uploader.bat")

Dependances : uniquement la bibliotheque standard (tkinter).
Optionnel : `pip install tkinterdnd2` pour le glisser-deposer DANS la fenetre.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import http.server
import json
import os
import queue
import re
import secrets
import socket
import socketserver
import ssl
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookiejar import CookieJar
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
    DND_AVAILABLE = True
except Exception:  # dependance optionnelle
    DND_AVAILABLE = False

try:
    import windows_integration as wi
except Exception:  # module absent ou plateforme non Windows
    wi = None


APP_NAME = "Torrent Uploader"
APP_VERSION = "2.0"

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

CONFIG_PATH = BASE_DIR / "config.json"
TOKEN_PATH = BASE_DIR / "ipc_token.txt"
CRASH_LOG = BASE_DIR / "erreurs.log"

# Canal local : sert a la fois de verrou d'instance unique et d'entree pour
# l'extension Chrome. Port fixe, hors plage ephemere.
IPC_HOST = "127.0.0.1"
IPC_PORT = 38471


def _ensure_std_streams():
    """Sous pythonw.exe, stdout/stderr/stdin valent None : toute ecriture planterait."""
    devnull = os.devnull
    if getattr(sys, "stdout", None) is None:
        sys.stdout = open(devnull, "w", encoding="utf-8")
    if getattr(sys, "stderr", None) is None:
        sys.stderr = open(devnull, "w", encoding="utf-8")
    if getattr(sys, "stdin", None) is None:
        sys.stdin = open(devnull, "r", encoding="utf-8")


def log_error(context: str, exc: BaseException | None = None):
    """Journalise dans un fichier : lance par l'explorateur, il n'y a aucune console."""
    try:
        with open(CRASH_LOG, "a", encoding="utf-8") as fh:
            fh.write("\n[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), context))
            if exc is not None:
                fh.write("".join(traceback.format_exception(
                    type(exc), exc, exc.__traceback__)))
    except Exception:
        pass

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
}


# --------------------------------------------------------------------------- #
#  Decodage bencode / lecture des .torrent
# --------------------------------------------------------------------------- #

class BencodeError(Exception):
    pass


def _bdecode(data: bytes, pos: int):
    """Decode une valeur bencode a partir de `pos`. Retourne (valeur, position_suivante)."""
    if pos >= len(data):
        raise BencodeError("fin de donnees inattendue")
    c = data[pos:pos + 1]

    if c == b"i":
        end = data.find(b"e", pos)
        if end < 0:
            raise BencodeError("entier non termine")
        return int(data[pos + 1:end]), end + 1

    if c == b"l":
        pos += 1
        out = []
        while data[pos:pos + 1] != b"e":
            value, pos = _bdecode(data, pos)
            out.append(value)
        return out, pos + 1

    if c == b"d":
        pos += 1
        out = {}
        while data[pos:pos + 1] != b"e":
            key, pos = _bdecode(data, pos)
            value, pos = _bdecode(data, pos)
            out[key] = value
        return out, pos + 1

    if c.isdigit():
        colon = data.find(b":", pos)
        if colon < 0:
            raise BencodeError("chaine mal formee")
        length = int(data[pos:colon])
        start = colon + 1
        return data[start:start + length], start + length

    raise BencodeError("octet inattendu %r a la position %d" % (c, pos))


def parse_torrent(path: Path) -> dict:
    """Lit un .torrent et retourne nom, taille, nombre de fichiers et infohash."""
    raw = path.read_bytes()
    if raw[:1] != b"d":
        raise BencodeError("ce fichier n'est pas un .torrent valide")

    pos = 1
    meta: dict = {}
    info_span = None
    while raw[pos:pos + 1] != b"e":
        key, pos = _bdecode(raw, pos)
        start = pos
        value, pos = _bdecode(raw, pos)
        if key == b"info":
            info_span = (start, pos)
        meta[key] = value

    if info_span is None or not isinstance(meta.get(b"info"), dict):
        raise BencodeError("section « info » absente du .torrent")

    info = meta[b"info"]
    infohash = hashlib.sha1(raw[info_span[0]:info_span[1]]).hexdigest().upper()

    name_raw = info.get(b"name.utf-8") or info.get(b"name") or b"(sans nom)"
    name = name_raw.decode("utf-8", "replace") if isinstance(name_raw, bytes) else str(name_raw)

    if b"files" in info and isinstance(info[b"files"], list):
        files = info[b"files"]
        size = sum(int(f.get(b"length", 0)) for f in files if isinstance(f, dict))
        nfiles = len(files)
    else:
        size = int(info.get(b"length", 0))
        nfiles = 1

    tracker = meta.get(b"announce", b"")
    tracker = tracker.decode("utf-8", "replace") if isinstance(tracker, bytes) else ""

    return {
        "path": path,
        "name": name,
        "size": size,
        "files": nfiles,
        "infohash": infohash,
        "private": bool(info.get(b"private", 0)),
        "tracker": tracker,
    }


class MagnetError(Exception):
    pass


# Un magnet arrive du shell : on le traite comme une entree hostile.
MAGNET_RE = re.compile(
    r"^magnet:\?.*\bxt=urn:btih:(?:[0-9a-fA-F]{40}|[A-Za-z2-7]{32})\b", re.I
)


def parse_magnet(link: str) -> dict:
    """Analyse un lien magnet. Retourne nom, infohash 40 hex majuscules et trackers."""
    link = (link or "").strip().strip('"')
    if not link.lower().startswith("magnet:"):
        raise MagnetError("ce lien n'est pas un magnet")
    if not MAGNET_RE.match(link):
        raise MagnetError("lien magnet invalide (xt=urn:btih:... absent ou mal forme)")

    params = urllib.parse.parse_qs(link[len("magnet:"):].lstrip("?"),
                                   keep_blank_values=False)

    infohash = ""
    for xt in params.get("xt", []) + params.get("xt.1", []):
        m = re.match(r"^urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})$", xt.strip())
        if not m:
            continue
        raw = m.group(1)
        if len(raw) == 40:
            infohash = raw.upper()
        else:                                   # base32 -> hex, sinon _confirm() echouerait
            try:
                infohash = base64.b32decode(raw.upper()).hex().upper()
            except Exception:
                continue
        break

    if not infohash:
        raise MagnetError("aucun infohash exploitable dans ce lien magnet")

    names = params.get("dn", [])
    name = (names[0] if names else "").replace("+", " ").strip()
    return {
        "name": name or ("Aimant %s" % infohash[:16]),
        "size": 0,
        "files": 0,
        "infohash": infohash,
        "trackers": params.get("tr", []),
    }


def torrent_is_ready(path: Path, min_age: float = 0.4) -> bool:
    """Vrai si le .torrent est complet, deverrouille et reellement decodable.

    Chrome ecrit un `.crdownload` puis renomme ; d'autres sources ecrivent
    directement. Chrome pose aussi la marque de provenance APRES le renommage,
    d'ou le verrou temporaire qu'on retente au tour suivant.
    """
    try:
        st = path.stat()
    except OSError:
        return False
    if st.st_size == 0 or time.time() - st.st_mtime < min_age:
        return False
    try:
        with open(path, "rb") as fh:
            if fh.read(1) != b"d":
                return False
        parse_torrent(path)
    except Exception:
        return False
    return True


def human_size(n: int) -> str:
    value = float(n or 0)
    for unit in ("o", "Ko", "Mo", "Go", "To", "Po"):
        if value < 1024 or unit == "Po":
            return ("%.0f %s" % (value, unit)) if unit == "o" else ("%.2f %s" % (value, unit))
        value /= 1024
    return "%.2f Po" % value


# --------------------------------------------------------------------------- #
#  Client ruTorrent
# --------------------------------------------------------------------------- #

class RuTorrentError(Exception):
    pass


# Index des champs renvoyes par plugins/httprpc/action.php (mode=list)
FIELD_NAME = 4
FIELD_SIZE = 5
FIELD_LABEL = 14
FIELD_BASE_PATH = 25


class RuTorrentClient:
    def __init__(self, url: str, username: str = "", password: str = "",
                 verify_ssl: bool = True, timeout: int = 60):
        self.base_url = self._normalize(url)
        self.username = username or ""
        self.password = password or ""
        self.verify_ssl = bool(verify_ssl)
        self.timeout = int(timeout or 60)
        self._opener = self._build_opener()

    # -- helpers ---------------------------------------------------------- #

    @staticmethod
    def _normalize(url: str) -> str:
        url = (url or "").strip()
        if not url:
            return ""
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url
        url = url.rstrip("/")
        # On accepte que l'utilisateur colle une URL complete d'endpoint.
        for suffix in ("/php/addtorrent.php", "/plugins/httprpc/action.php", "/index.html"):
            if url.lower().endswith(suffix):
                url = url[: -len(suffix)]
        return url.rstrip("/")

    def _build_opener(self):
        handlers = [urllib.request.HTTPCookieProcessor(CookieJar())]
        if not self.verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        if self.username:
            mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            mgr.add_password(None, self.base_url, self.username, self.password)
            handlers.append(urllib.request.HTTPBasicAuthHandler(mgr))
            handlers.append(urllib.request.HTTPDigestAuthHandler(mgr))
        return urllib.request.build_opener(*handlers)

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {
            "User-Agent": "TorrentUploader/%s" % APP_VERSION,
            "Accept": "*/*",
        }
        if self.username:
            token = base64.b64encode(
                ("%s:%s" % (self.username, self.password)).encode("utf-8")
            ).decode("ascii")
            headers["Authorization"] = "Basic " + token
        if extra:
            headers.update(extra)
        return headers

    def _post(self, endpoint: str, body: bytes, content_type: str) -> str:
        if not self.base_url:
            raise RuTorrentError("Aucune URL de seedbox configuree.")
        url = self.base_url + endpoint
        req = urllib.request.Request(
            url, data=body, headers=self._headers({"Content-Type": content_type}), method="POST"
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            if exc.code in (401, 403):
                raise RuTorrentError(
                    "Authentification refusee (HTTP %d). Verifiez l'identifiant et le mot de passe."
                    % exc.code
                ) from exc
            if exc.code == 404:
                raise RuTorrentError(
                    "Page introuvable (HTTP 404) : %s - l'URL de ruTorrent est probablement incorrecte."
                    % url
                ) from exc
            raise RuTorrentError("Erreur HTTP %d (%s). %s" % (exc.code, exc.reason, detail)) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ssl.SSLError):
                raise RuTorrentError(
                    "Erreur SSL : %s - decochez « Verifier le certificat SSL » si votre seedbox "
                    "utilise un certificat auto-signe." % reason
                ) from exc
            raise RuTorrentError("Connexion impossible : %s" % reason) from exc
        except TimeoutError as exc:
            raise RuTorrentError("Delai d'attente depasse (%d s)." % self.timeout) from exc

    @staticmethod
    def _multipart(fields, files):
        boundary = "----TorrentUploader" + uuid.uuid4().hex
        buf = bytearray()
        for name, value in fields:
            buf += b"--" + boundary.encode() + b"\r\n"
            buf += ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode("utf-8")
            buf += str(value).encode("utf-8") + b"\r\n"
        for name, filename, content in files:
            safe = filename.replace('"', "'")
            buf += b"--" + boundary.encode() + b"\r\n"
            buf += ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                    % (name, safe)).encode("utf-8")
            buf += b"Content-Type: application/x-bittorrent\r\n\r\n"
            buf += content + b"\r\n"
        buf += b"--" + boundary.encode() + b"--\r\n"
        return bytes(buf), "multipart/form-data; boundary=" + boundary

    # -- API -------------------------------------------------------------- #

    def add_torrent(self, torrent_path: Path, directory: str = "",
                    label: str = "", start: bool = True) -> str:
        """Envoie un .torrent. Retourne le message renvoye par ruTorrent."""
        content = Path(torrent_path).read_bytes()

        fields = []
        if directory.strip():
            fields.append(("dir_edit", directory.strip()))
        if label.strip():
            fields.append(("label", label.strip()))
        if not start:
            fields.append(("torrents_start_stopped", "on"))

        body, ctype = self._multipart(
            fields, [("torrent_file", Path(torrent_path).name, content)]
        )
        return self._check_add(self._post("/php/addtorrent.php", body, ctype))

    def add_url(self, uri: str, directory: str = "",
                label: str = "", start: bool = True) -> str:
        """Ajoute un lien magnet ou une URL de .torrent (champ `url` de ruTorrent)."""
        fields = [("url", uri)]
        if directory.strip():
            fields.append(("dir_edit", directory.strip()))
        if label.strip():
            fields.append(("label", label.strip()))
        if not start:
            fields.append(("torrents_start_stopped", "on"))
        body = urllib.parse.urlencode(fields).encode("utf-8")
        return self._check_add(self._post(
            "/php/addtorrent.php", body,
            "application/x-www-form-urlencoded; charset=UTF-8"))

    def _check_add(self, raw: str) -> str:
        lowered = raw.lower()
        for marker in ("showerror", "addtorrentfailed", "bad torrent", "not a valid",
                       "failed to add", "torrent file is invalid", "no such file",
                       "bad link", "invalid url"):
            if marker in lowered:
                raise RuTorrentError(self._clean_html(raw) or marker)
        return self._clean_html(raw)

    @staticmethod
    def _clean_html(raw: str) -> str:
        text = re.sub(r"<script.*?</script>", " ", raw, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        return " ".join(text.split())[:400]

    def list_torrents(self) -> dict:
        body = urllib.parse.urlencode({"mode": "list"}).encode("utf-8")
        raw = self._post("/plugins/httprpc/action.php", body,
                         "application/x-www-form-urlencoded; charset=UTF-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuTorrentError(
                "Reponse inattendue de la seedbox (le plugin httprpc est-il active ?)."
            ) from exc
        return data.get("t", {}) or {}

    def fetch_labels_and_dirs(self):
        torrents = self.list_torrents()
        labels, dirs = set(), set()
        for fields in torrents.values():
            if not isinstance(fields, list):
                continue
            if len(fields) > FIELD_LABEL:
                lbl = urllib.parse.unquote(str(fields[FIELD_LABEL] or "")).strip()
                if lbl:
                    labels.add(lbl)
            if len(fields) > FIELD_BASE_PATH:
                path = str(fields[FIELD_BASE_PATH] or "").strip()
                if path:
                    parent = path.rsplit("/", 1)[0] if "/" in path else path
                    if parent:
                        dirs.add(parent)
        return (sorted(labels, key=str.lower), sorted(dirs, key=str.lower), len(torrents))

    def has_infohash(self, infohash: str) -> bool:
        try:
            return infohash.upper() in {k.upper() for k in self.list_torrents()}
        except RuTorrentError:
            return False

    def test_connection(self) -> str:
        labels, dirs, count = self.fetch_labels_and_dirs()
        return ("Connexion reussie : %d torrent(s), %d etiquette(s), %d dossier(s) detecte(s)."
                % (count, len(labels), len(dirs)))


# --------------------------------------------------------------------------- #
#  Configuration
# --------------------------------------------------------------------------- #

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        log_error("save_config: %s" % exc)


# --------------------------------------------------------------------------- #
#  Canal local : instance unique + point d'entree de l'extension Chrome
# --------------------------------------------------------------------------- #

def get_token() -> str:
    """Secret partage, cree au premier lancement. Sans lui, aucune requete acceptee."""
    try:
        existing = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if len(existing) >= 16:
            return existing
    except Exception:
        pass
    token = secrets.token_hex(16)
    try:
        TOKEN_PATH.write_text(token, encoding="utf-8")
    except Exception as exc:
        log_error("ecriture du jeton: %s" % exc)
    return token


class _LocalServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False          # SO_REUSEADDR casserait le verrou sous Windows

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except (AttributeError, OSError):
            pass                          # non-Windows : allow_reuse_address=False suffit
        # On evite HTTPServer.server_bind : son socket.getfqdn() peut declencher une
        # resolution DNS et allonger la fenetre entre bind() et listen(), pendant
        # laquelle une instance concurrente se ferait refuser la connexion.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

    def handle_error(self, request, client_address):
        pass                              # pas de trace sur stderr (inexistant sous pythonw)


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "TorrentUploader/" + APP_VERSION
    protocol_version = "HTTP/1.1"

    # -- utilitaires --
    def log_message(self, *args):
        pass

    def _extension_origin(self):
        """N'autorise la lecture des reponses qu'a une extension de navigateur.

        Une page web ordinaire ne doit rien pouvoir lire ici, meme si elle devine
        l'adresse du service.
        """
        origin = self.headers.get("Origin") or ""
        return origin if origin.startswith("chrome-extension://") else None

    def _host_ok(self) -> bool:
        """Rejette une requete dont l'en-tete Host n'est pas la boucle locale.

        Parade au « DNS rebinding » : un nom de domaine controle par un tiers qui
        resoudrait vers 127.0.0.1 arriverait ici avec son propre Host.
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1", "")

    def _send(self, code: int, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
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
            self._send(403, {"ok": False, "error": "hote non autorise"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 8 * 1024 * 1024:
            self._send(400, {"ok": False, "error": "corps de requete invalide"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
            if not isinstance(payload, dict):
                raise ValueError
        except Exception:
            self._send(400, {"ok": False, "error": "JSON invalide"})
            return

        expected = self.server.token
        given = str(payload.get("token") or "")
        if not hmac.compare_digest(given, expected):
            self._send(403, {"ok": False, "error": "jeton invalide"})
            return

        if self.path.rstrip("/") == "/ping":
            self._send(200, {"ok": True, "app": APP_NAME, "version": APP_VERSION})
            return
        if self.path.rstrip("/") != "/add":
            self._send(404, {"ok": False, "error": "route inconnue"})
            return

        items = payload.get("items")
        if not isinstance(items, list) or not items:
            self._send(400, {"ok": False, "error": "aucun element"})
            return

        accepted, errors = [], []
        for entry in items[:200]:
            try:
                accepted.append(self._materialize(entry))
            except Exception as exc:
                errors.append(str(exc))

        if accepted:
            try:
                self.server.on_items(accepted)
            except Exception as exc:
                log_error("distribution des elements recus", exc)
        self._send(200, {"ok": bool(accepted), "accepted": len(accepted), "errors": errors})

    @staticmethod
    def _materialize(entry):
        """Transforme un element recu en chemin local, magnet ou URL."""
        if isinstance(entry, str):
            text = entry.strip()
            if text.lower().startswith("magnet:"):
                parse_magnet(text)             # valide avant d'aller plus loin
                return text
            if re.match(r"^https?://", text, re.I):
                return text
            p = Path(text)
            if p.suffix.lower() == ".torrent" and p.is_file():
                return str(p)
            raise ValueError("element refuse : %s" % text[:80])

        if isinstance(entry, dict) and entry.get("data_b64"):
            # L'extension a telecharge le .torrent avec les cookies du site
            # (indispensable pour les trackers prives) et nous envoie les octets.
            raw = base64.b64decode(entry["data_b64"], validate=True)
            if len(raw) > 8 * 1024 * 1024 or not raw.startswith(b"d"):
                raise ValueError("contenu .torrent invalide")
            # Nom fourni par une source externe : on ne garde qu'un composant sur,
            # sans separateur ni suite de points.
            name = os.path.basename(str(entry.get("name") or "recu").replace("\\", "/"))
            name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
            name = re.sub(r"\.{2,}", ".", name).strip("._") or "recu"
            name = name[:80]
            if not name.lower().endswith(".torrent"):
                name += ".torrent"
            folder = Path(tempfile.gettempdir()) / "TorrentUploader"
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / ("%s_%s" % (secrets.token_hex(4), name))
            dest.write_bytes(raw)
            parse_torrent(dest)                # refuse une page HTML deguisee
            return str(dest)

        raise ValueError("element non reconnu")


def start_local_server(on_items, token: str):
    """Tente de prendre le port. Retourne le serveur, ou None s'il est deja pris."""
    try:
        server = _LocalServer((IPC_HOST, IPC_PORT), _Handler)
    except OSError:
        return None
    server.token = token
    server.on_items = on_items
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _post_local(path: str, payload: dict, timeout: float):
    """Requete HTTP minimale sur la boucle locale, avec delai reellement maitrise.

    urllib ne permet pas de borner la phase de connexion : sous Windows, un port
    ferme met environ deux secondes a refuser, ce qui figerait le demarrage.
    """
    body = json.dumps(payload).encode("utf-8")
    head = (
        "POST %s HTTP/1.1\r\nHost: %s:%d\r\n"
        "Content-Type: application/json\r\nContent-Length: %d\r\n"
        "Connection: close\r\n\r\n" % (path, IPC_HOST, IPC_PORT, len(body))
    ).encode("ascii")
    sock = socket.create_connection((IPC_HOST, IPC_PORT), timeout=timeout)
    try:
        sock.settimeout(timeout)
        sock.sendall(head + body)
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        sock.close()
    data = b"".join(chunks)
    status_line, _, _ = data.partition(b"\r\n")
    parts = status_line.split(b" ")
    if len(parts) < 2 or not parts[1].isdigit():
        raise OSError("reponse HTTP illisible")
    return int(parts[1])


def forward_to_running_instance(items, token: str,
                                retries: int = 8, delay: float = 0.15,
                                timeout: float = 0.6) -> bool:
    """Transmet les arguments a l'instance deja ouverte.

    L'explorateur lance un processus par fichier selectionne : un perdant peut
    frapper entre le bind() et le listen() du gagnant, d'ou les tentatives.
    """
    payload = {"token": token, "items": list(items or [])}
    path = "/add" if payload["items"] else "/ping"
    for _ in range(retries):
        try:
            status = _post_local(path, payload, timeout)
        except Exception:
            time.sleep(delay)
            continue
        if status == 200:
            return True
        return False          # quelqu'un repond mais refuse : ce n'est pas nous
    return False


# --------------------------------------------------------------------------- #
#  Fenetre de parametres
# --------------------------------------------------------------------------- #

class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, cfg: dict):
        super().__init__(parent)
        self.title("Parametres de la seedbox")
        self.resizable(False, False)
        self.transient(parent)
        self.result = None
        self.cfg = dict(cfg)

        pad = {"padx": 10, "pady": 5}
        frm = ttk.Frame(self, padding=14)
        frm.grid(sticky="nsew")

        ttk.Label(frm, text="URL de ruTorrent").grid(row=0, column=0, sticky="w", **pad)
        self.var_url = tk.StringVar(value=cfg.get("url", ""))
        ttk.Entry(frm, textvariable=self.var_url, width=52).grid(row=0, column=1, **pad)
        ttk.Label(frm, text="ex. https://maseedbox.com/rutorrent", foreground="#666") \
            .grid(row=1, column=1, sticky="w", padx=10)

        ttk.Label(frm, text="Identifiant").grid(row=2, column=0, sticky="w", **pad)
        self.var_user = tk.StringVar(value=cfg.get("username", ""))
        ttk.Entry(frm, textvariable=self.var_user, width=52).grid(row=2, column=1, **pad)

        ttk.Label(frm, text="Mot de passe").grid(row=3, column=0, sticky="w", **pad)
        self.var_pass = tk.StringVar(value=cfg.get("password", ""))
        self.entry_pass = ttk.Entry(frm, textvariable=self.var_pass, width=52, show="*")
        self.entry_pass.grid(row=3, column=1, **pad)

        self.var_show = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Afficher le mot de passe", variable=self.var_show,
                        command=self._toggle_pass).grid(row=4, column=1, sticky="w", padx=10)

        self.var_ssl = tk.BooleanVar(value=bool(cfg.get("verify_ssl", True)))
        ttk.Checkbutton(frm, text="Verifier le certificat SSL", variable=self.var_ssl) \
            .grid(row=5, column=1, sticky="w", padx=10, pady=(8, 0))

        self.var_auto = tk.BooleanVar(value=bool(cfg.get("auto_refresh", True)))
        ttk.Checkbutton(frm, text="Recuperer etiquettes et dossiers au demarrage",
                        variable=self.var_auto).grid(row=6, column=1, sticky="w", padx=10)

        ttk.Label(frm, text="Delai reseau (s)").grid(row=7, column=0, sticky="w", **pad)
        self.var_timeout = tk.IntVar(value=int(cfg.get("timeout", 60)))
        ttk.Spinbox(frm, from_=10, to=600, increment=10, textvariable=self.var_timeout,
                    width=8).grid(row=7, column=1, sticky="w", padx=10, pady=5)

        self.lbl_test = ttk.Label(frm, text="", wraplength=420, foreground="#0a7a3f")
        self.lbl_test.grid(row=8, column=0, columnspan=2, sticky="w", padx=10, pady=(6, 0))

        bar = ttk.Frame(frm)
        bar.grid(row=9, column=0, columnspan=2, sticky="e", pady=(14, 0))
        self.btn_test = ttk.Button(bar, text="Tester la connexion", command=self._test)
        self.btn_test.pack(side="left", padx=4)
        ttk.Button(bar, text="Annuler", command=self.destroy).pack(side="left", padx=4)
        ttk.Button(bar, text="Enregistrer", command=self._save).pack(side="left", padx=4)

        self.bind("<Return>", lambda e: self._save())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.update_idletasks()
        x = parent.winfo_rootx() + max((parent.winfo_width() - self.winfo_width()) // 2, 0)
        y = parent.winfo_rooty() + 60
        self.geometry("+%d+%d" % (max(x, 0), max(y, 0)))

    def _toggle_pass(self):
        self.entry_pass.configure(show="" if self.var_show.get() else "*")

    def _collect(self) -> dict:
        cfg = dict(self.cfg)
        cfg.update({
            "url": self.var_url.get().strip(),
            "username": self.var_user.get().strip(),
            "password": self.var_pass.get(),
            "verify_ssl": bool(self.var_ssl.get()),
            "auto_refresh": bool(self.var_auto.get()),
            "timeout": int(self.var_timeout.get() or 60),
        })
        return cfg

    def _test(self):
        cfg = self._collect()
        if not cfg["url"]:
            self.lbl_test.configure(text="Indiquez d'abord l'URL de ruTorrent.", foreground="#b00020")
            return
        self.btn_test.configure(state="disabled")
        self.lbl_test.configure(text="Connexion en cours...", foreground="#666")

        def worker():
            client = RuTorrentClient(cfg["url"], cfg["username"], cfg["password"],
                                     cfg["verify_ssl"], cfg["timeout"])
            try:
                msg, color = client.test_connection(), "#0a7a3f"
            except RuTorrentError as exc:
                msg, color = str(exc), "#b00020"
            except Exception as exc:
                msg, color = "Erreur inattendue : %s" % exc, "#b00020"
            self._test_result = (msg, color)   # jamais de widget depuis un thread

        self._test_result = None
        threading.Thread(target=worker, daemon=True).start()
        self.after(150, self._poll_test)

    def _poll_test(self):
        """Sondage depuis le thread principal : tkinter n'est pas thread-safe."""
        result = getattr(self, "_test_result", None)
        if result is None:
            try:
                self.after(150, self._poll_test)
            except tk.TclError:
                pass
            return
        self._test_done(*result)

    def _test_done(self, msg, color):
        try:
            self.lbl_test.configure(text=msg, foreground=color)
            self.btn_test.configure(state="normal")
        except tk.TclError:
            pass

    def _save(self):
        cfg = self._collect()
        if not cfg["url"]:
            messagebox.showwarning(APP_NAME, "L'URL de ruTorrent est obligatoire.", parent=self)
            return
        self.result = cfg
        self.destroy()


# --------------------------------------------------------------------------- #
#  Fenetre d'integration (associations Windows, surveillance, extension Chrome)
# --------------------------------------------------------------------------- #

class IntegrationDialog(tk.Toplevel):
    def __init__(self, parent, app: "App"):
        super().__init__(parent)
        self.app = app
        self.title("Integration Chrome et Windows")
        self.resizable(False, False)
        self.transient(parent)

        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)

        # ---- 1. associations ----
        box1 = ttk.LabelFrame(frm, text="1. Ouvrir les .torrent et les liens magnet "
                                        "avec cette application", padding=10)
        box1.pack(fill="x")
        self.lbl_assoc = ttk.Label(box1, text="", wraplength=560, justify="left")
        self.lbl_assoc.pack(anchor="w", pady=(0, 8))
        row = ttk.Frame(box1)
        row.pack(anchor="w")
        self.btn_on = ttk.Button(row, text="Activer", command=self.do_register)
        self.btn_on.pack(side="left")
        self.btn_off = ttk.Button(row, text="Desactiver", command=self.do_unregister)
        self.btn_off.pack(side="left", padx=6)
        ttk.Button(row, text="Applications par defaut de Windows",
                   command=self.open_settings).pack(side="left")
        ttk.Label(box1, foreground="#666", wraplength=560, justify="left",
                  text="Une fois active : dans Chrome, un clic sur un .torrent telecharge "
                       "l'ouvre ici, et « Toujours ouvrir ce type de fichier » rend "
                       "l'operation automatique. Les liens magnet ouvrent aussi cette "
                       "application.").pack(anchor="w", pady=(8, 0))

        # ---- 2. surveillance du dossier ----
        box2 = ttk.LabelFrame(frm, text="2. Surveiller le dossier de telechargement",
                              padding=10)
        box2.pack(fill="x", pady=10)
        self.var_watch = tk.BooleanVar(value=bool(app.cfg.get("watch_downloads")))
        ttk.Checkbutton(box2, text="Importer automatiquement les nouveaux .torrent",
                        variable=self.var_watch, command=self._toggle_watch) \
            .grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(box2, text="Dossier").grid(row=1, column=0, sticky="w", pady=4)
        self.var_folder = tk.StringVar(value=app.watch_folder_display())
        ttk.Entry(box2, textvariable=self.var_folder, width=52) \
            .grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(box2, text="...", width=3, command=self.pick_folder) \
            .grid(row=1, column=2)
        self.var_autosend = tk.BooleanVar(value=bool(app.cfg.get("watch_auto_send")))
        ttk.Checkbutton(box2, text="Envoyer immediatement, sans attendre un clic "
                                   "(utilise le dossier et l'etiquette affiches)",
                        variable=self.var_autosend, command=self._toggle_watch) \
            .grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(box2, foreground="#666", wraplength=560, justify="left",
                  text="Utile si Chrome telecharge les .torrent sans les ouvrir. "
                       "Ne fonctionne que si les fichiers arrivent bien dans ce dossier "
                       "(votre Chrome est regle sur « demander ou enregistrer »).") \
            .grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        box2.columnconfigure(1, weight=1)

        # ---- 3. extension Chrome ----
        box3 = ttk.LabelFrame(frm, text="3. Extension Chrome (clic droit sur un lien)",
                              padding=10)
        box3.pack(fill="x")
        ttk.Label(box3, text="Jeton a coller dans les options de l'extension :") \
            .grid(row=0, column=0, sticky="w")
        self.var_token = tk.StringVar(value=app.token)
        ent = ttk.Entry(box3, textvariable=self.var_token, width=44, state="readonly")
        ent.grid(row=1, column=0, sticky="ew", pady=4)
        ttk.Button(box3, text="Copier", command=self.copy_token).grid(row=1, column=1, padx=6)
        self.lbl_srv = ttk.Label(box3, text="", foreground="#666", wraplength=560,
                                 justify="left")
        self.lbl_srv.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(box3, text="Ouvrir le dossier de l'extension",
                   command=self.open_ext_folder).grid(row=3, column=0, sticky="w", pady=(8, 0))
        box3.columnconfigure(0, weight=1)

        bar = ttk.Frame(frm)
        bar.pack(fill="x", pady=(14, 0))
        ttk.Button(bar, text="Fermer", command=self.close).pack(side="right")

        self.refresh()
        self.bind("<Escape>", lambda e: self.close())
        self.grab_set()
        self.update_idletasks()
        x = parent.winfo_rootx() + max((parent.winfo_width() - self.winfo_width()) // 2, 0)
        self.geometry("+%d+%d" % (max(x, 0), max(parent.winfo_rooty() + 30, 0)))

    # -- etat --
    def refresh(self):
        if wi is None or os.name != "nt":
            self.lbl_assoc.configure(
                text="Indisponible : ce mecanisme est propre a Windows.")
            self.btn_on.configure(state="disabled")
            self.btn_off.configure(state="disabled")
        else:
            try:
                st = wi.status()
                self.lbl_assoc.configure(text=wi.describe(st))
                self.btn_off.configure(state="normal" if st.get("registered") else "disabled")
            except Exception as exc:
                self.lbl_assoc.configure(text="Etat indisponible : %s" % exc)
        if self.app.server is not None:
            self.lbl_srv.configure(
                text="Service local actif sur http://%s:%d — l'extension parle a cette "
                     "adresse. Le jeton est indispensable : sans lui toute requete est "
                     "refusee." % (IPC_HOST, IPC_PORT))
        else:
            self.lbl_srv.configure(
                text="Service local INACTIF (port %d occupe). L'extension et le "
                     "regroupement en une seule fenetre ne fonctionneront pas." % IPC_PORT)

    def do_register(self):
        try:
            log = wi.register()
        except Exception as exc:
            messagebox.showerror(APP_NAME, "Echec de l'enregistrement :\n\n%s" % exc,
                                 parent=self)
            return
        st = wi.status()
        detail = "\n".join("• " + l for l in log)
        if st.get("effective_command") and "torrent_uploader" not in \
                (st.get("effective_command") or ""):
            detail += ("\n\nWindows continue de repondre :\n%s\nUtilisez « Ouvrir avec > "
                       "Toujours » sur un .torrent pour terminer."
                       % st["effective_command"])
        messagebox.showinfo(APP_NAME, "Enregistrement effectue.\n\n" + detail, parent=self)
        self.refresh()

    def do_unregister(self):
        if not messagebox.askyesno(
                APP_NAME, "Retirer l'association des .torrent et des liens magnet ?",
                parent=self):
            return
        try:
            log = wi.unregister()
        except Exception as exc:
            messagebox.showerror(APP_NAME, "Echec : %s" % exc, parent=self)
            return
        messagebox.showinfo(APP_NAME, "Retire.\n\n" + "\n".join("• " + l for l in log),
                            parent=self)
        self.refresh()

    def open_settings(self):
        try:
            wi.open_default_apps_settings()
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)

    def pick_folder(self):
        folder = filedialog.askdirectory(
            title="Dossier a surveiller", parent=self,
            initialdir=self.var_folder.get() or str(Path.home()))
        if folder:
            self.var_folder.set(folder)
            self._toggle_watch()

    def _toggle_watch(self):
        self.app.cfg["watch_downloads"] = bool(self.var_watch.get())
        self.app.cfg["watch_auto_send"] = bool(self.var_autosend.get())
        folder = self.var_folder.get().strip()
        default = str(self.app.default_watch_folder())
        self.app.cfg["watch_folder"] = "" if folder == default else folder
        save_config(self.app.cfg)
        self.app.restart_watcher()

    def copy_token(self):
        self.clipboard_clear()
        self.clipboard_append(self.app.token)
        self.lbl_srv.configure(text="Jeton copie dans le presse-papiers.")
        self.after(2500, self.refresh)

    def open_ext_folder(self):
        folder = BASE_DIR / "chrome-extension"
        if not folder.exists():
            messagebox.showwarning(APP_NAME, "Dossier introuvable : %s" % folder,
                                   parent=self)
            return
        try:
            os.startfile(str(folder))
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)

    def close(self):
        self._toggle_watch()
        self.destroy()


# --------------------------------------------------------------------------- #
#  Application principale
# --------------------------------------------------------------------------- #

class App:
    def __init__(self, root, initial_items, server=None, token: str = ""):
        self.root = root
        self.cfg = load_config()
        self.items = {}          # iid -> {kind, path|uri, info, status}
        self._server_dirs = []
        self._server_labels = []
        self.busy = False
        self.server = server
        self.token = token or get_token()
        self._watch_thread = None
        self._watch_stop = threading.Event()
        # tkinter n'est pas thread-safe : les threads ne touchent jamais aux
        # widgets, ils deposent une fonction ici et le thread principal l'execute.
        self._inbox = queue.Queue()
        self._closing = False

        self._build_ui()
        self._apply_config()
        self._setup_dnd()

        if initial_items:
            self.add_items(initial_items)

        self.root.after(120, self._pump)
        self.restart_watcher()

        if self.cfg.get("url"):
            if self.cfg.get("auto_refresh", True):
                self.root.after(500, lambda: self.refresh_server(silent=True))
        else:
            self.root.after(400, self.open_settings)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # -- construction de l'interface -------------------------------------- #

    def _build_ui(self):
        self.root.title(APP_NAME)
        self.root.geometry("860x660")
        self.root.minsize(740, 580)

        try:
            style = ttk.Style()
            if "vista" in style.theme_names():
                style.theme_use("vista")
        except Exception:
            pass

        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        # --- en-tete ---
        header = ttk.Frame(outer)
        header.pack(fill="x")
        self.var_server = tk.StringVar(value="-")
        ttk.Label(header, text="Seedbox :").pack(side="left")
        ttk.Label(header, textvariable=self.var_server, font=("Segoe UI", 9, "bold")) \
            .pack(side="left", padx=(4, 10))
        ttk.Button(header, text="Parametres...", command=self.open_settings).pack(side="right")
        ttk.Button(header, text="Integration Chrome...",
                   command=self.open_integration).pack(side="right", padx=6)
        self.btn_refresh = ttk.Button(header, text="Actualiser", command=self.refresh_server)
        self.btn_refresh.pack(side="right", padx=6)

        ttk.Separator(outer).pack(fill="x", pady=8)

        # --- zone de depot ---
        drop_text = ("Glissez-deposez vos fichiers .torrent ici"
                     if DND_AVAILABLE else
                     "Deposez vos .torrent sur « Torrent Uploader.bat »\n"
                     "ou cliquez ici pour les choisir")
        self.drop_zone = tk.Label(
            outer, text=drop_text, height=3, relief="ridge", bd=2,
            bg="#f4f6f8", fg="#33475b", font=("Segoe UI", 10), cursor="hand2",
        )
        self.drop_zone.pack(fill="x")
        self.drop_zone.bind("<Button-1>", lambda e: self.browse_files())

        # --- liste ---
        list_frame = ttk.Frame(outer)
        list_frame.pack(fill="both", expand=True, pady=8)

        cols = ("name", "type", "size", "files", "status")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("name", text="Torrent")
        self.tree.heading("type", text="Type")
        self.tree.heading("size", text="Taille")
        self.tree.heading("files", text="Fichiers")
        self.tree.heading("status", text="Statut")
        self.tree.column("name", width=360, anchor="w")
        self.tree.column("type", width=70, anchor="w")
        self.tree.column("size", width=90, anchor="e")
        self.tree.column("files", width=70, anchor="e")
        self.tree.column("status", width=230, anchor="w")
        self.tree.tag_configure("ok", foreground="#0a7a3f")
        self.tree.tag_configure("err", foreground="#b00020")
        self.tree.tag_configure("run", foreground="#0b5fa5")

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Delete>", lambda e: self.remove_selected())

        btns = ttk.Frame(outer)
        btns.pack(fill="x")
        ttk.Button(btns, text="Ajouter...", command=self.browse_files).pack(side="left")
        ttk.Button(btns, text="Retirer la selection", command=self.remove_selected) \
            .pack(side="left", padx=6)
        ttk.Button(btns, text="Vider la liste", command=self.clear_list).pack(side="left")

        # --- options ---
        opts = ttk.LabelFrame(outer, text="Options d'envoi", padding=10)
        opts.pack(fill="x", pady=(10, 6))
        opts.columnconfigure(1, weight=1)

        ttk.Label(opts, text="Dossier de destination").grid(row=0, column=0, sticky="w", pady=4)
        self.var_dir = tk.StringVar()
        self.cmb_dir = ttk.Combobox(opts, textvariable=self.var_dir, values=[])
        self.cmb_dir.grid(row=0, column=1, sticky="ew", padx=8, pady=4)
        dirbtns = ttk.Frame(opts)
        dirbtns.grid(row=0, column=2, sticky="w")
        ttk.Button(dirbtns, text="+", width=3, command=self.fav_dir_add).pack(side="left")
        ttk.Button(dirbtns, text="-", width=3, command=self.fav_dir_del).pack(side="left", padx=2)

        ttk.Label(opts, text="Etiquette (label)").grid(row=1, column=0, sticky="w", pady=4)
        self.var_label = tk.StringVar()
        self.cmb_label = ttk.Combobox(opts, textvariable=self.var_label, values=[])
        self.cmb_label.grid(row=1, column=1, sticky="ew", padx=8, pady=4)
        lblbtns = ttk.Frame(opts)
        lblbtns.grid(row=1, column=2, sticky="w")
        ttk.Button(lblbtns, text="+", width=3, command=self.fav_label_add).pack(side="left")
        ttk.Button(lblbtns, text="-", width=3, command=self.fav_label_del).pack(side="left", padx=2)

        ttk.Label(opts, text="Laissez le dossier vide pour utiliser celui par defaut de rTorrent. "
                             "Les boutons + / - gerent vos favoris.",
                  foreground="#666").grid(row=2, column=1, columnspan=2, sticky="w", padx=8)

        self.var_start = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Demarrer le torrent immediatement",
                        variable=self.var_start).grid(row=3, column=1, sticky="w",
                                                      padx=8, pady=(8, 0))
        self.var_clear = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Vider la liste apres un envoi reussi",
                        variable=self.var_clear).grid(row=4, column=1, sticky="w", padx=8)

        # --- barre d'action ---
        action = ttk.Frame(outer)
        action.pack(fill="x", pady=(4, 0))
        self.progress = ttk.Progressbar(action, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.btn_send = ttk.Button(action, text="Envoyer vers la seedbox", command=self.send_all)
        self.btn_send.pack(side="right")

        self.var_status = tk.StringVar(value="Pret.")
        ttk.Label(outer, textvariable=self.var_status, foreground="#444", wraplength=800) \
            .pack(fill="x", pady=(8, 0))

    def _setup_dnd(self):
        if not DND_AVAILABLE:
            return
        for widget in (self.drop_zone, self.tree):
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

    def _on_drop(self, event):
        try:
            paths = list(self.root.tk.splitlist(event.data))
        except Exception:
            paths = [event.data]
        self.add_files(paths)

    # -- configuration <-> interface --------------------------------------- #

    def _apply_config(self):
        self.var_server.set(self.cfg.get("url") or "(non configuree)")
        self.var_start.set(bool(self.cfg.get("start_torrents", True)))
        self.var_clear.set(bool(self.cfg.get("clear_after_upload", False)))
        self.var_dir.set(self.cfg.get("last_dir", ""))
        self.var_label.set(self.cfg.get("last_label", ""))
        self._refresh_combos()

    def _refresh_combos(self, server_labels=None, server_dirs=None):
        if server_dirs is not None:
            self._server_dirs = server_dirs
        if server_labels is not None:
            self._server_labels = server_labels
        dirs = list(dict.fromkeys(
            [d for d in self.cfg.get("favorite_dirs", []) if d] + list(self._server_dirs)
        ))
        labels = list(dict.fromkeys(
            [l for l in self.cfg.get("favorite_labels", []) if l] + list(self._server_labels)
        ))
        self.cmb_dir.configure(values=dirs)
        self.cmb_label.configure(values=labels)

    def _client(self) -> RuTorrentClient:
        return RuTorrentClient(
            self.cfg.get("url", ""), self.cfg.get("username", ""),
            self.cfg.get("password", ""), self.cfg.get("verify_ssl", True),
            self.cfg.get("timeout", 60),
        )

    # -- favoris ----------------------------------------------------------- #

    def _fav_toggle(self, key: str, value: str, add: bool):
        value = (value or "").strip()
        if not value:
            return
        lst = list(self.cfg.get(key, []))
        if add and value not in lst:
            lst.append(value)
        elif not add and value in lst:
            lst.remove(value)
        self.cfg[key] = lst
        save_config(self.cfg)
        self._refresh_combos()

    def fav_dir_add(self):
        self._fav_toggle("favorite_dirs", self.var_dir.get(), True)
        self.var_status.set("Dossier ajoute aux favoris.")

    def fav_dir_del(self):
        self._fav_toggle("favorite_dirs", self.var_dir.get(), False)
        self.var_status.set("Dossier retire des favoris.")

    def fav_label_add(self):
        self._fav_toggle("favorite_labels", self.var_label.get(), True)
        self.var_status.set("Etiquette ajoutee aux favoris.")

    def fav_label_del(self):
        self._fav_toggle("favorite_labels", self.var_label.get(), False)
        self.var_status.set("Etiquette retiree des favoris.")

    # -- gestion de la liste ------------------------------------------------ #

    def browse_files(self):
        paths = filedialog.askopenfilenames(
            title="Choisir des fichiers .torrent",
            filetypes=[("Fichiers torrent", "*.torrent"), ("Tous les fichiers", "*.*")],
        )
        if paths:
            self.add_files(list(paths))

    def add_files(self, paths, announce: bool = True):
        added, skipped, errors = 0, 0, []

        expanded = []
        for raw in paths:
            p = Path(str(raw).strip().strip("{}"))
            if p.is_dir():
                expanded.extend(sorted(p.glob("*.torrent")))
            else:
                expanded.append(p)

        for p in expanded:
            if p.suffix.lower() != ".torrent":
                continue
            if not p.exists():
                errors.append("%s : introuvable" % p.name)
                continue
            try:
                info = parse_torrent(p)
            except Exception as exc:
                errors.append("%s : %s" % (p.name, exc))
                continue
            if self._already_queued(info["infohash"], p):
                skipped += 1
                continue
            iid = self.tree.insert("", "end", values=(
                info["name"], "Fichier", human_size(info["size"]),
                info["files"], "En attente"
            ))
            self.items[iid] = {"kind": "file", "path": p, "info": info, "status": "pending"}
            added += 1

        if announce:
            msg = "%d torrent(s) ajoute(s)." % added
            if skipped:
                msg += " %d deja present(s)." % skipped
            self.var_status.set(msg)
            if errors:
                messagebox.showwarning(
                    APP_NAME, "Fichiers ignores :\n\n" + "\n".join(errors[:10]))
        elif errors:
            # Import automatique (surveillance, extension) : jamais de fenetre
            # modale, qui bloquerait l'application sans que personne ne regarde.
            self.var_status.set("Fichier ignore : %s" % errors[0])
            log_error("import automatique : " + " | ".join(errors[:10]))
        return added

    def _already_queued(self, infohash: str, path: Path | None = None) -> bool:
        for item in self.items.values():
            # un infohash vide (lien nu) ne doit jamais servir de cle d'egalite
            if infohash and item["info"].get("infohash") == infohash:
                return True
            if path is not None and item.get("path") and \
                    str(item["path"]).lower() == str(path).lower():
                return True
        return False

    def add_magnet(self, uri: str, announce: bool = True) -> bool:
        """Ajoute un lien magnet a la file."""
        try:
            info = parse_magnet(uri)
        except MagnetError as exc:
            if announce:
                messagebox.showwarning(APP_NAME, "Lien magnet ignore :\n\n%s" % exc)
            return False
        if self._already_queued(info["infohash"]):
            if announce:
                self.var_status.set("Ce lien magnet est deja dans la liste.")
            return False
        iid = self.tree.insert("", "end", values=(
            info["name"], "Aimant", "-", "-", "En attente"
        ))
        self.items[iid] = {"kind": "magnet", "uri": uri, "info": info, "status": "pending"}
        if announce:
            self.var_status.set("Lien magnet ajoute : %s" % info["name"])
        return True

    def add_url(self, url: str, announce: bool = True) -> bool:
        """Ajoute une URL de .torrent : ruTorrent la telechargera lui-meme."""
        name = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])
        info = {"name": name or url[:60], "size": 0, "files": 0, "infohash": ""}
        iid = self.tree.insert("", "end", values=(info["name"], "Lien", "-", "-", "En attente"))
        self.items[iid] = {"kind": "url", "uri": url, "info": info, "status": "pending"}
        if announce:
            self.var_status.set("Lien ajoute : %s" % info["name"])
        return True

    def add_items(self, entries, announce: bool = True) -> int:
        """Point d'entree unique : chemins, magnets et URL melanges.

        L'ordre d'arrivee est conserve : une selection mixte s'affiche telle
        qu'elle a ete deposee.
        """
        count = 0
        for raw in entries:
            text = str(raw).strip().strip('"').strip("{}")
            if not text:
                continue
            if text.lower().startswith("magnet:"):
                count += 1 if self.add_magnet(text, announce=False) else 0
            elif re.match(r"^https?://", text, re.I):
                count += 1 if self.add_url(text, announce=False) else 0
            else:
                count += self.add_files([text], announce=False)
        if announce:
            self.var_status.set("%d element(s) ajoute(s) a la file." % count)
        return count

    # -- passerelle thread -> interface -------------------------------------- #

    def post(self, fn):
        """Fait executer fn() par le thread principal. Appelable depuis un thread."""
        self._inbox.put(fn)

    def _pump(self):
        """Draine la file des taches deposees par les threads."""
        while True:
            try:
                fn = self._inbox.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as exc:
                log_error("traitement differe", exc)
        if not self._closing:
            self.root.after(120, self._pump)

    def submit_external(self, entries):
        """Entree thread-safe utilisee par le serveur local."""
        self.post(lambda: self.receive_external(list(entries)))

    def receive_external(self, entries):
        """Appele depuis le serveur local (autre instance ou extension Chrome)."""
        added = self.add_items(entries, announce=True)
        self.bring_to_front()
        if added and self.cfg.get("watch_auto_send") and not self.busy:
            self.root.after(300, self.send_all)

    def bring_to_front(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(400, lambda: self.root.attributes("-topmost", False))
            self.root.focus_force()
        except tk.TclError:
            pass

    def remove_selected(self):
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.items.pop(iid, None)
        self.var_status.set("Selection retiree.")

    def clear_list(self):
        for iid in list(self.items):
            self.tree.delete(iid)
        self.items.clear()
        self.progress.configure(value=0)
        self.var_status.set("Liste videe.")

    def _set_status(self, iid: str, text: str, tag: str = ""):
        if iid in self.items:
            try:
                self.tree.set(iid, "status", text)
                self.tree.item(iid, tags=(tag,) if tag else ())
            except tk.TclError:
                pass

    # -- parametres et rafraichissement ------------------------------------ #

    # -- surveillance du dossier de telechargement ------------------------- #

    def default_watch_folder(self) -> Path:
        if wi is not None:
            try:
                return wi.downloads_folder()
            except Exception:
                pass
        return Path.home() / "Downloads"

    def watch_folder(self) -> Path:
        custom = (self.cfg.get("watch_folder") or "").strip()
        return Path(custom) if custom else self.default_watch_folder()

    def watch_folder_display(self) -> str:
        return str(self.watch_folder())

    def restart_watcher(self):
        """(Re)demarre le surveillant selon la configuration courante."""
        self._watch_stop.set()
        self._watch_thread = None
        if not self.cfg.get("watch_downloads"):
            return
        folder = self.watch_folder()
        if not folder.is_dir():
            self.var_status.set("Surveillance impossible : %s est introuvable." % folder)
            return
        self._watch_stop = threading.Event()
        stop = self._watch_stop
        interval = max(1, int(self.cfg.get("watch_interval", 3) or 3))
        self._watch_thread = threading.Thread(
            target=self._watch_loop, args=(folder, stop, interval), daemon=True)
        self._watch_thread.start()
        self.var_status.set("Surveillance active : %s" % folder)

    def _watch_loop(self, folder: Path, stop: threading.Event, interval: int):
        # Amorcage : tout ce qui est deja la est ignore, on ne veut que le nouveau.
        seen = set()
        try:
            for p in folder.glob("*.torrent"):
                seen.add(str(p).lower())
        except OSError:
            pass
        while not stop.wait(interval):
            try:
                candidates = list(folder.glob("*.torrent"))
            except OSError:
                continue
            fresh = []
            for p in candidates:
                key = str(p).lower()
                if key in seen:
                    continue
                if torrent_is_ready(p):      # complet, deverrouille, bencode valide
                    seen.add(key)
                    fresh.append(str(p))
            if fresh and not stop.is_set():
                self.post(lambda f=fresh: self._on_watched(f))

    def _on_watched(self, paths):
        added = self.add_files(paths, announce=False)
        if not added:
            return
        self.var_status.set("%d torrent(s) importe(s) depuis le dossier surveille." % added)
        self.bring_to_front()
        if self.cfg.get("watch_auto_send") and not self.busy and self.cfg.get("url"):
            self.root.after(300, self.send_all)

    # -- fenetres ----------------------------------------------------------- #

    def open_integration(self):
        dlg = IntegrationDialog(self.root, self)
        self.root.wait_window(dlg)

    def open_settings(self):
        dlg = SettingsDialog(self.root, self.cfg)
        self.root.wait_window(dlg)
        if dlg.result:
            self.cfg.update(dlg.result)
            save_config(self.cfg)
            self.var_server.set(self.cfg.get("url") or "(non configuree)")
            self.var_status.set("Parametres enregistres.")
            self.refresh_server(silent=True)

    def refresh_server(self, silent: bool = False):
        if not self.cfg.get("url"):
            if not silent:
                messagebox.showinfo(APP_NAME, "Configurez d'abord l'acces a votre seedbox.")
            return
        self.btn_refresh.configure(state="disabled")
        self.var_status.set("Recuperation des etiquettes et dossiers...")

        client = self._client()

        def worker():
            try:
                labels, dirs, count = client.fetch_labels_and_dirs()
                self.post(lambda: self._on_refreshed(labels, dirs, count, None))
            except Exception as exc:
                err = str(exc)
                self.post(lambda: self._on_refreshed([], [], 0, err))

        threading.Thread(target=worker, daemon=True).start()

    def _on_refreshed(self, labels, dirs, count, error):
        self.btn_refresh.configure(state="normal")
        if error:
            self.var_status.set("Actualisation impossible : %s" % error)
            return
        self._refresh_combos(server_labels=labels, server_dirs=dirs)
        self.var_status.set(
            "Seedbox : %d torrent(s), %d etiquette(s), %d dossier(s) connus."
            % (count, len(labels), len(dirs))
        )

    # -- envoi --------------------------------------------------------------- #

    def send_all(self):
        if self.busy:
            return
        if not self.cfg.get("url"):
            messagebox.showinfo(APP_NAME, "Configurez d'abord l'acces a votre seedbox.")
            self.open_settings()
            return
        pending = [iid for iid, it in self.items.items() if it["status"] != "ok"]
        if not pending:
            messagebox.showinfo(APP_NAME, "Aucun torrent a envoyer.")
            return

        directory = self.var_dir.get().strip()
        label = self.var_label.get().strip()
        start = bool(self.var_start.get())

        self.cfg["last_dir"] = directory
        self.cfg["last_label"] = label
        self.cfg["start_torrents"] = start
        self.cfg["clear_after_upload"] = bool(self.var_clear.get())
        save_config(self.cfg)

        self.busy = True
        self.btn_send.configure(state="disabled")
        self.btn_refresh.configure(state="disabled")
        self.progress.configure(value=0, maximum=len(pending))

        client = self._client()
        threading.Thread(
            target=self._upload_worker,
            args=(client, pending, directory, label, start),
            daemon=True,
        ).start()

    def _upload_worker(self, client, iids, directory, label, start):
        ok = fail = 0
        for index, iid in enumerate(iids, 1):
            item = self.items.get(iid)
            if not item:
                continue
            info = item["info"]
            self.post(lambda i=iid: self._set_status(i, "Envoi en cours...", "run"))
            self.post(lambda n=info["name"]: self.var_status.set("Envoi de « %s »..." % n))
            try:
                if item.get("kind") in ("magnet", "url"):
                    client.add_url(item["uri"], directory, label, start)
                else:
                    client.add_torrent(item["path"], directory, label, start)
                confirmed = bool(info.get("infohash")) and \
                    self._confirm(client, info["infohash"])
                item["status"] = "ok"
                text = "Ajoute (confirme)" if confirmed else "Envoye (non confirme)"
                self.post(lambda i=iid, t=text: self._set_status(i, t, "ok"))
                ok += 1
            except Exception as exc:
                item["status"] = "error"
                msg = str(exc)
                self.post(lambda i=iid, m=msg:
                          self._set_status(i, "Echec : " + m[:150], "err"))
                fail += 1
            self.post(lambda v=index: self.progress.configure(value=v))

        self.post(lambda: self._upload_done(ok, fail))

    @staticmethod
    def _confirm(client: RuTorrentClient, infohash: str, attempts: int = 5) -> bool:
        for _ in range(attempts):
            time.sleep(1.2)
            try:
                if client.has_infohash(infohash):
                    return True
            except Exception:
                return False
        return False

    def _upload_done(self, ok: int, fail: int):
        self.busy = False
        self.btn_send.configure(state="normal")
        self.btn_refresh.configure(state="normal")
        self.var_status.set("Termine : %d reussi(s), %d echec(s)." % (ok, fail))
        if fail == 0 and ok and self.var_clear.get():
            self.clear_list()
        if fail:
            messagebox.showwarning(
                APP_NAME,
                "%d envoi(s) ont echoue. Le detail est indique dans la colonne « Statut »." % fail,
            )

    # -- fermeture ---------------------------------------------------------- #

    def on_close(self):
        self.cfg["last_dir"] = self.var_dir.get().strip()
        self.cfg["last_label"] = self.var_label.get().strip()
        self.cfg["start_torrents"] = bool(self.var_start.get())
        self.cfg["clear_after_upload"] = bool(self.var_clear.get())
        save_config(self.cfg)
        self._closing = True
        self._watch_stop.set()
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
        self.root.destroy()


# --------------------------------------------------------------------------- #
#  Point d'entree
# --------------------------------------------------------------------------- #

class _Sink:
    """Tampon : le serveur peut recevoir avant que la fenetre n'existe."""

    def __init__(self):
        self.buffer = []
        self.target = None

    def __call__(self, items):
        if self.target is not None:
            self.target(items)
        else:
            self.buffer.extend(items)

    def attach(self, target):
        self.target = target
        pending, self.buffer = self.buffer, []
        if pending:
            target(pending)


def parse_argv(argv):
    """Separe les options des cibles et recolle les URI decoupees par le shell.

    Windows ne protege pas les URI : « magnet:?xt=...&dn=Mon Film » arrive
    en plusieurs argv. On les rejoint.
    """
    flags = [a.lower() for a in argv if a.startswith("--")]
    targets = [a for a in argv if not a.startswith("--")]
    if targets and targets[0].strip().lower().startswith("magnet:"):
        targets = [" ".join(targets)]
    return flags, targets


def main():
    _ensure_std_streams()
    flags, targets = parse_argv(sys.argv[1:])

    if wi is not None and os.name == "nt":
        try:
            if "--register" in flags:
                for line in wi.register():
                    print("  +", line)
                return
            if "--unregister" in flags:
                for line in wi.unregister():
                    print("  -", line)
                return
            if "--status" in flags:
                print(wi.describe(wi.status()))
                return
        except Exception as exc:
            print("Erreur : %s" % exc)
            log_error("integration en ligne de commande", exc)
            return

    token = get_token()
    sink = _Sink()
    server = start_local_server(sink, token)

    if server is None:
        # Le port est pris : soit une instance tourne deja, soit un tiers l'occupe.
        if forward_to_running_instance(targets, token):
            return                       # transmis a la fenetre existante
        log_error("port %d occupe par un tiers : ouverture d'une fenetre isolee" % IPC_PORT)

    root = TkinterDnD.Tk() if DND_AVAILABLE else tk.Tk()
    app = App(root, targets, server=server, token=token)
    sink.attach(app.submit_external)   # depot thread-safe dans la file
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:            # sous pythonw, un plantage serait invisible
        log_error("plantage au demarrage", exc)
        try:
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror(
                APP_NAME, "Erreur inattendue :\n\n%s\n\nDetail dans %s" % (exc, CRASH_LOG))
        except Exception:
            pass
        raise
