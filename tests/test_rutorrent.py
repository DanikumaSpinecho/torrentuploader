# -*- coding: utf-8 -*-
"""Tests du client ruTorrent (tuploader/rutorrent.py).

Tout se passe sur la boucle locale :

* la fausse seedbox de tests/helpers.py pour les scénarios « métier » ;
* ServeurScripte (défini ici) quand la réponse doit être maîtrisée à l'octet
  près : codes HTTP rares, redirections, défi Digest, HTTPS auto-signé ;
* ServeurBrut (défini ici) quand il faut couper la connexion à un instant
  précis : réponse tronquée, RST, serveur muet, délai dépassé.

Les défauts du produit révélés à l'écriture de ces tests ont été corrigés ; les tests
qui les mettaient en évidence vérifient désormais le comportement corrigé. (TestDefautsConnus)
"""

from tests import helpers as h          # EN PREMIER : redirige les données vers le bac à sable

import base64
import contextlib
import hashlib
import json
import os
import re
import socket
import socketserver
import ssl
import struct
import threading
import types
import unittest
import urllib.parse
import xmlrpc.client
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from tuploader import __version__, paths, rutorrent
from tuploader.rutorrent import RuTorrentClient, RuTorrentError

# Ce que la fausse seedbox contient par défaut, vu par le client.
LISTES = (
    ["Films", "Séries"],
    ["/home/u/downloads/rtorrent",
     "/home/u/downloads/rtorrent/films",
     "/home/u/downloads/rtorrent/series"],
    3,
)
APPEL_BRUT = ["raw:d.multicall2", "raw:directory.default"]
ACTION = "/plugins/httprpc/action.php"
MAGNET = "magnet:?xt=urn:btih:0123456789ABCDEF0123456789ABCDEF01234567"

NGINX_502 = (b"<html>\r\n<head><title>502 Bad Gateway</title></head>\r\n<body>\r\n"
             b"<center><h1>502 Bad Gateway</h1></center>\r\n<hr><center>nginx</center>\r\n"
             b"</body>\r\n</html>\r\n")

# Certificat auto-signé JETABLE (CN=tests-torrent-uploader, IP 127.0.0.1), généré
# uniquement pour ces tests, comme ceux de la suite de tests de CPython. La clé
# ne protège rien : elle sert à monter un serveur HTTPS sur la boucle locale.
CERT_PEM = """-----BEGIN CERTIFICATE-----
MIIBqjCCAVCgAwIBAgIUBIiG2qlaDq9OLEEsMo+DaTc4UJgwCgYIKoZIzj0EAwIw
ITEfMB0GA1UEAwwWdGVzdHMtdG9ycmVudC11cGxvYWRlcjAgFw0yNjExMTExMzE2
MzFaGA8yMTI2MTAxODEzMTYzMVowITEfMB0GA1UEAwwWdGVzdHMtdG9ycmVudC11
cGxvYWRlcjBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABFU6tJId7rfTbDDMffTY
Ia8qrn23gakLqLNX0d1GNfnktU+cJm1O4jx6ZB/NPb/cVorzmgq3M3xq8VhQsRBV
kWujZDBiMB0GA1UdDgQWBBTWEcB9BZWOrRn0qfDth0zFKKYVzDAfBgNVHSMEGDAW
gBTWEcB9BZWOrRn0qfDth0zFKKYVzDAPBgNVHRMBAf8EBTADAQH/MA8GA1UdEQQI
MAaHBH8AAAEwCgYIKoZIzj0EAwIDSAAwRQIhAPsaSNjjtsjH0td50pQz7Rdi9mtd
DU0c2Nl1G4M3AY8wAiBSAzkmr2o8d91qHiHkH3+08qGS491GUDw1vp2JkB3/MQ==
-----END CERTIFICATE-----
"""
CLE_PEM = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgUBRu6ZdDg4EBtIQr
wS2bFoeAi/YfvGZm5FMhi/XFQomhRANCAARVOrSSHe6302wwzH302CGvKq59t4Gp
C6izV9HdRjX55LVPnCZtTuI8emQfzT2/3FaK85oKtzN8avFYULEQVZFr
-----END PRIVATE KEY-----
"""

BOX = None                    # fausse seedbox partagée par tout le module
TORRENTS_INITIAUX = []
_ENV = None


def setUpModule():
    global BOX, TORRENTS_INITIAUX, _ENV
    # Ceinture et bretelles : même si un proxy est configuré sur la machine, les
    # requêtes des tests ne doivent jamais quitter la boucle locale.
    _ENV = mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost"})
    _ENV.start()
    BOX = h.FakeSeedbox()
    TORRENTS_INITIAUX = list(BOX.torrents)


def tearDownModule():
    BOX.torrents = list(TORRENTS_INITIAUX)
    BOX.reset()
    BOX.stop()
    _ENV.stop()


# --------------------------------------------------------------------------- #
#  Outils propres à ce fichier
# --------------------------------------------------------------------------- #

def essais() -> int:
    """Nombre d'essais d'une lecture avec reprise : le premier + un par pause."""
    return 1 + len(RuTorrentClient.RETRY_DELAYS)


def basic(identifiant: str, mot_de_passe: str) -> str:
    brut = ("%s:%s" % (identifiant, mot_de_passe)).encode("utf-8")
    return "Basic " + base64.b64encode(brut).decode("ascii")


def reponse_brute(valeur):
    """Réponse XML-RPC (200) portant `valeur`, comme le relais brut de httprpc."""
    corps = xmlrpc.client.dumps((valeur,), methodresponse=True, allow_none=True).encode("utf-8")
    return (200, {"Content-Type": "text/xml"}, corps)


def reponse_liste(torrents) -> tuple:
    """Réponse JSON (200) de mode=list."""
    return (200, {"Content-Type": "application/json"},
            json.dumps({"t": torrents, "cid": 1}).encode("utf-8"))


def ligne(nom="n", etiquette="", base_path="", extras=(), colonnes=34) -> list:
    """Ligne de mode=list : `colonnes` champs standard, puis les colonnes demandées."""
    champs = ["0"] * colonnes
    champs[rutorrent.FIELD_NAME] = nom
    champs[rutorrent.FIELD_LABEL] = etiquette
    champs[rutorrent.FIELD_BASE_PATH] = base_path
    return champs + list(extras)


def genre(requete: dict) -> str:
    """Résume une requête reçue par ServeurScripte, à la manière de FakeSeedbox.calls."""
    corps = requete["body"]
    if corps.lstrip().startswith(b"<?xml"):
        return "raw:" + xmlrpc.client.loads(corps)[1]
    if corps.startswith(b"mode=list"):
        return "list"
    return requete["method"]


def seedbox_vide(requete: dict) -> tuple:
    """Répond comme une seedbox sans aucun torrent, en mode brut comme en mode=list."""
    if requete["body"].lstrip().startswith(b"<?xml"):
        methode = xmlrpc.client.loads(requete["body"])[1]
        return reponse_brute("/home/u/dl" if methode == "directory.default" else [])
    return reponse_liste({})


@contextlib.contextmanager
def pauses_enregistrees():
    """Remplace time.sleep DANS rutorrent.py seulement et consigne les pauses demandées."""
    pauses = []
    with mock.patch.object(rutorrent, "time", types.SimpleNamespace(sleep=pauses.append)):
        yield pauses


def decoupe_multipart(content_type: str, corps: bytes) -> list:
    """Analyse STRICTE d'un corps multipart/form-data.

    Retourne une liste de dictionnaires {nom, fichier, entetes, contenu} ; toute
    entorse au format (délimiteur, fins de ligne, guillemets) lève AssertionError.
    """
    m = re.fullmatch(r"multipart/form-data; boundary=([0-9A-Za-z'()+_,./:=?-]{1,70})",
                     content_type)
    if not m:
        raise AssertionError("Content-Type multipart inattendu : %r" % content_type)
    sep = b"--" + m.group(1).encode("ascii")
    if not corps.startswith(sep + b"\r\n") or not corps.endswith(b"\r\n" + sep + b"--\r\n"):
        raise AssertionError("délimiteurs multipart mal formés")
    interieur = corps[len(sep) + 2: len(corps) - len(sep) - 6]
    parties = []
    for bloc in interieur.split(b"\r\n" + sep + b"\r\n"):
        tete, vide, contenu = bloc.partition(b"\r\n\r\n")
        if not vide:
            raise AssertionError("partie multipart sans ligne vide après ses en-têtes")
        lignes = tete.decode("utf-8").split("\r\n")
        m = re.fullmatch(r'Content-Disposition: form-data; name="([^"\r\n]*)"'
                         r'(?:; filename="([^"\r\n]*)")?', lignes[0])
        if not m:
            raise AssertionError("Content-Disposition mal formé : %r" % lignes[0])
        parties.append({"nom": m.group(1), "fichier": m.group(2),
                        "entetes": lignes[1:], "contenu": contenu})
    return parties


class ServeurScripte:
    """Petit serveur HTTP(S) local dont chaque réponse est dictée par le test.

    `script` : réponses (code, en-têtes, corps) servies dans l'ordre ; une fois
    la liste épuisée, `defaut` sert à chaque requête. Une réponse peut aussi être
    une fonction recevant la requête (dict) et retournant un tel triplet.
    Les requêtes reçues sont consignées dans `requetes`.
    """

    def __init__(self, script=(), defaut=(200, {}, b"ok"), tls: bool = False):
        self.script = list(script)
        self.defaut = defaut
        self.requetes = []
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _traite(self):
                longueur = int(self.headers.get("Content-Length") or 0)
                corps = self.rfile.read(longueur) if longueur else b""
                requete = {"method": self.command, "path": self.path,
                           "headers": {k.lower(): v for k, v in self.headers.items()},
                           "body": corps}
                srv.requetes.append(requete)
                etape = srv.script.pop(0) if srv.script else srv.defaut
                if callable(etape):
                    etape = etape(requete)
                code, entetes, contenu = etape
                self.send_response(code)
                for cle, valeur in entetes.items():
                    self.send_header(cle, valeur)
                self.send_header("Content-Length", str(len(contenu)))
                self.end_headers()
                self.wfile.write(contenu)

            do_GET = do_POST = _traite

        class Serveur(socketserver.ThreadingMixIn, HTTPServer):
            daemon_threads = True

            def server_bind(self):                 # sans le getfqdn() de HTTPServer
                socketserver.TCPServer.server_bind(self)
                self.server_name, self.server_port = self.server_address[:2]

            def handle_error(self, request, client_address):
                pass                               # poignée de main TLS refusée, etc.

        self._serveur = Serveur(("127.0.0.1", 0), Handler)
        if tls:
            dossier = h.temp_dir("tls-")
            (dossier / "cert.pem").write_text(CERT_PEM, encoding="ascii")
            (dossier / "cle.pem").write_text(CLE_PEM, encoding="ascii")
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(dossier / "cert.pem"), str(dossier / "cle.pem"))
            self._serveur.socket = ctx.wrap_socket(self._serveur.socket, server_side=True)
        self.port = self._serveur.server_address[1]
        self.url = "%s://127.0.0.1:%d/rutorrent" % ("https" if tls else "http", self.port)
        threading.Thread(target=self._serveur.serve_forever, daemon=True).start()

    def genres(self) -> list:
        return [genre(r) for r in self.requetes]

    def stop(self):
        self._serveur.shutdown()
        self._serveur.server_close()


class ServeurBrut:
    """Serveur TCP minimal : `comportement(serveur, connexion)` décide de tout.

    Sert à reproduire ce qu'un serveur HTTP bien élevé ne fait jamais : fermer
    sans répondre, tronquer une réponse, envoyer un RST, ne jamais répondre.
    """

    def __init__(self, comportement):
        self.comportement = comportement
        self.requetes = []
        self.fin = threading.Event()
        self._ecoute = socket.socket()
        self._ecoute.bind(("127.0.0.1", 0))
        self._ecoute.listen(8)
        self.port = self._ecoute.getsockname()[1]
        self.url = "http://127.0.0.1:%d/rutorrent" % self.port
        threading.Thread(target=self._boucle, daemon=True).start()

    def _boucle(self):
        while True:
            try:
                conn, _ = self._ecoute.accept()
            except OSError:
                return
            threading.Thread(target=self._traite, args=(conn,), daemon=True).start()

    def _traite(self, conn):
        try:
            self.comportement(self, conn)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def lit_requete(self, conn) -> bytes:
        """Lit une requête HTTP entière (en-têtes + corps annoncé) et la consigne."""
        data = b""
        while b"\r\n\r\n" not in data:
            morceau = conn.recv(65536)
            if not morceau:
                break
            data += morceau
        tete, _, corps = data.partition(b"\r\n\r\n")
        m = re.search(rb"content-length:\s*(\d+)", tete, re.I)
        attendu = int(m.group(1)) if m else 0
        while len(corps) < attendu:
            morceau = conn.recv(65536)
            if not morceau:
                break
            corps += morceau
        self.requetes.append(tete + b"\r\n\r\n" + corps)
        return self.requetes[-1]

    def stop(self):
        self.fin.set()
        try:
            self._ecoute.close()
        except OSError:
            pass


DEBUT_REPONSE = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                 b"Content-Length: 1000\r\n\r\n" + b'{"t": {"AAAA": ["x"')


def _rst(conn):
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))
    conn.close()


def ferme_sans_repondre(srv, conn):
    srv.lit_requete(conn)


def reponse_tronquee(srv, conn):
    srv.lit_requete(conn)
    conn.sendall(DEBUT_REPONSE)               # 1000 octets annoncés, une vingtaine envoyés
    conn.shutdown(socket.SHUT_WR)


def rst_sans_repondre(srv, conn):
    srv.lit_requete(conn)
    _rst(conn)


def rst_en_plein_corps(srv, conn):
    srv.lit_requete(conn)
    conn.sendall(DEBUT_REPONSE)
    srv.fin.wait(0.1)                         # laisse le client lire les en-têtes
    _rst(conn)


def ne_repond_jamais(srv, conn):
    srv.lit_requete(conn)
    srv.fin.wait(10)


def fige_en_plein_corps(srv, conn):
    srv.lit_requete(conn)
    conn.sendall(DEBUT_REPONSE)
    srv.fin.wait(10)


def repond_en_clair(srv, conn):
    """Serveur HTTP ordinaire auquel on parle TLS : la poignée de main échoue."""
    conn.recv(65536)
    conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
    conn.settimeout(3)
    try:
        while conn.recv(65536):               # attend que le client raccroche
            pass
    except OSError:
        pass


def _taille_journal() -> int:
    try:
        return paths.log_path().stat().st_size
    except OSError:
        return 0


class CasClient(unittest.TestCase):
    """Base commune : journal repéré, mémoire du mode brut vidée, serveurs arrêtés."""

    def setUp(self):
        RuTorrentClient._raw_unsupported.clear()
        self.addCleanup(RuTorrentClient._raw_unsupported.clear)
        self._debut_journal = _taille_journal()

    def journal(self) -> str:
        """Ce que le test en cours a ajouté au journal du bac à sable."""
        try:
            with open(paths.log_path(), "rb") as fh:
                fh.seek(self._debut_journal)
                return fh.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def echec(self, fonction, *args, **kwargs) -> RuTorrentError:
        """Exécute `fonction`, exige une RuTorrentError et la retourne."""
        with self.assertRaises(RuTorrentError) as ctx:
            fonction(*args, **kwargs)
        return ctx.exception

    def scripte(self, *args, **kwargs) -> ServeurScripte:
        srv = ServeurScripte(*args, **kwargs)
        self.addCleanup(srv.stop)
        return srv

    def brut(self, comportement) -> ServeurBrut:
        srv = ServeurBrut(comportement)
        self.addCleanup(srv.stop)
        return srv

    @staticmethod
    def client(srv, timeout: int = 5, **options) -> RuTorrentClient:
        return RuTorrentClient(srv.url, "u", "p", timeout=timeout, **options)


class CasSeedbox(CasClient):
    """Base des tests qui parlent à la fausse seedbox, remise à neuf avant chacun."""

    def setUp(self):
        super().setUp()
        self.box = BOX
        self.box.torrents = list(TORRENTS_INITIAUX)
        self.box.reset()


# --------------------------------------------------------------------------- #
#  Construction du client, URL
# --------------------------------------------------------------------------- #

class TestNormalisation(unittest.TestCase):
    """_normalize : l'utilisateur colle ce qu'il veut, on retrouve la racine de ruTorrent."""

    def norm(self, url):
        return RuTorrentClient._normalize(url)

    def test_url_vide(self):
        """Une URL vide, blanche ou absente reste vide (aucun schéma inventé)."""
        for url in ("", "   ", "\t\n", None):
            with self.subTest(url=url):
                self.assertEqual(self.norm(url), "")

    def test_schema_https_ajoute(self):
        """Sans schéma, https:// est ajouté."""
        self.assertEqual(self.norm("seedbox.test/rutorrent"), "https://seedbox.test/rutorrent")
        self.assertEqual(self.norm("seedbox.test"), "https://seedbox.test")
        self.assertEqual(self.norm("127.0.0.1:8080/rutorrent"), "https://127.0.0.1:8080/rutorrent")

    def test_schema_existant_conserve(self):
        """http:// et https:// sont respectés, quelle que soit leur casse."""
        self.assertEqual(self.norm("http://seedbox.test/rutorrent"), "http://seedbox.test/rutorrent")
        self.assertEqual(self.norm("https://seedbox.test/rutorrent"), "https://seedbox.test/rutorrent")
        self.assertEqual(self.norm("HTTP://seedbox.test/rutorrent"), "HTTP://seedbox.test/rutorrent")
        self.assertEqual(self.norm("HttpS://seedbox.test"), "HttpS://seedbox.test")

    def test_espaces_et_barres_finales(self):
        """Espaces autour et barres obliques finales sont retirés."""
        self.assertEqual(self.norm("  https://seedbox.test/rutorrent/  "),
                         "https://seedbox.test/rutorrent")
        self.assertEqual(self.norm("https://seedbox.test/rutorrent///"),
                         "https://seedbox.test/rutorrent")
        self.assertEqual(self.norm("https://seedbox.test/"), "https://seedbox.test")

    def test_suffixes_d_endpoint_retires(self):
        """Une URL complète d'endpoint collée par l'utilisateur est ramenée à la racine."""
        for suffixe in ("/php/addtorrent.php", "/plugins/httprpc/action.php", "/index.html"):
            for variante in (suffixe, suffixe + "/", suffixe.upper(), suffixe.title()):
                with self.subTest(suffixe=variante):
                    self.assertEqual(self.norm("https://seedbox.test/rutorrent" + variante),
                                     "https://seedbox.test/rutorrent")

    def test_suffixe_sans_schema(self):
        """Schéma ajouté ET suffixe retiré dans le même passage."""
        self.assertEqual(self.norm("seedbox.test/rutorrent/index.html"),
                         "https://seedbox.test/rutorrent")

    def test_suffixe_a_la_racine_du_site(self):
        """ruTorrent installé à la racine : il ne reste que l'hôte."""
        self.assertEqual(self.norm("https://seedbox.test/index.html"), "https://seedbox.test")
        self.assertEqual(self.norm("https://seedbox.test/php/addtorrent.php"), "https://seedbox.test")

    def test_seul_un_suffixe_final_est_retire(self):
        """Le même texte ailleurs qu'en fin d'URL n'est pas touché."""
        self.assertEqual(self.norm("https://seedbox.test/index.html/rutorrent"),
                         "https://seedbox.test/index.html/rutorrent")
        self.assertEqual(self.norm("https://seedbox.test/rutorrent/monindex.html"),
                         "https://seedbox.test/rutorrent/monindex.html")

    def test_casse_du_chemin_conservee(self):
        """Seul le suffixe est comparé sans tenir compte de la casse ; le reste est intact."""
        self.assertEqual(self.norm("https://Seedbox.Test/RuTorrent/INDEX.HTML"),
                         "https://Seedbox.Test/RuTorrent")

    def test_base_url_du_client(self):
        """Le constructeur applique la normalisation."""
        client = RuTorrentClient("  127.0.0.1:1/rutorrent/plugins/httprpc/action.php/ ")
        self.assertEqual(client.base_url, "https://127.0.0.1:1/rutorrent")
        self.assertEqual(RuTorrentClient("").base_url, "")


