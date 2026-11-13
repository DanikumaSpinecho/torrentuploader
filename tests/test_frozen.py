# -*- coding: utf-8 -*-
"""Mode « exécutable autonome » (PyInstaller) : commandes, hôte natif, identification."""

from tests import helpers as h          # EN PREMIER : bac à sable

import unittest
from pathlib import Path
from unittest import mock

from tuploader import winintegration as wi

EXE = Path(r"C:\Programmes\Torrent Uploader\TorrentUploader.exe")


class ModeExecutable(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(wi, "FROZEN", True), mock.patch.object(wi, "EXE_PATH", EXE)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_commandes_visent_le_exe(self):
        fichier, uri = wi.commands()
        self.assertEqual(fichier, '"%s" "%%L"' % EXE)
        self.assertEqual(uri, '"%s" "%%1"' % EXE)

    def test_identite_et_icone(self):
        self.assertEqual(wi.script_path(), str(EXE))
        self.assertEqual(wi.python_launcher(), str(EXE))
        self.assertEqual(wi.icon_path(), str(EXE))

    def test_manifeste_hote_pointe_sur_le_exe(self):
        manifeste, bat = wi._host_files()
        self.assertEqual(wi._host_manifest(bat)["path"], str(EXE))

    def test_commande_reconnue(self):
        fichier, _ = wi.commands()
        self.assertEqual(wi._script_in_command(fichier), str(EXE))
        self.assertTrue(wi._is_our_command(fichier))


class IdentificationDesCommandes(unittest.TestCase):
    def test_script_python(self):
        cmd = r'"C:\Python\pythonw.exe" "D:\app\torrent_uploader.py" "%L"'
        self.assertEqual(wi._script_in_command(cmd), r"D:\app\torrent_uploader.py")
        self.assertTrue(wi._is_our_command(cmd))

    def test_interpreteur_seul_non_reconnu(self):
        self.assertEqual(wi._script_in_command(r'"C:\Python\pythonw.exe" "%1"'), "")

    def test_autre_programme(self):
        self.assertFalse(wi._is_our_command(r'"C:\qBittorrent\qbittorrent.exe" "%1"'))


if __name__ == "__main__":
    unittest.main()
