# -*- coding: utf-8 -*-
r"""Tests de tuploader/winintegration.py : intégration à Windows et aux navigateurs.

RÈGLES DE SÉCURITÉ appliquées par ce module (les vraies associations de
l'utilisateur vivent sur ce poste) :

* toutes les écritures de registre ont lieu sous
  HKCU\Software\TorrentUploaderSelfTest_wi, supprimée avant et après chaque test ;
* un garde-fou, posé pour la durée du module, fait échouer toute écriture de
  winintegration (_set, _delete_value, _delete_tree) qui viserait une autre clé ;
* l'état RÉEL (wi.status() sans argument : lecture seule) est photographié au
  début du module puis comparé à la fin : rien ne doit avoir bougé ;
* notify_shell() est remplacé par un bouchon (pas de rafraîchissement de
  l'explorateur) ; create_desktop_shortcut() et open_default_apps_settings()
  sont rendus inappelables ;
* les fichiers de l'hôte natif sont écrits dans le dossier de données du bac à
  sable (tests/helpers.py), jamais dans le profil de l'utilisateur.

Les défauts du produit révélés à l'écriture de ces tests ont été corrigés ; les tests
qui les mettaient en évidence vérifient désormais le comportement corrigé.
Le décorateur @defaut_produit reste disponible pour signaler un futur écart.
"""

from tests import helpers as h          # EN PREMIER : redirige les données vers le bac à sable

import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

if os.name != "nt":
    raise unittest.SkipTest("winintegration : tests réservés à Windows")

import winreg                                    # noqa: E402

from tuploader import APP_NAME, paths            # noqa: E402
from tuploader import winintegration as wi       # noqa: E402

HKCU = winreg.HKEY_CURRENT_USER
SB = r"Software\TorrentUploaderSelfTest_wi"      # SEULE clé où ces tests écrivent
CONCURRENT = "AutreClient.Torrent"               # ProgId d'un client torrent concurrent (fictif)
CMD_CONCURRENT = r'"C:\AutreClient\client.exe" "%1"'

# Clés de wi.status() RÉEL qui ne doivent pas changer entre le début et la fin du module.
CLES_SURVEILLEES = ("registered", "torrent_active", "magnet_active", "torrent_owner",
                    "effective_command")

_REEL_AVANT = None                               # photo prise par setUpModule()
_VIOLATIONS = []                                 # écritures hors bac à sable refusées par le garde-fou
_NOTIFY = mock.Mock(name="notify_shell")         # bouchon de wi.notify_shell
_VRAI_NOTIFY = wi.notify_shell                   # la vraie fonction, testée avec un faux windll


# --------------------------------------------------------------------------- #
#  Aides locales (indépendantes des aides registre du produit)
# --------------------------------------------------------------------------- #

def dans_le_bac(chemin) -> bool:
    return chemin == SB or str(chemin).startswith(SB + "\\")


def ecrire(chemin, valeur, nom=""):
    """Écrit une valeur REG_SZ — uniquement dans le bac à sable."""
    assert dans_le_bac(chemin), "écriture hors bac à sable : %r" % (chemin,)
    with winreg.CreateKeyEx(HKCU, chemin, 0, winreg.KEY_WRITE) as cle:
        winreg.SetValueEx(cle, nom, 0, winreg.REG_SZ, valeur)


def creer_cle(chemin):
    """Crée une clé vide (sans valeur) — uniquement dans le bac à sable."""
    assert dans_le_bac(chemin), "écriture hors bac à sable : %r" % (chemin,)
    winreg.CloseKey(winreg.CreateKeyEx(HKCU, chemin, 0, winreg.KEY_WRITE))


def lire(chemin, nom=""):
    """(donnée, type) ou None si la clé ou la valeur n'existe pas. Lecture seule."""
    try:
        with winreg.OpenKey(HKCU, chemin, 0, winreg.KEY_READ) as cle:
            return winreg.QueryValueEx(cle, nom)
    except OSError:
        return None


def existe(chemin) -> bool:
    try:
        winreg.CloseKey(winreg.OpenKey(HKCU, chemin, 0, winreg.KEY_READ))
        return True
    except OSError:
        return False


def instantane(racine=SB) -> dict:
    """{chemin de clé: {nom de valeur: (donnée, type)}} pour toute l'arborescence."""
    resultat = {}
    try:
        cle = winreg.OpenKey(HKCU, racine, 0, winreg.KEY_READ)
    except OSError:
        return resultat
    with cle:
        nb_cles, nb_valeurs, _ = winreg.QueryInfoKey(cle)
        valeurs = {}
        for i in range(nb_valeurs):
            nom, donnee, genre = winreg.EnumValue(cle, i)
            valeurs[nom] = (donnee, genre)
        enfants = [winreg.EnumKey(cle, i) for i in range(nb_cles)]
    resultat[racine] = valeurs
    for enfant in enfants:
        resultat.update(instantane(racine + "\\" + enfant))
    return resultat


def sz(texte):
    """Forme sous laquelle lire() et instantane() rendent une chaîne REG_SZ."""
    return (texte, winreg.REG_SZ)


def photo_du_reel() -> dict:
    """État RÉEL du poste, en LECTURE SEULE : wi.status() + quelques valeurs brutes."""
    etat = wi.status()
    photo = {cle: etat.get(cle) for cle in CLES_SURVEILLEES}
    photo["registre"] = {
        ".torrent": lire(wi.CLASSES + r"\.torrent"),
        "ouvrir avec": lire(wi.CLASSES + r"\.torrent\OpenWithProgids", wi.PROGID_FILE),
        "progid fichier": lire(wi.CLASSES + "\\" + wi.PROGID_FILE + r"\shell\open\command"),
        "progid magnet": lire(wi.CLASSES + "\\" + wi.PROGID_MAGNET + r"\shell\open\command"),
        "magnet": lire(wi.CLASSES + r"\magnet\shell\open\command"),
        "applications": lire(wi.REGISTERED_APPS, wi.REGISTERED_NAME),
        "capabilities": lire(wi.APP_KEY + r"\Capabilities", "ApplicationName"),
        "hôtes": {nom: lire(cle) for nom, cle in wi._host_roots(force_all=True)},
    }
    return photo


def defaut_produit(raison):
    """Marque un test qui échoue À CAUSE d'un défaut du produit (échec attendu)."""
    def decorateur(test):
        test.__doc__ = "%s\n\n        DÉFAUT PRODUIT (échec attendu) : %s" % (
            (test.__doc__ or test.__name__).strip(), raison)
        return unittest.expectedFailure(test)
    return decorateur


def _garde(fonction):
    """Enveloppe une aide d'écriture du produit : refuse tout chemin hors du bac à sable."""
    def gardee(chemin, *args, **kwargs):
        if not dans_le_bac(chemin):
            _VIOLATIONS.append((fonction.__name__, chemin))
            raise AssertionError("écriture de registre hors du bac à sable refusée : %s(%r)"
                                 % (fonction.__name__, chemin))
        return fonction(chemin, *args, **kwargs)
    gardee.__name__ = fonction.__name__
    return gardee


def _interdit(nom):
    def refuse(*args, **kwargs):
        raise AssertionError("%s() ne doit jamais être appelé par les tests" % nom)
    return refuse


def setUpModule():
    global _REEL_AVANT
    _REEL_AVANT = photo_du_reel()                # AVANT toute écriture
    remplacements = [(nom, _garde(getattr(wi, nom)))
                     for nom in ("_set", "_delete_value", "_delete_tree")]
    remplacements += [("notify_shell", _NOTIFY),
                      ("create_desktop_shortcut", _interdit("create_desktop_shortcut")),
                      ("open_default_apps_settings", _interdit("open_default_apps_settings"))]
    for nom, remplacement in remplacements:
        patch = mock.patch.object(wi, nom, remplacement)
        patch.start()
        unittest.addModuleCleanup(patch.stop)
    wi._delete_tree(SB)


def tearDownModule():
    wi._delete_tree(SB)
    if existe(SB):
        raise AssertionError("la clé bac à sable %s n'a pas pu être supprimée" % SB)
    if _VIOLATIONS:
        raise AssertionError("écritures tentées hors du bac à sable : %r" % (_VIOLATIONS,))
    apres = photo_du_reel()
    if apres != _REEL_AVANT:
        raise AssertionError("l'état RÉEL de l'intégration Windows a changé pendant les tests :\n"
                             "avant : %r\naprès : %r" % (_REEL_AVANT, apres))


class BacASable(unittest.TestCase):
    """Base : une clé de registre bac à sable neuve (et supprimée) pour chaque test."""

    def setUp(self):
        self.sb = SB
        self.classes = SB + r"\Classes"
        self.app_key = SB + r"\App"
        self.registered_apps = SB + r"\RegisteredApplications"
        self.native_roots = [
            ("Chrome", SB + "\\Chrome\\NativeMessagingHosts\\" + wi.HOST_NAME),
            ("Edge", SB + "\\Edge\\NativeMessagingHosts\\" + wi.HOST_NAME),
        ]
        # raccourcis
        self.progid = self.classes + "\\" + wi.PROGID_FILE
        self.progid_magnet = self.classes + "\\" + wi.PROGID_MAGNET
        self.torrent = self.classes + r"\.torrent"
        self.magnet = self.classes + r"\magnet"
        self.cap_key = self.app_key + r"\Capabilities"
        self.manifeste, self.bat = wi._host_files()
        self._menage()
        _NOTIFY.reset_mock()

        # status() lit le VRAI « UserChoice » de l'utilisateur, quel que soit `classes` :
        # on le masque pour que les tests ne dépendent pas des réglages de ce poste.
        self.choix = {wi.FILEEXTS_USERCHOICE: None, wi.MAGNET_USERCHOICE: None}
        vrai_get = wi._get

        def faux_get(chemin, nom=""):
            if chemin in self.choix:
                return self.choix[chemin]
            return vrai_get(chemin, nom)

        patch = mock.patch.object(wi, "_get", faux_get)
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        self._menage()

    def _menage(self):
        wi._delete_tree(SB)
        assert not existe(SB), "clé bac à sable non supprimée"
        dossier = paths.native_host_dir()
        assert h.SANDBOX in dossier.parents, "dossier de l'hôte natif hors bac à sable : %s" % dossier
        for fichier in dossier.iterdir():
            if fichier.is_file():
                fichier.unlink()

    # -- appels du produit, toujours avec les racines du bac à sable ---------- #

    def enregistrer(self, native_roots=None):
        return wi.register(self.classes, self.app_key, self.registered_apps,
                           self.native_roots if native_roots is None else native_roots)

    def desenregistrer(self, native_roots=None):
        return wi.unregister(self.classes, self.app_key, self.registered_apps,
                             self.native_roots if native_roots is None else native_roots)

    def etat(self, native_roots=None):
        return wi.status(classes=self.classes,
                         native_roots=self.native_roots if native_roots is None else native_roots)

    def commande(self, script, lanceur=None, marqueur="%L"):
        return '"%s" "%s" "%s"' % (lanceur or wi.python_launcher(), script, marqueur)

    def sans_python(self):
        """Contexte : aucun interpréteur utilisable (ni voisin de sys.executable, ni PATH)."""
        vide = h.temp_dir("sans-python-")
        pile = mock.patch.multiple(wi.shutil, which=mock.Mock(return_value=None))
        exe = mock.patch.object(sys, "executable", str(vide / "python.exe"))

        class _Contexte:
            def __enter__(self_inner):
                pile.start()
                exe.start()

            def __exit__(self_inner, *exc):
                exe.stop()
                pile.stop()
                return False

        return _Contexte()


