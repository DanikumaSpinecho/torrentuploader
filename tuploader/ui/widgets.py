# -*- coding: utf-8 -*-
"""Composants graphiques : boutons plats, cartes, zone de dépôt, infobulles."""

from __future__ import annotations

import tkinter as tk

from .theme import Theme


class Tooltip:
    """Infobulle affichée après un court survol."""

    def __init__(self, widget: tk.Misc, text: str, theme: Theme):
        self.widget, self.text, self.theme = widget, text, theme
        self._job = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._hide()
        try:
            self._job = self.widget.after(550, self._show)
        except tk.TclError:
            pass

    def _show(self):
        self._job = None
        if not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + self.theme.px(6)
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + self.theme.px(6)
            tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            tip.attributes("-topmost", True)
            c = self.theme.c
            tk.Label(tip, text=self.text, bg=c["text"], fg=c["card"], font=self.theme.font_small,
                     padx=self.theme.px(8), pady=self.theme.px(4), justify="left").pack()
            tip.wm_geometry("+%d+%d" % (x, y))
            self._tip = tip
        except tk.TclError:
            self._tip = None

    def _hide(self, _event=None):
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


class FlatButton(tk.Frame):
    """Bouton plat avec icône facultative.

    kind : "primary" (action principale), "default" (bordé), "subtle" (sans bordure),
           "header" (sur le bandeau sombre).
    """

    def __init__(self, parent, theme: Theme, text: str = "", icon: str = "", command=None,
                 kind: str = "default", tooltip: str = "", large: bool = False):
        self.theme, self.kind, self.command = theme, kind, command
        self.enabled = True
        self._hover = self._pressed = False
        c, px = theme.c, theme.px
        self._colors = {
            "primary": (c["accent"], c["accent_hover"], c["accent_press"], c["on_accent"], c["accent"]),
            "default": (c["card"], c["hover"], c["press"], c["text"], c["border"]),
            "subtle": (c["card"], c["hover"], c["press"], c["text"], c["card"]),
            "header": (c["header"], c["header_hover"], c["header_press"], c["header_text"], c["header"]),
        }[kind]
        bg = self._colors[0]
        super().__init__(parent, bg=bg, highlightthickness=1, highlightbackground=self._colors[4],
                         highlightcolor=c["accent"], bd=0, takefocus=1, cursor="hand2")
        pad_x, pad_y = (px(18), px(9)) if large else (px(11), px(6))
        inner = tk.Frame(self, bg=bg)
        inner.pack(padx=pad_x, pady=pad_y)
        self._parts = [self, inner]
        self._icon = self._label = None
        glyph = theme.icon(icon) if icon else ""
        if glyph:
            self._icon = tk.Label(inner, text=glyph, bg=bg, fg=self._colors[3], font=theme.font_icon)
            self._icon.pack(side="left", padx=(0, px(7) if text else 0))
            self._parts.append(self._icon)
        if text:
            self._label = tk.Label(inner, text=text, bg=bg, fg=self._colors[3],
                                   font=theme.font_bold if kind == "primary" else theme.font)
            self._label.pack(side="left")
            self._parts.append(self._label)
        for part in self._parts:
            part.bind("<Enter>", self._on_enter)
            part.bind("<Leave>", self._on_leave)
            part.bind("<ButtonPress-1>", self._on_press)
            part.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Return>", self._on_key)
        self.bind("<space>", self._on_key)
        if tooltip:
            Tooltip(self, tooltip, theme)

    # -- état -- #
    def _paint(self):
        normal, hover, press, fg, border = self._colors
        if not self.enabled:
            bg, fg = normal, self.theme.c["disabled"]
            if self.kind == "primary":
                bg, fg, border = self.theme.c["trough"], self.theme.c["disabled"], self.theme.c["trough"]
        else:
            bg = press if self._pressed else hover if self._hover else normal
        try:
            self.configure(highlightbackground=border, cursor="hand2" if self.enabled else "arrow")
            for part in self._parts:
                part.configure(bg=bg)
            for label in (self._icon, self._label):
                if label is not None:
                    label.configure(fg=fg)
        except tk.TclError:
            pass

    def set_enabled(self, flag: bool):
        self.enabled = bool(flag)
        self._hover = self._pressed = False
        self.configure(takefocus=1 if self.enabled else 0)
        self._paint()

    def set_text(self, text: str):
        if self._label is not None:
            self._label.configure(text=text)

    def set_icon(self, icon: str, color: str | None = None):
        if self._icon is not None:
            self._icon.configure(text=self.theme.icon(icon))
            if color:
                normal, hover, press, _fg, border = self._colors
                self._colors = (normal, hover, press, color, border)
                self._paint()

    # -- événements -- #
    def _on_enter(self, _e):
        self._hover = True
        self._paint()

    def _on_leave(self, _e):
        self._hover = self._pressed = False
        self._paint()

    def _on_press(self, _e):
        if self.enabled:
            self._pressed = True
            self._paint()

    def _on_release(self, event):
        was_pressed = self._pressed
        self._pressed = False
        self._paint()
        if not (was_pressed and self.enabled and self.command):
            return
        # le relâchement doit avoir lieu sur le bouton pour valoir un clic
        x, y = event.x_root - self.winfo_rootx(), event.y_root - self.winfo_rooty()
        if 0 <= x <= self.winfo_width() and 0 <= y <= self.winfo_height():
            self.command()

    def _on_key(self, _e):
        if self.enabled and self.command:
            self.command()
        return "break"

    def invoke(self):
        if self.enabled and self.command:
            self.command()


