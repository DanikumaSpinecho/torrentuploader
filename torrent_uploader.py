#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Torrent Uploader — point d'entrée.

    python torrent_uploader.py [fichier.torrent | lien magnet | adresse] ...
    python torrent_uploader.py --help

Ce fichier est celui que Windows lance (association des .torrent et des liens
magnet) : son nom et son emplacement doivent rester stables. Tout le code vit
dans le paquet « tuploader ».
"""

import sys

if sys.version_info < (3, 8):
    sys.exit("Torrent Uploader nécessite Python 3.8 ou plus récent.")

from tuploader.main import run

if __name__ == "__main__":
    sys.exit(run())
