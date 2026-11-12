#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hôte de messagerie native — lancé par le navigateur, jamais à la main.

Relaie les messages de l'extension vers l'application (voir tuploader/nativehost.py).
"""

import sys

from tuploader.nativehost import main

if __name__ == "__main__":
    sys.exit(main())