class TestConstruction(unittest.TestCase):

    def test_valeurs_par_defaut(self):
        """Sans précision : pas d'identifiants, certificat vérifié, 60 s de délai."""
        client = RuTorrentClient("http://127.0.0.1:1/rutorrent")
        self.assertEqual((client.username, client.password), ("", ""))
        self.assertIs(client.verify_ssl, True)
        self.assertEqual(client.timeout, 60)

    def test_delai_absent_ou_nul(self):
        """Un délai None, 0 ou vide (config incomplète) retombe sur 60 s."""
        for valeur in (None, 0, ""):
            with self.subTest(timeout=valeur):
                self.assertEqual(RuTorrentClient("http://127.0.0.1:1/r", timeout=valeur).timeout, 60)

    def test_delai_converti_en_entier(self):
        """Le délai lu dans config.json peut être une chaîne ou un flottant."""
        self.assertEqual(RuTorrentClient("http://127.0.0.1:1/r", timeout="30").timeout, 30)
        self.assertEqual(RuTorrentClient("http://127.0.0.1:1/r", timeout=12.7).timeout, 12)

    def test_identifiants_none(self):
        """Des identifiants None (config incomplète) deviennent des chaînes vides."""
        client = RuTorrentClient("http://127.0.0.1:1/r", None, None)
        self.assertEqual((client.username, client.password), ("", ""))
        self.assertNotIn("Authorization", client._headers())

    def test_verify_ssl_booleen(self):
        """verify_ssl est ramené à un booléen."""
        self.assertIs(RuTorrentClient("http://127.0.0.1:1/r", verify_ssl=0).verify_ssl, False)
        self.assertIs(RuTorrentClient("http://127.0.0.1:1/r", verify_ssl=1).verify_ssl, True)

    def test_erreur_par_defaut_non_passagere(self):
        """RuTorrentError : définitive et sans statut, sauf mention contraire."""
        exc = RuTorrentError("oups")
        self.assertEqual((str(exc), exc.transient, exc.status), ("oups", False, None))
        exc = RuTorrentError("oups", transient=True, status=503)
        self.assertEqual((exc.transient, exc.status), (True, 503))


# --------------------------------------------------------------------------- #
#  En-têtes et authentification
# --------------------------------------------------------------------------- #

class TestEnTetes(CasSeedbox):

    def test_basic_sur_tous_les_appels(self):
        """L'en-tête Basic accompagne d'emblée chaque type de requête."""
        client = self.box.client()
        client.fetch_labels_and_dirs()
        client.list_torrents()
        client.add_url(MAGNET)
        client.repair_settings_cache()
        self.assertEqual(self.box.calls, APPEL_BRUT + ["list", "add", "GET getplugins.php"])
        self.assertEqual([r["headers"].get("authorization") for r in self.box.requests],
                         [basic("u", "p")] * 5)

    def test_basic_identifiants_accentues(self):
        """Identifiant et mot de passe sont encodés en UTF-8 avant le base64."""
        client = RuTorrentClient(self.box.url, "rené", "pâss:wörd €", timeout=5)
        client.rtorrent_alive()
        recu = self.box.requests[-1]["headers"]["authorization"]
        self.assertTrue(recu.startswith("Basic "))
        self.assertEqual(base64.b64decode(recu[6:]).decode("utf-8"), "rené:pâss:wörd €")

    def test_mot_de_passe_vide(self):
        """Un identifiant sans mot de passe donne « identifiant: »."""
        RuTorrentClient(self.box.url, "u", "", timeout=5).rtorrent_alive()
        self.assertEqual(self.box.requests[-1]["headers"]["authorization"], basic("u", ""))

    def test_pas_d_en_tete_sans_identifiant(self):
        """Sans identifiant, aucun en-tête Authorization (même avec un mot de passe)."""
        for mot_de_passe in ("", "orphelin"):
            with self.subTest(mot_de_passe=mot_de_passe):
                self.box.reset()
                RuTorrentClient(self.box.url, "", mot_de_passe, timeout=5).rtorrent_alive()
                self.assertNotIn("authorization", self.box.requests[-1]["headers"])

    def test_user_agent_et_accept(self):
        """Le client s'annonce avec son nom et sa version."""
        self.box.client().rtorrent_alive()
        entetes = self.box.requests[-1]["headers"]
        self.assertEqual(entetes["user-agent"], "TorrentUploader/%s" % __version__)
        self.assertEqual(entetes["accept"], "*/*")

    def test_defi_basic_refuse_sans_boucle(self):
        """401 + défi Basic alors que les identifiants ont déjà été envoyés : un seul essai."""
        srv = self.scripte(defaut=(401, {"WWW-Authenticate": 'Basic realm="seedbox"'}, b"non"))
        exc = self.echec(self.client(srv).list_torrents)
        self.assertEqual((exc.status, exc.transient), (401, False))
        self.assertEqual(len(srv.requetes), 1)

    def test_authentification_digest(self):
        """Un serveur qui exige Digest reçoit une seconde requête correctement signée."""
        def defi(requete):
            if requete["headers"].get("authorization", "").startswith("Digest "):
                return reponse_liste({"ABCD": []})
            return (401, {"WWW-Authenticate":
                          'Digest realm="seedbox", nonce="n0nce", qop="auth", algorithm=MD5'},
                    b"non")

        srv = self.scripte(defaut=defi)
        self.assertEqual(self.client(srv).list_torrents(retry=False), {"ABCD": []})
        self.assertEqual(len(srv.requetes), 2)
        self.assertEqual(srv.requetes[0]["headers"]["authorization"], basic("u", "p"))
        self.assertEqual(srv.requetes[1]["body"], b"mode=list")      # corps renvoyé à l'identique

        recu = srv.requetes[1]["headers"]["authorization"]
        champs = dict(re.findall(r'(\w+)="?([^",]+)"?', recu[len("Digest "):]))
        self.assertEqual(champs["username"], "u")
        self.assertEqual(champs["realm"], "seedbox")
        self.assertEqual(champs["nonce"], "n0nce")
        self.assertEqual(champs["uri"], "/rutorrent" + ACTION)

        def md5(texte):
            return hashlib.md5(texte.encode("utf-8")).hexdigest()

        attendu = md5(":".join((md5("u:seedbox:p"), champs["nonce"], champs["nc"],
                                champs["cnonce"], champs["qop"], md5("POST:" + champs["uri"]))))
        self.assertEqual(champs["response"], attendu)

    def test_cookies_conserves_par_instance(self):
        """Un cookie de session posé par le serveur est renvoyé par le même client."""
        srv = self.scripte(
            script=[(200, {"Content-Type": "application/json",
                           "Set-Cookie": "PHPSESSID=abc123; Path=/"}, b'{"t": {}}')],
            defaut=reponse_liste({}))
        client = self.client(srv)
        client.list_torrents()
        client.list_torrents()
        self.client(srv).list_torrents()             # autre instance : autre bocal à cookies
        self.assertNotIn("cookie", srv.requetes[0]["headers"])
        self.assertEqual(srv.requetes[1]["headers"].get("cookie"), "PHPSESSID=abc123")
        self.assertNotIn("cookie", srv.requetes[2]["headers"])


# --------------------------------------------------------------------------- #
#  Ajout d'un fichier .torrent (multipart)
# --------------------------------------------------------------------------- #

class TestMultipart(unittest.TestCase):
    """_multipart, sans réseau."""

    def test_structure(self):
        """Champs puis fichiers, chacun dans sa partie, délimiteur final présent."""
        corps, ctype = RuTorrentClient._multipart(
            [("dir_edit", "/home/u/dl"), ("nombre", 5)],
            [("torrent_file", "a.torrent", b"d4:spam4:eggse")])
        parties = decoupe_multipart(ctype, corps)
        self.assertEqual([(p["nom"], p["fichier"]) for p in parties],
                         [("dir_edit", None), ("nombre", None), ("torrent_file", "a.torrent")])
        self.assertEqual(parties[0]["contenu"], b"/home/u/dl")
        self.assertEqual(parties[1]["contenu"], b"5")                 # valeur convertie en texte
        self.assertEqual(parties[0]["entetes"], [])
        self.assertEqual(parties[2]["entetes"], ["Content-Type: application/x-bittorrent"])
        self.assertEqual(parties[2]["contenu"], b"d4:spam4:eggse")

    def test_guillemets_du_nom_de_fichier(self):
        """Un guillemet double casserait l'en-tête : il devient une apostrophe."""
        corps, ctype = RuTorrentClient._multipart(
            [], [("torrent_file", 'Le "Grand" Bleu ".torrent', b"x")])
        self.assertIn(b'Content-Disposition: form-data; name="torrent_file"; '
                      b'filename="Le \'Grand\' Bleu \'.torrent"\r\n', corps)
        (partie,) = decoupe_multipart(ctype, corps)
        self.assertEqual(partie["fichier"], "Le 'Grand' Bleu '.torrent")

    def test_delimiteur_unique_et_absent_du_contenu(self):
        """Chaque corps a son propre délimiteur aléatoire, conforme à la RFC 2046."""
        contenu = b"------TorrentUploader" + b"0" * 32 + b"\r\n"
        corps1, ctype1 = RuTorrentClient._multipart([], [("f", "a", contenu)])
        corps2, ctype2 = RuTorrentClient._multipart([], [("f", "a", contenu)])
        self.assertNotEqual(ctype1, ctype2)
        delimiteur = ctype1.split("boundary=", 1)[1]
        self.assertLessEqual(len(delimiteur), 70)
        self.assertNotIn(delimiteur.encode("ascii"), contenu)
        self.assertEqual(decoupe_multipart(ctype1, corps1)[0]["contenu"], contenu)
        self.assertEqual(decoupe_multipart(ctype2, corps2)[0]["contenu"], contenu)

    def test_valeurs_accentuees_en_utf8(self):
        """Valeurs de champs et noms de fichiers partent en UTF-8."""
        corps, ctype = RuTorrentClient._multipart(
            [("label", "Séries été")], [("torrent_file", "Été à Zürich.torrent", b"x")])
        champ, fichier = decoupe_multipart(ctype, corps)
        self.assertEqual(champ["contenu"], "Séries été".encode("utf-8"))
        self.assertEqual(fichier["fichier"], "Été à Zürich.torrent")
        self.assertIn('filename="Été à Zürich.torrent"'.encode("utf-8"), corps)


