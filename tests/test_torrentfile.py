# -*- coding: utf-8 -*-
"""Tests de la lecture des .torrent et des liens magnet, des emplacements de
fichiers, de la configuration et de l'analyse de la ligne de commande.

Sous-systèmes couverts :
  * tuploader/torrentfile.py  (parse_torrent, parse_magnet, MAGNET_RE,
                               torrent_is_ready, human_size)
  * tuploader/paths.py        (dossiers, journal, purge, migration v1)
  * tuploader/config.py       (load_config, save_config)
  * tuploader/main.py         (parse_argv et Sink uniquement)

Aucun réseau, aucune fenêtre, aucun registre : ces tests n'écrivent que des
fichiers dans le bac à sable créé par tests/helpers.py.

Les défauts du produit révélés à l'écriture de ces tests ont été corrigés ; les tests
qui les mettaient en évidence vérifient désormais le comportement corrigé.
"""

from tests import helpers as h      # EN PREMIER : redirige les données vers le bac à sable

import base64                                            # noqa: E402
import hashlib                                           # noqa: E402
import json                                              # noqa: E402
import os                                                # noqa: E402
import random                                            # noqa: E402
import re                                                # noqa: E402
import subprocess                                        # noqa: E402
import sys                                               # noqa: E402
import time                                              # noqa: E402
import unittest                                          # noqa: E402
from pathlib import Path                                 # noqa: E402
from unittest import mock                                # noqa: E402

from tuploader import config, paths                      # noqa: E402
from tuploader import main as tmain                      # noqa: E402
from tuploader import torrentfile as tf                  # noqa: E402
from tuploader.torrentfile import BencodeError, MagnetError   # noqa: E402


# --------------------------------------------------------------------------- #
#  Données et outils communs
# --------------------------------------------------------------------------- #

HEX40 = "0123456789abcdef0123456789abcdef01234567"
AUTRE40 = "fedcba9876543210fedcba9876543210fedcba98"
BASE32 = base64.b32encode(bytes.fromhex(HEX40)).decode("ascii")     # 32 caractères
MAGNET = "magnet:?xt=urn:btih:" + HEX40
AUTRE_MAGNET = "magnet:?xt=urn:btih:" + AUTRE40

# Début commun des torrents fabriqués par helpers.torrent_bytes()
PREFIXE = b"d8:announce23:http://tracker.test/ann4:info"

VALIDE = h.torrent_bytes("Valide")

INFO_MINIMALE = {b"name": b"x", b"length": 5, b"piece length": 16384, b"pieces": b"\0" * 20}

# Un torrent plus riche : liste de trackers, dates, chemins imbriqués, drapeau privé.
DENSE = h.bencode({
    b"announce": b"http://tracker.test/ann",
    b"announce-list": [[b"http://a.test/ann"], [b"udp://b.test:80"]],
    b"comment": b"info",
    b"creation date": 1700000000,
    b"info": {
        b"files": [{b"length": 10, b"path": [b"a", b"b.bin"]},
                   {b"length": 20, b"path": [b"c.bin"]}],
        b"name": b"Dossier",
        b"name.utf-8": "Dossier é".encode("utf-8"),
        b"piece length": 16384,
        b"pieces": b"\x01" * 40,
        b"private": 1,
    },
})

CHEMIN_MEMOIRE = Path("en-memoire-inexistant.torrent")


def sha1_majuscule(octets: bytes) -> str:
    return hashlib.sha1(octets).hexdigest().upper()


def analyser_octets(donnees: bytes) -> dict:
    """parse_torrent sur des octets en mémoire (essais en masse, sans disque).

    Le chemin donné n'existe pas : si parse_torrent cessait un jour de lire par
    Path.read_bytes(), ces tests échoueraient bruyamment (fichier introuvable)
    au lieu de tester autre chose que prévu.
    """
    with mock.patch.object(Path, "read_bytes", return_value=donnees):
        return tf.parse_torrent(CHEMIN_MEMOIRE)


class CasIsole(unittest.TestCase):
    """Donne à chaque test son propre dossier de données, toujours DANS le bac à sable.

    La variable d'environnement n'est jamais retirée : elle est seulement pointée
    vers un sous-dossier du bac à sable, puis remise. Aucun test ne peut donc
    retomber sur le vrai profil de l'utilisateur, et les tests ne se gênent pas
    entre eux (ni avec les autres modules de la suite).
    """

    def setUp(self):
        super().setUp()
        self.donnees = h.temp_dir("donnees-")
        self.travail = h.temp_dir("travail-")
        precedent = os.environ[paths.ENV_DATA_DIR]
        os.environ[paths.ENV_DATA_DIR] = str(self.donnees)
        self.addCleanup(os.environ.__setitem__, paths.ENV_DATA_DIR, precedent)
        self.assertTrue(paths.is_sandboxed())
        self.assertTrue(paths.data_dir().is_relative_to(h.SANDBOX))

    def ecrire(self, nom: str, donnees: bytes, age: float = 60.0) -> Path:
        """Écrit un fichier de travail dont la date de modification remonte à `age` secondes."""
        chemin = self.travail / nom
        chemin.write_bytes(donnees)
        instant = time.time() - age
        os.utime(chemin, (instant, instant))
        return chemin

    def journal(self) -> str:
        chemin = paths.log_path()
        return chemin.read_text(encoding="utf-8") if chemin.exists() else ""


# --------------------------------------------------------------------------- #
#  parse_torrent : fichiers valides
# --------------------------------------------------------------------------- #

class AnalyseTorrentValide(CasIsole):

    def test_fichier_simple(self):
        """Un .torrent mono-fichier : nom, taille, un seul fichier, tracker, chemin."""
        chemin = h.make_torrent(self.travail, "Exemple", size=3_145_728)
        infos = tf.parse_torrent(chemin)
        self.assertEqual(infos["name"], "Exemple")
        self.assertEqual(infos["size"], 3_145_728)
        self.assertEqual(infos["files"], 1)
        self.assertEqual(infos["tracker"], "http://tracker.test/ann")
        self.assertIs(infos["private"], False)
        self.assertEqual(infos["path"], chemin)
        self.assertRegex(infos["infohash"], r"^[0-9A-F]{40}$")
        self.assertLessEqual({"path", "name", "size", "files", "infohash", "private", "tracker"},
                             set(infos))

    def test_multi_fichiers(self):
        """Un .torrent multi-fichiers : la taille est la somme, le nombre est compté."""
        chemin = h.make_torrent(self.travail, "Saison 1", size=3_000_000, files=3)
        infos = tf.parse_torrent(chemin)
        self.assertEqual(infos["name"], "Saison 1")
        self.assertEqual(infos["files"], 3)
        self.assertEqual(infos["size"], 3_000_000)

    def test_multi_fichiers_tailles_inegales(self):
        """Les longueurs de chaque fichier sont additionnées une à une."""
        info = {b"name": b"Lot", b"piece length": 16384, b"pieces": b"\0" * 20,
                b"files": [{b"length": 1, b"path": [b"a"]},
                           {b"length": 20, b"path": [b"sous", b"b"]},
                           {b"length": 300, b"path": [b"c"]}]}
        infos = tf.parse_torrent(self.ecrire("lot.torrent", h.bencode({b"info": info})))
        self.assertEqual((infos["size"], infos["files"]), (321, 3))

    def test_torrent_riche(self):
        """Liste de trackers, date, chemins imbriqués : tout est lu sans confusion."""
        infos = tf.parse_torrent(self.ecrire("dense.torrent", DENSE))
        self.assertEqual(infos["name"], "Dossier é")
        self.assertEqual((infos["size"], infos["files"]), (30, 2))
        self.assertIs(infos["private"], True)
        self.assertEqual(infos["tracker"], "http://tracker.test/ann")

    def test_chemin_donne_en_texte(self):
        """Un chemin fourni en str est accepté ; le résultat porte toujours un Path."""
        chemin = h.make_torrent(self.travail, "Texte")
        infos = tf.parse_torrent(str(chemin))
        self.assertIsInstance(infos["path"], Path)
        self.assertEqual(infos["path"], chemin)
        self.assertEqual(infos["name"], "Texte")

    def test_infohash_sha1_des_octets_exacts_de_info(self):
        """L'infohash est le SHA-1 (40 hex majuscules) des octets exacts du dictionnaire info."""
        brut = h.torrent_bytes("Exemple")
        octets_info = brut[len(PREFIXE):-1]
        self.assertTrue(octets_info.startswith(b"d") and octets_info.endswith(b"e"))
        infos = tf.parse_torrent(self.ecrire("exemple.torrent", brut))
        self.assertEqual(infos["infohash"], sha1_majuscule(octets_info))
        # valeur de référence figée (calculée hors du produit)
        self.assertEqual(infos["infohash"], "A9A4A79E840CB864CC5AADD42CFE523020B74300")

    def test_infohash_borne_a_la_section_info(self):
        """Les clés placées avant ou après « info » ne changent pas l'empreinte."""
        attendu = sha1_majuscule(h.bencode(INFO_MINIMALE))
        entourages = (
            {},
            {b"announce": b"http://t.test/a"},
            {b"announce": b"http://t.test/a", b"comment": b"info", b"creation date": 1,
             b"url-list": [b"http://w.test/"]},
            {b"zzz": {b"info": {b"name": b"leurre", b"length": 9}}},
            {b"a": [b"info", {b"info": b"info"}], b"zz": b"4:infod4:name6:leurree"},
        )
        for numero, autres in enumerate(entourages):
            with self.subTest(entourage=numero):
                meta = dict(autres)
                meta[b"info"] = INFO_MINIMALE
                infos = tf.parse_torrent(self.ecrire("borne%d.torrent" % numero, h.bencode(meta)))
                self.assertEqual(infos["infohash"], attendu)
                self.assertEqual(infos["name"], "x")
                self.assertEqual(infos["size"], 5)

    def test_infohash_info_non_canonique(self):
        """Clés non triées, entier à zéros de tête : on hache les octets lus, pas un ré-encodage."""
        octets_info = (b"d4:name3:abc6:lengthi0042e12:piece lengthi16384e6:pieces20:"
                       + b"\0" * 20 + b"e")
        brut = b"d8:announce15:http://t.test/a4:info" + octets_info + b"e"
        infos = tf.parse_torrent(self.ecrire("non-canonique.torrent", brut))
        self.assertEqual(infos["infohash"], sha1_majuscule(octets_info))
        canonique = h.bencode({b"name": b"abc", b"length": 42, b"piece length": 16384,
                               b"pieces": b"\0" * 20})
        self.assertNotEqual(infos["infohash"], sha1_majuscule(canonique))
        self.assertEqual((infos["name"], infos["size"]), ("abc", 42))

    def test_infohash_depend_du_contenu_pas_du_tracker(self):
        """Même info = même empreinte quel que soit le tracker ; info différente = empreinte différente."""
        def empreinte(nom_fichier, meta):
            return tf.parse_torrent(self.ecrire(nom_fichier, h.bencode(meta)))["infohash"]

        a = empreinte("a.torrent", {b"announce": b"http://un.test/a", b"info": INFO_MINIMALE})
        b = empreinte("b.torrent", {b"announce": b"http://deux.test/a", b"info": INFO_MINIMALE})
        autre_info = dict(INFO_MINIMALE)
        autre_info[b"length"] = 6
        c = empreinte("c.torrent", {b"announce": b"http://un.test/a", b"info": autre_info})
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_nom_utf8_prioritaire(self):
        """« name.utf-8 » l'emporte sur « name » (souvent dans un autre encodage)."""
        info = dict(INFO_MINIMALE)
        info[b"name"] = "Café".encode("latin-1")
        info[b"name.utf-8"] = "Café crème".encode("utf-8")
        infos = tf.parse_torrent(self.ecrire("cafe.torrent", h.bencode({b"info": info})))
        self.assertEqual(infos["name"], "Café crème")

    def test_nom_utf8_vide_retombe_sur_name(self):
        """Un « name.utf-8 » vide ne masque pas « name »."""
        info = dict(INFO_MINIMALE)
        info[b"name"] = b"Repli"
        info[b"name.utf-8"] = b""
        infos = tf.parse_torrent(self.ecrire("repli.torrent", h.bencode({b"info": info})))
        self.assertEqual(infos["name"], "Repli")

    def test_nom_accentue(self):
        """Un nom UTF-8 accentué (et non latin) est rendu tel quel."""
        for numero, nom in enumerate(("Été à Noël — l'œuvre", "日本語のファイル", "Ça & là [1080p]")):
            with self.subTest(nom=nom):
                chemin = self.ecrire("nom%d.torrent" % numero, h.torrent_bytes(nom))
                self.assertEqual(tf.parse_torrent(chemin)["name"], nom)

    def test_nom_illisible_en_utf8_est_remplace(self):
        """Un nom qui n'est pas de l'UTF-8 ne fait pas échouer la lecture."""
        info = dict(INFO_MINIMALE)
        info[b"name"] = b"Caf\xe9 \xff\xfe"
        infos = tf.parse_torrent(self.ecrire("latin.torrent", h.bencode({b"info": info})))
        self.assertTrue(infos["name"].startswith("Caf"))
        self.assertIn("\ufffd", infos["name"])

    def test_sans_nom(self):
        """Sans nom, un libellé de remplacement est fourni plutôt qu'une erreur."""
        info = {b"length": 5, b"piece length": 16384, b"pieces": b"\0" * 20}
        infos = tf.parse_torrent(self.ecrire("anonyme.torrent", h.bencode({b"info": info})))
        self.assertEqual(infos["name"], "(sans nom)")
        self.assertEqual(infos["size"], 5)

    def test_nom_de_type_inattendu(self):
        """Un nom qui n'est pas une chaîne est converti en texte, sans exception."""
        info = dict(INFO_MINIMALE)
        info[b"name"] = 2024
        infos = tf.parse_torrent(self.ecrire("nom-entier.torrent", h.bencode({b"info": info})))
        self.assertEqual(infos["name"], "2024")

    def test_drapeau_prive(self):
        """private=1 → True ; private=0 ou absent → False."""
        for valeur, attendu in ((1, True), (0, False), (None, False)):
            with self.subTest(private=valeur):
                info = dict(INFO_MINIMALE)
                if valeur is not None:
                    info[b"private"] = valeur
                chemin = self.ecrire("prive-%s.torrent" % valeur, h.bencode({b"info": info}))
                self.assertIs(tf.parse_torrent(chemin)["private"], attendu)

    def test_tracker_absent_ou_inattendu(self):
        """Sans « announce » (ou s'il n'est pas une chaîne), le tracker est une chaîne vide."""
        cas = {
            "absent": {b"info": INFO_MINIMALE},
            "liste": {b"announce": [b"http://t.test/a"], b"info": INFO_MINIMALE},
            "entier": {b"announce": 7, b"info": INFO_MINIMALE},
            "annonces multiples seulement": {b"announce-list": [[b"http://t.test/a"]],
                                             b"info": INFO_MINIMALE},
        }
        for numero, (nom, meta) in enumerate(cas.items()):
            with self.subTest(cas=nom):
                chemin = self.ecrire("tracker%d.torrent" % numero, h.bencode(meta))
                self.assertEqual(tf.parse_torrent(chemin)["tracker"], "")

    def test_tracker_illisible_en_utf8(self):
        """Un tracker qui n'est pas de l'UTF-8 est décodé avec remplacement, sans exception."""
        meta = {b"announce": b"http://t.test/\xff", b"info": INFO_MINIMALE}
        infos = tf.parse_torrent(self.ecrire("tracker-latin.torrent", h.bencode(meta)))
        self.assertTrue(infos["tracker"].startswith("http://t.test/"))

    def test_lecture_en_memoire_equivalente_au_disque(self):
        """Témoin : l'outil d'essais en mémoire donne le même résultat qu'un vrai fichier."""
        for numero, brut in enumerate((VALIDE, DENSE, h.torrent_bytes("Multi", files=4))):
            with self.subTest(torrent=numero):
                disque = tf.parse_torrent(self.ecrire("temoin%d.torrent" % numero, brut))
                memoire = analyser_octets(brut)
                for cle in ("name", "size", "files", "infohash", "private", "tracker"):
                    self.assertEqual(memoire[cle], disque[cle], cle)


