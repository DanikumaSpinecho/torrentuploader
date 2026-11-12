# -*- coding: utf-8 -*-
"""Outils communs aux tests.

À IMPORTER EN PREMIER dans chaque module de test : ce fichier redirige toutes
les données de l'application (configuration, jeton, journal) vers un dossier
temporaire AVANT que le paquet ne soit importé. Aucun test ne lit ni n'écrit
les réglages réels de l'utilisateur, et aucun ne contacte autre chose que la
boucle locale.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import xmlrpc.client
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

SANDBOX = Path(tempfile.mkdtemp(prefix="tu-tests-"))
os.environ["TORRENT_UPLOADER_DATA_DIR"] = str(SANDBOX)
atexit.register(shutil.rmtree, str(SANDBOX), True)

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tuploader import paths                      # noqa: E402
from tuploader.rutorrent import RuTorrentClient  # noqa: E402

assert paths.is_sandboxed(), "les tests doivent tourner en bac à sable"
RuTorrentClient.RETRY_DELAYS = (0.02, 0.04, 0.08)       # nouvelles tentatives rapides

ULTRA_500 = (b'<!DOCTYPE html><html><head><meta name="author" content="Ultra.cc">'
             b'<title>Error 500: Application Error | Ultra.cc</title></head>'
             b'<body><h1>Error 500</h1>Application Error</body></html>')


# --------------------------------------------------------------------------- #
#  Fichiers .torrent de test
# --------------------------------------------------------------------------- #

def bencode(obj) -> bytes:
    if isinstance(obj, bool):
        raise TypeError("pas de booléen en bencode")
    if isinstance(obj, int):
        return b"i%de" % obj
    if isinstance(obj, bytes):
        return b"%d:%s" % (len(obj), obj)
    if isinstance(obj, str):
        return bencode(obj.encode("utf-8"))
    if isinstance(obj, list):
        return b"l" + b"".join(bencode(x) for x in obj) + b"e"
    if isinstance(obj, dict):
        return b"d" + b"".join(bencode(k) + bencode(obj[k]) for k in sorted(obj)) + b"e"
    raise TypeError(type(obj))


def torrent_bytes(name: str = "Exemple", size: int = 3_145_728, files: int = 1) -> bytes:
    info = {b"name": name.encode("utf-8"), b"piece length": 262144, b"pieces": b"\0" * 20}
    if files > 1:
        info[b"files"] = [{b"length": size // files, b"path": [("f%02d.bin" % i).encode()]}
                          for i in range(files)]
    else:
        info[b"length"] = size
    return b"d8:announce23:http://tracker.test/ann4:info" + bencode(info) + b"e"


def make_torrent(folder, name: str = "Exemple", size: int = 3_145_728, files: int = 1) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name + ".torrent")
    path.write_bytes(torrent_bytes(name, size, files))
    return path


def temp_dir(prefix: str = "tu-") -> Path:
    """Dossier temporaire supprimé à la fin de la session de tests."""
    folder = Path(tempfile.mkdtemp(prefix=prefix, dir=str(SANDBOX)))
    return folder


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_config(**values) -> None:
    """Écrit le config.json du bac à sable (jamais celui de l'utilisateur)."""
    paths.config_path().write_text(json.dumps(values), encoding="utf-8")


# --------------------------------------------------------------------------- #
#  Fausse seedbox ruTorrent
# --------------------------------------------------------------------------- #

class FakeSeedbox:
    """Serveur local imitant ruTorrent : mode=list, XML-RPC brut, addtorrent, getplugins.

    mode :
      "ok"        tout fonctionne
      "poisoned"  cache ruTorrent corrompu : mode=list et addtorrent en 500, XML-RPC
                  brut fonctionnel ; un GET getplugins.php « répare » (repasse en ok)
      "dead"      rTorrent injoignable : XML-RPC brut renvoie « false », le reste 500
      "noraw"     serveur sans mode brut (répond du JSON aux requêtes XML)
      "fault"     le mode brut renvoie une erreur XML-RPC
      "unauth"    401 partout
    """

    def __init__(self):
        self.mode = "ok"
        # (hash, étiquette url-encodée, d.directory, nom, multi-fichiers 0/1)
        self.torrents = [
            ("AAAA", "S%C3%A9ries", "/home/u/downloads/rtorrent/series/Show S01", "Show S01", 1),
            ("BBBB", "Films", "/home/u/downloads/rtorrent/films", "film.mkv", 0),
            ("CCCC", "", "/home/u/downloads/rtorrent", "doc.pdf", 0),
        ]
        self.present: set[str] = set()       # empreintes ajoutées pendant le test
        self.land_on_add = True              # un ajout (même en erreur) arrive-t-il dans rTorrent ?
        self.added_hash = ""                 # empreinte à « faire arriver » lors d'un ajout
        self.calls: list[str] = []
        self.requests: list[dict] = []
        self.list_plan: list[str] = []       # réponses forcées : "500" | "401" | "garbage" | "empty"
        self.add_plan: list[str] = []        # "500" | "error"
        self.raw_failures = 0
        self.slow = 0.0
        self.inflight = 0
        self.max_inflight = 0
        self._lock = threading.Lock()
        box = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, body, ctype="text/html"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _enter(self, kind, body=b""):
                box.calls.append(kind)
                box.requests.append({"kind": kind, "method": self.command, "path": self.path,
                                     "headers": {k.lower(): v for k, v in self.headers.items()},
                                     "body": body})
                with box._lock:
                    box.inflight += 1
                    box.max_inflight = max(box.max_inflight, box.inflight)
                if box.slow:
                    time.sleep(box.slow)

            def _leave(self):
                with box._lock:
                    box.inflight -= 1

            def do_GET(self):
                self._enter("GET " + self.path.rsplit("/", 1)[-1])
                try:
                    if box.mode == "unauth":
                        return self._reply(401, b"no")
                    if self.path.endswith("getplugins.php"):
                        if box.mode == "poisoned":
                            box.mode = "ok"              # le rechargement répare le cache
                        return self._reply(200, b"theWebUI.plugins = [];", "application/javascript")
                    self._reply(404, b"")
                finally:
                    self._leave()

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                is_xml = body.lstrip().startswith(b"<?xml")
                if self.path.endswith("action.php"):
                    kind = "raw" if is_xml else "list"
                elif self.path.endswith("addtorrent.php"):
                    kind = "add"
                else:
                    kind = "other"
                if kind == "raw":
                    try:
                        kind = "raw:" + xmlrpc.client.loads(body)[1]
                    except Exception:
                        pass
                self._enter(kind, body)
                try:
                    if box.mode == "unauth":
                        return self._reply(401, b"no")
                    if kind.startswith("raw"):
                        return self._raw(body)
                    if kind == "list":
                        return self._list()
                    if kind == "add":
                        return self._add()
                    self._reply(404, b"")
                finally:
                    self._leave()

            def _rows(self):
                rows = list(box.torrents)
                rows += [(h, "", "/home/u/downloads/rtorrent/x", "x", 0) for h in sorted(box.present)]
                return rows

            def _list(self):
                step = box.list_plan.pop(0) if box.list_plan else ""
                if step == "500" or (not step and box.mode in ("poisoned", "dead")):
                    return self._reply(500, ULTRA_500)
                if step == "401":
                    return self._reply(401, b"no")
                if step == "garbage":
                    return self._reply(200, b"<html>maintenance</html>")
                t = {}
                if step != "empty":
                    for h, label, directory, name, multi in self._rows():
                        row = ["0"] * 34
                        row[4], row[14] = name, label
                        row[25] = directory if multi else directory + "/" + name
                        t[h] = row + [str(multi), directory]
                self._reply(200, json.dumps({"t": t, "cid": 1}).encode(), "application/json")

            def _add(self):
                step = box.add_plan.pop(0) if box.add_plan else ""
                if box.land_on_add and box.added_hash and box.mode != "dead":
                    box.present.add(box.added_hash.upper())
                if step == "500" or (not step and box.mode in ("poisoned", "dead")):
                    return self._reply(500, ULTRA_500)
                if step == "error":
                    return self._reply(200, b"<script>theWebUI.showError('Bad torrent');</script>")
                self._reply(200, b"<html><body>ok</body></html>")

            def _raw(self, body):
                if box.raw_failures > 0:
                    box.raw_failures -= 1
                    return self._reply(500, ULTRA_500)
                if box.mode == "dead":
                    return self._reply(200, b"false", "application/json")
                if box.mode == "noraw":
                    return self._reply(200, b'{"t":[]}', "application/json")
                if box.mode == "fault":
                    return self._reply(200, xmlrpc.client.dumps(
                        xmlrpc.client.Fault(-506, "Method not defined"),
                        methodresponse=True).encode(), "text/xml")
                params, method = xmlrpc.client.loads(body)
                if method == "system.client_version":
                    result = "0.9.8"
                elif method == "directory.default":
                    result = "~/downloads/rtorrent"
                elif method == "d.multicall2":
                    keys = ("d.hash=", "d.custom1=", "d.directory=", "d.name=", "d.is_multi_file=")
                    result = [[dict(zip(keys, row))[f] for f in params[2:]] for row in self._rows()]
                else:
                    result = 0
                self._reply(200, xmlrpc.client.dumps((result,), methodresponse=True).encode(),
                            "text/xml")

        class Server(ThreadingMixIn, HTTPServer):
            daemon_threads = True

        self._server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/rutorrent" % self._server.server_port

    def reset(self, mode: str = "ok"):
        self.mode = mode
        self.present = set()
        self.calls, self.requests = [], []
        self.list_plan, self.add_plan = [], []
        self.raw_failures = 0
        self.slow = 0.0
        self.max_inflight = 0
        self.land_on_add = True
        self.added_hash = ""
        RuTorrentClient._raw_unsupported.clear()

    def client(self, timeout: int = 5) -> RuTorrentClient:
        return RuTorrentClient(self.url, "u", "p", timeout=timeout)

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


# --------------------------------------------------------------------------- #
#  Tests d'interface (tkinter, sans jamais bloquer)
# --------------------------------------------------------------------------- #

class TkTestCase(unittest.TestCase):
    """Fournit self.root (fenêtre cachée) et neutralise les boîtes de dialogue.

    Les boîtes modales bloqueraient les tests : elles sont remplacées par des
    bouchons qui consignent leurs appels dans self.dialogs.
    """

    def setUp(self):
        import tkinter as tk
        from tkinter import messagebox
        self.dialogs: list[tuple] = []
        self._saved = {}
        for name, answer in (("showinfo", "ok"), ("showwarning", "ok"), ("showerror", "ok"),
                             ("askyesno", True)):
            self._saved[name] = getattr(messagebox, name)
            setattr(messagebox, name,
                    lambda *a, _n=name, _r=answer, **k: (self.dialogs.append((_n, a, k)), _r)[1])
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        from tkinter import messagebox
        for name, func in self._saved.items():
            setattr(messagebox, name, func)
        try:
            self.root.destroy()
        except Exception:
            pass

    def pump(self, seconds: float = 0.2):
        """Fait tourner la boucle d'événements pendant `seconds`."""
        end = time.time() + seconds
        while time.time() < end:
            self.root.update()
            time.sleep(0.01)

    def pump_until(self, condition, timeout: float = 10.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            self.root.update()
            if condition():
                return True
            time.sleep(0.02)
        self.root.update()
        return bool(condition())
