# -*- coding: utf-8 -*-
"""Test de bout en bout : extension Chrome -> hôte natif -> application.

    python tests/e2e/run_e2e.py --extension <dossier de l'extension> [--headful]

Nécessite Node.js et Google Chrome. Tout se passe dans un bac à sable :

* données de l'application dans un dossier temporaire (aucun réglage réel lu) ;
* Chrome lancé avec un profil temporaire ;
* faux trackers sur la boucle locale.

Une seule écriture hors du bac à sable, inévitable : Chrome ne trouve les hôtes
natifs que par le registre. La clé
    HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\com.torrentuploader.host
est donc pointée sur l'hôte de test le temps du test, puis RESTAURÉE à
l'identique (ou supprimée si elle n'existait pas). Le scénario « application
fermée » ouvre brièvement une vraie fenêtre de l'application (bac à sable),
fermée ensuite automatiquement.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SANDBOX = Path(tempfile.mkdtemp(prefix="tu-e2e-"))
os.environ["TORRENT_UPLOADER_DATA_DIR"] = str(SANDBOX)
sys.path.insert(0, str(ROOT))

import winreg                                               # noqa: E402

from tests.helpers import torrent_bytes                     # noqa: E402
from tuploader import paths, winintegration as wi           # noqa: E402
from tuploader.service import get_token, start_local_server  # noqa: E402
from tuploader.torrentfile import parse_torrent             # noqa: E402

CHROME_KEY = r"Software\Google\Chrome\NativeMessagingHosts" + "\\" + wi.HOST_NAME


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- #
#  Faux tracker : téléchargements publics et privés (cookie de session)
# --------------------------------------------------------------------------- #

def tracker(host: str):
    files = {
        "/dl/capture-simple.torrent": ("Capture Simple.torrent", "Capture Simple"),
        "/dl/capture-supprimee.torrent": ("Capture Supprimee.torrent", "Capture Supprimee"),
        "/dl/non-capture.torrent": ("Non Capture.torrent", "Non Capture"),
        "/private/prive.torrent": ("Prive.torrent", "Torrent Prive"),
        "/private/repli.torrent": ("Repli.torrent", "Torrent Repli"),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/login":
                return self._send(200, b"<p>connecte</p>", "text/html",
                                  {"Set-Cookie": "session=ok; Path=/"})
            if path == "/page":
                return self._send(200, b'<a href="/private/prive.torrent">t</a>', "text/html")
            if path == "/dl/document.pdf":
                return self._send(200, b"%PDF-1.4 faux", "application/pdf",
                                  {"Content-Disposition": 'attachment; filename="document.pdf"'})
            if path in files:
                if path.startswith("/private/") and "session=ok" not in (self.headers.get("Cookie") or ""):
                    return self._send(403, b"<html>connectez-vous</html>", "text/html")
                filename, name = files[path]
                return self._send(200, torrent_bytes(name), "application/x-bittorrent",
                                  {"Content-Disposition": 'attachment; filename="%s"' % filename})
            self._send(404, b"", "text/html")

    class Server(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    srv = Server((host, 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://%s:%d" % (host, srv.server_port)


# --------------------------------------------------------------------------- #

def read_key():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, CHROME_KEY)
    except OSError:
        return None
    try:
        return winreg.QueryValueEx(key, "")[0]
    except OSError:
        return None
    finally:
        winreg.CloseKey(key)


def restore_key(previous):
    if previous is None:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, CHROME_KEY)
        except OSError:
            pass
    else:
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, CHROME_KEY)
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, previous)
        winreg.CloseKey(key)


def pid_listening(port: int):
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                         encoding="oem", errors="replace").stdout or ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1] == "127.0.0.1:%d" % port and parts[3] == "LISTENING":
            return int(parts[4])
    return None


def run_node(scenario: str, env: dict, headful: bool) -> list:
    out = SANDBOX / ("resultats-%s.json" % scenario)
    cmd = ["node", str(HERE / "extension.mjs")] + (["--headful"] if headful else [])
    subprocess.run(cmd, env={**os.environ, **env, "SCENARIO": scenario, "OUT": str(out)},
                   timeout=240)
    return json.loads(out.read_text(encoding="utf-8")) if out.exists() else [
        {"name": "scénario %s" % scenario, "ok": False, "detail": "aucun résultat"}]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extension", required=True)
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--screenshots", help="dossier où enregistrer les captures de la notice")
    args = ap.parse_args()

    port = free_port()
    paths.config_path().write_text(json.dumps({
        "url": "http://127.0.0.1:9/rutorrent", "auto_refresh": False, "ipc_port": port}),
        encoding="utf-8")
    token = get_token()
    trk1, url1 = tracker("127.0.0.1")
    trk2, url2 = tracker("127.0.0.2")          # hors des permissions de l'extension
    dl_dir = SANDBOX / "telechargements"
    dl_dir.mkdir()
    env = {"EXT_DIR": str(Path(args.extension).resolve()), "EXT_ID": wi.EXTENSION_IDS[0],
           "TRK1": url1, "TRK2": url2, "DL_DIR": str(dl_dir)}
    if args.screenshots:
        Path(args.screenshots).mkdir(parents=True, exist_ok=True)
        env["SHOTS"] = str(Path(args.screenshots).resolve())

    previous = read_key()
    results, received = [], []
    try:
        wi.register_native_host(roots=[("Chrome", CHROME_KEY)])

        # ---- scénario 1 : application ouverte -------------------------------
        server = start_local_server(lambda items: received.extend(items), token, port)
        assert server is not None, "port de test occupé"
        results += run_node("running", env, args.headful)
        server.shutdown()
        server.server_close()

        def landed(pred):
            return [x for x in received if pred(x)]
        names = []
        for item in received:
            if not item.lower().startswith("magnet:"):
                try:
                    names.append(parse_torrent(Path(item))["name"])
                except Exception:
                    names.append("illisible:" + item)
        inbox = paths.inbox_dir().resolve()
        results.append({"name": "application : éléments reçus", "ok": True,
                        "detail": json.dumps(names + [x[:40] for x in received if x.startswith("magnet:")],
                                             ensure_ascii=False)})
        checks = [
            ("application : le .torrent capturé est lu à son emplacement d'origine",
             any(Path(x).parent.resolve() == dl_dir.resolve() and "Capture Simple" in x for x in received)),
            ("application : la capture « suppression » arrive en copie dans « recus »",
             any(Path(x).parent.resolve() == inbox and "Capture_Supprimee" in x for x in received)),
            ("application : le torrent privé lu dans la page est reçu (contenu valide)",
             "Torrent Prive" in names),
            ("application : le torrent du repli par téléchargement est reçu", "Torrent Repli" in names),
            ("application : le magnet est reçu", bool(landed(lambda x: x.startswith("magnet:")))),
            ("application : rien de plus que prévu", len(received) == (6 if args.screenshots else 5)),
        ]
        results += [{"name": n, "ok": bool(ok), "detail": ""} for n, ok in checks]

        # ---- scénario 2 : application fermée --------------------------------
        results += run_node("closed", env, args.headful)
        pid = pid_listening(port)
        results.append({"name": "l'application lancée par l'hôte écoute bien", "ok": pid is not None,
                        "detail": "pid %s" % pid})
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    finally:
        restore_key(previous)
        trk1.shutdown()
        trk2.shutdown()

    restored = read_key() == previous
    results.append({"name": "registre restauré à l'identique", "ok": restored,
                    "detail": "absent" if previous is None else previous})
    print()
    failed = [r for r in results if not r["ok"]]
    for r in results:
        print("%s %s%s" % ("[OK]   " if r["ok"] else "[ECHEC]", r["name"],
                           ("  " + r["detail"]) if r.get("detail") and not r["ok"] else ""))
    print("\n%d vérifications, %d échec(s)" % (len(results), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
