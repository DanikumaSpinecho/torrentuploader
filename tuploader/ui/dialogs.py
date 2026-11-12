# -*- coding: utf-8 -*-
"""Fenêtres secondaires : paramètres de la seedbox, intégration Windows / navigateur."""

from __future__ import annotations

import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .. import APP_NAME
from ..config import save_config
from ..rutorrent import RuTorrentClient, RuTorrentError
from ..service import IPC_HOST
from .theme import THEME_CHOICES, Theme, dark_title_bar
from .widgets import Card, CheckBox, FlatButton

try:
    from .. import winintegration as wi
except Exception:                         # plateforme sans registre
    wi = None


class BaseDialog(tk.Toplevel):
    """Fenêtre modale centrée sur son parent, habillée par le thème."""

    def __init__(self, parent, theme: Theme, title: str):
        super().__init__(parent)
        self.withdraw()                   # évite un flash avant le placement
        self.theme = theme
        self.title(title)
        self.configure(bg=theme.c["bg"])
        self.resizable(False, False)
        self.transient(parent)
        self._parent = parent
        self.content = tk.Frame(self, bg=theme.c["bg"])
        self.content.pack(fill="both", expand=True, padx=theme.px(16), pady=theme.px(16))
        self.bind("<Escape>", lambda e: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)

    def present(self):
        """À appeler une fois le contenu construit."""
        self.update_idletasks()
        parent = self._parent
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_reqwidth()) // 2
            y = parent.winfo_rooty() + self.theme.px(40)
        except tk.TclError:
            x = y = 100
        self.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        self.deiconify()
        dark_title_bar(self, self.theme.dark)
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.focus_set()

    def close(self):
        self.destroy()

    def button_bar(self) -> tk.Frame:
        bar = tk.Frame(self.content, bg=self.theme.c["bg"])
        bar.pack(fill="x", pady=(self.theme.px(14), 0))
        return bar


# --------------------------------------------------------------------------- #
#  Paramètres
# --------------------------------------------------------------------------- #

