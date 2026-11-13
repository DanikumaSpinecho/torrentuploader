# -*- coding: utf-8 -*-
"""Client ruTorrent : ajout de torrents, lecture des étiquettes et des dossiers."""

from __future__ import annotations

import base64
import html
import http.client
import json
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xmlrpc.client
from http.cookiejar import CookieJar
from pathlib import Path

from . import __version__
from .paths import log_error


class RuTorrentError(Exception):
    """Erreur de dialogue avec ruTorrent.

    `transient` distingue un incident passager (erreur 5xx, délai dépassé,
    connexion coupée : un nouvel essai a de bonnes chances de reussir) d'un
    refus définitif (mauvais mot de passe, mauvaise URL, certificat).
    """

    def __init__(self, message: str, transient: bool = False, status: int | None = None,
                 raw_unsupported: bool = False):
        super().__init__(message)
        self.transient = transient
        self.status = status
        # Vrai uniquement si c'est le MODE BRUT qui est refusé (réponse non XML,
        # Fault...). Une erreur d'accès (SSL, adresse, mot de passe) échouerait
        # tout autant par mode=list : elle ne doit pas condamner le mode brut.
        self.raw_unsupported = raw_unsupported


# Index des champs renvoyes par plugins/httprpc/action.php (mode=list)
FIELD_NAME = 4
FIELD_SIZE = 5
FIELD_LABEL = 14
FIELD_BASE_PATH = 25

# rTorrent traite les requêtes une par une. Plusieurs appels simultanés (rafraichir
# pendant qu'un envoi vérifie son résultat...) ne font qu'allonger la file et
# augmenter le risque d'erreur 500 : un seul appel a la fois vers la seedbox.
# Verrou au niveau du module, car chaque action cree son propre RuTorrentClient.
_SEEDBOX_LOCK = threading.Lock()


