# -*- coding: utf-8 -*-
"""Fenêtre principale : file d'attente, destination, envoi vers la seedbox."""

from __future__ import annotations

import queue
import re
import threading
import time
import tkinter as tk
import urllib.parse
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .. import APP_NAME
from ..config import load_config, save_config
from ..paths import ASSETS_DIR, inbox_dir, log_error
from ..rutorrent import RuTorrentClient, RuTorrentError
from ..service import get_token, resolve_port
from ..torrentfile import MagnetError, human_size, parse_magnet, parse_torrent, torrent_is_ready
from .dialogs import IntegrationDialog, SettingsDialog
from .theme import Theme, dark_title_bar
from .widgets import Card, CheckBox, DropZone, FlatButton, StatusPill

try:
    from tkinterdnd2 import DND_FILES, DND_TEXT            # type: ignore
    DND_AVAILABLE = True
except Exception:                                          # dépendance facultative
    DND_AVAILABLE = False

try:
    from .. import winintegration as wi
except Exception:
    wi = None

# Libellés d'état d'une ligne (centralisés : l'interface et les tests s'y réfèrent)
ST_PENDING = "En attente"
ST_SENDING = "Envoi en cours…"
ST_OK = "Ajouté (confirmé)"
ST_SENT = "Envoyé (non confirmé)"
ST_OK_AFTER_ERROR = "Ajouté (confirmé malgré une erreur passagère)"
ST_OK_REPAIRED = "Ajouté (après réparation du cache ruTorrent)"
ST_SENT_REPAIRED = "Envoyé après réparation du cache (non confirmé)"
ST_FAILED = "Échec : "

KIND_LABELS = {"file": "Fichier", "magnet": "Magnet", "url": "Lien"}
SETTINGS_KEYS = ("url", "username", "password", "verify_ssl", "auto_refresh", "timeout", "theme")
_TEXT_WIDGETS = ("TEntry", "TCombobox", "TSpinbox", "Entry", "Text")