# --------------------------------------------------------------------------- #
#  Interpréteur, scripts et commandes
# --------------------------------------------------------------------------- #

class TestInterpreteur(unittest.TestCase):

    def test_python_launcher_se_termine_par_pythonw(self):
        """python_launcher() rend un pythonw.exe existant, jamais l'alias du Store."""
        chemin = wi.python_launcher()
        self.assertEqual(os.path.basename(chemin).lower(), "pythonw.exe")
        self.assertTrue(chemin.lower().endswith("pythonw.exe"))
        self.assertTrue(Path(chemin).is_file(), chemin)
        self.assertGreater(Path(chemin).stat().st_size, 0)
        self.assertNotIn("\\microsoft\\windowsapps\\", chemin.lower())

    def test_python_console_se_termine_par_python(self):
        """python_console() rend un python.exe (pas pythonw) existant, jamais l'alias du Store."""
        chemin = wi.python_console()
        self.assertEqual(os.path.basename(chemin).lower(), "python.exe")
        self.assertTrue(chemin.lower().endswith("python.exe"))
        self.assertTrue(Path(chemin).is_file(), chemin)
        self.assertGreater(Path(chemin).stat().st_size, 0)
        self.assertNotIn("\\microsoft\\windowsapps\\", chemin.lower())

    def test_interpreteur_courant_prioritaire(self):
        """Le pythonw.exe voisin de sys.executable passe avant celui du PATH."""
        voisin = Path(sys.executable).with_name("pythonw.exe")
        if not wi._is_usable_exe(voisin):
            self.skipTest("pas de pythonw.exe à côté de l'interpréteur courant")
        ailleurs = h.temp_dir("py-") / "pythonw.exe"
        ailleurs.write_bytes(b"MZ factice")
        with mock.patch.object(wi.shutil, "which", return_value=str(ailleurs)):
            self.assertEqual(wi.python_launcher(), str(voisin))

    def test_is_usable_exe_refuse_un_chemin_windowsapps(self):
        """Un exécutable non vide rangé sous \\Microsoft\\WindowsApps\\ est écarté."""
        base = h.temp_dir("store-")
        for dossier in (base / "Microsoft" / "WindowsApps", base / "MICROSOFT" / "windowsapps" / "sous"):
            dossier.mkdir(parents=True, exist_ok=True)
            alias = dossier / "python.exe"
            alias.write_bytes(b"MZ" + b"\0" * 64)
            self.assertFalse(wi._is_usable_exe(alias), alias)
            self.assertFalse(wi._is_usable_exe(str(alias)), alias)
        # le même fichier, ailleurs, est accepté : c'est bien le chemin qui est refusé
        ailleurs = base / "Python" / "python.exe"
        ailleurs.parent.mkdir()
        ailleurs.write_bytes(b"MZ" + b"\0" * 64)
        self.assertTrue(wi._is_usable_exe(ailleurs))

    def test_is_usable_exe_refuse_le_vrai_alias_du_store(self):
        """L'alias réel du Microsoft Store, s'il existe sur ce poste, est écarté."""
        local = os.environ.get("LOCALAPPDATA")
        alias = Path(local) / "Microsoft" / "WindowsApps" / "python.exe" if local else None
        if alias is None or not os.path.lexists(str(alias)):
            self.skipTest("pas d'alias du Microsoft Store sur ce poste")
        self.assertFalse(wi._is_usable_exe(alias))

    def test_is_usable_exe_refuse_un_fichier_vide(self):
        """Un fichier de 0 octet (alias du Store) n'est pas un interpréteur."""
        vide = h.temp_dir("vide-") / "pythonw.exe"
        vide.write_bytes(b"")
        self.assertFalse(wi._is_usable_exe(vide))
        self.assertFalse(wi._is_usable_exe(str(vide)))
        vide.write_bytes(b"M")                   # un seul octet suffit à le distinguer de l'alias
        self.assertTrue(wi._is_usable_exe(vide))

    def test_is_usable_exe_refuse_absent_dossier_et_vide(self):
        """None, chaîne vide, fichier absent et dossier sont refusés sans exception."""
        dossier = h.temp_dir("exe-")
        self.assertFalse(wi._is_usable_exe(None))
        self.assertFalse(wi._is_usable_exe(""))
        self.assertFalse(wi._is_usable_exe(dossier / "absent.exe"))
        self.assertFalse(wi._is_usable_exe(dossier))
        self.assertFalse(wi._is_usable_exe("\0chemin invalide"))

    def test_is_usable_exe_accepte_l_interpreteur_courant(self):
        """L'interpréteur qui exécute les tests est utilisable (chaîne ou Path)."""
        self.assertTrue(wi._is_usable_exe(sys.executable))
        self.assertTrue(wi._is_usable_exe(Path(sys.executable)))

    def test_find_python_ecarte_l_alias_et_prend_le_candidat_suivant(self):
        """Un voisin de 0 octet est écarté au profit de l'exécutable trouvé dans le PATH."""
        faux_python = h.temp_dir("alias-")
        (faux_python / "pythonw.exe").write_bytes(b"")           # alias vide à côté de sys.executable
        bon = h.temp_dir("bon-") / "pythonw.exe"
        bon.write_bytes(b"MZ factice")
        with mock.patch.object(sys, "executable", str(faux_python / "python.exe")), \
                mock.patch.object(wi.shutil, "which", return_value=str(bon)) as which:
            self.assertEqual(wi.python_launcher(), str(bon))
        which.assert_called_with("pythonw.exe")

    def test_find_python_introuvable_leve_une_erreur_explicite(self):
        """Sans interpréteur utilisable : IntegrationError en français, qui nomme le fichier."""
        vide = h.temp_dir("rien-")
        store = h.temp_dir("store-") / "Microsoft" / "WindowsApps"
        store.mkdir(parents=True)
        for nom in ("python.exe", "pythonw.exe"):
            (store / nom).write_bytes(b"MZ alias")
        for which in (None, "alias"):
            def trouve(nom, _mode=which):
                return None if _mode is None else str(store / nom)
            with mock.patch.object(sys, "executable", str(vide / "python.exe")), \
                    mock.patch.object(wi.shutil, "which", side_effect=trouve):
                for fonction, nom in ((wi.python_launcher, "pythonw.exe"),
                                      (wi.python_console, "python.exe")):
                    with self.assertRaises(wi.IntegrationError) as ctx:
                        fonction()
                    message = str(ctx.exception)
                    self.assertTrue(message.startswith(nom), message)
                    self.assertIn("introuvable", message)
                    self.assertIn("python.org", message)
                    self.assertIn("Microsoft Store", message)

    def test_find_python_sans_sys_executable(self):
        """sys.executable vide (Python embarqué) : on se rabat sur le PATH sans planter."""
        bon = h.temp_dir("path-") / "python.exe"
        bon.write_bytes(b"MZ factice")
        with mock.patch.object(sys, "executable", ""), \
                mock.patch.object(wi.shutil, "which", return_value=str(bon)):
            self.assertEqual(wi.python_console(), str(bon))

    def test_chemins_des_scripts_et_de_l_icone(self):
        """script_path(), host_script_path() et icon_path() visent les fichiers du projet."""
        self.assertEqual(Path(wi.script_path()), h.ROOT / "torrent_uploader.py")
        self.assertEqual(Path(wi.host_script_path()), h.ROOT / "native_host.py")
        self.assertEqual(Path(wi.icon_path()), h.ROOT / "tuploader" / "assets" / "icon.ico")
        self.assertTrue(Path(wi.script_path()).is_file())
        self.assertTrue(Path(wi.host_script_path()).is_file())
        for chemin in (wi.script_path(), wi.host_script_path(), wi.icon_path()):
            self.assertIsInstance(chemin, str)
            self.assertTrue(os.path.isabs(chemin), chemin)

    def test_commands_fichier_en_L_et_uri_en_1(self):
        """commands() : même lanceur et même script, « %L » pour le fichier, « %1 » pour l'URI."""
        fichier, uri = wi.commands()
        prefixe = '"%s" "%s" ' % (wi.python_launcher(), wi.script_path())
        self.assertEqual(fichier, prefixe + '"%L"')
        self.assertEqual(uri, prefixe + '"%1"')
        self.assertIn("pythonw.exe", fichier.lower())     # pas de fenêtre de console

    def test_script_in_command(self):
        """_script_in_command() rend le script (2e élément), ou le .exe autonome seul."""
        self.assertEqual(
            wi._script_in_command(r'"C:\Py\pythonw.exe" "D:\Mes applis\torrent_uploader.py" "%L"'),
            r"D:\Mes applis\torrent_uploader.py")
        self.assertEqual(wi._script_in_command(wi.commands()[0]), wi.script_path())
        self.assertEqual(wi._script_in_command(wi.commands()[1]), wi.script_path())
        self.assertEqual(wi._script_in_command('"a" "b" "c" "d"'), "b")
        for sans_script in (None, "", r"C:\outil.exe %1", r'"C:\Py\pythonw.exe" "%1"',
                            'guillemet " orphelin'):
            self.assertEqual(wi._script_in_command(sans_script), "", repr(sans_script))
        # un .exe seul (hors interpréteur Python) identifie une installation autonome
        self.assertEqual(wi._script_in_command(r'"C:\seul.exe"'), r"C:\seul.exe")
        self.assertEqual(wi._script_in_command(r'"C:\outil.exe" %1'), r"C:\outil.exe")

    def test_constantes_du_contrat(self):
        """Noms attendus par Windows, par les navigateurs et par l'extension."""
        self.assertEqual(wi.CLASSES, r"Software\Classes")
        self.assertEqual(wi.REGISTERED_APPS, r"Software\RegisteredApplications")
        self.assertEqual(wi.PROGID_FILE, "TorrentUploader.torrent")
        self.assertEqual(wi.PROGID_MAGNET, "TorrentUploader.magnet")
        # nom d'hôte natif : minuscules, chiffres, « _ » et points, sans point en tête/queue
        self.assertRegex(wi.HOST_NAME, r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")
        self.assertTrue(wi.EXTENSION_IDS)
        for identifiant in wi.EXTENSION_IDS:
            self.assertRegex(identifiant, r"^[a-p]{32}$")

    def test_racines_des_navigateurs(self):
        """_host_roots(force_all=True) : une clé NativeMessagingHosts\\<hôte> par navigateur."""
        racines = dict(wi._host_roots(force_all=True))
        self.assertEqual(set(racines), {"Chrome", "Edge", "Brave", "Chromium"})
        self.assertEqual(racines["Chrome"],
                         r"Software\Google\Chrome\NativeMessagingHosts" + "\\" + wi.HOST_NAME)
        self.assertEqual(racines["Edge"],
                         r"Software\Microsoft\Edge\NativeMessagingHosts" + "\\" + wi.HOST_NAME)
        for cle in racines.values():
            self.assertTrue(cle.endswith("\\NativeMessagingHosts\\" + wi.HOST_NAME), cle)
        # sans force_all : seulement les navigateurs présents (lecture seule du registre réel)
        presents = wi._host_roots()
        self.assertTrue(set(presents) <= set(wi._host_roots(force_all=True)))


# --------------------------------------------------------------------------- #
#  Aides registre
# --------------------------------------------------------------------------- #

class TestAidesRegistre(BacASable):

    def test_set_puis_get(self):
        """_set() crée les clés intermédiaires ; _get() relit défaut et valeurs nommées."""
        cle = SB + r"\Aides\Clé accentuée\Niveau 3"
        wi._set(cle, "donnée par défaut — éàü")
        wi._set(cle, "nommée", name="Valeur nommée")
        wi._set(cle, "", name="Vide")
        self.assertEqual(wi._get(cle), "donnée par défaut — éàü")
        self.assertEqual(wi._get(cle, "Valeur nommée"), "nommée")
        self.assertEqual(wi._get(cle, "Vide"), "")
        self.assertEqual(lire(cle), sz("donnée par défaut — éàü"))
        self.assertEqual(lire(cle, "Vide"), sz(""))
        wi._set(cle, "remplacée")
        self.assertEqual(wi._get(cle), "remplacée")

    def test_get_absent_rend_none(self):
        """_get() : None pour une clé absente, une valeur absente, un défaut non défini."""
        self.assertIsNone(wi._get(SB + r"\Absente"))
        self.assertIsNone(wi._get(SB + r"\Absente", "Valeur"))
        creer_cle(SB + r"\Vide")
        self.assertIsNone(wi._get(SB + r"\Vide"))
        self.assertIsNone(wi._get(SB + r"\Vide", "Valeur"))
        self.assertFalse(existe(SB + r"\Absente"), "_get() ne doit rien créer")

    def test_exists(self):
        """_exists() distingue clé présente et clé absente, sans rien créer."""
        self.assertFalse(wi._exists(SB))
        creer_cle(SB + r"\Présente")
        self.assertTrue(wi._exists(SB))
        self.assertTrue(wi._exists(SB + r"\Présente"))
        self.assertFalse(wi._exists(SB + r"\Présente\Enfant"))
        self.assertFalse(existe(SB + r"\Présente\Enfant"))

    def test_delete_value(self):
        """_delete_value() : vrai si la valeur existait, faux sinon ; ne crée ni ne supprime de clé."""
        cle = SB + r"\Valeurs"
        ecrire(cle, "a", "A")
        ecrire(cle, "b", "B")
        self.assertTrue(wi._delete_value(cle, "A"))
        self.assertIsNone(lire(cle, "A"))
        self.assertEqual(lire(cle, "B"), sz("b"))
        self.assertFalse(wi._delete_value(cle, "A"))             # déjà retirée
        self.assertFalse(wi._delete_value(SB + r"\Absente", "A"))
        self.assertFalse(existe(SB + r"\Absente"))
        self.assertTrue(existe(cle))

    def test_delete_tree_arbre_profond(self):
        """_delete_tree() supprime une clé et toute sa descendance, même profonde."""
        racine = SB + r"\Profond"
        chemin = racine
        for niveau in range(14):
            chemin += "\\niveau %02d" % niveau
            ecrire(chemin, "valeur %d" % niveau)
            ecrire(chemin, "x", "nommée")
            ecrire(chemin + r"\branche latérale\feuille", "y")
        self.assertGreater(len(instantane(racine)), 40)
        self.assertTrue(wi._delete_tree(racine))
        self.assertFalse(existe(racine))
        self.assertEqual(instantane(racine), {})

    def test_delete_tree_arbre_large(self):
        """Beaucoup de sous-clés sœurs : la liste se décale à chaque suppression."""
        racine = SB + r"\Large"
        for i in range(60):
            ecrire(racine + "\\soeur %02d\\enfant" % i, str(i))
        self.assertTrue(wi._delete_tree(racine))
        self.assertFalse(existe(racine))

    def test_delete_tree_cle_absente(self):
        """_delete_tree() rend False pour une clé inexistante, sans rien créer."""
        self.assertFalse(wi._delete_tree(SB + r"\Jamais créée"))
        self.assertFalse(existe(SB))

    def test_delete_tree_epargne_les_voisins(self):
        """Seule la clé visée disparaît : ni ses sœurs, ni une clé au nom voisin."""
        ecrire(SB + r"\Arbre\enfant", "à supprimer")
        ecrire(SB + r"\Arbre2\enfant", "homonyme partiel")
        ecrire(SB + r"\Voisin", "intact")
        ecrire(SB, "racine", "valeur de la racine")
        self.assertTrue(wi._delete_tree(SB + r"\Arbre"))
        self.assertFalse(existe(SB + r"\Arbre"))
        self.assertEqual(lire(SB + r"\Arbre2\enfant"), sz("homonyme partiel"))
        self.assertEqual(lire(SB + r"\Voisin"), sz("intact"))
        self.assertEqual(lire(SB, "valeur de la racine"), sz("racine"))

    def test_delete_tree_feuille_avec_valeurs(self):
        """Une clé sans sous-clé mais avec des valeurs est supprimée d'un coup."""
        ecrire(SB + r"\Feuille", "a")
        ecrire(SB + r"\Feuille", "b", "B")
        self.assertTrue(wi._delete_tree(SB + r"\Feuille"))
        self.assertFalse(existe(SB + r"\Feuille"))

    def test_delete_tree_sous_cle_indestructible_ne_boucle_pas(self):
        """Une sous-clé impossible à supprimer doit donner False, pas une boucle infinie."""
        ecrire(SB + r"\Verrou\enfant", "x")
        vrai_enum, vrai_delete = winreg.EnumKey, winreg.DeleteKey
        appels = [0]

        class Boucle(BaseException):
            pass

        def enum(cle, index):
            appels[0] += 1
            if appels[0] > 100:                  # une clé à un seul enfant : 100 lectures = boucle
                raise Boucle()
            return vrai_enum(cle, index)

        def delete(racine, chemin, *reste):
            if chemin.endswith("enfant"):
                raise PermissionError(5, "Accès refusé")
            return vrai_delete(racine, chemin, *reste)

        resultat = "boucle"
        with mock.patch.object(wi.winreg, "EnumKey", enum), \
                mock.patch.object(wi.winreg, "DeleteKey", delete):
            try:
                resultat = wi._delete_tree(SB + r"\Verrou")
            except Boucle:
                pass
        self.assertIs(resultat, False, "boucle infinie sur une sous-clé indestructible")

    def test_le_garde_fou_refuse_toute_ecriture_hors_du_bac(self):
        """Filet de sécurité de ces tests : aucune aide d'écriture ne sort du bac à sable."""
        hors = SB + "_hors_bac"                  # même préfixe, mais PAS sous la clé bac à sable
        avant = len(_VIOLATIONS)
        try:
            for appel in (lambda: wi._set(hors, "x"),
                          lambda: wi._delete_value(hors, "x"),
                          lambda: wi._delete_tree(hors),
                          lambda: wi._set(wi.CLASSES + r"\.torrent", "x")):
                with self.assertRaises(AssertionError):
                    appel()
            self.assertFalse(existe(hors))
            self.assertEqual(len(_VIOLATIONS) - avant, 4)
        finally:
            del _VIOLATIONS[avant:]              # violations provoquées exprès par ce test
        with self.assertRaises(AssertionError):
            wi.create_desktop_shortcut()
        with self.assertRaises(AssertionError):
            wi.open_default_apps_settings()


# --------------------------------------------------------------------------- #
#  register()
# --------------------------------------------------------------------------- #

class TestRegister(BacASable):

    def test_commande_du_progid_se_termine_par_L(self):
        """Le ProgId des .torrent lance pythonw + torrent_uploader.py avec « "%L" »."""
        self.enregistrer()
        self.assertEqual(lire(self.progid), sz("Fichier torrent"))
        commande, genre = lire(self.progid + r"\shell\open\command")
        self.assertEqual(genre, winreg.REG_SZ)
        self.assertTrue(commande.endswith(' "%L"'), commande)
        self.assertEqual(commande, '"%s" "%s" "%%L"' % (wi.python_launcher(), wi.script_path()))
        self.assertEqual(commande, wi.commands()[0])
        self.assertEqual(wi._script_in_command(commande), wi.script_path())
        self.assertTrue(Path(wi._script_in_command(commande)).is_file())

    def test_icone_par_defaut(self):
        """DefaultIcon = « "<icon.ico>",0 » sur le ProgId et les deux clés magnet."""
        if not Path(wi.icon_path()).is_file():
            self.skipTest("tuploader/assets/icon.ico absent")
        self.enregistrer()
        attendu = sz('"%s",0' % wi.icon_path())
        for cle in (self.progid, self.magnet, self.progid_magnet):
            self.assertEqual(lire(cle + r"\DefaultIcon"), attendu, cle)
        self.assertEqual(lire(self.cap_key, "ApplicationIcon"), sz(wi.icon_path()))

    def test_sans_icone_aucune_cle_d_icone(self):
        """Sans icon.ico : ni DefaultIcon ni ApplicationIcon, et status() n'y voit pas un défaut."""
        absent = str(h.temp_dir("icone-") / "absente.ico")
        with mock.patch.object(wi, "icon_path", return_value=absent):
            self.enregistrer()
            st = self.etat(native_roots=[])
        for cle in (self.progid, self.magnet, self.progid_magnet):
            self.assertFalse(existe(cle + r"\DefaultIcon"), cle)
        self.assertIsNone(lire(self.cap_key, "ApplicationIcon"))
        self.assertTrue(st["icon_ok"])
        self.assertTrue(st["registered"])

    def test_torrent_associe_quand_absent(self):
        """.torrent sans propriétaire : le défaut devient notre ProgId."""
        journal = self.enregistrer()
        self.assertEqual(lire(self.torrent), sz(wi.PROGID_FILE))
        self.assertIn("Fichiers .torrent : associés à %s" % APP_NAME, journal)

    def test_torrent_associe_quand_defaut_vide(self):
        """.torrent existe mais son défaut est vide : il nous revient aussi."""
        ecrire(self.torrent, "")
        ecrire(self.torrent, "application/x-bittorrent", "Content Type")
        journal = self.enregistrer()
        self.assertEqual(lire(self.torrent), sz(wi.PROGID_FILE))
        self.assertEqual(lire(self.torrent, "Content Type"), sz("application/x-bittorrent"))
        self.assertIn("Fichiers .torrent : associés à %s" % APP_NAME, journal)

    def test_torrent_conserve_quand_deja_a_nous(self):
        """.torrent pointe déjà sur notre ProgId (autre copie, ancienne version) : conservé."""
        ecrire(self.torrent, wi.PROGID_FILE)
        ecrire(self.progid + r"\shell\open\command",
               self.commande(r"C:\Ancien dossier\torrent_uploader.py"))
        journal = self.enregistrer()
        self.assertEqual(lire(self.torrent), sz(wi.PROGID_FILE))
        self.assertIn("Fichiers .torrent : associés à %s" % APP_NAME, journal)
        # la commande périmée est remplacée par celle de cette installation
        self.assertEqual(lire(self.progid + r"\shell\open\command"), sz(wi.commands()[0]))
        self.assertTrue(self.etat(native_roots=[])["registered"])

    def test_openwithprogids_valeur_nommee_donnee_vide(self):
        """OpenWithProgids : une valeur NOMMÉE d'après le ProgId, de donnée vide."""
        self.enregistrer()
        self.assertEqual(instantane(self.torrent + r"\OpenWithProgids"),
                         {self.torrent + r"\OpenWithProgids": {wi.PROGID_FILE: sz("")}})
        self.assertIsNone(lire(self.torrent + r"\OpenWithProgids"), "pas de valeur par défaut")

    def test_les_deux_cles_magnet(self):
        """magnet et TorrentUploader.magnet : « URL Protocol » vide et commande en « "%1" »."""
        self.enregistrer()
        for cle in (self.magnet, self.progid_magnet):
            self.assertEqual(lire(cle), sz("URL:Magnet Link"), cle)
            # valeur présente ET vide : absente, Windows ne lance rien
            self.assertEqual(lire(cle, "URL Protocol"), sz(""), cle)
            commande, genre = lire(cle + r"\shell\open\command")
            self.assertEqual(genre, winreg.REG_SZ)
            self.assertTrue(commande.endswith(' "%1"'), commande)
            self.assertEqual(commande, '"%s" "%s" "%%1"' % (wi.python_launcher(), wi.script_path()))
            self.assertNotIn("%L", commande)

    def test_capabilities(self):
        """Capabilities : nom, description, associations de fichier et d'URL."""
        self.enregistrer()
        self.assertEqual(lire(self.cap_key, "ApplicationName"), sz(APP_NAME))
        description, genre = lire(self.cap_key, "ApplicationDescription")
        self.assertEqual(genre, winreg.REG_SZ)
        self.assertGreater(len(description), 20)
        self.assertIn("torrents", description)
        self.assertIn("magnet", description)
        self.assertEqual(instantane(self.cap_key + r"\FileAssociations"),
                         {self.cap_key + r"\FileAssociations": {".torrent": sz(wi.PROGID_FILE)}})
        self.assertEqual(instantane(self.cap_key + r"\UrlAssociations"),
                         {self.cap_key + r"\UrlAssociations": {"magnet": sz(wi.PROGID_MAGNET)}})
        # les ProgIds cités par Capabilities existent bien
        self.assertTrue(existe(self.progid + r"\shell\open\command"))
        self.assertTrue(existe(self.progid_magnet + r"\shell\open\command"))

    def test_registered_applications(self):
        """RegisteredApplications : valeur « TorrentUploader » = chemin de la clé Capabilities."""
        self.enregistrer()
        self.assertEqual(instantane(self.registered_apps),
                         {self.registered_apps: {wi.REGISTERED_NAME: sz(self.cap_key)}})
        self.assertTrue(existe(lire(self.registered_apps, wi.REGISTERED_NAME)[0]))

    def test_journal_en_francais(self):
        """register() rend le journal des actions, une ligne par étape."""
        journal = self.enregistrer()
        self.assertEqual(journal, [
            "Fichiers .torrent : commande d'ouverture enregistrée",
            "Fichiers .torrent : associés à %s" % APP_NAME,
            "Liens magnet : associés à %s" % APP_NAME,
            "Application déclarée dans les « Applications par défaut » de Windows",
            "Extension de navigateur : hôte déclaré pour Chrome",
            "Extension de navigateur : hôte déclaré pour Edge",
        ])

    def test_previent_l_explorateur_une_fois(self):
        """register() notifie le shell (SHCNE_ASSOCCHANGED) une seule fois, à la fin."""
        self.enregistrer()
        _NOTIFY.assert_called_once_with()

    def test_idempotence(self):
        """Deux register() de suite : même registre, mêmes fichiers, même journal."""
        journal1 = self.enregistrer()
        registre1 = instantane()
        fichiers1 = (self.manifeste.read_bytes(), self.bat.read_bytes())
        journal2 = self.enregistrer()
        self.assertEqual(instantane(), registre1)
        self.assertEqual((self.manifeste.read_bytes(), self.bat.read_bytes()), fichiers1)
        self.assertEqual(journal2, journal1)
        self.assertEqual(sorted(f.name for f in paths.native_host_dir().iterdir()),
                         sorted([self.manifeste.name, self.bat.name]))

    def test_n_ecrit_que_sous_les_racines_donnees(self):
        """Rien en dehors de classes / app_key / registered_apps / native_roots ; tout en REG_SZ."""
        self.enregistrer()
        registre = instantane()
        racines = (self.classes, self.app_key, self.registered_apps,
                   SB + r"\Chrome", SB + r"\Edge")
        for cle in registre:
            self.assertTrue(cle == SB or any(cle == r or cle.startswith(r + "\\") for r in racines), cle)
        sous_classes = {cle[len(self.classes) + 1:].split("\\")[0]
                        for cle in registre if cle.startswith(self.classes + "\\")}
        self.assertEqual(sous_classes, {wi.PROGID_FILE, wi.PROGID_MAGNET, ".torrent", "magnet"})
        for cle, valeurs in registre.items():
            for nom, (donnee, genre) in valeurs.items():
                self.assertEqual(genre, winreg.REG_SZ, "%s [%s]" % (cle, nom))
                self.assertIsInstance(donnee, str)
        self.assertEqual(registre[SB], {}, "aucune valeur à la racine")

    def test_sans_python_rien_n_est_ecrit(self):
        """Interpréteur introuvable : IntegrationError AVANT toute écriture."""
        with self.sans_python():
            with self.assertRaises(wi.IntegrationError):
                self.enregistrer()
        self.assertFalse(existe(SB))
        self.assertFalse(self.manifeste.exists())
        self.assertFalse(self.bat.exists())
        _NOTIFY.assert_not_called()

    def test_reparation_d_un_enregistrement_abime(self):
        """register() remet en place ce qui a été abîmé entre-temps."""
        self.enregistrer()
        reference = instantane()
        wi._delete_tree(self.progid_magnet)
        wi._delete_tree(self.cap_key)
        ecrire(self.progid + r"\shell\open\command", "n'importe quoi")
        ecrire(self.progid + r"\DefaultIcon", "ancienne.ico")
        wi._delete_value(self.magnet, "URL Protocol")
        wi._delete_tree(self.native_roots[1][1])
        self.manifeste.unlink()
        self.enregistrer()
        self.assertEqual(instantane(), reference)
        self.assertTrue(self.manifeste.is_file())


# --------------------------------------------------------------------------- #
#  Un autre programme détient déjà les .torrent
# --------------------------------------------------------------------------- #

class TestConcurrent(BacASable):

    def setUp(self):
        super().setUp()
        ecrire(self.torrent, CONCURRENT)
        ecrire(self.torrent, "application/x-bittorrent", "Content Type")
        ecrire(self.torrent + r"\OpenWithProgids", "", CONCURRENT)
        ecrire(self.classes + "\\" + CONCURRENT + r"\shell\open\command", CMD_CONCURRENT)
        self.avant = instantane(self.torrent)
        self.avant.update(instantane(self.classes + "\\" + CONCURRENT))

    def _du_concurrent(self):
        registre = instantane(self.torrent)
        registre.update(instantane(self.classes + "\\" + CONCURRENT))
        return registre

    def test_le_defaut_du_concurrent_n_est_pas_ecrase(self):
        """Le propriétaire existant de .torrent reste le défaut ; le journal le dit."""
        journal = self.enregistrer()
        self.assertEqual(lire(self.torrent), sz(CONCURRENT))
        self.assertEqual(lire(self.torrent, "Content Type"), sz("application/x-bittorrent"))
        self.assertEqual(lire(self.classes + "\\" + CONCURRENT + r"\shell\open\command"),
                         sz(CMD_CONCURRENT))
        ligne = [l for l in journal if CONCURRENT in l]
        self.assertEqual(len(ligne), 1, journal)
        self.assertIn("non modifiée", ligne[0])
        self.assertIn("Ouvrir avec", ligne[0])
        self.assertNotIn("Fichiers .torrent : associés à %s" % APP_NAME, journal)

    def test_ouvrir_avec_est_alimente_quand_meme(self):
        """OpenWithProgids reçoit notre ProgId sans perdre celui du concurrent."""
        self.enregistrer()
        self.assertEqual(instantane(self.torrent + r"\OpenWithProgids"),
                         {self.torrent + r"\OpenWithProgids": {CONCURRENT: sz(""),
                                                                wi.PROGID_FILE: sz("")}})
        # « Ouvrir avec » ne sert que si notre ProgId est lançable
        self.assertEqual(lire(self.progid + r"\shell\open\command"), sz(wi.commands()[0]))

    def test_deux_register_ne_changent_rien_de_plus(self):
        """Idempotence aussi face à un concurrent."""
        self.enregistrer()
        registre = instantane()
        self.enregistrer()
        self.assertEqual(instantane(), registre)
        self.assertEqual(lire(self.torrent), sz(CONCURRENT))

    def test_le_concurrent_survit_a_unregister(self):
        """unregister() ne retire que notre entrée « Ouvrir avec » ; le concurrent est intact."""
        self.enregistrer()
        journal = self.desenregistrer()
        self.assertEqual(self._du_concurrent(), self.avant)
        self.assertEqual(lire(self.torrent), sz(CONCURRENT))
        self.assertIsNone(lire(self.torrent + r"\OpenWithProgids", wi.PROGID_FILE))
        self.assertEqual(lire(self.torrent + r"\OpenWithProgids", CONCURRENT), sz(""))
        self.assertIn("Retirée de la liste « Ouvrir avec »", journal)
        self.assertNotIn("Association des fichiers .torrent retirée", journal)
        self.assertFalse(existe(self.progid))

    def test_unregister_sans_register_prealable(self):
        """unregister() seul, face à un concurrent : rien de ce qui est à lui ne bouge."""
        self.desenregistrer()
        self.assertEqual(self._du_concurrent(), self.avant)

    def test_status_face_a_un_concurrent(self):
        """Enregistrée mais pas par défaut : inactif pour .torrent, listée dans « Ouvrir avec »."""
        self.enregistrer()
        st = self.etat(native_roots=[])
        self.assertTrue(st["installed"])
        self.assertTrue(st["registered"])
        self.assertFalse(st["torrent_active"])
        self.assertTrue(st["magnet_active"])
        self.assertEqual(st["torrent_owner"], CONCURRENT)
        self.assertTrue(st["open_with_listed"])
        self.assertFalse(st["stale"])
        self.assertFalse(st["foreign"])
        # à jour : register() ne ferait rien de plus (sinon ensure_current() le relancerait sans fin)
        self.assertTrue(st["up_to_date"])
        self.assertEqual(wi.describe(st),
                         "Fichiers .torrent : inactif (géré par « %s ») · liens magnet : actif"
                         % CONCURRENT)

    def test_unregister_n_emporte_pas_le_magnet_d_un_concurrent(self):
        """« Et rien d'autre » : ce qu'un concurrent avait écrit sous magnet survit à unregister()."""
        ecrire(self.magnet, "URL:AutreClient")
        ecrire(self.magnet, "", "URL Protocol")
        ecrire(self.magnet + r"\shell\open\command", CMD_CONCURRENT)
        ecrire(self.magnet + r"\shell\ajouter\command", r'"C:\AutreClient\client.exe" --ajouter "%1"')
        self.enregistrer()
        self.desenregistrer()
        self.assertEqual(lire(self.magnet + r"\shell\ajouter\command"),
                         sz(r'"C:\AutreClient\client.exe" --ajouter "%1"'))


class TestTiersSansDefaut(BacASable):

    def test_unregister_n_emporte_pas_les_entrees_d_un_tiers(self):
        """« Et rien d'autre » : les valeurs d'un tiers sous .torrent survivent à unregister()."""
        ecrire(self.torrent, "application/x-bittorrent", "Content Type")
        ecrire(self.torrent + r"\OpenWithProgids", "", "Tiers.Torrent")
        self.enregistrer()
        self.assertEqual(lire(self.torrent), sz(wi.PROGID_FILE))     # défaut absent : il nous revient
        self.desenregistrer()
        self.assertEqual(lire(self.torrent + r"\OpenWithProgids", "Tiers.Torrent"), sz(""))
        self.assertEqual(lire(self.torrent, "Content Type"), sz("application/x-bittorrent"))
        self.assertIsNone(lire(self.torrent + r"\OpenWithProgids", wi.PROGID_FILE))
        self.assertIsNone(lire(self.torrent))


# --------------------------------------------------------------------------- #
#  Hôte de messagerie native
# --------------------------------------------------------------------------- #

class TestHoteNatif(BacASable):

    def test_emplacement_des_fichiers(self):
        """Manifeste et .bat vivent dans paths.native_host_dir(), donc dans le bac à sable."""
        self.enregistrer()
        dossier = paths.native_host_dir()
        self.assertEqual(self.manifeste, dossier / (wi.HOST_NAME + ".json"))
        self.assertEqual(self.bat.parent, dossier)
        self.assertEqual(self.bat.suffix, ".bat")
        self.assertIn(h.SANDBOX, self.manifeste.parents)
        self.assertTrue(self.manifeste.is_file())
        self.assertTrue(self.bat.is_file())

    def test_manifeste(self):
        """Manifeste : nom de l'hôte, type stdio, origines des extensions, chemin du .bat."""
        self.enregistrer()
        manifeste = json.loads(self.manifeste.read_text(encoding="utf-8"))
        self.assertEqual(set(manifeste), {"name", "description", "path", "type", "allowed_origins"})
        self.assertEqual(manifeste["name"], wi.HOST_NAME)
        self.assertEqual(manifeste["type"], "stdio")
        self.assertEqual(manifeste["allowed_origins"],
                         ["chrome-extension://%s/" % ext for ext in wi.EXTENSION_IDS])
        self.assertEqual(manifeste["path"], str(self.bat))
        self.assertTrue(os.path.isabs(manifeste["path"]))
        self.assertTrue(Path(manifeste["path"]).is_file())
        self.assertIsInstance(manifeste["description"], str)
        self.assertTrue(manifeste["description"])
        self.assertEqual(manifeste, wi._host_manifest(self.bat))
        self.assertFalse(self.manifeste.read_bytes().startswith(b"\xef\xbb\xbf"), "pas de BOM")

    def test_bat_contenu(self):
        """.bat : python.exe (pas pythonw) + native_host.py + %*, précédé de la ligne du bac à sable."""
        self.enregistrer()
        texte = self.bat.read_bytes().decode("utf-8")
        lignes = texte.split("\r\n")
        self.assertEqual(lignes[-1], "", "le fichier se termine par un saut de ligne")
        lignes = lignes[:-1]
        self.assertEqual(lignes[0], "@echo off")
        self.assertIn("chcp 65001 >nul", lignes)                 # chemins accentués
        # tests en bac à sable : l'hôte doit viser les mêmes données que l'application
        ligne_env = 'set "%s=%s"' % (paths.ENV_DATA_DIR, os.environ[paths.ENV_DATA_DIR])
        self.assertIn(ligne_env, lignes)
        self.assertEqual(os.environ[paths.ENV_DATA_DIR], str(h.SANDBOX))
        self.assertLess(lignes.index(ligne_env), len(lignes) - 1, "avant la ligne de lancement")
        lancement = re.fullmatch(r'"([^"]+)" "([^"]+)" %\*', lignes[-1])
        self.assertIsNotNone(lancement, lignes[-1])
        python, script = lancement.groups()
        self.assertEqual(os.path.basename(python).lower(), "python.exe")     # stdin/stdout requis
        self.assertNotIn("pythonw", lignes[-1].lower())
        self.assertEqual(python, wi.python_console())
        self.assertEqual(os.path.basename(script), "native_host.py")
        self.assertEqual(script, wi.host_script_path())
        self.assertTrue(Path(python).is_file())
        self.assertTrue(Path(script).is_file())
        self.assertEqual(len(lignes), 4)

    def test_bat_fins_de_ligne_crlf(self):
        """.bat : uniquement des fins de ligne CRLF, UTF-8 sans BOM."""
        self.enregistrer()
        brut = self.bat.read_bytes()
        self.assertFalse(brut.startswith(b"\xef\xbb\xbf"), "un BOM casserait « @echo off »")
        self.assertTrue(brut.endswith(b"\r\n"))
        self.assertEqual(brut.count(b"\n"), brut.count(b"\r\n"), "LF sans CR")
        self.assertEqual(brut.count(b"\r"), brut.count(b"\r\n"), "CR sans LF")
        self.assertNotIn(b"\r\r\n", brut)
        self.assertGreaterEqual(brut.count(b"\r\n"), 3)
        self.assertEqual(brut, wi._host_bat_content().encode("utf-8"))

    def test_bat_sans_ligne_d_environnement_hors_bac_a_sable(self):
        """En production (pas de bac à sable), le .bat ne fixe aucune variable d'environnement."""
        with mock.patch.object(wi, "is_sandboxed", return_value=False):
            contenu = wi._host_bat_content()             # fonction pure : n'écrit rien
        self.assertNotIn("set ", contenu)
        self.assertNotIn(paths.ENV_DATA_DIR, contenu)
        self.assertEqual(contenu.split("\r\n")[:2], ["@echo off", "chcp 65001 >nul"])
        self.assertTrue(contenu.endswith(' %*\r\n'))
        self.assertIn(paths.ENV_DATA_DIR, wi._host_bat_content())   # et bien présente en bac à sable

    def test_registre_de_chaque_navigateur(self):
        """La valeur par défaut de chaque racine native = chemin du manifeste."""
        self.enregistrer()
        for nom, cle in self.native_roots:
            self.assertEqual(lire(cle), sz(str(self.manifeste)), nom)
            self.assertEqual(instantane(cle), {cle: {"": sz(str(self.manifeste))}})
            self.assertTrue(cle.endswith("\\" + wi.HOST_NAME))

    def test_register_native_host_journal(self):
        """register_native_host() : une ligne par navigateur déclaré."""
        journal = wi.register_native_host(self.native_roots)
        self.assertEqual(journal, ["Extension de navigateur : hôte déclaré pour Chrome",
                                   "Extension de navigateur : hôte déclaré pour Edge"])
        self.assertEqual(lire(self.native_roots[0][1]), sz(str(self.manifeste)))

    def test_register_native_host_sans_navigateur(self):
        """Aucun navigateur : les fichiers sont écrits, le registre est intact, le journal le dit."""
        journal = wi.register_native_host([])
        self.assertEqual(journal, ["Extension de navigateur : aucun navigateur compatible détecté"])
        self.assertTrue(self.manifeste.is_file())
        self.assertTrue(self.bat.is_file())
        self.assertFalse(existe(SB))

    def test_un_seul_navigateur(self):
        """Seules les racines fournies sont écrites."""
        chrome = self.native_roots[:1]
        self.enregistrer(native_roots=chrome)
        self.assertEqual(lire(chrome[0][1]), sz(str(self.manifeste)))
        self.assertFalse(existe(SB + r"\Edge"))
        hote = wi.native_host_status(chrome)
        self.assertEqual(hote["browsers"], ["Chrome"])
        self.assertEqual(hote["declared"], ["Chrome"])

    def test_statut_avant_tout_enregistrement(self):
        """Rien d'enregistré : applicable (des navigateurs existent) mais ni déclaré ni prêt."""
        hote = wi.native_host_status(self.native_roots)
        self.assertEqual(hote, {"browsers": ["Chrome", "Edge"], "declared": [],
                                "ok": False, "applicable": True})
        self.assertFalse(existe(SB), "native_host_status() ne doit rien écrire")

    def test_statut_sans_navigateur(self):
        """Aucun navigateur : non applicable, jamais « ok », même avec les fichiers en place."""
        self.enregistrer()
        hote = wi.native_host_status([])
        self.assertEqual(hote, {"browsers": [], "declared": [], "ok": False, "applicable": False})

    def test_statut_declare_apres_register(self):
        """Après register() : tous les navigateurs sont déclarés et les fichiers sont exacts."""
        self.enregistrer()
        hote = wi.native_host_status(self.native_roots)
        self.assertEqual(hote["browsers"], ["Chrome", "Edge"])
        self.assertEqual(hote["declared"], ["Chrome", "Edge"])
        self.assertTrue(hote["applicable"])
        # les deux fichiers sont, octet pour octet, ceux que le produit attend
        self.assertEqual(json.loads(self.manifeste.read_text(encoding="utf-8")),
                         wi._host_manifest(self.bat))
        self.assertEqual(self.bat.read_bytes(), wi._host_bat_content().encode("utf-8"))

    def test_statut_ok_juste_apres_register(self):
        """Juste après register(), l'hôte natif est « ok »."""
        self.enregistrer()
        self.assertTrue(wi.native_host_status(self.native_roots)["ok"])

    def test_statut_pas_ok_sans_manifeste(self):
        """Manifeste supprimé : plus « ok », bien que le registre le déclare encore."""
        self.enregistrer()
        self.manifeste.unlink()
        hote = wi.native_host_status(self.native_roots)
        self.assertFalse(hote["ok"])
        self.assertEqual(hote["declared"], ["Chrome", "Edge"])
        self.assertTrue(hote["applicable"])

    def test_statut_pas_ok_si_manifeste_modifie_ou_illisible(self):
        """Manifeste altéré (autre extension, JSON cassé) : plus « ok », sans exception."""
        self.enregistrer()
        bon = json.loads(self.manifeste.read_text(encoding="utf-8"))
        for contenu in (json.dumps(dict(bon, allowed_origins=["chrome-extension://%s/" % ("a" * 32)])),
                        json.dumps(dict(bon, path=str(self.bat) + ".ancien")),
                        "{ ceci n'est pas du JSON", ""):
            self.manifeste.write_text(contenu, encoding="utf-8")
            self.assertFalse(wi.native_host_status(self.native_roots)["ok"], contenu)

    def test_statut_pas_ok_si_bat_modifie_ou_absent(self):
        """.bat édité (ancien Python, ancien dossier) ou supprimé : plus « ok »."""
        self.enregistrer()
        original = self.bat.read_bytes()
        self.bat.write_bytes(original.replace(b"native_host.py", b"ancien_host.py"))
        self.assertFalse(wi.native_host_status(self.native_roots)["ok"])
        self.bat.write_bytes(original + b"rem ajout\r\n")
        self.assertFalse(wi.native_host_status(self.native_roots)["ok"])
        self.bat.unlink()
        self.assertFalse(wi.native_host_status(self.native_roots)["ok"])

    def test_statut_pas_ok_si_un_navigateur_pointe_ailleurs(self):
        """Une racine qui vise un autre manifeste (ou a disparu) n'est plus « déclarée »."""
        self.enregistrer()
        ecrire(self.native_roots[1][1], r"C:\Ancien dossier\com.torrentuploader.host.json")
        hote = wi.native_host_status(self.native_roots)
        self.assertEqual(hote["declared"], ["Chrome"])
        self.assertEqual(hote["browsers"], ["Chrome", "Edge"])
        self.assertFalse(hote["ok"])
        wi._delete_tree(self.native_roots[0][1])
        hote = wi.native_host_status(self.native_roots)
        self.assertEqual(hote["declared"], [])
        self.assertFalse(hote["ok"])

    def test_statut_sans_python_ne_plante_pas(self):
        """python.exe introuvable : le statut répond « pas ok » au lieu de lever une exception."""
        self.enregistrer()
        with self.sans_python():
            hote = wi.native_host_status(self.native_roots)
        self.assertFalse(hote["ok"])
        self.assertEqual(hote["declared"], ["Chrome", "Edge"])

    def test_unregister_native_host(self):
        """unregister_native_host() retire les racines, les deux fichiers, et le journalise."""
        self.enregistrer()
        journal = wi.unregister_native_host(self.native_roots)
        self.assertEqual(journal, ["Hôte de l'extension retiré pour Chrome",
                                   "Hôte de l'extension retiré pour Edge"])
        for _, cle in self.native_roots:
            self.assertFalse(existe(cle))
        self.assertFalse(self.manifeste.exists())
        self.assertFalse(self.bat.exists())
        self.assertTrue(paths.native_host_dir().is_dir())
        # une seconde fois : plus rien à retirer, aucune erreur
        self.assertEqual(wi.unregister_native_host(self.native_roots), [])

    def test_unregister_native_host_epargne_les_autres_hotes(self):
        """L'hôte natif d'une autre application, dans la même clé de navigateur, reste en place."""
        autre = SB + r"\Chrome\NativeMessagingHosts\com.autre.hote"
        ecrire(autre, r"C:\Autre\hote.json")
        self.enregistrer()
        wi.unregister_native_host(self.native_roots)
        self.assertEqual(lire(autre), sz(r"C:\Autre\hote.json"))


# --------------------------------------------------------------------------- #
#  unregister()
# --------------------------------------------------------------------------- #

class TestUnregister(BacASable):

    def _planter_des_cles_etrangeres(self):
        ecrire(self.classes + r"\AutreAppli.Document\shell\open\command", r'"C:\Autre\autre.exe" "%1"')
        ecrire(self.classes + r"\.mkv", "AutreAppli.Video")
        ecrire(self.classes + r"\.mkv\OpenWithProgids", "", "AutreAppli.Video")
        ecrire(self.registered_apps, r"Software\AutreAppli\Capabilities", "AutreAppli")
        ecrire(SB + r"\AutreAppli\Capabilities", "Autre", "ApplicationName")
        ecrire(SB + r"\Chrome\NativeMessagingHosts\com.autre.hote", r"C:\Autre\hote.json")
        ecrire(SB + r"\Edge\NativeMessagingHosts\com.autre.hote", r"C:\Autre\hote.json")
        ecrire(SB, "valeur", "Valeur étrangère à la racine")

    def test_retire_tout_ce_que_register_a_ecrit(self):
        """ProgIds, .torrent, magnet, Capabilities, RegisteredApplications, hôte natif : retirés."""
        self.enregistrer()
        journal = self.desenregistrer()
        for cle in (self.progid, self.progid_magnet, self.torrent, self.magnet,
                    self.app_key, self.cap_key):
            self.assertFalse(existe(cle), cle)
        self.assertIsNone(lire(self.registered_apps, wi.REGISTERED_NAME))
        for _, cle in self.native_roots:
            self.assertFalse(existe(cle), cle)
        self.assertFalse(self.manifeste.exists())
        self.assertFalse(self.bat.exists())
        self.assertEqual(journal, [
            "Association des fichiers .torrent retirée",
            "Association des liens magnet retirée",
            "Déclaration « Applications par défaut » retirée",
            "Hôte de l'extension retiré pour Chrome",
            "Hôte de l'extension retiré pour Edge",
        ])
        st = self.etat()
        self.assertFalse(st["installed"])
        self.assertEqual(wi.describe(st), "Non activée.")

    def test_il_ne_reste_que_des_cles_conteneurs_vides(self):
        """Sur un registre vierge : register() puis unregister() ne laissent aucune valeur."""
        self.enregistrer()
        self.desenregistrer()
        restes = instantane()
        conteneurs = {SB, self.classes, self.registered_apps,
                      SB + r"\Chrome", SB + r"\Chrome\NativeMessagingHosts",
                      SB + r"\Edge", SB + r"\Edge\NativeMessagingHosts"}
        self.assertTrue(set(restes) <= conteneurs, sorted(set(restes) - conteneurs))
        for cle, valeurs in restes.items():
            self.assertEqual(valeurs, {}, cle)

    def test_rien_d_autre_que_ce_que_register_a_cree(self):
        """Les clés étrangères plantées avant register() sont intactes après unregister()."""
        self._planter_des_cles_etrangeres()
        etranger = paths.native_host_dir() / "autre-hote.json"
        etranger.write_text("{}", encoding="utf-8")
        avant = instantane()
        self.enregistrer()
        self.assertNotEqual(instantane(), avant)
        for cle, valeurs in avant.items():                       # register() non plus n'y touche pas
            for nom, valeur in valeurs.items():
                self.assertEqual(lire(cle, nom), valeur, "%s [%s]" % (cle, nom))
        self.desenregistrer()
        self.assertEqual(instantane(), avant)
        self.assertTrue(etranger.is_file(), "fichier étranger du dossier de l'hôte natif supprimé")
        self.assertEqual(sorted(f.name for f in paths.native_host_dir().iterdir()), [etranger.name])

    def test_magnet_conserve_s_il_ne_pointe_plus_vers_nous(self):
        """Un autre programme a repris magnet : sa clé reste, seul notre ProgId magnet part."""
        self.enregistrer()
        ecrire(self.magnet + r"\shell\open\command", CMD_CONCURRENT)
        journal = self.desenregistrer()
        self.assertEqual(lire(self.magnet + r"\shell\open\command"), sz(CMD_CONCURRENT))
        self.assertEqual(lire(self.magnet, "URL Protocol"), sz(""))
        self.assertNotIn("Association des liens magnet retirée", journal)
        self.assertFalse(existe(self.progid_magnet))
        self.assertFalse(existe(self.progid))

    def test_magnet_retire_s_il_pointe_vers_nous(self):
        """magnet pointe vers torrent_uploader.py : la clé est retirée et le journal le dit."""
        self.enregistrer()
        self.assertIn("torrent_uploader.py", lire(self.magnet + r"\shell\open\command")[0])
        journal = self.desenregistrer()
        self.assertFalse(existe(self.magnet))
        self.assertIn("Association des liens magnet retirée", journal)

    def test_sur_un_registre_vierge(self):
        """unregister() sans rien d'enregistré : aucune erreur, aucune clé créée."""
        journal = self.desenregistrer()
        self.assertIsInstance(journal, list)
        self.assertFalse(existe(SB))
        self.assertNotIn("Association des fichiers .torrent retirée", journal)
        self.assertNotIn("Association des liens magnet retirée", journal)
        self.assertFalse([l for l in journal if "Hôte" in l], journal)

    def test_deux_unregister_de_suite(self):
        """Un second unregister() est sans effet et sans erreur."""
        self._planter_des_cles_etrangeres()
        avant = instantane()
        self.enregistrer()
        self.desenregistrer()
        self.desenregistrer()
        self.assertEqual(instantane(), avant)

    def test_previent_l_explorateur_une_fois(self):
        """unregister() notifie le shell une seule fois."""
        self.enregistrer()
        _NOTIFY.reset_mock()
        self.desenregistrer()
        _NOTIFY.assert_called_once_with()

    def test_cycle_complet_rejouable(self):
        """register → unregister → register redonne exactement le même enregistrement."""
        self.enregistrer()
        reference = instantane()
        self.desenregistrer()
        self.enregistrer()
        self.assertEqual(instantane(), reference)


# --------------------------------------------------------------------------- #
#  status() et describe()
# --------------------------------------------------------------------------- #

class TestStatus(BacASable):

    CLES = {"supported", "installed", "registered", "foreign", "foreign_script", "stale",
            "torrent_active", "magnet_active", "torrent_owner", "user_choice",
            "magnet_user_choice", "open_with_listed", "native_host", "icon_ok", "up_to_date",
            "effective_command", "effective_magnet_command", "expected_command",
            "expected_magnet_command", "launcher", "script"}

    def test_non_installee(self):
        """Rien d'enregistré : tout est faux et describe() dit « Non activée. »."""
        st = self.etat()
        self.assertTrue(self.CLES <= set(st), self.CLES - set(st))
        self.assertTrue(st["supported"])
        for cle in ("installed", "registered", "foreign", "stale", "torrent_active",
                    "magnet_active", "open_with_listed", "up_to_date"):
            self.assertIs(st[cle], False, cle)
        self.assertIsNone(st["torrent_owner"])
        self.assertEqual(st["foreign_script"], "")
        self.assertEqual(st["native_host"], {"browsers": ["Chrome", "Edge"], "declared": [],
                                             "ok": False, "applicable": True})
        self.assertEqual(wi.describe(st), "Non activée.")

    def test_enregistree(self):
        """Après register() : installée, enregistrée, .torrent et magnet actifs."""
        self.enregistrer()
        st = self.etat()
        self.assertTrue(self.CLES <= set(st), self.CLES - set(st))
        for cle in ("supported", "installed", "registered", "torrent_active", "magnet_active",
                    "open_with_listed", "icon_ok"):
            self.assertIs(st[cle], True, cle)
        self.assertIs(st["foreign"], False)
        self.assertIs(st["stale"], False)
        self.assertEqual(st["foreign_script"], "")
        self.assertEqual(st["torrent_owner"], wi.PROGID_FILE)
        self.assertIsNone(st["user_choice"])
        self.assertIsNone(st["magnet_user_choice"])
        self.assertEqual(st["expected_command"], lire(self.progid + r"\shell\open\command")[0])
        self.assertEqual(st["expected_magnet_command"], lire(self.magnet + r"\shell\open\command")[0])
        self.assertEqual((st["expected_command"], st["expected_magnet_command"]), wi.commands())
        self.assertEqual(st["launcher"], wi.python_launcher())
        self.assertEqual(st["script"], wi.script_path())
        self.assertEqual(st["native_host"]["declared"], ["Chrome", "Edge"])
        self.assertEqual(st["native_host"]["browsers"], ["Chrome", "Edge"])
        self.assertTrue(st["native_host"]["applicable"])

    def test_describe_enregistree_sans_navigateur(self):
        """Enregistrée, aucun navigateur : phrase courte, sans mention de l'extension."""
        self.enregistrer()
        st = self.etat(native_roots=[])
        self.assertEqual(wi.describe(st), "Fichiers .torrent : actif · liens magnet : actif")

    def test_describe_enregistree_avec_navigateurs(self):
        """Enregistrée, navigateurs présents : la phrase parle aussi de l'extension."""
        self.enregistrer()
        phrase = wi.describe(self.etat())
        self.assertTrue(phrase.startswith("Fichiers .torrent : actif · liens magnet : actif · "
                                          "extension de navigateur : "), phrase)

    def test_commande_effective_vient_du_vrai_shell(self):
        """effective_command reflète le shell réel (lecture seule), pas le bac à sable."""
        self.enregistrer()
        st = self.etat()
        self.assertEqual(st["effective_command"], wi.assoc_command(".torrent"))
        self.assertEqual(st["effective_magnet_command"], wi.assoc_command("magnet"))

    def test_status_n_ecrit_rien(self):
        """status() est en lecture seule : registre et fichiers inchangés, vierge ou enregistré."""
        self.etat()
        self.assertFalse(existe(SB))
        self.assertEqual(list(paths.native_host_dir().iterdir()), [])
        self.enregistrer()
        avant = instantane()
        fichiers = (self.manifeste.read_bytes(), self.bat.read_bytes())
        _NOTIFY.reset_mock()
        self.etat()
        self.etat(native_roots=[])
        self.assertEqual(instantane(), avant)
        self.assertEqual((self.manifeste.read_bytes(), self.bat.read_bytes()), fichiers)
        _NOTIFY.assert_not_called()

    # -- périmé / autre copie -------------------------------------------------- #

    def test_perime_script_disparu(self):
        """La commande vise un script qui n'existe plus : périmé (stale), pas « autre copie »."""
        self.enregistrer()
        absent = h.temp_dir("ancien-") / "dossier supprimé" / "torrent_uploader.py"
        ecrire(self.progid + r"\shell\open\command", self.commande(absent))
        st = self.etat(native_roots=[])
        self.assertIs(st["installed"], True)
        self.assertIs(st["registered"], False)
        self.assertIs(st["foreign"], False)
        self.assertEqual(st["foreign_script"], "")
        self.assertIs(st["stale"], True)
        self.assertIs(st["torrent_active"], False)
        self.assertIs(st["magnet_active"], False)
        self.assertIs(st["up_to_date"], False)
        self.assertEqual(wi.describe(st), "Enregistrement à mettre à jour : cliquez sur « Activer ».")

    def test_perime_python_deplace(self):
        """Même script mais autre pythonw.exe (Python réinstallé ailleurs) : périmé."""
        self.enregistrer()
        ecrire(self.progid + r"\shell\open\command",
               self.commande(wi.script_path(), lanceur=r"C:\Ancien Python\pythonw.exe"))
        st = self.etat(native_roots=[])
        self.assertIs(st["registered"], False)
        self.assertIs(st["foreign"], False)
        self.assertIs(st["stale"], True)
        self.assertIs(st["up_to_date"], False)

    def test_perime_commande_sans_guillemets(self):
        """Commande illisible (sans guillemets) : périmé, sans exception."""
        ecrire(self.progid + r"\shell\open\command", r"C:\outil.exe %1")
        st = self.etat(native_roots=[])
        self.assertIs(st["installed"], True)
        self.assertIs(st["stale"], True)
        self.assertIs(st["foreign"], False)

    def test_autre_copie(self):
        """La commande vise un AUTRE torrent_uploader.py existant : « foreign »."""
        self.enregistrer()
        autre = h.temp_dir("autre-copie-") / "torrent_uploader.py"
        autre.write_text("# autre copie de l'application\n", encoding="utf-8")
        ecrire(self.progid + r"\shell\open\command", self.commande(autre))
        st = self.etat(native_roots=[])
        self.assertIs(st["installed"], True)
        self.assertIs(st["registered"], False)
        self.assertIs(st["foreign"], True)
        self.assertEqual(st["foreign_script"], str(autre))
        self.assertIs(st["stale"], False)
        self.assertIs(st["torrent_active"], False)
        self.assertIs(st["magnet_active"], False)
        self.assertIs(st["up_to_date"], False)
        self.assertEqual(wi.describe(st),
                         "Une autre copie de l'application est enregistrée (%s)." % autre)

    def test_meme_script_a_la_casse_pres_n_est_pas_une_autre_copie(self):
        """Le même script écrit avec une autre casse n'est pas pris pour une autre copie."""
        self.enregistrer()
        ecrire(self.progid + r"\shell\open\command", self.commande(wi.script_path().upper()))
        st = self.etat(native_roots=[])
        self.assertIs(st["foreign"], False)
        self.assertEqual(st["foreign_script"], "")

    # -- up_to_date ------------------------------------------------------------ #

    def test_a_jour_sans_navigateur(self):
        """Enregistrée, icône en place, magnet à nous, hôte non applicable : à jour."""
        self.enregistrer()
        st = self.etat(native_roots=[])
        self.assertIs(st["native_host"]["applicable"], False)
        self.assertIs(st["up_to_date"], True)

    def test_a_jour_juste_apres_register_avec_navigateurs(self):
        """Juste après register(), tout est à jour et l'extension est « prête »."""
        self.enregistrer()
        st = self.etat()
        self.assertIs(st["native_host"]["ok"], True)
        self.assertIs(st["up_to_date"], True)
        self.assertEqual(wi.describe(st), "Fichiers .torrent : actif · liens magnet : actif · "
                                          "extension de navigateur : prête")

    def test_pas_a_jour_si_l_hote_natif_est_incomplet(self):
        """Navigateurs présents mais manifeste absent : pas à jour, extension « à activer »."""
        self.enregistrer()
        self.manifeste.unlink()
        st = self.etat()
        self.assertIs(st["registered"], True)
        self.assertIs(st["native_host"]["ok"], False)
        self.assertIs(st["up_to_date"], False)
        self.assertEqual(wi.describe(st), "Fichiers .torrent : actif · liens magnet : actif · "
                                          "extension de navigateur : à activer")
        # sans navigateur, le même état de disque ne gêne pas
        self.assertIs(self.etat(native_roots=[])["up_to_date"], True)

    def test_pas_a_jour_si_l_icone_a_change(self):
        """DefaultIcon différent de l'icône livrée : icon_ok et up_to_date passent à faux."""
        if not Path(wi.icon_path()).is_file():
            self.skipTest("tuploader/assets/icon.ico absent")
        self.enregistrer()
        ecrire(self.progid + r"\DefaultIcon", r'"C:\Ancien dossier\icon.ico",0')
        st = self.etat(native_roots=[])
        self.assertIs(st["icon_ok"], False)
        self.assertIs(st["registered"], True)
        self.assertIs(st["up_to_date"], False)
        wi._delete_tree(self.progid + r"\DefaultIcon")
        st = self.etat(native_roots=[])
        self.assertIs(st["icon_ok"], False)
        self.assertIs(st["up_to_date"], False)

    def test_pas_a_jour_si_magnet_a_ete_repris(self):
        """magnet repris par un autre programme (sans choix explicite) : inactif, pas à jour."""
        self.enregistrer()
        ecrire(self.magnet + r"\shell\open\command", CMD_CONCURRENT)
        st = self.etat(native_roots=[])
        self.assertIs(st["registered"], True)
        self.assertIs(st["torrent_active"], True)
        self.assertIs(st["magnet_active"], False)
        self.assertIs(st["up_to_date"], False)
        self.assertEqual(wi.describe(st), "Fichiers .torrent : actif · liens magnet : inactif")

    def test_a_jour_si_l_utilisateur_a_choisi_un_autre_gestionnaire_magnet(self):
        """Choix explicite d'un autre programme pour magnet : on ne le lui dispute pas."""
        self.enregistrer()
        ecrire(self.magnet + r"\shell\open\command", CMD_CONCURRENT)
        self.choix[wi.MAGNET_USERCHOICE] = "AutreClient.Magnet"
        st = self.etat(native_roots=[])
        self.assertEqual(st["magnet_user_choice"], "AutreClient.Magnet")
        self.assertIs(st["magnet_active"], False)
        self.assertIs(st["up_to_date"], True)

    def test_a_jour_meme_si_torrent_supprime(self):
        """La clé .torrent a disparu : inactif, mais sans exception."""
        self.enregistrer()
        wi._delete_tree(self.torrent)
        st = self.etat(native_roots=[])
        self.assertIs(st["registered"], True)
        self.assertIs(st["torrent_active"], False)
        self.assertIsNone(st["torrent_owner"])
        self.assertIs(st["open_with_listed"], False)
        self.assertIsInstance(wi.describe(st), str)

    # -- choix explicite de l'utilisateur (UserChoice) -------------------------- #

    def test_choix_utilisateur_d_un_concurrent(self):
        """UserChoice désigne un autre programme : inactif, et describe() nomme ce programme."""
        self.enregistrer()
        self.choix[wi.FILEEXTS_USERCHOICE] = CONCURRENT
        st = self.etat(native_roots=[])
        self.assertEqual(st["user_choice"], CONCURRENT)
        self.assertEqual(st["torrent_owner"], wi.PROGID_FILE)
        self.assertIs(st["torrent_active"], False)
        self.assertIs(st["magnet_active"], True)
        self.assertEqual(wi.describe(st),
                         "Fichiers .torrent : inactif (géré par « %s ») · liens magnet : actif"
                         % CONCURRENT)

    def test_choix_utilisateur_magnet_d_un_concurrent(self):
        """UserChoice magnet désigne un autre programme : liens magnet inactifs."""
        self.enregistrer()
        self.choix[wi.MAGNET_USERCHOICE] = "AutreClient.Magnet"
        st = self.etat(native_roots=[])
        self.assertIs(st["magnet_active"], False)
        self.assertIs(st["torrent_active"], True)
        self.assertEqual(wi.describe(st), "Fichiers .torrent : actif · liens magnet : inactif")

    def test_choix_utilisateur_en_notre_faveur_torrent(self):
        """L'utilisateur a choisi Torrent Uploader pour les .torrent : c'est actif."""
        self.enregistrer()
        self.choix[wi.FILEEXTS_USERCHOICE] = wi.PROGID_FILE
        st = self.etat(native_roots=[])
        self.assertIs(st["torrent_active"], True)
        self.assertTrue(wi.describe(st).startswith("Fichiers .torrent : actif"))

    def test_choix_utilisateur_en_notre_faveur_magnet(self):
        """L'utilisateur a choisi Torrent Uploader pour magnet : c'est actif."""
        self.enregistrer()
        self.choix[wi.MAGNET_USERCHOICE] = wi.PROGID_MAGNET
        st = self.etat(native_roots=[])
        self.assertIs(st["magnet_active"], True)
        self.assertIn("liens magnet : actif", wi.describe(st))

    def test_describe_sans_proprietaire_n_affiche_pas_none(self):
        """Sans propriétaire connu, la phrase ne doit pas contenir « None »."""
        self.enregistrer()
        wi._delete_tree(self.torrent)
        phrase = wi.describe(self.etat(native_roots=[]))
        self.assertIn("Fichiers .torrent : inactif", phrase)
        self.assertNotIn("None", phrase)

    # -- erreur ------------------------------------------------------------------ #

    def test_erreur_si_python_introuvable(self):
        """Pas d'interpréteur utilisable : status() rend l'erreur, describe() l'affiche."""
        self.enregistrer()
        with self.sans_python():
            st = self.etat()
        self.assertEqual(set(st), {"supported", "error"})
        self.assertIs(st["supported"], True)
        self.assertIn("pythonw.exe", st["error"])
        self.assertIn("introuvable", st["error"])
        self.assertEqual(wi.describe(st), st["error"])


class TestDescribe(unittest.TestCase):
    """describe() sur des états construits à la main : une phrase française par cas."""

    BASE = {"supported": True, "installed": True, "registered": True, "foreign": False,
            "foreign_script": "", "stale": False, "torrent_active": True, "magnet_active": True,
            "torrent_owner": wi.PROGID_FILE, "user_choice": None,
            "native_host": {"browsers": [], "declared": [], "ok": False, "applicable": False}}

    def etat(self, **modifs):
        return dict(self.BASE, **modifs)

    def test_hors_windows(self):
        self.assertEqual(wi.describe({"supported": False}),
                         "Intégration disponible uniquement sous Windows.")
        self.assertEqual(wi.describe({}), "Intégration disponible uniquement sous Windows.")

    def test_erreur(self):
        message = "pythonw.exe est introuvable. Installez Python depuis python.org."
        self.assertEqual(wi.describe({"supported": True, "error": message}), message)

    def test_non_activee(self):
        self.assertEqual(wi.describe({"supported": True, "installed": False}), "Non activée.")

    def test_autre_copie_prioritaire(self):
        """« foreign » l'emporte sur le reste et cite le script de l'autre copie."""
        phrase = wi.describe(self.etat(registered=False, foreign=True,
                                       foreign_script=r"D:\Autre\torrent_uploader.py"))
        self.assertEqual(phrase, r"Une autre copie de l'application est enregistrée "
                                 r"(D:\Autre\torrent_uploader.py).")

    def test_perimee(self):
        phrase = wi.describe(self.etat(registered=False, stale=True,
                                       torrent_active=False, magnet_active=False))
        self.assertEqual(phrase, "Enregistrement à mettre à jour : cliquez sur « Activer ».")

    def test_tout_actif(self):
        self.assertEqual(wi.describe(self.etat()), "Fichiers .torrent : actif · liens magnet : actif")

    def test_extension_prete_ou_a_activer(self):
        prete = self.etat(native_host={"applicable": True, "ok": True})
        a_activer = self.etat(native_host={"applicable": True, "ok": False})
        self.assertEqual(wi.describe(prete), "Fichiers .torrent : actif · liens magnet : actif · "
                                             "extension de navigateur : prête")
        self.assertEqual(wi.describe(a_activer), "Fichiers .torrent : actif · liens magnet : actif · "
                                                 "extension de navigateur : à activer")

    def test_sans_cle_native_host(self):
        """Un état sans « native_host » (ancienne forme) ne fait pas planter describe()."""
        etat = self.etat()
        del etat["native_host"]
        self.assertEqual(wi.describe(etat), "Fichiers .torrent : actif · liens magnet : actif")

    def test_inactif_nomme_le_gestionnaire(self):
        """Le choix explicite de l'utilisateur est cité avant le propriétaire du registre."""
        par_registre = self.etat(torrent_active=False, torrent_owner=CONCURRENT)
        self.assertEqual(wi.describe(par_registre),
                         "Fichiers .torrent : inactif (géré par « %s ») · liens magnet : actif"
                         % CONCURRENT)
        par_choix = self.etat(torrent_active=False, torrent_owner=CONCURRENT,
                              user_choice="Applications.Choisie", magnet_active=False)
        self.assertEqual(wi.describe(par_choix),
                         "Fichiers .torrent : inactif (géré par « Applications.Choisie ») · "
                         "liens magnet : inactif")

    def test_toujours_une_phrase_d_une_ligne(self):
        """Quel que soit l'état : une chaîne non vide, sur une seule ligne."""
        etats = [{}, {"supported": False}, {"supported": True, "error": "x"},
                 {"supported": True, "installed": False}, self.etat(),
                 self.etat(stale=True, registered=False),
                 self.etat(foreign=True, registered=False, foreign_script=r"C:\x.py"),
                 self.etat(torrent_active=False, torrent_owner=CONCURRENT),
                 self.etat(native_host={"applicable": True, "ok": True})]
        for etat in etats:
            phrase = wi.describe(etat)
            self.assertIsInstance(phrase, str)
            self.assertTrue(phrase.strip(), etat)
            self.assertNotIn("\n", phrase)


# --------------------------------------------------------------------------- #
#  Dossiers connus, API du shell, notification
# --------------------------------------------------------------------------- #

class TestDossiersEtShell(unittest.TestCase):

    def test_dossier_telechargements(self):
        """downloads_folder() rend un dossier existant."""
        dossier = wi.downloads_folder()
        self.assertIsInstance(dossier, Path)
        self.assertTrue(dossier.is_absolute())
        self.assertTrue(dossier.is_dir(), dossier)

    def test_dossier_bureau(self):
        """desktop_folder() rend un dossier existant."""
        dossier = wi.desktop_folder()
        self.assertIsInstance(dossier, Path)
        self.assertTrue(dossier.is_absolute())
        self.assertTrue(dossier.is_dir(), dossier)
        self.assertNotEqual(dossier, wi.downloads_folder())

    def test_dossier_connu_inexistant(self):
        """Un identifiant de dossier inconnu donne None, sans exception."""
        self.assertIsNone(wi._known_folder(0x12345678, 0x1234, 0x1234, (1, 2, 3, 4, 5, 6, 7, 8)))

    def test_repli_si_le_shell_ne_repond_pas(self):
        """Sans réponse du shell : repli sur ~/Downloads et ~/Desktop."""
        with mock.patch.object(wi, "_known_folder", return_value=None):
            self.assertEqual(wi.downloads_folder(), Path.home() / "Downloads")
            self.assertEqual(wi.desktop_folder(), Path.home() / "Desktop")

    def test_assoc_command(self):
        """assoc_command() rend une commande (texte) ou None, jamais d'exception."""
        for assoc in (".torrent", "magnet", ".txt", ".tu-selftest-extension-inconnue", "tu-selftest:"):
            commande = wi.assoc_command(assoc)
            self.assertTrue(commande is None or (isinstance(commande, str) and commande), assoc)
        self.assertIsInstance(wi.assoc_command(".txt"), str)     # toujours associé sous Windows

    def test_notify_shell_envoie_assocchanged(self):
        """notify_shell() appelle SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, NULL, NULL)."""
        faux = mock.Mock()
        with mock.patch.object(wi.ctypes, "windll", faux):
            self.assertIsNone(_VRAI_NOTIFY())
        faux.shell32.SHChangeNotify.assert_called_once_with(0x08000000, 0x0000, None, None)

    def test_notify_shell_n_echoue_jamais(self):
        """Une erreur du shell est avalée : la notification est facultative."""
        faux = mock.Mock()
        faux.shell32.SHChangeNotify.side_effect = OSError("shell indisponible")
        with mock.patch.object(wi.ctypes, "windll", faux):
            self.assertIsNone(_VRAI_NOTIFY())
        faux.shell32.SHChangeNotify.assert_called_once()


# --------------------------------------------------------------------------- #
#  ensure_current()
# --------------------------------------------------------------------------- #

class TestEnsureCurrent(BacASable):

    PERIME = {"supported": True, "installed": True, "registered": False, "foreign": False,
              "stale": True, "up_to_date": False}

    def test_bac_a_sable_ne_fait_rien(self):
        """En bac à sable, ensure_current() rend [] sans rien lire ni écrire."""
        self.assertTrue(paths.is_sandboxed())
        self.assertEqual(wi.ensure_current(), [])
        self.assertFalse(existe(SB))
        self.assertEqual(list(paths.native_host_dir().iterdir()), [])
        _NOTIFY.assert_not_called()

    def test_bac_a_sable_meme_si_l_enregistrement_est_perime(self):
        """En bac à sable, ni status() ni register() ne sont appelés, même si tout est périmé."""
        with mock.patch.object(wi, "register", return_value=["refait"]) as register, \
                mock.patch.object(wi, "status", return_value=dict(self.PERIME)) as status:
            self.assertEqual(wi.ensure_current(), [])
        register.assert_not_called()
        status.assert_not_called()

    def _hors_bac(self, etat=None, erreur_status=None, erreur_register=None):
        """Déroule la décision « de production » SANS aucun accès réel possible :
        register() et status() sont des bouchons posés AVANT de lever le drapeau du
        bac à sable (et le garde-fou du module refuserait de toute façon toute écriture).
        """
        with mock.patch.object(wi, "register", return_value=["refait"],
                               side_effect=erreur_register) as register, \
                mock.patch.object(wi, "status", return_value=etat,
                                  side_effect=erreur_status) as status, \
                mock.patch.object(wi, "is_sandboxed", return_value=False):
            resultat = wi.ensure_current()
        self.assertFalse(existe(SB))
        return resultat, register, status

    def test_decision_ne_rien_faire(self):
        """Erreur, jamais activée, autre copie, déjà à jour : register() n'est pas appelé."""
        cas = {
            "erreur": {"supported": True, "error": "pythonw.exe est introuvable."},
            "jamais activée": dict(self.PERIME, installed=False, stale=False),
            "autre copie": dict(self.PERIME, foreign=True, stale=False),
            "à jour": dict(self.PERIME, registered=True, stale=False, up_to_date=True),
        }
        for nom, etat in cas.items():
            resultat, register, status = self._hors_bac(etat)
            self.assertEqual(resultat, [], nom)
            register.assert_not_called()
            status.assert_called_once_with()

    def test_decision_remettre_a_niveau(self):
        """Activée, périmée, pas d'autre copie : register() est appelé et son journal rendu."""
        resultat, register, _ = self._hors_bac(dict(self.PERIME))
        self.assertEqual(resultat, ["refait"])
        register.assert_called_once_with()

    def test_une_exception_ne_remonte_jamais(self):
        """Le démarrage de l'application ne doit pas dépendre de l'intégration : [] en cas d'erreur."""
        resultat, register, _ = self._hors_bac(erreur_status=OSError("registre illisible"))
        self.assertEqual(resultat, [])
        register.assert_not_called()
        resultat, register, _ = self._hors_bac(dict(self.PERIME),
                                               erreur_register=wi.IntegrationError("échec"))
        self.assertEqual(resultat, [])
        register.assert_called_once_with()


# --------------------------------------------------------------------------- #
#  Sentinelle — le nom « TestZz… » la fait passer en dernier
# --------------------------------------------------------------------------- #

class TestZzRegistreReelIntact(unittest.TestCase):

    def test_etat_reel_inchange(self):
        """Les vraies associations du poste sont identiques à la photo prise au début du module."""
        self.assertIsNotNone(_REEL_AVANT)
        apres = photo_du_reel()
        for cle in CLES_SURVEILLEES:
            self.assertEqual(apres[cle], _REEL_AVANT[cle], cle)
        self.assertEqual(apres, _REEL_AVANT)

    def test_aucune_ecriture_tentee_hors_du_bac(self):
        """Le garde-fou n'a intercepté aucune écriture visant le registre réel."""
        self.assertEqual(_VIOLATIONS, [])

    def test_le_bac_a_sable_est_vide(self):
        """Plus aucune trace de la clé bac à sable ni des fichiers de l'hôte natif."""
        self.assertFalse(existe(SB))
        self.assertEqual([f.name for f in paths.native_host_dir().iterdir()], [])


if __name__ == "__main__":
    unittest.main()