class _PreemptiveBasic(urllib.request.BaseHandler):
    """Envoie Basic d'emblée sur chaque requête vers la seedbox, redirections comprises.

    En en-tête « non redirigé » : si le serveur exige Digest, la relance d'urllib peut
    le remplacer (un en-tête ordinaire l'emporterait, et la relance bouclerait sans fin).
    """

    handler_order = 400                   # avant l'envoi effectif

    def __init__(self, client):
        self.client = client
        self.host = urllib.parse.urlsplit(client.base_url).netloc.lower()

    def http_request(self, req):
        if urllib.parse.urlsplit(req.full_url).netloc.lower() == self.host                 and not req.has_header("Authorization"):
            req.add_unredirected_header("Authorization", self.client._basic_auth())
        return req

    https_request = http_request


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
        # On accepte que l'utilisateur colle une URL complète d'endpoint.
        for suffix in ("/php/addtorrent.php", "/plugins/httprpc/action.php", "/index.html"):
            if url.lower().endswith(suffix):
                url = url[: -len(suffix)]
        url = url.rstrip("/")
        if url.isascii():
            return url                    # cas courant : rien à réécrire
        # Caractères non ASCII (accents dans le chemin, nom de domaine internationalisé) :
        # encodés ici, sinon la bibliothèque HTTP lèverait une erreur brute.
        try:
            parts = urllib.parse.urlsplit(url)
            host = parts.hostname or ""
            try:
                host.encode("ascii")
            except UnicodeEncodeError:
                ascii_host = host.encode("idna").decode("ascii")
                netloc = parts.netloc.replace(host, ascii_host)
                parts = parts._replace(netloc=netloc)
            path = urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=~-._")
            url = urllib.parse.urlunsplit(parts._replace(path=path))
        except (ValueError, UnicodeError):
            pass                          # laissé tel quel : _post le signalera proprement
        return url

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
            handlers.append(_PreemptiveBasic(self))
        return urllib.request.build_opener(*handlers)

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {
            "User-Agent": "TorrentUploader/%s" % __version__,
            "Accept": "*/*",
        }
        if extra:
            headers.update(extra)
        return headers

    def _basic_auth(self) -> str:
        token = base64.b64encode(
            ("%s:%s" % (self.username, self.password)).encode("utf-8")
        ).decode("ascii")
        return "Basic " + token

    def _post(self, endpoint: str, body, content_type: str = "",
              extra: dict | None = None) -> str:
        """Requête vers la seedbox (POST si `body`, GET si `body` vaut None)."""
        if not self.base_url:
            raise RuTorrentError("Aucune URL de seedbox configurée.")
        url = self.base_url + endpoint
        headers = dict(extra or {})
        if content_type:
            headers["Content-Type"] = content_type
        try:
            req = urllib.request.Request(
                url, data=body, headers=self._headers(headers),
                method="POST" if body is not None else "GET"
            )
        except ValueError as exc:
            raise RuTorrentError("Adresse de la seedbox invalide : %s" % exc) from exc
        try:
            with _SEEDBOX_LOCK, self._opener.open(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            page = ""
            try:
                page = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            finally:
                exc.close()
            if exc.code in (401, 403):
                raise RuTorrentError(
                    "Authentification refusée (HTTP %d). Vérifiez l'identifiant et le mot de passe."
                    % exc.code, status=exc.code
                ) from exc
            if exc.code == 404:
                raise RuTorrentError(
                    "Page introuvable (HTTP 404) : %s - l'URL de ruTorrent est probablement incorrecte."
                    % endpoint, status=exc.code
                ) from exc
            if exc.code >= 500:
                # La page d'erreur HTML de l'hebergeur n'apprend rien a l'utilisateur :
                # elle part dans le journal, le message reste lisible.
                log_error("HTTP %d sur %s : %s" % (exc.code, endpoint, self._clean_html(page)[:300]))
                raise RuTorrentError(
                    "erreur interne de la seedbox (HTTP %d) - rTorrent est probablement "
                    "occupé ou en train de redémarrer" % exc.code,
                    transient=True, status=exc.code
                ) from exc
            raise RuTorrentError("Erreur HTTP %d (%s). %s"
                                 % (exc.code, exc.reason, self._clean_html(page)[:120]),
                                 status=exc.code) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ssl.SSLError):
                raise RuTorrentError(
                    "Erreur SSL : %s - décochez « Vérifier le certificat SSL » si votre seedbox "
                    "utilise un certificat auto-signé." % reason
                ) from exc
            raise RuTorrentError("Connexion impossible : %s" % reason, transient=True) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise RuTorrentError("Délai d'attente dépassé (%d s)." % self.timeout,
                                 transient=True) from exc
        except (http.client.InvalidURL, UnicodeError, ValueError) as exc:
            raise RuTorrentError("Adresse de la seedbox invalide (%s). Vérifiez-la dans les "
                                 "paramètres." % exc) from exc
        except (ConnectionError, http.client.HTTPException) as exc:
            # coupure en cours de réponse (RemoteDisconnected, IncompleteRead...)
            raise RuTorrentError("Connexion interrompue par la seedbox (%s)."
                                 % type(exc).__name__, transient=True) from exc

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

    # -- XML-RPC brut ------------------------------------------------------ #
    #
    # Le plugin httprpc relaie tel quel vers rTorrent tout corps XML-RPC qui ne
    # contient pas de clé `mode=` (mode « raw », présent de la v3.10 a la v5).
    # Deux avantages décisifs sur `mode=list` :
    #   - il ne passe PAS par la table de correspondance des noms de commandes que
    #     ruTorrent garde en cache. Jusqu'a la v5.3.12, si une page ruTorrent est
    #     chargee pendant que rTorrent ne répond pas, ce cache est enregistre vide :
    #     `mode=list` envoie alors d'anciens noms refuses par rTorrent et échoue en
    #     500 a chaque appel, jusqu'au prochain chargement de page dans un
    #     navigateur (ruTorrent PR #3191) ;
    #   - on ne demande que les champs utiles, et ruTorrent n'écrit aucun fichier
    #     de cache a chaque appel.

    # Les clients sont recrees a chaque action : on retient ici, par seedbox, que
    # le mode brut est indisponible, pour ne pas le retenter a chaque fois.
    _raw_unsupported = set()

    def _xmlrpc(self, method: str, params: tuple = ()):
        body = xmlrpc.client.dumps(params, method).encode("utf-8")
        # Content-Type sans « charset » : rTorrent le refuse (ruTorrent #2879).
        raw = self._post("/plugins/httprpc/action.php", body, "text/xml",
                         {"Accept-Encoding": "identity"})
        text = raw.strip()
        if text in ("false", '""', "null", ""):
            # v3.10 a v5.2 : rTorrent injoignable -> HTTP 200 avec ce corps.
            raise RuTorrentError("rTorrent ne répond pas", transient=True)
        if not (text.startswith("<?xml") or text.startswith("<methodResponse")):
            if text.startswith("<"):        # page HTML de maintenance servie en 200
                log_error("XML-RPC %s : page HTML reçue : %s"
                          % (method, self._clean_html(text)[:200]))
                raise RuTorrentError("réponse illisible de la seedbox", transient=True)
            raise RuTorrentError("mode XML-RPC brut indisponible sur cette seedbox",
                                 raw_unsupported=True)
        try:
            return xmlrpc.client.loads(text)[0][0]
        except xmlrpc.client.Fault as fault:
            raise RuTorrentError("rTorrent a refusé %s : %s"
                                 % (method, fault.faultString), raw_unsupported=True) from fault
        except Exception as exc:
            raise RuTorrentError("réponse XML-RPC illisible (%s)"
                                 % type(exc).__name__, raw_unsupported=True) from exc

    @staticmethod
    def _dir_from(directory: str, name: str, multi) -> str:
        """Dossier a proposer, a partir de d.directory.

        Multi-fichiers : d.directory vaut <dossier choisi>/<nom du torrent>,
        on remonte d'un cran. Fichier unique : c'est déjà le dossier choisi.
        """
        directory = (directory or "").rstrip("/")
        if not directory.startswith("/"):
            return ""
        if str(multi) == "1" and name and directory.endswith("/" + name):
            directory = directory[: -len(name) - 1]
        return directory

    def _labels_dirs_raw(self):
        rows = self._xmlrpc("d.multicall2", ("", "main", "d.custom1=", "d.directory=",
                                             "d.name=", "d.is_multi_file="))
        if not isinstance(rows, list):
            raise RuTorrentError("réponse XML-RPC inattendue", raw_unsupported=True)
        labels, dirs = set(), set()
        for r in rows:
            if not isinstance(r, list) or len(r) < 4:
                continue
            lbl = urllib.parse.unquote(str(r[0] or "")).strip()
            if lbl:
                labels.add(lbl)
            directory = self._dir_from(str(r[1] or ""), str(r[2] or ""), r[3])
            if directory:
                dirs.add(directory)
        return labels, dirs, len(rows)

    def default_directory(self, known_dirs=()) -> str:
        """Dossier de téléchargement par défaut de rTorrent, en chemin absolu.

        Garantit une proposition même sur une seedbox sans aucun torrent.
        rTorrent le renvoie souvent sous la forme « ~/... » : on le résout a
        l'aide d'un dossier déjà connu, sinon on s'abstient.
        """
        try:
            value = str(self._xmlrpc("directory.default") or "").strip().rstrip("/")
        except RuTorrentError:
            return ""
        if value.startswith("/"):
            return value
        if value.startswith("~/"):
            tail = value[1:]                               # "/downloads/rtorrent"
            for d in known_dirs:
                i = (d + "/").find(tail + "/")
                if i > 0:
                    return d[:i] + tail
        return ""

    def rtorrent_alive(self) -> bool:
        """rTorrent repond-il, sans passer par le cache de ruTorrent ?"""
        try:
            self._xmlrpc("system.client_version")
            return True
        except RuTorrentError:
            return False

    def repair_settings_cache(self) -> bool:
        """Reconstruit le cache de ruTorrent, comme un chargement de page.

        Chaque ouverture de ruTorrent dans un navigateur appelle getplugins.php,
        qui interroge de nouveau rTorrent et reecrit le cache des noms de
        commandes. C'est pourquoi « tout remarche après un passage par le
        navigateur ». On reproduit exactement cet appel ; le contenu renvoye
        (du JavaScript) est ignore.
        """
        try:
            self._post("/php/getplugins.php", None)
            log_error("cache ruTorrent reconstruit (php/getplugins.php)")
            return True
        except RuTorrentError as exc:
            log_error("reconstruction du cache ruTorrent impossible : %s" % exc)
            return False

    # Pauses entre deux essais. L'interface web de ruTorrent interroge le serveur
    # toutes les quelques secondes et ignore en silence un échec isole : c'est pour
    # cela qu'un incident passager n'y est jamais visible. On fait de même.
    RETRY_DELAYS = (1.0, 2.5, 5.0)

    def _list_once(self, extra_cmds=()) -> dict:
        # Les colonnes demandées par `cmd=` sont ajoutees EN FIN de ligne, dans
        # l'ordre : c'est le mecanisme qu'emploient les plugins de l'interface web.
        fields = [("mode", "list")] + [("cmd", c) for c in extra_cmds]
        body = urllib.parse.urlencode(fields).encode("utf-8")
        raw = self._post("/plugins/httprpc/action.php", body,
                         "application/x-www-form-urlencoded; charset=UTF-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            # Une page HTML servie avec un statut 200 (proxy, maintenance) : passager.
            log_error("liste illisible : %s" % self._clean_html(raw)[:300])
            raise RuTorrentError(
                "réponse illisible de la seedbox (maintenance en cours, ou plugin "
                "httprpc désactivé ?)", transient=True) from exc
        if not isinstance(data, dict):
            raise RuTorrentError("réponse inattendue de la seedbox", transient=True)
        torrents = data.get("t") or {}
        return torrents if isinstance(torrents, dict) else {}

    def list_torrents(self, retry: bool = True, extra_cmds=()) -> dict:
        """Liste des torrents. Réessaie seul sur un incident passager."""
        delays = (0.0,) + (self.RETRY_DELAYS if retry else ())
        last = None
        for delay in delays:
            if delay:
                time.sleep(delay)
            try:
                return self._list_once(extra_cmds)
            except RuTorrentError as exc:
                if not exc.transient:
                    raise                     # mot de passe, URL... inutile d'insister
                last = exc
        raise last

    # Colonnes demandées en plus de la liste standard, lues PAR LA FIN de ligne :
    # ainsi la lecture ne depend pas du nombre de colonnes de base, qui varie selon
    # la version de ruTorrent et les plugins installes.
    LIST_EXTRA_CMDS = ("d.is_multi_file=", "d.directory=")

    @staticmethod
    def _unescape(value) -> str:
        """Retire l'échappement que ruTorrent ajoute aux valeurs de `mode=list`.

        rXMLRPCRequest::run() double \\ et \" AVANT l'encodage JSON : json.loads
        ne retire qu'une couche. (Le mode brut n'a pas ce défaut.)
        """
        return re.sub(r"\\(.)", r"\1", str(value or ""))

    @classmethod
    def _torrent_dir(cls, fields: list) -> str:
        """Dossier de destination d'un torrent issu de `mode=list`.

        d.base_path (colonne 25) est VIDE pour un torrent arrêté : un dossier ne
        contenant que des torrents arrêtés disparaissait des propositions.
        d.directory, demande en fin de ligne, est toujours renseigné.
        """
        if len(fields) >= 2:
            directory = cls._unescape(fields[-1])
            multi = str(fields[-2])
            if directory.startswith("/") and multi in ("0", "1"):
                name = cls._unescape(fields[FIELD_NAME]) if len(fields) > FIELD_NAME else ""
                return cls._dir_from(directory, name, multi)
        # serveur qui ignorerait les colonnes demandées : ancienne méthode
        if len(fields) > FIELD_BASE_PATH:
            path = cls._unescape(fields[FIELD_BASE_PATH]).strip().rstrip("/")
            if "/" in path:
                return path.rsplit("/", 1)[0]
        return ""

    def _labels_dirs_list(self):
        torrents = self.list_torrents(extra_cmds=self.LIST_EXTRA_CMDS)
        labels, dirs = set(), set()
        for fields in torrents.values():
            if not isinstance(fields, list):
                continue
            if len(fields) > FIELD_LABEL:
                lbl = urllib.parse.unquote(self._unescape(fields[FIELD_LABEL])).strip()
                if lbl:
                    labels.add(lbl)
            directory = self._torrent_dir(fields)
            if directory:
                dirs.add(directory)
        return labels, dirs, len(torrents)

    def _raw_with_retry(self, fn):
        """Execute une lecture en mode brut, avec nouveaux essais si passager.

        Leve RuTorrentError transitoire si rTorrent reste injoignable, ou non
        transitoire si le mode brut n'est pas disponible (l'appelant bascule
        alors sur `mode=list`).
        """
        last = None
        for delay in (0.0,) + self.RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                return fn()
            except RuTorrentError as exc:
                if not exc.transient:
                    raise
                last = exc
        raise last

    def fetch_labels_and_dirs(self):
        """Étiquettes et dossiers en usage sur la seedbox.

        Voie principale : XML-RPC brut (léger, insensible au cache corrompu de
        ruTorrent). Repli : `mode=list`, si le mode brut est indisponible.
        """
        if self.base_url not in self._raw_unsupported:
            try:
                labels, dirs, count = self._raw_with_retry(self._labels_dirs_raw)
            except RuTorrentError as exc:
                if not exc.raw_unsupported:
                    raise                    # rTorrent injoignable, accès refusé, SSL...
                log_error("mode brut indisponible, repli sur mode=list : %s" % exc)
                self._raw_unsupported.add(self.base_url)
            else:
                default = self.default_directory(dirs)
                if default:
                    dirs.add(default)
                return (sorted(labels, key=str.lower), sorted(dirs, key=str.lower), count)
        labels, dirs, count = self._labels_dirs_list()
        return (sorted(labels, key=str.lower), sorted(dirs, key=str.lower), count)

    def known_hashes(self, retry: bool = False) -> set:
        """Empreintes de tous les torrents présents (un seul champ par torrent)."""
        if self.base_url not in self._raw_unsupported:
            try:
                fn = lambda: self._xmlrpc("d.multicall2", ("", "main", "d.hash="))
                rows = self._raw_with_retry(fn) if retry else fn()
                if not isinstance(rows, list):
                    raise RuTorrentError("réponse XML-RPC inattendue", raw_unsupported=True)
                return {str(r[0]).upper() for r in rows if isinstance(r, list) and r}
            except RuTorrentError as exc:
                if not exc.raw_unsupported:
                    raise
                self._raw_unsupported.add(self.base_url)
        return {k.upper() for k in self.list_torrents(retry=retry)}

    def has_infohash(self, infohash: str, retry: bool = False) -> bool:
        """Vrai si la seedbox connaît ce torrent. Leve RuTorrentError si injoignable."""
        return infohash.upper() in self.known_hashes(retry=retry)

    def test_connection(self) -> str:
        labels, dirs, count = self.fetch_labels_and_dirs()
        return ("Connexion réussie : %d torrent(s), %d étiquette(s), %d dossier(s) détecté(s)."
                % (count, len(labels), len(dirs)))
