# -*- coding: utf-8 -*-
"""Tests de la fenêtre principale (tuploader/ui/app.py, classe App).

Couvre aussi, en « fumée », les fenêtres secondaires (ui/dialogs.py) et les
composants graphiques (ui/widgets.py).

Règles suivies par ce module :

* tout passe par le bac à sable de tests/helpers.py (importé en premier) ;
* la seule seedbox contactée est la fausse seedbox locale (FakeSeedbox) ;
* aucune boucle mainloop() ni wait_window() réelle : on fait tourner la boucle
  d'événements à la main (pump / pump_until) ;
* le presse-papiers réel n'est jamais lu ni écrit (clipboard_get est remplacé) ;
* le registre n'est jamais lu : le module winintegration est remplacé par un
  bouchon dans les tests des fenêtres secondaires ;
* la vérification d'un envoi (App._confirm) est accélérée : budget et
  intervalle réduits, mêmes règles de décision.

Les défauts du produit révélés à l'écriture de ces tests ont été corrigés ; les tests
qui les mettaient en évidence vérifient désormais le comportement corrigé. (TestDefautsConnus)
"""

from tests import helpers as h          # EN PREMIER : redirige les données vers le bac à sable

import base64
import os
import queue
import threading
import time
import tkinter as tk
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from tuploader import paths
from tuploader.config import load_config
from tuploader.rutorrent import RuTorrentClient
from tuploader.service import ask_running_instance, materialize, start_local_server
from tuploader.torrentfile import human_size, parse_torrent
from tuploader.ui import app as app_mod
from tuploader.ui import dialogs as dialogs_mod
from tuploader.ui.app import (App, ST_FAILED, ST_OK, ST_OK_AFTER_ERROR, ST_OK_REPAIRED,
                              ST_PENDING, ST_SENDING, ST_SENT, ST_SENT_REPAIRED)
from tuploader.ui.dialogs import IntegrationDialog, SettingsDialog
from tuploader.ui.theme import ICONS, THEME_CHOICES, Theme
from tuploader.ui.widgets import CheckBox, DropZone, FlatButton, StatusPill

# --------------------------------------------------------------------------- #
#  Outils propres à ce module
# --------------------------------------------------------------------------- #

BOX: h.FakeSeedbox | None = None
TORRENTS_INITIAUX: list = []

MAGNET_HASH = "0123456789ABCDEF0123456789ABCDEF01234567"
URL_1 = "http://127.0.0.1:9/telechargement/Mon%20Film.torrent"
URL_2 = "http://127.0.0.1:9/telechargement/Autre%20Film.torrent"

# Ce que la fausse seedbox contient par défaut, vu par l'application.
LABELS_SERVEUR = ["Films", "Séries"]
DIRS_SERVEUR = ["/home/u/downloads/rtorrent",
                "/home/u/downloads/rtorrent/films",
                "/home/u/downloads/rtorrent/series"]

# Vérification d'envoi accélérée : mêmes décisions, attentes raccourcies.
_CONFIRM_REEL = App._confirm
BUDGET_RAPIDE = 0.25
INTERVALLE_RAPIDE = 0.02


def confirm_rapide(client, infohash, budget=15.0, interval=2.0):
    return _CONFIRM_REEL(client, infohash, budget=min(budget, BUDGET_RAPIDE),
                         interval=INTERVALLE_RAPIDE)


def setUpModule():
    global BOX, TORRENTS_INITIAUX
    BOX = h.FakeSeedbox()
    TORRENTS_INITIAUX = list(BOX.torrents)


def tearDownModule():
    BOX.stop()


def magnet(infohash: str = MAGNET_HASH, nom: str = "Magnet de test") -> str:
    return "magnet:?xt=urn:btih:%s&dn=%s" % (infohash, urllib.parse.quote_plus(nom))


def base32(infohash: str) -> str:
    return base64.b32encode(bytes.fromhex(infohash)).decode("ascii")


def empreinte(path) -> str:
    return parse_torrent(Path(path))["infohash"]


def vieillir(path, secondes: float = 120.0) -> None:
    """Recule la date de modification (le dossier surveillé exige un fichier « posé »)."""
    t = time.time() - secondes
    os.utime(path, (t, t))


def valeurs(combo) -> list:
    return list(combo.tk.splitlist(combo.cget("values")))


def textes(widget) -> list:
    """Textes de tous les Label contenus (récursivement) dans `widget`."""
    out = []
    for child in widget.winfo_children():
        try:
            if child.winfo_class() == "Label":
                out.append(str(child.cget("text")))
        except tk.TclError:
            pass
        out.extend(textes(child))
    return out


def relances(journal) -> list:
    """Entrées de root.after() correspondant à des relances (≥ 1 s)."""
    return [e for e in journal if isinstance(e[0], int) and e[0] >= 1000]


class ToursScriptes:
    """Remplace l'Event d'arrêt du dossier surveillé : chaque wait() joue l'étape
    suivante puis laisse le tour se faire ; quand il n'y a plus d'étape, arrêt.

    Le balayage tourne alors dans le thread du test, sans aucune attente réelle.
    """

    def __init__(self, *etapes):
        self.etapes = list(etapes)
        self.intervalles = []
        self.arrete = False

    def wait(self, timeout=None):
        self.intervalles.append(timeout)
        if not self.etapes:
            self.arrete = True
            return True
        self.etapes.pop(0)()
        return False

    def is_set(self):
        return self.arrete


def vider_boite(app) -> None:
    """Exécute ce que les threads ont déposé dans la boîte de l'application."""
    while True:
        try:
            fn = app._inbox.get_nowait()
        except queue.Empty:
            return
        fn()


