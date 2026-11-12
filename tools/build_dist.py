# -*- coding: utf-8 -*-
"""Assemble le dossier de distribution, prêt à copier ou à partager.

    python tools/build_dist.py [--extension <dossier>] [--out <dossier>] [--no-pdf]

Résultat (par défaut à côté du dossier du projet) :

    Torrent Uploader - Distribution/
        Torrent Uploader/              l'application (sans tests ni outils)
        Extension Chrome/              l'extension (sans sa clé privée)
        Notice d'utilisation.pdf
        Notice d'utilisation (HTML)/   la même notice, consultable dans un navigateur
        LISEZMOI.txt
        Torrent Uploader <version>.zip tout ce qui précède, en une archive

Aucune donnée personnelle n'y entre : les réglages vivent dans le profil
Windows, et l'outil vérifie qu'aucun config.json, jeton ou journal ne s'y glisse.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tuploader import __version__                     # noqa: E402

APP_FILES = ["torrent_uploader.py", "Torrent Uploader.pyw", "Torrent Uploader.bat",
             "native_host.py", "requirements.txt", "tuploader"]
EXT_FILES = ["manifest.json", "sw.js", "popup.html", "popup.js", "options.html", "options.js",
             "ui.css", "icons", "README.md"]
FORBIDDEN = {"config.json", "ipc_token.txt", "erreurs.log", "extension-key.pem"}

LISEZMOI = """\
Torrent Uploader {version}
==========================

Envoie vos fichiers .torrent et vos liens magnet vers une seedbox ruTorrent.

Démarrage rapide
----------------
1. Installez Python 3 depuis https://www.python.org/downloads/
2. Copiez le dossier « Torrent Uploader » où vous voulez (par exemple dans Documents).
3. Double-cliquez sur « Torrent Uploader.pyw » et renseignez votre seedbox.
4. Bouton « Intégration » puis « Activer » : les .torrent et les liens magnet
   s'ouvriront alors d'un clic.
5. Facultatif, pour envoyer depuis Chrome ou Edge : chargez le dossier
   « Extension Chrome » (Mode développeur > Charger l'extension non empaquetée).

Tout est détaillé dans « Notice d'utilisation.pdf ».
"""


def copy_tree(src: Path, dst: Path, names):
    dst.mkdir(parents=True, exist_ok=True)
    for name in names:
        source = src / name
        if not source.exists():
            raise SystemExit("fichier manquant : %s" % source)
        target = dst / name
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "*.pem", ".git*"))
        else:
            shutil.copy2(source, target)


def main() -> int:
    ap = argparse.ArgumentParser()
    default_ext = ROOT / "chrome-extension"
    if not default_ext.is_dir():
        default_ext = ROOT.parent / "torrent-uploader-chrome-extension"
    ap.add_argument("--extension", default=str(default_ext))
    ap.add_argument("--out", default=str(ROOT.parent / "Torrent Uploader - Distribution"))
    ap.add_argument("--no-pdf", action="store_true")
    args = ap.parse_args()

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    copy_tree(ROOT, out / "Torrent Uploader", APP_FILES)
    copy_tree(Path(args.extension), out / "Extension Chrome", EXT_FILES)

    notice_dir = out / "Notice d'utilisation (HTML)"
    notice_dir.mkdir()
    shutil.copy2(ROOT / "docs" / "notice.html", notice_dir / "Notice d'utilisation.html")
    shutil.copytree(ROOT / "docs" / "images", notice_dir / "images")
    (out / "LISEZMOI.txt").write_text(LISEZMOI.format(version=__version__), encoding="utf-8-sig",
                                      newline="\r\n")

    if not args.no_pdf:
        subprocess.run(["node", str(ROOT / "tools" / "notice_pdf.mjs"),
                        str(notice_dir / "Notice d'utilisation.html"),
                        str(out / "Notice d'utilisation.pdf")], check=True)

    leaked = [p for p in out.rglob("*") if p.name in FORBIDDEN]
    if leaked:
        raise SystemExit("donnée personnelle dans la distribution : %s" % leaked)

    archive = out / ("Torrent Uploader %s.zip" % __version__)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(out.rglob("*")):
            if path.is_file() and path != archive:
                zf.write(path, path.relative_to(out).as_posix())
    count = sum(1 for p in out.rglob("*") if p.is_file())
    print("distribution prête : %s (%d fichiers, archive de %d Ko)"
          % (out, count, archive.stat().st_size // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
