# -*- coding: utf-8 -*-
"""Construit TorrentUploader.exe, un exécutable autonome (Python non requis).

    pip install pyinstaller
    python tools/build_exe.py            -> dist/TorrentUploader.exe

Le même .exe sert d'application et d'hôte de messagerie native pour l'extension
(le navigateur le lance avec l'origine de l'extension en argument).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tuploader import APP_NAME, __version__        # noqa: E402

BUILD = ROOT / "build"
DIST = ROOT / "dist"

VERSION_INFO = """\
VSVersionInfo(
  ffi=FixedFileInfo(filevers=({v}), prodvers=({v}), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040C04B0', [
      StringStruct('CompanyName', ''),
      StringStruct('FileDescription', '{name} - envoi vers une seedbox ruTorrent'),
      StringStruct('FileVersion', '{dotted}'),
      StringStruct('InternalName', 'TorrentUploader'),
      StringStruct('OriginalFilename', 'TorrentUploader.exe'),
      StringStruct('ProductName', '{name}'),
      StringStruct('ProductVersion', '{dotted}')])]),
    VarFileInfo([VarStruct('Translation', [0x040C, 1200])])
  ]
)
"""


def main() -> int:
    parts = [int(x) for x in __version__.split(".")] + [0]
    BUILD.mkdir(exist_ok=True)
    version_file = BUILD / "version_info.txt"
    version_file.write_text(VERSION_INFO.format(
        v=", ".join(str(x) for x in parts[:4]), dotted=__version__, name=APP_NAME), encoding="utf-8")

    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile", "--windowed",
        "--name", "TorrentUploader",
        "--icon", str(ROOT / "tuploader" / "assets" / "icon.ico"),
        "--version-file", str(version_file),
        "--add-data", "%s;tuploader/assets" % (ROOT / "tuploader" / "assets"),
        "--exclude-module", "tests",
        "--distpath", str(DIST), "--workpath", str(BUILD / "work"), "--specpath", str(BUILD),
        str(ROOT / "torrent_uploader.py"),
    ]
    subprocess.run(cmd, check=True, cwd=ROOT)
    exe = DIST / "TorrentUploader.exe"
    print("exécutable prêt : %s (%.1f Mo)" % (exe, exe.stat().st_size / 1e6))
    shutil.rmtree(BUILD / "work", ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