class Card(tk.Frame):
    """Bloc à fond uni et fine bordure ; le contenu se place dans `.body`."""

    def __init__(self, parent, theme: Theme, title: str = "", padding: int = 14):
        c = theme.c
        # highlightcolor = bordure : sinon Tk assombrit le cadre dès qu'un champ
        # qu'il contient prend le focus.
        super().__init__(parent, bg=c["card"], highlightthickness=1,
                         highlightbackground=c["border"], highlightcolor=c["border"], bd=0)
        pad = theme.px(padding)
        self.header = None
        if title:
            self.header = tk.Frame(self, bg=c["card"])
            self.header.pack(fill="x", padx=pad, pady=(pad, 0))
            tk.Label(self.header, text=title, bg=c["card"], fg=c["text"],
                     font=theme.font_section).pack(side="left")
        self.body = tk.Frame(self, bg=c["card"])
        self.body.pack(fill="both", expand=True, padx=pad,
                       pady=(theme.px(8) if title else pad, pad))


class CheckBox(tk.Frame):
    """Case à cocher dessinée, à l'échelle de l'écran (celle de ttk reste minuscule)."""

    def __init__(self, parent, theme: Theme, text: str, variable: tk.BooleanVar,
                 command=None, wraplength: int = 0):
        c = theme.c
        super().__init__(parent, bg=c["card"], takefocus=1, cursor="hand2", highlightthickness=0)
        self.theme, self.variable, self.command = theme, variable, command
        self._hover = self._focus = False
        self._size = theme.px(18)
        self._box = tk.Canvas(self, width=self._size, height=self._size, bg=c["card"],
                              highlightthickness=0, bd=0)
        self._box.pack(side="left", anchor="n", padx=(0, theme.px(9)), pady=(theme.px(2), 0))
        self._label = tk.Label(self, text=text, bg=c["card"], fg=c["text"], font=theme.font,
                               justify="left", anchor="w", wraplength=wraplength)
        self._label.pack(side="left", fill="x")
        for part in (self, self._box, self._label):
            part.bind("<Button-1>", self._toggle)
            part.bind("<Enter>", lambda e: self._set(hover=True))
            part.bind("<Leave>", lambda e: self._set(hover=False))
        self.bind("<space>", self._toggle)
        self.bind("<FocusIn>", lambda e: self._set(focus=True))
        self.bind("<FocusOut>", lambda e: self._set(focus=False))
        self._trace = variable.trace_add("write", lambda *_: self._paint())
        self.bind("<Destroy>", self._on_destroy)
        self._paint()

    def _on_destroy(self, event):
        if event.widget is self and self._trace:
            try:
                self.variable.trace_remove("write", self._trace)
            except (tk.TclError, ValueError):
                pass
            self._trace = None

    def _set(self, hover=None, focus=None):
        if hover is not None:
            self._hover = hover
        if focus is not None:
            self._focus = focus
        self._paint()

    def _toggle(self, _event=None):
        self.focus_set()
        self.variable.set(not bool(self.variable.get()))
        if self.command:
            self.command()
        return "break"

    def _paint(self):
        try:
            c, s, box = self.theme.c, self._size, self._box
            on = bool(self.variable.get())
            box.delete("all")
            outline = c["accent"] if (on or self._hover or self._focus) else c["faint"]
            box.create_rectangle(1, 1, s - 2, s - 2, fill=c["accent"] if on else c["field"],
                                 outline=outline, width=max(1, self.theme.px(1.3)))
            if on:
                box.create_line(s * 0.25, s * 0.50, s * 0.42, s * 0.68, s * 0.74, s * 0.30,
                                fill=c["on_accent"], width=max(2, self.theme.px(1.8)),
                                capstyle="round", joinstyle="round")
        except tk.TclError:
            pass