class TestAjoutFichier(CasSeedbox):

    def setUp(self):
        super().setUp()
        self.dossier = h.temp_dir("rt-")
        self.torrent = h.make_torrent(self.dossier, "Exemple")

    def envoi(self, chemin, *args, **options):
        """Envoie `chemin` ; retourne (réponse, requête reçue, parties du multipart)."""
        reponse = self.box.client().add_torrent(chemin, *args, **options)
        requete = self.box.requests[-1]
        return reponse, requete, decoupe_multipart(requete["headers"]["content-type"],
                                                   requete["body"])

    @staticmethod
    def champs(parties):
        return [(p["nom"], p["contenu"].decode("utf-8")) for p in parties if p["fichier"] is None]

    def test_envoi_minimal(self):
        """Sans dossier ni étiquette : seul le fichier part, vers php/addtorrent.php."""
        reponse, requete, parties = self.envoi(self.torrent)
        self.assertEqual(reponse, "ok")
        self.assertEqual(self.box.calls, ["add"])
        self.assertEqual(requete["method"], "POST")
        self.assertEqual(requete["path"], "/rutorrent/php/addtorrent.php")
        self.assertEqual(int(requete["headers"]["content-length"]), len(requete["body"]))
        self.assertEqual(len(parties), 1)
        fichier = parties[0]
        self.assertEqual((fichier["nom"], fichier["fichier"]), ("torrent_file", "Exemple.torrent"))
        self.assertEqual(fichier["entetes"], ["Content-Type: application/x-bittorrent"])
        self.assertEqual(fichier["contenu"], self.torrent.read_bytes())

    def test_dossier_et_etiquette(self):
        """dir_edit et label sont transmis, débarrassés des espaces autour."""
        _, _, parties = self.envoi(self.torrent, "  /home/u/dl/Séries  ", "  Séries TV ")
        self.assertEqual(self.champs(parties),
                         [("dir_edit", "/home/u/dl/Séries"), ("label", "Séries TV")])
        self.assertEqual(parties[-1]["nom"], "torrent_file")

    def test_dossier_et_etiquette_blancs_omis(self):
        """Un dossier ou une étiquette vides ou blancs ne créent aucun champ."""
        _, _, parties = self.envoi(self.torrent, "   ", "\t")
        self.assertEqual(self.champs(parties), [])

    def test_demarrage_differe(self):
        """torrents_start_stopped n'existe QUE si start=False."""
        _, _, parties = self.envoi(self.torrent, "/d", "L", start=False)
        self.assertEqual(self.champs(parties),
                         [("dir_edit", "/d"), ("label", "L"), ("torrents_start_stopped", "on")])
        _, _, parties = self.envoi(self.torrent, start=False)
        self.assertEqual(self.champs(parties), [("torrents_start_stopped", "on")])
        for depart in ({}, {"start": True}):
            with self.subTest(options=depart):
                _, requete, parties = self.envoi(self.torrent, "/d", "L", **depart)
                self.assertEqual(self.champs(parties), [("dir_edit", "/d"), ("label", "L")])
                self.assertNotIn(b"torrents_start_stopped", requete["body"])

    def test_octets_transmis_tels_quels(self):
        """Le contenu binaire arrive à l'identique (tous les octets, CRLF, faux délimiteurs)."""
        contenu = (b"d4:info" + bytes(range(256)) * 8 + b"\r\n--\r\n\r\n------TorrentUploader"
                   + b"\x00\xff\r\n\r\n" + b"--\r\n" + b"e")
        chemin = self.dossier / "binaire.torrent"
        chemin.write_bytes(contenu)
        _, requete, parties = self.envoi(chemin)
        self.assertEqual(parties[-1]["contenu"], contenu)
        self.assertEqual(requete["body"].count(contenu), 1)

    def test_torrent_multi_fichiers_intact(self):
        """Un vrai .torrent multi-fichiers est transmis octet pour octet."""
        chemin = h.make_torrent(self.dossier, "Pack", files=5)
        _, _, parties = self.envoi(chemin)
        self.assertEqual(parties[-1]["contenu"], h.torrent_bytes("Pack", files=5))

    def test_nom_de_fichier_accentue(self):
        """Le nom du fichier (accents, espaces) est transmis en UTF-8."""
        chemin = h.make_torrent(self.dossier, "Série été à Zürich")
        _, _, parties = self.envoi(chemin)
        self.assertEqual(parties[-1]["fichier"], "Série été à Zürich.torrent")

    def test_nom_de_fichier_avec_guillemets(self):
        """Des guillemets dans le nom (impossibles sous Windows, possibles ailleurs) sont neutralisés."""
        with mock.patch.object(Path, "read_bytes", return_value=b"d4:spam4:eggse"):
            reponse = self.box.client().add_torrent(Path('dossier/Le "Grand" Bleu.torrent'))
        self.assertEqual(reponse, "ok")
        requete = self.box.requests[-1]
        (partie,) = decoupe_multipart(requete["headers"]["content-type"], requete["body"])
        self.assertEqual(partie["fichier"], "Le 'Grand' Bleu.torrent")
        self.assertEqual(partie["contenu"], b"d4:spam4:eggse")

    def test_chemin_en_chaine(self):
        """Le chemin peut être donné comme simple chaîne ; seul le nom du fichier est transmis."""
        _, requete, parties = self.envoi(str(self.torrent))
        self.assertEqual(parties[-1]["fichier"], "Exemple.torrent")
        self.assertNotIn(str(self.dossier).encode("utf-8"), requete["body"])

    def test_fichier_absent(self):
        """Fichier illisible : erreur locale, rien n'est envoyé à la seedbox."""
        with self.assertRaises(OSError):
            self.box.client().add_torrent(self.dossier / "absent.torrent")
        self.assertEqual(self.box.calls, [])

    def test_erreur_signalee_par_rutorrent(self):
        """Une réponse 200 contenant showError est un échec définitif, au message non vide."""
        self.box.add_plan = ["error"]
        exc = self.echec(self.box.client().add_torrent, self.torrent)
        self.assertFalse(exc.transient)
        self.assertIsNone(exc.status)
        self.assertTrue(str(exc).strip())
        self.assertEqual(self.box.calls, ["add"])                     # pas de second envoi

    def test_erreur_500_passagere(self):
        """Un 500 pendant l'ajout est passager (l'appelant vérifiera si le torrent est arrivé)."""
        self.box.add_plan = ["500"]
        exc = self.echec(self.box.client().add_torrent, self.torrent)
        self.assertEqual((exc.transient, exc.status), (True, 500))
        self.assertEqual(self.box.calls, ["add"])                     # add_torrent ne réessaie pas seul

    def test_redirection_de_rutorrent_reussie(self):
        """ruTorrent répond par un 302 vers ?result[]=Success puis un script noty : succès."""
        def ajout(requete):
            if requete["method"] == "POST":
                return (302, {"Location": "//127.0.0.1:%d/rutorrent/php/addtorrent.php"
                                          "?result[]=Success&name[]=Exemple.torrent" % srv.port},
                        b"")
            return (200, {"Content-Type": "text/html"},
                    b'noty("Exemple.torrent - "+theUILang.addTorrentSuccess,"success");')

        srv = self.scripte(defaut=ajout)
        reponse = self.client(srv).add_torrent(self.torrent)
        self.assertIn("addTorrentSuccess", reponse)
        self.assertEqual([r["method"] for r in srv.requetes], ["POST", "GET"])
        self.assertTrue(srv.requetes[1]["path"].endswith(
            "/php/addtorrent.php?result[]=Success&name[]=Exemple.torrent"))
        self.assertEqual(srv.requetes[1]["headers"].get("authorization"), basic("u", "p"))

    def test_redirection_de_rutorrent_echouee(self):
        """Même parcours, mais ruTorrent annonce addTorrentFailed : échec définitif."""
        def ajout(requete):
            if requete["method"] == "POST":
                return (302, {"Location": "//127.0.0.1:%d/rutorrent/php/addtorrent.php"
                                          "?result[]=Failed&name[]=Exemple.torrent" % srv.port},
                        b"")
            return (200, {"Content-Type": "text/html"},
                    b'noty("Exemple.torrent - "+theUILang.addTorrentFailed,"error");')

        srv = self.scripte(defaut=ajout)
        exc = self.echec(self.client(srv).add_torrent, self.torrent)
        self.assertFalse(exc.transient)
        self.assertIn("addTorrentFailed", str(exc))


# --------------------------------------------------------------------------- #
#  Ajout d'un lien (magnet ou URL)
# --------------------------------------------------------------------------- #

class TestAjoutLien(CasSeedbox):

    def champs(self):
        requete = self.box.requests[-1]
        return urllib.parse.parse_qsl(requete["body"].decode("ascii"), keep_blank_values=True,
                                      strict_parsing=True, encoding="utf-8", errors="strict")

    def test_lien_seul(self):
        """Sans option : un seul champ « url », en POST urlencodé vers php/addtorrent.php."""
        self.assertEqual(self.box.client().add_url(MAGNET), "ok")
        requete = self.box.requests[-1]
        self.assertEqual(self.box.calls, ["add"])
        self.assertEqual(requete["method"], "POST")
        self.assertEqual(requete["path"], "/rutorrent/php/addtorrent.php")
        self.assertEqual(requete["headers"]["content-type"],
                         "application/x-www-form-urlencoded; charset=UTF-8")
        self.assertEqual(self.champs(), [("url", MAGNET)])
        self.assertEqual(requete["body"],
                         b"url=magnet%3A%3Fxt%3Durn%3Abtih%3A0123456789ABCDEF0123456789ABCDEF01234567")

    def test_tous_les_champs(self):
        """dir_edit, label et torrents_start_stopped suivent le lien, valeurs nettoyées."""
        self.box.client().add_url(MAGNET, "  /home/u/dl/Séries ", " Séries TV  ", start=False)
        self.assertEqual(self.champs(), [("url", MAGNET), ("dir_edit", "/home/u/dl/Séries"),
                                         ("label", "Séries TV"), ("torrents_start_stopped", "on")])
        self.assertIn(b"&label=S%C3%A9ries+TV&", self.box.requests[-1]["body"])

    def test_demarrage_immediat_par_defaut(self):
        """torrents_start_stopped est absent quand start vaut True (défaut)."""
        self.box.client().add_url(MAGNET, "/d", "L")
        self.assertEqual(self.champs(), [("url", MAGNET), ("dir_edit", "/d"), ("label", "L")])
        self.box.client().add_url(MAGNET, start=True)
        self.assertEqual(self.champs(), [("url", MAGNET)])

    def test_champs_blancs_omis(self):
        """Dossier et étiquette blancs ne sont pas envoyés."""
        self.box.client().add_url(MAGNET, "  ", " ", start=False)
        self.assertEqual(self.champs(), [("url", MAGNET), ("torrents_start_stopped", "on")])

    def test_caracteres_speciaux_du_lien(self):
        """&, =, ?, +, espaces et accents du lien survivent à l'encodage."""
        lien = (MAGNET + "&dn=Été à la mer + bonus (100%)&tr=http://tracker.test/ann?a=1&b=2"
                "&tr=udp://tracker.test:6969")
        self.box.client().add_url(lien, label="A&B=C")
        self.assertEqual(self.champs(), [("url", lien), ("label", "A&B=C")])
        self.assertEqual(self.box.requests[-1]["body"].count(b"&"), 1)    # un seul séparateur réel

    def test_url_http_de_torrent(self):
        """Une URL http(s) de .torrent passe par le même champ « url »."""
        lien = "https://tracker.test/download.php?id=42&passkey=abc"
        self.box.client().add_url(lien)
        self.assertEqual(self.champs(), [("url", lien)])

    def test_erreur_signalee_par_rutorrent(self):
        """Une réponse d'erreur de ruTorrent lève une erreur définitive."""
        self.box.add_plan = ["error"]
        exc = self.echec(self.box.client().add_url, MAGNET)
        self.assertFalse(exc.transient)
        self.assertTrue(str(exc).strip())

    def test_erreur_500_passagere(self):
        """Cache ruTorrent corrompu : addtorrent répond 500, erreur passagère."""
        self.box.reset("poisoned")
        exc = self.echec(self.box.client().add_url, MAGNET)
        self.assertEqual((exc.transient, exc.status), (True, 500))


