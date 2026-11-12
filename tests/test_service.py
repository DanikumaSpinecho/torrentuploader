# -*- coding: utf-8 -*-
"""Tests du service local (instance unique, porte d'entrée HTTP) et de l'hôte
de messagerie native.

Sous-systèmes couverts :
  * tuploader/service.py     (get_token, resolve_port, materialize et la
                              validation des noms reçus, start_local_server et
                              ses routes HTTP, post_local, ask_running_instance,
                              forward_to_running_instance)
  * tuploader/nativehost.py  (read_message, write_message, handle, launch_app
                              avec subprocess entièrement simulé, main)

Sécurité :
  * tous les fichiers vont dans le bac à sable créé par tests/helpers.py ;
  * seule la boucle locale est contactée, sur des ports tirés par
    h.free_port() ; le port réel de l'application (38471) est interdit par une
    garde posée sur service.post_local pendant toute la durée du module, et
    par les assistants HTTP de ce fichier ;
  * nativehost.launch_app est toujours remplacé (le vrai lancement n'a jamais
    lieu) ; les tests de launch_app simulent subprocess.Popen ET l'interpréteur.

Les défauts du produit révélés à l'écriture de ces tests ont été corrigés ; les tests
qui les mettaient en évidence vérifient désormais le comportement corrigé. (DefautsConnusTests)
"""

from tests import helpers as h      # EN PREMIER : redirige les données vers le bac à sable

import base64                                            # noqa: E402
import io                                                # noqa: E402
import json                                              # noqa: E402
import os                                                # noqa: E402
import socket                                            # noqa: E402
import socketserver                                      # noqa: E402
import struct                                            # noqa: E402
import subprocess                                        # noqa: E402
import sys                                               # noqa: E402
import threading                                         # noqa: E402
import time                                              # noqa: E402
import types                                             # noqa: E402
import unittest                                          # noqa: E402
import urllib.error                                      # noqa: E402
import urllib.request                                    # noqa: E402
from collections import namedtuple                       # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer   # noqa: E402
from pathlib import Path                                 # noqa: E402
from unittest import mock                                # noqa: E402

from tuploader import APP_NAME, __version__              # noqa: E402
from tuploader import config, nativehost, paths, service  # noqa: E402
from tuploader import torrentfile as tf                  # noqa: E402

assert paths.is_sandboxed(), "les tests doivent tourner en bac à sable"


# --------------------------------------------------------------------------- #
#  Garde : le port réel de l'application n'est JAMAIS contacté
# --------------------------------------------------------------------------- #

PORTS_INTERDITS = {38471, service.IPC_PORT}
VIOLATIONS: list = []
_POST_LOCAL_ORIGINAL = service.post_local


def _post_local_garde(route, payload, port, timeout=0.6):
    if port is None or int(port) in PORTS_INTERDITS:
        VIOLATIONS.append((route, port))
        raise ConnectionRefusedError("garde des tests : port réel %r interdit" % port)
    return _POST_LOCAL_ORIGINAL(route, payload, port, timeout)


def setUpModule():
    service.post_local = _post_local_garde


def tearDownModule():
    service.post_local = _POST_LOCAL_ORIGINAL


def verifier_port(port) -> int:
    if port is None or int(port) in PORTS_INTERDITS:
        VIOLATIONS.append(("assistant", port))
        raise AssertionError("port réel de l'application interdit dans les tests : %r" % port)
    return int(port)


def port_libre() -> int:
    for _ in range(50):
        port = h.free_port()
        if port not in PORTS_INTERDITS:
            return port
    raise RuntimeError("aucun port de test disponible")


# --------------------------------------------------------------------------- #
#  Données et outils communs
# --------------------------------------------------------------------------- #

HEX40 = "0123456789abcdef0123456789abcdef01234567"
AUTRE40 = "fedcba9876543210fedcba9876543210fedcba98"
MAGNET = "magnet:?xt=urn:btih:" + HEX40
AUTRE_MAGNET = "magnet:?xt=urn:btih:" + AUTRE40
MAGNET_B32 = ("magnet:?xt=urn:btih:" + base64.b32encode(bytes.fromhex(HEX40)).decode("ascii")
              + "&dn=Mon+Film")
MAGNET_INVALIDE = "magnet:?xt=urn:btih:123"
URL = "https://tracker.test/fichiers/exemple.torrent"
JETON_ETRANGER = "f" * 32

Reponse = namedtuple("Reponse", "status headers body data")
_HOTE_DEFAUT = object()
_DEFAUT = object()


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def corps(token, **extra) -> dict:
    return {"token": token, **extra}


def requete_brute(port, methode="POST", chemin="/ping", contenu=b"", entetes=None,
                  hote=_HOTE_DEFAUT) -> bytes:
    """Octets d'une requête HTTP/1.1 (connexion maintenue, sauf en-tête contraire).

    `hote=None` ou une valeur None dans `entetes` supprime l'en-tête concerné.
    """
    if not isinstance(contenu, bytes):
        contenu = json.dumps(contenu).encode("utf-8")
    tetes = {"Host": ("127.0.0.1:%d" % port) if hote is _HOTE_DEFAUT else hote,
             "Content-Type": "application/json"}
    if methode == "POST":
        tetes["Content-Length"] = str(len(contenu))
    tetes.update(entetes or {})
    lignes = ["%s %s HTTP/1.1" % (methode, chemin)]
    lignes += ["%s: %s" % (k, v) for k, v in tetes.items() if v is not None]
    return ("\r\n".join(lignes) + "\r\n\r\n").encode("utf-8") + contenu


def lire_reponse(fichier):
    """Lit UNE réponse HTTP (d'après Content-Length). None si la connexion est close."""
    ligne = fichier.readline()
    if not ligne:
        return None
    morceaux = ligne.decode("latin-1").split(" ", 2)
    if not ligne.startswith(b"HTTP/") or len(morceaux) < 2 or not morceaux[1].isdigit():
        return Reponse(0, {}, ligne, None)          # réponse HTTP/0.9 ou illisible
    status = int(morceaux[1])
    tetes = {}
    while True:
        ligne = fichier.readline()
        if ligne in (b"\r\n", b"\n", b""):
            break
        cle, _, valeur = ligne.decode("latin-1").partition(":")
        tetes[cle.strip().lower()] = valeur.strip()
    taille = int(tetes.get("content-length", "0") or 0)
    contenu = fichier.read(taille) if taille else b""
    try:
        data = json.loads(contenu.decode("utf-8"))
    except ValueError:
        data = None
    return Reponse(status, tetes, contenu, data)


def http(port, methode="POST", chemin="/ping", contenu=b"", entetes=None, hote=_HOTE_DEFAUT):
    """Envoie une requête brute sur la boucle locale et lit une réponse."""
    verifier_port(port)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(requete_brute(port, methode, chemin, contenu, entetes, hote))
        with sock.makefile("rb") as fichier:
            return lire_reponse(fichier)


def lister_inbox() -> list:
    return sorted(p.name for p in paths.inbox_dir().iterdir())


def vider_inbox() -> None:
    dossier = paths.inbox_dir().resolve()
    assert Path(h.SANDBOX).resolve() in dossier.parents, dossier
    for entree in dossier.iterdir():
        if entree.is_file():
            entree.unlink()


def taille_journal() -> int:
    try:
        return paths.log_path().stat().st_size
    except OSError:
        return 0


def torrent_de_taille(total: int) -> bytes:
    """Un .torrent valide d'exactement `total` octets (bourrage dans « pieces »)."""
    def fabriquer(n):
        info = {b"name": b"Gros", b"length": 1, b"piece length": 262144, b"pieces": b"\0" * n}
        return b"d4:info" + h.bencode(info) + b"e"
    n = max(0, total - len(fabriquer(0)))
    for _ in range(5):
        ecart = len(fabriquer(n)) - total
        if ecart == 0:
            break
        n -= ecart
    raw = fabriquer(n)
    assert len(raw) == total, (len(raw), total)
    return raw


def arreter(serveur) -> None:
    """shutdown() + server_close(), sans attendre la fin du sondage de 0,5 s de serve_forever.

    Une connexion vide réveille la boucle du serveur, qui voit aussitôt la
    demande d'arrêt : le démontage de chaque test reste rapide.
    """
    if serveur is None or getattr(serveur, "_arrete_par_test", False):
        return
    serveur._arrete_par_test = True
    port = verifier_port(serveur.server_address[1])
    fil = threading.Thread(target=serveur.shutdown, daemon=True)
    fil.start()
    limite = time.monotonic() + 3
    while fil.is_alive() and time.monotonic() < limite:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                pass
        except OSError:
            pass
        fil.join(0.05)
    fil.join(2)
    serveur.server_close()


class ServeurEtranger:
    """Programme tiers qui occupe le port et répond à tout par une page HTML."""

    def __init__(self, port, code=200, contenu=b"<html><body>Bienvenue</body></html>",
                 ctype="text/html"):
        verifier_port(port)
        etranger = self
        self.requetes = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                etranger.requetes.append((self.path, self.rfile.read(n)))
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(contenu)))
                self.end_headers()
                self.wfile.write(contenu)

        class Serveur(socketserver.ThreadingMixIn, HTTPServer):
            daemon_threads = True

            def server_bind(self):              # pas de socket.getfqdn() (DNS)
                socketserver.TCPServer.server_bind(self)
                self.server_name, self.server_port = self.server_address[:2]

        self._serveur = Serveur(("127.0.0.1", port), Handler)
        threading.Thread(target=self._serveur.serve_forever, daemon=True).start()

    def fermer(self):
        self._serveur.shutdown()
        self._serveur.server_close()