class StatusPill(tk.Frame):
    """Pastille colorée suivie d'un texte (état de la connexion)."""

    def __init__(self, parent, theme: Theme, bg: str, fg: str):
        super().__init__(parent, bg=bg)
        self.theme = theme
        size = theme.px(10)
        self._canvas = tk.Canvas(self, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
        self._canvas.pack(side="left", padx=(0, theme.px(7)))
        self._dot = self._canvas.create_oval(1, 1, size - 1, size - 1, outline="", fill=theme.c["faint"])
        self._label = tk.Label(self, text="", bg=bg, fg=fg, font=theme.font_small)
        self._label.pack(side="left")

    def set(self, text: str, level: str = "idle"):
        color = {"ok": self.theme.c["success"], "error": self.theme.c["danger"],
                 "warn": self.theme.c["warning"], "busy": self.theme.c["info"]}.get(
                     level, self.theme.c["faint"])
        if level == "ok" and not self.theme.dark:
            color = "#3DD68C"                     # vert vif, lisible sur le bandeau sombre
        elif level == "error" and not self.theme.dark:
            color = "#FF7A70"
        elif level == "warn" and not self.theme.dark:
            color = "#F5B94E"
        try:
            self._canvas.itemconfigure(self._dot, fill=color)
            self._label.configure(text=text)
        except tk.TclError:
            pass


class DropZone(tk.Canvas):
    """Zone de dépôt : cadre en pointillés, icône et texte d'invite. Cliquable."""

    def __init__(self, parent, theme: Theme, title: str, subtitle: str, command=None, height: int = 104):
        c = theme.c
        super().__init__(parent, height=theme.px(height), bg=c["bg"], highlightthickness=0,
                         bd=0, cursor="hand2", takefocus=1)
        self.theme, self.title, self.subtitle, self.command = theme, title, subtitle, command
        self._hover = False
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Button-1>", lambda e: self.command() if self.command else None)
        self.bind("<Return>", lambda e: self.command() if self.command else None)

    def _set_hover(self, flag: bool):
        self._hover = flag
        self._draw()

    def flash(self):
        """Bref retour visuel après un dépôt."""
        self._set_hover(True)
        self.after(350, lambda: self._set_hover(False))

    def _draw(self):
        try:
            self.delete("all")
            c, px = self.theme.c, self.theme.px
            w, h = self.winfo_width(), self.winfo_height()
            if w < 20 or h < 20:
                return
            m = px(1)
            fill = c["accent_soft"] if self._hover else c["drop"]
            outline = c["accent"] if self._hover else c["drop_border"]
            self.create_rectangle(m, m, w - m - 1, h - m - 1, fill=fill, outline=outline,
                                  width=px(1.4), dash=(px(6), px(4)))
            cy = h // 2
            glyph = self.theme.icon("upload")
            text_x = w // 2
            if glyph:
                self.create_text(w // 2, cy - px(22), text=glyph, font=self.theme.font_icon_large,
                                 fill=c["accent"])
                cy += px(8)
            self.create_text(text_x, cy + px(2), text=self.title, font=self.theme.font_bold,
                             fill=c["text"])
            self.create_text(text_x, cy + px(22), text=self.subtitle, font=self.theme.font_small,
                             fill=c["muted"])
        except tk.TclError:
            pass
