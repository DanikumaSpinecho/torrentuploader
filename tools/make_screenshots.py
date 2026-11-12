# -*- coding: utf-8 -*-
"""Produit les captures d'écran de la notice, à partir de la vraie interface.

    python tools/make_screenshots.py [dossier_de_sortie]

L'application est lancée dans un bac à sable (données factices, aucun réglage
réel lu ni modifié) ; chaque fenêtre est photographiée par Windows lui-même.
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
import tempfile
import zlib
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = Path(tempfile.mkdtemp(prefix="tu-captures-"))
os.environ["TORRENT_UPLOADER_DATA_DIR"] = str(SANDBOX)      # AVANT tout import du paquet
sys.path.insert(0, str(ROOT))

import json                                                  # noqa: E402
import tkinter as tk                                         # noqa: E402

from tuploader.paths import config_path                      # noqa: E402
from tuploader.ui import dialogs                             # noqa: E402
from tuploader.ui.app import (App, ST_FAILED, ST_OK, ST_PENDING, ST_SENDING)  # noqa: E402
from tuploader.ui.theme import enable_dpi_awareness, set_app_id               # noqa: E402

DEMO_CONFIG = {
    "url": "https://seedbox.exemple.net/rutorrent", "username": "demo", "password": "demo",
    "auto_refresh": False, "last_dir": "/home/demo/downloads/rtorrent/logiciels",
    "last_label": "Linux", "favorite_dirs": ["/home/demo/downloads/rtorrent/logiciels"],
    "favorite_labels": ["Linux"],
    "cache_labels": ["Documentaires", "Films", "Linux", "Musique", "Séries"],
    "cache_dirs": ["/home/demo/downloads/rtorrent", "/home/demo/downloads/rtorrent/films",
                   "/home/demo/downloads/rtorrent/logiciels", "/home/demo/downloads/rtorrent/series"],
    "cache_time": "10/11/2026 20:15",
    # dossier générique : la notice ne doit montrer aucun nom de session réel
    "watch_folder": r"C:\Users\Utilisateur\Downloads",
}

DEMO_STATUS = {
    "supported": True, "installed": True, "registered": True, "foreign": False,
    "foreign_script": "", "stale": False, "torrent_active": True, "magnet_active": True,
    "torrent_owner": "TorrentUploader.torrent", "user_choice": None, "magnet_user_choice": None,
    "open_with_listed": True, "icon_ok": True, "up_to_date": True,
    "native_host": {"browsers": ["Chrome", "Edge"], "declared": ["Chrome", "Edge"],
                    "ok": True, "applicable": True},
    "effective_command": "torrent_uploader.py",
}


# --------------------------------------------------------------------------- #
#  Torrents de démonstration
# --------------------------------------------------------------------------- #

def _bencode(obj) -> bytes:
    if isinstance(obj, int):
        return b"i%de" % obj
    if isinstance(obj, bytes):
        return b"%d:%s" % (len(obj), obj)
    if isinstance(obj, str):
        return _bencode(obj.encode("utf-8"))
    if isinstance(obj, list):
        return b"l" + b"".join(_bencode(x) for x in obj) + b"e"
    if isinstance(obj, dict):
        return b"d" + b"".join(_bencode(k) + _bencode(obj[k]) for k in sorted(obj)) + b"e"
    raise TypeError(type(obj))


def make_torrent(folder: Path, name: str, size: int, files: int = 1) -> Path:
    info = {b"name": name.encode("utf-8"), b"piece length": 262144, b"pieces": b"\0" * 20}
    if files > 1:
        info[b"files"] = [{b"length": size // files, b"path": [("fichier%02d" % i).encode()]}
                          for i in range(files)]
    else:
        info[b"length"] = size
    path = folder / (name + ".torrent")
    path.write_bytes(b"d8:announce30:http://tracker.exemple.net/ann4:info" + _bencode(info) + b"e")
    return path


# --------------------------------------------------------------------------- #
#  Capture d'une fenêtre (PrintWindow) et écriture PNG
# --------------------------------------------------------------------------- #

class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def capture(window: tk.Misc, path: Path) -> tuple[int, int]:
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    user32.GetParent.restype = wintypes.HWND
    user32.GetWindowDC.restype = wintypes.HDC
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]

    window.update_idletasks()
    window.update()
    hwnd = user32.GetParent(window.winfo_id())
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    user32.PrintWindow(hwnd, mem, 2)                 # 2 = rendu complet (barre de titre incluse)
    header = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(header), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)

    # Windows ajoute une marge invisible autour des fenêtres : on la retire.
    frame = wintypes.RECT()
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(frame), ctypes.sizeof(frame))
        left, top = frame.left - rect.left, frame.top - rect.top
        right, bottom = frame.right - rect.left, frame.bottom - rect.top
    except Exception:
        left, top, right, bottom = 0, 0, w, h
    raw = buf.raw
    rows = bytearray()
    for y in range(top, bottom):
        line = bytearray(raw[(y * w + left) * 4:(y * w + right) * 4])
        line[0::4], line[2::4] = line[2::4], line[0::4]          # BGRA -> RGBA
        line[3::4] = b"\xff" * (len(line) // 4)
        rows += b"\0" + line
    cw, ch = right - left, bottom - top

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", cw, ch, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))
    return cw, ch


# --------------------------------------------------------------------------- #

def settle(root, ms=350):
    end = root.tk.call("clock", "milliseconds") + ms
    while root.tk.call("clock", "milliseconds") < end:
        root.update()


def build_app(theme: str):
    config_path().write_text(json.dumps({**DEMO_CONFIG, "theme": theme}), encoding="utf-8")
    root = tk.Tk()
    app = App(root, [])
    folder = SANDBOX / "torrents"
    folder.mkdir(exist_ok=True)
    app.add_items([
        str(make_torrent(folder, "ubuntu-26.04-desktop-amd64.iso", 6_190_000_000)),
        str(make_torrent(folder, "debian-13.2.0-amd64-DVD", 4_020_000_000, files=3)),
        "magnet:?xt=urn:btih:C12FE1C06BBA254A9DC9F519B335AA7C1367A88A&dn=archlinux-2026.11.01-x86_64.iso",
        str(make_torrent(folder, "Big.Buck.Bunny.2008.1080p", 890_000_000, files=4)),
        str(make_torrent(folder, "LibreOffice_26.8_Win_x86-64", 365_000_000)),
    ], announce=False)
    rows = app.tree.get_children()
    for iid, (text, tag, status) in zip(rows, (
            (ST_OK, "ok", "ok"), (ST_OK, "ok", "ok"), (ST_SENDING, "run", "pending"),
            (ST_PENDING, "", "pending"),
            (ST_FAILED + "erreur interne de la seedbox (HTTP 500) — réessayez dans un instant",
             "err", "error"))):
        app.items[iid]["status"] = status
        app._set_status(iid, text, tag)
    app._set_link("connectée", "ok")
    app.server = app.server or object()      # démo : service local réputé actif
    app.progress.configure(maximum=5, value=2)
    app.set_status("Envoi de « archlinux-2026.11.01-x86_64.iso »…")
    root.geometry("+60+40")
    settle(root)
    return root, app


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "images"
    out.mkdir(parents=True, exist_ok=True)
    enable_dpi_awareness()
    set_app_id()

    root, app = build_app("light")
    print("fenetre principale (clair)  :", capture(root, out / "principale-clair.png"))

    dialogs.wi = type("FakeIntegration", (), {"status": staticmethod(lambda: dict(DEMO_STATUS))})
    dlg = dialogs.IntegrationDialog(root, app)
    settle(root)
    print("integration                 :", capture(dlg, out / "integration.png"))
    dlg.destroy()

    dlg = dialogs.SettingsDialog(root, app.theme, {**app.cfg, "auto_refresh": True})
    settle(root)
    print("parametres                  :", capture(dlg, out / "parametres.png"))
    dlg.destroy()
    app.on_close()

    root, app = build_app("dark")
    print("fenetre principale (sombre) :", capture(root, out / "principale-sombre.png"))
    app.on_close()
    print("captures ecrites dans", out)


if __name__ == "__main__":
    main()