class TestControleAjout(unittest.TestCase):
    """_check_add : ruTorrent répond 200 même quand l'ajout a échoué."""

    MARQUEURS = ("showerror", "addtorrentfailed", "bad torrent", "not a valid", "failed to add",
                 "torrent file is invalid", "no such file", "bad link", "invalid url")

    def setUp(self):
        self.client = RuTorrentClient("http://127.0.0.1:1/rutorrent")      # jamais contacté

    def test_chaque_marqueur_leve(self):
        """Chaque marqueur d'échec connu, dans n'importe quelle casse, lève une erreur définitive."""
        for marqueur in self.MARQUEURS:
            for texte in (marqueur, marqueur.upper(), marqueur.title()):
                with self.subTest(texte=texte):
                    with self.assertRaises(RuTorrentError) as ctx:
                        self.client._check_add("<html><body><b>Erreur :</b> %s !</body></html>" % texte)
                    self.assertFalse(ctx.exception.transient)
                    self.assertEqual(str(ctx.exception), "Erreur : %s !" % texte)

    def test_marqueur_dans_un_script(self):
        """Le marqueur est cherché dans la réponse brute ; le message n'est jamais vide."""
        with self.assertRaises(RuTorrentError) as ctx:
            self.client._check_add("<script>theWebUI.showError('Bad torrent');</script>")
        self.assertTrue(str(ctx.exception).strip())
        self.assertNotIn("<", str(ctx.exception))

    def test_statuts_d_echec_de_rutorrent(self):
        """Tous les statuts d'échec de ruTorrent (Failed, FailedURL, FailedDirectory...) lèvent."""
        for statut in ("Failed", "FailedURL", "FailedFile", "FailedDirectory"):
            with self.subTest(statut=statut):
                with self.assertRaises(RuTorrentError):
                    self.client._check_add('noty("x.torrent - "+theUILang.addTorrent%s,"error");' % statut)

    def test_reponses_de_succes(self):
        """Une réponse sans marqueur est rendue nettoyée de son HTML."""
        self.assertEqual(self.client._check_add("<html><body>ok</body></html>"), "ok")
        self.assertEqual(self.client._check_add(""), "")
        self.assertEqual(
            self.client._check_add('noty("x.torrent - "+theUILang.addTorrentSuccess,"success");'),
            'noty("x.torrent - "+theUILang.addTorrentSuccess,"success");')


class TestNettoyageHtml(unittest.TestCase):
    """_clean_html : ce qui reste lisible d'une page renvoyée par le serveur."""

    def test_balises_scripts_entites(self):
        """Scripts supprimés, balises retirées, entités décodées, blancs regroupés."""
        page = ("<html><head><SCRIPT type='x'>var a = '<b>';\nalert(1)</SCRIPT></head>\n"
                "<body><h1>Erreur&nbsp;500</h1>\n\n  <p>C&#39;est   cass&eacute; &amp; ferm&eacute;</p></body></html>")
        self.assertEqual(RuTorrentClient._clean_html(page), "Erreur 500 C'est cassé & fermé")

    def test_longueur_bornee(self):
        """Jamais plus de 400 caractères."""
        self.assertEqual(len(RuTorrentClient._clean_html("<p>" + "x" * 5000 + "</p>")), 400)

    def test_texte_vide(self):
        self.assertEqual(RuTorrentClient._clean_html(""), "")
        self.assertEqual(RuTorrentClient._clean_html("<br/>  <hr>"), "")


# --------------------------------------------------------------------------- #
#  _post : classement des erreurs
# --------------------------------------------------------------------------- #

class TestClassementErreurs(CasSeedbox):

    def test_sans_url(self):
        """Sans URL configurée : erreur définitive et explicite, aucune requête."""
        client = RuTorrentClient("")
        for appel in (client.list_torrents, client.rtorrent_alive.__self__.known_hashes,
                      lambda: client.add_url(MAGNET), lambda: client._post("/x", None)):
            exc = self.echec(appel)
            self.assertEqual(str(exc), "Aucune URL de seedbox configurée.")
            self.assertFalse(exc.transient)

    def test_get_si_corps_absent(self):
        """body=None produit un GET sans corps ni Content-Type."""
        texte = self.box.client()._post("/php/getplugins.php", None)
        self.assertEqual(texte, "theWebUI.plugins = [];")
        requete = self.box.requests[-1]
        self.assertEqual((requete["method"], requete["path"]), ("GET", "/rutorrent/php/getplugins.php"))
        self.assertEqual(requete["body"], b"")
        self.assertNotIn("content-type", requete["headers"])
        self.assertNotIn("content-length", requete["headers"])

    def test_post_si_corps_present(self):
        """Un corps, même vide, produit un POST avec le Content-Type demandé."""
        for corps in (b"", b"mode=list"):
            with self.subTest(corps=corps):
                self.box.reset()
                self.box.client()._post(ACTION, corps, "application/x-test", {"X-Essai": "1"})
                requete = self.box.requests[-1]
                self.assertEqual(requete["method"], "POST")
                self.assertEqual(requete["body"], corps)
                self.assertEqual(requete["headers"]["content-type"], "application/x-test")
                self.assertEqual(requete["headers"]["x-essai"], "1")

    def test_reponse_decodee_en_utf8_tolerant(self):
        """La réponse est décodée en UTF-8 ; un octet invalide ne fait pas échouer la lecture."""
        srv = self.scripte(defaut=(200, {}, "été ".encode("utf-8") + b"\xff fin"))
        self.assertEqual(self.client(srv)._post("/x", b"a"), "été \ufffd fin")

    def test_401_definitif(self):
        """401 : refus définitif, statut conservé, aucun nouvel essai."""
        self.box.reset("unauth")
        exc = self.echec(self.box.client().list_torrents)            # retry=True par défaut
        self.assertEqual((exc.transient, exc.status), (False, 401))
        self.assertIn("Authentification refusée (HTTP 401)", str(exc))
        self.assertIn("mot de passe", str(exc))
        self.assertEqual(self.box.calls, ["list"])

    def test_403_definitif(self):
        """403 : même traitement que 401."""
        srv = self.scripte(defaut=(403, {}, b"<h1>Forbidden</h1>"))
        exc = self.echec(self.client(srv).list_torrents)
        self.assertEqual((exc.transient, exc.status), (False, 403))
        self.assertIn("Authentification refusée (HTTP 403)", str(exc))
        self.assertNotIn("Forbidden", str(exc))
        self.assertEqual(len(srv.requetes), 1)

    def test_404_definitif_sans_nom_d_hote(self):
        """404 : définitif ; le message cite le chemin mais ni l'hôte ni le port."""
        srv = self.scripte(defaut=(404, {}, b"<html><body>Not Found on this server</body></html>"))
        exc = self.echec(self.client(srv).list_torrents)
        message = str(exc)
        self.assertEqual((exc.transient, exc.status), (False, 404))
        self.assertIn("HTTP 404", message)
        self.assertIn(ACTION, message)
        self.assertIn("URL de ruTorrent", message)
        for interdit in ("127.0.0.1", str(srv.port), "://", "/rutorrent/", "Not Found"):
            self.assertNotIn(interdit, message)
        self.assertEqual(len(srv.requetes), 1)                         # pas de nouvel essai

    def test_404_sur_la_fausse_seedbox(self):
        """Même constat sur un endpoint inconnu de la fausse seedbox."""
        exc = self.echec(self.box.client()._post, "/php/inconnu.php", b"x=1")
        self.assertEqual((exc.transient, exc.status), (False, 404))
        self.assertIn("/php/inconnu.php", str(exc))
        self.assertNotIn("127.0.0.1", str(exc))

    def test_500_passager_message_propre_page_au_journal(self):
        """500 : passager ; la page de l'hébergeur va au journal, pas dans le message."""
        self.box.list_plan = ["500"]
        exc = self.echec(self.box.client().list_torrents, retry=False)
        message = str(exc)
        self.assertEqual((exc.transient, exc.status), (True, 500))
        self.assertIn("HTTP 500", message)
        for interdit in ("<", ">", "Ultra", "Application Error", "DOCTYPE", "html", "127.0.0.1"):
            self.assertNotIn(interdit, message)
        journal = self.journal()
        self.assertIn("HTTP 500 sur %s : " % ACTION, journal)
        self.assertIn("Error 500: Application Error | Ultra.cc", journal)
        self.assertNotIn("<html>", journal)                            # page nettoyée de ses balises
        self.assertNotIn("127.0.0.1", journal)

    def test_journal_dans_le_bac_a_sable(self):
        """Le journal alimenté par les tests est celui du bac à sable, jamais celui de l'utilisateur."""
        self.assertTrue(paths.is_sandboxed())
        self.assertEqual(paths.log_path().parent, Path(h.SANDBOX))

    def test_autres_5xx_passagers(self):
        """502, 503, 504 (page nginx) : passagers, message sans texte de la page."""
        for code in (502, 503, 504):
            with self.subTest(code=code):
                srv = self.scripte(defaut=(code, {"Content-Type": "text/html"}, NGINX_502))
                exc = self.echec(self.client(srv).list_torrents, retry=False)
                self.assertEqual((exc.transient, exc.status), (True, code))
                self.assertIn("HTTP %d" % code, str(exc))
                for interdit in ("<", "nginx", "Bad Gateway", "127.0.0.1"):
                    self.assertNotIn(interdit, str(exc))
                self.assertIn("HTTP %d sur %s : 502 Bad Gateway 502 Bad Gateway nginx" % (code, ACTION),
                              self.journal())

    def test_page_5xx_tronquee_au_journal(self):
        """Une page d'erreur énorme n'occupe que quelques lignes du journal."""
        srv = self.scripte(defaut=(500, {}, b"<p>" + b"x" * 50000 + b"</p>"))
        self.echec(self.client(srv).list_torrents, retry=False)
        self.assertLess(len(self.journal()), 500)

    def test_500_sans_corps(self):
        """Un 500 sans page d'erreur reste classé passager."""
        srv = self.scripte(defaut=(500, {}, b""))
        exc = self.echec(self.client(srv).list_torrents, retry=False)
        self.assertEqual((exc.transient, exc.status), (True, 500))

    def test_autre_4xx_definitif(self):
        """Un autre code 4xx : définitif, statut conservé, pas de nouvel essai."""
        srv = self.scripte(defaut=(418, {}, b"<html><body>Je suis une <b>th\xc3\xa9i\xc3\xa8re</b></body></html>"))
        exc = self.echec(self.client(srv).list_torrents)
        self.assertEqual((exc.transient, exc.status), (False, 418))
        self.assertIn("Erreur HTTP 418", str(exc))
        self.assertNotIn("<", str(exc))
        self.assertEqual(len(srv.requetes), 1)

    def test_delai_depasse_passager(self):
        """Serveur qui ne répond pas, ou se fige en pleine réponse : délai dépassé, passager."""
        for comportement in (ne_repond_jamais, fige_en_plein_corps):
            with self.subTest(comportement=comportement.__name__):
                srv = self.brut(comportement)
                exc = self.echec(self.client(srv, timeout=1).list_torrents, retry=False)
                self.assertTrue(exc.transient)
                self.assertIsNone(exc.status)
                self.assertEqual(str(exc), "Délai d'attente dépassé (1 s).")
                self.assertEqual(len(srv.requetes), 1)
                srv.stop()

    def test_connexion_fermee_sans_reponse(self):
        """Le serveur raccroche sans répondre : passager."""
        srv = self.brut(ferme_sans_repondre)
        exc = self.echec(self.client(srv).list_torrents, retry=False)
        self.assertTrue(exc.transient)
        self.assertEqual(str(exc), "Connexion interrompue par la seedbox (RemoteDisconnected).")

    def test_reponse_tronquee(self):
        """La réponse s'arrête avant la longueur annoncée : passager."""
        srv = self.brut(reponse_tronquee)
        exc = self.echec(self.client(srv).list_torrents, retry=False)
        self.assertTrue(exc.transient)
        self.assertIsNone(exc.status)
        self.assertEqual(str(exc), "Connexion interrompue par la seedbox (IncompleteRead).")

    def test_connexion_reinitialisee(self):
        """RST avant la réponse ou en plein corps : passager, sans jargon réseau brut."""
        for comportement in (rst_sans_repondre, rst_en_plein_corps):
            with self.subTest(comportement=comportement.__name__):
                srv = self.brut(comportement)
                exc = self.echec(self.client(srv).list_torrents, retry=False)
                self.assertTrue(exc.transient)
                self.assertIsNone(exc.status)
                self.assertRegex(str(exc), r"^Connexion interrompue par la seedbox \(\w+\)\.$")
                self.assertNotIn("WinError", str(exc))

    def test_coupure_reessayee_puis_abandonnee(self):
        """Une coupure à chaque essai : list_torrents insiste, puis rend l'erreur passagère."""
        srv = self.brut(ferme_sans_repondre)
        exc = self.echec(self.client(srv).list_torrents)
        self.assertTrue(exc.transient)
        self.assertEqual(len(srv.requetes), essais())

    def test_port_ferme(self):
        """Personne n'écoute : « Connexion impossible », passager."""
        client = RuTorrentClient("http://127.0.0.1:%d/rutorrent" % h.free_port(), "u", "p", timeout=1)
        exc = self.echec(client.list_torrents, retry=False)
        self.assertTrue(exc.transient)
        self.assertIsNone(exc.status)
        self.assertTrue(str(exc).startswith("Connexion impossible : "), str(exc))

    def test_erreur_ssl_definitive(self):
        """Échec de la poignée de main TLS : définitif, avec le conseil sur le certificat."""
        srv = self.brut(repond_en_clair)
        client = RuTorrentClient("https://127.0.0.1:%d/rutorrent" % srv.port, "u", "p", timeout=5)
        exc = self.echec(client.list_torrents)
        self.assertFalse(exc.transient)
        self.assertIsNone(exc.status)
        self.assertTrue(str(exc).startswith("Erreur SSL : "), str(exc))
        self.assertIn("Vérifier le certificat SSL", str(exc))


# --------------------------------------------------------------------------- #
#  HTTPS
# --------------------------------------------------------------------------- #

class TestHttps(CasClient):

    def test_certificat_auto_signe_refuse_par_defaut(self):
        """verify_ssl=True : un certificat auto-signé est refusé avant tout échange."""
        srv = self.scripte(defaut=seedbox_vide, tls=True)
        exc = self.echec(self.client(srv).list_torrents)
        self.assertFalse(exc.transient)
        self.assertTrue(str(exc).startswith("Erreur SSL : "), str(exc))
        self.assertIn("Vérifier le certificat SSL", str(exc))
        self.assertEqual(srv.requetes, [])                 # ni identifiants ni requête envoyés

    def test_certificat_auto_signe_accepte_sur_demande(self):
        """verify_ssl=False : la même seedbox répond, en mode brut, identifiants transmis."""
        srv = self.scripte(defaut=seedbox_vide, tls=True)
        client = self.client(srv, verify_ssl=False)
        self.assertEqual(client.fetch_labels_and_dirs(), ([], ["/home/u/dl"], 0))
        self.assertEqual(srv.genres(), APPEL_BRUT)
        self.assertEqual({r["headers"].get("authorization") for r in srv.requetes}, {basic("u", "p")})

    def test_ajout_en_https(self):
        """Un envoi de fichier passe aussi par le canal HTTPS non vérifié."""
        srv = self.scripte(defaut=(200, {}, b"<html><body>ok</body></html>"), tls=True)
        chemin = h.make_torrent(h.temp_dir("rt-"), "Exemple")
        self.assertEqual(self.client(srv, verify_ssl=False).add_torrent(chemin, "/d", "L"), "ok")
        requete = srv.requetes[-1]
        parties = decoupe_multipart(requete["headers"]["content-type"], requete["body"])
        self.assertEqual(parties[-1]["contenu"], chemin.read_bytes())


