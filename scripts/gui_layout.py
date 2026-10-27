"""Presentation layer for the desktop helper; behavior lives in gui.py."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
import tkinter as tk
from tkinter import ttk

if __package__:
    from .gui_theme import (FIELD, GREEN, GREEN_PALE, INK, LINE, MUTED,
                            PANEL, PAPER, RoundedButton,
                            RoundedPanel)
else:
    from gui_theme import (FIELD, GREEN, GREEN_PALE, INK, LINE, MUTED,
                           PANEL, PAPER, RoundedButton,
                           RoundedPanel)

if TYPE_CHECKING:
    from gui import HelperWindow


def bind_responsive_wrap(frame: ttk.Frame, *labels: ttk.Label) -> None:
    def update_wrap(event: tk.Event[Any]) -> None:
        for label in labels:
            label.configure(wraplength=max(80, event.width - 30))

    frame.bind("<Configure>", update_wrap, add="+")


def configure_styles(root: tk.Tk) -> None:
    root.configure(background=PAPER)
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    style.configure("TFrame", background=PAPER)
    style.configure("TLabel", background=PAPER, foreground=INK, font=("Microsoft YaHei UI", 10))
    style.configure("TButton", font=("Microsoft YaHei UI", 10))
    style.configure("Card.TFrame", background=PANEL)
    style.configure("Card.TLabel", background=PANEL, foreground=INK)
    style.configure("Muted.Card.TLabel", background=PANEL, foreground=MUTED)
    style.configure("Status.Card.TLabel", background=GREEN_PALE, foreground=GREEN,
                    padding=(9, 4), font=("Microsoft YaHei UI", 9, "bold"))
    style.configure("Card.TCheckbutton", background=PANEL, foreground=INK,
                    font=("Microsoft YaHei UI", 9), padding=(0, 2))
    style.map("Card.TCheckbutton", background=[("active", PANEL)],
              indicatorcolor=[("selected", GREEN), ("!selected", PANEL)])
    style.configure("Heading.TLabel", background=PAPER, foreground=INK,
                    font=("Microsoft YaHei UI", 17, "bold"))
    style.configure("CardTitle.Card.TLabel", background=PANEL, foreground=INK,
                    font=("Microsoft YaHei UI", 13, "bold"))
    style.configure("Toolbar.TButton", background=PAPER, foreground=GREEN, borderwidth=0,
                    focusthickness=1, focuscolor=GREEN, padding=(9, 6))
    style.map("Toolbar.TButton", background=[("active", GREEN_PALE)])
    style.configure("Input.TEntry", fieldbackground=FIELD, foreground=INK,
                    bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, padding=(8, 6))
    style.map("Input.TEntry", bordercolor=[("focus", GREEN)])
    style.configure("Input.TCombobox", fieldbackground=FIELD, background=FIELD,
                    foreground=INK, arrowcolor=GREEN, bordercolor=LINE, padding=(8, 6))
    style.map("Input.TCombobox", fieldbackground=[("readonly", FIELD)],
              foreground=[("readonly", INK)], bordercolor=[("focus", GREEN)])
    style.configure("Hidden.TNotebook", background=PAPER, borderwidth=0, tabmargins=0,
                    bordercolor=PAPER, lightcolor=PAPER, darkcolor=PAPER)
    style.layout("Hidden.TNotebook.Tab", [])


def build_layout(
    app: HelperWindow,
    modes: Sequence[str],
) -> None:
    root = app.root
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    main = ttk.Frame(root, padding=(18, 16, 18, 16))
    main.grid(row=0, column=0, sticky="nsew")
    main.columnconfigure(0, weight=1)
    main.rowconfigure(1, weight=1)
    heading = ttk.Frame(main)
    heading.grid(row=0, column=0, sticky="ew", pady=(0, 12))
    heading.columnconfigure(0, weight=1)
    ttk.Label(heading, text="WTHelper", style="Heading.TLabel").grid(row=0, column=0, sticky="w")
    ttk.Label(heading, textvariable=app.desktop_status, foreground=GREEN, background=GREEN_PALE,
              padding=(10, 5), font=("Microsoft YaHei UI", 9, "bold")).grid(row=0, column=1, sticky="e")
    ttk.Button(heading, text="选择桌宠", style="Toolbar.TButton",
               command=app.choose_desktop).grid(row=0, column=2, padx=(8, 0))
    if not app.admin:
        ttk.Button(heading, text="以管理员身份运行", style="Toolbar.TButton",
                   command=app.elevate).grid(row=0, column=3, padx=(8, 0))

    app.notebook = ttk.Notebook(main, style="Hidden.TNotebook")
    app.notebook.grid(row=1, column=0, sticky="nsew")
    control = ttk.Frame(app.notebook)
    control.columnconfigure((0, 1), weight=1, uniform="control-cards")
    control.rowconfigure(0, weight=1)
    app.notebook.add(control, text="运行控制")

    chest_panel = RoundedPanel(control, height=330, padding=10)
    chest_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
    chest = chest_panel.content
    chest.columnconfigure(0, weight=1)
    chest.rowconfigure(4, weight=1)
    ttk.Label(chest, text="自动领取", style="CardTitle.Card.TLabel").grid(row=0, column=0, sticky="w")
    ttk.Label(chest, textvariable=app.chest_status, style="Status.Card.TLabel").grid(row=0, column=1, sticky="e")
    ttk.Checkbutton(chest, text="允许设备自动睡眠", variable=app.allow_sleep,
                    style="Card.TCheckbutton").grid(row=1, column=0, columnspan=2, sticky="w", pady=(14, 2))
    ttk.Checkbutton(chest, text="预览模式 · 只检查，不领取", variable=app.chest_preview,
                    style="Card.TCheckbutton").grid(row=2, column=0, columnspan=2, sticky="w", pady=2)
    chest_hint = ttk.Label(chest, text="默认保持设备清醒，屏幕可正常关闭。", style="Muted.Card.TLabel",
                           font=("Microsoft YaHei UI", 9), wraplength=310)
    chest_hint.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 12))
    chest_buttons = ttk.Frame(chest, style="Card.TFrame")
    chest_buttons.grid(row=5, column=0, columnspan=2, sticky="sw")
    app.chest_start = RoundedButton(chest_buttons, text="开始领取", primary=True, command=app.start_chest, width=112, height=36)
    app.chest_start.grid(row=0, column=0, padx=(0, 8))
    app.chest_stop = RoundedButton(chest_buttons, text="停止", command=lambda: app.stop_job("chest"), width=72, height=36, state="disabled")
    app.chest_stop.grid(row=0, column=1)
    chest_detail = ttk.Label(chest, textvariable=app.chest_detail, style="Muted.Card.TLabel",
                             font=("Microsoft YaHei UI", 9), wraplength=310)
    chest_detail.grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))
    bind_responsive_wrap(chest, chest_hint, chest_detail)

    tap_panel = RoundedPanel(control, height=330, padding=10)
    tap_panel.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
    tap = tap_panel.content
    tap.columnconfigure(1, weight=1)
    tap.rowconfigure(6, weight=1)
    ttk.Label(tap, text="敲击计数", style="CardTitle.Card.TLabel").grid(row=0, column=0, sticky="w")
    ttk.Label(tap, textvariable=app.tap_status, style="Status.Card.TLabel").grid(row=0, column=1, sticky="e")
    ttk.Label(tap, text="输入模式", style="Muted.Card.TLabel", font=("Microsoft YaHei UI", 9)).grid(row=2, column=0, sticky="w", pady=(8, 4))
    app.mode_box = ttk.Combobox(tap, textvariable=app.mode, values=tuple(modes), state="readonly", style="Input.TCombobox", width=15)
    app.mode_box.grid(row=2, column=1, sticky="ew", pady=(8, 4))
    fields = ttk.Frame(tap, style="Card.TFrame")
    fields.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 0))
    for column, (title, variable) in enumerate((("轮数", app.count), ("轮 / 秒", app.rate), ("延迟 / 秒", app.delay))):
        fields.columnconfigure(column, weight=1, uniform="tap-fields")
        ttk.Label(fields, text=title, style="Muted.Card.TLabel", font=("Microsoft YaHei UI", 9)).grid(row=0, column=column, sticky="w", padx=(0, 7))
        ttk.Entry(fields, textvariable=variable, style="Input.TEntry", width=7).grid(row=1, column=column, sticky="ew", padx=(0, 7), pady=(3, 0))
    tap_hint = ttk.Label(tap, textvariable=app.tap_hint, style="Muted.Card.TLabel", font=("Microsoft YaHei UI", 9),
                         wraplength=310)
    tap_hint.grid(row=4, column=0, columnspan=2, sticky="w", pady=(7, 2))
    ttk.Checkbutton(tap, text="预览模式 · 不发送输入", variable=app.tap_preview,
                    style="Card.TCheckbutton").grid(row=5, column=0, columnspan=2, sticky="w", pady=(0, 5))
    tap_buttons = ttk.Frame(tap, style="Card.TFrame")
    tap_buttons.grid(row=7, column=0, columnspan=2, sticky="sw")
    app.tap_start = RoundedButton(tap_buttons, text="开始刷数", primary=True, command=app.start_tap, width=112, height=36)
    app.tap_start.grid(row=0, column=0, padx=(0, 8))
    app.tap_stop = RoundedButton(tap_buttons, text="停止", command=lambda: app.stop_job("tap"), width=72, height=36, state="disabled")
    app.tap_stop.grid(row=0, column=1)
    tap_detail = ttk.Label(tap, textvariable=app.tap_detail, style="Muted.Card.TLabel",
                           font=("Microsoft YaHei UI", 9), wraplength=310)
    tap_detail.grid(row=8, column=0, columnspan=2, sticky="w", pady=(8, 0))
    bind_responsive_wrap(tap, tap_hint, tap_detail)