# --------------------------------------------------------------------------- #
#  parse_torrent : entrées invalides -> toujours BencodeError
# --------------------------------------------------------------------------- #

class AnalyseTorrentInvalide(CasIsole):
    """Quel que soit le contenu, l'échec est une BencodeError — jamais une autre exception.

    assertRaises(BencodeError) laisse remonter toute autre exception : le test
    passe alors en ERREUR, ce qui est exactement le défaut recherché.
    """

    def _refuse(self, donnees: bytes, nom: str = "invalide.torrent"):
        chemin = self.ecrire(nom, donnees)
        with self.assertRaises(BencodeError):
            tf.parse_torrent(chemin)

    def _refuse_tous(self, cas: dict):
        for numero, (nom, donnees) in enumerate(cas.items()):
            with self.subTest(cas=nom):
                self._refuse(donnees, "cas%03d.torrent" % numero)

    def test_fichier_vide(self):
        """Un fichier de 0 octet (téléchargement pas encore commencé)."""
        self._refuse(b"")

    def test_page_html(self):
        """Une page d'erreur enregistrée sous le nom d'un .torrent."""
        self._refuse_tous({
            "page 404": b"<!DOCTYPE html><html><body>404 Not Found</body></html>",
            "erreur 500 de la seedbox": h.ULTRA_500,
            "html avec BOM": b"\xef\xbb\xbf<html></html>",
            "html commençant par d": b"doctype html><html><body>d</body></html>",
            "json": b'{"error": "not found"}',
            "texte": b"Fichier introuvable\r\n",
        })

    def test_tronque_a_chaque_octet(self):
        """Coupé à N'IMPORTE QUEL octet, un torrent valide devient une BencodeError."""
        torrents = {"simple": h.torrent_bytes("Simple"),
                    "multi": h.torrent_bytes("Multi", files=5),
                    "riche": DENSE}
        for nom, brut in torrents.items():
            self.assertRegex(analyser_octets(brut)["infohash"], r"^[0-9A-F]{40}$")   # témoin
            with mock.patch.object(Path, "read_bytes") as lecture:
                for coupe in range(len(brut)):
                    lecture.return_value = brut[:coupe]
                    with self.assertRaises(BencodeError, msg="%s coupé à %d octets" % (nom, coupe)):
                        tf.parse_torrent(CHEMIN_MEMOIRE)

    def test_tronque_sur_disque(self):
        """Même chose avec de vrais fichiers, à quelques points de coupe caractéristiques."""
        brut = h.torrent_bytes("Coupé", files=3)
        for coupe in (1, 2, 11, len(PREFIXE), len(PREFIXE) + 1, len(brut) // 2,
                      len(brut) - 21, len(brut) - 2, len(brut) - 1):
            with self.subTest(coupe=coupe):
                self._refuse(brut[:coupe], "coupe%04d.torrent" % coupe)

    def test_section_info_absente_ou_inattendue(self):
        """Pas de dictionnaire « info » : refus net."""
        self._refuse_tous({
            "dictionnaire vide": b"de",
            "sans info": h.bencode({b"announce": b"http://t.test/a", b"comment": b"rien"}),
            "info entier": b"d4:infoi5ee",
            "info chaine": b"d4:info3:abce",
            "info liste": b"d4:infolee",
            "info seulement en valeur": b"d7:comment4:infoe",
            "info imbrique ailleurs": h.bencode({b"autre": {b"info": INFO_MINIMALE}}),
        })

    def test_racine_qui_n_est_pas_un_dictionnaire(self):
        """La racine d'un .torrent doit être un dictionnaire."""
        self._refuse_tous({
            "liste": b"l4:infod4:name1:xee",
            "entier": b"i42e",
            "chaine": b"4:info",
            "d seul": b"d",
            "zeros": b"\x00" * 64,
            "d puis zeros": b"d" + b"\x00" * 64,
            "espace avant": b" d4:infod4:name1:xee",
        })

    def test_entiers_illisibles(self):
        """Entiers vides, non terminés ou contenant autre chose que des chiffres."""
        self._refuse_tous({
            "vide": b"d4:infod6:lengthiee",
            "lettres": b"d4:infod6:lengthi12x4e4:name1:xee",
            "non termine": b"d4:infod6:lengthi12",
            "decimal": b"d4:infod6:lengthi1.5e4:name1:xee",
            "hexadecimal": b"d4:infod6:lengthi0x10e4:name1:xee",
            "double signe": b"d4:infod6:lengthi--5e4:name1:xee",
            "signe seul": b"d4:infod6:lengthi-e4:name1:xee",
            "octets binaires": b"d4:infod6:lengthi\xff\xfee4:name1:xee",
            "entier avant info": b"d1:aiXe4:infod4:name1:xee",
            "entier comme cle": b"di1ei2e4:infod4:name1:xe" + b"x",
        })

    def test_longueurs_de_chaine_absurdes(self):
        """Une longueur annoncée plus grande que le fichier (ou illisible) est refusée."""
        self._refuse_tous({
            "plus longue que le fichier": b"d4:infod4:name99:xee",
            "vingt chiffres": b"d4:infod4:name99999999999999999999:xee",
            "5000 chiffres": b"d4:infod4:name" + b"9" * 5000 + b":xee",
            "sans deux-points": b"d4:infod4:name5",
            "negative": b"d4:infod4:name-5:abcdeee",
            "lettres dans la longueur": b"d4:infod4:name1x:abee",
            "cle trop longue": b"d999999:info",
            "cle a la racine": b"d99999999999999999999999:infod4:name1:xee",
            "un octet manquant": b"d4:infod4:name3:xye",
        })

    def test_imbrication_demesuree(self):
        """Des dizaines de milliers de listes imbriquées : BencodeError, pas RecursionError."""
        self._refuse_tous({
            "listes ouvertes dans info": b"d4:info" + b"l" * 100_000,
            "listes fermees dans info": b"d4:info" + b"l" * 100_000 + b"e" * 100_000 + b"e",
            "listes avant info": (b"d1:a" + b"l" * 50_000 + b"e" * 50_000
                                  + b"4:infod4:name1:xee"),
            "dictionnaires ouverts": b"d4:info" + b"d1:a" * 50_000,
            "listes et dictionnaires": b"d4:info" + b"ld1:a" * 30_000,
            "listes a la racine": b"d" + b"l" * 100_000,
        })
        # l'analyseur reste utilisable après ces refus
        self.assertEqual(tf.parse_torrent(self.ecrire("apres.torrent", VALIDE))["name"], "Valide")

    def test_cles_non_hachables(self):
        """Une liste ou un dictionnaire en guise de clé."""
        self._refuse_tous({
            "liste a la racine": b"dli1ee4:infod4:name1:xee",
            "dictionnaire a la racine": b"dde4:infod4:name1:xee",
            "liste dans info": b"d4:infodlei1e4:name1:xee",
            "dictionnaire dans info": b"d4:infoddei1e4:name1:xee",
        })

    def test_valeurs_de_type_inattendu(self):
        """Longueurs qui ne sont pas des nombres."""
        self._refuse_tous({
            "length liste": b"d4:infod6:lengthle4:name1:xee",
            "length dictionnaire": b"d4:infod6:lengthde4:name1:xee",
            "length texte": b"d4:infod6:length3:abc4:name1:xee",
            "length de fichier dictionnaire": b"d4:infod5:filesld6:lengthdeee4:name1:xee",
            "length de fichier liste": b"d4:infod5:filesld6:lengthleee4:name1:xee",
            "length de fichier texte": b"d4:infod5:filesld6:length2:abee4:name1:xee",
        })

    def test_octets_inattendus(self):
        """Un octet qui n'ouvre aucune valeur bencode."""
        self._refuse_tous({
            "valeur inconnue": b"d4:infod4:namexee",
            "cle inconnue": b"d4:infodx4:name1:xee",
            "fin de ligne": b"d4:info\r\nd4:name1:xee",
            "binaire": b"d4:info" + bytes(range(256)),
            "info sans fin": b"d4:infod4:name1:x",
            "racine sans fin": b"d4:infod4:name1:xe",
        })

    def test_entier_geant_jamais_d_autre_exception(self):
        """Un entier de plusieurs milliers de chiffres : accepté ou BencodeError, rien d'autre."""
        for chiffres in (400, 4000, 5000, 20_000):
            with self.subTest(chiffres=chiffres):
                brut = b"d4:infod6:lengthi" + b"9" * chiffres + b"e4:name1:xee"
                try:
                    infos = analyser_octets(brut)
                except BencodeError:
                    continue
                self.assertIsInstance(infos["size"], int)

    def test_mutations_aleatoires_jamais_d_autre_exception(self):
        """Milliers de torrents abîmés au hasard (graine fixe) : résultat ou BencodeError."""
        alea = random.Random(20261111)
        alphabet = b"dlie0123456789:-\x00\xff"
        bilan = {"acceptes": 0, "refuses": 0}
        bases = (h.torrent_bytes("Simple"), h.torrent_bytes("Multi", files=4), DENSE)
        self.assertEqual(analyser_octets(bases[0])["name"], "Simple")              # témoin
        with mock.patch.object(Path, "read_bytes") as lecture:
            for base in bases:
                for _ in range(2500):
                    mutant = self._muter(alea, base, alphabet)
                    lecture.return_value = mutant
                    try:
                        infos = tf.parse_torrent(CHEMIN_MEMOIRE)
                    except BencodeError:
                        bilan["refuses"] += 1
                        continue
                    except Exception as exc:        # le défaut recherché
                        self.fail("%s inattendue (%s) pour %r"
                                  % (type(exc).__name__, exc, mutant))
                    bilan["acceptes"] += 1
                    # un torrent accepté donne toujours un résultat cohérent
                    self.assertIsInstance(infos["name"], str)
                    self.assertIsInstance(infos["size"], int)
                    self.assertIsInstance(infos["files"], int)
                    self.assertIsInstance(infos["tracker"], str)
                    self.assertRegex(infos["infohash"], r"^[0-9A-F]{40}$")
        self.assertEqual(bilan["acceptes"] + bilan["refuses"], 7500)
        self.assertGreater(bilan["refuses"], 0)

    @staticmethod
    def _muter(alea, base: bytes, alphabet: bytes) -> bytes:
        octets = bytearray(base)
        for _ in range(alea.randint(1, 4)):
            position = alea.randrange(len(octets))
            operation = alea.randrange(3)
            if operation == 0:                                        # remplacement
                octets[position] = alea.choice(alphabet)
            elif operation == 1:                                      # suppression
                del octets[position:position + alea.randint(1, 5)]
            else:                                                     # insertion
                octets[position:position] = bytes(alea.choice(alphabet)
                                                  for _ in range(alea.randint(1, 4)))
            if not octets:
                octets = bytearray(b"d")
        return bytes(octets)