class BaseApp(h.TkTestCase):
    """Fournit creer_app() et attend la fin des threads avant de tout fermer."""

    def setUp(self):
        super().setUp()
        BOX.reset()
        BOX.torrents = list(TORRENTS_INITIAUX)
        self.apps: list[App] = []
        for patcher in (mock.patch.object(App, "_confirm", staticmethod(confirm_rapide)),
                        # nouveaux essais du client immédiats : seul compte ici ce
                        # que l'application fait de l'échec final
                        mock.patch.object(RuTorrentClient, "RETRY_DELAYS", (0.0, 0.0, 0.0))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        try:
            for app in self.apps:
                if app._closing:
                    continue
                self.pump_until(lambda a=app: not a.busy and not a._refreshing, 15)
                try:
                    app.on_close()
                except tk.TclError:
                    pass
        finally:
            super().tearDown()

    # -- fabrication ---------------------------------------------------------- #

    def creer_app(self, initial_items=(), server=None, notice="", **cfg) -> App:
        values = dict(url=BOX.url, username="u", password="p", auto_refresh=False,
                      ipc_port=h.free_port(), theme="light")
        values.update(cfg)
        h.write_config(**values)
        app = App(self.root, initial_items, server=server, notice=notice)
        self.apps.append(app)
        return app

    def faux_reglages(self, modifs):
        """Remplace SettingsDialog par un faux dont `.result` vaut cfg + modifs (ou None)."""
        journal = []

        class FauxReglages:
            def __init__(self, parent, theme, cfg):
                journal.append(dict(cfg))
                self.result = None if modifs is None else dict(cfg, **modifs)

        patcher = mock.patch.object(app_mod, "SettingsDialog", FauxReglages)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root.wait_window = lambda *a, **k: None       # jamais de boucle modale
        return journal

    def journal_after(self, app) -> list:
        """Consigne chaque root.after(ms, fn) de l'application : (ms, fn, identifiant)."""
        journal = []
        vrai = app.root.after

        def after(ms, func=None, *args):
            job = vrai(ms, func, *args)
            journal.append((ms, func, job))
            return job

        app.root.after = after
        return journal

    # -- attentes ------------------------------------------------------------- #

    def attendre_envoi(self, app, timeout: float = 15.0):
        self.assertTrue(self.pump_until(lambda: not app.busy, timeout), "envoi non terminé")
        self.pump(0.05)

    def attendre_actualisation(self, app, timeout: float = 10.0):
        self.assertTrue(self.pump_until(lambda: not app._refreshing, timeout),
                        "actualisation non terminée")
        self.pump(0.05)

    def envoyer(self, app):
        app.send_all()
        self.assertTrue(app.busy)
        self.attendre_envoi(app)

    # -- lecture de la liste ---------------------------------------------------- #

    @staticmethod
    def lignes(app, col: str = "name") -> list:
        return [app.tree.set(iid, col) for iid in app.tree.get_children()]

    @staticmethod
    def tags(app, iid) -> tuple:
        return tuple(app.tree.tk.splitlist(app.tree.item(iid, "tags")))

    def boites(self) -> list:
        return [d[0] for d in self.dialogs]


# --------------------------------------------------------------------------- #
#  File d'attente
# --------------------------------------------------------------------------- #

class TestFileAttente(BaseApp):

    def test_ordre_d_arrivee_conserve_pour_une_selection_mixte(self):
        """Fichiers, magnet et adresse mélangés s'affichent dans l'ordre déposé."""
        app = self.creer_app()
        d = h.temp_dir()
        f1 = h.make_torrent(d, "Premier")
        f2 = h.make_torrent(d, "Dernier", size=9000, files=3)
        n = app.add_items([str(f1), magnet(nom="Au milieu"), URL_1, str(f2)])
        self.assertEqual(n, 4)
        self.assertEqual(self.lignes(app), ["Premier", "Au milieu", "Mon Film.torrent", "Dernier"])
        self.assertEqual(self.lignes(app, "type"), ["Fichier", "Magnet", "Lien", "Fichier"])
        kinds = [app.items[i]["kind"] for i in app.tree.get_children()]
        self.assertEqual(kinds, ["file", "magnet", "url", "file"])

    def test_valeurs_des_colonnes(self):
        """Taille et nombre de fichiers pour un fichier ; un tiret pour magnet et lien."""
        app = self.creer_app()
        d = h.temp_dir()
        f1 = h.make_torrent(d, "Unique", size=3_145_728)
        f2 = h.make_torrent(d, "Multi", size=3000, files=3)
        app.add_items([str(f1), str(f2), magnet(nom="Mag"), URL_1])
        rows = [tuple(str(v) for v in app.tree.item(i, "values")) for i in app.tree.get_children()]
        self.assertEqual(rows[0], ("Unique", "Fichier", human_size(3_145_728), "1", ST_PENDING))
        self.assertEqual(rows[1], ("Multi", "Fichier", human_size(3000), "3", ST_PENDING))
        self.assertEqual(rows[2], ("Mag", "Magnet", "—", "—", ST_PENDING))
        self.assertEqual(rows[3], ("Mon Film.torrent", "Lien", "—", "—", ST_PENDING))
        for item in app.items.values():
            self.assertEqual(item["status"], "pending")
            self.assertEqual(item["text"], ST_PENDING)

    def test_nom_d_une_adresse_sans_chemin(self):
        """Une adresse sans nom de fichier prend l'adresse elle-même comme nom."""
        app = self.creer_app()
        self.assertTrue(app.add_url("http://127.0.0.1:9/"))
        iid = app.tree.get_children()[0]
        self.assertEqual(app.tree.set(iid, "name"), "http://127.0.0.1:9/")
        self.assertEqual(app.items[iid]["info"]["infohash"], "")

    def test_doublon_par_empreinte_fichier_puis_magnet(self):
        """Un magnet (hex minuscule ou base32) du même torrent qu'un fichier est refusé."""
        app = self.creer_app()
        f = h.make_torrent(h.temp_dir(), "Doublon")
        H = empreinte(f)
        n = app.add_items([str(f), magnet(H.lower(), "Même"), magnet(base32(H), "Encore")])
        self.assertEqual(n, 1)
        self.assertEqual(len(app.items), 1)
        self.assertEqual(self.lignes(app, "type"), ["Fichier"])

    def test_doublon_par_empreinte_magnet_puis_fichier(self):
        app = self.creer_app()
        f = h.make_torrent(h.temp_dir(), "Doublon")
        self.assertEqual(app.add_items([magnet(empreinte(f))]), 1)
        self.assertEqual(app.add_items([str(f)]), 0)
        self.assertEqual(app.add_files([f]), 0)
        self.assertIn("1", app.var_status.get())          # « 1 déjà présent(s) »
        self.assertEqual(self.lignes(app, "type"), ["Magnet"])

    def test_meme_fichier_deux_fois_meme_avec_une_autre_casse(self):
        app = self.creer_app()
        f = h.make_torrent(h.temp_dir(), "Casse")
        self.assertEqual(app.add_files([f, f, Path(str(f).upper())]), 1)
        self.assertEqual(len(app.items), 1)

    def test_meme_adresse_refusee_deux_adresses_differentes_acceptees(self):
        """L'empreinte vide d'une adresse ne sert jamais de clé d'égalité."""
        app = self.creer_app()
        self.assertEqual(app.add_items([URL_1, URL_2]), 2)
        self.assertEqual(app.add_items([URL_1]), 0)
        self.assertFalse(app.add_url(URL_2))
        self.assertEqual(len(app.items), 2)
        self.assertFalse(app._already_queued(""))
        self.assertTrue(app.add_magnet(magnet()))
        self.assertEqual(len(app.items), 3)

    def test_magnet_en_double_ou_invalide(self):
        app = self.creer_app()
        self.assertTrue(app.add_magnet(magnet()))
        self.assertFalse(app.add_magnet(magnet(MAGNET_HASH.lower(), "Autre nom")))
        self.assertEqual(len(app.items), 1)
        self.assertFalse(app.add_magnet("magnet:?dn=sans-empreinte", announce=False))
        self.assertEqual(self.dialogs, [])
        self.assertFalse(app.add_magnet("magnet:?dn=sans-empreinte", announce=True))
        self.assertEqual(self.boites(), ["showwarning"])
        self.assertEqual(len(app.items), 1)

    def test_un_dossier_ajoute_ses_torrents(self):
        """Un dossier ajoute ses .torrent (triés, sans descendre dans les sous-dossiers)."""
        app = self.creer_app()
        d = h.temp_dir()
        h.make_torrent(d, "B")
        h.make_torrent(d, "A")
        (d / "notes.txt").write_text("rien", encoding="utf-8")
        h.make_torrent(d / "sous-dossier", "Cache")
        self.assertEqual(app.add_items([str(d)]), 2)
        self.assertEqual(self.lignes(app), ["A", "B"])
        self.assertEqual(app.add_files([d]), 0)            # tout est déjà là

    def test_fichier_corrompu_sans_annonce_jamais_de_fenetre(self):
        """Import automatique : un fichier illisible n'ouvre aucune boîte modale."""
        app = self.creer_app()
        d = h.temp_dir()
        casse = d / "Casse.torrent"
        casse.write_bytes(b"<html>pas un torrent</html>")
        absent = d / "Absent.torrent"
        self.assertEqual(app.add_files([casse, absent], announce=False), 0)
        self.assertEqual(self.dialogs, [])
        self.assertIn("Casse.torrent", app.var_status.get())
        journal = paths.log_path().read_text(encoding="utf-8")
        self.assertIn("Casse.torrent", journal)
        self.assertIn("Absent.torrent", journal)

    def test_fichier_corrompu_avec_annonce_avertit(self):
        app = self.creer_app()
        d = h.temp_dir()
        casse = d / "Casse.torrent"
        casse.write_bytes(b"d4:infoi1e")
        bon = h.make_torrent(d, "Bon")
        self.assertEqual(app.add_files([casse, bon], announce=True), 1)
        self.assertEqual(self.boites(), ["showwarning"])
        self.assertIn("Casse.torrent", self.dialogs[0][1][1])
        self.assertEqual(self.lignes(app), ["Bon"])

    def test_add_items_n_ouvre_jamais_de_fenetre(self):
        """Le point d'entrée unique reste silencieux, même sur des entrées invalides."""
        app = self.creer_app()
        d = h.temp_dir()
        casse = d / "Casse.torrent"
        casse.write_bytes(b"\x00\x01")
        texte = d / "texte.txt"
        texte.write_text("x", encoding="utf-8")
        n = app.add_items([str(casse), str(d / "Absent.torrent"), str(texte),
                           "magnet:?xt=urn:btih:zz", "", "   ", '"{}"'])
        self.assertEqual(n, 0)
        self.assertEqual(self.dialogs, [])
        self.assertEqual(app.items, {})

    def test_compteur_et_message_de_liste_vide(self):
        app = self.creer_app()
        self.assertEqual(app.lbl_count.cget("text"), "")
        self.assertEqual(app.lbl_empty.winfo_manager(), "place")
        app.add_items([magnet()])
        self.assertEqual(app.lbl_count.cget("text"), "1 torrent")
        self.assertEqual(app.lbl_empty.winfo_manager(), "")
        app.add_items([URL_1, URL_2])
        self.assertEqual(app.lbl_count.cget("text"), "3 torrents")
        iid = app.tree.get_children()[0]
        app.items[iid]["status"] = "ok"
        app._set_status(iid, ST_OK, "ok")
        text = app.lbl_count.cget("text")
        self.assertTrue(text.startswith("3 torrents"), text)
        self.assertIn("2", text.replace("3 torrents", ""))
        app.clear_list()
        self.assertEqual(app.lbl_count.cget("text"), "")
        self.assertEqual(app.lbl_empty.winfo_manager(), "place")

    def test_lignes_alternees_et_couleur_d_etat(self):
        app = self.creer_app()
        app.add_items([magnet(), URL_1, URL_2])
        a, b, c = app.tree.get_children()
        self.assertEqual([self.tags(app, i) for i in (a, b, c)], [(), ("odd",), ()])
        app._set_status(b, ST_FAILED + "test", "err")
        self.assertEqual(self.tags(app, b), ("odd", "err"))
        self.assertEqual(app.tree.set(b, "status"), ST_FAILED + "test")
        app._set_status("ligne-inexistante", "rien")           # ignoré sans erreur

    def test_retirer_la_selection(self):
        app = self.creer_app()
        app.add_items([magnet(), URL_1, URL_2])
        a, b, c = app.tree.get_children()
        app.tree.selection_set((a, c))
        app.remove_selected()
        self.assertEqual(app.tree.get_children(), (b,))
        self.assertEqual(set(app.items), {b})
        self.assertEqual(self.tags(app, b), ())               # rerayée : première ligne
        self.assertIn("2", app.var_status.get())
        self.assertEqual(app.lbl_count.cget("text"), "1 torrent")
        app.set_status("inchangé")
        app.tree.selection_set(())
        app.remove_selected()                                  # rien de sélectionné
        self.assertEqual(app.var_status.get(), "inchangé")
        self.assertEqual(set(app.items), {b})

    def test_vider_la_liste(self):
        app = self.creer_app()
        app.add_items([magnet(), URL_1])
        app.progress.configure(value=2, maximum=2)
        app.clear_list()
        self.assertEqual(app.items, {})
        self.assertEqual(app.tree.get_children(), ())
        self.assertEqual(float(app.progress.cget("value")), 0.0)
        self.assertEqual(app.lbl_empty.winfo_manager(), "place")

    def test_elements_initiaux_et_message_d_accueil(self):
        f = h.make_torrent(h.temp_dir(), "Initial")
        app = self.creer_app(initial_items=[str(f), magnet()], notice="Bienvenue")
        self.assertEqual(self.lignes(app), ["Initial", "Magnet de test"])
        self.assertEqual(app.var_status.get(), "Bienvenue")

    def test_detail_d_une_ligne(self):
        """Double-clic : le détail complet (chemin, empreinte, état) s'affiche."""
        app = self.creer_app()
        f = h.make_torrent(h.temp_dir(), "Detail")
        app.add_files([f])
        iid = app.tree.get_children()[0]
        app.tree.identify_row = lambda y: iid
        app._show_row_detail(types.SimpleNamespace(y=5))
        self.assertEqual(self.boites(), ["showinfo"])
        message = self.dialogs[0][1][1]
        self.assertIn("Detail", message)
        self.assertIn(str(f), message)
        self.assertIn(empreinte(f), message)
        self.assertIn(ST_PENDING, message)
        app.tree.identify_row = lambda y: ""
        app._show_row_detail(types.SimpleNamespace(y=500))     # hors ligne : rien
        self.assertEqual(len(self.dialogs), 1)


# --------------------------------------------------------------------------- #
#  Presse-papiers, glisser-déposer, sélection de fichiers
# --------------------------------------------------------------------------- #

class TestPressePapiersEtDepot(BaseApp):

    def coller(self, app, texte):
        """Colle `texte` sans jamais toucher au presse-papiers réel (None = vide)."""
        def faux_presse_papiers():
            if texte is None:
                raise tk.TclError("CLIPBOARD selection doesn't exist or form \"STRING\" not defined")
            return texte
        app.root.clipboard_get = faux_presse_papiers
        app.paste_clipboard()

    def test_coller_magnet_adresse_et_chemin_existant(self):
        app = self.creer_app()
        f = h.make_torrent(h.temp_dir(), "Colle")
        for cas, texte, attendu in (
                ("magnet entouré d'espaces", "  %s  " % magnet(), ["Magnet"]),
                ("adresse", URL_1, ["Lien"]),
                ("chemin entre guillemets", '"%s"\r\n' % f, ["Fichier"]),
                ("plusieurs lignes", "%s\r\n%s\r\n\r\n%s" % (magnet(nom="Un"), URL_1, f),
                 ["Magnet", "Lien", "Fichier"])):
            with self.subTest(cas):
                app.clear_list()
                self.coller(app, texte)
                self.assertEqual(self.lignes(app, "type"), attendu)
        self.assertEqual(self.lignes(app), ["Un", "Mon Film.torrent", "Colle"])   # ordre gardé
        self.assertEqual(self.dialogs, [])

    def test_coller_rien_de_reconnaissable(self):
        app = self.creer_app()
        d = h.temp_dir()
        texte = d / "lisezmoi.txt"
        texte.write_text("x", encoding="utf-8")
        for cas, contenu in (
                ("texte quelconque", "bonjour\nC:\\nulle\\part\\x.torrent\n"
                                     "ftp://127.0.0.1/x.torrent\nmagnet:sans-point-interrogation\n"
                                     "%s" % texte),
                ("presse-papiers vide", None)):
            with self.subTest(cas):
                app.set_status("Prêt.")
                self.coller(app, contenu)
                self.assertEqual(app.items, {})
                self.assertNotEqual(app.var_status.get(), "Prêt.")
        self.assertEqual(self.dialogs, [])

    def test_ctrl_v_ne_colle_pas_dans_un_champ_de_saisie(self):
        app = self.creer_app()
        appels = []
        app.paste_clipboard = lambda: appels.append(1)
        event = types.SimpleNamespace(widget=app.cmb_dir)
        self.assertIsNone(app._on_paste_key(event))
        self.assertEqual(appels, [])
        event.widget = app.tree
        self.assertEqual(app._on_paste_key(event), "break")
        self.assertEqual(appels, [1])
        autre = tk.Toplevel(self.root)
        autre.withdraw()
        event.widget = tk.Label(autre)
        self.assertIsNone(app._on_paste_key(event))            # autre fenêtre
        event.widget = object()
        self.assertIsNone(app._on_paste_key(event))            # widget sans winfo_class
        self.assertEqual(appels, [1])

    def test_depot_de_chemins_avec_espaces(self):
        """Un dépôt de fichiers arrive en liste Tcl : accolades autour des espaces."""
        app = self.creer_app()
        d = h.temp_dir()
        f1 = h.make_torrent(d / "dossier avec espace", "Nom Long")
        f2 = h.make_torrent(d, "Court")
        data = "{%s} %s" % (f1.as_posix(), f2.as_posix())
        flashs = []
        app.drop_zone.flash = lambda: flashs.append(1)          # pas de minuterie orpheline
        app._on_drop(types.SimpleNamespace(data=data))
        self.assertEqual(self.lignes(app), ["Nom Long", "Court"])
        self.assertEqual(flashs, [1])

    def test_depot_de_liens(self):
        app = self.creer_app()
        app.drop_zone.flash = lambda: None
        app._on_drop(types.SimpleNamespace(data="%s\n%s" % (magnet(nom="Glisse"), URL_1)))
        self.assertEqual(self.lignes(app, "type"), ["Magnet", "Lien"])

    def test_zone_de_depot_ouvre_le_choix_de_fichiers(self):
        app = self.creer_app()
        f = h.make_torrent(h.temp_dir(), "Choisi")
        with mock.patch.object(app_mod.filedialog, "askopenfilenames",
                               return_value=(str(f),)) as choix:
            app.drop_zone.command()
        choix.assert_called_once()
        self.assertEqual(self.lignes(app), ["Choisi"])
        with mock.patch.object(app_mod.filedialog, "askopenfilenames", return_value=()):
            app.browse_files()                                  # annulation : rien
        self.assertEqual(len(app.items), 1)


# --------------------------------------------------------------------------- #
#  Favoris et listes en cache
# --------------------------------------------------------------------------- #

class TestFavorisEtCache(BaseApp):

    def test_favori_persiste_et_passe_en_tete(self):
        app = self.creer_app(cache_dirs=["/b", "/a"], cache_labels=["Films"])
        app.var_dir.set("  /z/favori  ")
        app.btn_fav_dir.invoke()
        self.assertEqual(load_config()["favorite_dirs"], ["/z/favori"])
        self.assertEqual(valeurs(app.cmb_dir), ["/z/favori", "/b", "/a"])
        app.var_dir.set("/z/favori")
        if app.theme.has_icons:
            self.assertEqual(app.btn_fav_dir._icon.cget("text"), ICONS["star_fill"])
        else:
            self.assertEqual(app.btn_fav_dir._label.cget("text"), "★")
        # un favori déjà proposé par le serveur n'apparaît qu'une fois, en tête
        app.var_label.set("Films")
        app.btn_fav_label.invoke()
        self.assertEqual(load_config()["favorite_labels"], ["Films"])
        self.assertEqual(valeurs(app.cmb_label), ["Films"])
        # second clic : retiré
        app.btn_fav_dir.invoke()
        self.assertEqual(load_config()["favorite_dirs"], [])
        self.assertEqual(valeurs(app.cmb_dir), ["/b", "/a"])
        if app.theme.has_icons:
            self.assertEqual(app.btn_fav_dir._icon.cget("text"), ICONS["star"])

    def test_favori_vide_refuse(self):
        app = self.creer_app(favorite_dirs=["/garde"])
        app.var_dir.set("   ")
        app.btn_fav_dir.invoke()
        self.assertEqual(load_config()["favorite_dirs"], ["/garde"])
        self.assertNotEqual(app.var_status.get(), "Prêt.")

    def test_listes_en_cache_affichees_avant_toute_requete(self):
        app = self.creer_app(cache_labels=["Séries", ""], cache_dirs=["/data/films", ""],
                             cache_time="01/01/2026 10:00", favorite_dirs=["/fav"],
                             last_dir="/data/films", last_label="Séries")
        self.assertEqual(valeurs(app.cmb_label), ["Séries"])
        self.assertEqual(valeurs(app.cmb_dir), ["/fav", "/data/films"])
        self.assertEqual(app.var_dir.get(), "/data/films")
        self.assertEqual(app.var_label.get(), "Séries")
        self.pump(0.3)
        self.assertEqual(BOX.calls, [])

    def test_cache_aux_valeurs_douteuses_filtre(self):
        """L'application écarte elle-même les entrées vides ou non textuelles du cache."""
        app = self.creer_app()
        app.cfg["cache_labels"] = ["A", 5, None, ""]
        app.cfg["cache_dirs"] = None
        app._apply_config()
        self.assertEqual(valeurs(app.cmb_label), ["A"])
        self.assertEqual(valeurs(app.cmb_dir), [])

    def test_options_port_et_bandeau_repris_de_la_configuration(self):
        port = h.free_port()
        app = self.creer_app(start_torrents=False, clear_after_upload=True, ipc_port=port)
        self.assertFalse(app.var_start.get())
        self.assertTrue(app.var_clear.get())
        self.assertEqual(app.port, port)
        self.assertEqual(app._host(), "127.0.0.1")
        self.assertIn("127.0.0.1", app.pill._label.cget("text"))


# --------------------------------------------------------------------------- #
#  Lecture des étiquettes et des dossiers
# --------------------------------------------------------------------------- #

class TestActualisation(BaseApp):

    def test_actualisation_reussie(self):
        app = self.creer_app(favorite_dirs=["/fav"])
        app.refresh_server()
        self.assertTrue(app._refreshing)
        self.assertFalse(app.btn_refresh.enabled)
        self.assertEqual(app._link_state[1], "busy")
        self.attendre_actualisation(app)
        self.assertEqual(valeurs(app.cmb_label), LABELS_SERVEUR)
        self.assertEqual(valeurs(app.cmb_dir), ["/fav"] + DIRS_SERVEUR)
        cfg = load_config()
        self.assertEqual(cfg["cache_labels"], LABELS_SERVEUR)
        self.assertEqual(cfg["cache_dirs"], DIRS_SERVEUR)
        self.assertRegex(cfg["cache_time"], r"^\d\d/\d\d/\d{4} \d\d:\d\d$")
        self.assertEqual(app._link_state, ("connectée", "ok"))
        self.assertIn("connectée", app.pill._label.cget("text"))
        self.assertIn("127.0.0.1", app.pill._label.cget("text"))
        self.assertTrue(app.btn_refresh.enabled)
        self.assertIsNone(app._auto_retry_job)
        self.assertEqual(self.dialogs, [])

    def test_actualisation_automatique_au_demarrage(self):
        app = self.creer_app(auto_refresh=True)
        self.assertEqual(BOX.calls, [])                        # rien de synchrone
        self.assertTrue(self.pump_until(lambda: app._link_state[1] == "ok", 5))
        self.attendre_actualisation(app)
        self.assertEqual(valeurs(app.cmb_label), LABELS_SERVEUR)

    def test_echec_persistant_garde_les_listes_sans_html(self):
        app = self.creer_app(cache_labels=["Ancienne"], cache_dirs=["/ancien"],
                             cache_time="01/01/2026 10:00")
        BOX.raw_failures = 10 ** 6                             # page HTML 500 à chaque appel
        journal = self.journal_after(app)
        app.refresh_server()
        self.attendre_actualisation(app)
        status = app.var_status.get()
        self.assertNotIn("<", status)
        self.assertNotIn("Ultra.cc", status)
        self.assertNotIn("DOCTYPE", status)
        self.assertNotIn("<", app.pill._label.cget("text"))
        self.assertIn("01/01/2026 10:00", status)              # date du relevé conservé
        self.assertEqual(valeurs(app.cmb_label), ["Ancienne"])
        self.assertEqual(valeurs(app.cmb_dir), ["/ancien"])
        cfg = load_config()
        self.assertEqual(cfg["cache_labels"], ["Ancienne"])
        self.assertEqual(cfg["cache_dirs"], ["/ancien"])
        self.assertEqual(app._link_state[1], "warn")
        self.assertIsNotNone(app._auto_retry_job)
        self.assertEqual([e[0] for e in relances(journal)], [App.AUTO_RETRY_DELAYS[0] * 1000])
        self.assertTrue(app.btn_refresh.enabled)
        self.assertEqual(self.dialogs, [])

    def test_relances_automatiques_suivent_auto_retry_delays_puis_cessent(self):
        app = self.creer_app(cache_labels=["L"], cache_dirs=["/d"])
        BOX.raw_failures = 10 ** 6
        journal = self.journal_after(app)
        app.refresh_server()
        self.attendre_actualisation(app)
        attendus = [d * 1000 for d in App.AUTO_RETRY_DELAYS]
        for rang in range(1, len(attendus) + 1):
            planifiees = relances(journal)
            self.assertEqual([e[0] for e in planifiees], attendus[:rang])
            _ms, relance, job = planifiees[-1]
            self.assertEqual(app._auto_retry_job, job)
            app.root.after_cancel(job)                        # on « déclenche » sans attendre
            relance()
            self.assertTrue(app._refreshing)
            self.attendre_actualisation(app)
        self.assertEqual(len(relances(journal)), len(attendus))   # plus rien après la dernière
        self.assertEqual(app._auto_retry_index, len(attendus))
        self.assertEqual(valeurs(app.cmb_label), ["L"])

    def test_actualisation_manuelle_relance_la_sequence(self):
        app = self.creer_app(cache_labels=["L"])
        BOX.raw_failures = 10 ** 6
        journal = self.journal_after(app)
        app.refresh_server()
        self.attendre_actualisation(app)
        _ms, relance, job = relances(journal)[-1]
        app.root.after_cancel(job)
        relance()
        self.attendre_actualisation(app)
        self.assertEqual([e[0] for e in relances(journal)],
                         [d * 1000 for d in App.AUTO_RETRY_DELAYS[:2]])
        en_attente = app._auto_retry_job
        app.refresh_server()                                   # clic manuel
        self.assertNotIn(en_attente, app.root.tk.splitlist(app.root.tk.call("after", "info")))
        self.assertEqual(app._auto_retry_index, 0)
        self.attendre_actualisation(app)
        self.assertEqual(relances(journal)[-1][0], App.AUTO_RETRY_DELAYS[0] * 1000)
        self.assertEqual(app._auto_retry_index, 1)

    def test_succes_apres_echec_annule_la_relance(self):
        app = self.creer_app()
        BOX.raw_failures = 10 ** 6
        journal = self.journal_after(app)
        app.refresh_server()
        self.attendre_actualisation(app)
        _ms, relance, job = relances(journal)[-1]
        BOX.raw_failures = 0
        app.root.after_cancel(job)
        relance()
        self.attendre_actualisation(app)
        self.assertIsNone(app._auto_retry_job)
        self.assertEqual(app._auto_retry_index, 0)
        self.assertEqual(app._link_state, ("connectée", "ok"))
        self.assertEqual(valeurs(app.cmb_label), LABELS_SERVEUR)

    def test_refus_definitif_sans_relance(self):
        """Mauvais mot de passe : pas de relance automatique, listes conservées."""
        app = self.creer_app(cache_labels=["Ancienne"])
        BOX.reset("unauth")
        journal = self.journal_after(app)
        app.refresh_server()
        self.attendre_actualisation(app)
        self.assertEqual(app._link_state[1], "error")
        self.assertIsNone(app._auto_retry_job)
        self.assertEqual(relances(journal), [])
        self.assertEqual(valeurs(app.cmb_label), ["Ancienne"])

    def test_reponse_vide_garde_le_cache(self):
        """rTorrent qui redémarre liste 0 torrent : les listes connues restent."""
        app = self.creer_app(cache_labels=["Ancienne"], cache_dirs=["/ancien"],
                             cache_time="01/01/2026 10:00")
        BOX.torrents = []
        app.refresh_server()
        self.attendre_actualisation(app)
        self.assertEqual(valeurs(app.cmb_label), ["Ancienne"])
        self.assertEqual(valeurs(app.cmb_dir), ["/ancien"])
        cfg = load_config()
        self.assertEqual(cfg["cache_labels"], ["Ancienne"])
        self.assertEqual(cfg["cache_dirs"], ["/ancien"])
        self.assertEqual(cfg["cache_time"], "01/01/2026 10:00")
        self.assertEqual(app._link_state, ("connectée", "ok"))

    def test_reponse_vide_sans_cache_vide_les_listes(self):
        app = self.creer_app()
        BOX.torrents = []
        app.refresh_server()
        self.attendre_actualisation(app)
        self.assertEqual(valeurs(app.cmb_label), [])
        self.assertEqual(load_config()["cache_labels"], [])

    def test_actualisation_auto_differee_pendant_un_envoi(self):
        app = self.creer_app()
        journal = self.journal_after(app)
        app.busy = True
        try:
            app.refresh_server(silent=True, auto=True)
            self.assertFalse(app._refreshing)
            self.assertIsNotNone(app._auto_retry_job)
            self.assertIn(10000, [e[0] for e in journal])
            self.pump(0.3)
            self.assertEqual(BOX.calls, [])
        finally:
            app.busy = False

    def test_double_clic_sur_actualiser_une_seule_requete(self):
        app = self.creer_app()
        app.refresh_server()
        app.refresh_server()
        self.attendre_actualisation(app)
        self.assertEqual(BOX.calls.count("raw:d.multicall2"), 1)

    def test_sans_adresse_rien_n_est_demande(self):
        app = self.creer_app()
        app.cfg["url"] = ""
        app.refresh_server(silent=True)
        self.assertEqual(self.dialogs, [])
        app.refresh_server()
        self.assertEqual(self.boites(), ["showinfo"])
        self.assertFalse(app._refreshing)
        self.pump(0.2)
        self.assertEqual(BOX.calls, [])


# --------------------------------------------------------------------------- #
#  Paramètres (même chemin de code que le bouton « Paramètres »)
# --------------------------------------------------------------------------- #

class TestParametres(BaseApp):

    def test_changer_d_adresse_vide_le_cache(self):
        box2 = h.FakeSeedbox()
        self.addCleanup(box2.stop)
        box2.torrents = [("DDDD", "Autre", "/data/autre", "a.bin", 0)]
        app = self.creer_app(cache_labels=["Ancienne"], cache_dirs=["/ancien"],
                             cache_time="01/01/2026 10:00", favorite_dirs=["/fav"])
        journal = self.faux_reglages({"url": box2.url})
        app.open_settings()
        self.assertEqual(len(journal), 1)
        # tout de suite : les listes de l'ancienne seedbox ont disparu
        self.assertEqual(app.cfg["cache_labels"], [])
        self.assertEqual(app.cfg["cache_dirs"], [])
        self.assertEqual(app.cfg["cache_time"], "")
        self.assertEqual(valeurs(app.cmb_label), [])
        self.assertEqual(valeurs(app.cmb_dir), ["/fav"])
        saved = load_config()
        self.assertEqual(saved["url"], box2.url)
        self.assertEqual(saved["cache_labels"], [])
        self.assertTrue(app._refreshing)                       # relecture sur la nouvelle
        self.attendre_actualisation(app)
        self.assertEqual(valeurs(app.cmb_label), ["Autre"])
        self.assertIn("/data/autre", valeurs(app.cmb_dir))
        self.assertIn("raw:d.multicall2", box2.calls)
        self.assertEqual(BOX.calls, [])                        # l'ancienne n'est pas contactée

    def test_meme_adresse_garde_le_cache(self):
        app = self.creer_app(cache_labels=["Ancienne"], cache_dirs=["/ancien"])
        BOX.raw_failures = 10 ** 6                             # la relecture échoue
        self.faux_reglages({"timeout": 30})
        app.open_settings()
        self.assertEqual(app.cfg["cache_labels"], ["Ancienne"])
        self.assertEqual(load_config()["timeout"], 30)
        self.attendre_actualisation(app)
        self.assertEqual(valeurs(app.cmb_label), ["Ancienne"])

    def test_annuler_ne_change_rien(self):
        app = self.creer_app(cache_labels=["Ancienne"])
        avant = paths.config_path().read_text(encoding="utf-8")
        self.faux_reglages(None)
        app.open_settings()
        self.assertFalse(app._refreshing)
        self.assertEqual(paths.config_path().read_text(encoding="utf-8"), avant)
        self.pump(0.2)
        self.assertEqual(BOX.calls, [])

    def test_changer_de_theme_reconstruit_sans_rien_perdre(self):
        app = self.creer_app(theme="light", cache_labels=["Cache"], cache_dirs=["/cache"])
        d = h.temp_dir()
        app.add_items([str(h.make_torrent(d, "Un")), str(h.make_torrent(d, "Deux")), magnet()])
        premier = app.tree.get_children()[0]
        app.items[premier]["status"] = "ok"
        app._set_status(premier, ST_OK, "ok")
        app.var_dir.set("/dest/x")
        app.var_label.set("Étiquette")
        app.var_start.set(False)
        ancien_arbre = app.tree
        self.faux_reglages({"theme": "dark"})
        with mock.patch.object(app, "_rebuild_ui", wraps=app._rebuild_ui) as espion:
            app.open_settings()
        espion.assert_called_once_with()
        self.assertFalse(ancien_arbre.winfo_exists())
        self.assertTrue(app.theme.dark)
        self.assertEqual(self.lignes(app), ["Un", "Deux", "Magnet de test"])
        self.assertEqual(self.lignes(app, "status"), [ST_OK, ST_PENDING, ST_PENDING])
        self.assertEqual([app.items[i]["status"] for i in app.tree.get_children()],
                         ["ok", "pending", "pending"])
        self.assertIn("ok", self.tags(app, app.tree.get_children()[0]))
        self.assertEqual(app.var_dir.get(), "/dest/x")
        self.assertEqual(app.var_label.get(), "Étiquette")
        self.assertFalse(app.var_start.get())
        self.assertEqual(valeurs(app.cmb_label), ["Cache"])
        self.assertEqual(load_config()["theme"], "dark")
        self.attendre_actualisation(app)

    def test_reconstruction_directe_garde_l_etat(self):
        app = self.creer_app(theme="light")
        app.add_items([magnet(), URL_1])
        app._refresh_combos(server_labels=["Mémoire"], server_dirs=["/memoire"])
        app._set_link("connectée", "ok")
        app.set_status("Message conservé")
        app.cfg["theme"] = "dark"
        app._rebuild_ui()
        self.assertEqual(self.lignes(app, "type"), ["Magnet", "Lien"])
        self.assertEqual(valeurs(app.cmb_label), ["Mémoire"])
        self.assertEqual(valeurs(app.cmb_dir), ["/memoire"])
        self.assertIn("connectée", app.pill._label.cget("text"))
        self.assertEqual(app.var_status.get(), "Message conservé")
        self.assertEqual(app.lbl_count.cget("text"), "2 torrents")
        self.assertTrue(app.btn_refresh.enabled)

    def test_changer_de_theme_pendant_un_envoi_est_differe(self):
        app = self.creer_app(theme="light")
        self.faux_reglages({"theme": "dark"})
        app.busy = True
        try:
            with mock.patch.object(app, "_rebuild_ui") as espion:
                app.open_settings()
            espion.assert_not_called()
            self.assertFalse(app.theme.dark)
            self.assertEqual(load_config()["theme"], "dark")
            self.attendre_actualisation(app)
        finally:
            app.busy = False

    def test_sans_adresse_les_parametres_s_ouvrent_seuls(self):
        journal = self.faux_reglages(None)
        app = self.creer_app(url="")
        self.assertEqual(journal, [])
        self.assertTrue(self.pump_until(lambda: len(journal) == 1, 3))
        self.assertEqual(app._host(), "")
        self.pump(0.2)
        self.assertEqual(BOX.calls, [])

    def test_envoyer_sans_adresse_ouvre_les_parametres(self):
        journal = self.faux_reglages(None)
        app = self.creer_app()
        app.cfg["url"] = ""
        app.add_items([magnet()])
        app.send_all()
        self.assertEqual(self.boites(), ["showinfo"])
        self.assertEqual(len(journal), 1)
        self.assertFalse(app.busy)


# --------------------------------------------------------------------------- #
#  Envoi vers la seedbox
# --------------------------------------------------------------------------- #

class TestEnvoi(BaseApp):

    def requetes_ajout(self):
        return [r for r in BOX.requests if r["kind"] == "add"]

    def test_envoi_normal_confirme(self):
        app = self.creer_app()
        f = h.make_torrent(h.temp_dir(), "Normal")
        BOX.added_hash = empreinte(f)
        app.add_files([f])
        iid = app.tree.get_children()[0]
        app.var_dir.set("/home/u/downloads/rtorrent/films")
        app.var_label.set("Séries")
        app.var_start.set(False)
        self.envoyer(app)
        self.assertEqual(app.tree.set(iid, "status"), ST_OK)
        self.assertEqual(app.items[iid]["status"], "ok")
        self.assertIn("ok", self.tags(app, iid))
        self.assertEqual(BOX.calls.count("add"), 1)
        self.assertNotIn("GET getplugins.php", BOX.calls)
        self.assertEqual(self.dialogs, [])
        body = self.requetes_ajout()[0]["body"]
        self.assertIn(b'name="dir_edit"\r\n\r\n/home/u/downloads/rtorrent/films', body)
        self.assertIn('name="label"\r\n\r\nSéries'.encode("utf-8"), body)
        self.assertIn(b'name="torrents_start_stopped"', body)
        self.assertEqual(float(app.progress.cget("value")), 1.0)
        self.assertTrue(app.btn_send.enabled)
        self.assertTrue(app.btn_refresh.enabled)
        cfg = load_config()
        self.assertEqual(cfg["last_dir"], "/home/u/downloads/rtorrent/films")
        self.assertEqual(cfg["last_label"], "Séries")
        self.assertFalse(cfg["start_torrents"])

    def test_etat_pendant_l_envoi(self):
        app = self.creer_app()
        app.add_items([magnet()])
        BOX.present = {MAGNET_HASH}
        BOX.slow = 0.15
        iid = app.tree.get_children()[0]
        app.send_all()
        self.assertFalse(app.btn_send.enabled)
        self.assertFalse(app.btn_refresh.enabled)
        self.assertTrue(self.pump_until(lambda: app.tree.set(iid, "status") == ST_SENDING, 3))
        app.send_all()                                         # second clic ignoré
        self.attendre_envoi(app)
        self.assertEqual(app.tree.set(iid, "status"), ST_OK)
        self.assertEqual(BOX.calls.count("add"), 1)

    def test_erreur_500_mais_torrent_arrive(self):
        """Le 500 survient après la prise en charge : confirmé, et jamais renvoyé."""
        app = self.creer_app()
        f = h.make_torrent(h.temp_dir(), "Arrive")
        BOX.added_hash = empreinte(f)
        BOX.add_plan = ["500"]
        app.add_files([f])
        iid = app.tree.get_children()[0]
        self.envoyer(app)
        self.assertEqual(app.tree.set(iid, "status"), ST_OK_AFTER_ERROR)
        self.assertEqual(app.items[iid]["status"], "ok")
        self.assertEqual(BOX.calls.count("add"), 1)
        self.assertNotIn("GET getplugins.php", BOX.calls)
        self.assertEqual(self.dialogs, [])

    def _reparation_qui_fait_aboutir(self):
        vraie = RuTorrentClient.repair_settings_cache

        def reparer(client):
            ok = vraie(client)
            BOX.land_on_add = True          # après réparation, l'ajout atteint rTorrent
            return ok
        return mock.patch.object(RuTorrentClient, "repair_settings_cache", reparer)

    def test_cache_empoisonne_repare_puis_renvoye(self):
        BOX.reset("poisoned")
        app = self.creer_app()
        f = h.make_torrent(h.temp_dir(), "Empoisonne")
        BOX.added_hash = empreinte(f)
        BOX.land_on_add = False             # le premier ajout n'arrive pas
        app.add_files([f])
        iid = app.tree.get_children()[0]
        with self._reparation_qui_fait_aboutir():
            self.envoyer(app)
        self.assertEqual(app.tree.set(iid, "status"), ST_OK_REPAIRED)
        self.assertEqual(BOX.calls.count("GET getplugins.php"), 1)
        self.assertEqual(BOX.calls.count("add"), 2)
        self.assertIn("raw:system.client_version", BOX.calls)
        dernier_ajout = len(BOX.calls) - 1 - BOX.calls[::-1].index("add")
        self.assertLess(BOX.calls.index("GET getplugins.php"), dernier_ajout)
        self.assertEqual(self.dialogs, [])

    def test_cache_empoisonne_repare_sans_confirmation(self):
        BOX.reset("poisoned")
        app = self.creer_app()
        f = h.make_torrent(h.temp_dir(), "SansConfirmation")
        BOX.added_hash = empreinte(f)
        BOX.land_on_add = False
        app.add_files([f])
        iid = app.tree.get_children()[0]
        self.envoyer(app)
        self.assertEqual(app.tree.set(iid, "status"), ST_SENT_REPAIRED)
        self.assertEqual(app.items[iid]["status"], "ok")
        self.assertEqual(BOX.calls.count("GET getplugins.php"), 1)
        self.assertEqual(BOX.calls.count("add"), 2)

    def test_un_seul_renvoi_apres_reparation(self):
        """Si le renvoi échoue encore, on s'arrête : pas de troisième ajout."""
        app = self.creer_app()
        f = h.make_torrent(h.temp_dir(), "Tetu")
        BOX.added_hash = empreinte(f)
        BOX.land_on_add = False
        BOX.add_plan = ["500", "500"]
        app.add_files([f])
        iid = app.tree.get_children()[0]
        self.envoyer(app)
        status = app.tree.set(iid, "status")
        self.assertTrue(status.startswith(ST_FAILED), status)
        self.assertEqual(BOX.calls.count("add"), 2)
        self.assertEqual(BOX.calls.count("GET getplugins.php"), 1)
        self.assertEqual(self.boites(), ["showwarning"])

    def test_rtorrent_injoignable_echec_a_reessayer(self):
        BOX.reset("dead")
        app = self.creer_app()
        f = h.make_torrent(h.temp_dir(), "Mort")
        BOX.added_hash = empreinte(f)
        app.add_files([f])
        iid = app.tree.get_children()[0]
        self.envoyer(app)
        status = app.tree.set(iid, "status")
        self.assertTrue(status.startswith(ST_FAILED), status)
        self.assertIn("réessayez", status)
        self.assertNotIn("<", status)
        self.assertEqual(app.items[iid]["status"], "error")
        self.assertIn("err", self.tags(app, iid))
        self.assertNotIn("GET getplugins.php", BOX.calls)
        self.assertEqual(BOX.calls.count("add"), 1)
        self.assertEqual(self.boites(), ["showwarning"])
        self.assertTrue(app.btn_send.enabled)

    def test_refus_definitif_sans_verification(self):
        """ruTorrent refuse le torrent : échec immédiat, ni vérification ni renvoi."""
        app = self.creer_app()
        f = h.make_torrent(h.temp_dir(), "Refuse")
        BOX.add_plan = ["error"]
        app.add_files([f])
        iid = app.tree.get_children()[0]
        self.envoyer(app)
        status = app.tree.set(iid, "status")
        self.assertTrue(status.startswith(ST_FAILED), status)
        self.assertNotIn("réessayez", status)
        self.assertNotIn("raw:d.multicall2", BOX.calls)
        self.assertEqual(BOX.calls.count("add"), 1)

    def test_fichier_disparu_avant_l_envoi(self):
        app = self.creer_app()
        f = h.make_torrent(h.temp_dir(), "Disparu")
        app.add_files([f])
        f.unlink()
        iid = app.tree.get_children()[0]
        self.envoyer(app)
        self.assertTrue(app.tree.set(iid, "status").startswith(ST_FAILED))
        self.assertEqual(BOX.calls.count("add"), 0)
        self.assertEqual(self.boites(), ["showwarning"])

    def test_aiguillage_multipart_ou_formulaire(self):
        """Fichier : multipart ; magnet et adresse : formulaire url-encodé."""
        app = self.creer_app()
        f = h.make_torrent(h.temp_dir(), "Aiguillage")
        BOX.present = {empreinte(f), MAGNET_HASH}
        app.add_items([str(f), magnet(), URL_1])
        a, b, c = app.tree.get_children()
        self.envoyer(app)
        ajouts = self.requetes_ajout()
        self.assertEqual(len(ajouts), 3)
        types_contenu = [r["headers"]["content-type"] for r in ajouts]
        self.assertTrue(types_contenu[0].startswith("multipart/form-data"), types_contenu[0])
        for ct in types_contenu[1:]:
            self.assertTrue(ct.startswith("application/x-www-form-urlencoded"), ct)
        self.assertEqual(urllib.parse.parse_qs(ajouts[1]["body"].decode())["url"], [magnet()])
        self.assertEqual(urllib.parse.parse_qs(ajouts[2]["body"].decode())["url"], [URL_1])
        self.assertIn(b'filename="Aiguillage.torrent"', ajouts[0]["body"])
        self.assertEqual([app.tree.set(i, "status") for i in (a, b, c)], [ST_OK, ST_OK, ST_SENT])
        self.assertEqual(float(app.progress.cget("value")), 3.0)

    def test_adresse_envoyee_sans_confirmation(self):
        """Une adresse n'a pas d'empreinte : aucune vérification n'est tentée."""
        app = self.creer_app()
        app.add_items([URL_1])
        iid = app.tree.get_children()[0]
        self.envoyer(app)
        self.assertEqual(app.tree.set(iid, "status"), ST_SENT)
        self.assertEqual(app.items[iid]["status"], "ok")
        self.assertNotIn("raw:d.multicall2", BOX.calls)

    def test_rien_a_envoyer(self):
        app = self.creer_app()
        app.send_all()
        self.assertFalse(app.busy)
        app.add_items([magnet()])
        iid = app.tree.get_children()[0]
        app.items[iid]["status"] = "ok"
        app.send_all()
        self.assertFalse(app.busy)
        self.pump(0.2)
        self.assertEqual(BOX.calls, [])

    def test_echecs_renvoyes_au_clic_suivant(self):
        app = self.creer_app()
        app.add_items([URL_1])
        BOX.add_plan = ["error"]
        self.envoyer(app)
        iid = app.tree.get_children()[0]
        self.assertEqual(app.items[iid]["status"], "error")
        self.envoyer(app)
        self.assertEqual(app.tree.set(iid, "status"), ST_SENT)
        self.assertEqual(BOX.calls.count("add"), 2)

    def test_vider_apres_envoi_reussi(self):
        app = self.creer_app(clear_after_upload=True)
        f = h.make_torrent(h.temp_dir(), "AVider")
        BOX.added_hash = empreinte(f)
        app.add_files([f])
        self.envoyer(app)
        self.assertEqual(app.items, {})
        self.assertEqual(app.tree.get_children(), ())
        self.assertTrue(load_config()["clear_after_upload"])

    def test_pas_de_vidage_si_un_envoi_echoue(self):
        app = self.creer_app(clear_after_upload=True)
        app.add_items([URL_1, URL_2])
        BOX.add_plan = ["error", ""]
        self.envoyer(app)
        self.assertEqual(len(app.items), 2)

    def test_dossier_et_etiquette_utilises_proposes_ensuite(self):
        app = self.creer_app(cache_dirs=["/existant"], cache_labels=["Existante"])
        app.add_items([URL_1])
        app.var_dir.set("/home/u/nouveau dossier")
        app.var_label.set("Nouvelle étiquette")
        self.envoyer(app)
        self.assertEqual(valeurs(app.cmb_dir), ["/existant", "/home/u/nouveau dossier"])
        self.assertEqual(valeurs(app.cmb_label), ["Existante", "Nouvelle étiquette"])

    def test_dossier_d_un_envoi_rate_non_propose(self):
        app = self.creer_app()
        app.add_items([URL_1])
        app.var_dir.set("/home/u/rate")
        BOX.add_plan = ["error"]
        self.envoyer(app)
        self.assertNotIn("/home/u/rate", valeurs(app.cmb_dir))

    def test_copie_recue_supprimee_apres_envoi_fichier_normal_conserve(self):
        app = self.creer_app()
        origine = h.make_torrent(h.temp_dir(), "Navigateur")
        copie = Path(materialize({"path": str(origine), "consume": True}))
        self.assertEqual(copie.resolve().parent, paths.inbox_dir().resolve())
        normal = h.make_torrent(h.temp_dir(), "Normal")
        BOX.present = {empreinte(origine), empreinte(normal)}
        app.add_items([str(copie), str(normal)])
        self.envoyer(app)
        self.assertEqual(self.lignes(app, "status"), [ST_OK, ST_OK])
        self.assertFalse(copie.exists())
        self.assertTrue(normal.exists())
        self.assertTrue(origine.exists())

    def test_copie_recue_conservee_si_l_envoi_echoue(self):
        app = self.creer_app()
        copie = h.make_torrent(paths.inbox_dir(), "CopieRatee")
        self.addCleanup(lambda: copie.unlink(missing_ok=True))
        normal = h.make_torrent(h.temp_dir(), "Reussi")
        BOX.present = {empreinte(normal)}
        BOX.add_plan = ["error", ""]
        app.add_items([str(copie), str(normal)])
        self.envoyer(app)
        self.assertTrue(copie.exists())
        self.assertTrue(normal.exists())


# --------------------------------------------------------------------------- #
#  Vérification après un ajout (App._confirm, appelée directement)
# --------------------------------------------------------------------------- #

class TestConfirmation(unittest.TestCase):

    def setUp(self):
        BOX.reset()
        BOX.torrents = list(TORRENTS_INITIAUX)

    def test_torrent_present(self):
        BOX.present = {MAGNET_HASH}
        debut = time.time()
        self.assertTrue(_CONFIRM_REEL(BOX.client(), MAGNET_HASH.lower(), budget=2, interval=0.02))
        self.assertLess(time.time() - debut, 1.0)

    def test_torrent_absent_dans_le_budget(self):
        debut = time.time()
        self.assertFalse(_CONFIRM_REEL(BOX.client(), MAGNET_HASH, budget=0.3, interval=0.05))
        duree = time.time() - debut
        self.assertGreaterEqual(duree, 0.3)
        self.assertLess(duree, 2.0)

    def test_echec_passager_ne_clot_pas_la_verification(self):
        BOX.present = {MAGNET_HASH}
        BOX.raw_failures = 3
        self.assertTrue(_CONFIRM_REEL(BOX.client(), MAGNET_HASH, budget=2, interval=0.02))
        self.assertGreaterEqual(BOX.calls.count("raw:d.multicall2"), 4)

    def test_refus_definitif_arrete_aussitot(self):
        BOX.reset("unauth")
        debut = time.time()
        self.assertFalse(_CONFIRM_REEL(BOX.client(), MAGNET_HASH, budget=5, interval=0.02))
        self.assertLess(time.time() - debut, 1.5)
        self.assertEqual(BOX.calls.count("raw:d.multicall2"), 1)

    def test_exception_inattendue(self):
        class ClientCasse:
            def has_infohash(self, infohash):
                raise ValueError("inattendu")
        self.assertFalse(_CONFIRM_REEL(ClientCasse(), MAGNET_HASH, budget=5, interval=0.01))


# --------------------------------------------------------------------------- #
#  Dossier surveillé
# --------------------------------------------------------------------------- #

class TestSurveillance(BaseApp):

    def demarrer(self, dossier, **cfg):
        app = self.creer_app(watch_folder=str(dossier), watch_interval=1, **cfg)
        self.premier_plan = []
        app.bring_to_front = lambda: self.premier_plan.append(1)
        app.cfg["watch_downloads"] = True
        app.restart_watcher()
        self.assertIsNotNone(app._watch_thread)
        self.assertTrue(app._watch_thread.is_alive())
        self.pump(0.3)                                         # amorçage du thread
        return app

    def test_thread_reel_importe_le_nouveau_puis_s_arrete(self):
        """De bout en bout : thread, dépôt dans la boîte, pompe du thread principal."""
        dossier = h.temp_dir("veille-")
        vieillir(h.make_torrent(dossier, "Preexistant", size=1000))
        app = self.demarrer(dossier)
        thread = app._watch_thread
        vieillir(h.make_torrent(dossier, "Nouveau", size=2000))
        self.assertTrue(self.pump_until(lambda: len(app.items) == 1, 4))
        self.assertEqual(self.lignes(app), ["Nouveau"])
        self.assertEqual(self.dialogs, [])
        self.assertTrue(self.premier_plan)
        app.cfg["watch_downloads"] = False
        app.restart_watcher()
        self.assertIsNone(app._watch_thread)
        thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_tours_scriptes_ignorent_existant_crdownload_et_incomplet(self):
        """Fichiers déjà là, .crdownload, page HTML et fichier tout juste écrit sont ignorés."""
        dossier = h.temp_dir("veille-")
        vieillir(h.make_torrent(dossier, "Preexistant", size=1000))
        app = self.creer_app(watch_folder=str(dossier))
        app.bring_to_front = lambda: None
        partiel = dossier / "Nouveau.torrent.crdownload"
        abandon = dossier / "Abandon.torrent.crdownload"
        casse = dossier / "Casse.torrent"
        recent = dossier / "Recent.torrent"
        vus = []

        def tour1():
            for chemin, nom in ((partiel, "Nouveau"), (abandon, "Abandon")):
                chemin.write_bytes(h.torrent_bytes(nom, 2000))
                vieillir(chemin)
            casse.write_bytes(b"<html>page d'erreur</html>")
            vieillir(casse)

        def tour2():
            vider_boite(app)
            vus.append(self.lignes(app))
            partiel.rename(dossier / "Nouveau.torrent")
            recent.write_bytes(h.torrent_bytes("Recent", 3000))
            vieillir(recent, -60)                  # date future : « en cours d'écriture »

        def tour3():
            vider_boite(app)
            vus.append(self.lignes(app))
            vieillir(recent)

        def tour4():
            vider_boite(app)
            vus.append(self.lignes(app))

        arret = ToursScriptes(tour1, tour2, tour3, tour4)
        app._watch_loop(dossier, arret, 7)
        vider_boite(app)
        self.assertEqual(vus, [[], ["Nouveau"], ["Nouveau", "Recent"]])
        self.assertEqual(self.lignes(app), ["Nouveau", "Recent"])
        self.assertEqual(set(arret.intervalles), {7})
        self.assertEqual(self.dialogs, [])

    def test_arret_pendant_un_tour_n_importe_rien(self):
        dossier = h.temp_dir("veille-")
        app = self.creer_app(watch_folder=str(dossier))
        arret = ToursScriptes()

        def tour():
            vieillir(h.make_torrent(dossier, "Tardif", size=1000))
            arret.arrete = True                    # arrêt demandé pendant le balayage
        arret.etapes = [tour]
        app._watch_loop(dossier, arret, 1)
        self.assertTrue(app._inbox.empty())

    def test_dossier_introuvable(self):
        app = self.creer_app(watch_downloads=True, watch_folder=str(h.temp_dir() / "absent"))
        self.assertIsNone(app._watch_thread)
        self.assertIn("absent", app.var_status.get())

    def test_intervalle_illisible_ou_trop_petit(self):
        app = self.creer_app(watch_folder=str(h.temp_dir()))
        app.cfg["watch_downloads"] = True
        try:
            for valeur, attendu in (("abc", 3), (None, 3), (0, 3), ("0.5", 3), (-5, 1),
                                    ("2", 2), (1, 1)):
                app.cfg["watch_interval"] = valeur
                app.restart_watcher()
                self.assertIsNotNone(app._watch_thread)
                self.assertEqual(app._watch_thread._args[2], attendu, valeur)
        finally:
            app.cfg["watch_downloads"] = False
            app.restart_watcher()

    def test_import_automatique_puis_envoi_si_demande(self):
        app = self.creer_app(watch_auto_send=True)
        app.bring_to_front = lambda: None
        envois = []
        app.send_all = lambda: envois.append(1)
        d = h.temp_dir()
        f = h.make_torrent(d, "Veille")
        app._on_watched([str(f)])
        self.assertTrue(self.pump_until(lambda: envois == [1], 2))
        app._on_watched([str(f)])                              # déjà là : pas d'envoi
        self.pump(0.4)
        self.assertEqual(envois, [1])


# --------------------------------------------------------------------------- #
#  Entrées extérieures (service local, autre instance, extension)
# --------------------------------------------------------------------------- #

class TestEntreesExternes(BaseApp):

    def test_submit_external_depuis_un_autre_thread(self):
        app = self.creer_app()
        premier_plan = []
        app.bring_to_front = lambda: premier_plan.append(threading.current_thread())
        t = threading.Thread(target=app.submit_external, args=([magnet(), URL_1],))
        t.start()
        t.join(3)
        self.assertEqual(app.items, {})                        # rien n'est touché depuis le thread
        self.assertFalse(app._inbox.empty())
        self.assertTrue(self.pump_until(lambda: len(app.items) == 2, 3))
        self.assertEqual(self.lignes(app, "type"), ["Magnet", "Lien"])
        self.assertEqual(premier_plan, [threading.main_thread()])

    def test_request_show_ne_plante_pas(self):
        app = self.creer_app()
        self.root.attributes("-alpha", 0.0)                    # reste invisible à l'écran
        self.root.focus_force = lambda: None                   # ne vole pas le focus
        t = threading.Thread(target=app.request_show)
        t.start()
        t.join(3)
        self.assertTrue(self.pump_until(lambda: self.root.state() == "normal", 3))
        self.pump(0.5)
        self.assertFalse(int(self.root.attributes("-topmost")))
        self.root.withdraw()

    def test_reception_sans_envoi_automatique(self):
        app = self.creer_app(watch_auto_send=False)
        app.bring_to_front = lambda: None
        envois = []
        app.send_all = lambda: envois.append(1)
        app.receive_external([magnet()])
        self.assertEqual(len(app.items), 1)
        self.pump(0.4)
        self.assertEqual(envois, [])

    def test_reception_avec_envoi_automatique(self):
        app = self.creer_app(watch_auto_send=True)
        app.bring_to_front = lambda: None
        envois = []
        app.send_all = lambda: envois.append(1)
        app.receive_external([magnet()])
        self.assertTrue(self.pump_until(lambda: envois == [1], 2))
        app.receive_external([magnet()])                       # rien de nouveau
        app.busy = True
        app.receive_external([URL_1])                          # envoi déjà en cours
        app.busy = False
        self.pump(0.4)
        self.assertEqual(envois, [1])

    def test_une_tache_differee_qui_plante_n_arrete_pas_la_pompe(self):
        app = self.creer_app()
        faits = []

        def boum():
            raise RuntimeError("tâche défaillante de test")
        app.post(boum)
        app.post(lambda: faits.append(1))
        self.assertTrue(self.pump_until(lambda: faits == [1], 2))
        app.post(lambda: faits.append(2))
        self.assertTrue(self.pump_until(lambda: faits == [1, 2], 2))
        self.assertIn("traitement différé", paths.log_path().read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
#  Fermeture
# --------------------------------------------------------------------------- #

class TestFermeture(BaseApp):

    def test_fermeture_enregistre_et_arrete_tout(self):
        port = h.free_port()
        token = "jeton-de-test-0123456789abcdef"
        server = start_local_server(lambda items: None, token, port)
        self.assertIsNotNone(server)
        reponse = ask_running_instance("/ping", token, port, retries=3, timeout=1.0)
        self.assertTrue(reponse and reponse.get("ok"), reponse)
        app = self.creer_app(server=server, watch_downloads=True,
                             watch_folder=str(h.temp_dir()), watch_interval=1)
        veille = app._watch_thread
        self.assertTrue(veille.is_alive())
        BOX.raw_failures = 10 ** 6
        app.refresh_server()
        self.attendre_actualisation(app)
        self.assertIsNotNone(app._auto_retry_job)
        app.var_dir.set("  /home/u/fin  ")
        app.var_label.set("Fin")
        app.var_start.set(False)
        app.var_clear.set(True)
        app.on_close()
        self.assertTrue(app._closing)
        self.assertIsNone(app._auto_retry_job)
        self.assertEqual(app._schedule_auto_retry(), 0)
        cfg = load_config()
        self.assertEqual(cfg["last_dir"], "/home/u/fin")
        self.assertEqual(cfg["last_label"], "Fin")
        self.assertFalse(cfg["start_torrents"])
        self.assertTrue(cfg["clear_after_upload"])
        veille.join(3)
        self.assertFalse(veille.is_alive())
        with self.assertRaises(tk.TclError):
            self.root.winfo_exists()                           # fenêtre détruite
        self.assertIsNone(ask_running_instance("/ping", token, port, retries=1, delay=0,
                                               timeout=0.3))


# --------------------------------------------------------------------------- #
#  Fenêtres secondaires (fumée)
# --------------------------------------------------------------------------- #

class FauxIntegration:
    """Bouchon du module winintegration : le registre n'est jamais lu."""

    def __init__(self, etat=None, telechargements=None, erreur=None):
        self.etat = etat or {}
        self.telechargements = telechargements
        self.erreur = erreur
        self.appels = []

    def status(self, *a, **k):
        self.appels.append("status")
        if self.erreur is not None:
            raise self.erreur
        return dict(self.etat)

    def downloads_folder(self):
        self.appels.append("downloads_folder")
        return Path(self.telechargements)

    def __getattr__(self, nom):
        if nom.startswith("__"):
            raise AttributeError(nom)
        raise AssertionError("appel interdit pendant les tests : wi.%s" % nom)


ETAT_ACTIF = {
    "supported": True, "installed": True, "up_to_date": True, "stale": False, "foreign": False,
    "torrent_active": True, "magnet_active": False, "torrent_owner": "TorrentUploader.torrent",
    "user_choice": "",
    "native_host": {"ok": True, "applicable": True, "declared": ["Chrome", "Edge"]},
}
ETAT_INACTIF = {"supported": True, "installed": False,
                "native_host": {"ok": False, "applicable": True}}


class TestDialogues(BaseApp):

    def bouchonner_integration(self, **kw):
        stub = FauxIntegration(telechargements=str(h.temp_dir("telechargements-")), **kw)
        for module in (dialogs_mod, app_mod):
            patcher = mock.patch.object(module, "wi", stub)
            patcher.start()
            self.addCleanup(patcher.stop)
        return stub

    # -- paramètres -- #

    def test_reglages_construction_et_collecte(self):
        app = self.creer_app(timeout=30, verify_ssl=False)
        dlg = SettingsDialog(self.root, app.theme, app.cfg)
        try:
            self.root.update()
            self.assertEqual(dlg.var_url.get(), BOX.url)
            self.assertEqual(dlg.var_user.get(), "u")
            self.assertEqual(dlg.var_pass.get(), "p")
            self.assertEqual(dlg.var_timeout.get(), "30")
            self.assertFalse(dlg.var_ssl.get())
            self.assertFalse(dlg.var_auto.get())
            self.assertEqual(dlg.var_theme.get(), dict(THEME_CHOICES)["light"])
            self.assertEqual(dlg.ent_pass.cget("show"), "•")
            for saisie, attendu in (("3", 10), ("-20", 10), ("5", 10), ("45.9", 45), ("600", 600),
                                    ("9999", 600), ("abc", 60), ("", 60), ("nan", 60)):
                dlg.var_timeout.set(saisie)
                self.assertEqual(dlg._collect()["timeout"], attendu, saisie)
            for cle, libelle in THEME_CHOICES:
                dlg.var_theme.set(libelle)
                self.assertEqual(dlg._collect()["theme"], cle)
            dlg.var_theme.set("Inconnu")
            self.assertEqual(dlg._collect()["theme"], "system")
            dlg.var_url.set("  http://127.0.0.1:9/rutorrent  ")
            dlg.var_user.set("  bob  ")
            dlg.var_pass.set(" mot de passe ")
            dlg.var_ssl.set(True)
            dlg.var_auto.set(True)
            cfg = dlg._collect()
            self.assertEqual(cfg["url"], "http://127.0.0.1:9/rutorrent")
            self.assertEqual(cfg["username"], "bob")
            self.assertEqual(cfg["password"], " mot de passe ")
            self.assertTrue(cfg["verify_ssl"])
            self.assertTrue(cfg["auto_refresh"])
            self.assertEqual(cfg["ipc_port"], app.cfg["ipc_port"])     # le reste est gardé
            self.assertEqual(app.cfg["url"], BOX.url)                  # copie, pas l'original
            dlg.var_show.set(True)
            dlg._toggle_pass()
            self.assertEqual(dlg.ent_pass.cget("show"), "")
            self.assertIsNone(dlg.result)
        finally:
            dlg.destroy()

    def test_reglages_enregistrer(self):
        app = self.creer_app()
        dlg = SettingsDialog(self.root, app.theme, app.cfg)
        self.root.update()
        dlg.var_url.set("   ")
        dlg._save()
        self.assertEqual(self.boites(), ["showwarning"])
        self.assertIsNone(dlg.result)
        self.assertTrue(dlg.winfo_exists())
        dlg.var_url.set(BOX.url)
        dlg.var_timeout.set("25")
        dlg._save()
        self.assertEqual(dlg.result["url"], BOX.url)
        self.assertEqual(dlg.result["timeout"], 25)
        self.assertFalse(dlg.winfo_exists())

    def test_reglages_tester_la_connexion(self):
        app = self.creer_app()
        dlg = SettingsDialog(self.root, app.theme, app.cfg)
        try:
            self.root.update()
            dlg.var_url.set("")
            dlg._test()
            self.assertEqual(str(dlg.lbl_test.cget("fg")), app.theme.c["danger"])
            dlg.var_url.set(BOX.url)
            dlg._test()
            self.assertFalse(dlg.btn_test.enabled)
            self.assertTrue(self.pump_until(lambda: dlg.btn_test.enabled, 10))
            self.assertEqual(str(dlg.lbl_test.cget("fg")), app.theme.c["success"])
            BOX.reset("unauth")
            dlg._test()
            self.assertTrue(self.pump_until(lambda: dlg.btn_test.enabled, 10))
            self.assertEqual(str(dlg.lbl_test.cget("fg")), app.theme.c["danger"])
            texte = dlg.lbl_test.cget("text")
            self.assertEqual(texte[:1], texte[:1].upper())
        finally:
            dlg.destroy()

    # -- intégration -- #

    def test_integration_active(self):
        stub = self.bouchonner_integration(etat=ETAT_ACTIF)
        app = self.creer_app(watch_folder=str(h.temp_dir()))
        dlg = IntegrationDialog(self.root, app)
        try:
            self.root.update()
            self.assertIn("status", stub.appels)
            self.assertEqual(len(dlg.rows.winfo_children()), 2)       # .torrent + magnet
            self.assertEqual(dlg.btn_on._label.cget("text"), "Réparer")
            self.assertTrue(dlg.btn_off.enabled)
            ext = " ".join(textes(dlg.ext_rows))
            self.assertIn("Chrome, Edge", ext)
            self.assertIn(str(app.port), ext)                         # pas de service local
            self.assertEqual(len(dlg.ext_rows.winfo_children()), 2)
            self.assertEqual(dlg.var_endpoint.get(), "http://127.0.0.1:%d" % app.port)
            self.assertEqual(dlg.var_token.get(), app.token)
            self.assertEqual(dlg.var_folder.get(), app.cfg["watch_folder"])
            self.assertFalse(dlg.var_watch.get())
        finally:
            dlg.destroy()

    def test_integration_inactive(self):
        self.bouchonner_integration(etat=ETAT_INACTIF)
        app = self.creer_app(watch_folder=str(h.temp_dir()))
        dlg = IntegrationDialog(self.root, app)
        try:
            self.root.update()
            self.assertEqual(len(dlg.rows.winfo_children()), 1)
            self.assertEqual(dlg.btn_on._label.cget("text"), "Activer")
            self.assertFalse(dlg.btn_off.enabled)
            self.assertIn("Activer", " ".join(textes(dlg.ext_rows)))
        finally:
            dlg.destroy()

    def test_integration_etat_illisible(self):
        self.bouchonner_integration(erreur=RuntimeError("registre illisible (test)"))
        app = self.creer_app(watch_folder=str(h.temp_dir()))
        dlg = IntegrationDialog(self.root, app)
        try:
            self.root.update()
            self.assertIn("registre illisible (test)", " ".join(textes(dlg.rows)))
        finally:
            dlg.destroy()

    def test_integration_fermer_applique_la_surveillance(self):
        stub = self.bouchonner_integration(etat=ETAT_ACTIF)
        dossier = h.temp_dir("surveille-")
        app = self.creer_app()
        dlg = IntegrationDialog(self.root, app)
        self.root.update()
        self.assertEqual(dlg.var_folder.get(), stub.telechargements)   # dossier par défaut
        dlg._toggle_manual()
        dlg.var_watch.set(True)
        dlg.var_autosend.set(True)
        dlg.var_folder.set(str(dossier))
        dlg.close()
        self.assertFalse(dlg.winfo_exists())
        self.assertTrue(app.cfg["watch_downloads"])
        self.assertTrue(app.cfg["watch_auto_send"])
        self.assertEqual(app.cfg["watch_folder"], str(dossier))
        self.assertTrue(load_config()["watch_downloads"])
        self.assertIsNotNone(app._watch_thread)
        self.assertTrue(app._watch_thread.is_alive())
        app.cfg["watch_downloads"] = False
        app.restart_watcher()

    def test_integration_dossier_par_defaut_enregistre_vide(self):
        stub = self.bouchonner_integration(etat=ETAT_ACTIF)
        app = self.creer_app(watch_folder="")
        dlg = IntegrationDialog(self.root, app)
        self.root.update()
        dlg.var_watch.set(True)
        dlg.close()
        self.assertEqual(app.cfg["watch_folder"], "")
        self.assertEqual(app.watch_folder(), Path(stub.telechargements))
        app.cfg["watch_downloads"] = False
        app.restart_watcher()


# --------------------------------------------------------------------------- #
#  Composants graphiques (fumée)
# --------------------------------------------------------------------------- #

class TestComposants(h.TkTestCase):

    def setUp(self):
        super().setUp()
        self.theme = Theme(self.root, "light")
        self.theme.apply(self.root)
        # fenêtre réellement affichée (les clics simulés l'exigent) mais transparente
        self.top = tk.Toplevel(self.root)
        self.top.attributes("-alpha", 0.0)
        self.top.geometry("320x320+40+40")

    def test_flatbutton_invoke_et_desactivation(self):
        clics = []
        b = FlatButton(self.top, self.theme, "Bouton", icon="send", command=lambda: clics.append(1))
        b.pack()
        b.invoke()
        self.assertEqual(clics, [1])
        b.set_enabled(False)
        self.assertFalse(b.enabled)
        self.assertEqual(str(b.cget("takefocus")), "0")
        b.invoke()
        self.assertEqual(b._on_key(None), "break")
        self.assertEqual(clics, [1])
        b.set_enabled(True)
        b._on_key(None)
        self.assertEqual(clics, [1, 1])
        b.set_text("Autre")
        self.assertEqual(b._label.cget("text"), "Autre")

    def test_flatbutton_clic_a_la_souris(self):
        clics = []
        b = FlatButton(self.top, self.theme, "Clic", command=lambda: clics.append(1))
        b.pack()
        self.root.update()
        x0, y0 = b.winfo_rootx(), b.winfo_rooty()
        b.event_generate("<ButtonPress-1>", x=3, y=3)
        b.event_generate("<ButtonRelease-1>", x=3, y=3, rootx=x0 + 3, rooty=y0 + 3)
        self.root.update()
        self.assertEqual(clics, [1])
        b.event_generate("<ButtonPress-1>", x=3, y=3)          # relâché hors du bouton
        b.event_generate("<ButtonRelease-1>", x=3, y=3, rootx=x0 + 900, rooty=y0 + 900)
        self.root.update()
        self.assertEqual(clics, [1])

    def test_checkbox_bascule_sa_variable(self):
        var = tk.BooleanVar(value=False)
        appels = []
        cb = CheckBox(self.top, self.theme, "Case", var, command=lambda: appels.append(var.get()))
        cb.pack()
        self.root.update()
        self.assertEqual(cb._toggle(), "break")
        self.assertTrue(var.get())
        cb.event_generate("<Button-1>", x=2, y=2)
        self.root.update()
        self.assertFalse(var.get())
        self.assertEqual(appels, [True, False])
        self.assertTrue(var.trace_info())
        cb.destroy()
        self.assertEqual(var.trace_info(), [])                 # plus d'écouteur orphelin
        var.set(True)                                          # sans erreur après destruction

    def test_dropzone_commande(self):
        appels = []
        dz = DropZone(self.top, self.theme, "Titre", "Sous-titre", command=lambda: appels.append(1))
        dz.pack(fill="x")
        self.root.update()
        self.assertTrue(dz.find_all())                         # dessinée
        dz.event_generate("<Button-1>", x=10, y=10)
        self.root.update()
        self.assertEqual(appels, [1])
        dz.flash()
        self.assertTrue(dz._hover)
        self.pump(0.45)
        self.assertFalse(dz._hover)

    def test_statuspill(self):
        pill = StatusPill(self.top, self.theme, self.theme.c["header"], self.theme.c["header_muted"])
        pill.pack()
        pill.set("hôte — connectée", "ok")
        self.assertEqual(pill._label.cget("text"), "hôte — connectée")
        self.assertNotEqual(pill._canvas.itemcget(pill._dot, "fill"), self.theme.c["faint"])
        pill.set("hôte", "niveau-inconnu")
        self.assertEqual(pill._canvas.itemcget(pill._dot, "fill"), self.theme.c["faint"])


# --------------------------------------------------------------------------- #
#  Défauts connus du produit
# --------------------------------------------------------------------------- #

class TestDefautsConnus(BaseApp):

    def test_vider_apres_envoi_garde_les_ajouts_faits_pendant_l_envoi(self):
        """Corrigé — « Vider la liste après un envoi réussi » efface aussi les éléments
        ajoutés PENDANT l'envoi, qui n'ont jamais été envoyés (perte silencieuse)."""
        app = self.creer_app(clear_after_upload=True)
        f = h.make_torrent(h.temp_dir(), "Lot")
        BOX.present = {empreinte(f)}
        app.add_files([f])
        app.send_all()
        self.assertTrue(app.busy)          # ne retombe qu'à _upload_done, dans ce thread-ci
        app.add_items([magnet(nom="Arrive pendant l'envoi")])
        self.attendre_envoi(app)
        self.assertEqual([it["kind"] for it in app.items.values()], ["magnet"])

    def test_envoi_auto_d_un_element_recu_pendant_un_envoi(self):
        """Corrigé — avec l'envoi automatique, un torrent reçu pendant un envoi en
        cours n'est jamais envoyé (il reste « En attente » indéfiniment)."""
        app = self.creer_app(watch_auto_send=True)
        app.bring_to_front = lambda: None
        f = h.make_torrent(h.temp_dir(), "Premier")
        BOX.present = {empreinte(f), MAGNET_HASH}
        app.add_files([f])
        app.send_all()
        self.assertTrue(app.busy)          # ne retombe qu'à _upload_done, dans ce thread-ci
        app.receive_external([magnet(nom="Second")])
        self.attendre_envoi(app)
        self.pump(0.45)                    # laisse passer un éventuel envoi différé (300 ms)
        self.attendre_envoi(app)
        etats = {it["kind"]: it["status"] for it in app.items.values()}
        self.assertEqual(etats.get("magnet"), "ok")

    def test_envoi_debloque_si_le_client_ne_peut_etre_cree(self):
        """Corrigé — si la création du client échoue (configuration illisible), send_all
        laisse busy=True et le bouton désactivé : plus aucun envoi possible."""
        app = self.creer_app()
        app.add_items([magnet()])

        def client_impossible():
            raise ValueError("délai réseau illisible")
        app._client = client_impossible
        try:
            try:
                app.send_all()
            except ValueError:
                pass
            self.assertFalse(app.busy)
            self.assertTrue(app.btn_send.enabled)
        finally:
            app.busy = False

    def test_actualisation_debloquee_si_le_client_ne_peut_etre_cree(self):
        """Corrigé — même cause côté actualisation : _refreshing reste vrai, le bouton
        « Actualiser » reste grisé et toute actualisation ultérieure est ignorée."""
        app = self.creer_app()

        def client_impossible():
            raise ValueError("délai réseau illisible")
        app._client = client_impossible
        try:
            try:
                app.refresh_server()
            except ValueError:
                pass
            self.assertFalse(app._refreshing)
            self.assertTrue(app.btn_refresh.enabled)
        finally:
            app._refreshing = False

    def test_delai_demesure_ne_fait_pas_planter_l_enregistrement(self):
        """Corrigé — « 1e999 » dans le délai réseau lève OverflowError dans _collect() :
        le bouton « Enregistrer » ne fait plus rien."""
        app = self.creer_app()
        dlg = SettingsDialog(self.root, app.theme, app.cfg)
        try:
            dlg.var_timeout.set("1e999")
            timeout = dlg._collect()["timeout"]
        finally:
            dlg.destroy()
        self.assertTrue(5 <= timeout <= 600)

    def test_enregistrer_les_parametres_n_ecrase_pas_un_releve_recent(self):
        """Corrigé — la fenêtre « Paramètres » renvoie une copie COMPLÈTE de la
        configuration prise à son ouverture ; à l'enregistrement, elle écrase ce qui a
        changé entre-temps (ici les listes relues pendant que la fenêtre était ouverte)."""
        app = self.creer_app(cache_labels=["Ancienne"], cache_dirs=["/ancien"],
                             cache_time="01/01/2026 10:00")
        BOX.raw_failures = 10 ** 6         # la relecture qui suit l'enregistrement échoue

        def pendant_la_fenetre(dlg, *a, **k):
            # une actualisation aboutit pendant que la fenêtre modale est ouverte
            app._on_refreshed(["Nouvelle"], ["/nouveau"], 1, None)
            dlg.var_timeout.set("30")
            dlg._save()
        self.root.wait_window = pendant_la_fenetre
        app.open_settings()
        self.assertEqual(app.cfg["timeout"], 30)
        self.attendre_actualisation(app)
        self.assertEqual(load_config()["cache_labels"], ["Nouvelle"])

    def test_fichier_recree_sous_le_meme_nom_est_reimporte(self):
        """Corrigé — le dossier surveillé retient pour toujours les noms déjà vus ; un
        .torrent supprimé puis téléchargé de nouveau sous le même nom est ignoré."""
        dossier = h.temp_dir("veille-")
        app = self.creer_app(watch_folder=str(dossier))
        app.bring_to_front = lambda: None
        cible = dossier / "Recycle.torrent"
        importes = []

        def tour1():
            cible.write_bytes(h.torrent_bytes("Recycle v1", 1000))
            vieillir(cible)

        def tour2():
            vider_boite(app)
            importes.append(self.lignes(app))
            app.clear_list()
            cible.unlink()                 # le tour suivant constate la disparition

        def tour3():
            vider_boite(app)
            cible.write_bytes(h.torrent_bytes("Recycle v2", 2000))   # nouveau téléchargement
            vieillir(cible)

        app._watch_loop(dossier, ToursScriptes(tour1, tour2, tour3, lambda: None), 1)
        vider_boite(app)
        self.assertEqual(importes, [["Recycle v1"]])
        self.assertEqual(self.lignes(app), ["Recycle v2"])


if __name__ == "__main__":
    unittest.main()