# --------------------------------------------------------------------------- #
#  Verrou : un seul appel à la fois vers la seedbox
# --------------------------------------------------------------------------- #

class TestVerrou(CasSeedbox):

    def rafale(self, fils: int, tours: int):
        """`fils` fils, chacun avec SON client, enchaînent `tours` appels variés."""
        erreurs, depart = [], threading.Barrier(fils)

        def travail(numero):
            client = self.box.client()             # une instance par action, comme l'application
            try:
                depart.wait(10)
                for _ in range(tours):
                    if numero % 4 == 0:
                        client.known_hashes()
                    elif numero % 4 == 1:
                        client.list_torrents(retry=False)
                    elif numero % 4 == 2:
                        client.add_url(MAGNET)
                    else:
                        client.repair_settings_cache()
            except Exception as exc:               # noqa: BLE001 - rapporté au fil principal
                erreurs.append(exc)

        equipe = [threading.Thread(target=travail, args=(n,), daemon=True) for n in range(fils)]
        for fil in equipe:
            fil.start()
        for fil in equipe:
            fil.join(30)
        self.assertFalse(any(fil.is_alive() for fil in equipe), "un fil est resté bloqué")
        self.assertEqual(erreurs, [])

    def test_requetes_serialisees(self):
        """Plusieurs instances dans plusieurs fils : jamais deux requêtes en même temps."""
        self.box.slow = 0.03
        self.rafale(fils=8, tours=2)
        self.assertEqual(len(self.box.calls), 16)
        self.assertEqual(self.box.max_inflight, 1)

    def test_temoin_sans_verrou(self):
        """Contre-épreuve : sans le verrou du module, les mêmes fils se chevauchent."""
        self.box.slow = 0.25
        with mock.patch.object(rutorrent, "_SEEDBOX_LOCK", contextlib.nullcontext()):
            self.rafale(fils=4, tours=1)
        self.assertGreaterEqual(self.box.max_inflight, 2)

    def test_un_appel_attend_le_verrou(self):
        """Tant que le verrou est pris, rien ne part vers la seedbox."""
        client = self.box.client()
        resultat = []
        fil = threading.Thread(target=lambda: resultat.append(client.rtorrent_alive()), daemon=True)
        with rutorrent._SEEDBOX_LOCK:
            fil.start()
            fil.join(0.25)
            self.assertTrue(fil.is_alive())
            self.assertEqual(self.box.calls, [])
        fil.join(10)
        self.assertEqual(resultat, [True])
        self.assertEqual(self.box.calls, ["raw:system.client_version"])

    def test_verrou_commun_aux_instances(self):
        """Le verrou est celui du module, pas celui d'une instance."""
        self.assertIs(type(rutorrent._SEEDBOX_LOCK), type(threading.Lock()))
        self.assertFalse(hasattr(self.box.client(), "_lock"))

    def test_verrou_libere_apres_chaque_echec(self):
        """Quelle que soit l'erreur, le verrou est rendu : l'appel suivant passe."""
        scenarios = [
            ("500", lambda: self.scripte(defaut=(500, {}, h.ULTRA_500))),
            ("401", lambda: self.scripte(defaut=(401, {}, b"non"))),
            ("404", lambda: self.scripte(defaut=(404, {}, b""))),
            ("coupure", lambda: self.brut(ferme_sans_repondre)),
            ("tronquee", lambda: self.brut(reponse_tronquee)),
            ("rst", lambda: self.brut(rst_sans_repondre)),
        ]
        for nom, fabrique in scenarios:
            with self.subTest(scenario=nom):
                self.echec(self.client(fabrique()).list_torrents, retry=False)
                self.assertFalse(rutorrent._SEEDBOX_LOCK.locked())
        self.assertTrue(self.box.client().rtorrent_alive())


# --------------------------------------------------------------------------- #
#  Voie principale : XML-RPC brut
# --------------------------------------------------------------------------- #

class TestModeBrut(CasSeedbox):

    def test_voie_principale_sans_mode_list(self):
        """Étiquettes et dossiers sont lus en mode brut ; mode=list n'est jamais appelé."""
        self.assertEqual(self.box.client().fetch_labels_and_dirs(), LISTES)
        self.assertEqual(self.box.calls, APPEL_BRUT)
        self.assertEqual(RuTorrentClient._raw_unsupported, set())

    def test_requetes_brutes_a_l_octet(self):
        """Corps XML-RPC sans « mode= », Content-Type sans charset, pas de compression."""
        self.box.client().fetch_labels_and_dirs()
        liste, defaut = self.box.requests
        for requete in (liste, defaut):
            self.assertEqual(requete["method"], "POST")
            self.assertEqual(requete["path"], "/rutorrent" + ACTION)
            self.assertEqual(requete["headers"]["content-type"], "text/xml")
            self.assertEqual(requete["headers"]["accept-encoding"], "identity")
            self.assertTrue(requete["body"].startswith(b"<?xml"))
            self.assertNotIn(b"mode=", requete["body"])
        self.assertEqual(xmlrpc.client.loads(liste["body"]),
                         (("", "main", "d.custom1=", "d.directory=", "d.name=", "d.is_multi_file="),
                          "d.multicall2"))
        self.assertEqual(xmlrpc.client.loads(defaut["body"]), ((), "directory.default"))

    def test_etiquettes_decodees_dedoublonnees_triees(self):
        """Étiquettes url-décodées, sans doublon ni vide, triées sans tenir compte de la casse."""
        self.box.torrents = [
            ("H1", "zeta", "/d/a", "a", 0),
            ("H2", "Alpha", "/d/a", "b", 0),
            ("H3", "beta", "/d/a", "c", 0),
            ("H4", "%C3%89t%C3%A9%20%26%20Co", "/d/a", "d", 0),
            ("H5", "Alpha", "/d/a", "e", 0),
            ("H6", "", "/d/a", "f", 0),
            ("H7", "%20%20", "/d/a", "g", 0),
            ("H8", "  Bravo ", "/d/a", "h", 0),
            ("H9", "S%C3%A9ries", "/d/a", "i", 0),
        ]
        labels, dirs, count = self.box.client().fetch_labels_and_dirs()
        self.assertEqual(labels, ["Alpha", "beta", "Bravo", "Séries", "zeta", "Été & Co"])
        self.assertEqual(dirs, ["/d/a"])
        self.assertEqual(count, 9)

    def test_dossiers_tries_sans_casse(self):
        """Les dossiers aussi sont triés sans tenir compte de la casse."""
        self.box.torrents = [("H1", "", "/data/Zoo", "a", 0), ("H2", "", "/data/alpha", "b", 0),
                             ("H3", "", "/data/Beta", "c", 0), ("H4", "", "/data/alpha", "d", 0)]
        _, dirs, _ = self.box.client().fetch_labels_and_dirs()
        self.assertEqual(dirs, ["/data/alpha", "/data/Beta", "/data/Zoo"])

    def test_multi_fichiers_dossier_parent(self):
        """Multi-fichiers : d.directory = <dossier>/<nom>, on propose le dossier PARENT."""
        racine = "/home/u/downloads/rtorrent"
        self.box.torrents = [
            ("H1", "", racine + "/series/Show S01", "Show S01", 1),
            ("H2", "", racine + "/films", "film.mkv", 0),
            ("H3", "", racine + "/vrac/", "Autre nom", 1),              # dossier renommé : tel quel
            ("H4", "", racine + "/iso/debian.iso", "debian.iso", 0),    # fichier unique : tel quel
        ]
        _, dirs, count = self.box.client().fetch_labels_and_dirs()
        self.assertEqual(dirs, [racine, racine + "/films", racine + "/iso/debian.iso",
                                racine + "/series", racine + "/vrac"])
        self.assertNotIn(racine + "/series/Show S01", dirs)
        self.assertEqual(count, 4)

    def test_dossier_par_defaut_resolu_et_ajoute(self):
        """directory.default « ~/downloads/rtorrent » est résolu grâce à un dossier connu, puis ajouté."""
        self.box.torrents = [("H1", "Films", "/home/u/downloads/rtorrent/films", "film.mkv", 0)]
        labels, dirs, count = self.box.client().fetch_labels_and_dirs()
        self.assertEqual((labels, count), (["Films"], 1))
        self.assertEqual(dirs, ["/home/u/downloads/rtorrent", "/home/u/downloads/rtorrent/films"])

    def test_rien_d_invente_si_non_resoluble(self):
        """Aucun dossier connu ne contient « /downloads/rtorrent » : on s'abstient."""
        self.box.torrents = [("H1", "", "/data/media/films", "film.mkv", 0),
                             ("H2", "", "/srv/downloads/rtorrent2/x", "x", 0)]
        _, dirs, _ = self.box.client().fetch_labels_and_dirs()
        self.assertEqual(dirs, ["/data/media/films", "/srv/downloads/rtorrent2/x"])
        self.assertEqual(self.box.calls, APPEL_BRUT)

    def test_seedbox_sans_torrent(self):
        """Aucun torrent et un dossier par défaut en « ~/ » : listes vides, rien d'inventé."""
        self.box.torrents = []
        self.assertEqual(self.box.client().fetch_labels_and_dirs(), ([], [], 0))
        self.assertEqual(self.box.calls, APPEL_BRUT)

    def test_dossier_par_defaut_absolu_sur_seedbox_vide(self):
        """Un dossier par défaut absolu garantit une proposition, même sans aucun torrent."""
        srv = self.scripte(defaut=seedbox_vide)
        self.assertEqual(self.client(srv).fetch_labels_and_dirs(), ([], ["/home/u/dl"], 0))
        self.assertEqual(srv.genres(), APPEL_BRUT)

    def test_panne_de_directory_default_sans_consequence(self):
        """Si directory.default échoue, les listes déjà lues sont rendues quand même, sans insister."""
        client = self.box.client()
        vrai = client._xmlrpc

        def capricieux(methode, params=()):
            if methode == "directory.default":
                raise RuTorrentError("rTorrent ne répond pas", transient=True)
            return vrai(methode, params)

        client._xmlrpc = capricieux
        with pauses_enregistrees() as pauses:
            self.assertEqual(client.fetch_labels_and_dirs(), LISTES)
        self.assertEqual(pauses, [])
        self.assertEqual(self.box.calls, ["raw:d.multicall2"])

    def test_lignes_malformees_ignorees_mais_comptees(self):
        """Lignes trop courtes, non-listes, dossiers relatifs : ignorés, sans faire échouer la lecture."""
        lignes = [
            ["L%201", "/dl/a", "n", 0],
            ["trop", "court"],
            "pas une liste",
            ["", "relatif/x", "n", 0],
            ["L2", "/dl/Show/", "Show", 1],
            [None, None, None, None],
            ["L3", "/", "n", 0],
        ]
        srv = self.scripte(script=[reponse_brute(lignes), reponse_brute("/dl/")])
        self.assertEqual(self.client(srv).fetch_labels_and_dirs(),
                         (["L 1", "L2", "L3"], ["/dl", "/dl/a"], 7))

    def test_reponse_brute_qui_n_est_pas_une_liste(self):
        """d.multicall2 renvoie autre chose qu'une liste : mode brut écarté, repli sur mode=list."""
        srv = self.scripte(script=[reponse_brute(0)],
                           defaut=reponse_liste({"H1": ligne(etiquette="L", extras=("0", "/dl"))}))
        self.assertEqual(self.client(srv).fetch_labels_and_dirs(), (["L"], ["/dl"], 1))
        self.assertEqual(srv.genres(), ["raw:d.multicall2", "list"])


class TestXmlRpc(CasSeedbox):
    """_xmlrpc : interprétation de ce que le relais httprpc renvoie avec un statut 200."""

    def test_valeur_rendue(self):
        """La valeur XML-RPC est rendue telle quelle (chaîne, liste...)."""
        client = self.box.client()
        self.assertEqual(client._xmlrpc("system.client_version"), "0.9.8")
        self.assertEqual(client._xmlrpc("directory.default"), "~/downloads/rtorrent")
        self.assertEqual(client._xmlrpc("d.multicall2", ("", "main", "d.hash=")),
                         [["AAAA"], ["BBBB"], ["CCCC"]])

    def test_corps_vides_ou_faux(self):
        """« false », « "" », « null » ou rien (v3.10 à v5.2) : rTorrent ne répond pas, passager."""
        for corps in (b"false", b'""', b"null", b"", b"  false\r\n"):
            with self.subTest(corps=corps):
                srv = self.scripte(defaut=(200, {"Content-Type": "application/json"}, corps))
                exc = self.echec(self.client(srv)._xmlrpc, "system.client_version")
                self.assertTrue(exc.transient)
                self.assertEqual(str(exc), "rTorrent ne répond pas")
                srv.stop()

    def test_page_html_en_200(self):
        """Page de maintenance servie en 200 : passager, page au journal, message sobre."""
        srv = self.scripte(defaut=(200, {"Content-Type": "text/html"},
                                   b"<!DOCTYPE html><html><body><h1>Maintenance Ultra.cc</h1></body></html>"))
        exc = self.echec(self.client(srv)._xmlrpc, "d.multicall2", ("", "main", "d.hash="))
        self.assertTrue(exc.transient)
        self.assertEqual(str(exc), "réponse illisible de la seedbox")
        self.assertIn("XML-RPC d.multicall2 : page HTML reçue : Maintenance Ultra.cc", self.journal())

    def test_reponse_non_xml(self):
        """Du JSON en réponse à du XML : le serveur n'a pas de mode brut, erreur définitive."""
        self.box.reset("noraw")
        exc = self.echec(self.box.client()._xmlrpc, "system.client_version")
        self.assertFalse(exc.transient)
        self.assertIsNone(exc.status)
        self.assertIn("brut indisponible", str(exc))

    def test_faute_xmlrpc(self):
        """Une faute XML-RPC est définitive et cite la méthode et le motif."""
        self.box.reset("fault")
        exc = self.echec(self.box.client()._xmlrpc, "d.multicall2", ("", "main", "d.hash="))
        self.assertFalse(exc.transient)
        self.assertEqual(str(exc), "rTorrent a refusé d.multicall2 : Method not defined")

    def test_xml_illisible(self):
        """XML tronqué ou sans valeur : erreur définitive, jamais une exception brute."""
        for corps in (b"<?xml version='1.0'?><methodResponse><params>",
                      b"<methodResponse><params></params></methodResponse>"):
            with self.subTest(corps=corps):
                srv = self.scripte(defaut=(200, {"Content-Type": "text/xml"}, corps))
                exc = self.echec(self.client(srv)._xmlrpc, "system.client_version")
                self.assertFalse(exc.transient)
                self.assertTrue(str(exc).startswith("réponse XML-RPC illisible ("), str(exc))
                srv.stop()

    def test_reponse_sans_prologue_xml(self):
        """Une réponse commençant directement par <methodResponse> est acceptée."""
        corps = xmlrpc.client.dumps(("0.9.8",), methodresponse=True).encode("utf-8")
        self.assertTrue(corps.startswith(b"<?xml"))
        corps = b"\r\n  " + corps[corps.index(b"<methodResponse"):]
        srv = self.scripte(defaut=(200, {"Content-Type": "text/xml"}, corps))
        self.assertEqual(self.client(srv)._xmlrpc("system.client_version"), "0.9.8")


