# -*- coding: utf-8 -*-
"""Thème de l'interface : palettes claire et sombre, styles ttk, mise à l'échelle."""

from __future__ import annotations

import ctypes
import os
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

PALETTES = {
    "light": {
        "bg": "#F2F4F8", "card": "#FFFFFF", "border": "#D8DEE8", "field": "#FFFFFF",
        "text": "#18212F", "muted": "#667085", "faint": "#98A2B3",
        "accent": "#2563EB", "accent_hover": "#1D4FD8", "accent_press": "#1A44BB",
        "on_accent": "#FFFFFF", "accent_soft": "#E8EFFD",
        "header": "#111C33", "header_text": "#FFFFFF", "header_muted": "#A8B3C8",
        "header_hover": "#1E2B49", "header_press": "#283860",
        "hover": "#EEF2F8", "press": "#E2E8F2", "row_alt": "#F8FAFC", "select": "#DCE7FD",
        "trough": "#E4E9F1", "scroll": "#C5CDDA",
        "drop": "#F8FAFD", "drop_border": "#B4C0D4",
        "success": "#0F7B45", "danger": "#C42B1C", "warning": "#A15C00", "info": "#1D5BC2",
        "disabled": "#A6B0C0",
    },
    "dark": {
        "bg": "#11151C", "card": "#1A2029", "border": "#2B3442", "field": "#131820",
        "text": "#E7EBF2", "muted": "#98A4B8", "faint": "#6B778B",
        "accent": "#4F8BF5", "accent_hover": "#6A9DF7", "accent_press": "#3E78E0",
        "on_accent": "#0A1222", "accent_soft": "#1C2B48",
        "header": "#0B0F15", "header_text": "#FFFFFF", "header_muted": "#8D99AE",
        "header_hover": "#1A2230", "header_press": "#242E40",
        "hover": "#232B37", "press": "#2C3644", "row_alt": "#1E2530", "select": "#27406E",
        "trough": "#262E3A", "scroll": "#3A4556",
        "drop": "#161B23", "drop_border": "#3A4658",
        "success": "#4CC38A", "danger": "#F2766E", "warning": "#E3A84C", "info": "#6FA6F8",
        "disabled": "#5A6577",
    },
}

THEME_CHOICES = (("system", "Automatique (suit Windows)"), ("light", "Clair"), ("dark", "Sombre"))

# Glyphes de la police d'icônes livrée avec Windows 10 et 11.
ICONS = {
    "refresh": "\uE72C", "settings": "\uE713", "add": "\uE710", "delete": "\uE74D",
    "clear": "\uE75C", "link": "\uE71B", "paste": "\uE77F", "send": "\uE724",
    "folder": "\uED25", "tag": "\uE8EC", "star": "\uE734", "star_fill": "\uE735",
    "puzzle": "\uEA86", "connect": "\uE8CE", "upload": "\uE898", "download": "\uE896",
    "check": "\uE73E", "warning": "\uE7BA", "info": "\uE946", "copy": "\uE8C8",
    "pin": "\uE718", "apps": "\uECAA", "close": "\uE711",
}
ICON_FONT = "Segoe MDL2 Assets"


def enable_dpi_awareness() -> None:
    """Texte net sur les écrans à forte densité (à appeler AVANT de créer la fenêtre)."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def set_app_id(app_id: str = "TorrentUploader.App") -> None:
    """Identité propre dans la barre des tâches (sinon l'icône est celle de Python)."""
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        except Exception:
            pass


def system_prefers_dark() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        try:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        finally:
            winreg.CloseKey(key)
        return value == 0
    except Exception:
        return False


def resolve_mode(setting: str) -> str:
    if setting in ("light", "dark"):
        return setting
    return "dark" if system_prefers_dark() else "light"


def dark_title_bar(window, dark: bool) -> None:
    """Assortit la barre de titre de Windows au thème."""
    if os.name != "nt":
        return
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value),
                                                   ctypes.sizeof(value))
    except Exception:
        pass