class SettingsDialog(BaseDialog):
    def __init__(self, parent, theme: Theme, cfg: dict):
        super().__init__(parent, theme, "Paramètres")
        self.cfg = dict(cfg)
        self.result: dict | None = None
        self._test_result = None
        c, px = theme.c, theme.px

        # ---- seedbox ----
        card = Card(self.content, theme, "Seedbox ruTorrent")
        card.pack(fill="x")
        body = card.body
        body.columnconfigure(1, weight=1)

        def row(r, label, widget, hint=""):
            ttk.Label(body, text=label, style="Card.TLabel").grid(
                row=r, column=0, sticky="w", pady=px(5), padx=(0, px(14)))
            widget.grid(row=r, column=1, sticky="ew", pady=px(5))
            if hint:
                ttk.Label(body, text=hint, style="Muted.TLabel").grid(
                    row=r + 1, column=1, sticky="w")

        self.var_url = tk.StringVar(value=cfg.get("url", ""))
        self.ent_url = ttk.Entry(body, textvariable=self.var_url, width=52)
        row(0, "Adresse de ruTorrent", self.ent_url, "Exemple : https://maseedbox.com/rutorrent")

        self.var_user = tk.StringVar(value=cfg.get("username", ""))
        row(2, "Identifiant", ttk.Entry(body, textvariable=self.var_user))

        self.var_pass = tk.StringVar(value=cfg.get("password", ""))
        self.ent_pass = ttk.Entry(body, textvariable=self.var_pass, show="•")
        row(3, "Mot de passe", self.ent_pass)

        self.var_show = tk.BooleanVar(value=False)
        CheckBox(body, theme, "Afficher le mot de passe", self.var_show,
                 command=self._toggle_pass).grid(row=4, column=1, sticky="w", pady=(px(2), 0))

        self.var_ssl = tk.BooleanVar(value=bool(cfg.get("verify_ssl", True)))
        CheckBox(body, theme, "Vérifier le certificat SSL (à décocher seulement pour un "
                              "certificat auto-signé)", self.var_ssl).grid(
                                  row=5, column=1, sticky="w", pady=(px(6), 0))

        self.var_timeout = tk.StringVar(value=str(cfg.get("timeout", 60)))
        spin = ttk.Spinbox(body, from_=10, to=600, increment=5, textvariable=self.var_timeout, width=7)
        ttk.Label(body, text="Délai réseau (secondes)", style="Card.TLabel").grid(
            row=6, column=0, sticky="w", pady=px(8), padx=(0, px(14)))
        spin.grid(row=6, column=1, sticky="w", pady=px(8))

        # ---- application ----
        card2 = Card(self.content, theme, "Application")
        card2.pack(fill="x", pady=(px(12), 0))
        body2 = card2.body
        body2.columnconfigure(1, weight=1)
        self.var_auto = tk.BooleanVar(value=bool(cfg.get("auto_refresh", True)))
        CheckBox(body2, theme, "Lire les étiquettes et les dossiers de la seedbox au démarrage",
                 self.var_auto).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(body2, text="Apparence", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", pady=(px(8), 0), padx=(0, px(14)))
        labels = [label for _key, label in THEME_CHOICES]
        current = dict(THEME_CHOICES).get(cfg.get("theme", "system"), labels[0])
        self.var_theme = tk.StringVar(value=current)
        ttk.Combobox(body2, textvariable=self.var_theme, values=labels, state="readonly",
                     width=30).grid(row=1, column=1, sticky="w", pady=(px(8), 0))

        # ---- résultat du test + boutons ----
        self.lbl_test = tk.Label(self.content, text="", bg=c["bg"], fg=c["muted"],
                                 font=theme.font_small, wraplength=px(520), justify="left",
                                 anchor="w")
        self.lbl_test.pack(fill="x", pady=(px(10), 0))

        bar = self.button_bar()
        self.btn_test = FlatButton(bar, theme, "Tester la connexion", icon="connect",
                                   command=self._test)
        self.btn_test.pack(side="left")
        FlatButton(bar, theme, "Enregistrer", icon="check", kind="primary",
                   command=self._save).pack(side="right")
        FlatButton(bar, theme, "Annuler", command=self.close).pack(side="right", padx=px(8))

        self.bind("<Return>", lambda e: self._save())
        self.present()
        self.ent_url.focus_set()

    def _toggle_pass(self):
        self.ent_pass.configure(show="" if self.var_show.get() else "•")

    def _collect(self) -> dict:
        cfg = dict(self.cfg)
        try:
            timeout = min(600, max(10, int(float(self.var_timeout.get()))))
        except (TypeError, ValueError, OverflowError):
            timeout = 60
        theme_key = {label: key for key, label in THEME_CHOICES}.get(self.var_theme.get(), "system")
        cfg.update({
            "url": self.var_url.get().strip(),
            "username": self.var_user.get().strip(),
            "password": self.var_pass.get(),
            "verify_ssl": bool(self.var_ssl.get()),
            "auto_refresh": bool(self.var_auto.get()),
            "timeout": timeout,
            "theme": theme_key,
        })
        return cfg

    def _say(self, text: str, level: str = "muted"):
        color = {"ok": "success", "error": "danger"}.get(level, "muted")
        try:
            self.lbl_test.configure(text=text, fg=self.theme.c[color])
        except tk.TclError:
            pass

    def _test(self):
        cfg = self._collect()
        if not cfg["url"]:
            self._say("Indiquez d'abord l'adresse de ruTorrent.", "error")
            return
        self.btn_test.set_enabled(False)
        self._say("Connexion en cours…")
        self._test_result = None

        def worker():
            client = RuTorrentClient(cfg["url"], cfg["username"], cfg["password"],
                                     cfg["verify_ssl"], cfg["timeout"])
            try:
                result = (client.test_connection(), "ok")
            except RuTorrentError as exc:
                text = str(exc)
                result = (text[:1].upper() + text[1:], "error")
            except Exception as exc:
                result = ("Erreur inattendue : %s" % exc, "error")
            self._test_result = result      # jamais de widget depuis un thread

        threading.Thread(target=worker, daemon=True).start()
        self.after(150, self._poll_test)

    def _poll_test(self):
        """Sondage depuis le thread principal : tkinter n'est pas thread-safe."""
        if self._test_result is None:
            try:
                self.after(150, self._poll_test)
            except tk.TclError:
                pass
            return
        self._say(*self._test_result)
        try:
            self.btn_test.set_enabled(True)
        except tk.TclError:
            pass

    def _save(self):
        cfg = self._collect()
        if not cfg["url"]:
            messagebox.showwarning(APP_NAME, "L'adresse de ruTorrent est obligatoire.", parent=self)
            return
        self.result = cfg
        self.destroy()


# --------------------------------------------------------------------------- #
#  Intégration Windows et navigateur
# --------------------------------------------------------------------------- #

class IntegrationDialog(BaseDialog):
    def __init__(self, parent, app):
        super().__init__(parent, app.theme, "Intégration Windows et navigateur")
        self.app = app
        theme, c, px = self.theme, self.theme.c, self.theme.px
        wrap = px(560)

        # ---- 1. Windows ----
        card = Card(self.content, theme, "Windows")
        card.pack(fill="x")
        tk.Label(card.body, bg=c["card"], fg=c["muted"], font=theme.font_small, justify="left",
                 wraplength=wrap, anchor="w",
                 text="Ouvre les fichiers .torrent et les liens magnet avec l'application, depuis "
                      "l'Explorateur comme depuis le navigateur. Tout est enregistré dans votre "
                      "profil : aucun droit administrateur, et « Désactiver » retire tout."
                 ).pack(fill="x")
        self.rows = tk.Frame(card.body, bg=c["card"])
        self.rows.pack(fill="x", pady=(px(10), px(10)))
        bar = tk.Frame(card.body, bg=c["card"])
        bar.pack(fill="x")
        self.btn_on = FlatButton(bar, theme, "Activer", icon="check", kind="primary",
                                 command=self.do_register)
        self.btn_on.pack(side="left")
        self.btn_off = FlatButton(bar, theme, "Désactiver", command=self.do_unregister)
        self.btn_off.pack(side="left", padx=px(8))
        FlatButton(bar, theme, "Applications par défaut…", icon="apps",
                   command=self.open_default_apps,
                   tooltip="Ouvre les réglages de Windows, si un autre programme est prioritaire"
                   ).pack(side="left")
        FlatButton(bar, theme, "Raccourci sur le Bureau", icon="pin",
                   command=self.make_shortcut,
                   tooltip="On peut déposer des .torrent sur ce raccourci").pack(side="left", padx=px(8))

        # ---- 2. extension ----
        card2 = Card(self.content, theme, "Extension de navigateur (Chrome, Edge)")
        card2.pack(fill="x", pady=(px(12), 0))
        tk.Label(card2.body, bg=c["card"], fg=c["muted"], font=theme.font_small, justify="left",
                 wraplength=wrap, anchor="w",
                 text="L'extension envoie ici les .torrent que vous téléchargez et ajoute "
                      "« Envoyer vers la seedbox » au clic droit sur un lien. Elle trouve "
                      "l'application toute seule — et la lance si elle est fermée — dès que "
                      "l'intégration Windows est activée. Elle s'installe à part : voir la notice."
                 ).pack(fill="x")
        self.ext_rows = tk.Frame(card2.body, bg=c["card"])
        self.ext_rows.pack(fill="x", pady=(px(10), 0))
        self.manual = tk.Frame(card2.body, bg=c["card"])
        self.btn_manual = FlatButton(card2.body, theme, "Connexion manuelle (avancé)", kind="subtle",
                                     icon="info", command=self._toggle_manual)
        self.btn_manual.pack(anchor="w", pady=(px(6), 0))
        self._build_manual()

        # ---- 3. dossier surveillé ----
        card3 = Card(self.content, theme, "Dossier surveillé (facultatif)")
        card3.pack(fill="x", pady=(px(12), 0))
        b3 = card3.body
        b3.columnconfigure(1, weight=1)
        self.var_watch = tk.BooleanVar(value=bool(app.cfg.get("watch_downloads")))
        CheckBox(b3, theme, "Importer automatiquement les nouveaux .torrent de ce dossier",
                 self.var_watch, command=self._apply_watch).grid(
                     row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(b3, text="Dossier", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", pady=px(6), padx=(0, px(12)))
        self.var_folder = tk.StringVar(value=app.watch_folder_display())
        entry = ttk.Entry(b3, textvariable=self.var_folder)
        entry.grid(row=1, column=1, sticky="ew", pady=px(6))
        entry.bind("<FocusOut>", lambda e: self._apply_watch())
        FlatButton(b3, theme, icon="folder", text="Parcourir…", command=self.pick_folder).grid(
            row=1, column=2, padx=(px(8), 0))
        self.var_autosend = tk.BooleanVar(value=bool(app.cfg.get("watch_auto_send")))
        CheckBox(b3, theme, "Envoyer aussitôt à la seedbox tout torrent reçu automatiquement "
                            "(dossier surveillé ou extension)", self.var_autosend,
                 command=self._apply_watch, wraplength=wrap).grid(
                     row=2, column=0, columnspan=3, sticky="w")
        tk.Label(b3, bg=c["card"], fg=c["muted"], font=theme.font_small, justify="left",
                 wraplength=wrap, anchor="w",
                 text="Inutile si vous utilisez l'extension. L'envoi immédiat utilise le dossier "
                      "de destination et l'étiquette affichés dans la fenêtre principale."
                 ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(px(6), 0))

        bar = self.button_bar()
        FlatButton(bar, theme, "Fermer", command=self.close).pack(side="right")

        self.refresh()
        self.present()

    # -- affichage de l'état ------------------------------------------------ #

    def _status_row(self, parent, state, text: str):
        """state : True (ok), False (problème), None (neutre)."""
        theme, c = self.theme, self.theme.c
        line = tk.Frame(parent, bg=c["card"])
        line.pack(fill="x", pady=theme.px(2))
        icon = {True: "check", False: "warning"}.get(state, "info")
        color = {True: c["success"], False: c["warning"]}.get(state, c["faint"])
        glyph = theme.icon(icon) or {True: "✓", False: "!"}.get(state, "·")
        tk.Label(line, text=glyph, bg=c["card"], fg=color,
                 font=theme.font_icon if theme.has_icons else theme.font_bold,
                 width=2).pack(side="left")
        tk.Label(line, text=text, bg=c["card"], fg=c["text"], font=theme.font, justify="left",
                 wraplength=theme.px(520), anchor="w").pack(side="left", fill="x")

    def refresh(self):
        for holder in (self.rows, self.ext_rows):
            for child in holder.winfo_children():
                child.destroy()

        st = {}
        if wi is None or os.name != "nt":
            self._status_row(self.rows, None, "Disponible uniquement sous Windows.")
            self.btn_on.set_enabled(False)
            self.btn_off.set_enabled(False)
        else:
            try:
                st = wi.status()
            except Exception as exc:
                st = {"supported": True, "error": str(exc)}
            if st.get("error"):
                self._status_row(self.rows, False, st["error"])
            elif st.get("foreign"):
                self._status_row(self.rows, False,
                                 "Une autre copie de l'application est enregistrée :\n%s\n"
                                 "« Activer » fera pointer Windows sur celle-ci." % st["foreign_script"])
            elif not st.get("installed"):
                self._status_row(self.rows, None, "Intégration non activée.")
            else:
                if st.get("stale"):
                    self._status_row(self.rows, False,
                                     "L'enregistrement pointe sur un ancien emplacement : "
                                     "cliquez sur « Mettre à jour ».")
                owner = st.get("user_choice") or st.get("torrent_owner") or ""
                if st["torrent_active"]:
                    detail = ""
                elif owner and owner != getattr(wi, "PROGID_FILE", ""):
                    detail = " — gérés par « %s »" % owner
                else:
                    detail = " — aucun programme associé : cliquez sur « Réparer »"
                self._status_row(self.rows, st["torrent_active"], "Fichiers .torrent" + detail)
                self._status_row(self.rows, st["magnet_active"],
                                 "Liens magnet" + ("" if st["magnet_active"] else
                                                   " — gérés par un autre programme"))
            self.btn_on.set_text("Activer" if not st.get("installed") else
                                 "Mettre à jour" if not st.get("up_to_date") else "Réparer")
            self.btn_off.set_enabled(bool(st.get("installed")))

        host = st.get("native_host") or {}
        if host.get("ok"):
            self._status_row(self.ext_rows, True, "Liaison automatique prête pour : %s."
                             % ", ".join(host.get("declared") or []))
        elif host.get("applicable"):
            self._status_row(self.ext_rows, False,
                             "Liaison automatique inactive : cliquez sur « %s » ci-dessus."
                             % ("Activer" if not st.get("installed") else "Mettre à jour"))
        else:
            self._status_row(self.ext_rows, None,
                             "Aucun navigateur compatible détecté sur ce poste.")
        if self.app.server is None:
            self._status_row(self.ext_rows, False,
                             "Le service local n'a pas pu démarrer (port %d occupé par un autre "
                             "programme) : l'extension ne pourra pas joindre l'application."
                             % self.app.port)

    # -- connexion manuelle ------------------------------------------------- #

    def _build_manual(self):
        theme, c, px = self.theme, self.theme.c, self.theme.px
        frame = self.manual
        frame.columnconfigure(1, weight=1)
        tk.Label(frame, bg=c["card"], fg=c["muted"], font=theme.font_small, justify="left",
                 wraplength=px(560), anchor="w",
                 text="À n'utiliser que si la liaison automatique est impossible : reportez ces "
                      "deux valeurs dans les options de l'extension. L'application doit alors être "
                      "ouverte pour recevoir les envois."
                 ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(px(6), px(4)))
        self.var_endpoint = tk.StringVar(value="http://%s:%d" % (IPC_HOST, self.app.port))
        self.var_token = tk.StringVar(value=self.app.token)
        for r, (label, var) in enumerate((("Adresse", self.var_endpoint), ("Jeton", self.var_token)), 1):
            ttk.Label(frame, text=label, style="Card.TLabel").grid(
                row=r, column=0, sticky="w", pady=px(4), padx=(0, px(12)))
            ttk.Entry(frame, textvariable=var, state="readonly").grid(
                row=r, column=1, sticky="ew", pady=px(4))
            FlatButton(frame, theme, "Copier", icon="copy",
                       command=lambda v=var: self._copy(v.get())).grid(row=r, column=2, padx=(px(8), 0))

    def _toggle_manual(self):
        if self.manual.winfo_ismapped():
            self.manual.pack_forget()
        else:
            self.manual.pack(fill="x")

    def _copy(self, text: str):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.app.set_status("Copié dans le presse-papiers.")

    # -- actions Windows ---------------------------------------------------- #

    def do_register(self):
        try:
            log = wi.register()
        except Exception as exc:
            messagebox.showerror(APP_NAME, "L'activation a échoué :\n\n%s" % exc, parent=self)
            return
        detail = "\n".join("•  " + line for line in log)
        try:
            st = wi.status()
            effective = st.get("effective_command") or ""
            if "torrent_uploader" not in effective:
                detail += ("\n\nUn autre programme reste prioritaire pour les .torrent. Faites un "
                           "clic droit sur un .torrent > « Ouvrir avec » > « Toujours », ou "
                           "utilisez le bouton « Applications par défaut… ».")
        except Exception:
            pass
        messagebox.showinfo(APP_NAME, "Intégration activée.\n\n" + detail, parent=self)
        self.refresh()

    def do_unregister(self):
        if not messagebox.askyesno(
                APP_NAME, "Retirer l'association des .torrent, des liens magnet et la liaison "
                          "avec l'extension de navigateur ?", parent=self):
            return
        try:
            log = wi.unregister()
        except Exception as exc:
            messagebox.showerror(APP_NAME, "Échec : %s" % exc, parent=self)
            return
        messagebox.showinfo(APP_NAME, "Intégration retirée.\n\n"
                            + "\n".join("•  " + line for line in log), parent=self)
        self.refresh()

    def open_default_apps(self):
        try:
            wi.open_default_apps_settings()
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)

    def make_shortcut(self):
        try:
            link = wi.create_desktop_shortcut()
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return
        messagebox.showinfo(APP_NAME, "Raccourci créé :\n%s\n\nVous pouvez y déposer des fichiers "
                            ".torrent." % link, parent=self)

    # -- dossier surveillé -------------------------------------------------- #

    def pick_folder(self):
        folder = filedialog.askdirectory(
            title="Dossier à surveiller", parent=self,
            initialdir=self.var_folder.get() or str(Path.home()))
        if folder:
            self.var_folder.set(str(Path(folder)))
            self._apply_watch()

    def _apply_watch(self):
        app = self.app
        folder = self.var_folder.get().strip()
        new = {
            "watch_downloads": bool(self.var_watch.get()),
            "watch_auto_send": bool(self.var_autosend.get()),
            "watch_folder": "" if folder == str(app.default_watch_folder()) else folder,
        }
        if all(app.cfg.get(k) == v for k, v in new.items()):
            return
        app.cfg.update(new)
        save_config(app.cfg)
        app.restart_watcher()

    def close(self):
        try:
            self._apply_watch()
        finally:
            self.destroy()
