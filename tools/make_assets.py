# -*- coding: utf-8 -*-
"""Génère l'icône de l'application (PNG + ICO), sans aucune dépendance.

    python tools/make_assets.py [dossier_de_sortie_supplémentaire_pour_PNG]

L'image est décrite par des formes mathématiques, rendue en suréchantillonnage
(4 x 4 sous-pixels) pour des bords lisses, puis encodée à la main en PNG / ICO.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "tuploader" / "assets"

TOP = (59, 130, 246)        # bleu clair
BOTTOM = (29, 78, 216)      # bleu soutenu
WHITE = (255, 255, 255)
SS = 4                      # suréchantillonnage


def _in_rounded_rect(u, v, x0, y0, x1, y1, r):
    if not (x0 <= u <= x1 and y0 <= v <= y1):
        return False
    cx = min(max(u, x0 + r), x1 - r)
    cy = min(max(v, y0 + r), y1 - r)
    return (u - cx) ** 2 + (v - cy) ** 2 <= r * r


def _sample(u, v):
    """Couleur (r, g, b, a) du point (u, v), coordonnées dans [0, 1]."""
    if not _in_rounded_rect(u, v, 0.03, 0.03, 0.97, 0.97, 0.22):
        return None
    white = (
        (0.445 <= u <= 0.555 and 0.20 <= v <= 0.50)                       # hampe
        or (0.44 <= v <= 0.68 and abs(u - 0.5) <= (0.68 - v) * 0.95)      # pointe
        or _in_rounded_rect(u, v, 0.24, 0.745, 0.76, 0.815, 0.035)        # plateau
    )
    if white:
        return WHITE
    t = v
    return tuple(int(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3))


def render(size: int) -> bytes:
    """Pixels RGBA, lignes de haut en bas."""
    out = bytearray()
    n = size * SS
    for y in range(size):
        for x in range(size):
            r = g = b = hit = 0
            for sy in range(SS):
                v = (y * SS + sy + 0.5) / n
                for sx in range(SS):
                    col = _sample((x * SS + sx + 0.5) / n, v)
                    if col is not None:
                        r += col[0]; g += col[1]; b += col[2]; hit += 1
            if hit:
                out += bytes((r // hit, g // hit, b // hit, 255 * hit // (SS * SS)))
            else:
                out += b"\0\0\0\0"
    return bytes(out)


def png(size: int, rgba: bytes) -> bytes:
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    rows = b"".join(b"\0" + rgba[y * size * 4:(y + 1) * size * 4] for y in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def ico(images: dict[int, bytes]) -> bytes:
    """Fichier .ico dont chaque taille est stockée en PNG (Windows Vista et suivants)."""
    sizes = sorted(images)
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries, blobs = b"", b""
    for s in sizes:
        data = images[s]
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blobs += data
    return header + entries + blobs


def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    pngs = {}
    for size in (16, 24, 32, 48, 64, 128, 256):
        pngs[size] = png(size, render(size))
    (ASSETS / "icon.ico").write_bytes(ico(pngs))
    (ASSETS / "icon.png").write_bytes(pngs[256])
    (ASSETS / "icon-64.png").write_bytes(pngs[64])
    (ASSETS / "icon-32.png").write_bytes(pngs[32])
    print("icônes de l'application écrites dans", ASSETS)
    for extra in sys.argv[1:]:                      # ex. le dossier de l'extension
        folder = Path(extra)
        folder.mkdir(parents=True, exist_ok=True)
        for size in (16, 32, 48, 128):
            (folder / ("icon%d.png" % size)).write_bytes(pngs[size])
        print("icônes PNG 16/32/48/128 écrites dans", folder)


if __name__ == "__main__":
    main()