class TestPannesModeBrut(CasSeedbox):

    def test_cache_corrompu_sans_effet(self):
        """« poisoned » : mode=list est en 500, mais le mode brut rend quand même les listes."""
        self.box.reset("poisoned")
        self.assertEqual(self.box.client().fetch_labels_and_dirs(), LISTES)
        self.assertEqual(self.box.calls, APPEL_BRUT)
        self.assertEqual(self.box.mode, "poisoned")          # et sans « réparer » par mégarde

    def test_rtorrent_injoignable(self):
        """« dead » : erreur passagère après 1 + len(RETRY_DELAYS) essais bruts, sans repli."""
        self.box.reset("dead")
        with pauses_enregistrees() as pauses:
            exc = self.echec(self.box.client().fetch_labels_and_dirs)
        self.assertTrue(exc.transient)
        self.assertEqual(str(exc), "rTorrent ne répond pas")
        self.assertEqual(self.box.calls, ["raw:d.multicall2"] * essais())
        self.assertEqual(essais(), 4)
        self.assertEqual(pauses, list(RuTorrentClient.RETRY_DELAYS))
        self.assertNotIn("list", self.box.calls)
        self.assertEqual(RuTorrentClient._raw_unsupported, set())

    def test_rtorrent_injoignable_avec_les_vraies_pauses(self):
        """Même scénario sans bouchon sur time.sleep (pauses raccourcies par helpers.py)."""
        self.box.reset("dead")
        exc = self.echec(self.box.client().fetch_labels_and_dirs)
        self.assertTrue(exc.transient)
        self.assertEqual(self.box.calls, ["raw:d.multicall2"] * essais())

    def test_repli_sur_mode_list(self):
        """« noraw » et « fault » : repli sur mode=list, retenu pour les clients suivants."""
        for mode, motif in (("noraw", "brut indisponible"), ("fault", "Method not defined")):
            with self.subTest(mode=mode):
                self.box.reset(mode)
                debut = len(self.journal())
                with pauses_enregistrees() as pauses:
                    self.assertEqual(self.box.client().fetch_labels_and_dirs(), LISTES)
                self.assertEqual(self.box.calls, ["raw:d.multicall2", "list"])
                self.assertEqual(pauses, [])                 # pas d'essais répétés : c'est définitif
                self.assertEqual(RuTorrentClient._raw_unsupported, {self.box.url})
                ajoute = self.journal()[debut:]
                self.assertIn("mode brut indisponible, repli sur mode=list", ajoute)
                self.assertIn(motif, ajoute)

                # un NOUVEAU client (l'application en crée un par action) ne retente pas le mode brut
                self.box.calls.clear()
                self.assertEqual(self.box.client().fetch_labels_and_dirs(), LISTES)
                self.assertEqual(self.box.client().known_hashes(), {"AAAA", "BBBB", "CCCC"})
                self.assertEqual(self.box.calls, ["list", "list"])

    def test_memoire_du_repli_par_url_normalisee(self):
        """Le repli est retenu par seedbox : même URL écrite autrement = même seedbox."""
        self.box.reset("noraw")
        self.box.client().fetch_labels_and_dirs()
        self.box.calls.clear()
        for variante in (self.box.url + "/", self.box.url + ACTION, "  " + self.box.url + "/index.html"):
            RuTorrentClient(variante, "u", "p", timeout=5).fetch_labels_and_dirs()
        self.assertEqual(self.box.calls, ["list"] * 3)

    def test_memoire_du_repli_propre_a_chaque_seedbox(self):
        """Une seedbox sans mode brut ne prive pas les AUTRES seedbox du mode brut."""
        autre = h.FakeSeedbox()
        self.addCleanup(autre.stop)
        self.box.reset("noraw")
        self.box.client().fetch_labels_and_dirs()
        self.assertEqual(autre.client().fetch_labels_and_dirs(), LISTES)
        self.assertEqual(autre.calls, APPEL_BRUT)
        self.assertEqual(RuTorrentClient._raw_unsupported, {self.box.url})

    def test_non_autorise(self):
        """« unauth » : un seul appel, ni nouvel essai ni repli sur mode=list."""
        self.box.reset("unauth")
        with pauses_enregistrees() as pauses:
            exc = self.echec(self.box.client().fetch_labels_and_dirs)
        self.assertEqual((exc.transient, exc.status), (False, 401))
        self.assertEqual(self.box.calls, ["raw:d.multicall2"])
        self.assertEqual(pauses, [])
        self.assertEqual(RuTorrentClient._raw_unsupported, set())

    def test_403_et_404_sans_repli(self):
        """403 ou 404 en mode brut : l'accès ou l'URL sont en cause, mode=list n'y changerait rien."""
        for code in (403, 404):
            with self.subTest(code=code):
                srv = self.scripte(defaut=(code, {}, b"non"))
                exc = self.echec(self.client(srv).fetch_labels_and_dirs)
                self.assertEqual((exc.transient, exc.status), (False, code))
                self.assertEqual(srv.genres(), ["raw:d.multicall2"])
                self.assertEqual(RuTorrentClient._raw_unsupported, set())
                exc = self.echec(self.client(srv).known_hashes)
                self.assertEqual(exc.status, code)
                self.assertEqual(srv.genres(), ["raw:d.multicall2"] * 2)
                self.assertEqual(RuTorrentClient._raw_unsupported, set())

    def test_500_brut_deux_fois_puis_succes(self):
        """Deux 500 puis une réponse : réessayé EN MODE BRUT, qui n'est pas déclaré indisponible."""
        self.box.raw_failures = 2
        with pauses_enregistrees() as pauses:
            self.assertEqual(self.box.client().fetch_labels_and_dirs(), LISTES)
        self.assertEqual(self.box.calls, ["raw:d.multicall2"] * 3 + ["raw:directory.default"])
        self.assertEqual(pauses, list(RuTorrentClient.RETRY_DELAYS[:2]))
        self.assertEqual(RuTorrentClient._raw_unsupported, set())
        self.assertEqual(self.box.raw_failures, 0)

    def test_500_brut_jusqu_au_dernier_essai(self):
        """Le dernier essai permis suffit : trois 500 puis une réponse."""
        self.box.raw_failures = len(RuTorrentClient.RETRY_DELAYS)
        self.assertEqual(self.box.client().fetch_labels_and_dirs(), LISTES)
        self.assertEqual(self.box.calls, ["raw:d.multicall2"] * essais() + ["raw:directory.default"])

    def test_500_brut_persistant(self):
        """500 à chaque essai brut : erreur passagère, toujours sans repli ni mise à l'écart."""
        self.box.raw_failures = 99
        exc = self.echec(self.box.client().fetch_labels_and_dirs)
        self.assertEqual((exc.transient, exc.status), (True, 500))
        self.assertEqual(self.box.calls, ["raw:d.multicall2"] * essais())
        self.assertEqual(RuTorrentClient._raw_unsupported, set())
        self.assertNotIn("Ultra", str(exc))

    def test_page_html_en_200_reessayee_sans_repli(self):
        """Page de maintenance en 200 : passager, réessayé en brut, mode brut conservé."""
        srv = self.scripte(defaut=(200, {"Content-Type": "text/html"},
                                   b"<html><body>Maintenance en cours</body></html>"))
        exc = self.echec(self.client(srv).fetch_labels_and_dirs)
        self.assertTrue(exc.transient)
        self.assertEqual(srv.genres(), ["raw:d.multicall2"] * essais())
        self.assertEqual(RuTorrentClient._raw_unsupported, set())

    def test_coupure_puis_succes(self):
        """Une coupure de connexion suivie d'une réponse : la lecture aboutit en mode brut."""
        vrai = RuTorrentClient._post
        restants = [2]

        def capricieux(client, endpoint, body, content_type="", extra=None):
            if restants[0] > 0:
                restants[0] -= 1
                raise RuTorrentError("Connexion interrompue par la seedbox (RemoteDisconnected).",
                                     transient=True)
            return vrai(client, endpoint, body, content_type, extra)

        with mock.patch.object(RuTorrentClient, "_post", capricieux):
            self.assertEqual(self.box.client().fetch_labels_and_dirs(), LISTES)
        self.assertEqual(self.box.calls, APPEL_BRUT)
        self.assertEqual(RuTorrentClient._raw_unsupported, set())


class TestDossiers(unittest.TestCase):
    """_dir_from et default_directory, sans réseau."""

    def test_dir_from_fichier_unique(self):
        """Fichier unique : d.directory est déjà le dossier choisi."""
        self.assertEqual(RuTorrentClient._dir_from("/dl/films", "film.mkv", 0), "/dl/films")
        self.assertEqual(RuTorrentClient._dir_from("/dl/films/", "film.mkv", "0"), "/dl/films")
        self.assertEqual(RuTorrentClient._dir_from("/dl/film.mkv", "film.mkv", 0), "/dl/film.mkv")

    def test_dir_from_multi_fichiers(self):
        """Multi-fichiers : on remonte d'un cran si le dossier finit par le nom du torrent."""
        for multi in (1, "1"):
            with self.subTest(multi=multi):
                self.assertEqual(RuTorrentClient._dir_from("/dl/series/Show S01", "Show S01", multi),
                                 "/dl/series")
                self.assertEqual(RuTorrentClient._dir_from("/dl/series/Show S01/", "Show S01", multi),
                                 "/dl/series")
        self.assertEqual(RuTorrentClient._dir_from("/dl/series/Renomme", "Show S01", 1),
                         "/dl/series/Renomme")
        self.assertEqual(RuTorrentClient._dir_from("/dl/series/XShow", "Show", 1), "/dl/series/XShow")
        self.assertEqual(RuTorrentClient._dir_from("/dl/series", "", 1), "/dl/series")

    def test_dir_from_chemins_inexploitables(self):
        """Chemin vide, relatif ou absent : aucune proposition."""
        for chemin in ("", None, "relatif/dossier", "~/downloads", "C:\\dl", "/"):
            with self.subTest(chemin=chemin):
                self.assertEqual(RuTorrentClient._dir_from(chemin, "nom", 0), "")

    def dossier_par_defaut(self, valeur, connus=()):
        client = RuTorrentClient("http://127.0.0.1:1/rutorrent")          # jamais contacté
        appels = []

        def faux(methode, params=()):
            appels.append(methode)
            if isinstance(valeur, Exception):
                raise valeur
            return valeur

        client._xmlrpc = faux
        resultat = client.default_directory(connus)
        self.assertEqual(appels, ["directory.default"])
        return resultat

    def test_defaut_absolu(self):
        """Un chemin absolu est rendu tel quel, sans barre finale, même sans dossier connu."""
        self.assertEqual(self.dossier_par_defaut("/home/u/downloads"), "/home/u/downloads")
        self.assertEqual(self.dossier_par_defaut("  /home/u/downloads/ \n"), "/home/u/downloads")

    def test_defaut_tilde_resolu(self):
        """« ~/... » est résolu avec le préfixe d'un dossier connu qui le contient."""
        connus = ["/data/ailleurs", "/home/u/downloads/rtorrent/films"]
        self.assertEqual(self.dossier_par_defaut("~/downloads/rtorrent", connus),
                         "/home/u/downloads/rtorrent")
        self.assertEqual(self.dossier_par_defaut("~/downloads/rtorrent/", connus),
                         "/home/u/downloads/rtorrent")
        self.assertEqual(self.dossier_par_defaut("~/downloads/rtorrent",
                                                 {"/home9/bob/downloads/rtorrent"}),
                         "/home9/bob/downloads/rtorrent")

    def test_defaut_tilde_non_resoluble(self):
        """Sans dossier connu qui contienne la fin du chemin, on s'abstient."""
        cas = [
            (),                                              # aucun dossier connu
            ["/data/media/films"],                           # rien de commun
            ["/home/u/downloads/rtorrent2/films"],           # « rtorrent2 » n'est pas « rtorrent »
            ["/downloads/rtorrent/films"],                   # le dossier personnel serait vide
            ["/home/u/Downloads/rtorrent"],                  # casse différente
        ]
        for connus in cas:
            with self.subTest(connus=connus):
                self.assertEqual(self.dossier_par_defaut("~/downloads/rtorrent", connus), "")

    def test_defaut_toujours_ancetre_d_un_dossier_connu(self):
        """Le dossier résolu est toujours un dossier connu ou l'un de ses parents : rien d'inventé."""
        connus = ["/mnt/a/home/u/downloads/rtorrent/x/y", "/srv/z"]
        resultat = self.dossier_par_defaut("~/downloads/rtorrent", connus)
        self.assertEqual(resultat, "/mnt/a/home/u/downloads/rtorrent")
        self.assertTrue(any((d + "/").startswith(resultat + "/") for d in connus))

    def test_defaut_valeurs_inexploitables(self):
        """Valeur vide, relative, « ~ » seul, non textuelle : chaîne vide."""
        connus = ["/home/u/downloads/rtorrent"]
        for valeur in ("", "   ", "~", "~/", "downloads/rtorrent", "~u/downloads", None, 0, []):
            with self.subTest(valeur=valeur):
                self.assertEqual(self.dossier_par_defaut(valeur, connus), "")

    def test_defaut_en_cas_d_erreur(self):
        """rTorrent injoignable ou refus : chaîne vide, jamais d'exception."""
        for erreur in (RuTorrentError("rTorrent ne répond pas", transient=True),
                       RuTorrentError("refus", status=401)):
            with self.subTest(erreur=str(erreur)):
                self.assertEqual(self.dossier_par_defaut(erreur, ["/home/u/downloads/rtorrent"]), "")


# --------------------------------------------------------------------------- #
#  Voie de repli : mode=list
# --------------------------------------------------------------------------- #