class Theme:
    """Couleurs, polices et échelle partagées par tous les composants."""

    def __init__(self, root: tk.Misc, setting: str = "system"):
        self.setting = setting
        self.mode = resolve_mode(setting)
        self.c = PALETTES[self.mode]
        try:
            self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        except tk.TclError:
            self.scale = 1.0
        families = set(tkfont.families(root))
        base = "Segoe UI" if "Segoe UI" in families else tkfont.nametofont("TkDefaultFont").actual("family")
        semibold = "Segoe UI Semibold" if "Segoe UI Semibold" in families else base
        self.font = (base, 10)
        self.font_small = (base, 9)
        self.font_bold = (semibold, 10) if semibold != base else (base, 10, "bold")
        self.font_title = (semibold, 13) if semibold != base else (base, 13, "bold")
        self.font_section = (semibold, 10) if semibold != base else (base, 10, "bold")
        self.has_icons = ICON_FONT in families
        self.font_icon = (ICON_FONT, 10)
        self.font_icon_large = (ICON_FONT, 20)

    @property
    def dark(self) -> bool:
        return self.mode == "dark"

    def px(self, n: float) -> int:
        """Convertit des pixels « logiques » (à 100 %) en pixels réels."""
        return max(1, int(round(n * self.scale)))

    def icon(self, name: str) -> str:
        return ICONS.get(name, "") if self.has_icons else ""

    # -- styles ttk -------------------------------------------------------- #

    def apply(self, root: tk.Misc) -> None:
        c, px = self.c, self.px
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        root.configure(bg=c["bg"])

        style.configure(".", background=c["bg"], foreground=c["text"], font=self.font,
                        bordercolor=c["border"], focuscolor=c["accent"],
                        troughcolor=c["trough"], fieldbackground=c["field"],
                        insertcolor=c["text"], selectbackground=c["accent"],
                        selectforeground=c["on_accent"])
        style.configure("TFrame", background=c["bg"])
        style.configure("Card.TFrame", background=c["card"])
        style.configure("TLabel", background=c["bg"], foreground=c["text"])
        style.configure("Card.TLabel", background=c["card"], foreground=c["text"])
        style.configure("Muted.TLabel", background=c["card"], foreground=c["muted"],
                        font=self.font_small)
        style.configure("Section.TLabel", background=c["card"], foreground=c["text"],
                        font=self.font_section)

        for name in ("TEntry", "TSpinbox"):
            style.configure(name, fieldbackground=c["field"], foreground=c["text"],
                            bordercolor=c["border"], lightcolor=c["field"],
                            darkcolor=c["field"], insertcolor=c["text"],
                            arrowcolor=c["muted"], padding=(px(6), px(5)))
            style.map(name,
                      bordercolor=[("focus", c["accent"])],
                      lightcolor=[("focus", c["accent"])],
                      darkcolor=[("focus", c["accent"])],
                      fieldbackground=[("readonly", c["field"]), ("disabled", c["bg"])],
                      foreground=[("disabled", c["disabled"])])

        style.configure("TCombobox", fieldbackground=c["field"], foreground=c["text"],
                        background=c["card"], arrowcolor=c["muted"],
                        bordercolor=c["border"], lightcolor=c["field"],
                        darkcolor=c["field"], insertcolor=c["text"],
                        padding=(px(6), px(5)), arrowsize=px(14))
        style.map("TCombobox",
                  bordercolor=[("focus", c["accent"])],
                  lightcolor=[("focus", c["accent"])],
                  darkcolor=[("focus", c["accent"])],
                  fieldbackground=[("readonly", c["field"])],
                  background=[("active", c["hover"])],
                  selectbackground=[("!focus", c["field"])],
                  selectforeground=[("!focus", c["text"])])
        root.option_add("*TCombobox*Listbox.background", c["field"])
        root.option_add("*TCombobox*Listbox.foreground", c["text"])
        root.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
        root.option_add("*TCombobox*Listbox.selectForeground", c["on_accent"])
        root.option_add("*TCombobox*Listbox.font", self.font)
        root.option_add("*TCombobox*Listbox.borderWidth", 0)

        style.configure("Card.TCheckbutton", background=c["card"], foreground=c["text"],
                        indicatorbackground=c["field"], indicatorforeground=c["on_accent"],
                        upperbordercolor=c["border"], lowerbordercolor=c["border"],
                        indicatormargin=(0, 0, px(8), 0), padding=(0, px(3)))
        style.map("Card.TCheckbutton",
                  background=[("active", c["card"])],
                  foreground=[("disabled", c["disabled"])],
                  indicatorbackground=[("selected", c["accent"]), ("active", c["hover"])],
                  upperbordercolor=[("selected", c["accent"])],
                  lowerbordercolor=[("selected", c["accent"])])

        style.configure("Treeview", background=c["card"], fieldbackground=c["card"],
                        foreground=c["text"], rowheight=px(30), borderwidth=0,
                        font=self.font)
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.map("Treeview", background=[("selected", c["select"])],
                  foreground=[("selected", c["text"])])
        style.configure("Treeview.Heading", background=c["card"], foreground=c["muted"],
                        relief="flat", borderwidth=0, font=self.font_small,
                        padding=(px(3), px(7)))
        style.map("Treeview.Heading", background=[("active", c["hover"])])

        style.configure("Accent.Horizontal.TProgressbar", troughcolor=c["trough"],
                        background=c["accent"], bordercolor=c["trough"],
                        lightcolor=c["accent"], darkcolor=c["accent"], thickness=px(5))

        style.layout("Flat.Vertical.TScrollbar", [
            ("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
                ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        style.configure("Flat.Vertical.TScrollbar", troughcolor=c["card"],
                        background=c["scroll"], bordercolor=c["card"],
                        lightcolor=c["scroll"], darkcolor=c["scroll"],
                        relief="flat", width=px(10), arrowsize=px(10))
        style.map("Flat.Vertical.TScrollbar", background=[("active", c["faint"])])

        style.configure("TSeparator", background=c["border"])