class App:
    # Nouvel essai automatique après un incident passager (secondes), comme le fait
    # l'interface web de ruTorrent qui se réactualise en permanence.
    AUTO_RETRY_DELAYS = (15, 30, 60, 120, 300)

    def __init__(self, root, initial_items=(), server=None, token: str = "", notice: str = ""):
        self.root = root
        self.cfg = load_config()
        self.items: dict[str, dict] = {}     # iid -> {kind, path|uri, info, status, text, tag}
        self._server_dirs: list[str] = []
        self._server_labels: list[str] = []
        self.busy = False
        self.server = server
        self.token = token or get_token()
        self.port = resolve_port(self.cfg)
        self._watch_thread = None
        self._watch_stop = threading.Event()
        # tkinter n'est pas thread-safe : les threads ne touchent jamais aux
        # widgets, ils déposent une fonction ici et le thread principal l'exécute.
        self._inbox: queue.Queue = queue.Queue()
        self._closing = False
        self._refreshing = False
        self._auto_retry_job = None
        self._auto_retry_index = 0
        self._link_state = ("", "idle")
        self._pump_job = None
        self._send_again = False          # envoi automatique demandé pendant un envoi

        self.theme = Theme(root, self.cfg.get("theme", "system"))
        self.theme.apply(root)
        self._setup_window()
        self._build_ui()
        self._apply_config()
        self._setup_dnd()

        if initial_items:
            self.add_items(initial_items)
        if notice:
            self.set_status(notice)

        self._pump_job = self.root.after(120, self._pump)
        self.restart_watcher()

        if self.cfg.get("url"):
            if self.cfg.get("auto_refresh", True):
                self.root.after(500, lambda: self.refresh_server(silent=True))
        else:
            self.root.after(400, self.open_settings)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # -- fenêtre ------------------------------------------------------------ #

    def _setup_window(self):
        root, px = self.root, self.theme.px
        root.title(APP_NAME)
        root.geometry("%dx%d" % (px(940), px(740)))
        root.minsize(px(780), px(620))
        try:
            root.iconbitmap(default=str(ASSETS_DIR / "icon.ico"))
        except tk.TclError:
            pass
        dark_title_bar(root, self.theme.dark)
        root.bind_all("<Control-v>", self._on_paste_key)
        root.bind_all("<Control-V>", self._on_paste_key)
        root.bind("<F5>", lambda e: self.refresh_server())
        root.bind("<Control-Return>", lambda e: self.send_all())

    def _build_ui(self):
        theme, c, px = self.theme, self.theme.c, self.theme.px
        root = self.root

        # ---- bandeau ----
        header = tk.Frame(root, bg=c["header"])
        header.pack(fill="x")
        bar = tk.Frame(header, bg=c["header"])
        bar.pack(fill="x", padx=px(16), pady=px(10))
        try:
            self._logo = tk.PhotoImage(
                file=str(ASSETS_DIR / ("icon-64.png" if theme.scale >= 1.4 else "icon-32.png")))
            tk.Label(bar, image=self._logo, bg=c["header"]).pack(side="left", padx=(0, px(10)))
        except tk.TclError:
            self._logo = None
        titles = tk.Frame(bar, bg=c["header"])
        titles.pack(side="left")
        tk.Label(titles, text=APP_NAME, bg=c["header"], fg=c["header_text"],
                 font=theme.font_title).pack(anchor="w")
        self.pill = StatusPill(titles, theme, c["header"], c["header_muted"])
        self.pill.pack(anchor="w")

        FlatButton(bar, theme, "Paramètres", icon="settings", kind="header",
                   command=self.open_settings).pack(side="right")
        FlatButton(bar, theme, "Intégration", icon="puzzle", kind="header",
                   command=self.open_integration,
                   tooltip="Windows, navigateur et dossier surveillé").pack(side="right", padx=px(4))
        self.btn_refresh = FlatButton(bar, theme, "Actualiser", icon="refresh", kind="header",
                                      command=self.refresh_server,
                                      tooltip="Relire les étiquettes et les dossiers de la seedbox (F5)")
        self.btn_refresh.pack(side="right")

        # ---- contenu ----
        content = tk.Frame(root, bg=c["bg"])
        content.pack(fill="both", expand=True, padx=px(16), pady=px(14))

        # pied : état + bouton d'envoi (placé d'abord pour rester visible à la réduction)
        footer = tk.Frame(content, bg=c["bg"])
        footer.pack(side="bottom", fill="x", pady=(px(12), 0))
        self.btn_send = FlatButton(footer, theme, "Envoyer vers la seedbox", icon="send",
                                   kind="primary", large=True, command=self.send_all,
                                   tooltip="Ctrl + Entrée")
        self.btn_send.pack(side="right")
        self.var_status = tk.StringVar(value="Prêt.")
        self.lbl_status = tk.Label(footer, textvariable=self.var_status, bg=c["bg"], fg=c["muted"],
                                   font=theme.font_small, anchor="w", justify="left")
        self.lbl_status.pack(side="left", fill="x", expand=True, padx=(0, px(14)))
        self.lbl_status.bind(
            "<Configure>", lambda e: self.lbl_status.configure(wraplength=max(e.width - 4, 100)))

        self.progress = ttk.Progressbar(content, mode="determinate",
                                        style="Accent.Horizontal.TProgressbar")
        self.progress.pack(side="bottom", fill="x", pady=(px(12), 0))

        # destination
        dest = Card(content, theme, "Destination sur la seedbox")
        dest.pack(side="bottom", fill="x", pady=(px(12), 0))
        body = dest.body
        body.columnconfigure(1, weight=1)
        self.var_dir = tk.StringVar()
        self.var_label = tk.StringVar()
        ttk.Label(body, text="Dossier", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", pady=px(4), padx=(0, px(14)))
        self.cmb_dir = ttk.Combobox(body, textvariable=self.var_dir, values=[])
        self.cmb_dir.grid(row=0, column=1, sticky="ew", pady=px(4))
        self.btn_fav_dir = self._star_button(body, "favorite_dirs", self.var_dir, "ce dossier")
        self.btn_fav_dir.grid(row=0, column=2, padx=(px(8), 0))
        ttk.Label(body, text="Étiquette", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", pady=px(4), padx=(0, px(14)))
        self.cmb_label = ttk.Combobox(body, textvariable=self.var_label, values=[])
        self.cmb_label.grid(row=1, column=1, sticky="ew", pady=px(4))
        self.btn_fav_label = self._star_button(body, "favorite_labels", self.var_label,
                                               "cette étiquette")
        self.btn_fav_label.grid(row=1, column=2, padx=(px(8), 0))
        ttk.Label(body, style="Muted.TLabel",
                  text="Laissez le dossier vide pour utiliser celui par défaut de rTorrent."
                  ).grid(row=2, column=1, sticky="w")
        options = tk.Frame(body, bg=c["card"])
        options.grid(row=3, column=0, columnspan=3, sticky="w", pady=(px(8), 0))
        self.var_start = tk.BooleanVar(value=True)
        CheckBox(options, theme, "Démarrer le torrent immédiatement", self.var_start).pack(
            side="left")
        self.var_clear = tk.BooleanVar(value=False)
        CheckBox(options, theme, "Vider la liste après un envoi réussi", self.var_clear).pack(
            side="left", padx=(px(28), 0))
        self.var_dir.trace_add("write", self._update_stars)
        self.var_label.trace_add("write", self._update_stars)

        # zone de dépôt
        hint = ("Déposez des fichiers .torrent ici, ou cliquez pour les choisir"
                if DND_AVAILABLE else "Cliquez pour choisir des fichiers .torrent")
        self.drop_zone = DropZone(
            content, theme, hint,
            "Ctrl + V colle un lien magnet ou une adresse · le navigateur peut aussi les envoyer ici",
            command=self.browse_files)
        self.drop_zone.pack(side="top", fill="x")

        # file d'attente
        card = Card(content, theme, "File d'attente")
        card.pack(side="top", fill="both", expand=True, pady=(px(12), 0))
        self.lbl_count = tk.Label(card.header, text="", bg=c["card"], fg=c["muted"],
                                  font=theme.font_small)
        self.lbl_count.pack(side="left", padx=(px(8), 0))
        FlatButton(card.header, theme, "Vider", icon="clear", kind="subtle",
                   command=self.clear_list).pack(side="right")
        FlatButton(card.header, theme, "Retirer", icon="delete", kind="subtle",
                   command=self.remove_selected, tooltip="Retire les lignes sélectionnées (Suppr)"
                   ).pack(side="right")
        FlatButton(card.header, theme, "Coller un lien", icon="paste", kind="subtle",
                   command=self.paste_clipboard, tooltip="Lien magnet ou adresse d'un .torrent (Ctrl + V)"
                   ).pack(side="right")
        FlatButton(card.header, theme, "Ajouter", icon="add", kind="subtle",
                   command=self.browse_files).pack(side="right")

        holder = tk.Frame(card.body, bg=c["card"])
        holder.pack(fill="both", expand=True)
        cols = ("name", "type", "size", "files", "status")
        self.tree = ttk.Treeview(holder, columns=cols, show="headings", selectmode="extended")
        for col, text, width, anchor, stretch in (
                ("name", "Torrent", 330, "w", True), ("type", "Type", 70, "w", False),
                ("size", "Taille", 88, "e", False), ("files", "Fichiers", 74, "center", False),
                ("status", "État", 250, "w", True)):
            self.tree.heading(col, text=text, anchor=anchor)
            self.tree.column(col, width=px(width), minwidth=px(50), anchor=anchor, stretch=stretch)
        self.tree.tag_configure("odd", background=c["row_alt"])
        self.tree.tag_configure("ok", foreground=c["success"])
        self.tree.tag_configure("err", foreground=c["danger"])
        self.tree.tag_configure("run", foreground=c["info"])
        self._scroll = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview,
                                     style="Flat.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=self._on_tree_scroll)
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self._scroll.grid(row=0, column=1, sticky="ns")
        self._scroll.grid_remove()
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<Double-1>", self._show_row_detail)
        self.lbl_empty = tk.Label(
            self.tree, bg=c["card"], fg=c["faint"], font=theme.font, justify="center",
            text="Aucun torrent en attente.\nDéposez un fichier, collez un lien ou "
                 "téléchargez un .torrent dans votre navigateur.")
        self._update_count()
        self._show_link_state()

    def _on_tree_scroll(self, first, last):
        """La barre de défilement n'apparaît que si la liste dépasse la zone visible."""
        self._scroll.set(first, last)
        needed = float(first) > 0.0 or float(last) < 1.0
        if needed and not self._scroll.winfo_ismapped():
            self._scroll.grid()
        elif not needed and self._scroll.winfo_ismapped():
            self._scroll.grid_remove()

    def _star_button(self, parent, key: str, var: tk.StringVar, what: str) -> FlatButton:
        theme = self.theme
        button = FlatButton(parent, theme, text="" if theme.has_icons else "☆", icon="star",
                            kind="subtle", command=lambda: self._toggle_favorite(key, var),
                            tooltip="Ajouter ou retirer %s des favoris" % what)
        return button

    # -- glisser-déposer et presse-papiers ---------------------------------- #

    def _setup_dnd(self):
        if not DND_AVAILABLE:
            return
        for widget in (self.drop_zone, self.tree):
            try:
                widget.drop_target_register(DND_FILES, DND_TEXT)
                widget.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

    def _on_drop(self, event):
        data = str(event.data or "").strip()
        if re.match(r"^(magnet:|https?://)", data, re.I):
            entries = data.split()
        else:
            try:
                entries = list(self.root.tk.splitlist(event.data))
            except Exception:
                entries = [data]
        self.drop_zone.flash()
        self.add_items(entries)

    def _on_paste_key(self, event):
        try:
            if event.widget.winfo_class() in _TEXT_WIDGETS:
                return None              # collage normal dans un champ de saisie
            if event.widget.winfo_toplevel() is not self.root:
                return None
        except (tk.TclError, AttributeError):
            return None
        self.paste_clipboard()
        return "break"

    def paste_clipboard(self):
        """Ajoute les liens magnet, adresses et chemins de .torrent du presse-papiers."""
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            text = ""
        entries = []
        for line in str(text).replace("\r", "\n").split("\n"):
            line = line.strip().strip('"')
            if re.match(r"^(magnet:\?|https?://)", line, re.I):
                entries.append(line)
            elif line.lower().endswith(".torrent") and Path(line).is_file():
                entries.append(line)
        if not entries:
            self.set_status("Le presse-papiers ne contient ni lien magnet, ni adresse, "
                            "ni chemin de fichier .torrent.")
            return
        self.add_items(entries)

    # -- configuration <-> interface ---------------------------------------- #

    def _apply_config(self):
        self.var_start.set(bool(self.cfg.get("start_torrents", True)))
        self.var_clear.set(bool(self.cfg.get("clear_after_upload", False)))
        self.var_dir.set(self.cfg.get("last_dir", ""))
        self.var_label.set(self.cfg.get("last_label", ""))
        # Dernières listes connues : visibles dès l'ouverture, sans attendre la
        # seedbox — et même si elle ne répond pas à ce moment-là.
        self._server_labels = [x for x in self.cfg.get("cache_labels") or []
                               if isinstance(x, str) and x]
        self._server_dirs = [x for x in self.cfg.get("cache_dirs") or []
                             if isinstance(x, str) and x]
        self._refresh_combos()
        if not self._link_state[0]:
            self._set_link("", "idle")

    def _refresh_combos(self, server_labels=None, server_dirs=None):
        if server_dirs is not None:
            self._server_dirs = list(server_dirs)
        if server_labels is not None:
            self._server_labels = list(server_labels)
        dirs = list(dict.fromkeys(
            [d for d in self.cfg.get("favorite_dirs", []) if d] + list(self._server_dirs)))
        labels = list(dict.fromkeys(
            [l for l in self.cfg.get("favorite_labels", []) if l] + list(self._server_labels)))
        self.cmb_dir.configure(values=dirs)
        self.cmb_label.configure(values=labels)
        self._update_stars()

    def _client(self) -> RuTorrentClient:
        return RuTorrentClient(
            self.cfg.get("url", ""), self.cfg.get("username", ""),
            self.cfg.get("password", ""), self.cfg.get("verify_ssl", True),
            self.cfg.get("timeout", 60))

    def _host(self) -> str:
        url = self.cfg.get("url") or ""
        try:
            return urllib.parse.urlparse(RuTorrentClient._normalize(url)).hostname or ""
        except ValueError:
            return ""

    def set_status(self, text: str):
        self.var_status.set(text)

    def _set_link(self, text: str, level: str):
        """État de la liaison avec la seedbox, affiché dans le bandeau."""
        self._link_state = (text, level)
        self._show_link_state()

    def _show_link_state(self):
        text, level = self._link_state
        host = self._host()
        if not host:
            self.pill.set("Seedbox non configurée", "idle")
        else:
            self.pill.set("%s — %s" % (host, text) if text else host, level)

    # -- favoris ------------------------------------------------------------ #

    def _toggle_favorite(self, key: str, var: tk.StringVar):
        value = var.get().strip()
        if not value:
            self.set_status("Saisissez d'abord une valeur à mettre en favori.")
            return
        favorites = list(self.cfg.get(key, []))
        if value in favorites:
            favorites.remove(value)
            self.set_status("« %s » retiré des favoris." % value)
        else:
            favorites.append(value)
            self.set_status("« %s » ajouté aux favoris : il restera proposé en tête de liste."
                            % value)
        self.cfg[key] = favorites
        save_config(self.cfg)
        self._refresh_combos()

    def _update_stars(self, *_):
        c = self.theme.c
        for var, key, button in ((self.var_dir, "favorite_dirs", self.btn_fav_dir),
                                 (self.var_label, "favorite_labels", self.btn_fav_label)):
            on = bool(var.get().strip()) and var.get().strip() in self.cfg.get(key, [])
            if self.theme.has_icons:
                button.set_icon("star_fill" if on else "star", c["warning"] if on else c["muted"])
            else:
                button.set_text("★" if on else "☆")

    # -- gestion de la liste ------------------------------------------------ #

    def browse_files(self):
        paths = filedialog.askopenfilenames(
            title="Choisir des fichiers .torrent", parent=self.root,
            filetypes=[("Fichiers torrent", "*.torrent"), ("Tous les fichiers", "*.*")])
        if paths:
            self.add_files(list(paths))

    def _insert_row(self, item: dict) -> str:
        info = item["info"]
        is_file = item["kind"] == "file"
        iid = self.tree.insert("", "end", values=(
            info["name"], KIND_LABELS.get(item["kind"], "?"),
            human_size(info["size"]) if is_file else "—",
            info["files"] if is_file else "—",
            item.get("text") or ST_PENDING))
        item.setdefault("status", "pending")
        item.setdefault("text", ST_PENDING)
        item.setdefault("tag", "")
        self.items[iid] = item
        self._restripe()
        return iid

    def _restripe(self):
        """Lignes alternées + couleur d'état, et compteur de la file."""
        for index, iid in enumerate(self.tree.get_children()):
            item = self.items.get(iid) or {}
            tags = [t for t in ("odd" if index % 2 else "", item.get("tag", "")) if t]
            self.tree.item(iid, tags=tuple(tags))
        self._update_count()

    def _update_count(self):
        total = len(self.items)
        waiting = sum(1 for it in self.items.values() if it.get("status") != "ok")
        if not total:
            self.lbl_count.configure(text="")
            self.lbl_empty.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self.lbl_empty.place_forget()
            self.lbl_count.configure(
                text="%d torrent%s" % (total, "s" if total > 1 else "")
                     + (" · %d à envoyer" % waiting if waiting != total else ""))

    def add_files(self, paths, announce: bool = True) -> int:
        added, skipped, errors = 0, 0, []

        expanded = []
        for raw in paths:
            p = Path(str(raw).strip().strip("{}"))
            if p.is_dir():
                expanded.extend(sorted(p.glob("*.torrent")))
            else:
                expanded.append(p)

        for p in expanded:
            if p.suffix.lower() != ".torrent":
                continue
            if not p.exists():
                errors.append("%s : introuvable" % p.name)
                continue
            try:
                info = parse_torrent(p)
            except Exception as exc:
                errors.append("%s : %s" % (p.name, exc))
                continue
            if self._already_queued(info["infohash"], p):
                skipped += 1
                continue
            self._insert_row({"kind": "file", "path": p, "info": info})
            added += 1

        if announce:
            msg = "%d torrent(s) ajouté(s)." % added
            if skipped:
                msg += " %d déjà présent(s)." % skipped
            self.set_status(msg)
            if errors:
                messagebox.showwarning(
                    APP_NAME, "Fichiers ignorés :\n\n" + "\n".join(errors[:10]), parent=self.root)
        elif errors:
            # Import automatique (surveillance, extension) : jamais de fenêtre
            # modale, qui bloquerait l'application sans que personne ne regarde.
            self.set_status("Fichier ignoré : %s" % errors[0])
            log_error("import automatique : " + " | ".join(errors[:10]))
        return added

    def _already_queued(self, infohash: str, path: Path | None = None) -> bool:
        for item in self.items.values():
            # un infohash vide (lien nu) ne doit jamais servir de clé d'égalité
            if infohash and item["info"].get("infohash") == infohash:
                return True
            if path is not None and item.get("path") and \
                    str(item["path"]).lower() == str(path).lower():
                return True
        return False

    def add_magnet(self, uri: str, announce: bool = True) -> bool:
        """Ajoute un lien magnet à la file."""
        try:
            info = parse_magnet(uri)
        except MagnetError as exc:
            if announce:
                messagebox.showwarning(APP_NAME, "Lien magnet ignoré :\n\n%s" % exc,
                                       parent=self.root)
            return False
        if self._already_queued(info["infohash"]):
            if announce:
                self.set_status("Ce lien magnet est déjà dans la liste.")
            return False
        self._insert_row({"kind": "magnet", "uri": uri, "info": info})
        if announce:
            self.set_status("Lien magnet ajouté : %s" % info["name"])
        return True

    def add_url(self, url: str, announce: bool = True) -> bool:
        """Ajoute l'adresse d'un .torrent : ruTorrent le téléchargera lui-même."""
        if any(it.get("uri") == url for it in self.items.values()):
            if announce:
                self.set_status("Ce lien est déjà dans la liste.")
            return False
        name = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])
        info = {"name": name or url[:60], "size": 0, "files": 0, "infohash": ""}
        self._insert_row({"kind": "url", "uri": url, "info": info})
        if announce:
            self.set_status("Lien ajouté : %s" % info["name"])
        return True

    def add_items(self, entries, announce: bool = True) -> int:
        """Point d'entrée unique : chemins, magnets et adresses mélangés.

        L'ordre d'arrivée est conservé : une sélection mixte s'affiche telle
        qu'elle a été déposée.
        """
        count = 0
        for raw in entries:
            text = str(raw).strip().strip('"').strip("{}")
            if not text:
                continue
            if text.lower().startswith("magnet:"):
                count += 1 if self.add_magnet(text, announce=False) else 0
            elif re.match(r"^https?://", text, re.I):
                count += 1 if self.add_url(text, announce=False) else 0
            else:
                count += self.add_files([text], announce=False)
        if announce:
            self.set_status("%d élément(s) ajouté(s) à la file." % count if count else
                            "Rien de nouveau à ajouter (déjà dans la liste, ou non reconnu).")
        return count

    def remove_selected(self):
        selection = self.tree.selection()
        for iid in selection:
            self.tree.delete(iid)
            self.items.pop(iid, None)
        self._restripe()
        if selection:
            self.set_status("%d ligne(s) retirée(s)." % len(selection))

    def clear_list(self):
        for iid in list(self.items):
            try:
                self.tree.delete(iid)
            except tk.TclError:
                pass
        self.items.clear()
        self.progress.configure(value=0)
        self._update_count()
        self.set_status("Liste vidée.")

    def _set_status(self, iid: str, text: str, tag: str = ""):
        item = self.items.get(iid)
        if item is None:
            return
        item["text"], item["tag"] = text, tag
        try:
            self.tree.set(iid, "status", text)
        except tk.TclError:
            return
        self._restripe()

    def _show_row_detail(self, event):
        """Double-clic : affiche l'état complet (les messages d'erreur sont tronqués)."""
        iid = self.tree.identify_row(event.y)
        item = self.items.get(iid)
        if not item:
            return
        info = item["info"]
        lines = [info["name"], "", "Type : %s" % KIND_LABELS.get(item["kind"], "?")]
        if item.get("path"):
            lines.append("Fichier : %s" % item["path"])
        if info.get("infohash"):
            lines.append("Empreinte : %s" % info["infohash"])
        lines += ["", "État : %s" % item.get("text", "")]
        messagebox.showinfo(APP_NAME, "\n".join(lines), parent=self.root)

    # -- passerelle thread -> interface ------------------------------------- #

    def post(self, fn):
        """Fait exécuter fn() par le thread principal. Appelable depuis un thread."""
        self._inbox.put(fn)

    def _pump(self):
        """Draine la file des tâches déposées par les threads."""
        while True:
            try:
                fn = self._inbox.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as exc:
                log_error("traitement différé", exc)
        if not self._closing:
            self._pump_job = self.root.after(120, self._pump)

    def submit_external(self, entries):
        """Entrée thread-safe utilisée par le service local."""
        entries = list(entries)
        self.post(lambda: self.receive_external(entries))

    def request_show(self):
        """Entrée thread-safe : un second lancement demande la fenêtre."""
        self.post(self.bring_to_front)

    def receive_external(self, entries):
        """Éléments venus d'une autre instance, de l'extension ou de l'hôte natif."""
        added = self.add_items(entries, announce=True)
        self.bring_to_front()
        if added:
            self._auto_send()

    def bring_to_front(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(400, lambda: self.root.attributes("-topmost", False))
            self.root.focus_force()
        except tk.TclError:
            pass

    # -- surveillance d'un dossier ------------------------------------------ #

    def default_watch_folder(self) -> Path:
        if wi is not None:
            try:
                return wi.downloads_folder()
            except Exception:
                pass
        return Path.home() / "Downloads"

    def watch_folder(self) -> Path:
        custom = (self.cfg.get("watch_folder") or "").strip()
        return Path(custom) if custom else self.default_watch_folder()

    def watch_folder_display(self) -> str:
        return str(self.watch_folder())

    def restart_watcher(self):
        """(Re)démarre la surveillance selon la configuration courante."""
        self._watch_stop.set()
        self._watch_thread = None
        if not self.cfg.get("watch_downloads"):
            return
        folder = self.watch_folder()
        if not folder.is_dir():
            self.set_status("Surveillance impossible : %s est introuvable." % folder)
            return
        self._watch_stop = threading.Event()
        try:
            interval = max(1, int(self.cfg.get("watch_interval", 3) or 3))
        except (TypeError, ValueError):
            interval = 3
        self._watch_thread = threading.Thread(
            target=self._watch_loop, args=(folder, self._watch_stop, interval), daemon=True)
        self._watch_thread.start()
        self.set_status("Surveillance active : %s" % folder)

    def _watch_loop(self, folder: Path, stop: threading.Event, interval: int):
        # Amorçage : tout ce qui est déjà là est ignoré, on ne veut que le nouveau.
        seen = set()
        try:
            for p in folder.glob("*.torrent"):
                seen.add(str(p).lower())
        except OSError:
            pass
        while not stop.wait(interval):
            try:
                candidates = list(folder.glob("*.torrent"))
            except OSError:
                continue
            # on oublie les fichiers disparus : recréés plus tard, ils seront repris
            seen &= {str(p).lower() for p in candidates}
            fresh = []
            for p in candidates:
                key = str(p).lower()
                if key in seen:
                    continue
                if torrent_is_ready(p):      # complet, déverrouillé, bencode valide
                    seen.add(key)
                    fresh.append(str(p))
            if fresh and not stop.is_set():
                self.post(lambda f=fresh: self._on_watched(f))

    def _on_watched(self, paths):
        added = self.add_files(paths, announce=False)
        if not added:
            return
        self.set_status("%d torrent(s) importé(s) depuis le dossier surveillé." % added)
        self.bring_to_front()
        self._auto_send()

    def _auto_send(self):
        """Envoi automatique d'un arrivage, différé si un envoi est déjà en cours."""
        if not (self.cfg.get("watch_auto_send") and self.cfg.get("url")):
            return
        if self.busy:
            self._send_again = True       # repris par _upload_done
        else:
            self.root.after(300, self.send_all)

    # -- fenêtres secondaires ----------------------------------------------- #

    def open_integration(self):
        dlg = IntegrationDialog(self.root, self)
        self.root.wait_window(dlg)

    def open_settings(self):
        dlg = SettingsDialog(self.root, self.theme, self.cfg)
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        previous_url, previous_theme = self.cfg.get("url"), self.cfg.get("theme", "system")
        # seulement ce que la fenêtre modifie : un relevé arrivé pendant qu'elle était
        # ouverte (listes, cache) ne doit pas être écrasé par sa copie de départ
        self.cfg.update({k: dlg.result[k] for k in SETTINGS_KEYS if k in dlg.result})
        if self.cfg.get("url") != previous_url:
            # listes d'une autre seedbox : elles n'ont plus de sens ici
            self.cfg.update({"cache_labels": [], "cache_dirs": [], "cache_time": ""})
            self._server_labels, self._server_dirs = [], []
            self._set_link("", "idle")
            self._refresh_combos()
        save_config(self.cfg)
        self.set_status("Paramètres enregistrés.")
        if self.cfg.get("theme", "system") != previous_theme:
            if self.busy:
                self.set_status("Paramètres enregistrés. La nouvelle apparence sera appliquée "
                                "au prochain démarrage.")
            else:
                self._rebuild_ui()
        self._show_link_state()
        self.refresh_server(silent=True)

    def _rebuild_ui(self):
        """Reconstruit la fenêtre avec le thème choisi, sans perdre la file d'attente."""
        kept = [self.items[iid] for iid in self.tree.get_children() if iid in self.items]
        self.cfg["last_dir"] = self.var_dir.get().strip()
        self.cfg["last_label"] = self.var_label.get().strip()
        self.cfg["start_torrents"] = bool(self.var_start.get())
        self.cfg["clear_after_upload"] = bool(self.var_clear.get())
        labels, dirs, status = self._server_labels, self._server_dirs, self.var_status.get()
        for child in self.root.winfo_children():
            child.destroy()
        self.items = {}
        self.theme = Theme(self.root, self.cfg.get("theme", "system"))
        self.theme.apply(self.root)
        dark_title_bar(self.root, self.theme.dark)
        self._build_ui()
        self._apply_config()
        self._refresh_combos(server_labels=labels, server_dirs=dirs)
        self._setup_dnd()
        for item in kept:
            self._insert_row(item)
        self.btn_refresh.set_enabled(not self._refreshing)
        self.set_status(status)

    # -- lecture des étiquettes et des dossiers ------------------------------ #

    def refresh_server(self, silent: bool = False, auto: bool = False):
        if not self.cfg.get("url"):
            if not silent:
                messagebox.showinfo(APP_NAME, "Configurez d'abord l'accès à votre seedbox.",
                                    parent=self.root)
            return
        if self._refreshing:
            return
        if auto and self.busy:
            # un envoi est en cours : ne pas ajouter une requête à la file de
            # rTorrent, déjà occupé à créer le torrent ; on repasse plus tard.
            self._cancel_auto_retry()
            self._auto_retry_job = self.root.after(
                10000, lambda: self.refresh_server(silent=True, auto=True))
            return
        if not auto:                      # clic manuel : on repart d'une séquence neuve
            self._cancel_auto_retry()
            self._auto_retry_index = 0
        try:
            client = self._client()
        except Exception as exc:
            log_error("paramètres de connexion inutilisables", exc)
            self.set_status("Paramètres de connexion inutilisables : %s" % exc)
            return
        self._refreshing = True
        self.btn_refresh.set_enabled(False)
        self.set_status("Lecture des étiquettes et des dossiers…")
        self._set_link("connexion…", "busy")

        def worker():
            try:
                labels, dirs, count = client.fetch_labels_and_dirs()
                self.post(lambda: self._on_refreshed(labels, dirs, count, None, False))
            except RuTorrentError as exc:
                err, transient = str(exc), exc.transient
                self.post(lambda: self._on_refreshed([], [], 0, err, transient))
            except Exception as exc:
                log_error("actualisation", exc)
                err = str(exc)
                self.post(lambda: self._on_refreshed([], [], 0, err, False))

        threading.Thread(target=worker, daemon=True).start()

    def _on_refreshed(self, labels, dirs, count, error, transient=False):
        self._refreshing = False
        if not self.busy:
            self.btn_refresh.set_enabled(True)
        have_cache = bool(self._server_labels or self._server_dirs)

        if error:
            parts = ["Actualisation impossible : %s." % error.rstrip(".")]
            if have_cache:
                when = self.cfg.get("cache_time")
                parts.append("Listes précédentes conservées%s."
                             % (" (relevé du %s)" % when if when else ""))
            if transient:
                delay = self._schedule_auto_retry()
                if delay:
                    parts.append("Nouvel essai automatique dans %d s." % delay)
            self.set_status(" ".join(parts))
            self._set_link("ne répond pas" if transient else "erreur", "warn" if transient else "error")
            return

        self._cancel_auto_retry()
        self._auto_retry_index = 0
        self._set_link("connectée", "ok")

        if count == 0 and have_cache:
            # rTorrent qui redémarre liste 0 torrent le temps de recharger sa session :
            # ne pas effacer des listes valides sur la foi d'une réponse vide.
            self.set_status("La seedbox ne liste aucun torrent pour l'instant "
                            "(redémarrage en cours ?) — listes précédentes conservées.")
            return

        self._refresh_combos(server_labels=labels, server_dirs=dirs)
        self.cfg["cache_labels"] = list(labels)
        self.cfg["cache_dirs"] = list(dirs)
        self.cfg["cache_time"] = time.strftime("%d/%m/%Y %H:%M")
        save_config(self.cfg)
        self.set_status("Seedbox : %d torrent(s), %d étiquette(s), %d dossier(s) connus."
                        % (count, len(labels), len(dirs)))

    def _schedule_auto_retry(self) -> int:
        if self._closing or self._auto_retry_index >= len(self.AUTO_RETRY_DELAYS):
            return 0
        delay = self.AUTO_RETRY_DELAYS[self._auto_retry_index]
        self._auto_retry_index += 1
        self._cancel_auto_retry()
        self._auto_retry_job = self.root.after(
            delay * 1000, lambda: self.refresh_server(silent=True, auto=True))
        return delay

    def _cancel_auto_retry(self):
        if self._auto_retry_job is not None:
            try:
                self.root.after_cancel(self._auto_retry_job)
            except tk.TclError:
                pass
            self._auto_retry_job = None

    # -- envoi --------------------------------------------------------------- #

    def send_all(self):
        if self.busy:
            return
        if not self.cfg.get("url"):
            messagebox.showinfo(APP_NAME, "Configurez d'abord l'accès à votre seedbox.",
                                parent=self.root)
            self.open_settings()
            return
        pending = [iid for iid in self.tree.get_children()
                   if iid in self.items and self.items[iid]["status"] != "ok"]
        if not pending:
            self.set_status("Aucun torrent à envoyer.")
            return

        directory = self.var_dir.get().strip()
        label = self.var_label.get().strip()
        start = bool(self.var_start.get())
        self._remember_choices()
        try:
            client = self._client()
        except Exception as exc:
            log_error("paramètres de connexion inutilisables", exc)
            self.set_status("Paramètres de connexion inutilisables : %s" % exc)
            return

        self.busy = True
        self._send_again = False
        self.btn_send.set_enabled(False)
        self.btn_refresh.set_enabled(False)
        self.progress.configure(value=0, maximum=len(pending))

        threading.Thread(
            target=self._upload_worker,
            args=(client, pending, directory, label, start),
            daemon=True,
        ).start()

    def _remember_choices(self):
        self.cfg["last_dir"] = self.var_dir.get().strip()
        self.cfg["last_label"] = self.var_label.get().strip()
        self.cfg["start_torrents"] = bool(self.var_start.get())
        self.cfg["clear_after_upload"] = bool(self.var_clear.get())
        save_config(self.cfg)

    def _upload_worker(self, client, iids, directory, label, start):
        ok = fail = 0
        for index, iid in enumerate(iids, 1):
            item = self.items.get(iid)
            if not item:
                continue
            name = item["info"]["name"]
            self.post(lambda i=iid: self._set_status(i, ST_SENDING, "run"))
            self.post(lambda n=name: self.set_status("Envoi de « %s »…" % n))
            try:
                text = self._send_one(client, item, directory, label, start)
                item["status"] = "ok"
                self.post(lambda i=iid, t=text: self._set_status(i, t, "ok"))
                ok += 1
            except Exception as exc:
                item["status"] = "error"
                msg = str(exc)
                if isinstance(exc, RuTorrentError) and exc.transient:
                    msg += " — réessayez dans un instant"
                elif not isinstance(exc, RuTorrentError):
                    log_error("envoi de %s" % name, exc)
                self.post(lambda i=iid, m=msg: self._set_status(i, ST_FAILED + m, "err"))
                fail += 1
            self.post(lambda v=index: self.progress.configure(value=v))

        self.post(lambda: self._upload_done(ok, fail, directory, label, iids))

    def _send_one(self, client: RuTorrentClient, item: dict,
                  directory: str, label: str, start: bool) -> str:
        """Envoie un élément et retourne le libellé d'état. Lève en cas d'échec."""
        infohash = item["info"].get("infohash") or ""

        def add():
            if item.get("kind") in ("magnet", "url"):
                client.add_url(item["uri"], directory, label, start)
            else:
                client.add_torrent(item["path"], directory, label, start)

        try:
            add()
        except RuTorrentError as exc:
            if not exc.transient:
                raise
            # 1. Une erreur 5xx ou une coupure peut survenir APRÈS que rTorrent a
            #    pris le torrent : on vérifie avant de conclure à un échec.
            if infohash and self._confirm(client, infohash, budget=8.0):
                return ST_OK_AFTER_ERROR
            # 2. rTorrent répond, mais ruTorrent échoue : signature du cache de
            #    ruTorrent corrompu. On le reconstruit comme le ferait un
            #    navigateur, puis on renvoie une seule fois.
            if not (client.rtorrent_alive() and client.repair_settings_cache()):
                raise
            add()
            if infohash and self._confirm(client, infohash):
                return ST_OK_REPAIRED
            return ST_SENT_REPAIRED

        if infohash and self._confirm(client, infohash):
            return ST_OK
        return ST_SENT

    @staticmethod
    def _confirm(client: RuTorrentClient, infohash: str,
                 budget: float = 15.0, interval: float = 2.0) -> bool:
        """Cherche le torrent sur la seedbox pendant `budget` secondes au plus.

        Juste après un ajout, rTorrent est souvent occupé (création des fichiers,
        vérification) et peut répondre 500 : un échec passager ne clôt pas la
        vérification, il est simplement suivi d'un nouvel essai.
        """
        deadline = time.time() + budget
        while True:
            time.sleep(interval)
            try:
                if client.has_infohash(infohash):
                    return True
            except RuTorrentError as exc:
                if not exc.transient:
                    return False
            except Exception:
                return False
            if time.time() >= deadline:
                return False

    def _upload_done(self, ok: int, fail: int, directory: str = "", label: str = "",
                     batch=()):
        self.busy = False
        self.btn_send.set_enabled(True)
        self.btn_refresh.set_enabled(not self._refreshing)
        summary = "Terminé : %d réussi(s), %d échec(s)." % (ok, fail)
        self.set_status(summary)
        if ok:
            # ce qui vient de servir est proposé sans attendre la prochaine actualisation
            if directory and directory not in self._server_dirs:
                self._server_dirs.append(directory)
            if label and label not in self._server_labels:
                self._server_labels.append(label)
            self._refresh_combos()
            self._discard_sent_copies()
        self._restripe()
        if fail == 0 and ok and self.var_clear.get():
            # uniquement les lignes de CE lot parties avec succès : ce qui est arrivé
            # pendant l'envoi n'a pas encore été envoyé et doit rester dans la file
            for iid in batch:
                if self.items.get(iid, {}).get("status") == "ok":
                    try:
                        self.tree.delete(iid)
                    except tk.TclError:
                        pass
                    self.items.pop(iid, None)
            self._restripe()
            self.set_status(summary + " Envois réussis retirés de la liste.")
        if self._send_again:
            self._send_again = False
            if any(it.get("status") != "ok" for it in self.items.values()):
                self.root.after(300, self.send_all)
        if fail:
            messagebox.showwarning(
                APP_NAME, "%d envoi(s) ont échoué.\nLe détail figure dans la colonne « État » "
                          "(double-clic sur une ligne pour le lire en entier)." % fail,
                parent=self.root)

    def _discard_sent_copies(self):
        """Supprime les copies de .torrent reçues du navigateur une fois envoyées."""
        try:
            inbox = inbox_dir().resolve()
        except OSError:
            return
        for item in self.items.values():
            path = item.get("path")
            if item.get("status") == "ok" and path:
                try:
                    if Path(path).resolve().parent == inbox:
                        Path(path).unlink()
                except OSError:
                    pass

    # -- fermeture ----------------------------------------------------------- #

    def on_close(self):
        self._remember_choices()
        self._closing = True
        self._cancel_auto_retry()
        if self._pump_job is not None:
            try:
                self.root.after_cancel(self._pump_job)
            except tk.TclError:
                pass
        self._watch_stop.set()
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
        self.root.destroy()