class TestModeListe(CasSeedbox):

    def setUp(self):
        super().setUp()
        RuTorrentClient._raw_unsupported.add(self.box.url)       # force la voie de repli

    def test_memes_listes_que_le_mode_brut(self):
        """La voie de repli rend les mêmes étiquettes, dossiers et compte que le mode brut."""
        self.assertEqual(self.box.client().fetch_labels_and_dirs(), LISTES)
        self.assertEqual(self.box.calls, ["list"])

    def test_requete_a_l_octet(self):
        """mode=list, avec les colonnes d.is_multi_file et d.directory demandées en fin de ligne."""
        client = self.box.client()
        client.fetch_labels_and_dirs()
        requete = self.box.requests[-1]
        self.assertEqual((requete["method"], requete["path"]), ("POST", "/rutorrent" + ACTION))
        self.assertEqual(requete["headers"]["content-type"],
                         "application/x-www-form-urlencoded; charset=UTF-8")
        self.assertEqual(requete["body"], b"mode=list&cmd=d.is_multi_file%3D&cmd=d.directory%3D")
        self.assertEqual(RuTorrentClient.LIST_EXTRA_CMDS, ("d.is_multi_file=", "d.directory="))
        client.list_torrents()
        self.assertEqual(self.box.requests[-1]["body"], b"mode=list")

    def test_liste_rendue_par_empreinte(self):
        """list_torrents rend le dictionnaire empreinte -> champs."""
        torrents = self.box.client().list_torrents()
        self.assertEqual(sorted(torrents), ["AAAA", "BBBB", "CCCC"])
        self.assertEqual(torrents["BBBB"][rutorrent.FIELD_NAME], "film.mkv")
        self.assertEqual(torrents["BBBB"][rutorrent.FIELD_LABEL], "Films")
        self.assertEqual(torrents["BBBB"][rutorrent.FIELD_BASE_PATH],
                         "/home/u/downloads/rtorrent/films/film.mkv")

    def test_500_puis_illisible_puis_succes(self):
        """Deux incidents passagers de nature différente, puis la liste : un seul appel côté appelant."""
        self.box.list_plan = ["500", "garbage"]
        with pauses_enregistrees() as pauses:
            self.assertEqual(sorted(self.box.client().list_torrents()), ["AAAA", "BBBB", "CCCC"])
        self.assertEqual(self.box.calls, ["list"] * 3)
        self.assertEqual(pauses, list(RuTorrentClient.RETRY_DELAYS[:2]))

    def test_500_persistant(self):
        """500 à chaque essai : erreur passagère après 1 + len(RETRY_DELAYS) appels."""
        self.box.list_plan = ["500"] * 10
        with pauses_enregistrees() as pauses:
            exc = self.echec(self.box.client().list_torrents)
        self.assertEqual((exc.transient, exc.status), (True, 500))
        self.assertEqual(self.box.calls, ["list"] * essais())
        self.assertEqual(pauses, list(RuTorrentClient.RETRY_DELAYS))

    def test_illisible_persistant(self):
        """Du HTML en 200 à chaque essai : passager, page au journal, message sobre."""
        self.box.list_plan = ["garbage"] * 10
        exc = self.echec(self.box.client().list_torrents)
        self.assertTrue(exc.transient)
        self.assertIsNone(exc.status)
        self.assertIn("réponse illisible de la seedbox", str(exc))
        self.assertNotIn("<", str(exc))
        self.assertEqual(self.box.calls, ["list"] * essais())
        self.assertEqual(self.journal().count("liste illisible : maintenance"), essais())

    def test_derniere_erreur_rapportee(self):
        """Après épuisement des essais, c'est la DERNIÈRE erreur qui est levée."""
        self.box.list_plan = ["garbage"] * (essais() - 1) + ["500"]
        exc = self.echec(self.box.client().list_torrents)
        self.assertEqual(exc.status, 500)
        self.box.reset()
        RuTorrentClient._raw_unsupported.add(self.box.url)
        self.box.list_plan = ["500"] * (essais() - 1) + ["garbage"]
        exc = self.echec(self.box.client().list_torrents)
        self.assertIsNone(exc.status)
        self.assertIn("illisible", str(exc))

    def test_liste_vide_valide(self):
        """Une liste vide est une réponse valable : pas de nouvel essai."""
        self.box.list_plan = ["empty"]
        self.assertEqual(self.box.client().list_torrents(), {})
        self.assertEqual(self.box.calls, ["list"])
        self.box.list_plan = ["empty"]
        self.assertEqual(self.box.client().fetch_labels_and_dirs(), ([], [], 0))

    def test_401_sans_insister(self):
        """401 : levé aussitôt, même avec retry=True."""
        self.box.list_plan = ["401"]
        with pauses_enregistrees() as pauses:
            exc = self.echec(self.box.client().list_torrents)
        self.assertEqual((exc.transient, exc.status), (False, 401))
        self.assertEqual(self.box.calls, ["list"])
        self.assertEqual(pauses, [])

    def test_401_apres_un_incident_passager(self):
        """Un 401 qui suit un 500 interrompt la série d'essais."""
        self.box.list_plan = ["500", "401"]
        exc = self.echec(self.box.client().list_torrents)
        self.assertEqual(exc.status, 401)
        self.assertEqual(self.box.calls, ["list"] * 2)

    def test_retry_false_un_seul_essai(self):
        """retry=False : un seul appel, quelle que soit l'erreur."""
        for plan in ("500", "garbage", "401"):
            with self.subTest(plan=plan):
                self.box.reset()
                self.box.list_plan = [plan]
                with pauses_enregistrees() as pauses:
                    exc = self.echec(self.box.client().list_torrents, retry=False)
                self.assertEqual(exc.transient, plan != "401")
                self.assertEqual(self.box.calls, ["list"])
                self.assertEqual(pauses, [])

    def test_lecture_complete_avec_incidents(self):
        """fetch_labels_and_dirs profite des nouveaux essais de la voie de repli."""
        self.box.list_plan = ["500", "garbage", "500"]
        self.assertEqual(self.box.client().fetch_labels_and_dirs(), LISTES)
        self.assertEqual(self.box.calls, ["list"] * 4)

    def test_cache_corrompu_fatal_en_mode_list(self):
        """« poisoned » : sans mode brut, la lecture échoue (c'est tout l'intérêt du mode brut)."""
        self.box.reset("poisoned")
        RuTorrentClient._raw_unsupported.add(self.box.url)
        exc = self.echec(self.box.client().fetch_labels_and_dirs)
        self.assertEqual((exc.transient, exc.status), (True, 500))
        self.assertEqual(self.box.calls, ["list"] * essais())

    def test_json_inattendu(self):
        """JSON valide mais pas un objet (false, liste, nombre) : passager."""
        for corps in (b"false", b"[]", b"null", b"42", b'"texte"'):
            with self.subTest(corps=corps):
                srv = self.scripte(defaut=(200, {"Content-Type": "application/json"}, corps))
                exc = self.echec(self.client(srv).list_torrents, retry=False)
                self.assertTrue(exc.transient)
                self.assertEqual(str(exc), "réponse inattendue de la seedbox")
                srv.stop()

    def test_cle_t_absente_ou_inattendue(self):
        """« t » absent, nul ou tableau vide (PHP encode ainsi un tableau vide) : aucun torrent."""
        for corps in (b"{}", b'{"t": []}', b'{"t": null}', b'{"t": ["x"]}', b'{"cid": 3}'):
            with self.subTest(corps=corps):
                srv = self.scripte(defaut=(200, {"Content-Type": "application/json"}, corps))
                self.assertEqual(self.client(srv).list_torrents(retry=False), {})
                srv.stop()

    def test_lignes_de_la_liste(self):
        """Étiquettes décodées (URL puis échappement ruTorrent), lignes anormales ignorées mais comptées."""
        torrents = {
            "H1": ligne("Show S01", "S%C3%A9ries", "/dl/series/Show S01", ("1", "/dl/series/Show S01")),
            "H2": ligne("film.mkv", 'Mes \\"Films\\"', "/dl/films/film.mkv", ("0", "/dl/films")),
            "H3": ligne("arrete.mkv", "zeta", "", ("0", "/dl/arretes")),       # torrent arrêté
            "H4": "pas une liste",
            "H5": ["trop", "court"],
            "H6": ligne("sans.mkv", "%20", "/dl/vieux/sans.mkv"),            # serveur sans colonnes ajoutées
            "H7": ligne("x", "alpha", "", ("0", "/dl/Mes \\\\ \\\"Docs\\\"")),
        }
        srv = self.scripte(defaut=reponse_liste(torrents))
        RuTorrentClient._raw_unsupported.add(srv.url)
        labels, dirs, count = self.client(srv).fetch_labels_and_dirs()
        self.assertEqual(labels, ["alpha", 'Mes "Films"', "Séries", "zeta"])
        self.assertEqual(dirs, ["/dl/arretes", "/dl/films", '/dl/Mes \\ "Docs"', "/dl/series", "/dl/vieux"])
        self.assertEqual(count, 7)
        self.assertEqual(srv.genres(), ["list"])


class TestLectureLignes(unittest.TestCase):
    """_torrent_dir et _unescape, sans réseau."""

    def test_unescape(self):
        """Une seule couche d'échappement est retirée : \\\\ -> \\ et \\" -> "."""
        self.assertEqual(RuTorrentClient._unescape('Mes \\"Films\\"'), 'Mes "Films"')
        self.assertEqual(RuTorrentClient._unescape("C:\\\\dl\\\\x"), "C:\\dl\\x")
        self.assertEqual(RuTorrentClient._unescape('a\\\\\\"b'), 'a\\"b')
        self.assertEqual(RuTorrentClient._unescape("sans échappement / été"), "sans échappement / été")
        self.assertEqual(RuTorrentClient._unescape("fin\\"), "fin\\")

    def test_unescape_valeurs_non_textuelles(self):
        """None et valeurs vides donnent une chaîne vide ; un nombre devient son texte."""
        self.assertEqual(RuTorrentClient._unescape(None), "")
        self.assertEqual(RuTorrentClient._unescape(""), "")
        self.assertEqual(RuTorrentClient._unescape(12), "12")

    def test_colonnes_lues_par_la_fin(self):
        """d.directory et d.is_multi_file sont lus en FIN de ligne, quel que soit le nombre de colonnes."""
        for colonnes in (26, 34, 35, 48):
            with self.subTest(colonnes=colonnes):
                unique = ligne("film.mkv", "", "/ancien/film.mkv", ("0", "/dl/films"), colonnes)
                multi = ligne("Show S01", "", "/ancien/Show S01", ("1", "/dl/series/Show S01"), colonnes)
                self.assertEqual(RuTorrentClient._torrent_dir(unique), "/dl/films")
                self.assertEqual(RuTorrentClient._torrent_dir(multi), "/dl/series")

    def test_torrent_arrete(self):
        """Torrent arrêté : d.base_path est vide, mais d.directory donne toujours le dossier."""
        self.assertEqual(RuTorrentClient._torrent_dir(ligne("film.mkv", "", "", ("0", "/dl/films"))),
                         "/dl/films")
        self.assertEqual(RuTorrentClient._torrent_dir(ligne("Show", "", "", ("1", "/dl/series/Show"))),
                         "/dl/series")

    def test_valeurs_echappees(self):
        """Dossier et nom sont déséchappés avant d'être comparés."""
        champs = ligne('Le \\"Show\\"', "", "", ("1", '/dl/series/Le \\"Show\\"'))
        self.assertEqual(RuTorrentClient._torrent_dir(champs), "/dl/series")
        champs = ligne("x", "", "", ("0", '/dl/Mes \\"Films\\"/'))
        self.assertEqual(RuTorrentClient._torrent_dir(champs), '/dl/Mes "Films"')

    def test_multi_en_entier(self):
        """Le drapeau multi-fichiers peut arriver comme entier."""
        self.assertEqual(RuTorrentClient._torrent_dir(ligne("Show", "", "", (1, "/dl/series/Show"))),
                         "/dl/series")
        self.assertEqual(RuTorrentClient._torrent_dir(ligne("f", "", "", (0, "/dl/films"))), "/dl/films")

    def test_repli_sur_base_path(self):
        """Serveur qui ignore les colonnes demandées : dossier parent de d.base_path."""
        self.assertEqual(RuTorrentClient._torrent_dir(ligne("film.mkv", "", "/dl/films/film.mkv")),
                         "/dl/films")
        self.assertEqual(RuTorrentClient._torrent_dir(ligne("Show", "", "/dl/series/Show/")),
                         "/dl/series")
        self.assertEqual(RuTorrentClient._torrent_dir(ligne("f", "", '  /dl/Mes \\"Films\\"/f.mkv ')),
                         '/dl/Mes "Films"')

    def test_colonnes_finales_incoherentes(self):
        """Fin de ligne qui ne ressemble pas aux colonnes demandées : repli sur d.base_path."""
        for extras in (("2", "/dl/x"), ("0", "relatif"), ("", "/dl/x"), ("oui", "/dl/x"), ("0", "")):
            with self.subTest(extras=extras):
                champs = ligne("film.mkv", "", "/dl/films/film.mkv", extras)
                self.assertEqual(RuTorrentClient._torrent_dir(champs), "/dl/films")

    def test_rien_d_exploitable(self):
        """Ligne vide, trop courte ou sans chemin : aucune proposition, jamais d'exception."""
        for champs in ([], ["x"], ["a", "b"], ["0"] * 20, ligne("f", "", ""), ligne("f", "", "sans-barre"),
                       ligne("f", "", None)):
            with self.subTest(champs=len(champs)):
                self.assertEqual(RuTorrentClient._torrent_dir(champs), "")

    def test_ligne_courte_avec_colonnes_finales(self):
        """Ligne trop courte pour contenir un nom : le dossier final est pris tel quel."""
        self.assertEqual(RuTorrentClient._torrent_dir(["1", "/dl/series/Show"]), "/dl/series/Show")
        self.assertEqual(RuTorrentClient._torrent_dir(["x", "0", "/dl/films"]), "/dl/films")


# --------------------------------------------------------------------------- #
#  Empreintes : known_hashes / has_infohash
# --------------------------------------------------------------------------- #