class EcouteurMuet:
    """Accepte les connexions et les referme aussitôt, sans rien répondre."""

    def __init__(self, port):
        verifier_port(port)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(8)
        self.sock.settimeout(0.1)
        self.connexions = 0
        self._stop = threading.Event()
        self._fil = threading.Thread(target=self._boucle, daemon=True)
        self._fil.start()

    def _boucle(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.connexions += 1
            conn.close()

    def fermer(self):
        self._stop.set()
        self._fil.join(2)
        self.sock.close()


# --------------------------------------------------------------------------- #
#  Classe de base
# --------------------------------------------------------------------------- #

class Base(unittest.TestCase):
    """Suit les serveurs démarrés et vérifie que la garde du port réel n'a pas joué."""

    def setUp(self):
        super().setUp()
        self._serveurs = []
        self.recus: list = []          # appels de on_items (une liste par appel)
        self.affichages: list = []     # appels de on_show
        self.token = service.get_token()
        self.addCleanup(self._verifier_garde)
        self.addCleanup(self._arreter_serveurs)

    def _verifier_garde(self):
        if VIOLATIONS:
            vues = list(VIOLATIONS)
            VIOLATIONS.clear()
            self.fail("le port réel de l'application a été visé : %r" % vues)

    def _arreter_serveurs(self):
        for serveur in list(self._serveurs):
            arreter(serveur)

    def _on_items(self, items):
        self.recus.append(list(items))

    def _on_show(self):
        self.affichages.append(True)

    def serveur(self, token=None, port=None, on_items=_DEFAUT, on_show=_DEFAUT):
        """Démarre un service local sur un port de test (jamais le port réel)."""
        on_items = self._on_items if on_items is _DEFAUT else on_items
        on_show = self._on_show if on_show is _DEFAUT else on_show
        token = self.token if token is None else token
        srv = None
        for _ in range(1 if port is not None else 10):
            p = verifier_port(port if port is not None else port_libre())
            srv = service.start_local_server(on_items, token, p, on_show=on_show)
            if srv is not None:
                break
        if srv is not None:
            self._serveurs.append(srv)
        return srv


class BaseServeur(Base):
    """Un service local démarré pour chaque test (self.srv, self.port)."""

    def setUp(self):
        super().setUp()
        self.srv = self.serveur()
        self.assertIsNotNone(self.srv, "impossible de démarrer le service de test")
        self.port = self.srv.server_address[1]

    def post(self, chemin="/ping", **extra):
        return http(self.port, chemin=chemin, contenu=corps(self.token, **extra))


# --------------------------------------------------------------------------- #
#  service.get_token / resolve_port
# --------------------------------------------------------------------------- #

class JetonTests(Base):
    """get_token() : secret partagé stocké dans le bac à sable."""

    def setUp(self):
        super().setUp()
        self.chemin = paths.token_path()
        self.assertIn(Path(h.SANDBOX).resolve(), self.chemin.resolve().parents)
        self.chemin.unlink(missing_ok=True)

    def test_jeton_cree_en_32_hex_dans_le_bac_a_sable(self):
        """Premier appel : 32 caractères hexadécimaux, écrits dans ipc_token.txt du bac à sable."""
        jeton = service.get_token()
        self.assertRegex(jeton, r"^[0-9a-f]{32}$")
        self.assertTrue(self.chemin.is_file())
        self.assertEqual(self.chemin.read_text(encoding="utf-8").strip(), jeton)

    def test_jeton_stable_d_un_appel_a_l_autre(self):
        """Les appels suivants relisent le même jeton sans réécrire le fichier."""
        premier = service.get_token()
        contenu = self.chemin.read_bytes()
        self.assertEqual([service.get_token() for _ in range(3)], [premier] * 3)
        self.assertEqual(self.chemin.read_bytes(), contenu)

    def test_jeton_existant_conserve_et_nettoye(self):
        """Un jeton déjà présent (≥ 16 caractères) est conservé, espaces et fin de ligne ôtés."""
        self.chemin.write_text("  " + "a1" * 10 + "\r\n", encoding="utf-8")
        self.assertEqual(service.get_token(), "a1" * 10)

    def test_jeton_trop_court_remplace(self):
        """Un jeton de moins de 16 caractères est jugé invalide et remplacé."""
        self.chemin.write_text("abc", encoding="utf-8")
        jeton = service.get_token()
        self.assertRegex(jeton, r"^[0-9a-f]{32}$")
        self.assertEqual(self.chemin.read_text(encoding="utf-8").strip(), jeton)

    def test_deux_jetons_crees_sont_differents(self):
        """Le jeton est aléatoire : deux créations successives diffèrent."""
        premier = service.get_token()
        self.chemin.unlink()
        self.assertNotEqual(service.get_token(), premier)


class PortTests(unittest.TestCase):
    """resolve_port() : clé « ipc_port », défaut 38471, plage 1024-65535."""

    def test_valeurs_par_defaut_et_invalides(self):
        """Absente, vide, hors plage ou illisible : on retombe sur le port par défaut."""
        for cfg in (None, {}, {"ipc_port": None}, {"ipc_port": 0}, {"ipc_port": ""},
                    {"ipc_port": 80}, {"ipc_port": 1023}, {"ipc_port": 65536},
                    {"ipc_port": 99999}, {"ipc_port": -1}, {"ipc_port": "abc"},
                    {"ipc_port": [1]}, {"ipc_port": {"a": 1}}, {"autre": 45001}):
            with self.subTest(cfg=cfg):
                self.assertEqual(service.resolve_port(cfg), service.IPC_PORT)

    def test_valeurs_valides(self):
        """Entier ou texte numérique dans la plage : pris tel quel, bornes comprises."""
        for valeur, attendu in ((45001, 45001), ("45001", 45001), (" 45001 ", 45001),
                                (1024, 1024), (65535, 65535)):
            with self.subTest(valeur=valeur):
                self.assertEqual(service.resolve_port({"ipc_port": valeur}), attendu)


# --------------------------------------------------------------------------- #
#  service.start_local_server : verrou d'instance unique
# --------------------------------------------------------------------------- #

class DemarrageServeurTests(Base):

    def test_ecoute_sur_la_boucle_locale(self):
        """Le service n'écoute que sur 127.0.0.1, au port demandé."""
        port = port_libre()
        srv = self.serveur(port=port)
        self.assertIsNotNone(srv)
        self.assertEqual(srv.server_address[:2], ("127.0.0.1", port))
        self.assertEqual(srv.token, self.token)

    def test_second_demarrage_sur_le_meme_port_refuse(self):
        """Le port sert de verrou : un second service sur le même port retourne None."""
        srv = self.serveur()
        port = srv.server_address[1]
        self.assertIsNone(service.start_local_server(self._on_items, self.token, port))
        self.assertEqual(http(port, contenu=corps(self.token)).status, 200)   # le premier vit

    def test_port_libere_apres_arret(self):
        """Après shutdown() + server_close(), le port se reprend immédiatement (même après du trafic)."""
        srv = self.serveur()
        port = srv.server_address[1]
        self.assertEqual(http(port, contenu=corps(self.token)).status, 200)
        self.assertEqual(service.ask_running_instance("/ping", self.token, port, retries=1)["status"],
                         200)
        arreter(srv)
        repris = self.serveur(port=port)
        self.assertIsNotNone(repris, "le port n'a pas été libéré")
        self.assertEqual(http(port, contenu=corps(self.token)).status, 200)

    @unittest.skipUnless(os.name == "nt", "SO_EXCLUSIVEADDRUSE est propre à Windows")
    def test_port_non_detournable_par_so_reuseaddr(self):
        """Tant que le service tient le port, un socket SO_REUSEADDR ne peut pas s'y lier."""
        srv = self.serveur()
        port = srv.server_address[1]
        intrus = socket.socket()
        try:
            intrus.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with self.assertRaises(OSError):
                intrus.bind(("127.0.0.1", port))
        finally:
            intrus.close()

    def test_port_occupe_par_un_tiers(self):
        """Un port déjà tenu par un autre programme : start_local_server retourne None."""
        port = port_libre()
        tiers = socket.socket()
        try:
            tiers.bind(("127.0.0.1", port))
            tiers.listen(1)
            self.assertIsNone(service.start_local_server(self._on_items, self.token, port))
        finally:
            tiers.close()

    def test_un_rappel_lent_ne_bloque_pas_les_autres_requetes(self):
        """Serveur multi-fils : un on_items qui traîne ne fige pas /ping."""
        entre, libere = threading.Event(), threading.Event()

        def lent(items):
            entre.set()
            libere.wait(5)

        srv = self.serveur(on_items=lent)
        port = srv.server_address[1]
        resultat = {}
        fil = threading.Thread(target=lambda: resultat.setdefault(
            "add", http(port, chemin="/add", contenu=corps(self.token, items=[MAGNET]))),
            daemon=True)
        fil.start()
        try:
            self.assertTrue(entre.wait(5), "on_items n'a pas été appelé")
            debut = time.monotonic()
            self.assertEqual(http(port, contenu=corps(self.token)).status, 200)
            self.assertLess(time.monotonic() - debut, 2.0)
        finally:
            libere.set()
            fil.join(5)
        self.assertEqual(resultat["add"].status, 200)


# --------------------------------------------------------------------------- #
#  Routes HTTP du service local
# --------------------------------------------------------------------------- #

class RoutesTests(BaseServeur):

    def test_ping(self):
        """/ping : 200, JSON avec le nom et la version de l'application."""
        rep = self.post("/ping")
        self.assertEqual(rep.status, 200)
        self.assertTrue(rep.headers["content-type"].startswith("application/json"))
        self.assertEqual(rep.data, {"ok": True, "app": APP_NAME, "version": __version__})
        self.assertEqual((self.recus, self.affichages), ([], []))

    def test_ping_par_urllib_sans_proxy(self):
        """Un client HTTP standard (urllib, sans proxy) obtient la même réponse."""
        ouvreur = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        requete = urllib.request.Request(
            "http://127.0.0.1:%d/ping" % verifier_port(self.port),
            data=json.dumps({"token": self.token}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with ouvreur.open(requete, timeout=5) as reponse:
            self.assertEqual(reponse.status, 200)
            self.assertEqual(json.loads(reponse.read())["app"], APP_NAME)
        requete = urllib.request.Request(
            "http://127.0.0.1:%d/ping" % self.port, data=b'{"token": "mauvais"}',
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            ouvreur.open(requete, timeout=5)
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()

    def test_route_avec_barre_finale(self):
        """« /ping/ » équivaut à « /ping »."""
        self.assertEqual(self.post("/ping/").status, 200)

    def test_show_appelle_on_show(self):
        """/show : 200 et le rappel on_show est appelé une fois, on_items jamais."""
        rep = self.post("/show")
        self.assertEqual(rep.status, 200)
        self.assertIs(rep.data["ok"], True)
        self.assertEqual(self.affichages, [True])
        self.assertEqual(self.recus, [])

    def test_show_sans_rappel(self):
        """/show sans on_show (None) : 200 quand même."""
        srv = self.serveur(on_show=None)
        rep = http(srv.server_address[1], chemin="/show", contenu=corps(self.token))
        self.assertEqual(rep.status, 200)

    def test_show_rappel_en_erreur_journalise(self):
        """Un on_show qui plante ne casse pas la réponse ; l'erreur est journalisée."""
        def plante():
            raise RuntimeError("fenêtre détruite")
        srv = self.serveur(on_show=plante)
        avant = taille_journal()
        rep = http(srv.server_address[1], chemin="/show", contenu=corps(self.token))
        self.assertEqual(rep.status, 200)
        self.assertGreater(taille_journal(), avant)

    def test_route_inconnue_404(self):
        """Jeton valide mais route inconnue : 404, aucun rappel."""
        for chemin in ("/inconnue", "/", "/pingx", "/add/plus"):
            with self.subTest(chemin=chemin):
                rep = self.post(chemin, items=[MAGNET])
                self.assertEqual(rep.status, 404)
                self.assertIs(rep.data["ok"], False)
        self.assertEqual((self.recus, self.affichages), ([], []))

    def test_methode_get_refusee_sans_fuite(self):
        """GET n'est pas servi (code d'erreur) et la réponse ne révèle pas le jeton."""
        rep = http(self.port, methode="GET", chemin="/ping")
        self.assertGreaterEqual(rep.status, 400)
        self.assertNotIn(self.token.encode("ascii"), rep.body)

    def test_jeton_absent_ou_faux_403(self):
        """Jeton absent, faux, tronqué, allongé, d'un autre type ou non ASCII : 403."""
        cas = [{}, {"token": ""}, {"token": None}, {"token": "mauvais"},
               {"token": self.token[:-1]}, {"token": self.token + "0"},
               {"token": self.token.upper()}, {"token": 0}, {"token": [self.token]},
               {"token": {"t": self.token}}, {"token": "jéton-été-" + self.token}]
        for charge in cas:
            for chemin in ("/ping", "/show", "/add"):
                with self.subTest(charge=charge, chemin=chemin):
                    rep = http(self.port, chemin=chemin, contenu=dict(charge, items=[MAGNET]))
                    self.assertEqual(rep.status, 403)
                    self.assertIs(rep.data["ok"], False)
                    self.assertNotIn(self.token.encode("ascii"), rep.body)
        self.assertEqual((self.recus, self.affichages), ([], []))

    def test_jeton_faux_rien_n_est_materialise(self):
        """Avec un jeton faux, un contenu .torrent joint n'est même pas écrit dans « recus »."""
        vider_inbox()
        rep = http(self.port, chemin="/add", contenu={
            "token": "mauvais", "items": [{"name": "x.torrent", "data_b64": b64(h.torrent_bytes())}]})
        self.assertEqual(rep.status, 403)
        self.assertEqual(lister_inbox(), [])

    def test_hote_local_accepte(self):
        """En-tête Host : 127.0.0.1, localhost (toute casse), [::1], avec ou sans port."""
        for hote in ("127.0.0.1:%d" % self.port, "localhost:%d" % self.port,
                     "LocalHost:%d" % self.port, "[::1]:%d" % self.port, "127.0.0.1", "localhost"):
            with self.subTest(hote=hote):
                rep = http(self.port, contenu=corps(self.token), hote=hote)
                self.assertEqual(rep.status, 200)

    def test_hote_etranger_refuse(self):
        """Parade au DNS rebinding : un Host étranger est refusé en 403 malgré un jeton valide."""
        for hote in ("evil.example:%d" % self.port, "evil.example",
                     "127.0.0.1.evil.example:%d" % self.port, "localhost.evil.example",
                     "192.168.1.10:%d" % self.port, "0.0.0.0:%d" % self.port):
            for chemin in ("/ping", "/show", "/add"):
                with self.subTest(hote=hote, chemin=chemin):
                    rep = http(self.port, chemin=chemin, hote=hote,
                               contenu=corps(self.token, items=[MAGNET]))
                    self.assertEqual(rep.status, 403)
                    self.assertIs(rep.data["ok"], False)
        self.assertEqual((self.recus, self.affichages), ([], []))

    def test_cors_pour_une_extension(self):
        """Origin chrome-extension:// : la réponse autorise exactement cette origine."""
        origine = "chrome-extension://ejbkdabahogcncpmabcgmenfmefokgjj"
        rep = http(self.port, contenu=corps(self.token), entetes={"Origin": origine})
        self.assertEqual(rep.status, 200)
        self.assertEqual(rep.headers.get("access-control-allow-origin"), origine)
        self.assertIn("content-type", rep.headers.get("access-control-allow-headers", "").lower())

    def test_cors_aussi_sur_les_erreurs_pour_une_extension(self):
        """L'extension peut lire le motif d'un refus (403 jeton) : en-tête CORS présent."""
        origine = "chrome-extension://abcdefghijklmnop"
        rep = http(self.port, contenu={"token": "mauvais"}, entetes={"Origin": origine})
        self.assertEqual(rep.status, 403)
        self.assertEqual(rep.headers.get("access-control-allow-origin"), origine)

    def test_pas_de_cors_pour_une_page_web(self):
        """Une page web ordinaire (ou sans Origin) ne reçoit jamais d'en-tête CORS."""
        for origine in (None, "https://evil.example", "http://localhost:8080", "null",
                        "https://chrome-extension.evil.example", "http://127.0.0.1:%d" % self.port,
                        "file://"):
            for charge in (corps(self.token), {"token": "mauvais"}):
                with self.subTest(origine=origine, charge=charge):
                    rep = http(self.port, contenu=charge, entetes={"Origin": origine})
                    self.assertNotIn("access-control-allow-origin", rep.headers)
                    self.assertNotIn("access-control-allow-headers", rep.headers)

    def test_options_preflight_extension(self):
        """OPTIONS depuis une extension : 204 avec Allow-Origin, Allow-Methods (POST), Allow-Headers."""
        origine = "chrome-extension://abcdefghijklmnop"
        rep = http(self.port, methode="OPTIONS", chemin="/add",
                   entetes={"Origin": origine, "Access-Control-Request-Method": "POST",
                            "Access-Control-Request-Headers": "content-type"})
        self.assertEqual(rep.status, 204)
        self.assertEqual(rep.headers.get("access-control-allow-origin"), origine)
        self.assertIn("POST", rep.headers.get("access-control-allow-methods", ""))
        self.assertIn("content-type", rep.headers.get("access-control-allow-headers", "").lower())
        self.assertEqual(rep.body, b"")
        self.assertEqual((self.recus, self.affichages), ([], []))

    def test_options_preflight_page_web(self):
        """OPTIONS depuis une page web : aucune autorisation CORS accordée."""
        for origine in ("https://evil.example", None, "null"):
            with self.subTest(origine=origine):
                rep = http(self.port, methode="OPTIONS", chemin="/add",
                           entetes={"Origin": origine, "Access-Control-Request-Method": "POST"})
                self.assertLess(rep.status, 500)
                self.assertNotIn("access-control-allow-origin", rep.headers)
                self.assertNotIn("access-control-allow-methods", rep.headers)

    def test_corps_invalides_400(self):
        """JSON invalide, non objet, vide, absent, longueur illisible, négative ou démesurée : 400."""
        jeton = json.dumps({"token": self.token}).encode("utf-8")
        cas = {
            "json_invalide": (b"{pas du json", None),
            "liste": (json.dumps([self.token]).encode("utf-8"), None),
            "chaine": (json.dumps(self.token).encode("utf-8"), None),
            "nombre": (b"42", None),
            "utf8_invalide": (b"\xff\xfe{\x00", None),
            "vide": (b"", None),
            "sans_longueur": (b"", {"Content-Length": None}),
            "longueur_illisible": (b"", {"Content-Length": "abc"}),
            "longueur_negative": (b"", {"Content-Length": "-5"}),
            "longueur_demesuree": (b"", {"Content-Length": str(service.MAX_BODY + 1)}),
            "jeton_puis_longueur_demesuree": (jeton, {"Content-Length": str(service.MAX_BODY + 1)}),
        }
        for nom, (contenu, entetes) in cas.items():
            with self.subTest(cas=nom):
                rep = http(self.port, chemin="/show", contenu=contenu, entetes=entetes)
                self.assertEqual(rep.status, 400)
                self.assertIs(rep.data["ok"], False)
        self.assertEqual((self.recus, self.affichages), ([], []))


# --------------------------------------------------------------------------- #
#  service.materialize
# --------------------------------------------------------------------------- #

class MaterializeTests(Base):

    def setUp(self):
        super().setUp()
        vider_inbox()
        self.dossier = h.temp_dir("tu-mat-")
        self.inbox = paths.inbox_dir().resolve()

    def verifier_dans_inbox(self, resultat, raw):
        resultat = Path(resultat)
        self.assertEqual(resultat.resolve().parent, self.inbox)
        self.assertTrue(resultat.is_file())
        self.assertEqual(resultat.read_bytes(), raw)
        nom = resultat.name
        self.assertTrue(nom.lower().endswith(".torrent"), nom)
        self.assertFalse(set(nom) & set('<>:"/\\|?*'), nom)
        self.assertFalse(any(ord(c) < 32 for c in nom), nom)
        self.assertNotIn("..", nom)
        self.assertLessEqual(len(nom), 100)
        self.assertTrue(nom[:-len(".torrent")].strip("._ "), "nom sans racine : %r" % nom)
        return nom

    # --- liens --------------------------------------------------------------

    def test_magnet_valide_renvoye_tel_quel(self):
        """Lien magnet (hexadécimal, base32, majuscules) : renvoyé tel quel, espaces ôtés."""
        for lien in (MAGNET, MAGNET_B32, "MAGNET:?XT=URN:BTIH:" + HEX40.upper()):
            with self.subTest(lien=lien):
                self.assertEqual(service.materialize("  %s \n" % lien), lien)
        self.assertEqual(lister_inbox(), [])

    def test_magnet_invalide_refuse(self):
        """Magnet sans infohash exploitable : refusé, rien n'est écrit."""
        for lien in (MAGNET_INVALIDE, "magnet:?dn=Film", "magnet:", "magnet:?xt=urn:sha1:" + HEX40):
            with self.subTest(lien=lien):
                with self.assertRaises((ValueError, tf.MagnetError)):
                    service.materialize(lien)
        self.assertEqual(lister_inbox(), [])

    def test_url_http_renvoyee_sans_contact_reseau(self):
        """URL http(s) : renvoyée telle quelle, sans aucune connexion (la seedbox téléchargera)."""
        with mock.patch.object(socket, "create_connection", side_effect=AssertionError("réseau")), \
                mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS")):
            for url in (URL, "http://tracker.test/x.torrent?id=3", "HTTPS://TRACKER.TEST/A"):
                with self.subTest(url=url):
                    self.assertEqual(service.materialize(" %s " % url), url)
        self.assertEqual(lister_inbox(), [])

    def test_autres_schemas_refuses(self):
        """ftp://, javascript:, file:// … ne sont ni des URL acceptées ni des fichiers."""
        for texte in ("ftp://tracker.test/x.torrent", "javascript:alert(1)//.torrent",
                      "file:///C:/x.torrent", "data:,x.torrent"):
            with self.subTest(texte=texte):
                with self.assertRaises(ValueError):
                    service.materialize(texte)

    # --- fichiers locaux ----------------------------------------------------

    def test_fichier_local_existant_renvoye_tel_quel(self):
        """Chemin d'un .torrent existant : renvoyé tel quel, aucune copie dans « recus »."""
        chemin = h.make_torrent(self.dossier, "Local")
        self.assertEqual(service.materialize(str(chemin)), str(chemin))
        self.assertEqual(service.materialize("  %s  " % chemin), str(chemin))
        self.assertEqual(lister_inbox(), [])

    def test_extension_en_majuscules_acceptee(self):
        """« .TORRENT » est reconnu comme « .torrent »."""
        chemin = self.dossier / "MAJ.TORRENT"
        chemin.write_bytes(h.torrent_bytes("MAJ"))
        self.assertEqual(service.materialize(str(chemin)), str(chemin))

    def test_chemins_refuses(self):
        """Fichier absent, extension autre, dossier nommé .torrent : refusés."""
        texte = self.dossier / "notes.txt"
        texte.write_bytes(h.torrent_bytes())
        dossier = self.dossier / "dossier.torrent"
        dossier.mkdir()
        for chemin in (self.dossier / "absent.torrent", texte, dossier, self.dossier,
                       str(self.dossier / "Local") + ".torrent.exe"):
            with self.subTest(chemin=str(chemin)):
                with self.assertRaises(ValueError):
                    service.materialize(str(chemin))
                with self.assertRaises(ValueError):
                    service.materialize({"path": str(chemin), "consume": True})
        self.assertEqual(lister_inbox(), [])

    def test_elements_non_reconnus(self):
        """Ni texte ni dictionnaire exploitable : ValueError."""
        for entree in (42, None, 3.5, b"magnet:?xt=urn:btih:" + HEX40.encode(), [MAGNET], {},
                       {"path": ""}, {"path": 42}, {"path": None}, {"consume": True},
                       {"data_b64": ""}, {"name": "x.torrent"}, ""):
            with self.subTest(entree=entree):
                with self.assertRaises(ValueError):
                    service.materialize(entree)
        self.assertEqual(lister_inbox(), [])

    def test_path_sans_consume_renvoie_l_original(self):
        """{"path": ...} sans « consume » (ou consume faux) : l'original, sans copie."""
        chemin = h.make_torrent(self.dossier, "Original")
        for entree in ({"path": str(chemin)}, {"path": str(chemin), "consume": False},
                       {"path": str(chemin), "consume": 0}):
            with self.subTest(entree=entree):
                self.assertEqual(service.materialize(entree), str(chemin))
        self.assertEqual(lister_inbox(), [])

    def test_path_consume_copie_dans_recus(self):
        """{"path", "consume": true} : copie dans « recus », l'original reste intact."""
        chemin = h.make_torrent(self.dossier, "Mon film été")
        raw = chemin.read_bytes()
        mtime = chemin.stat().st_mtime_ns
        resultat = service.materialize({"path": str(chemin), "consume": True})
        self.assertNotEqual(Path(resultat).resolve(), chemin.resolve())
        self.verifier_dans_inbox(resultat, raw)
        self.assertTrue(chemin.is_file())
        self.assertEqual(chemin.read_bytes(), raw)
        self.assertEqual(chemin.stat().st_mtime_ns, mtime)
        tf.parse_torrent(Path(resultat))

    def test_path_consume_contenu_invalide(self):
        """consume d'une page HTML (ou d'un faux bencode) nommée .torrent : refus, rien ne traîne."""
        for contenu in (h.ULTRA_500, b"d<html>maintenance</html>", b"de", b""):
            with self.subTest(contenu=contenu[:12]):
                chemin = self.dossier / "faux.torrent"
                chemin.write_bytes(contenu)
                with self.assertRaises(ValueError):
                    service.materialize({"path": str(chemin), "consume": True})
                self.assertEqual(chemin.read_bytes(), contenu)
                self.assertEqual(lister_inbox(), [])

    def test_path_consume_trop_volumineux(self):
        """consume d'un fichier de plus de MAX_TORRENT octets : refusé avant toute copie."""
        chemin = self.dossier / "enorme.torrent"
        chemin.write_bytes(b"d" + b"0" * service.MAX_TORRENT)
        with self.assertRaises(ValueError):
            service.materialize({"path": str(chemin), "consume": True})
        self.assertEqual(lister_inbox(), [])
        self.assertTrue(chemin.is_file())

    # --- contenu transmis en base64 -----------------------------------------

    def test_data_b64_valide_stocke(self):
        """{"name", "data_b64"} valide : écrit dans « recus », contenu identique, lisible."""
        raw = h.torrent_bytes("Envoi", files=3)
        resultat = service.materialize({"name": "Envoi.torrent", "data_b64": b64(raw)})
        nom = self.verifier_dans_inbox(resultat, raw)
        self.assertIn("Envoi", nom)
        self.assertEqual(tf.parse_torrent(Path(resultat))["files"], 3)

    def test_data_b64_noms_assainis(self):
        """Noms hostiles ou exotiques : toujours UN composant sûr, dans « recus »."""
        noms = ["..\\..\\evil", "../../evil.torrent", "a/b/c.torrent",
                "C:\\Windows\\System32\\evil.torrent", "\\\\serveur\\partage\\evil.torrent",
                "Mon film été.torrent", "Ça va  «bien».torrent", "", None, ".", "..", "...",
                "CON", "NUL.torrent", "aux", "x" * 300, "evil.torrent:flux",
                "nom\x00nul.torrent", "fin.torrent.", " espace.torrent ", "\u202egnp.torrent",
                12345]
        for nom in noms:
            with self.subTest(nom=nom):
                raw = h.torrent_bytes("N")
                entree = {"data_b64": b64(raw)}
                if nom is not None:
                    entree["name"] = nom
                self.verifier_dans_inbox(service.materialize(entree), raw)
        self.assertEqual(len(lister_inbox()), len(noms))

    def test_data_b64_sans_traversee_de_dossier(self):
        """« ..\\..\\evil » : le fichier reste dans « recus », rien n'apparaît au-dessus."""
        avant_data = sorted(p.name for p in paths.data_dir().iterdir())
        resultat = Path(service.materialize({"name": "..\\..\\evil",
                                             "data_b64": b64(h.torrent_bytes())}))
        self.assertEqual(resultat.resolve().parent, self.inbox)
        self.assertIn("evil", resultat.name)
        self.assertEqual(sorted(p.name for p in paths.data_dir().iterdir()), avant_data)
        self.assertFalse((paths.data_dir() / "evil.torrent").exists())
        self.assertFalse((paths.data_dir().parent / "evil.torrent").exists())

    def test_data_b64_meme_nom_deux_fichiers(self):
        """Deux envois du même nom ne s'écrasent pas."""
        a = service.materialize({"name": "Même.torrent", "data_b64": b64(h.torrent_bytes("A"))})
        b = service.materialize({"name": "Même.torrent", "data_b64": b64(h.torrent_bytes("B"))})
        self.assertNotEqual(a, b)
        self.assertEqual(len(lister_inbox()), 2)
        self.assertEqual(tf.parse_torrent(Path(a))["name"], "A")
        self.assertEqual(tf.parse_torrent(Path(b))["name"], "B")

    def test_data_b64_html_refuse_sans_fichier_residuel(self):
        """Page HTML, faux bencode, dictionnaire sans « info » : refus, « recus » reste vide."""
        for contenu in (h.ULTRA_500, b"<html>login</html>", b"d<html>maintenance</html>",
                        b"de", b"d4:infoi1ee", b"d8:announce3:abce", b"li1ee", b"\x00\x01"):
            with self.subTest(contenu=contenu[:16]):
                with self.assertRaises(ValueError):
                    service.materialize({"name": "x.torrent", "data_b64": b64(contenu)})
                self.assertEqual(lister_inbox(), [])

    def test_data_b64_base64_invalide(self):
        """Base64 illisible ou d'un autre type : ValueError, rien n'est écrit."""
        for donnees in ("!!!!", "abc", "ZGU", "d===", "ZG U=", 12345, ["ZGU="], {"a": 1}):
            with self.subTest(donnees=donnees):
                with self.assertRaises(ValueError):
                    service.materialize({"name": "x.torrent", "data_b64": donnees})
        self.assertEqual(lister_inbox(), [])

    def test_data_b64_trop_volumineux(self):
        """Contenu décodé de plus de MAX_TORRENT octets : refusé, rien n'est écrit."""
        raw = b"d" + b"0" * service.MAX_TORRENT
        with self.assertRaises(ValueError):
            service.materialize({"name": "gros.torrent", "data_b64": b64(raw)})
        self.assertEqual(lister_inbox(), [])

    def test_data_b64_exactement_max_torrent_accepte(self):
        """Borne : un .torrent valide d'exactement MAX_TORRENT octets est accepté."""
        raw = torrent_de_taille(service.MAX_TORRENT)
        resultat = service.materialize({"name": "limite.torrent", "data_b64": b64(raw)})
        self.verifier_dans_inbox(resultat, raw)


# --------------------------------------------------------------------------- #
#  Route /add
# --------------------------------------------------------------------------- #

class AjoutTests(BaseServeur):

    def setUp(self):
        super().setUp()
        vider_inbox()
        self.dossier = h.temp_dir("tu-add-")

    def test_reponse_mixte(self):
        """Bons et mauvais éléments : {"ok", "accepted", "errors"} et on_items ne reçoit que les bons."""
        local = h.make_torrent(self.dossier, "Local")
        raw = h.torrent_bytes("Joint")
        items = [MAGNET, MAGNET_INVALIDE, str(local), str(self.dossier / "absent.torrent"),
                 {"name": "Joint.torrent", "data_b64": b64(raw)}, {"data_b64": "!!!!"}, 42, URL]
        rep = self.post("/add", items=items)
        self.assertEqual(rep.status, 200)
        self.assertEqual(set(rep.data), {"ok", "accepted", "errors"})
        self.assertIs(rep.data["ok"], True)
        self.assertEqual(rep.data["accepted"], 4)
        self.assertEqual(len(rep.data["errors"]), 4)
        self.assertTrue(all(isinstance(e, str) and e for e in rep.data["errors"]))
        self.assertEqual(len(self.recus), 1)
        recu = self.recus[0]
        self.assertEqual(len(recu), 4)
        self.assertEqual(recu[0], MAGNET)
        self.assertEqual(recu[1], str(local))
        self.assertEqual(Path(recu[2]).resolve().parent, paths.inbox_dir().resolve())
        self.assertEqual(Path(recu[2]).read_bytes(), raw)
        self.assertEqual(recu[3], URL)
        self.assertEqual(len(lister_inbox()), 1)

    def test_tous_refuses(self):
        """Aucun élément valide : 200 avec ok faux, accepted 0, une erreur par élément, pas de rappel."""
        items = [MAGNET_INVALIDE, str(self.dossier / "absent.torrent"), {"data_b64": "!!"}]
        rep = self.post("/add", items=items)
        self.assertEqual(rep.status, 200)
        self.assertEqual(rep.data["ok"], False)
        self.assertEqual(rep.data["accepted"], 0)
        self.assertEqual(len(rep.data["errors"]), 3)
        self.assertEqual(self.recus, [])
        self.assertEqual(lister_inbox(), [])

    def test_items_absents_ou_mal_formes_400(self):
        """items absent, vide, texte, dictionnaire ou nul : 400 et aucun rappel."""
        for charge in ({}, {"items": []}, {"items": MAGNET}, {"items": {"0": MAGNET}},
                       {"items": None}, {"items": 0}):
            with self.subTest(charge=charge):
                rep = http(self.port, chemin="/add", contenu=corps(self.token, **charge))
                self.assertEqual(rep.status, 400)
                self.assertIs(rep.data["ok"], False)
        self.assertEqual(self.recus, [])

    def test_plafond_max_items(self):
        """Au-delà de MAX_ITEMS, les éléments ne sont pas traités (ni acceptés ni écrits)."""
        items = [MAGNET] * service.MAX_ITEMS + [
            {"name": "hors-plafond.torrent", "data_b64": b64(h.torrent_bytes())}, AUTRE_MAGNET]
        rep = self.post("/add", items=items)
        self.assertEqual(rep.status, 200)
        self.assertEqual(rep.data["accepted"], service.MAX_ITEMS)
        self.assertEqual(len(self.recus), 1)
        self.assertEqual(len(self.recus[0]), service.MAX_ITEMS)
        self.assertNotIn(AUTRE_MAGNET, self.recus[0])
        self.assertEqual(lister_inbox(), [])

    def test_rappel_on_items_en_erreur(self):
        """Un on_items qui plante n'empêche pas la réponse ; l'erreur est journalisée."""
        def plante(items):
            raise RuntimeError("interface absente")
        srv = self.serveur(on_items=plante)
        avant = taille_journal()
        rep = http(srv.server_address[1], chemin="/add", contenu=corps(self.token, items=[MAGNET]))
        self.assertEqual(rep.status, 200)
        self.assertEqual(rep.data["accepted"], 1)
        self.assertGreater(taille_journal(), avant)

    def test_sans_rappel_on_items(self):
        """on_items à None : la route répond quand même."""
        srv = self.serveur(on_items=None)
        rep = http(srv.server_address[1], chemin="/add", contenu=corps(self.token, items=[MAGNET]))
        self.assertEqual(rep.status, 200)
        self.assertEqual(rep.data["accepted"], 1)

    def test_add_via_ask_running_instance(self):
        """Le client officiel transmet les éléments et rapporte le statut HTTP."""
        rep = service.ask_running_instance("/add", self.token, self.port, items=[MAGNET, MAGNET_INVALIDE],
                                           retries=1)
        self.assertEqual(rep["status"], 200)
        self.assertEqual(rep["accepted"], 1)
        self.assertEqual(len(rep["errors"]), 1)
        self.assertEqual(self.recus, [[MAGNET]])

    def test_add_liste_vide_via_client(self):
        """ask_running_instance avec items=[] : la route répond 400, ok faux."""
        rep = service.ask_running_instance("/add", self.token, self.port, items=[], retries=1)
        self.assertEqual(rep["status"], 400)
        self.assertIs(rep["ok"], False)
        self.assertEqual(self.recus, [])


# --------------------------------------------------------------------------- #
#  Client : post_local, ask_running_instance, forward_to_running_instance
# --------------------------------------------------------------------------- #

class ClientTests(Base):

    def test_post_local_ping(self):
        """post_local : (statut, corps JSON) avec un Host local accepté par le service."""
        srv = self.serveur()
        statut, reponse = service.post_local("/ping", {"token": self.token}, srv.server_address[1])
        self.assertEqual(statut, 200)
        self.assertEqual(reponse["app"], APP_NAME)

    def test_post_local_port_ferme_leve_vite(self):
        """post_local vers un port fermé : OSError, en un temps borné par `timeout`."""
        port = port_libre()
        debut = time.monotonic()
        with self.assertRaises(OSError):
            service.post_local("/ping", {"token": self.token}, port, timeout=0.3)
        self.assertLess(time.monotonic() - debut, 2.0)

    def test_ask_sans_ecoute_none_en_temps_borne(self):
        """Personne n'écoute : None, après `retries` tentatives rapides."""
        port = port_libre()
        debut = time.monotonic()
        rep = service.ask_running_instance("/ping", self.token, port, retries=3, delay=0.05,
                                           timeout=0.3)
        duree = time.monotonic() - debut
        self.assertIsNone(rep)
        self.assertLess(duree, 3 * (0.3 + 0.05) + 1.5)

    def test_ask_ping(self):
        """Instance compatible : réponse 200 enrichie du statut."""
        srv = self.serveur()
        rep = service.ask_running_instance("/ping", self.token, srv.server_address[1], retries=1)
        self.assertEqual(rep["status"], 200)
        self.assertIs(rep["ok"], True)
        self.assertEqual((rep["app"], rep["version"]), (APP_NAME, __version__))

    def test_ask_jeton_refuse(self):
        """Jeton refusé : {"ok": False, "status": 403} — quelqu'un écoute, mais incompatible."""
        srv = self.serveur(token=JETON_ETRANGER)
        rep = service.ask_running_instance("/show", self.token, srv.server_address[1], retries=1)
        self.assertIsNotNone(rep)
        self.assertIs(rep["ok"], False)
        self.assertEqual(rep["status"], 403)
        self.assertTrue(rep.get("error"))
        self.assertEqual(self.affichages, [])

    def test_ask_route_inconnue(self):
        """Route inconnue : statut 404 et ok faux."""
        srv = self.serveur()
        rep = service.ask_running_instance("/rien", self.token, srv.server_address[1], retries=1)
        self.assertEqual((rep["status"], rep["ok"]), (404, False))

    def test_ask_reessaie_jusqu_a_l_ecoute(self):
        """Un perdant qui frappe trop tôt réessaie : le serveur apparu entre-temps répond."""
        port = port_libre()
        minuterie = threading.Timer(0.3, lambda: self.serveur(port=port))
        minuterie.start()
        try:
            rep = service.ask_running_instance("/ping", self.token, port, retries=15, delay=0.1,
                                               timeout=0.3)
        finally:
            minuterie.join(5)
        self.assertIsNotNone(rep)
        self.assertEqual(rep["status"], 200)

    def test_ask_ecouteur_non_http(self):
        """Un programme qui accepte puis raccroche sans répondre : None (pas une instance)."""
        port = port_libre()
        muet = EcouteurMuet(port)
        try:
            rep = service.ask_running_instance("/ping", self.token, port, retries=2, delay=0.05,
                                               timeout=0.3)
        finally:
            muet.fermer()
        self.assertIsNone(rep)
        self.assertGreaterEqual(muet.connexions, 1)

    def test_forward_vrai_avec_elements(self):
        """forward_to_running_instance : True et l'instance reçoit les éléments."""
        srv = self.serveur()
        self.assertTrue(service.forward_to_running_instance([MAGNET, URL], self.token,
                                                            srv.server_address[1], retries=1))
        self.assertEqual(self.recus, [[MAGNET, URL]])
        self.assertEqual(self.affichages, [])

    def test_forward_accepte_un_iterable(self):
        """Un générateur d'éléments est accepté."""
        srv = self.serveur()
        self.assertTrue(service.forward_to_running_instance((x for x in [MAGNET]), self.token,
                                                            srv.server_address[1], retries=1))
        self.assertEqual(self.recus, [[MAGNET]])

    def test_forward_sans_elements_frappe_show(self):
        """Sans éléments (liste vide ou None) : /show, la fenêtre passe au premier plan."""
        srv = self.serveur()
        port = srv.server_address[1]
        self.assertTrue(service.forward_to_running_instance([], self.token, port, retries=1))
        self.assertTrue(service.forward_to_running_instance(None, self.token, port, retries=1))
        self.assertEqual(self.affichages, [True, True])
        self.assertEqual(self.recus, [])

    def test_forward_tous_refuses_reste_vrai(self):
        """L'instance compatible a répondu (200) même si elle refuse tout : True."""
        srv = self.serveur()
        self.assertTrue(service.forward_to_running_instance([MAGNET_INVALIDE], self.token,
                                                            srv.server_address[1], retries=1))
        self.assertEqual(self.recus, [])

    def test_forward_faux_sans_ecoute(self):
        """Personne n'écoute : False, en un temps borné."""
        port = port_libre()
        debut = time.monotonic()
        self.assertFalse(service.forward_to_running_instance([MAGNET], self.token, port, retries=2,
                                                             delay=0.05, timeout=0.3))
        self.assertLess(time.monotonic() - debut, 2 * (0.3 + 0.05) + 1.5)

    def test_forward_faux_si_jeton_refuse(self):
        """Une instance au jeton différent : False, et elle ne reçoit rien."""
        srv = self.serveur(token=JETON_ETRANGER)
        port = srv.server_address[1]
        self.assertFalse(service.forward_to_running_instance([MAGNET], self.token, port, retries=1))
        self.assertFalse(service.forward_to_running_instance([], self.token, port, retries=1))
        self.assertEqual((self.recus, self.affichages), ([], []))


# --------------------------------------------------------------------------- #
#  nativehost : trames stdin/stdout
# --------------------------------------------------------------------------- #

def trame(objet) -> bytes:
    data = objet if isinstance(objet, bytes) else json.dumps(objet).encode("utf-8")
    return struct.pack("<I", len(data)) + data


class TramesTests(unittest.TestCase):

    def test_aller_retour_avec_accents(self):
        """write_message puis read_message : même objet ; longueur sur 4 octets little-endian."""
        message = {"action": "add", "items": ["Été à la plage — «ça» ✓", MAGNET], "n": 3}
        flux = io.BytesIO()
        nativehost.write_message(flux, message)
        octets = flux.getvalue()
        (longueur,) = struct.unpack("<I", octets[:4])
        self.assertEqual(longueur, len(octets) - 4)
        self.assertEqual(json.loads(octets[4:].decode("utf-8")), message)
        self.assertEqual(nativehost.read_message(io.BytesIO(octets)), message)

    def test_lecture_utf8_brut(self):
        """Le navigateur envoie de l'UTF-8 brut (non échappé) : décodé correctement."""
        data = json.dumps({"action": "ping", "nom": "Série été"}, ensure_ascii=False).encode("utf-8")
        self.assertEqual(nativehost.read_message(io.BytesIO(trame(data))),
                         {"action": "ping", "nom": "Série été"})

    def test_longueur_little_endian(self):
        """En-tête 05 00 00 00 : cinq octets de JSON."""
        self.assertEqual(nativehost.read_message(io.BytesIO(b"\x05\x00\x00\x00\"abc\"")), "abc")

    def test_messages_successifs_puis_fin(self):
        """Deux messages à la suite, puis None en fin de flux."""
        flux = io.BytesIO(trame({"a": 1}) + trame({"b": "é"}))
        self.assertEqual(nativehost.read_message(flux), {"a": 1})
        self.assertEqual(nativehost.read_message(flux), {"b": "é"})
        self.assertIsNone(nativehost.read_message(flux))

    def test_flux_vide_ou_tronque(self):
        """Flux vide, en-tête incomplet ou corps tronqué : None."""
        for octets in (b"", b"\x01", b"\x10\x00\x00", struct.pack("<I", 10) + b'{"a":',
                       struct.pack("<I", 2)):
            with self.subTest(octets=octets):
                self.assertIsNone(nativehost.read_message(io.BytesIO(octets)))

    def test_longueur_demesuree(self):
        """Longueur > MAX_MESSAGE : ValueError, sans lire le corps."""
        flux = io.BytesIO(struct.pack("<I", nativehost.MAX_MESSAGE + 1) + b"{}")
        with self.assertRaises(ValueError):
            nativehost.read_message(flux)
        self.assertEqual(flux.tell(), 4)
        with self.assertRaises(ValueError):
            nativehost.read_message(io.BytesIO(b"\xff\xff\xff\xff"))

    def test_longueur_max_message_tolere(self):
        """Borne : exactement MAX_MESSAGE n'est pas « démesuré » (ici tronqué, donc None)."""
        flux = io.BytesIO(struct.pack("<I", nativehost.MAX_MESSAGE) + b"{}")
        self.assertIsNone(nativehost.read_message(flux))

    def test_json_invalide(self):
        """Corps qui n'est pas du JSON (ou pas de l'UTF-8) : ValueError."""
        for data in (b"{pas du json}", b"\xff\xfe", b""):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    nativehost.read_message(io.BytesIO(trame(data)))

    def test_ecriture_videe(self):
        """write_message vide le tampon (flush) : le navigateur reçoit tout de suite."""
        class Flux(io.BytesIO):
            vidages = 0

            def flush(self):
                Flux.vidages += 1
                super().flush()
        flux = Flux()
        nativehost.write_message(flux, {"ok": True})
        self.assertGreaterEqual(Flux.vidages, 1)
        self.assertEqual(nativehost.read_message(io.BytesIO(flux.getvalue())), {"ok": True})


# --------------------------------------------------------------------------- #
#  nativehost.handle
# --------------------------------------------------------------------------- #

class BaseHote(Base):
    """Hôte natif ciblant le port de test ; launch_app est toujours simulé."""

    LAUNCH_TIMEOUT = 2.0

    def setUp(self):
        super().setUp()
        self.port = port_libre()
        h.write_config(ipc_port=self.port, auto_refresh=False)
        self.assertEqual(service.resolve_port(config.load_config()), self.port)
        self.lancements: list = []
        self._minuteries: list = []
        self.lanceur = self._lanceur_interdit
        for cible, valeur in ((nativehost, "launch_app"), (nativehost, "LAUNCH_TIMEOUT")):
            remplacement = (lambda: self.lanceur()) if valeur == "launch_app" else self.LAUNCH_TIMEOUT
            patch = mock.patch.object(cible, valeur, remplacement)
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self._joindre_minuteries)     # avant l'arrêt des serveurs (LIFO)

    def _joindre_minuteries(self):
        for minuterie in self._minuteries:
            minuterie.join(5)

    def _lanceur_interdit(self):
        self.lancements.append("interdit")
        raise AssertionError("launch_app ne devait pas être appelé dans ce test")

    def lanceur_echoue(self):
        self.lancements.append("échec")
        return False

    def lanceur_muet(self):
        self.lancements.append("muet")
        return True

    def lanceur_differe(self, delai=0.3, token=None):
        """Simule un lancement : le service apparaît sur le port de test après `delai`."""
        def lancer():
            self.lancements.append("différé")
            minuterie = threading.Timer(delai, lambda: self.serveur(token=token, port=self.port))
            minuterie.daemon = True
            self._minuteries.append(minuterie)
            minuterie.start()
            return True
        return lancer


class HandleTests(BaseHote):

    def test_message_invalide(self):
        """Message qui n'est pas un objet : ok faux avec une erreur."""
        for message in (None, "ping", ["ping"], 42, True):
            with self.subTest(message=message):
                rep = nativehost.handle(message)
                self.assertIs(rep["ok"], False)
                self.assertTrue(rep.get("error"))
        self.assertEqual(self.lancements, [])

    def test_action_inconnue(self):
        """Action absente ou inconnue : ok faux, erreur, nom et version de l'application."""
        for message in ({}, {"action": "supprimer"}, {"action": None}, {"action": ["ping"]},
                        {"action": "PING"}, {"items": [MAGNET]}):
            with self.subTest(message=message):
                rep = nativehost.handle(message)
                self.assertIs(rep["ok"], False)
                self.assertTrue(rep.get("error"))
                self.assertEqual((rep["app"], rep["version"]), (APP_NAME, __version__))
        self.assertEqual(self.lancements, [])

    def test_ping_sans_application(self):
        """ping, application fermée : ok vrai, running faux, sans lancement."""
        debut = time.monotonic()
        rep = nativehost.handle({"action": "ping"})
        self.assertIs(rep["ok"], True)
        self.assertIs(rep["running"], False)
        self.assertEqual((rep["app"], rep["version"]), (APP_NAME, __version__))
        self.assertEqual(self.lancements, [])
        self.assertLess(time.monotonic() - debut, 3.0)

    def test_ping_avec_application(self):
        """ping, application ouverte sur le port configuré : running vrai, aucun rappel."""
        self.serveur(port=self.port)
        rep = nativehost.handle({"action": "ping"})
        self.assertIs(rep["ok"], True)
        self.assertIs(rep["running"], True)
        self.assertEqual((self.recus, self.affichages), ([], []))

    def test_add_sans_elements(self):
        """add sans liste d'éléments exploitable : refus immédiat, sans réseau ni lancement."""
        with mock.patch.object(nativehost, "ask_running_instance",
                               side_effect=AssertionError("réseau interdit")):
            for message in ({"action": "add"}, {"action": "add", "items": []},
                            {"action": "add", "items": MAGNET}, {"action": "add", "items": None},
                            {"action": "add", "items": {"0": MAGNET}}):
                with self.subTest(message=message):
                    rep = nativehost.handle(message)
                    self.assertIs(rep["ok"], False)
                    self.assertTrue(rep.get("error"))
        self.assertEqual(self.lancements, [])

    def test_add_transmis_a_l_application_ouverte(self):
        """add, application ouverte : accepted/errors relayés, launched faux."""
        self.serveur(port=self.port)
        rep = nativehost.handle({"action": "add", "items": [MAGNET, MAGNET_INVALIDE]})
        self.assertIs(rep["ok"], True)
        self.assertIs(rep["launched"], False)
        self.assertEqual(rep["accepted"], 1)
        self.assertEqual(len(rep["errors"]), 1)
        self.assertEqual(self.recus, [[MAGNET]])
        self.assertEqual(self.lancements, [])

    def test_add_tous_refuses_par_l_application(self):
        """add dont tous les éléments sont refusés : ok faux, accepted 0, erreurs relayées."""
        self.serveur(port=self.port)
        rep = nativehost.handle({"action": "add", "items": [MAGNET_INVALIDE, "C:\\absent.torrent"]})
        self.assertIs(rep["ok"], False)
        self.assertIs(rep["launched"], False)
        self.assertEqual(rep["accepted"], 0)
        self.assertEqual(len(rep["errors"]), 2)
        self.assertEqual(self.recus, [])

    def test_show_transmis_a_l_application_ouverte(self):
        """show, application ouverte : on_show appelé, ok vrai, launched faux."""
        self.serveur(port=self.port)
        rep = nativehost.handle({"action": "show"})
        self.assertIs(rep["ok"], True)
        self.assertIs(rep["launched"], False)
        self.assertEqual(self.affichages, [True])
        self.assertEqual(self.lancements, [])

    def test_add_lancement_impossible(self):
        """add, application fermée et lancement impossible : ok faux, launched faux, erreur."""
        self.lanceur = self.lanceur_echoue
        rep = nativehost.handle({"action": "add", "items": [MAGNET]})
        self.assertIs(rep["ok"], False)
        self.assertIs(rep["launched"], False)
        self.assertTrue(rep.get("error"))
        self.assertEqual(self.lancements, ["échec"])

    def test_show_lancement_impossible(self):
        """show, application fermée et lancement impossible : même refus."""
        self.lanceur = self.lanceur_echoue
        rep = nativehost.handle({"action": "show"})
        self.assertIs(rep["ok"], False)
        self.assertIs(rep["launched"], False)
        self.assertTrue(rep.get("error"))
        self.assertEqual(self.lancements, ["échec"])

    def test_add_apres_lancement(self):
        """add, application fermée : lancée, puis le message lui parvient (launched vrai)."""
        self.lanceur = self.lanceur_differe(0.3)
        rep = nativehost.handle({"action": "add", "items": [MAGNET, AUTRE_MAGNET]})
        self.assertIs(rep["ok"], True, rep)
        self.assertIs(rep["launched"], True)
        self.assertEqual(rep["accepted"], 2)
        self.assertEqual(rep["errors"], [])
        self.assertEqual(self.recus, [[MAGNET, AUTRE_MAGNET]])
        self.assertEqual(self.lancements, ["différé"])

    def test_show_apres_lancement(self):
        """show, application fermée : lancée, puis mise au premier plan."""
        self.lanceur = self.lanceur_differe(0.3)
        rep = nativehost.handle({"action": "show"})
        self.assertIs(rep["ok"], True, rep)
        self.assertIs(rep["launched"], True)
        self.assertEqual(self.affichages, [True])

    def test_lancee_mais_muette(self):
        """Lancement « réussi » mais rien n'écoute avant LAUNCH_TIMEOUT : ok faux, launched vrai."""
        self.lanceur = self.lanceur_muet
        debut = time.monotonic()
        with mock.patch.object(nativehost, "LAUNCH_TIMEOUT", 0.8):
            rep = nativehost.handle({"action": "add", "items": [MAGNET]})
        self.assertIs(rep["ok"], False)
        self.assertIs(rep["launched"], True)
        self.assertTrue(rep.get("error"))
        self.assertEqual(self.lancements, ["muet"])
        self.assertLess(time.monotonic() - debut, 6.0)

    def test_application_au_jeton_different(self):
        """Une instance au jeton différent : erreur relayée, pas de lancement, rien de reçu."""
        self.serveur(token=JETON_ETRANGER, port=self.port)
        attendu = service.ask_running_instance("/add", self.token, self.port, [MAGNET],
                                               retries=1)["error"]
        for message in ({"action": "add", "items": [MAGNET]}, {"action": "show"}):
            with self.subTest(message=message):
                rep = nativehost.handle(message)
                self.assertIs(rep["ok"], False)
                self.assertIs(rep["launched"], False)
                self.assertEqual(rep["error"], attendu)
        self.assertEqual((self.recus, self.affichages), ([], []))
        self.assertEqual(self.lancements, [])

    def test_lancement_puis_jeton_refuse(self):
        """Lancée, l'instance qui répond refuse le jeton : ok faux, launched vrai, erreur."""
        self.lanceur = self.lanceur_differe(0.2, token=JETON_ETRANGER)
        rep = nativehost.handle({"action": "add", "items": [MAGNET]})
        self.assertIs(rep["ok"], False)
        self.assertIs(rep["launched"], True)
        self.assertTrue(rep.get("error"))
        self.assertEqual(self.recus, [])


# --------------------------------------------------------------------------- #
#  nativehost.launch_app (subprocess et interpréteur simulés)
# --------------------------------------------------------------------------- #

class LancementTests(Base):
    """launch_app sans jamais rien lancer : Popen ET les chemins sont des leurres."""

    LANCEUR = r"C:\inexistant-tu-tests\pythonw.exe"
    SCRIPT = r"C:\inexistant-tu-tests\torrent_uploader.py"

    def setUp(self):
        super().setUp()
        from tuploader import winintegration as wi
        self.wi = wi
        self.launcher = mock.MagicMock(name="python_launcher", return_value=self.LANCEUR)
        self.popen = mock.MagicMock(name="Popen")
        for cible, nom, valeur in ((wi, "python_launcher", self.launcher),
                                   (wi, "script_path", mock.MagicMock(return_value=self.SCRIPT)),
                                   (subprocess, "Popen", self.popen)):
            patch = mock.patch.object(cible, nom, valeur)
            patch.start()
            self.addCleanup(patch.stop)
        self.assertIs(nativehost.subprocess.Popen, self.popen)

    def test_commande_et_redirections(self):
        """Commande [pythonw, script], flux redirigés vers DEVNULL, descripteurs fermés."""
        self.assertIs(nativehost.launch_app(), True)
        self.assertEqual(self.popen.call_count, 1)
        args, kwargs = self.popen.call_args
        self.assertEqual(args[0], [self.LANCEUR, self.SCRIPT])
        for flux in ("stdin", "stdout", "stderr"):
            self.assertEqual(kwargs[flux], subprocess.DEVNULL)
        self.assertIs(kwargs["close_fds"], True)

    @unittest.skipUnless(os.name == "nt", "options de création propres à Windows")
    def test_detache_et_sort_du_job(self):
        """Sous Windows : DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB."""
        nativehost.launch_app()
        self.assertEqual(self.popen.call_args.kwargs["creationflags"], 0x08 | 0x200 | 0x01000000)

    @unittest.skipUnless(os.name == "nt", "options de création propres à Windows")
    def test_repli_sans_breakaway(self):
        """Si sortir du job est refusé, on relance sans CREATE_BREAKAWAY_FROM_JOB."""
        self.popen.side_effect = [OSError("accès refusé"), mock.MagicMock()]
        self.assertIs(nativehost.launch_app(), True)
        self.assertEqual(self.popen.call_count, 2)
        self.assertEqual(self.popen.call_args_list[1].kwargs["creationflags"], 0x08 | 0x200)

    def test_echec_total(self):
        """Toutes les tentatives échouent : False et une trace dans le journal."""
        self.popen.side_effect = OSError("introuvable")
        avant = taille_journal()
        self.assertIs(nativehost.launch_app(), False)
        self.assertGreaterEqual(self.popen.call_count, 1)
        self.assertGreater(taille_journal(), avant)

    def test_interpreteur_introuvable(self):
        """pythonw.exe introuvable : False, sans tentative de lancement."""
        self.launcher.side_effect = self.wi.IntegrationError("pythonw.exe est introuvable")
        avant = taille_journal()
        self.assertIs(nativehost.launch_app(), False)
        self.popen.assert_not_called()
        self.assertGreater(taille_journal(), avant)


# --------------------------------------------------------------------------- #
#  nativehost.main (stdin/stdout simulés)
# --------------------------------------------------------------------------- #

def flux_binaire(octets=b""):
    return types.SimpleNamespace(buffer=io.BytesIO(octets))


class MainHoteTests(BaseHote):

    def executer(self, entree: bytes, sortie=None):
        stdin, stdout = flux_binaire(entree), sortie or flux_binaire()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout):
            code = nativehost.main()
        return code, stdout.buffer.getvalue() if isinstance(stdout.buffer, io.BytesIO) else b""

    def reponse_unique(self, octets: bytes) -> dict:
        """La sortie contient exactement une trame ; retourne son contenu."""
        self.assertGreaterEqual(len(octets), 4)
        (longueur,) = struct.unpack("<I", octets[:4])
        self.assertEqual(len(octets), 4 + longueur)
        return nativehost.read_message(io.BytesIO(octets))

    def test_ping(self):
        """Un message ping encadré produit une réponse encadrée, code 0."""
        code, sortie = self.executer(trame({"action": "ping"}))
        self.assertEqual(code, 0)
        rep = self.reponse_unique(sortie)
        self.assertIs(rep["ok"], True)
        self.assertIs(rep["running"], False)
        self.assertEqual(rep["app"], APP_NAME)

    def test_add_vers_application_ouverte(self):
        """add par stdin, application ouverte : la réponse relaie le résultat de l'ajout."""
        self.serveur(port=self.port)
        code, sortie = self.executer(trame({"action": "add", "items": [MAGNET]}))
        self.assertEqual(code, 0)
        rep = self.reponse_unique(sortie)
        self.assertEqual((rep["ok"], rep["accepted"], rep["launched"]), (True, 1, False))
        self.assertEqual(self.recus, [[MAGNET]])

    def test_reponse_utf8_lisible(self):
        """La réponse est du JSON UTF-8 valide (accents des messages d'erreur compris)."""
        code, sortie = self.executer(trame({"action": "inconnue-é"}))
        self.assertEqual(code, 0)
        rep = json.loads(sortie[4:].decode("utf-8"))
        self.assertIs(rep["ok"], False)

    def test_flux_vide_ou_tronque(self):
        """Fin de flux immédiate ou message tronqué : code 0, rien n'est écrit."""
        for entree in (b"", b"\x02\x00", struct.pack("<I", 50) + b'{"action"'):
            with self.subTest(entree=entree):
                code, sortie = self.executer(entree)
                self.assertEqual(code, 0)
                self.assertEqual(sortie, b"")

    def test_message_illisible(self):
        """JSON ou UTF-8 invalide, longueur démesurée : code 1 et réponse d'erreur encadrée."""
        for entree in (trame(b"{pas du json"), trame(b"\xff\xfe"),
                       struct.pack("<I", nativehost.MAX_MESSAGE + 1)):
            with self.subTest(entree=entree[:8]):
                avant = taille_journal()
                code, sortie = self.executer(entree)
                self.assertEqual(code, 1)
                rep = self.reponse_unique(sortie)
                self.assertIs(rep["ok"], False)
                self.assertTrue(rep.get("error"))
                self.assertGreater(taille_journal(), avant)

    def test_handle_qui_plante(self):
        """Erreur inattendue dans handle : code 1, réponse d'erreur, trace journalisée."""
        avant = taille_journal()
        with mock.patch.object(nativehost, "handle", side_effect=RuntimeError("boum")):
            code, sortie = self.executer(trame({"action": "ping"}))
        self.assertEqual(code, 1)
        rep = self.reponse_unique(sortie)
        self.assertIs(rep["ok"], False)
        self.assertGreater(taille_journal(), avant)

    def test_sortie_inutilisable(self):
        """stdout qui refuse d'écrire : main retourne 1 sans lever d'exception."""
        class SortieCassee:
            def write(self, data):
                raise OSError("tuyau rompu")

            def flush(self):
                raise OSError("tuyau rompu")
        code, _ = self.executer(trame({"action": "inconnue"}),
                                types.SimpleNamespace(buffer=SortieCassee()))
        self.assertEqual(code, 1)


# --------------------------------------------------------------------------- #
#  Défauts connus du produit (signalés au mainteneur)
# --------------------------------------------------------------------------- #

class DefautsConnusTests(BaseHote):
    """Chaque test décrit un défaut du produit ; il échoue tant qu'il n'est pas corrigé."""

    def test_premiers_appels_concurrents_partagent_le_meme_jeton(self):
        """Corrigé (service.get_token) : création non atomique du jeton.

        Deux processus lancés ensemble au tout premier démarrage (l'explorateur en
        lance un par fichier sélectionné) lisent tous deux « pas de jeton », en
        génèrent chacun un et l'écrivent : ils ne partagent plus le même secret.
        """
        chemin = paths.token_path()
        chemin.unlink(missing_ok=True)
        barriere = threading.Barrier(2, timeout=2)
        original = service.secrets.token_hex

        def token_hex(*args):
            try:
                barriere.wait()
            except threading.BrokenBarrierError:
                pass
            return original(*args)

        resultats = [None, None]

        def appel(i):
            resultats[i] = service.get_token()

        with mock.patch.object(service.secrets, "token_hex", token_hex):
            fils = [threading.Thread(target=appel, args=(i,)) for i in range(2)]
            for fil in fils:
                fil.start()
            for fil in fils:
                fil.join(5)
        try:
            self.assertEqual(resultats[0], resultats[1])
            self.assertEqual(chemin.read_text(encoding="utf-8").strip(), resultats[0])
        finally:
            chemin.unlink(missing_ok=True)

    def test_connexion_maintenue_apres_refus_d_hote(self):
        """Corrigé (service._Handler.do_POST) : corps non lu après un 403 « hôte non autorisé ».

        En HTTP/1.1 (connexion maintenue, comme fetch()), le corps non consommé est
        ensuite lu comme la requête suivante : la requête valide qui suit reçoit un
        400 « Bad request syntax » au lieu de 200.
        """
        srv = self.serveur()
        port = srv.server_address[1]
        with socket.create_connection(("127.0.0.1", verifier_port(port)), timeout=5) as sock, \
                sock.makefile("rb") as fichier:
            sock.sendall(requete_brute(port, contenu=corps(self.token),
                                       hote="evil.example:%d" % port))
            premiere = lire_reponse(fichier)
            self.assertEqual(premiere.status, 403)
            if premiere.headers.get("connection", "").lower() == "close":
                return                     # correction acceptable : le serveur ferme
            sock.sendall(requete_brute(port, contenu=corps(self.token)))
            seconde = lire_reponse(fichier)
            self.assertIsNotNone(seconde)
            self.assertEqual(seconde.status, 200)

    def test_connexion_maintenue_apres_longueur_demesuree(self):
        """Corrigé (service._Handler.do_POST) : même désynchronisation après un 400 de longueur.

        Le corps annoncé (Content-Length > MAX_BODY) n'est ni lu ni la connexion
        fermée : ses octets sont pris pour une nouvelle requête.
        """
        srv = self.serveur()
        port = srv.server_address[1]
        with socket.create_connection(("127.0.0.1", verifier_port(port)), timeout=5) as sock, \
                sock.makefile("rb") as fichier:
            sock.sendall(requete_brute(port, contenu=corps(self.token),
                                       entetes={"Content-Length": str(service.MAX_BODY + 1)}))
            premiere = lire_reponse(fichier)
            self.assertEqual(premiere.status, 400)
            if premiere.headers.get("connection", "").lower() == "close":
                return
            sock.sendall(requete_brute(port, contenu=corps(self.token)))
            seconde = lire_reponse(fichier)
            self.assertIsNotNone(seconde)
            self.assertEqual(seconde.status, 200)

    def test_elements_au_dela_de_max_items_signales(self):
        """Corrigé (service._Handler._add) : les éléments au-delà de MAX_ITEMS disparaissent sans bruit.

        La réponse annonce accepted=200 et errors=[] pour 203 éléments envoyés :
        l'expéditeur ne peut pas savoir que trois éléments ont été ignorés.
        """
        srv = self.serveur()
        envoyes = service.MAX_ITEMS + 3
        rep = http(srv.server_address[1], chemin="/add",
                   contenu=corps(self.token, items=[MAGNET] * envoyes))
        if rep.status == 400:
            return                          # correction acceptable : refus explicite du lot
        self.assertEqual(rep.status, 200)
        self.assertEqual(rep.data["accepted"] + len(rep.data["errors"]), envoyes)

    def test_ping_ne_declare_pas_saine_une_instance_qui_refuse_le_jeton(self):
        """Corrigé (nativehost.handle, action ping) : running vrai pour une instance incompatible.

        Un service qui répond 403 (jeton différent) fait répondre {"ok": true,
        "running": true} : l'extension croit l'application joignable alors que
        chaque « add » échouera sur « jeton invalide ».
        """
        self.serveur(token=JETON_ETRANGER, port=self.port)
        rep = nativehost.handle({"action": "ping"})
        self.assertFalse(rep.get("ok") is True and rep.get("running") is True, rep)

    def test_forward_refuse_un_tiers_qui_repond_200_sans_json(self):
        """Corrigé (service.ask_running_instance / forward_to_running_instance) :

        un programme tiers qui occupe le port et répond « 200 » avec du HTML est
        pris pour une instance compatible : le second lancement croit avoir
        transmis ses torrents et se termine, ils sont perdus.
        """
        etranger = ServeurEtranger(self.port)
        try:
            transmis = service.forward_to_running_instance([MAGNET], self.token, self.port,
                                                           retries=1)
        finally:
            etranger.fermer()
        self.assertEqual(len(etranger.requetes), 1)
        self.assertFalse(transmis)

    def test_hote_natif_ne_crie_pas_victoire_face_a_un_tiers(self):
        """Corrigé (nativehost.handle) : même cause, l'extension reçoit {"ok": true, "accepted": 0}.

        `answer.get("ok", True)` transforme une réponse sans JSON en succès.
        """
        etranger = ServeurEtranger(self.port)
        try:
            rep = nativehost.handle({"action": "add", "items": [MAGNET]})
        finally:
            etranger.fermer()
        self.assertIs(rep["ok"], False, rep)


if __name__ == "__main__":
    unittest.main()
