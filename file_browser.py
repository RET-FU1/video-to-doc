"""应用内目录浏览器。

系统 shell 打不开文件夹时（缺少关联、被策略拦截、完整性级别受限等），
用纯 tkinter 列目录作为兜底 —— 不依赖任何系统调用，任何环境都能用。

拆成独立模块的原因：它只通过三个回调与主窗口交互（打开路径 / 复制路径 /
写日志），放在 gui.py 里会让 gui 越来越臃肿（改造前 gui.py 约 1240 行）。
"""
import datetime
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, List, Optional, Tuple

from ui_theme import C, F
from ui_widgets import FlatButton
from utils import fmt_size

# 窗口尺寸
WINDOW_W, WINDOW_H = 760, 560
WINDOW_MIN_W, WINDOW_MIN_H = 560, 360

# Treeview 列宽
COL_NAME_W, COL_SIZE_W, COL_MTIME_W = 440, 96, 150
ROW_HEIGHT = 26


class FolderBrowser(tk.Toplevel):
    """列出目录内容，可进入子目录 / 双击打开文件。

    :param open_path: 用系统方式打开路径，返回 (是否成功, 失败原因)
    :param copy_path: 复制路径到剪贴板
    :param log:       往主窗口日志区写一行
    """

    def __init__(self, parent: tk.Misc, folder: Path, *,
                 open_path: Callable[[Path], Tuple[bool, str]],
                 copy_path: Callable[[Path], None],
                 log: Callable[[str], None],
                 note: str = "") -> None:
        super().__init__(parent)
        self._open_path = open_path
        self._copy_path = copy_path
        self._log = log
        self._dir = Path(folder)

        self.title("输出目录")
        self.configure(bg=C["bg"])
        self.geometry(f"{WINDOW_W}x{WINDOW_H}")
        self.minsize(WINDOW_MIN_W, WINDOW_MIN_H)
        try:
            self.transient(parent)
        except tk.TclError:
            pass

        self._build_header(note)
        self._build_toolbar()
        self._build_tree()
        self._build_status()
        self.refresh()
        self.focus_set()

    # ---- 布局 ----

    def _build_header(self, note: str) -> None:
        head = tk.Frame(self, bg=C["card"], padx=16, pady=10)
        head.pack(fill="x")
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")

        self._path_var = tk.StringVar(value=str(self._dir))
        entry = tk.Entry(head, textvariable=self._path_var, font=F["body"],
                         bg=C["bg"], fg=C["text"], relief="flat", borderwidth=0,
                         state="readonly", readonlybackground=C["bg"])
        entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 10))

        if note:
            tk.Label(self, text=f"系统拒绝自动打开（{note}），这里是应用内目录列表。",
                     font=F["small"], fg=C["faint"], bg=C["bg"],
                     anchor="w", justify="left", wraplength=700,
                     padx=16).pack(fill="x", pady=(6, 0))

    def _build_toolbar(self) -> None:
        tools = tk.Frame(self, bg=C["bg"], padx=16, pady=8)
        tools.pack(fill="x")
        FlatButton(tools, "上一级", self.go_up, compact=True).pack(side="left", padx=(0, 6))
        FlatButton(tools, "刷新", self.refresh, compact=True).pack(side="left", padx=(0, 6))
        FlatButton(tools, "复制路径", self.copy_current,
                   compact=True).pack(side="left", padx=(0, 6))
        FlatButton(tools, "用资源管理器打开", self.retry_system,
                   compact=True).pack(side="left")

    def _build_tree(self) -> None:
        wrap = tk.Frame(self, bg=C["card"], highlightthickness=1,
                        highlightbackground=C["border"])
        wrap.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        # 让 Treeview 融入整体配色（默认样式是系统灰白，很突兀）
        style = ttk.Style(self)
        style.configure("Folder.Treeview",
                        background=C["card"], fieldbackground=C["card"],
                        foreground=C["text"], borderwidth=0, relief="flat",
                        rowheight=ROW_HEIGHT, font=F["body"])
        style.configure("Folder.Treeview.Heading",
                        background=C["card"], foreground=C["muted"],
                        borderwidth=0, relief="flat", font=F["small"],
                        padding=(8, 6))
        style.map("Folder.Treeview",
                  background=[("selected", C["accent_soft"])],
                  foreground=[("selected", C["accent"])])
        style.map("Folder.Treeview.Heading", background=[("active", C["hover"])])

        self._tree = ttk.Treeview(wrap, columns=("size", "mtime"),
                                  show="tree headings", selectmode="browse",
                                  style="Folder.Treeview")
        self._tree.heading("#0", text="名称", anchor="w")
        self._tree.heading("size", text="大小", anchor="e")
        self._tree.heading("mtime", text="修改时间", anchor="w")
        self._tree.column("#0", width=COL_NAME_W, anchor="w", stretch=True)
        self._tree.column("size", width=COL_SIZE_W, anchor="e", stretch=False)
        self._tree.column("mtime", width=COL_MTIME_W, anchor="w", stretch=False)
        self._tree.pack(side="left", fill="both", expand=True)

        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self._tree.yview)
        vsb.pack(side="right", fill="y")
        self._tree.configure(yscrollcommand=vsb.set)

        self._tree.bind("<Double-1>", self.open_selected)
        self._tree.bind("<Return>", self.open_selected)

    def _build_status(self) -> None:
        self._info = tk.Label(self, text="", font=F["small"], fg=C["faint"],
                              bg=C["bg"], anchor="w", padx=16)
        self._info.pack(fill="x", pady=(0, 10))

    # ---- 行为 ----

    def _list_entries(self) -> Optional[List[Tuple[Path, object]]]:
        try:
            items = sorted(self._dir.iterdir(),
                           key=lambda x: (x.is_file(), x.name.lower()))
        except OSError as e:
            self._info.configure(text=f"无法读取目录: {e}")
            return None
        out = []
        for item in items:
            if item.name.startswith("."):
                continue          # 跳过隐藏文件（.pipeline_state 之类）
            try:
                st = item.stat()
                ts = datetime.datetime.fromtimestamp(
                    st.st_mtime).strftime("%Y-%m-%d %H:%M")
            except OSError:
                st, ts = None, ""
            out.append((item, (st, ts)))
        return out

    def refresh(self) -> None:
        self._tree.delete(*self._tree.get_children())
        self._path_var.set(str(self._dir))

        entries = self._list_entries()
        if entries is None:
            return

        dirs = files = 0
        for item, (st, ts) in entries:
            if item.is_dir():
                dirs += 1
                self._tree.insert("", "end", iid=str(item),
                                  text=f"📁  {item.name}",
                                  values=("<文件夹>", ts))
            else:
                files += 1
                self._tree.insert("", "end", iid=str(item),
                                  text=f"📄  {item.name}",
                                  values=(fmt_size(st.st_size, space=True) if st else "", ts))
        self._info.configure(text=f"{dirs} 个文件夹，{files} 个文件")

    def open_selected(self, _event=None) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        target = Path(sel[0])
        if target.is_dir():
            self._dir = target
            self.refresh()
            return

        ok, why = self._open_path(target)
        if ok:
            self._log(f"已打开文件: {target.name}")
            return
        # 文件也打不开时，至少把所在目录给用户
        self._copy_path(target.parent)
        messagebox.showinfo(
            "无法打开文件",
            f"系统拒绝打开这个文件（{why}）。\n\n"
            f"文件位置：\n{target}\n\n"
            f"所在目录路径已复制到剪贴板。",
        )

    def go_up(self) -> None:
        parent = self._dir.parent
        if parent != self._dir and parent.exists():
            self._dir = parent
            self.refresh()

    def copy_current(self) -> None:
        self._copy_path(self._dir)

    def retry_system(self) -> None:
        ok, why = self._open_path(self._dir)
        self._info.configure(text="已在资源管理器中打开" if ok
                             else f"系统仍然拒绝打开：{why}")