class TestEmpreintes(CasSeedbox):

    def test_appel_brut_a_un_seul_champ(self):
        """Un seul appel brut, qui ne demande que d.hash."""
        self.assertEqual(self.box.client().known_hashes(), {"AAAA", "BBBB", "CCCC"})
        self.assertEqual(self.box.calls, ["raw:d.multicall2"])
        self.assertEqual(xmlrpc.client.loads(self.box.requests[0]["body"]),
                         (("", "main", "d.hash="), "d.multicall2"))

    def test_comparaison_en_majuscules(self):
        """Les empreintes sont comparées sans tenir compte de la casse, des deux côtés."""
        self.box.torrents = [("abcdef0123", "", "/d", "a", 0), ("ABCD9999", "", "/d", "b", 0)]
        client = self.box.client()
        self.assertEqual(client.known_hashes(), {"ABCDEF0123", "ABCD9999"})
        self.assertTrue(client.has_infohash("abcdef0123"))
        self.assertTrue(client.has_infohash("ABCDEF0123"))
        self.assertTrue(client.has_infohash("AbCd9999"))
        self.assertFalse(client.has_infohash("ABCDEF"))
        self.assertFalse(client.has_infohash(""))

    def test_torrent_qui_vient_d_arriver(self):
        """Un torrent ajouté entre deux appels est vu au suivant (rien n'est mis en cache)."""
        client = self.box.client()
        self.assertFalse(client.has_infohash("dddd"))
        self.box.present.add("DDDD")
        self.assertTrue(client.has_infohash("dddd"))
        self.assertEqual(self.box.calls, ["raw:d.multicall2"] * 2)

    def test_seedbox_vide(self):
        """Aucun torrent : ensemble vide, has_infohash faux, sans erreur."""
        self.box.torrents = []
        self.assertEqual(self.box.client().known_hashes(), set())
        self.assertFalse(self.box.client().has_infohash("AAAA"))

    def test_lignes_anormales_ignorees(self):
        """Lignes vides ou non-listes dans la réponse brute : ignorées."""
        srv = self.scripte(defaut=reponse_brute([["abcd"], [], "x", ["EF01", "en trop"], [1234]]))
        self.assertEqual(self.client(srv).known_hashes(), {"ABCD", "EF01", "1234"})

    def test_cache_corrompu_sans_effet(self):
        """« poisoned » : la vérification après envoi fonctionne grâce au mode brut."""
        self.box.reset("poisoned")
        self.assertTrue(self.box.client().has_infohash("aaaa"))
        self.assertEqual(self.box.calls, ["raw:d.multicall2"])

    def test_injoignable_sans_retry(self):
        """« dead » : has_infohash LÈVE (passager) après un seul appel ; il ne répond pas « absent »."""
        self.box.reset("dead")
        with pauses_enregistrees() as pauses:
            exc = self.echec(self.box.client().has_infohash, "AAAA")
        self.assertTrue(exc.transient)
        self.assertEqual(self.box.calls, ["raw:d.multicall2"])
        self.assertEqual(pauses, [])
        self.assertEqual(RuTorrentClient._raw_unsupported, set())

    def test_injoignable_avec_retry(self):
        """retry=True : 1 + len(RETRY_DELAYS) essais bruts avant de lever, toujours sans repli."""
        self.box.reset("dead")
        exc = self.echec(self.box.client().has_infohash, "AAAA", retry=True)
        self.assertTrue(exc.transient)
        self.assertEqual(self.box.calls, ["raw:d.multicall2"] * essais())
        self.box.reset("dead")
        exc = self.echec(self.box.client().known_hashes, retry=True)
        self.assertTrue(exc.transient)
        self.assertEqual(self.box.calls, ["raw:d.multicall2"] * essais())

    def test_500_brut_passager(self):
        """500 en mode brut : levé sans retry ; surmonté avec retry=True."""
        self.box.raw_failures = 1
        exc = self.echec(self.box.client().known_hashes)
        self.assertEqual((exc.transient, exc.status), (True, 500))
        self.assertEqual(self.box.calls, ["raw:d.multicall2"])
        self.box.reset()
        self.box.raw_failures = 2
        self.assertEqual(self.box.client().known_hashes(retry=True), {"AAAA", "BBBB", "CCCC"})
        self.assertEqual(self.box.calls, ["raw:d.multicall2"] * 3)
        self.assertEqual(RuTorrentClient._raw_unsupported, set())

    def test_non_autorise(self):
        """« unauth » : erreur définitive 401, un seul appel, pas de repli."""
        self.box.reset("unauth")
        exc = self.echec(self.box.client().has_infohash, "AAAA", retry=True)
        self.assertEqual((exc.transient, exc.status), (False, 401))
        self.assertEqual(self.box.calls, ["raw:d.multicall2"])
        self.assertEqual(RuTorrentClient._raw_unsupported, set())

    def test_repli_sur_mode_list(self):
        """« noraw » et « fault » : repli sur mode=list (sans colonnes en plus), retenu ensuite."""
        for mode in ("noraw", "fault"):
            with self.subTest(mode=mode):
                self.box.reset(mode)
                self.box.torrents = [("abcd", "", "/d", "a", 0)]
                self.assertEqual(self.box.client().known_hashes(), {"ABCD"})
                self.assertEqual(self.box.calls, ["raw:d.multicall2", "list"])
                self.assertEqual(self.box.requests[-1]["body"], b"mode=list")
                self.assertEqual(RuTorrentClient._raw_unsupported, {self.box.url})
                self.assertTrue(self.box.client().has_infohash("AbCd"))
                self.assertEqual(self.box.calls, ["raw:d.multicall2", "list", "list"])

    def test_voie_de_repli_et_retry(self):
        """En mode=list, retry=False fait un seul essai ; retry=True surmonte un incident passager."""
        RuTorrentClient._raw_unsupported.add(self.box.url)
        self.box.list_plan = ["500"]
        exc = self.echec(self.box.client().has_infohash, "AAAA")
        self.assertEqual((exc.transient, exc.status), (True, 500))
        self.assertEqual(self.box.calls, ["list"])
        self.box.list_plan = ["500", "garbage"]
        self.assertTrue(self.box.client().has_infohash("aaaa", retry=True))
        self.assertEqual(self.box.calls, ["list"] * 4)
        self.box.list_plan = ["401"]
        exc = self.echec(self.box.client().has_infohash, "AAAA", retry=True)
        self.assertEqual(exc.status, 401)


# --------------------------------------------------------------------------- #
#  État de rTorrent, réparation du cache, test de connexion
# --------------------------------------------------------------------------- #

class TestEtatEtReparation(CasSeedbox):

    def test_rtorrent_alive_selon_le_mode(self):
        """Vrai si rTorrent répond en mode brut (même cache corrompu) ; faux sinon, sans exception."""
        attendus = {"ok": True, "poisoned": True, "dead": False, "noraw": False,
                    "fault": False, "unauth": False}
        for mode, attendu in attendus.items():
            with self.subTest(mode=mode):
                self.box.reset(mode)
                with pauses_enregistrees() as pauses:
                    self.assertIs(self.box.client().rtorrent_alive(), attendu)
                self.assertEqual(self.box.calls, ["raw:system.client_version"])   # un seul appel
                self.assertEqual(pauses, [])
                self.assertEqual(RuTorrentClient._raw_unsupported, set())

    def test_rtorrent_alive_sans_serveur(self):
        """Connexion coupée ou URL absente : faux, jamais d'exception."""
        self.assertIs(self.client(self.brut(ferme_sans_repondre)).rtorrent_alive(), False)
        self.assertIs(RuTorrentClient("").rtorrent_alive(), False)

    def test_rtorrent_alive_requete(self):
        """La sonde est un appel brut system.client_version sans paramètre."""
        self.box.client().rtorrent_alive()
        requete = self.box.requests[-1]
        self.assertEqual(xmlrpc.client.loads(requete["body"]), ((), "system.client_version"))
        self.assertEqual(requete["headers"]["content-type"], "text/xml")

    def test_reparation_est_un_get_de_getplugins(self):
        """La réparation reproduit le chargement de page : GET php/getplugins.php, authentifié."""
        self.assertIs(self.box.client().repair_settings_cache(), True)
        self.assertEqual(self.box.calls, ["GET getplugins.php"])
        requete = self.box.requests[-1]
        self.assertEqual((requete["method"], requete["path"]), ("GET", "/rutorrent/php/getplugins.php"))
        self.assertEqual(requete["body"], b"")
        self.assertEqual(requete["headers"]["authorization"], basic("u", "p"))
        self.assertIn("cache ruTorrent reconstruit (php/getplugins.php)", self.journal())

    def test_reparation_du_cache_corrompu(self):
        """« poisoned » : mode=list échoue, la réparation réussit, mode=list refonctionne."""
        self.box.reset("poisoned")
        client = self.box.client()
        self.echec(client.list_torrents, retry=False)
        self.assertTrue(client.rtorrent_alive())
        self.assertIs(client.repair_settings_cache(), True)
        self.assertEqual(sorted(client.list_torrents(retry=False)), ["AAAA", "BBBB", "CCCC"])
        self.assertEqual(self.box.calls,
                         ["list", "raw:system.client_version", "GET getplugins.php", "list"])

    def test_reparation_refusee(self):
        """Échec de la réparation : False (jamais d'exception), motif au journal, un seul appel."""
        self.box.reset("unauth")
        self.assertIs(self.box.client().repair_settings_cache(), False)
        self.assertEqual(self.box.calls, ["GET getplugins.php"])
        journal = self.journal()
        self.assertIn("reconstruction du cache ruTorrent impossible : Authentification refusée (HTTP 401)",
                      journal)
        self.assertNotIn("cache ruTorrent reconstruit", journal)

    def test_reparation_impossible(self):
        """500, 404, coupure ou URL absente : False, sans nouvel essai."""
        for code in (500, 404):
            with self.subTest(code=code):
                srv = self.scripte(defaut=(code, {}, b"non"))
                self.assertIs(self.client(srv).repair_settings_cache(), False)
                self.assertEqual(len(srv.requetes), 1)
        srv = self.brut(ferme_sans_repondre)
        self.assertIs(self.client(srv).repair_settings_cache(), False)
        self.assertEqual(len(srv.requetes), 1)
        self.assertIs(RuTorrentClient("").repair_settings_cache(), False)

    def test_message_du_test_de_connexion(self):
        """Le test de connexion résume torrents, étiquettes et dossiers détectés."""
        self.assertEqual(self.box.client().test_connection(),
                         "Connexion réussie : 3 torrent(s), 2 étiquette(s), 3 dossier(s) détecté(s).")
        self.assertEqual(self.box.calls, APPEL_BRUT)

    def test_message_du_test_de_connexion_seedbox_vide(self):
        """Seedbox vide : la connexion est tout de même annoncée réussie, avec des zéros."""
        self.box.torrents = []
        self.assertEqual(self.box.client().test_connection(),
                         "Connexion réussie : 0 torrent(s), 0 étiquette(s), 0 dossier(s) détecté(s).")

    def test_test_de_connexion_cache_corrompu(self):
        """« poisoned » : le test de connexion réussit (mode brut)."""
        self.box.reset("poisoned")
        self.assertTrue(self.box.client().test_connection().startswith("Connexion réussie : 3 torrent(s)"))

    def test_test_de_connexion_par_la_voie_de_repli(self):
        """« noraw » : même message, obtenu par mode=list."""
        self.box.reset("noraw")
        self.assertEqual(self.box.client().test_connection(),
                         "Connexion réussie : 3 torrent(s), 2 étiquette(s), 3 dossier(s) détecté(s).")
        self.assertEqual(self.box.calls, ["raw:d.multicall2", "list"])

    def test_test_de_connexion_en_echec(self):
        """Le test de connexion laisse remonter l'erreur, avec son classement."""
        self.box.reset("unauth")
        exc = self.echec(self.box.client().test_connection)
        self.assertEqual((exc.transient, exc.status), (False, 401))
        self.box.reset("dead")
        exc = self.echec(self.box.client().test_connection)
        self.assertTrue(exc.transient)
        exc = self.echec(RuTorrentClient("").test_connection)
        self.assertEqual(str(exc), "Aucune URL de seedbox configurée.")


# --------------------------------------------------------------------------- #
#  Défauts du produit mis en évidence par ces tests
# --------------------------------------------------------------------------- #

class TestDefautsConnus(CasClient):
    """Défauts de rutorrent.py relevés à l'écriture de ces tests, désormais corrigés.

    Chaque test décrit un défaut corrigé : il protège contre son retour.
    """

    def test_url_malformee_classee_definitive(self):
        """Corrigé — une URL refusée par http.client (espace, port non numérique) est classée
        « Connexion interrompue par la seedbox (InvalidURL) », passagère, donc réessayée."""
        srv = self.scripte(defaut=reponse_liste({}))
        for url in ("http://127.0.0.1:%d/ru torrent" % srv.port, "http://127.0.0.1:abc/rutorrent"):
            with self.subTest(url=url):
                try:
                    RuTorrentClient(url, "u", "p", timeout=5).list_torrents(retry=False)
                except RuTorrentError as exc:
                    # « mauvaise URL » = refus définitif d'après la docstring de RuTorrentError
                    self.assertFalse(exc.transient, str(exc))
                    self.assertNotIn("interrompue", str(exc))

    def test_url_accentuee_sans_exception_brute(self):
        """Corrigé — un caractère accentué dans le chemin de l'URL fait fuir un UnicodeEncodeError
        hors de _post, au lieu d'une RuTorrentError (ou d'une URL correctement encodée)."""
        srv = self.scripte(defaut=reponse_liste({}))
        client = RuTorrentClient("http://127.0.0.1:%d/rutörrent" % srv.port, "u", "p", timeout=5)
        try:
            client.list_torrents(retry=False)
        except RuTorrentError:
            pass

    def test_erreur_ssl_ne_condamne_pas_le_mode_brut(self):
        """Corrigé — une erreur SSL (certificat auto-signé refusé) inscrit la seedbox dans
        _raw_unsupported ; une fois la vérification décochée, tout passe par mode=list
        jusqu'à la fermeture de l'application."""
        srv = self.scripte(defaut=seedbox_vide, tls=True)
        exc = self.echec(self.client(srv).fetch_labels_and_dirs)
        self.assertTrue(str(exc).startswith("Erreur SSL"), str(exc))
        self.assertEqual(self.client(srv, verify_ssl=False).fetch_labels_and_dirs(),
                         ([], ["/home/u/dl"], 0))
        self.assertEqual(srv.genres(), APPEL_BRUT)                    # aujourd'hui : ["list"]
        self.assertEqual(RuTorrentClient._raw_unsupported, set())

    def test_empreintes_reponse_brute_inattendue(self):
        """Corrigé — si d.multicall2 renvoie autre chose qu'une liste, known_hashes laisse fuir un
        TypeError (fetch_labels_and_dirs, lui, lève RuTorrentError puis se replie sur mode=list)."""
        srv = self.scripte(script=[reponse_brute(0)], defaut=reponse_liste({"abcd": []}))
        try:
            self.assertEqual(self.client(srv).known_hashes(), {"ABCD"})
        except RuTorrentError:
            pass


if __name__ == "__main__":
    unittest.main()