# --------------------------------------------------------------------------- #
#  parse_magnet et MAGNET_RE
# --------------------------------------------------------------------------- #

class LienMagnet(unittest.TestCase):

    def _refuse(self, lien, fragment: str = ""):
        with self.assertRaises(MagnetError) as contexte:
            tf.parse_magnet(lien)
        if fragment:
            self.assertIn(fragment, str(contexte.exception))

    def test_empreinte_hexadecimale(self):
        """40 caractères hexadécimaux, rendus en majuscules."""
        infos = tf.parse_magnet(MAGNET)
        self.assertEqual(infos["infohash"], HEX40.upper())
        self.assertEqual(tf.parse_magnet("magnet:?xt=urn:btih:" + HEX40.upper())["infohash"],
                         HEX40.upper())

    def test_structure_du_resultat(self):
        """Un magnet n'a ni taille ni nombre de fichiers connus."""
        infos = tf.parse_magnet(MAGNET + "&dn=Nom")
        self.assertEqual(infos, {"name": "Nom", "size": 0, "files": 0,
                                 "infohash": HEX40.upper(), "trackers": []})

    def test_empreinte_base32_convertie_en_hexadecimal(self):
        """32 caractères base32 donnent la même empreinte 40 hex que la forme hexadécimale."""
        self.assertEqual(len(BASE32), 32)
        attendu = tf.parse_magnet(MAGNET)["infohash"]
        for variante in (BASE32, BASE32.lower(), BASE32.capitalize()):
            with self.subTest(base32=variante):
                infos = tf.parse_magnet("magnet:?xt=urn:btih:" + variante + "&dn=Nom")
                self.assertEqual(infos["infohash"], attendu)
                self.assertRegex(infos["infohash"], r"^[0-9A-F]{40}$")

    def test_base32_valeur_de_reference(self):
        """Vecteur connu : 32 « A » en base32 valent vingt octets nuls."""
        self.assertEqual(tf.parse_magnet("magnet:?xt=urn:btih:" + "A" * 32)["infohash"], "0" * 40)
        self.assertEqual(tf.parse_magnet("magnet:?xt=urn:btih:" + "7" * 32)["infohash"], "F" * 40)

    def test_nom_decode_avec_accents_et_plus(self):
        """Le nom (dn) est décodé : séquences %XX en UTF-8 et « + » pour l'espace."""
        cas = {
            "dn=%C3%89t%C3%A9+%C3%A0+No%C3%ABl": "Été à Noël",
            "dn=Mon+Film+2024": "Mon Film 2024",
            "dn=Mon%20Film%202024": "Mon Film 2024",
            "dn=Le%20Ch%C3%A2teau+dans+le+ciel+%5B1080p%5D": "Le Château dans le ciel [1080p]",
            "dn=%E6%97%A5%E6%9C%AC%E8%AA%9E": "日本語",
            "dn=Fran%C3%A7ois+%26+Zo%C3%A9": "François & Zoé",
            "dn=Été à Noël": "Été à Noël",
            "dn=++Espaces+autour++": "Espaces autour",
        }
        for parametre, attendu in cas.items():
            with self.subTest(dn=parametre):
                self.assertEqual(tf.parse_magnet(MAGNET + "&" + parametre)["name"], attendu)

    def test_nom_place_avant_l_empreinte(self):
        """L'ordre des paramètres est indifférent."""
        infos = tf.parse_magnet("magnet:?dn=Avant&tr=http%3A%2F%2Ft.test%2Fa&xt=urn:btih:" + HEX40)
        self.assertEqual((infos["name"], infos["infohash"]), ("Avant", HEX40.upper()))
        self.assertEqual(infos["trackers"], ["http://t.test/a"])

    def test_nom_mal_encode_ne_fait_pas_echouer(self):
        """Un dn en latin-1 (%E9) ou tronqué (%C3) est décodé au mieux, sans exception."""
        for parametre in ("dn=Caf%E9", "dn=Caf%C3", "dn=100%", "dn=%ZZ%"):
            with self.subTest(dn=parametre):
                infos = tf.parse_magnet(MAGNET + "&" + parametre)
                self.assertIsInstance(infos["name"], str)
                self.assertTrue(infos["name"])

    def test_nom_absent_nom_de_repli(self):
        """Sans dn exploitable, le nom est « Magnet » suivi du début de l'empreinte."""
        repli = "Magnet " + HEX40.upper()[:16]
        for lien in (MAGNET, MAGNET + "&dn=", MAGNET + "&dn=+++", MAGNET + "&dn=%20%20",
                     MAGNET + "&tr=http%3A%2F%2Ft.test%2Fa",
                     "magnet:?xt=urn:btih:" + BASE32):
            with self.subTest(lien=lien):
                self.assertEqual(tf.parse_magnet(lien)["name"], repli)

    def test_trackers_decodes_dans_l_ordre(self):
        """Les trackers (tr) sont décodés et gardés dans l'ordre du lien."""
        lien = (MAGNET + "&tr=udp%3A%2F%2Ftracker.test%3A80&dn=x"
                "&tr=http%3A%2F%2Fautre.test%2Fannounce")
        self.assertEqual(tf.parse_magnet(lien)["trackers"],
                         ["udp://tracker.test:80", "http://autre.test/announce"])

    def test_entree_entre_guillemets_ou_espaces(self):
        """Le shell peut livrer le lien entouré de guillemets ou d'espaces."""
        for lien in ('"%s"' % MAGNET, "  %s  " % MAGNET, '\t"%s&dn=Nom"\r\n' % MAGNET,
                     MAGNET + "\n"):
            with self.subTest(lien=lien):
                self.assertEqual(tf.parse_magnet(lien)["infohash"], HEX40.upper())

    def test_casse_du_schema_et_de_l_empreinte(self):
        """« MAGNET: » en majuscules et empreinte en casse mélangée sont acceptés."""
        melange = "".join(c.upper() if i % 2 else c for i, c in enumerate(HEX40))
        for lien in ("MAGNET:?xt=urn:btih:" + HEX40, "Magnet:?xt=urn:btih:" + HEX40.upper(),
                     "mAgNeT:?xt=urn:btih:" + melange):
            with self.subTest(lien=lien):
                self.assertEqual(tf.parse_magnet(lien)["infohash"], HEX40.upper())

    def test_casse_des_parametres_jamais_d_autre_exception(self):
        """« XT= » ou « URN:BTIH: » en majuscules : bonne empreinte ou MagnetError, rien d'autre."""
        for lien in ("magnet:?XT=urn:btih:" + HEX40, "magnet:?xt=URN:BTIH:" + HEX40,
                     "MAGNET:?XT=URN:BTIH:" + HEX40.upper(), "magnet:?Xt=Urn:Btih:" + BASE32,
                     "magnet:?xt=urn:btih:" + HEX40 + "&DN=Nom"):
            with self.subTest(lien=lien):
                try:
                    infos = tf.parse_magnet(lien)
                except MagnetError:
                    continue
                self.assertEqual(infos["infohash"], HEX40.upper())

    def test_casse_de_xt_coherente_avec_magnet_re(self):
        """Corrigé — MAGNET_RE (insensible à la casse) accepte « XT= » et « URN:BTIH: »
        que parse_magnet refuse ensuite (« aucun infohash exploitable »)."""
        for lien in ("magnet:?XT=urn:btih:" + HEX40, "magnet:?xt=URN:BTIH:" + HEX40):
            filtre = bool(tf.MAGNET_RE.match(lien))
            try:
                tf.parse_magnet(lien)
                analyse = True
            except MagnetError:
                analyse = False
            self.assertEqual(filtre, analyse, lien)

    def test_plus_encode_conserve_dans_le_nom(self):
        """Corrigé — un « + » encodé (%2B) est transformé en espace (« C++ » devient « C »)."""
        self.assertEqual(tf.parse_magnet(MAGNET + "&dn=C%2B%2B+Primer")["name"], "C++ Primer")

    def test_ce_n_est_pas_un_magnet(self):
        """Tout ce qui ne commence pas par « magnet: » est refusé comme tel."""
        for lien in ("", "   ", None, "http://exemple.test/x.torrent", "magnets:?xt=urn:btih:" + HEX40,
                     "x" + MAGNET, "C:\\dossier\\fichier.torrent", "urn:btih:" + HEX40):
            with self.subTest(lien=lien):
                self._refuse(lien, "pas un magnet")

    def test_xt_absent(self):
        """Sans xt=urn:btih:, le lien est invalide."""
        for lien in ("magnet:", "magnet:?", "magnet:?dn=Nom", "magnet:?xt=", "magnet:?xt=urn:btih:",
                     "magnet:?xt=urn:sha1:" + BASE32, "magnet:?xt=urn:btmh:1220" + HEX40 + HEX40[:24],
                     "magnet:?dn=xt=urn:btih:", "magnet:xt=urn:btih:" + HEX40,
                     "magnet:?notxt=urn:btih:" + HEX40):
            with self.subTest(lien=lien):
                self._refuse(lien)

    def test_xt_trop_court_trop_long_ou_mal_forme(self):
        """Une empreinte qui n'a ni 40 caractères hexadécimaux ni 32 caractères base32."""
        for empreinte in (HEX40[:39], HEX40 + "a", HEX40[:20], BASE32[:31], BASE32 + "A",
                          "g" * 40, "z" * 40, "1" * 32, "8" * 32, "0" * 32, HEX40[:38] + "--",
                          HEX40 + "-suite", HEX40 + "%20", HEX40[:20] + " " + HEX40[20:],
                          HEX40 + HEX40):
            with self.subTest(empreinte=empreinte):
                self._refuse("magnet:?xt=urn:btih:" + empreinte)
                self._refuse("magnet:?xt=urn:btih:" + empreinte + "&dn=Nom")

    def test_plusieurs_xt(self):
        """Le premier xt exploitable est retenu ; un xt d'un autre type est ignoré."""
        infos = tf.parse_magnet("magnet:?xt=urn:sha1:" + BASE32 + "&xt=urn:btih:" + HEX40)
        self.assertEqual(infos["infohash"], HEX40.upper())
        infos = tf.parse_magnet(MAGNET + "&xt=urn:btih:" + AUTRE40)
        self.assertEqual(infos["infohash"], HEX40.upper())

    def test_deux_liens_colles_refuses(self):
        """Deux magnets collés par un espace ne donnent pas un résultat approximatif."""
        self._refuse(MAGNET + " " + AUTRE_MAGNET)

    def test_entrees_hostiles(self):
        """Caractères sosies, retours à la ligne, octets nuls, liens interminables : MagnetError."""
        hostiles = (
            "magnet:?xt=urn:btih:" + "\u212a" * 32,              # signe Kelvin, sosie de K
            "magnet:?xt=urn:btih:" + "\u017f" * 32,              # s long
            "magnet:?xt=urn:btih:" + "\uff21" * 40,              # A pleine chasse
            "magnet:?xt=urn:btih:" + "\u0661" * 40,              # chiffres arabes
            "magnet:?xt=urn:btih:" + HEX40 + "\n& calc.exe",
            "magnet:?xt=urn:btih:" + HEX40 + "\x00",
            "magnet:?dn=a\nxt=urn:btih:" + HEX40,
            "magnet:?" + "&".join(["xt=urn:btih:" + HEX40[:39]] * 5000),
            "magnet:?dn=" + "A" * 200_000,
            "magnet:?xt=urn:btih:" + "A" * 200_000,
        )
        for numero, lien in enumerate(hostiles):
            with self.subTest(cas=numero):
                self._refuse(lien)

    def test_lien_tres_long_mais_valide(self):
        """Un nom interminable ne gêne pas la lecture de l'empreinte."""
        infos = tf.parse_magnet(MAGNET + "&dn=" + "A" * 100_000)
        self.assertEqual(infos["infohash"], HEX40.upper())
        self.assertEqual(len(infos["name"]), 100_000)

    def test_filtre_magnet_re(self):
        """MAGNET_RE : « magnet:? » en tête, puis xt=urn:btih: suivi de 40 hex ou 32 base32."""
        acceptes = (MAGNET, MAGNET + "&dn=Nom", "magnet:?dn=Nom&xt=urn:btih:" + HEX40,
                    "magnet:?xt=urn:btih:" + BASE32, "magnet:?xt=urn:btih:" + BASE32.lower(),
                    "MAGNET:?XT=URN:BTIH:" + HEX40.upper(), "magnet:?xt=urn:btih:" + HEX40 + "&")
        refuses = ("", "magnet:", "magnet:?dn=Nom", " " + MAGNET, "x" + MAGNET,
                   "http://exemple.test/?xt=urn:btih:" + HEX40, "magnet:xt=urn:btih:" + HEX40,
                   "magnet:?xt=urn:btih:" + HEX40[:39], "magnet:?xt=urn:btih:" + HEX40 + "a",
                   "magnet:?xt=urn:btih:" + BASE32[:31], "magnet:?xt=urn:btih:" + BASE32 + "A",
                   "magnet:?notxt=urn:btih:" + HEX40, "magnet:?dn=a\nxt=urn:btih:" + HEX40,
                   "magnet:?xt=urn:sha1:" + BASE32)
        for lien in acceptes:
            with self.subTest(accepte=lien):
                self.assertIsNotNone(tf.MAGNET_RE.match(lien))
        for lien in refuses:
            with self.subTest(refuse=lien):
                self.assertIsNone(tf.MAGNET_RE.match(lien))


