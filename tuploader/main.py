# -*- coding: utf-8 -*-
"""Démarrage de l'application : arguments, instance unique, fenêtre principale."""

from __future__ import annotations

import os
import re
import sys

from . import APP_NAME, __version__
from .config import load_config
from .paths import ensure_std_streams, log_error, log_path, migrate_legacy, purge_inbox
from .service import (ask_running_instance, forward_to_running_instance, get_token,
                      resolve_port, start_local_server)

try:
    from . import winintegration as wi
except Exception:                         # plateforme sans registre
    wi = None

USAGE = """\
%s %s

  torrent_uploader.py [fichier.torrent | lien magnet | adresse http(s)] ...
  torrent_uploader.py --register      active l'intégration Windows / navigateur
  torrent_uploader.py --unregister    la retire
  torrent_uploader.py --status        affiche son état
  torrent_uploader.py --version
""" % (APP_NAME, __version__)


class Sink:
    """Tampon : le service local peut recevoir avant que la fenêtre n'existe."""

    def __init__(self):
        self._items: list = []
        self._show = False
        self._on_items = None
        self._on_show = None

    def items(self, entries):
        if self._on_items is not None:
            self._on_items(entries)
        else:
            self._items.extend(entries)

    def show(self):
        if self._on_show is not None:
            self._on_show()
        else:
            self._show = True

    def attach(self, on_items, on_show):
        self._on_items, self._on_show = on_items, on_show
        pending, self._items = self._items, []
        if pending:
            on_items(pending)
        if self._show:
            self._show = False
            on_show()


_SHORT_FLAGS = {"-h": "--help", "/?": "--help", "-v": "--version"}


def _starts_target(arg: str) -> bool:
    """Un argument qui ouvre une nouvelle cible (et ne prolonge donc pas un magnet)."""
    text = arg.strip().strip('"')
    if re.match(r"^(magnet:|https?://)", text, re.I):
        return True
    return text.lower().endswith(".torrent") or os.path.exists(text)


def parse_argv(argv):
    """Sépare les options des cibles et recolle les URI découpées par le shell.

    Windows ne protège pas les URI : « magnet:?xt=...&dn=Mon Film » arrive
    en plusieurs arguments. Un fragment est rattaché au magnet qui le précède,
    sauf s'il commence lui-même une cible (autre magnet, adresse, fichier).
    """
    flags, targets = [], []
    for arg in argv:
        low = arg.lower()
        if arg.startswith("--") or low in _SHORT_FLAGS:
            flags.append(_SHORT_FLAGS.get(low, low))
        elif (targets and targets[-1].strip().lower().startswith("magnet:")
              and not _starts_target(arg)):
            targets[-1] += " " + arg
        else:
            targets.append(arg)
    return flags, targets


def absolutize(target: str) -> str:
    """Chemin local rendu absolu : l'instance qui le reçoit n'a pas le même dossier courant."""
    text = target.strip().strip('"')
    if re.match(r"^(magnet:|https?://)", text, re.I):
        return target
    return os.path.abspath(text)


def _show_errors(errors) -> None:
    """Un second lancement transmet puis se termine : il affiche lui-même les refus."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        hidden = tk.Tk()
        hidden.withdraw()
        messagebox.showwarning(APP_NAME, "Non ajouté à la file :\n\n" + "\n".join(errors[:10]))
        hidden.destroy()
    except Exception:
        pass


def _command_line(flags) -> int | None:
    """Traite les options en ligne de commande. Retourne None s'il faut ouvrir la fenêtre."""
    if "--version" in flags:
        print("%s %s" % (APP_NAME, __version__))
        return 0
    if "--help" in flags:
        print(USAGE)
        return 0
    wanted = [f for f in ("--register", "--unregister", "--status") if f in flags]
    if not wanted:
        return None
    if wi is None or os.name != "nt":
        print("L'intégration n'est disponible que sous Windows.")
        return 1
    try:
        if wanted[0] == "--register":
            for line in wi.register():
                print("  +", line)
        elif wanted[0] == "--unregister":
            for line in wi.unregister():
                print("  -", line)
        else:
            print(wi.describe(wi.status()))
    except Exception as exc:
        print("Erreur : %s" % exc)
        log_error("intégration en ligne de commande", exc)
        return 1
    return 0


def main(argv=None) -> int:
    ensure_std_streams()
    flags, targets = parse_argv(list(sys.argv[1:] if argv is None else argv))
    moved = migrate_legacy()

    code = _command_line(flags)
    if code is not None:
        return code

    targets = [absolutize(t) for t in targets]
    token = get_token()
    port = resolve_port(load_config())
    sink = Sink()
    server = start_local_server(sink.items, token, port, on_show=sink.show)
    if server is None:
        # Le port est pris : soit une instance tourne déjà, soit un tiers l'occupe.
        if not targets:
            if forward_to_running_instance([], token, port):
                return 0                 # fenêtre existante passée au premier plan
        else:
            answer = ask_running_instance("/add", token, port, targets)
            if answer and answer.get("status") == 200 and not answer.get("incompatible"):
                if answer.get("errors"):
                    _show_errors(answer["errors"])
                return 0                 # transmis à la fenêtre existante
        log_error("port %d occupé par un autre programme : ouverture d'une fenêtre isolée" % port)

    notice = ""
    if moved:
        notice = "Vos réglages ont été déplacés dans votre profil Windows (%s)." % log_path().parent
    if wi is not None:
        updated = wi.ensure_current()
        if updated:
            log_error("intégration Windows remise à niveau : " + " | ".join(updated))
            notice = "Intégration Windows et navigateur mise à jour pour cette version."
    purge_inbox()

    import tkinter as tk
    from .ui.theme import enable_dpi_awareness, set_app_id
    enable_dpi_awareness()
    set_app_id()
    try:
        from tkinterdnd2 import TkinterDnD            # type: ignore
        root = TkinterDnD.Tk()
    except Exception:
        root = tk.Tk()

    from .ui.app import App
    app = App(root, targets, server=server, token=token, notice=notice)
    sink.attach(app.submit_external, app.request_show)
    root.mainloop()
    return 0


def run() -> int:
    """Point d'entrée protégé : sous pythonw, un plantage serait invisible."""
    # Le .exe autonome sert aussi d'hôte de messagerie native : le navigateur le
    # lance avec l'origine de l'extension en argument.
    if any(a.startswith("chrome-extension://") for a in sys.argv[1:]):
        from .nativehost import main as host_main
        return host_main()
    try:
        return main()
    except SystemExit:
        raise
    except Exception as exc:
        log_error("plantage", exc)
        try:
            import tkinter as tk
            from tkinter import messagebox
            hidden = tk.Tk()
            hidden.withdraw()
            messagebox.showerror(APP_NAME, "Erreur inattendue :\n\n%s\n\nLe détail est dans :\n%s"
                                 % (exc, log_path()))
            hidden.destroy()
        except Exception:
            pass
        return 1
