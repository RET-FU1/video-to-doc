"""自定义 tkinter 控件：胶囊开关（Chip）与扁平按钮（FlatButton）。

不引入任何第三方 UI 依赖，只用标准库 tkinter 增强观感：
- Chip 替代原生 Checkbutton（原生控件在 Windows 上是方框 + 焦点虚线）
- FlatButton 提供 hover / pressed 反馈
"""
import tkinter as tk

from ui_theme import C, F


class Chip(tk.Label):
    """可选中的胶囊按钮（替代原生 Checkbutton）

    原生 Checkbutton 在 Windows 上会画一个方框和焦点虚线，观感和现代界面差距大。
    这里用 Label 自己实现：选中 = 实心强调色底 + 白字，未选中 = 白底细边框。
    """

    def __init__(self, parent, text, variable, command=None, **kw):
        self._var = variable
        self._command = command
        self._enabled = True
        super().__init__(parent, text=text, font=F["chip"], bd=0,
                         padx=12, pady=5, cursor="hand2",
                         highlightthickness=1, **kw)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self._refresh()

    def _on_click(self, _e=None):
        if not self._enabled:
            return
        self._var.set(not self._var.get())
        self._refresh()
        if self._command:
            self._command()

    def _on_enter(self, _e=None):
        if not self._enabled:
            return
        if not self._var.get():
            self.configure(bg=C["hover"])

    def _on_leave(self, _e=None):
        self._refresh()

    def _refresh(self):
        if not self._enabled:
            self.configure(bg=C["card"], fg=C["faint"],
                           highlightbackground=C["border"],
                           highlightcolor=C["border"])
            return
        if self._var.get():
            self.configure(bg=C["accent"], fg="white",
                           highlightbackground=C["accent"],
                           highlightcolor=C["accent"])
        else:
            self.configure(bg=C["card"], fg=C["text"],
                           highlightbackground=C["border_2"],
                           highlightcolor=C["border_2"])

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "arrow")
        self._refresh()


class FlatButton(tk.Button):
    """扁平按钮：无边框、有 hover / pressed 反馈"""

    def __init__(self, parent, text, command, *, kind="secondary",
                 compact=False, **kw):
        self._kind = kind
        if kind == "primary":
            bg, fg, hover, pressed = C["accent"], "white", C["accent_2"], "#3730A3"
        elif kind == "danger":
            bg, fg, hover, pressed = C["danger"], "white", C["danger_2"], "#991B1B"
        else:
            bg, fg, hover, pressed = C["card"], C["text"], C["hover"], "#E8E8EA"
        self._bg, self._fg, self._hover, self._pressed = bg, fg, hover, pressed
        padx, pady = (12, 5) if compact else (18, 8)
        font = F["chip"] if compact else F["body"]
        super().__init__(parent, text=text, command=command, font=font,
                         bg=bg, fg=fg, activebackground=hover, activeforeground=fg,
                         relief="flat", bd=0, cursor="hand2",
                         padx=padx, pady=pady, highlightthickness=0, **kw)
        self.bind("<Enter>", lambda _e: self._set_bg(self._hover))
        self.bind("<Leave>", lambda _e: self._set_bg(self._bg))
        self.bind("<ButtonPress-1>", lambda _e: self._set_bg(self._pressed))
        self.bind("<ButtonRelease-1>", lambda _e: self._set_bg(self._hover))

    def _set_bg(self, color):
        try:
            self.configure(bg=color)
        except tk.TclError:
            pass