# --------------------------------------------------------------------------- #
#  torrent_is_ready
# --------------------------------------------------------------------------- #

class TorrentPret(CasIsole):

    @staticmethod
    def _horloge(instant: float):
        """Fige l'heure vue par torrentfile (et par lui seul)."""
        return mock.patch.object(tf, "time", mock.Mock(time=mock.Mock(return_value=instant)))

    def test_fichier_valide_et_ancien(self):
        """Un .torrent complet, écrit depuis assez longtemps, est prêt."""
        chemin = self.ecrire("pret.torrent", VALIDE, age=60)
        self.assertIs(tf.torrent_is_ready(chemin), True)
        self.assertIs(tf.torrent_is_ready(chemin, min_age=0), True)
        self.assertIs(tf.torrent_is_ready(chemin, min_age=30), True)

    def test_fichier_absent(self):
        """Un fichier disparu entre-temps (renommé par le navigateur) n'est pas prêt."""
        self.assertIs(tf.torrent_is_ready(self.travail / "absent.torrent", min_age=0), False)
        self.assertIs(tf.torrent_is_ready(self.travail / "sans" / "dossier.torrent"), False)

    def test_fichier_vide(self):
        """0 octet : le téléchargement n'a pas commencé."""
        chemin = self.ecrire("vide.torrent", b"")
        self.assertIs(tf.torrent_is_ready(chemin, min_age=0), False)

    def test_page_html(self):
        """Une page d'erreur déguisée en .torrent n'est jamais prête."""
        for numero, contenu in enumerate((b"<!DOCTYPE html><html></html>", h.ULTRA_500,
                                          b"doctype html", b"d", b"dxyz", b"de",
                                          b"d4:infoi1ee")):
            with self.subTest(contenu=contenu[:20]):
                chemin = self.ecrire("faux%d.torrent" % numero, contenu)
                self.assertIs(tf.torrent_is_ready(chemin, min_age=0), False)

    def test_fichier_tronque(self):
        """Un téléchargement interrompu n'est pas prêt, où que soit la coupure."""
        brut = h.torrent_bytes("Partiel", files=3)
        for coupe in (1, 10, len(PREFIXE) + 3, len(brut) // 2, len(brut) - 1):
            with self.subTest(coupe=coupe):
                chemin = self.ecrire("partiel%04d.torrent" % coupe, brut[:coupe])
                self.assertIs(tf.torrent_is_ready(chemin, min_age=0), False)
        self.assertIs(tf.torrent_is_ready(self.ecrire("entier.torrent", brut), min_age=0), True)

    def test_imbrication_demesuree(self):
        """Un fichier hostile ne fait pas planter la surveillance : simplement « pas prêt »."""
        chemin = self.ecrire("profond.torrent", b"d4:info" + b"l" * 100_000)
        self.assertIs(tf.torrent_is_ready(chemin, min_age=0), False)

    def test_trop_recent_avec_min_age(self):
        """Tant que le fichier a été modifié depuis moins de min_age secondes, on attend."""
        chemin = self.ecrire("recent.torrent", VALIDE, age=0)
        modification = chemin.stat().st_mtime
        with self._horloge(modification + 5):
            self.assertIs(tf.torrent_is_ready(chemin, min_age=10), False)
            self.assertIs(tf.torrent_is_ready(chemin, min_age=5.5), False)
            self.assertIs(tf.torrent_is_ready(chemin, min_age=4.5), True)
            self.assertIs(tf.torrent_is_ready(chemin, min_age=0), True)

    def test_delai_par_defaut(self):
        """Par défaut, un fichier modifié il y a moins de 0,4 s n'est pas encore prêt."""
        chemin = self.ecrire("defaut.torrent", VALIDE, age=0)
        modification = chemin.stat().st_mtime
        with self._horloge(modification + 0.2):
            self.assertIs(tf.torrent_is_ready(chemin), False)
        with self._horloge(modification + 0.6):
            self.assertIs(tf.torrent_is_ready(chemin), True)

    def test_dossier(self):
        """Un dossier nommé comme un .torrent n'est pas prêt (et ne lève rien)."""
        dossier = self.travail / "dossier.torrent"
        dossier.mkdir()
        (dossier / "contenu.bin").write_bytes(b"d" * 100)
        self.assertIs(tf.torrent_is_ready(dossier, min_age=0), False)

    @unittest.skipUnless(os.name == "nt", "verrou d'octets propre à Windows (msvcrt)")
    def test_fichier_verrouille_puis_libere(self):
        """Verrouillé par un autre programme : pas prêt ; relâché : prêt au tour suivant."""
        import msvcrt
        chemin = self.ecrire("verrou.torrent", VALIDE, age=60)
        with open(chemin, "r+b") as verrou:
            msvcrt.locking(verrou.fileno(), msvcrt.LK_NBLCK, len(VALIDE))
            try:
                self.assertIs(tf.torrent_is_ready(chemin, min_age=0), False)
            finally:
                verrou.seek(0)
                msvcrt.locking(verrou.fileno(), msvcrt.LK_UNLCK, len(VALIDE))
        self.assertIs(tf.torrent_is_ready(chemin, min_age=0), True)


# --------------------------------------------------------------------------- #
#  human_size
# --------------------------------------------------------------------------- #

class TailleLisible(unittest.TestCase):

    def test_octets_sans_decimales(self):
        """En dessous de 1024, la taille est en octets entiers."""
        for valeur, attendu in ((0, "0 o"), (1, "1 o"), (512, "512 o"), (1023, "1023 o")):
            with self.subTest(valeur=valeur):
                self.assertEqual(tf.human_size(valeur), attendu)

    def test_seuils_des_unites(self):
        """Chaque puissance de 1024 fait passer à l'unité suivante, avec deux décimales."""
        attendus = (
            (1024, "1.00 Ko"), (1536, "1.50 Ko"), (1024 ** 2 - 1024, "1023.00 Ko"),
            (1024 ** 2, "1.00 Mo"), (3_145_728, "3.00 Mo"), (1024 ** 3 - 1024 ** 2, "1023.00 Mo"),
            (1024 ** 3, "1.00 Go"), (int(2.5 * 1024 ** 3), "2.50 Go"),
            (1024 ** 4, "1.00 To"), (1024 ** 5, "1.00 Po"),
        )
        for valeur, attendu in attendus:
            with self.subTest(valeur=valeur):
                self.assertEqual(tf.human_size(valeur), attendu)

    def test_au_dela_du_petaoctet(self):
        """Il n'y a pas d'unité au-delà de « Po » : la valeur continue de grandir."""
        self.assertEqual(tf.human_size(1024 ** 6), "1024.00 Po")
        self.assertEqual(tf.human_size(5 * 1024 ** 6), "5120.00 Po")

    def test_valeur_absente(self):
        """None (taille inconnue) vaut zéro."""
        self.assertEqual(tf.human_size(None), "0 o")

    def test_taille_demesuree_ne_plante_pas(self):
        """Corrigé — un .torrent déclarant une longueur de 400 chiffres est accepté par
        parse_torrent, puis human_size lève OverflowError (l'ajout à la liste plante)."""
        try:
            infos = analyser_octets(b"d4:infod6:lengthi" + b"9" * 400 + b"e4:name1:xee")
        except BencodeError:
            return                              # correction possible : refuser le fichier
        self.assertIsInstance(tf.human_size(infos["size"]), str)


# --------------------------------------------------------------------------- #
#  paths : emplacements, migration, purge
# --------------------------------------------------------------------------- #

class Emplacements(CasIsole):

    def test_bac_a_sable_actif(self):
        """Les tests tournent redirigés : rien ne peut viser le profil réel."""
        self.assertTrue(paths.is_sandboxed())
        self.assertTrue(os.environ.get(paths.ENV_DATA_DIR))
        self.assertEqual(paths.ENV_DATA_DIR, "TORRENT_UPLOADER_DATA_DIR")
        for chemin in (paths.data_dir(), paths.config_path(), paths.token_path(),
                       paths.log_path(), paths.inbox_dir(), paths.native_host_dir()):
            self.assertTrue(chemin.is_relative_to(h.SANDBOX), chemin)

    def test_data_dir_respecte_la_redirection(self):
        """data_dir() suit la variable d'environnement, à chaque appel."""
        self.assertEqual(paths.data_dir(), self.donnees)
        autre = h.temp_dir("autre-")
        os.environ[paths.ENV_DATA_DIR] = str(autre)       # remis en place par CasIsole
        self.assertEqual(paths.data_dir(), autre)
        self.assertEqual(paths.config_path().parent, autre)

    def test_data_dir_cree_le_dossier_manquant(self):
        """Un dossier redirigé qui n'existe pas encore est créé, parents compris."""
        cible = self.donnees / "profil portable" / "données"
        self.assertFalse(cible.exists())
        os.environ[paths.ENV_DATA_DIR] = str(cible)       # remis en place par CasIsole
        self.assertEqual(paths.data_dir(), cible)
        self.assertTrue(cible.is_dir())
        self.assertEqual(paths.data_dir(), cible)         # second appel : toujours sans erreur

    def test_sans_redirection_le_profil_de_l_utilisateur(self):
        """Sans redirection, les données vont dans le profil (ici un faux profil, dans un
        processus séparé : le processus de test, lui, ne quitte jamais le bac à sable)."""
        faux_profil = h.temp_dir("profil-")
        env = dict(os.environ)
        env.pop(paths.ENV_DATA_DIR, None)
        env["PYTHONIOENCODING"] = "utf-8"
        if os.name == "nt":
            env["APPDATA"] = str(faux_profil)
            attendu = faux_profil / "TorrentUploader"
        else:
            env["XDG_CONFIG_HOME"] = str(faux_profil)
            attendu = faux_profil / "torrent-uploader"
        code = ("import json, sys; sys.path.insert(0, sys.argv[1]); "
                "from tuploader import paths; "
                "print(json.dumps([paths.is_sandboxed(), str(paths.data_dir()), "
                "str(paths.config_path())]))")
        fini = subprocess.run([sys.executable, "-c", code, str(h.ROOT)], env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(fini.returncode, 0, fini.stderr)
        redirige, dossier, fichier = json.loads(fini.stdout.strip().splitlines()[-1])
        self.assertIs(redirige, False)
        self.assertEqual(Path(dossier), attendu)
        self.assertEqual(Path(fichier), attendu / "config.json")
        self.assertTrue(attendu.is_dir())
        self.assertTrue(paths.is_sandboxed())             # le processus de test n'a pas bougé

    def test_fichiers_du_profil(self):
        """config.json, ipc_token.txt et erreurs.log vivent dans le dossier de données."""
        self.assertEqual(paths.config_path(), self.donnees / "config.json")
        self.assertEqual(paths.token_path(), self.donnees / "ipc_token.txt")
        self.assertEqual(paths.log_path(), self.donnees / "erreurs.log")
        # demander un chemin ne crée pas le fichier
        self.assertEqual(list(self.donnees.iterdir()), [])

    def test_dossiers_recus_et_hote_natif(self):
        """Les sous-dossiers « recus » et « native-host » sont créés à la demande."""
        recus = paths.inbox_dir()
        hote = paths.native_host_dir()
        self.assertEqual(recus, self.donnees / "recus")
        self.assertEqual(hote, self.donnees / "native-host")
        self.assertTrue(recus.is_dir())
        self.assertTrue(hote.is_dir())
        self.assertEqual(paths.inbox_dir(), recus)        # déjà présent : pas d'erreur

    def test_dossier_de_l_application(self):
        """APP_DIR est le dossier qui contient torrent_uploader.py ; les ressources sont dans le paquet."""
        self.assertEqual(paths.APP_DIR, h.ROOT)
        self.assertTrue((paths.APP_DIR / "torrent_uploader.py").is_file())
        self.assertEqual(paths.ASSETS_DIR, paths.APP_DIR / "tuploader" / "assets")

    def _ancienne_installation(self) -> Path:
        ancien = h.temp_dir("v1-")
        (ancien / "config.json").write_text('{"url": "http://127.0.0.1:1/v1"}', encoding="utf-8")
        (ancien / "ipc_token.txt").write_text("jeton-de-la-v1-0123456789", encoding="utf-8")
        (ancien / "erreurs.log").write_text("ancien journal\n", encoding="utf-8")
        return ancien

    def test_migration_sans_effet_en_bac_a_sable(self):
        """En bac à sable, migrate_legacy() ne déplace RIEN, même si des fichiers v1 existent."""
        ancien = self._ancienne_installation()
        avant = sorted(p.name for p in ancien.iterdir())
        with mock.patch.object(paths, "APP_DIR", ancien):
            self.assertEqual(paths.migrate_legacy(), [])
        self.assertEqual(sorted(p.name for p in ancien.iterdir()), avant)
        self.assertEqual((ancien / "ipc_token.txt").read_text(encoding="utf-8"),
                         "jeton-de-la-v1-0123456789")
        self.assertEqual(list(self.donnees.iterdir()), [])

    def test_migration_sans_effet_sur_le_vrai_dossier(self):
        """Appelée telle quelle pendant les tests, la migration ne touche pas au dossier du programme."""
        avant = sorted(p.name for p in paths.APP_DIR.iterdir())
        self.assertEqual(paths.migrate_legacy(), [])
        self.assertEqual(sorted(p.name for p in paths.APP_DIR.iterdir()), avant)
        self.assertEqual(list(self.donnees.iterdir()), [])

    def test_migration_deplace_sans_ecraser(self):
        """Hors bac à sable (simulé, entre deux dossiers temporaires) : les fichiers v1 sont
        déplacés vers le profil, sauf ceux qui y existent déjà."""
        ancien = self._ancienne_installation()
        (ancien / "erreurs.log").unlink()                               # absent côté v1
        (self.donnees / "ipc_token.txt").write_text("jeton-v2-deja-la", encoding="utf-8")
        with mock.patch.object(paths, "APP_DIR", ancien), \
                mock.patch.object(paths, "is_sandboxed", return_value=False):
            # garde-fous : source ET destination sont des dossiers temporaires
            self.assertEqual(paths.APP_DIR, ancien)
            self.assertEqual(paths.data_dir(), self.donnees)
            self.assertEqual(paths.migrate_legacy(), ["config.json"])
            self.assertEqual(paths.migrate_legacy(), [])                # rien à refaire
        self.assertTrue(paths.is_sandboxed())
        self.assertFalse((ancien / "config.json").exists())
        self.assertEqual(json.loads(paths.config_path().read_text(encoding="utf-8")),
                         {"url": "http://127.0.0.1:1/v1"})
        # le jeton déjà présent dans le profil n'est pas écrasé, l'ancien reste où il est
        self.assertEqual(paths.token_path().read_text(encoding="utf-8"), "jeton-v2-deja-la")
        self.assertTrue((ancien / "ipc_token.txt").exists())
        self.assertFalse(paths.log_path().exists())

    def test_migration_complete(self):
        """Les trois fichiers de la v1 sont déplacés et annoncés, dans l'ordre."""
        ancien = self._ancienne_installation()
        (ancien / "autre.txt").write_text("ne me touchez pas", encoding="utf-8")
        with mock.patch.object(paths, "APP_DIR", ancien), \
                mock.patch.object(paths, "is_sandboxed", return_value=False):
            self.assertEqual(paths.APP_DIR, ancien)
            self.assertEqual(paths.data_dir(), self.donnees)
            self.assertEqual(paths.migrate_legacy(),
                             ["config.json", "ipc_token.txt", "erreurs.log"])
        self.assertEqual(sorted(p.name for p in ancien.iterdir()), ["autre.txt"])
        self.assertEqual(sorted(p.name for p in self.donnees.iterdir()),
                         ["config.json", "erreurs.log", "ipc_token.txt"])
        self.assertEqual(paths.log_path().read_text(encoding="utf-8"), "ancien journal\n")

    def _recu(self, nom: str, jours: float) -> Path:
        chemin = paths.inbox_dir() / nom
        chemin.write_bytes(VALIDE)
        instant = time.time() - jours * 86400
        os.utime(chemin, (instant, instant))
        return chemin

    def test_purge_supprime_seulement_les_vieux_fichiers(self):
        """Les copies reçues il y a plus d'une semaine partent ; les autres restent."""
        vieux = self._recu("vieux.torrent", 8)
        tres_vieux = self._recu("tres-vieux.torrent", 400)
        limite = self._recu("six-jours.torrent", 6)
        recent = self._recu("recent.torrent", 0)
        sous_dossier = paths.inbox_dir() / "sous-dossier"
        sous_dossier.mkdir()
        garde = sous_dossier / "garde.torrent"
        garde.write_bytes(VALIDE)
        ancien = time.time() - 30 * 86400
        os.utime(garde, (ancien, ancien))
        os.utime(sous_dossier, (ancien, ancien))
        hors_boite = self.donnees / "config.json"
        hors_boite.write_text("{}", encoding="utf-8")
        os.utime(hors_boite, (ancien, ancien))

        self.assertIsNone(paths.purge_inbox())

        self.assertFalse(vieux.exists())
        self.assertFalse(tres_vieux.exists())
        self.assertTrue(limite.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(garde.exists())                   # les sous-dossiers ne sont pas visités
        self.assertTrue(hors_boite.exists())              # seul « recus » est purgé

    def test_purge_duree_reglable(self):
        """max_age_days fixe l'âge limite."""
        deux_jours = self._recu("deux-jours.torrent", 2)
        demi_journee = self._recu("demi-journee.torrent", 0.5)
        paths.purge_inbox(max_age_days=3)
        self.assertTrue(deux_jours.exists())
        paths.purge_inbox(max_age_days=1)
        self.assertFalse(deux_jours.exists())
        self.assertTrue(demi_journee.exists())

    def test_purge_dossier_vide_ou_inaccessible(self):
        """Rien à purger, ou dossier illisible : aucune exception."""
        paths.purge_inbox()                               # « recus » vient d'être créé, vide
        self.assertTrue(paths.inbox_dir().is_dir())
        with mock.patch.object(paths, "inbox_dir", side_effect=PermissionError("refusé")):
            paths.purge_inbox()

    def test_flux_standard_absents_remplaces(self):
        """Sous pythonw, stdout/stderr/stdin valent None : ils sont remplacés par des flux muets."""
        sauvegarde = sys.stdout, sys.stderr, sys.stdin
        ouverts, lu = [], None
        try:
            sys.stdout = sys.stderr = sys.stdin = None
            paths.ensure_std_streams()
            ouverts = [sys.stdout, sys.stderr, sys.stdin]
            if all(flux is not None for flux in ouverts):
                sys.stdout.write("écriture sans console\n")
                sys.stderr.write("écriture sans console\n")
                lu = sys.stdin.read()
        finally:
            sys.stdout, sys.stderr, sys.stdin = sauvegarde
            for flux in ouverts:
                if flux is not None:
                    flux.close()
        self.assertTrue(all(flux is not None for flux in ouverts))
        self.assertEqual(lu, "")

    def test_flux_standard_presents_conserves(self):
        """Des flux existants ne sont pas remplacés."""
        avant = sys.stdout, sys.stderr, sys.stdin
        paths.ensure_std_streams()
        self.assertIs(sys.stdout, avant[0])
        self.assertIs(sys.stderr, avant[1])
        self.assertIs(sys.stdin, avant[2])


# --------------------------------------------------------------------------- #
#  paths : journal d'erreurs
# --------------------------------------------------------------------------- #

class Journal(CasIsole):

    @staticmethod
    def _ancien_journal(taille_min: int):
        """Journal au format de log_error, de plus de `taille_min` octets. Retourne (octets, nombre)."""
        lignes, total, numero = [], 0, 0
        while total <= taille_min:
            ligne = ("\n[2024-01-01 00:00:00] ancienne entrée %06d — éàü\n" % numero).encode("utf-8")
            lignes.append(ligne)
            total += len(ligne)
            numero += 1
        return b"".join(lignes), numero

    def test_ajoute_sans_ecraser(self):
        """Chaque appel ajoute une entrée horodatée à la suite des précédentes."""
        self.assertFalse(paths.log_path().exists())
        self.assertIsNone(paths.log_error("première erreur"))
        paths.log_error("seconde erreur — accentuée : éàüœ")
        texte = self.journal()
        self.assertRegex(texte, r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] première erreur\n")
        self.assertRegex(texte, r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] seconde erreur — accentuée : éàüœ\n")
        self.assertLess(texte.index("première erreur"), texte.index("seconde erreur"))
        self.assertEqual(len(re.findall(r"^\[", texte, re.M)), 2)

    def test_conserve_un_journal_existant(self):
        """Un journal déjà présent (migré de la v1) est complété, pas remplacé."""
        paths.log_path().write_text("contenu antérieur\n", encoding="utf-8")
        paths.log_error("nouvelle entrée")
        texte = self.journal()
        self.assertTrue(texte.startswith("contenu antérieur\n"))
        self.assertIn("nouvelle entrée", texte)

    def test_avec_exception_et_sa_trace(self):
        """L'exception fournie est journalisée avec sa trace complète."""
        try:
            raise ValueError("valeur refusée : é")
        except ValueError as exc:
            paths.log_error("contexte de l'erreur", exc)
        texte = self.journal()
        self.assertIn("contexte de l'erreur", texte)
        self.assertIn("Traceback (most recent call last)", texte)
        self.assertIn("ValueError: valeur refusée : é", texte)
        self.assertIn("test_avec_exception_et_sa_trace", texte)

    def test_exception_enchainee(self):
        """La cause d'une exception enchaînée figure aussi dans le journal."""
        try:
            try:
                raise KeyError("cause")
            except KeyError as cause:
                raise BencodeError("fichier .torrent corrompu") from cause
        except BencodeError as exc:
            paths.log_error("lecture", exc)
        texte = self.journal()
        self.assertIn("KeyError: 'cause'", texte)
        self.assertIn("BencodeError: fichier .torrent corrompu", texte)

    def test_exception_jamais_levee(self):
        """Une exception construite sans avoir été levée (pas de trace) est acceptée."""
        paths.log_error("sans trace", RuntimeError("jamais levée"))
        paths.log_error("interruption", KeyboardInterrupt())
        texte = self.journal()
        self.assertIn("RuntimeError: jamais levée", texte)
        self.assertIn("KeyboardInterrupt", texte)

    def test_ne_leve_jamais_avec_des_arguments_etranges(self):
        """Contexte non textuel, « exception » qui n'en est pas une : aucune exception ne sort."""

        class Recalcitrante(Exception):
            def __str__(self):
                raise RuntimeError("même str() échoue")

        paths.log_error(12345)
        paths.log_error(None)
        paths.log_error("octets", b"pas une exception")
        paths.log_error("objet", object())
        paths.log_error("récalcitrante", Recalcitrante())
        paths.log_error("après tout cela")
        texte = self.journal()
        self.assertIn("12345", texte)
        self.assertIn("après tout cela", texte)           # le journal continue de fonctionner

    def test_ne_leve_jamais_si_le_journal_est_inaccessible(self):
        """Journal impossible à ouvrir (un dossier porte son nom) : l'erreur est avalée."""
        paths.log_path().mkdir()
        try:
            raise OSError("disque plein")
        except OSError as exc:
            self.assertIsNone(paths.log_error("écriture impossible", exc))
        self.assertTrue(paths.log_path().is_dir())

    def test_ne_leve_jamais_si_le_dossier_de_donnees_echoue(self):
        """Même si l'emplacement du journal ne peut pas être déterminé, rien ne sort."""
        with mock.patch.object(paths, "log_path", side_effect=PermissionError("profil verrouillé")):
            self.assertIsNone(paths.log_error("sans journal", ValueError("x")))

    def test_rotation_garde_la_fin(self):
        """Au-dessus du plafond, seule la moitié la plus récente est gardée, entrées entières."""
        plafond = 20_000
        ancien, nombre = self._ancien_journal(plafond)
        self.assertGreater(len(ancien), plafond)
        paths.log_path().write_bytes(ancien)
        with mock.patch.object(paths, "_LOG_MAX_BYTES", plafond):
            paths.log_error("entrée la plus récente")
        brut = paths.log_path().read_bytes()
        texte = brut.decode("utf-8")                      # strict : aucun caractère coupé en deux
        self.assertLess(len(brut), plafond * 0.6)
        self.assertGreater(len(brut), plafond * 0.4)
        self.assertTrue(brut.lstrip(b"\r\n").startswith(b"[2024-01-01 00:00:00] ancienne"))
        numeros = [int(n) for n in re.findall(r"ancienne entrée (\d{6})", texte)]
        self.assertGreater(numeros[0], 0)                               # le début a disparu
        self.assertEqual(numeros, list(range(numeros[0], nombre)))      # la fin est intacte
        gardees = [ligne for ligne in texte.splitlines() if "ancienne" in ligne]
        for ligne in gardees:
            self.assertRegex(ligne, r"^\[2024-01-01 00:00:00\] ancienne entrée \d{6} — éàü$")
        self.assertTrue(texte.rstrip().endswith("entrée la plus récente"))
        self.assertEqual(texte.count("entrée la plus récente"), 1)

    def test_pas_de_rotation_jusqu_au_plafond(self):
        """Tant que le journal ne dépasse pas le plafond, rien n'est retiré."""
        ancien, _ = self._ancien_journal(5_000)
        paths.log_path().write_bytes(ancien)
        with mock.patch.object(paths, "_LOG_MAX_BYTES", len(ancien)):         # pile au plafond
            paths.log_error("ajout sans rotation")
        brut = paths.log_path().read_bytes()
        self.assertTrue(brut.startswith(ancien))
        self.assertIn("ajout sans rotation".encode("utf-8"), brut[len(ancien):])

    def test_rotation_des_le_premier_octet_de_trop(self):
        """Un seul octet au-dessus du plafond déclenche la rotation."""
        ancien, _ = self._ancien_journal(5_000)
        paths.log_path().write_bytes(ancien)
        with mock.patch.object(paths, "_LOG_MAX_BYTES", len(ancien) - 1):
            paths.log_error("ajout avec rotation")
        brut = paths.log_path().read_bytes()
        self.assertLess(len(brut), len(ancien))
        self.assertFalse(brut.startswith(ancien[:200]))
        self.assertTrue(brut.rstrip().endswith("ajout avec rotation".encode("utf-8")))

    def test_rotation_avec_le_plafond_reel(self):
        """Avec le plafond du produit, un journal trop gros est ramené à environ sa moitié."""
        plafond = paths._LOG_MAX_BYTES
        self.assertGreaterEqual(plafond, 64 * 1024)
        ancien, nombre = self._ancien_journal(plafond)
        paths.log_path().write_bytes(ancien)
        paths.log_error("après le plafond réel")
        brut = paths.log_path().read_bytes()
        self.assertLess(len(brut), plafond * 0.6)
        self.assertGreater(len(brut), plafond * 0.4)
        texte = brut.decode("utf-8")
        self.assertIn("ancienne entrée %06d" % (nombre - 1), texte)
        self.assertNotIn("ancienne entrée 000000", texte)
        self.assertTrue(texte.rstrip().endswith("après le plafond réel"))

    def test_rotation_d_un_journal_sans_entree_reconnaissable(self):
        """Un journal d'un seul bloc (aucun début d'entrée) est quand même raccourci."""
        plafond = 10_000
        bloc = bytes(65 + (i % 26) for i in range(plafond + 500))
        paths.log_path().write_bytes(bloc)
        with mock.patch.object(paths, "_LOG_MAX_BYTES", plafond):
            paths.log_error("après le bloc")
        brut = paths.log_path().read_bytes()
        self.assertLess(len(brut), plafond * 0.6)
        garde = brut[:brut.index(b"[")].rstrip(b"\r\n")
        self.assertGreater(len(garde), plafond * 0.4)
        self.assertTrue(bloc.endswith(garde))             # c'est bien la FIN qui est gardée

    def test_rotations_successives_bornent_la_taille(self):
        """Écrit en continu, le journal ne dépasse jamais durablement son plafond."""
        plafond = 3_000
        with mock.patch.object(paths, "_LOG_MAX_BYTES", plafond):
            for numero in range(400):
                paths.log_error("entrée continue %04d — é" % numero)
                self.assertLess(paths.log_path().stat().st_size, plafond + 200)
        texte = paths.log_path().read_bytes().decode("utf-8")
        numeros = [int(n) for n in re.findall(r"entrée continue (\d{4})", texte)]
        self.assertEqual(numeros[-1], 399)
        self.assertGreater(numeros[0], 0)
        self.assertEqual(numeros, list(range(numeros[0], 400)))


# --------------------------------------------------------------------------- #
#  config
# --------------------------------------------------------------------------- #

class Configuration(CasIsole):

    def _fichiers(self) -> list:
        return sorted(p.name for p in self.donnees.iterdir())

    def test_valeurs_par_defaut_sans_fichier(self):
        """Sans config.json, on obtient une copie des valeurs par défaut (et rien n'est créé)."""
        self.assertFalse(paths.config_path().exists())
        cfg = config.load_config()
        self.assertEqual(cfg, config.DEFAULT_CONFIG)
        self.assertIsNot(cfg, config.DEFAULT_CONFIG)
        cfg["url"] = "modifiée"
        cfg["nouvelle"] = 1
        self.assertEqual(config.DEFAULT_CONFIG["url"], "")
        self.assertNotIn("nouvelle", config.DEFAULT_CONFIG)
        self.assertEqual(config.load_config()["url"], "")
        self.assertEqual(self._fichiers(), [])

    def test_valeurs_par_defaut_attendues(self):
        """Les réglages d'origine : rien d'envoyé nulle part tant que l'adresse est vide."""
        cfg = config.load_config()
        self.assertEqual((cfg["url"], cfg["username"], cfg["password"]), ("", "", ""))
        self.assertIs(cfg["verify_ssl"], True)
        self.assertIs(cfg["start_torrents"], True)
        self.assertIs(cfg["watch_downloads"], False)
        self.assertIs(cfg["watch_auto_send"], False)
        self.assertEqual(cfg["timeout"], 60)
        self.assertEqual(cfg["theme"], "system")
        self.assertEqual(cfg["ipc_port"], 38471)
        self.assertEqual((cfg["favorite_dirs"], cfg["favorite_labels"]), ([], []))

    def test_valeurs_du_fichier(self):
        """Les valeurs enregistrées remplacent les défauts ; les clés absentes gardent le leur."""
        h.write_config(url="http://127.0.0.1:1/rutorrent", username="zoé", timeout=15,
                       verify_ssl=False, favorite_labels=["Séries", "Films"])
        cfg = config.load_config()
        self.assertEqual(cfg["url"], "http://127.0.0.1:1/rutorrent")
        self.assertEqual(cfg["username"], "zoé")
        self.assertEqual(cfg["timeout"], 15)
        self.assertIs(cfg["verify_ssl"], False)
        self.assertEqual(cfg["favorite_labels"], ["Séries", "Films"])
        self.assertEqual(cfg["password"], "")
        self.assertEqual(cfg["watch_interval"], 3)
        self.assertEqual(set(cfg), set(config.DEFAULT_CONFIG))

    def test_cles_inconnues_ecartees(self):
        """Une clé que l'application ne connaît pas n'entre pas dans la configuration."""
        h.write_config(url="http://127.0.0.1:1/rutorrent", inconnue="x", URL="majuscules",
                       ancien_reglage={"a": 1}, **{"": "vide"})
        cfg = config.load_config()
        self.assertEqual(set(cfg), set(config.DEFAULT_CONFIG))
        self.assertEqual(cfg["url"], "http://127.0.0.1:1/rutorrent")
        self.assertEqual(self.journal(), "")              # ce n'est pas une erreur

    def test_json_corrompu_valeurs_par_defaut(self):
        """Un config.json illisible ne bloque pas le démarrage : défauts, et une ligne au journal."""
        illisibles = (b"{pas du json", b"", b'{"url": "http://127.0.0.1:1/x"', b"\xff\xfe\x00d",
                      b'{"url": "caf\xe9"}', b"{'url': 'x'}", b'{"url": "x"} suite')
        for numero, contenu in enumerate(illisibles):
            with self.subTest(contenu=contenu):
                paths.config_path().write_bytes(contenu)
                if paths.log_path().exists():
                    paths.log_path().unlink()
                self.assertEqual(config.load_config(), config.DEFAULT_CONFIG)
                self.assertEqual(paths.config_path().read_bytes(), contenu)   # lecture seule
                self.assertIn("config.json illisible", self.journal())

    def test_json_valide_mais_pas_un_objet(self):
        """Une liste, un nombre ou null à la place de l'objet attendu : valeurs par défaut."""
        for contenu in (b"null", b"[1, 2, 3]", b'"texte"', b"42", b"true", b'[{"url": "x"}]'):
            with self.subTest(contenu=contenu):
                paths.config_path().write_bytes(contenu)
                self.assertEqual(config.load_config(), config.DEFAULT_CONFIG)

    def test_config_remplacee_par_un_dossier(self):
        """Si « config.json » est un dossier, on démarre quand même avec les défauts."""
        paths.config_path().mkdir()
        self.assertEqual(config.load_config(), config.DEFAULT_CONFIG)

    def test_enregistrement_et_relecture_avec_accents(self):
        """Aller-retour fidèle des accents, guillemets, barres obliques et autres écritures."""
        cfg = config.load_config()
        cfg.update({
            "url": "http://127.0.0.1:1/rutorrent",
            "username": "Zoé",
            "password": 'p@ss"w\\ord é€ 日本',
            "last_dir": "/home/zoé/téléchargements/Séries été",
            "last_label": "Séries été",
            "favorite_dirs": ["/home/zoé/films à voir", "D:\\Vidéos\\Été"],
            "favorite_labels": ["Films à voir", "日本語", ""],
            "timeout": 15,
            "verify_ssl": False,
            "cache_time": "11/11/2026 09:30",
        })
        self.assertIsNone(config.save_config(cfg))
        self.assertEqual(config.load_config(), cfg)
        brut = paths.config_path().read_bytes()
        self.assertEqual(json.loads(brut.decode("utf-8")), cfg)
        # UTF-8 lisible dans le fichier, pas des séquences \u00e9
        self.assertIn("Séries été".encode("utf-8"), brut)
        self.assertIn("日本語".encode("utf-8"), brut)
        self.assertNotIn(b"\\u00e9", brut)

    def test_enregistrement_atomique_sans_fichier_temporaire(self):
        """Après l'enregistrement il ne reste que config.json : aucun .tmp, aucune erreur."""
        config.save_config(dict(config.DEFAULT_CONFIG, url="http://127.0.0.1:1/rutorrent"))
        self.assertEqual(self._fichiers(), ["config.json"])
        config.save_config(dict(config.DEFAULT_CONFIG, url="http://127.0.0.1:2/rutorrent"))
        self.assertEqual(self._fichiers(), ["config.json"])
        self.assertEqual(config.load_config()["url"], "http://127.0.0.1:2/rutorrent")

    def test_enregistrement_remplace_tout_le_fichier(self):
        """Le nouveau contenu remplace entièrement l'ancien, même plus court."""
        config.save_config({"url": "http://127.0.0.1:1/" + "x" * 5000, "username": "long"})
        config.save_config({"url": "court"})
        self.assertEqual(json.loads(paths.config_path().read_text(encoding="utf-8")),
                         {"url": "court"})

    def test_fichier_temporaire_residuel(self):
        """Un .tmp laissé par un arrêt brutal ne gêne pas l'enregistrement suivant."""
        residu = self.donnees / "config.json.tmp"
        residu.write_bytes(b'{"url": "moitie ecri')
        config.save_config({"url": "http://127.0.0.1:1/rutorrent"})
        self.assertEqual(self._fichiers(), ["config.json"])
        self.assertEqual(config.load_config()["url"], "http://127.0.0.1:1/rutorrent")

    def test_residu_temporaire_ignore_a_la_lecture(self):
        """Un .tmp à moitié écrit n'est jamais lu à la place du vrai fichier."""
        h.write_config(url="http://127.0.0.1:1/bon")
        (self.donnees / "config.json.tmp").write_bytes(b'{"url": "http://127.0.0.1:1/mauvais"')
        self.assertEqual(config.load_config()["url"], "http://127.0.0.1:1/bon")

    def test_echec_de_serialisation_preserve_l_ancien_fichier(self):
        """Une valeur impossible à écrire : pas d'exception, ancien fichier intact, erreur journalisée."""
        config.save_config({"url": "http://127.0.0.1:1/rutorrent", "username": "zoé"})
        avant = paths.config_path().read_bytes()
        self.assertIsNone(config.save_config({"url": "nouvelle", "objet": object()}))
        self.assertEqual(paths.config_path().read_bytes(), avant)
        self.assertEqual(self._fichiers(), ["config.json", "erreurs.log"])
        self.assertIn("enregistrement de la configuration", self.journal())

    def test_echec_au_remplacement_preserve_l_ancien_fichier(self):
        """Coupure au moment de remplacer le fichier : l'ancien reste entier, le .tmp est retiré."""
        config.save_config({"url": "http://127.0.0.1:1/ancien"})
        avant = paths.config_path().read_bytes()
        with mock.patch.object(config.os, "replace", side_effect=PermissionError("verrouillé")):
            self.assertIsNone(config.save_config({"url": "http://127.0.0.1:1/nouveau"}))
        self.assertEqual(paths.config_path().read_bytes(), avant)
        self.assertEqual(self._fichiers(), ["config.json", "erreurs.log"])
        self.assertIn("enregistrement de la configuration", self.journal())
        self.assertIn("PermissionError", self.journal())
        # et l'enregistrement suivant refonctionne
        config.save_config({"url": "http://127.0.0.1:1/nouveau"})
        self.assertEqual(config.load_config()["url"], "http://127.0.0.1:1/nouveau")

    def test_premier_enregistrement_en_echec_ne_laisse_rien(self):
        """Sans fichier préalable, un échec ne crée ni config.json ni .tmp."""
        with mock.patch.object(config.os, "replace", side_effect=OSError("disque plein")):
            config.save_config({"url": "http://127.0.0.1:1/rutorrent"})
        self.assertEqual(self._fichiers(), ["erreurs.log"])
        self.assertEqual(config.load_config(), config.DEFAULT_CONFIG)

    def test_bom_utf8_tolere(self):
        """Corrigé — un config.json enregistré en « UTF-8 avec BOM » (Bloc-notes,
        PowerShell 5) est pris pour un fichier corrompu : identifiants ignorés."""
        contenu = json.dumps({"url": "http://127.0.0.1:1/rutorrent", "username": "zoé"},
                             ensure_ascii=False).encode("utf-8")
        paths.config_path().write_bytes(b"\xef\xbb\xbf" + contenu)
        cfg = config.load_config()
        self.assertEqual(cfg["url"], "http://127.0.0.1:1/rutorrent")
        self.assertEqual(cfg["username"], "zoé")

    def test_valeur_de_mauvais_type_ecartee(self):
        """Corrigé — aucune vérification de type ; « "favorite_dirs": null » est repris
        tel quel et fait planter l'ouverture de la fenêtre (TypeError dans _refresh_combos)."""
        h.write_config(url="http://127.0.0.1:1/rutorrent", favorite_dirs=None,
                       favorite_labels="Films", timeout="abc", last_dir=None)
        cfg = config.load_config()
        self.assertEqual(cfg["url"], "http://127.0.0.1:1/rutorrent")
        self.assertIsInstance(cfg["favorite_dirs"], list)
        self.assertIsInstance(cfg["favorite_labels"], list)
        self.assertIsInstance(cfg["timeout"], int)
        self.assertIsInstance(cfg["last_dir"], str)

    def test_listes_par_defaut_non_partagees(self):
        """Corrigé — copie superficielle, les listes rendues par load_config()
        sont les objets mêmes de DEFAULT_CONFIG ; un .append() polluerait les défauts."""
        cfg = config.load_config()
        self.assertEqual(cfg["favorite_dirs"], [])
        for cle in ("favorite_dirs", "favorite_labels", "cache_labels", "cache_dirs"):
            self.assertIsNot(cfg[cle], config.DEFAULT_CONFIG[cle], cle)


# --------------------------------------------------------------------------- #
#  main.parse_argv
# --------------------------------------------------------------------------- #

class LigneDeCommande(unittest.TestCase):

    def test_aucun_argument(self):
        """Lancement sans argument : ni option ni cible."""
        self.assertEqual(tmain.parse_argv([]), ([], []))

    def test_options_separees_des_cibles(self):
        """Ce qui commence par « -- » est une option, le reste une cible, dans l'ordre."""
        options, cibles = tmain.parse_argv(["a.torrent", "--status", "b.torrent", "--version"])
        self.assertEqual(options, ["--status", "--version"])
        self.assertEqual(cibles, ["a.torrent", "b.torrent"])

    def test_options_en_minuscules_cibles_intactes(self):
        """Les options sont normalisées en minuscules ; la casse des cibles est conservée."""
        options, cibles = tmain.parse_argv(["--REGISTER", "C:\\Téléchargements\\Mon Film.TORRENT",
                                            "--Status"])
        self.assertEqual(options, ["--register", "--status"])
        self.assertEqual(cibles, ["C:\\Téléchargements\\Mon Film.TORRENT"])

    def test_liste_d_origine_non_modifiee(self):
        """parse_argv ne modifie pas la liste reçue et accepte un tuple."""
        arguments = ["--status", MAGNET + "&dn=Mon", "Film"]
        copie = list(arguments)
        tmain.parse_argv(arguments)
        self.assertEqual(arguments, copie)
        self.assertEqual(tmain.parse_argv(tuple(arguments)), tmain.parse_argv(arguments))

    def test_magnet_seul(self):
        """Un magnet arrivé entier reste tel quel."""
        self.assertEqual(tmain.parse_argv([MAGNET + "&dn=Nom"]), ([], [MAGNET + "&dn=Nom"]))

    def test_magnet_decoupe_par_le_shell_est_recolle(self):
        """« magnet:?...&dn=Mon Film » arrivé en plusieurs arguments redevient un seul lien."""
        options, cibles = tmain.parse_argv([MAGNET + "&dn=Mon", "Film", "de", "Vacances", "2024"])
        self.assertEqual(options, [])
        self.assertEqual(cibles, [MAGNET + "&dn=Mon Film de Vacances 2024"])
        infos = tf.parse_magnet(cibles[0])
        self.assertEqual(infos["name"], "Mon Film de Vacances 2024")
        self.assertEqual(infos["infohash"], HEX40.upper())

    def test_magnet_recolle_malgre_la_casse_et_les_espaces(self):
        """« MAGNET: » en majuscules ou précédé d'espaces est reconnu et recollé."""
        for debut in ("MAGNET:?xt=urn:btih:" + HEX40, "  " + MAGNET, "Magnet:?xt=urn:btih:" + HEX40):
            with self.subTest(debut=debut):
                options, cibles = tmain.parse_argv([debut + "&dn=Mon", "Film"])
                self.assertEqual(options, [])
                self.assertEqual(len(cibles), 1)
                self.assertEqual(tf.parse_magnet(cibles[0])["name"], "Mon Film")

    def test_options_retirees_avant_de_recoller(self):
        """Une option placée avant le magnet ne l'empêche pas d'être recollé."""
        options, cibles = tmain.parse_argv(["--Status", MAGNET + "&dn=Mon", "Film"])
        self.assertEqual(options, ["--status"])
        self.assertEqual(cibles, [MAGNET + "&dn=Mon Film"])

    def test_fichiers_jamais_recolles(self):
        """Un fichier n'est jamais joint à l'argument suivant, même s'il contient des espaces."""
        arguments = ["C:\\Mes Torrents\\Un Film.torrent", "autre.torrent", "troisieme.torrent"]
        self.assertEqual(tmain.parse_argv(arguments), ([], arguments))

    def test_fichier_puis_magnet_non_recolles(self):
        """Un fichier suivi d'un magnet : deux cibles distinctes."""
        arguments = ["a.torrent", MAGNET + "&dn=Nom"]
        self.assertEqual(tmain.parse_argv(arguments), ([], arguments))

    def test_adresse_http_non_recollee(self):
        """Une adresse http(s) n'est pas un magnet : elle n'absorbe pas la suite."""
        arguments = ["https://exemple.test/a.torrent", "b.torrent"]
        self.assertEqual(tmain.parse_argv(arguments), ([], arguments))

    def test_nom_de_fichier_ressemblant_a_magnet(self):
        """« magnet.torrent » ou « magnetique.torrent » sont des fichiers, pas des liens."""
        for premier in ("magnet.torrent", "magnetique.torrent", "magnet", "magnets:liste.torrent",
                        "C:\\magnet:\\x.torrent"):
            with self.subTest(premier=premier):
                arguments = [premier, "b.torrent"]
                self.assertEqual(tmain.parse_argv(arguments), ([], arguments))

    def test_deux_magnets_restent_deux_cibles(self):
        """Corrigé — deux magnets sur la ligne de commande sont collés en un seul lien,
        que parse_magnet refuse ensuite — les deux sont perdus."""
        _, cibles = tmain.parse_argv([MAGNET, AUTRE_MAGNET])
        self.assertEqual(cibles, [MAGNET, AUTRE_MAGNET])

    def test_magnet_puis_fichier_ne_l_avale_pas(self):
        """Corrigé — un .torrent placé après un magnet est absorbé dans le nom du magnet
        et n'est jamais ajouté."""
        _, cibles = tmain.parse_argv([MAGNET + "&dn=Mon", "Film", "C:\\dl\\autre.torrent"])
        self.assertIn("C:\\dl\\autre.torrent", cibles)
        self.assertEqual(tf.parse_magnet(cibles[0])["name"], "Mon Film")

    def test_option_courte_h_reconnue(self):
        """« -h » est une option (l'aide), pas une cible."""
        options, cibles = tmain.parse_argv(["-h"])
        self.assertIn("--help", options)
        self.assertEqual(cibles, [])


# --------------------------------------------------------------------------- #
#  main.Sink
# --------------------------------------------------------------------------- #

class TamponDeDemarrage(unittest.TestCase):
    """Sink : ce que le service local reçoit avant que la fenêtre n'existe."""

    def setUp(self):
        self.sink = tmain.Sink()
        self.evenements = []

    def _elements(self, entrees):
        self.evenements.append(("elements", list(entrees)))

    def _afficher(self):
        self.evenements.append(("afficher",))

    def test_elements_gardes_jusqu_a_attach(self):
        """Les éléments reçus avant attach() sont remis en une fois, dans l'ordre d'arrivée."""
        self.sink.items(["a.torrent"])
        self.sink.items([MAGNET, "b.torrent"])
        self.assertEqual(self.evenements, [])
        self.sink.attach(self._elements, self._afficher)
        self.assertEqual(self.evenements, [("elements", ["a.torrent", MAGNET, "b.torrent"])])

    def test_demande_d_affichage_gardee_jusqu_a_attach(self):
        """Une demande d'affichage reçue trop tôt est honorée dès attach()."""
        self.sink.show()
        self.assertEqual(self.evenements, [])
        self.sink.attach(self._elements, self._afficher)
        self.assertEqual(self.evenements, [("afficher",)])

    def test_plusieurs_demandes_d_affichage_une_seule_execution(self):
        """Dix demandes en attente ne font passer la fenêtre au premier plan qu'une fois."""
        for _ in range(10):
            self.sink.show()
        self.sink.attach(self._elements, self._afficher)
        self.assertEqual(self.evenements, [("afficher",)])

    def test_elements_puis_affichage(self):
        """À l'attachement, les éléments en attente sont remis avant l'affichage."""
        self.sink.show()
        self.sink.items(["a.torrent"])
        self.sink.attach(self._elements, self._afficher)
        self.assertEqual(self.evenements, [("elements", ["a.torrent"]), ("afficher",)])

    def test_rien_en_attente_rien_n_est_appele(self):
        """Sans rien en attente, attach() n'appelle aucun des deux rappels."""
        self.sink.attach(self._elements, self._afficher)
        self.assertEqual(self.evenements, [])

    def test_lot_vide_en_attente_ignore(self):
        """Un lot vide reçu avant attach() ne déclenche pas de remise vide."""
        self.sink.items([])
        self.sink.attach(self._elements, self._afficher)
        self.assertEqual(self.evenements, [])

    def test_apres_attach_transmission_directe(self):
        """Une fois attaché, chaque lot et chaque demande sont transmis immédiatement."""
        self.sink.attach(self._elements, self._afficher)
        self.sink.items(["a.torrent"])
        self.assertEqual(self.evenements, [("elements", ["a.torrent"])])
        self.sink.show()
        self.sink.items(["b.torrent", "c.torrent"])
        self.sink.show()
        self.assertEqual(self.evenements, [("elements", ["a.torrent"]), ("afficher",),
                                           ("elements", ["b.torrent", "c.torrent"]),
                                           ("afficher",)])

    def test_lot_transmis_tel_quel_apres_attach(self):
        """Après attach(), le rappel reçoit l'objet même qui a été déposé."""
        recus = []
        self.sink.attach(recus.append, self._afficher)
        lot = ["a.torrent", {"path": "b.torrent", "consume": True}]
        self.sink.items(lot)
        self.assertEqual(len(recus), 1)
        self.assertIs(recus[0], lot)

    def test_tampon_vide_apres_attach(self):
        """Ce qui a été remis ne l'est pas une seconde fois (ni éléments, ni affichage)."""
        self.sink.items(["a.torrent"])
        self.sink.show()
        self.sink.attach(self._elements, self._afficher)
        self.assertEqual(len(self.evenements), 2)
        seconds = []
        self.sink.attach(lambda entrees: seconds.append(("elements", list(entrees))),
                         lambda: seconds.append(("afficher",)))
        self.assertEqual(seconds, [])
        self.sink.items(["b.torrent"])
        self.sink.show()
        self.assertEqual(seconds, [("elements", ["b.torrent"]), ("afficher",)])
        self.assertEqual(len(self.evenements), 2)         # les anciens rappels ne servent plus

    def test_iterables_quelconques_avant_attach(self):
        """Tuples et générateurs sont acceptés en attente comme les listes."""
        self.sink.items(("a.torrent",))
        self.sink.items(nom for nom in ("b.torrent", "c.torrent"))
        self.sink.attach(self._elements, self._afficher)
        self.assertEqual(self.evenements,
                         [("elements", ["a.torrent", "b.torrent", "c.torrent"])])

    def test_lot_en_attente_independant_de_la_liste_d_origine(self):
        """Modifier la liste d'origine après le dépôt ne change pas ce qui sera remis."""
        lot = ["a.torrent"]
        self.sink.items(lot)
        lot.append("ajouté après coup")
        lot.clear()
        self.sink.attach(self._elements, self._afficher)
        self.assertEqual(self.evenements, [("elements", ["a.torrent"])])

    def test_tampons_independants(self):
        """Deux Sink ne partagent rien."""
        autre = tmain.Sink()
        self.sink.items(["a.torrent"])
        self.sink.show()
        recus = []
        autre.attach(recus.append, lambda: recus.append("afficher"))
        self.assertEqual(recus, [])


if __name__ == "__main__":
    unittest.main()
