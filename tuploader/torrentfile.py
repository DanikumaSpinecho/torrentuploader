# -*- coding: utf-8 -*-
"""Lecture des fichiers .torrent (bencode) et des liens magnet."""

from __future__ import annotations

import base64
import hashlib
import re
import time
import urllib.parse
from pathlib import Path


class BencodeError(Exception):
    pass


def _bdecode(data: bytes, pos: int):
    """Decode une valeur bencode a partir de `pos`. Retourne (valeur, position_suivante)."""
    if pos >= len(data):
        raise BencodeError("fin de données inattendue")
    c = data[pos:pos + 1]

    if c == b"i":
        end = data.find(b"e", pos)
        if end < 0:
            raise BencodeError("entier non terminé")
        return int(data[pos + 1:end]), end + 1

    if c == b"l":
        pos += 1
        out = []
        while data[pos:pos + 1] != b"e":
            value, pos = _bdecode(data, pos)
            out.append(value)
        return out, pos + 1

    if c == b"d":
        pos += 1
        out = {}
        while data[pos:pos + 1] != b"e":
            key, pos = _bdecode(data, pos)
            value, pos = _bdecode(data, pos)
            out[key] = value
        return out, pos + 1

    if c.isdigit():
        colon = data.find(b":", pos)
        if colon < 0:
            raise BencodeError("chaîne mal formée")
        length = int(data[pos:colon])
        start = colon + 1
        if start + length > len(data):
            raise BencodeError("chaîne tronquée")
        return data[start:start + length], start + length

    raise BencodeError("octet inattendu %r à la position %d" % (c, pos))


# Bornes de vraisemblance : au-delà, le fichier est corrompu ou malveillant.
MAX_FILE_SIZE = 2 ** 53           # ~ 9 Po par fichier
MAX_TOTAL_SIZE = 2 ** 60


def _length(value) -> int:
    if not isinstance(value, int) or value < 0 or value > MAX_FILE_SIZE:
        raise BencodeError("taille de fichier invraisemblable")
    return value


def parse_torrent(path: Path) -> dict:
    """Lit un .torrent et retourne nom, taille, nombre de fichiers et infohash."""
    try:
        return _parse_torrent(Path(path))
    except BencodeError:
        raise
    except (ValueError, IndexError, RecursionError, TypeError, AttributeError) as exc:
        # entier illisible, imbrication démesurée... : le fichier est corrompu
        raise BencodeError("fichier .torrent corrompu") from exc


def _parse_torrent(path: Path) -> dict:
    raw = path.read_bytes()
    if raw[:1] != b"d":
        raise BencodeError("ce fichier n'est pas un .torrent valide")

    pos = 1
    meta: dict = {}
    info_span = None
    while raw[pos:pos + 1] != b"e":
        key, pos = _bdecode(raw, pos)
        start = pos
        value, pos = _bdecode(raw, pos)
        if key == b"info":
            info_span = (start, pos)
        meta[key] = value

    if info_span is None or not isinstance(meta.get(b"info"), dict):
        raise BencodeError("section « info » absente du .torrent")

    info = meta[b"info"]
    infohash = hashlib.sha1(raw[info_span[0]:info_span[1]]).hexdigest().upper()

    name_raw = info.get(b"name.utf-8") or info.get(b"name") or b"(sans nom)"
    name = name_raw.decode("utf-8", "replace") if isinstance(name_raw, bytes) else str(name_raw)

    if b"files" in info and isinstance(info[b"files"], list):
        files = info[b"files"]
        size = sum(_length(f.get(b"length", 0)) for f in files if isinstance(f, dict))
        nfiles = len(files)
    else:
        size = _length(info.get(b"length", 0))
        nfiles = 1
    if size > MAX_TOTAL_SIZE:
        raise BencodeError("taille totale invraisemblable")

    tracker = meta.get(b"announce", b"")
    tracker = tracker.decode("utf-8", "replace") if isinstance(tracker, bytes) else ""

    return {
        "path": path,
        "name": name,
        "size": size,
        "files": nfiles,
        "infohash": infohash,
        "private": bool(info.get(b"private", 0)),
        "tracker": tracker,
    }


class MagnetError(Exception):
    pass


# Un magnet arrive du shell : on le traite comme une entrée hostile.
MAGNET_RE = re.compile(
    r"^magnet:\?.*\bxt=urn:btih:(?:[0-9a-fA-F]{40}|[A-Za-z2-7]{32})\b", re.I | re.A
)
_BTIH_RE = re.compile(r"^urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})$", re.I | re.A)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_MAGNET_LENGTH = 1024 * 1024


def parse_magnet(link: str) -> dict:
    """Analyse un lien magnet. Retourne nom, infohash 40 hex majuscules et trackers."""
    link = (link or "").strip().strip('"')
    if not link.lower().startswith("magnet:"):
        raise MagnetError("ce lien n'est pas un magnet")
    if len(link) > MAX_MAGNET_LENGTH:
        raise MagnetError("lien magnet anormalement long")
    if _CONTROL_RE.search(link):
        raise MagnetError("lien magnet invalide (caractères de contrôle)")
    if not MAGNET_RE.match(link):
        raise MagnetError("lien magnet invalide (xt=urn:btih:... absent ou mal formé)")

    raw_params = urllib.parse.parse_qs(link[len("magnet:"):].lstrip("?"),
                                       keep_blank_values=False)
    params: dict = {}
    for key, values in raw_params.items():      # « XT= » vaut « xt= » (comme MAGNET_RE)
        params.setdefault(key.lower(), []).extend(values)

    infohash = ""
    exact_topics = [v for key in sorted(params) if key == "xt" or key.startswith("xt.")
                    for v in params[key]]
    for xt in exact_topics:
        m = _BTIH_RE.match(xt)
        if not m:
            continue
        raw = m.group(1)
        if len(raw) == 40:
            infohash = raw.upper()
        else:                                   # base32 -> hex, sinon la confirmation échouerait
            try:
                infohash = base64.b32decode(raw.upper()).hex().upper()
            except Exception:
                continue
        break

    if not infohash:
        raise MagnetError("aucun infohash exploitable dans ce lien magnet")

    names = params.get("dn", [])
    name = (names[0] if names else "").strip()
    return {
        "name": name or ("Magnet %s" % infohash[:16]),
        "size": 0,
        "files": 0,
        "infohash": infohash,
        "trackers": params.get("tr", []),
    }


def torrent_is_ready(path: Path, min_age: float = 0.4) -> bool:
    """Vrai si le .torrent est complet, déverrouillé et réellement décodable.

    Chrome écrit un `.crdownload` puis renomme ; d'autres sources écrivent
    directement. Chrome pose aussi la marque de provenance après le renommage,
    d'où le verrou temporaire qu'on retente au tour suivant.
    """
    try:
        st = path.stat()
    except OSError:
        return False
    if st.st_size == 0 or time.time() - st.st_mtime < min_age:
        return False
    try:
        with open(path, "rb") as fh:
            if fh.read(1) != b"d":
                return False
        parse_torrent(path)
    except Exception:
        return False
    return True


def human_size(n: int) -> str:
    try:
        value = float(n or 0)
    except (OverflowError, TypeError, ValueError):
        return "?"
    for unit in ("o", "Ko", "Mo", "Go", "To", "Po"):
        if value < 1024 or unit == "Po":
            return ("%.0f %s" % (value, unit)) if unit == "o" else ("%.2f %s" % (value, unit))
        value /= 1024
    return "%.2f Po" % value
