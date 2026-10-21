"""
Video-to-Doc 图形界面

设计原则：
- 只用 tkinter 标准库，不引入任何第三方 UI 依赖
- 现代简约：中性色背景 + 白色卡片 + 单一强调色（靛蓝），细边框不用阴影
- 「chip」式开关代替原生 Checkbutton：更接近现代工具的选择器观感
- 所有按钮/开关都有 hover 反馈，操作可见

日志窗口保持深色终端风格（长时间盯着不刺眼），并保留按级别着色。
"""
import sys
from pathlib import Path

# 自动重定向到项目虚拟环境
_PROJECT_ROOT = Path(__file__).parent
_VENV_DIR = _PROJECT_ROOT / "venv" / ("Scripts" if sys.platform == "win32" else "bin")
_VENV_PYTHON = _VENV_DIR / ("python.exe" if sys.platform == "win32" else "python")
if _VENV_PYTHON.exists() and Path(sys.executable).resolve().parent != _VENV_DIR.resolve():
    import subprocess
    sys.exit(subprocess.run([str(_VENV_PYTHON), __file__] + sys.argv[1:]).returncode)

import logging
import os
import queue
import re
import subprocess
import threading
from contextlib import contextmanager
import tkinter as tk
from tkinter import ttk, messagebox

import yaml
from pipeline import Pipeline
from utils import (CancelToken, CancelledError, load_env, setup_logging,
                   fmt_size)

PROJECT_ROOT = Path(__file__).parent

# ── 设计令牌 ──────────────────────────────────────────────
C = {
    "bg":         "#F6F6F7",   # 页面底色（近白灰）
    "card":       "#FFFFFF",   # 卡片
    "border":     "#E7E7E9",   # 细边框
    "border_2":   "#D9D9DE",   # 输入框边框
    "text":       "#18181B",   # 主文字
    "muted":      "#6F6F76",   # 次要文字
    "faint":      "#9A9AA2",   # 提示文字
    "accent":     "#4F46E5",   # 强调色（靛蓝）
    "accent_2":   "#4338CA",   # hover
    "accent_soft": "#EEF0FF",  # 浅强调底
    "hover":      "#F0F0F2",   # 中性 hover
    "danger":     "#DC2626",
    "danger_2":   "#B91C1C",
    "log_bg":     "#17171A",
    "log_fg":     "#D4D4D8",
    "log_warn":   "#FBBF24",
    "log_err":    "#F87171",
    "log_ok":     "#4ADE80",
    "log_stage":  "#93C5FD",
}

F = {
    "title":   ("Microsoft YaHei UI", 15, "bold"),
    "section": ("Microsoft YaHei UI", 10, "bold"),
    "body":    ("Microsoft YaHei UI", 10),
    "chip":    ("Microsoft YaHei UI", 9),
    "small":   ("Microsoft YaHei UI", 9),
    "mono":    ("Consolas", 9),
}


class _GuiLogHandler(logging.Handler):
    """日志处理器：将 log 消息桥接到 GUI 日志窗口"""

    def __init__(self, app: "App") -> None:
        super().__init__()
        self._app = app

    def emit(self, record: logging.LogRecord) -> None:
        self._app._log(self.format(record))


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


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Video-to-Doc")
        self.root.configure(bg=C["bg"])
        self.root.minsize(760, 620)
        # 取消令牌：start() 时新建，stop() 时 cancel()
        self._cancel_token = CancelToken()
        # worker → 主线程的 UI 操作队列（tkinter 非线程安全，见 _ui/_pump_ui）
        self._ui_queue: "queue.Queue" = queue.Queue()
        self._task_running: bool = False
        self._style_probe = ttk.Style(self.root)
        self._build_ui()
        self._center_window(940, 940)
        self.root.after(40, self._pump_ui)
        # 处理中直接关窗会让进程退出、worker 可能写到一半（留下半截文件），
        # 所以关窗前先确认，并把取消信号发出去让 worker 尽快收尾。
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # GUI 由 启动.vbs 以隐藏窗口启动，没有控制台 —— Tk 回调里抛的异常
        # 默认只会打印到 stderr（看不见），表现为"点了按钮没反应"。
        # 接管后一律写进日志窗口，任何静默失败都能被看到。
        self.root.report_callback_exception = self._report_callback_exception
        # 启动时打印自身运行环境：定位"打开目录被系统拒绝"这类问题时，
        # 进程的完整性级别/会话/是否提权是最关键的线索（低完整性级别会
        # 被系统拒绝一切 shell 调用，而用户完全看不出来）。
        self._log_env_info()

    def _log_env_info(self) -> None:
        """把本进程的运行环境写进日志（用于诊断"系统拒绝"类问题）

        完整性级别是最关键的线索：低于 Medium 的进程会被系统拒绝一切
        shell 操作（打开目录、启动外部程序全部失败），而用户完全看不出来。
        读取方式走 `whoami /groups` 里的 SID（语言无关），比手写
        GetTokenInformation 的结构体解析可靠得多 —— 后者我写错过一次，
        把 Medium 误报成 Untrusted。
        """
        import ctypes

        try:
            sid = ctypes.c_ulong()
            ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
            session = sid.value
        except Exception:      # noqa: BLE001
            session = -1

        level_rid = None
        try:
            out = subprocess.run(["whoami", "/groups"], capture_output=True,
                                 timeout=15,
                                 encoding="mbcs", errors="replace").stdout or ""
            # 完整性标签的 SID 固定是 S-1-16-<RID>，与系统语言无关
            m = re.search(r"S-1-16-(\d+)", out)
            if m:
                level_rid = int(m.group(1))
        except Exception:      # noqa: BLE001
            level_rid = None

        names = {0: "Untrusted", 0x1000: "Low", 0x2000: "Medium",
                 0x3000: "High", 0x4000: "System"}
        if level_rid is None:
            level_name = "未知（读取失败）"
        else:
            level_name = names.get(level_rid, f"未知(0x{level_rid:x})")

        try:
            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:      # noqa: BLE001
            is_admin = False

        self._log(f"[环境] Python: {sys.executable}")
        self._log(f"[环境] 会话={session}  完整性级别={level_name}  管理员={is_admin}")
        self._log(f"[环境] 工作目录: {os.getcwd()}")

        if level_rid is not None and level_rid < 0x2000:
            self._log("[警告] 本进程完整性级别低于 Medium —— 系统会拒绝它的一切 shell 操作"
                      "（打开目录、启动外部程序都会失败），这不是本项目能修复的。")
            self._log("[警告] 说明本程序是被某个受限环境/沙箱以降权方式启动的。"
                      "请改为在资源管理器里双击「视频转文档.lnk」启动（正常应为 Medium）。")

    def _report_callback_exception(self, exc_type, exc_value, exc_tb) -> None:
        """把 Tk 回调里的未捕获异常显示到日志区（而不是丢进隐藏的 stderr）"""
        import traceback
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        self._log(f"[错误] 界面操作失败: {exc_type.__name__}: {exc_value}")
        for line in detail.strip().splitlines()[-8:]:
            self._log(f"    {line}")

    def _on_close(self):
        """关闭窗口：任务进行中先确认并请求取消"""
        if self._task_running:
            if not messagebox.askokcancel(
                "任务进行中",
                "当前任务还在处理，关闭窗口会中断它（已完成的进度会保留）。\n\n确定关闭吗？",
            ):
                return
            self._cancel_token.cancel()
            self._log("[WARN] 窗口关闭，正在中断任务…")
            # 给 worker 一点时间落盘/清理，再销毁窗口
            self.root.after(1200, self.root.destroy)
            return
        self.root.destroy()

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------

    def _center_window(self, w: int, h: int) -> None:
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _build_ui(self):
        self._build_header()

        # 顺序很重要：状态栏必须**先** pack 到底部，否则日志区的
        # expand=True 会先吃掉全部剩余空间，状态栏被挤出窗口外。
        self._build_statusbar()

        main = tk.Frame(self.root, bg=C["bg"])
        main.pack(fill="both", expand=True, padx=20, pady=(14, 0))

        # 上下可拖拽分割：上半是输入区，下半是选项 + 日志。
        # 用户嫌输入框矮就往拖，嫌日志小就往上拖 —— 比固定高度灵活。
        self._vsplit = tk.PanedWindow(main, orient="vertical", bg=C["bg"],
                                      sashwidth=8, sashrelief="flat", bd=0)
        self._vsplit.pack(fill="both", expand=True)

        top = tk.Frame(self._vsplit, bg=C["bg"])
        self._build_source_card(top)

        bottom = tk.Frame(self._vsplit, bg=C["bg"])
        self._build_option_card(bottom)
        self._build_action_row(bottom)
        self._build_log_panel(bottom)

        self._vsplit.add(top, minsize=110, stretch="always")
        self._vsplit.add(bottom, minsize=300, stretch="never")
        # 初始分配：输入区约占 1/4，其余给下半部分（日志才有空间）
        self.root.after_idle(self._apply_initial_split)

    def _apply_initial_split(self) -> None:
        try:
            total = self._vsplit.winfo_height()
            if total > 200:
                self._vsplit.sash_place(0, 0, max(120, int(total * 0.27)))
        except tk.TclError:
            pass

    # ---- 顶部标题栏 ----

    def _build_header(self):
        header = tk.Frame(self.root, bg=C["card"])
        header.pack(fill="x")

        inner = tk.Frame(header, bg=C["card"])
        inner.pack(fill="x", padx=20, pady=13)

        mark = tk.Frame(inner, bg=C["accent"], width=3, height=26)
        mark.pack(side="left", padx=(0, 10))
        mark.pack_propagate(False)

        tk.Label(inner, text="Video-to-Doc", font=F["title"],
                 fg=C["text"], bg=C["card"]).pack(side="left")
        tk.Label(inner, text="下载 · 转写 · 总结", font=F["small"],
                 fg=C["faint"], bg=C["card"]).pack(side="left", padx=(10, 0), pady=(3, 0))

        # 右上角状态药丸
        self._status_pill = tk.Label(inner, text="就绪", font=F["small"],
                                     fg=C["accent"], bg=C["accent_soft"],
                                     padx=12, pady=4)
        self._status_pill.pack(side="right")

        # 1px 分隔线
        tk.Frame(self.root, bg=C["border"], height=1).pack(fill="x")

    # ---- 输入卡片 ----

    def _build_source_card(self, parent):
        """输入卡片：填满上半区，输入框随卡片伸缩（拖动分割条即可调整高度）"""
        with self._card(parent, fill="both", expand=True) as card:
            head = tk.Frame(card, bg=C["card"])
            head.pack(fill="x")
            tk.Label(head, text="视频 / 音频", font=F["section"],
                     fg=C["text"], bg=C["card"]).pack(side="left")
            tk.Label(head, text="URL / 本地文件 / 文件夹路径（文件夹模式只填一行）",
                     font=F["small"], fg=C["faint"], bg=C["card"]).pack(side="left", padx=(10, 0))

            # fill="both" + expand=True：高度跟着卡片走，拖分割条就能变高
            self.url_text = tk.Text(card, height=3, wrap="word",
                                    font=F["body"], bg="#FBFBFC", fg=C["text"],
                                    insertbackground=C["text"],
                                    relief="flat", borderwidth=0,
                                    highlightthickness=1,
                                    highlightbackground=C["border_2"],
                                    highlightcolor=C["accent"],
                                    padx=10, pady=8)
            self.url_text.pack(fill="both", expand=True, pady=(8, 0))
            self._add_context_menu(self.url_text, is_text=True)

    # ---- 选项卡片 ----

    def _build_option_card(self, parent):
        with self._card(parent, pady=(10, 10)) as card:
            # ── 第 1 行：模式 + 附加功能（合并成一行，节省纵向空间）──
            row1 = tk.Frame(card, bg=C["card"])
            row1.pack(fill="x", pady=(0, 8))

            tk.Label(row1, text="模式", font=F["small"],
                     fg=C["muted"], bg=C["card"]).pack(side="left", padx=(0, 8))

            self.playlist_var = tk.BooleanVar()
            self.playlist_chip = Chip(row1, "播放列表", self.playlist_var,
                                      command=self._on_playlist_toggle)
            self.playlist_chip.pack(side="left", padx=(0, 5))

            self.folder_var = tk.BooleanVar()
            self.folder_chip = Chip(row1, "文件夹", self.folder_var,
                                    command=self._on_folder_toggle)
            self.folder_chip.pack(side="left", padx=(0, 5))

            self.dlonly_var = tk.BooleanVar()
            self.dlonly_chip = Chip(row1, "仅下载", self.dlonly_var,
                                    command=self._on_dlonly_toggle)
            self.dlonly_chip.pack(side="left", padx=(0, 18))

            self.multi_speaker_var = tk.BooleanVar()
            self.multi_speaker_chip = Chip(row1, "多说话人", self.multi_speaker_var)
            self.multi_speaker_chip.pack(side="left", padx=(0, 5))

            self.translate_var = tk.BooleanVar()
            self.translate_chip = Chip(row1, "翻译", self.translate_var)
            self.translate_chip.pack(side="left", padx=(0, 5))

            self.srt_var = tk.BooleanVar()
            self.srt_chip = Chip(row1, "字幕", self.srt_var)
            self.srt_chip.pack(side="left", padx=(0, 5))

            self.nosummary_var = tk.BooleanVar()
            self.nosummary_chip = Chip(row1, "跳过总结", self.nosummary_var)
            self.nosummary_chip.pack(side="left")

            # ── 第 2 行：总结风格 ──
            row2 = tk.Frame(card, bg=C["card"])
            row2.pack(fill="x", pady=(0, 8))

            tk.Label(row2, text="总结", font=F["small"],
                     fg=C["muted"], bg=C["card"]).pack(side="left", padx=(0, 8))

            self.style_checkboxes = {}
            self._style_chips = {}
            for label, key in [("全面总结", "auto"), ("知识点", "knowledge_points"),
                               ("操作步骤", "steps"), ("核心观点", "core_ideas"),
                               ("专家深度", "expert"), ("自定义", "custom")]:
                var = tk.BooleanVar(value=(key == "auto"))
                chip = Chip(row2, label, var, command=self._on_style_change)
                chip.pack(side="left", padx=(0, 5))
                self.style_checkboxes[key] = (var, chip)
                self._style_chips[key] = chip

            # 自定义提示词（勾选"自定义"时出现）
            self.custom_prompt_frame = tk.Frame(card, bg=C["card"])
            tk.Label(self.custom_prompt_frame, text="自定义提示词", font=F["small"],
                     fg=C["muted"], bg=C["card"]).pack(anchor="w", pady=(0, 4))
            self.custom_prompt_text = tk.Text(self.custom_prompt_frame, height=4,
                                              font=F["body"], wrap="word",
                                              bg="#FBFBFC", fg=C["text"],
                                              relief="flat", borderwidth=0,
                                              highlightthickness=1,
                                              highlightbackground=C["border_2"],
                                              highlightcolor=C["accent"],
                                              padx=10, pady=8)
            self.custom_prompt_text.pack(fill="x")

            # ── 第 3 行：输出格式 + 输出目录（合并成一行）──
            row3 = tk.Frame(card, bg=C["card"])
            row3.pack(fill="x")
            self._row3 = row3

            tk.Label(row3, text="格式", font=F["small"],
                     fg=C["muted"], bg=C["card"]).pack(side="left", padx=(0, 8))

            self.fmt_md = tk.BooleanVar(value=True)
            self.fmt_txt = tk.BooleanVar(value=False)
            self.fmt_html = tk.BooleanVar(value=False)
            self.fmt_chips = []
            for var, text in [(self.fmt_md, ".md"), (self.fmt_txt, ".txt"),
                              (self.fmt_html, ".html")]:
                chip = Chip(row3, text, var)
                chip.pack(side="left", padx=(0, 5))
                self.fmt_chips.append(chip)

            tk.Label(row3, text="目录", font=F["small"],
                     fg=C["muted"], bg=C["card"]).pack(side="left", padx=(16, 8))

            self.output_dir_var = tk.StringVar(value=str(PROJECT_ROOT / "output"))
            self.output_entry = tk.Entry(row3, textvariable=self.output_dir_var,
                                         font=F["body"], bg="#FBFBFC", fg=C["text"],
                                         insertbackground=C["text"],
                                         relief="flat", borderwidth=0,
                                         highlightthickness=1,
                                         highlightbackground=C["border_2"],
                                         highlightcolor=C["accent"])
            self.output_entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 6))
            self._add_context_menu(self.output_entry)

            FlatButton(row3, "浏览…", self._browse_output_dir,
                       compact=True).pack(side="left")

    # ---- 操作按钮行 ----

    def _build_action_row(self, parent):
        row = tk.Frame(parent, bg=C["bg"])
        row.pack(fill="x", pady=(0, 12))

        self.start_btn = FlatButton(row, "▶  开始处理", self.start, kind="primary")
        self.start_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = FlatButton(row, "■  停止", self.stop, kind="danger")

        self.open_btn = FlatButton(row, "打开输出目录", self.open_output)
        self.open_btn.pack(side="left", padx=(0, 8))

        # 系统拒绝打开目录时，复制路径是最省事的兜底
        self.copy_btn = FlatButton(row, "复制路径", self.copy_output_path)
        self.copy_btn.pack(side="left")

        # 细进度条（运行时才显示）
        self.progress = ttk.Progressbar(parent, mode="indeterminate", length=200)
        self._style_probe.configure("Thin.Horizontal.TProgressbar",
                                    troughcolor=C["border"], background=C["accent"],
                                    bordercolor=C["bg"], lightcolor=C["accent"],
                                    darkcolor=C["accent"], thickness=3)
        self.progress.configure(style="Thin.Horizontal.TProgressbar")

    # ---- 日志面板 ----

    def _build_log_panel(self, parent):
        shell = tk.Frame(parent, bg=C["log_bg"], highlightthickness=1,
                         highlightbackground=C["log_bg"])
        shell.pack(fill="both", expand=True, pady=(0, 12))

        bar = tk.Frame(shell, bg=C["log_bg"])
        bar.pack(fill="x", padx=12, pady=(6, 0))
        tk.Label(bar, text="运行日志", font=F["small"],
                 fg="#71717A", bg=C["log_bg"]).pack(side="left")

        # 清空日志（日志很长时手动清理，比滚到底更方便）
        clear = tk.Label(bar, text="清空", font=F["small"],
                         fg="#71717A", bg=C["log_bg"], cursor="hand2", padx=6)
        clear.pack(side="right")
        clear.bind("<Button-1>", lambda _e: self._clear_log())
        clear.bind("<Enter>", lambda _e: clear.configure(fg=C["log_fg"]))
        clear.bind("<Leave>", lambda _e: clear.configure(fg="#71717A"))

        self.log = tk.Text(shell, wrap="word", state="disabled",
                           font=F["mono"], bg=C["log_bg"], fg=C["log_fg"],
                           relief="flat", borderwidth=0, padx=12, pady=6,
                           insertbackground="white", selectbackground="#3F3F46")
        self.log.pack(side="left", fill="both", expand=True)

        scrollbar = tk.Scrollbar(shell, bg=C["log_bg"], troughcolor=C["log_bg"],
                                 activebackground="#3F3F46", bd=0,
                                 highlightthickness=0, width=10)
        scrollbar.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scrollbar.set)
        scrollbar.configure(command=self.log.yview)

        self.log.tag_configure("warn",  foreground=C["log_warn"])
        self.log.tag_configure("error", foreground=C["log_err"])
        self.log.tag_configure("ok",    foreground=C["log_ok"])
        self.log.tag_configure("stage", foreground=C["log_stage"])
        self.log.tag_configure("dim",   foreground="#71717A")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ---- 底部状态栏 ----

    def _build_statusbar(self):
        bar = tk.Frame(self.root, bg=C["card"])
        bar.pack(fill="x", side="bottom")
        tk.Frame(bar, bg=C["border"], height=1).pack(fill="x", side="top")

        self.status_label = tk.Label(bar, text="就绪", font=F["small"],
                                     fg=C["faint"], bg=C["card"], anchor="w", padx=20)
        self.status_label.pack(fill="x", pady=6)

    # ------------------------------------------------------------------
    # 通用控件
    # ------------------------------------------------------------------

    @contextmanager
    def _card(self, parent, **pack_kw):
        """白底细边框卡片（tk 没有圆角，用 1px 边框 + 留白换观感）

        允许调用方用 fill="both" 让卡片填满父容器（输入卡片就是这么用的，
        这样拖动分割条时输入框会跟着长高）。
        """
        f = tk.Frame(parent, bg=C["card"], highlightthickness=1,
                     highlightbackground=C["border"], padx=18, pady=14)
        pack_kw.setdefault("fill", "x")
        f.pack(**pack_kw)
        yield f

    def _add_context_menu(self, widget, *, is_text=False):
        """为输入框绑定右键菜单"""
        menu = tk.Menu(widget, tearoff=0)
        if is_text:
            menu.add_command(label="剪切", command=lambda: widget.event_generate("<<Cut>>"))
            menu.add_command(label="复制", command=lambda: widget.event_generate("<<Copy>>"))
            menu.add_command(label="粘贴", command=lambda: widget.event_generate("<<Paste>>"))
        else:
            menu.add_command(label="粘贴", command=lambda: widget.event_generate("<<Paste>>"))

        def _show_menu(event):
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        widget.bind("<Button-3>", _show_menu)
        widget.bind("<Control-Button-1>", _show_menu)

    # ------------------------------------------------------------------
    # 互斥 / 联动
    # ------------------------------------------------------------------

    def _on_playlist_toggle(self):
        """播放列表和文件夹模式互斥"""
        if self.playlist_var.get():
            self.folder_var.set(False)
            self.folder_chip.set_enabled(False)
            self.folder_chip._refresh()
        else:
            self.folder_chip.set_enabled(True)

    def _on_folder_toggle(self):
        """文件夹模式和播放列表互斥，文件夹模式下自动关仅下载"""
        if self.folder_var.get():
            self.playlist_var.set(False)
            self.playlist_chip.set_enabled(False)
            self.playlist_chip._refresh()
            self.dlonly_var.set(False)
            self.dlonly_chip._refresh()
            self._on_dlonly_toggle()
        else:
            self.playlist_chip.set_enabled(True)

    def _on_dlonly_toggle(self):
        """仅下载模式下禁用不相关的选项"""
        dlonly = self.dlonly_var.get()
        if dlonly:
            self.folder_var.set(False)
            self.folder_chip._refresh()
        for chip in (self.multi_speaker_chip, self.translate_chip,
                     self.srt_chip, self.nosummary_chip,
                     *self.fmt_chips, *self._style_chips.values()):
            chip.set_enabled(not dlonly)

    def _on_style_change(self, event=None):
        """勾选"自定义"时显示提示词输入框（插在风格行之后、格式行之前）"""
        var, _ = self.style_checkboxes.get("custom", (None, None))
        if var and var.get():
            self.custom_prompt_frame.pack(fill="x", pady=(0, 8), before=self._row3)
        else:
            self.custom_prompt_frame.pack_forget()

    def _browse_output_dir(self):
        from tkinter import filedialog
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.output_dir_var.set(path)

    # ------------------------------------------------------------------
    # 跨线程 UI 派发
    # ------------------------------------------------------------------

    def _ui(self, fn) -> None:
        """把 UI 操作派发到主线程。

        tkinter 不是线程安全的：worker 线程直接调 `root.after()` 会在 Tcl 层
        抛 `RuntimeError: main thread is not in main loop`，一旦抛在 finally
        里，`_done()` 就永远不会执行，界面永久卡在"处理中"（实测踩到）。
        这里统一走队列，由主线程的 `_pump_ui` 定时取出来执行。
        """
        if threading.current_thread() is threading.main_thread():
            fn()
            return
        self._ui_queue.put(fn)

    def _pump_ui(self) -> None:
        """主线程定时排空 UI 队列"""
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except tk.TclError:
                    pass          # 窗口已销毁，忽略
                except Exception:  # noqa: BLE001 - 单个回调失败不能拖垮泵
                    pass
        except queue.Empty:
            pass
        finally:
            try:
                self.root.after(40, self._pump_ui)
            except tk.TclError:
                pass

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------

    @staticmethod
    def _log_tag(text):
        upper = text.upper()
        if "[ERROR]" in upper or "错误" in text or "异常" in text:
            return "error"
        if "[WARN]" in upper or "失败" in text or "跳过" in text or "已停止" in text:
            return "warn"
        if "完成" in text or "已保存" in text or "成功" in text:
            return "ok"
        if text.startswith("[") and "/" in text[:6]:
            return "stage"
        return None

    def _log(self, text):
        self._ui(lambda: self._append_log(text))

    def _append_log(self, text):
        tag = self._log_tag(text)
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_status(self, text):
        def _apply():
            self.status_label.configure(text=text)
            self._status_pill.configure(text=text)
        self._ui(_apply)

    # ------------------------------------------------------------------
    # 业务逻辑
    # ------------------------------------------------------------------

    def start(self):
        raw = self.url_text.get("1.0", "end").strip()
        urls = [u.strip() for u in raw.split("\n") if u.strip()]
        if not urls:
            messagebox.showwarning("提示", "请输入视频链接或本地文件路径")
            return

        # 输出格式至少要选一个：全不选时 save_formats 什么都不写，
        # 但 pipeline 仍会把状态标成 done（永久标记为空跑完成），必须在启动前拦住。
        # 仅下载模式不产出文档，不受此限制。
        if (not self.dlonly_var.get()
                and not (self.fmt_md.get() or self.fmt_txt.get() or self.fmt_html.get())):
            messagebox.showwarning("提示", "请至少选择一种输出格式（.md / .txt / .html）")
            return

        self._cancel_token = CancelToken()

        # 只在**主线程**读一次所有 tkinter 变量，打包成普通 dict 交给 worker。
        # tkinter 不是线程安全的：worker 线程直接 get() BooleanVar / Text
        # 在 Tcl 层可能报 "main thread is not in main loop" 或读到半更新状态。
        job = {
            "formats": [fmt for var, fmt in
                        ((self.fmt_md, "md"), (self.fmt_txt, "txt"),
                         (self.fmt_html, "html")) if var.get()],
            "styles": [k for k, (var, _) in self.style_checkboxes.items() if var.get()],
            "custom_prompt": self.custom_prompt_text.get("1.0", "end-1c").strip(),
            "multi_speaker": self.multi_speaker_var.get(),
            "translate": self.translate_var.get(),
            "srt": self.srt_var.get(),
            "skip_summary": self.nosummary_var.get(),
            "dlonly": self.dlonly_var.get(),
            "playlist": self.playlist_var.get(),
            "folder": self.folder_var.get(),
            "output_dir": self.output_dir_var.get().strip(),
        }

        self.start_btn.pack_forget()
        self.stop_btn.pack(side="left", padx=(0, 8))
        self.stop_btn.configure(state="normal")
        self.progress.pack(fill="x", pady=(0, 12))
        self.progress.start()
        self._task_running = True
        self._log(f"共 {len(urls)} 个任务，开始处理…")
        self._set_status(f"处理中 0/{len(urls)}")

        threading.Thread(target=self._run, args=(urls, self._cancel_token, job),
                         daemon=True).start()

    def stop(self):
        """请求取消：只发信号并置灰按钮，真正恢复 UI 由 worker 的 finally 负责。"""
        if self._cancel_token.cancelled:
            return
        self._cancel_token.cancel()
        self.stop_btn.configure(state="disabled")
        self._set_status("正在停止…")
        self._log("[WARN] 收到停止请求，正在中断当前任务…")

    def _run(self, urls, cancel_token, job):
        """worker 线程：只使用 job 里快照好的普通值，不碰任何 tkinter 对象"""
        formats = job["formats"]
        selected_styles = job["styles"] or ["auto"]

        handler = _GuiLogHandler(self)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        failed = 0
        try:
            # 读配置也放进 try：config.yaml 损坏 / 缺文件时不能卡在"处理中"
            with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            load_env()

            summarizer_cfg = config.setdefault("summarizer", {})
            summarizer_cfg["summary_styles"] = selected_styles
            if "custom" in selected_styles and job["custom_prompt"]:
                summarizer_cfg["custom_prompt"] = job["custom_prompt"]
            summarizer_cfg["output_formats"] = formats
            if job["multi_speaker"]:
                config.setdefault("summarizer", {})["multi_speaker"] = True
            if job["output_dir"]:
                config["output_dir"] = job["output_dir"]

            pipeline = Pipeline(config,
                                translate=job["translate"],
                                srt=job["srt"],
                                skip_summary=job["skip_summary"],
                                cancel_token=cancel_token)

            for i, url in enumerate(urls):
                if cancel_token.cancelled:
                    break
                self._set_status(f"处理中 {i+1}/{len(urls)}")
                self._log(f"\n[{i+1}/{len(urls)}] {url}")

                try:
                    if job["dlonly"]:
                        pipeline.download_only(url, is_playlist=job["playlist"])
                    elif job["folder"]:
                        pipeline.last_failures = 0
                        pipeline.process_folder(url)
                        failed += pipeline.last_failures
                    elif job["playlist"]:
                        pipeline.last_failures = 0
                        pipeline.process(url, is_playlist=True)
                        failed += pipeline.last_failures
                    else:
                        pipeline.process(url)
                except CancelledError:
                    raise
                except Exception as e:
                    failed += 1
                    self._log(f"  [错误] {e}")

            if cancel_token.cancelled:
                self._log("[WARN] 已停止，进度已保存")
                self._set_status("已停止")
            elif failed:
                # 部分失败就不能报"全部完成"，否则用户会以为产物齐全
                self._log(f"\n完成，但有 {failed}/{len(urls)} 个任务失败（见上方 [错误]）")
                self._set_status(f"完成（{failed} 个失败）")
            else:
                self._log("\n全部完成！")
                self._set_status("完成")

        except CancelledError:
            self._log("[WARN] 已停止，进度已保存")
            self._set_status("已停止")
        except Exception as e:
            self._log(f"异常: {e}")
            self._set_status("出错")
        finally:
            root_logger.removeHandler(handler)
            self._ui(self._done)

    def _done(self):
        self._task_running = False
        self.progress.stop()
        self.progress.pack_forget()
        self.stop_btn.pack_forget()
        self.stop_btn.configure(state="normal")
        self.start_btn.pack(side="left", padx=(0, 8))
        self.start_btn.configure(state="normal")

    # ------------------------------------------------------------------
    # 打开输出目录
    # ------------------------------------------------------------------

    def open_output(self):
        """打开输出目录。

        依次尝试系统级打开方式；如果全都被系统拒绝（安全软件 / 组策略 /
        沙箱），就退回到**应用内目录浏览器** —— 纯 tkinter 实现，不依赖
        任何系统 shell 调用，因此一定能打开。这样按钮永远不会"点了没反应"。
        """
        path_str = self.output_dir_var.get().strip()
        output_dir = Path(path_str) if path_str else (PROJECT_ROOT / "output")

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._log(f"[错误] 输出目录不可用: {output_dir} ({e})")
            messagebox.showerror("打开输出目录失败",
                                 f"目录不存在且无法创建：\n{output_dir}\n\n{e}")
            return

        ok, reason = self._open_path(output_dir)
        if ok:
            self._log(f"已打开输出目录: {output_dir}")
            self._set_status("已打开输出目录")
            return

        # 系统方式全部失败：先做一次环境自检（判断是"只有这个目录"还是
        # "整个 shell 都打不开"），再给用户可用的应用内列表。
        self._log(f"[WARN] 系统拒绝打开目录（{reason}），改用应用内目录列表")
        self._log("[诊断] 打开目录环境自检（无弹窗）：")
        probe = self._probe_open_env(output_dir)
        only_this = any(ok_ for label, ok_, _e in probe if label != "输出目录")
        for label, ok_, err in probe:
            mark = "成功" if ok_ else f"失败（{err}）"
            self._log(f"    {label}: {mark}")
        if only_this:
            self._log("    → 其他目录可正常打开，说明是该输出目录本身的权限/属性问题")
        else:
            self._log("    → 本程序进程内所有目录都打不开（错误码 5 = 拒绝访问）。")
            self._log("      注意：这只说明**本进程**被拒绝，不代表系统坏了。请先做两个对照测试：")
            self._log("      A) 按 Win+R，输入下面这个路径回车 —— 能打开吗？")
            self._log(f"         {output_dir}")
            self._log("      B) 打开 PowerShell，粘贴并回车：")
            self._log(f'         Start-Process "{output_dir}"')
            self._log("      若 A/B 能打开、只有本程序不行 → 是本进程被策略/安全软件针对；")
            self._log("      若 A/B 也打不开 → 是该目录或 shell 本身的问题。")
            self._log("      本进程的可执行文件与项目目录（加白名单时用）：")
            self._log(f"         1) {sys.executable}")
            self._log(f"         2) {PROJECT_ROOT}")

        self._set_status("已打开目录列表")
        self._show_folder_window(output_dir, note=reason)

    # ---- 应用内目录浏览器（不依赖系统 shell，任何环境都能用）----

    def _show_folder_window(self, folder: Path, note: str = ""):
        """在应用内列出目录内容，并可进入子目录 / 打开文件"""
        win = tk.Toplevel(self.root)
        win.title("输出目录")
        win.configure(bg=C["bg"])
        win.geometry("760x560")
        win.minsize(560, 360)
        try:
            win.transient(self.root)
        except tk.TclError:
            pass

        state = {"dir": Path(folder)}

        # 顶部：路径 + 操作
        head = tk.Frame(win, bg=C["card"], padx=16, pady=10)
        head.pack(fill="x")
        tk.Frame(win, bg=C["border"], height=1).pack(fill="x")

        path_var = tk.StringVar(value=str(folder))
        entry = tk.Entry(head, textvariable=path_var, font=F["body"],
                         bg=C["bg"], fg=C["text"], relief="flat", borderwidth=0,
                         state="readonly", readonlybackground=C["bg"])
        entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 10))
        self._add_context_menu(entry)

        if note:
            tk.Label(win, text=f"系统拒绝自动打开（{note}），这里是应用内目录列表。",
                     font=F["small"], fg=C["faint"], bg=C["bg"],
                     anchor="w", justify="left", wraplength=700,
                     padx=16).pack(fill="x", pady=(6, 0))

        # 工具栏
        tools = tk.Frame(win, bg=C["bg"], padx=16, pady=8)
        tools.pack(fill="x")

        tree_wrap = tk.Frame(win, bg=C["card"], highlightthickness=1,
                             highlightbackground=C["border"])
        tree_wrap.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        # 让 Treeview 融入整体配色（默认样式是系统灰白，很突兀）
        style = ttk.Style(win)
        style.configure("Folder.Treeview",
                        background=C["card"], fieldbackground=C["card"],
                        foreground=C["text"], borderwidth=0, relief="flat",
                        rowheight=26, font=F["body"])
        style.configure("Folder.Treeview.Heading",
                        background=C["card"], foreground=C["muted"],
                        borderwidth=0, relief="flat", font=F["small"], padding=(8, 6))
        style.map("Folder.Treeview",
                  background=[("selected", C["accent_soft"])],
                  foreground=[("selected", C["accent"])])
        style.map("Folder.Treeview.Heading", background=[("active", C["hover"])])
        style.layout("Folder.Treeview", [
            ("Folder.Treeview.treearea", {"sticky": "nswe"})
        ])

        columns = ("size", "mtime")
        tree = ttk.Treeview(tree_wrap, columns=columns, show="tree headings",
                            selectmode="browse", style="Folder.Treeview")
        tree.heading("#0", text="名称", anchor="w")
        tree.heading("size", text="大小", anchor="e")
        tree.heading("mtime", text="修改时间", anchor="w")
        tree.column("#0", width=440, anchor="w", stretch=True)
        tree.column("size", width=96, anchor="e", stretch=False)
        tree.column("mtime", width=150, anchor="w", stretch=False)
        tree.pack(side="left", fill="both", expand=True)

        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=tree.yview)
        vsb.pack(side="right", fill="y")
        tree.configure(yscrollcommand=vsb.set)

        # 状态行
        info = tk.Label(win, text="", font=F["small"], fg=C["faint"],
                        bg=C["bg"], anchor="w", padx=16)
        info.pack(fill="x", pady=(0, 10))


        def refresh() -> None:
            tree.delete(*tree.get_children())
            cur = state["dir"]
            path_var.set(str(cur))
            try:
                entries = sorted(cur.iterdir(),
                                 key=lambda x: (x.is_file(), x.name.lower()))
            except OSError as e:
                info.configure(text=f"无法读取目录: {e}")
                return
            dirs = files = 0
            import datetime
            for item in entries:
                if item.name.startswith("."):
                    continue
                try:
                    st = item.stat()
                    ts = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
                except OSError:
                    ts, st = "", None
                if item.is_dir():
                    dirs += 1
                    tree.insert("", "end", iid=str(item), text=f"📁  {item.name}",
                                values=("<文件夹>", ts))
                else:
                    files += 1
                    tree.insert("", "end", iid=str(item), text=f"📄  {item.name}",
                                values=(fmt_size(st.st_size, space=True) if st else "", ts))
            info.configure(text=f"{dirs} 个文件夹，{files} 个文件")

        def open_selected(_event=None) -> None:
            sel = tree.selection()
            if not sel:
                return
            target = Path(sel[0])
            if target.is_dir():
                state["dir"] = target
                refresh()
                return
            ok2, why = App._open_path(target)
            if ok2:
                self._log(f"已打开文件: {target.name}")
            else:
                # 文件也打不开时，至少把上级目录路径给用户
                self._copy_path(target.parent)
                messagebox.showinfo(
                    "无法打开文件",
                    f"系统拒绝打开这个文件（{why}）。\n\n"
                    f"文件位置：\n{target}\n\n"
                    f"所在目录路径已复制到剪贴板。",
                )

        def go_up() -> None:
            parent = state["dir"].parent
            if parent != state["dir"] and parent.exists():
                state["dir"] = parent
                refresh()

        def copy_current() -> None:
            self._copy_path(state["dir"])

        def retry_system() -> None:
            ok2, why = App._open_path(state["dir"])
            if ok2:
                info.configure(text="已在资源管理器中打开")
            else:
                info.configure(text=f"系统仍然拒绝打开：{why}")

        FlatButton(tools, "上一级", go_up, compact=True).pack(side="left", padx=(0, 6))
        FlatButton(tools, "刷新", refresh, compact=True).pack(side="left", padx=(0, 6))
        FlatButton(tools, "复制路径", copy_current, compact=True).pack(side="left", padx=(0, 6))
        FlatButton(tools, "用资源管理器打开", retry_system,
                   compact=True).pack(side="left")

        tree.bind("<Double-1>", open_selected)
        tree.bind("<Return>", open_selected)

        refresh()
        win.focus_set()

    def _copy_path(self, path: Path) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(path))
            self._log(f"路径已复制到剪贴板: {path}")
        except tk.TclError as e:
            self._log(f"[WARN] 复制路径失败: {e}")

    def copy_output_path(self) -> None:
        """复制输出目录路径到剪贴板（不依赖任何系统调用，一定能用）"""
        path_str = self.output_dir_var.get().strip()
        output_dir = Path(path_str) if path_str else (PROJECT_ROOT / "output")
        self._copy_path(output_dir)
        self._set_status("路径已复制到剪贴板")

    @staticmethod
    def _open_path(path: Path):
        """尝试用系统方式打开路径，返回 (是否成功, 失败原因说明)

        注意：这里**不做 `Path.resolve()`**。改造前能正常打开的实现是直接把
        用户填的路径字符串交给 `os.startfile`；`resolve()` 会把符号链接/短路径
        展开成另一种写法，在个别环境下会让 shell 解析出不同结果。保持"原样
        传递"最接近已验证可用的行为。
        """
        path = Path(path)
        if not path.is_absolute():
            path = path.absolute()
        p = str(path)

        if sys.platform == "win32":
            return App._shell_open_windows(p)

        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", p], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(["xdg-open", p], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, ""
        except Exception as e:      # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

    # ---- Windows 打开目录：ShellExecuteEx（不带 UI，失败不弹系统框）----

    @staticmethod
    def _shell_execute_ex(path_str: str, verb: "str | None" = None) -> "tuple[bool, int]":
        """调用 ShellExecuteExW，返回 (是否成功, 错误码)。

        相比 `os.startfile`，这里加了 `SEE_MASK_FLAG_NO_UI`：失败时**不会**
        弹 Windows 的「无法访问指定设备、路径或文件」系统对话框，而是把手
        交给我们自己处理（记录原因 + 回退到应用内浏览器）。verb=None 表示
        用系统默认动作，和 `os.startfile` 行为一致。
        """
        import ctypes

        class _SEI(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong), ("fMask", ctypes.c_ulong),
                ("hwnd", ctypes.c_void_p), ("lpVerb", ctypes.c_wchar_p),
                ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
                ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int),
                ("hInstApp", ctypes.c_void_p), ("lpIDList", ctypes.c_void_p),
                ("lpClass", ctypes.c_wchar_p), ("hkeyClass", ctypes.c_void_p),
                ("dwHotKey", ctypes.c_ulong), ("hIcon", ctypes.c_void_p),
                ("hProcess", ctypes.c_void_p),
            ]

        SEE_MASK_NOCLOSEPROCESS = 0x00000040
        SEE_MASK_FLAG_NO_UI = 0x00000400
        SEE_MASK_INVOKEIDLIST = 0x0000000C

        sei = _SEI()
        sei.cbSize = ctypes.sizeof(_SEI)
        sei.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_FLAG_NO_UI | SEE_MASK_INVOKEIDLIST
        sei.lpVerb = verb
        sei.lpFile = path_str
        sei.nShow = 1                       # SW_SHOWNORMAL
        try:
            # 必须用 use_last_error=True 才能可靠读回错误码，
            # 否则 ctypes.get_last_error() 恒为 0，诊断信息毫无意义。
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SEI)]
            shell32.ShellExecuteExW.restype = ctypes.c_bool
            ok = shell32.ShellExecuteExW(ctypes.byref(sei))
            err = 0 if ok else ctypes.get_last_error()
        except Exception:                   # noqa: BLE001
            return False, -1
        if sei.hProcess:
            try:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
                kernel32.CloseHandle(ctypes.c_void_p(sei.hProcess))
            except Exception:               # noqa: BLE001
                pass
        return ok, err

    @staticmethod
    def _shell_open_windows(path_str: str):
        """Windows：打开目录，失败时返回可读原因。

        依次尝试（全部用 ShellExecuteExW + 不弹 UI）：
          1. 默认动作（等价于原实现的 os.startfile）
          2. 显式 "open"      —— 某些Association 缺失时默认动作找不到，指定动词反而能成
          3. 显式 "explore"   —— 目录专用动词

        **绝不 `Popen(["explorer", ...])`**：受限环境下 explorer.exe 会以
        0xc0000142 STATUS_DLL_INIT_FAILED 崩溃并弹系统级报错框（实测踩过）。

        注意：不要用 `ShellExecuteW`（老 API）作为判断依据 —— 它在某些环境
        返回 >32 的"成功"值却没有真正打开窗口，会让我们误判成功。
        """
        _ERROR_TEXT = {
            0: "系统未返回错误码",
            2: "找不到路径",
            3: "找不到路径",
            5: "拒绝访问",
            31: "没有可用于打开的程序",
        }

        errors = []
        for verb, label in ((None, "默认动作"), ("open", "open"), ("explore", "explore")):
            ok, err = App._shell_execute_ex(path_str, verb)
            if ok:
                return True, ""
            errors.append(f"{label}→{_ERROR_TEXT.get(err, f'错误码 {err}')}")

        return False, "；".join(errors)

    @staticmethod
    def _probe_open_env(target: Path) -> "list[tuple[str, bool, str]]":
        """诊断：分别尝试打开几个代表性路径，用于判断「只有这个目录不行」
        还是「整个系统的 shell 都打不开」。全部走无 UI 调用，不弹框。"""
        import tempfile
        candidates = [
            ("输出目录", target),
            ("项目目录", PROJECT_ROOT),
            ("用户临时目录", Path(tempfile.gettempdir())),
            ("C:\\Windows", Path("C:\\Windows")),
        ]
        results = []
        for label, path in candidates:
            try:
                exists = Path(path).exists()
            except OSError:
                exists = False
            if not exists:
                results.append((label, False, "路径不存在"))
                continue
            ok, err = App._shell_execute_ex(str(path), None)
            results.append((label, ok, "" if ok else f"错误码 {err}"))
        return results


def main():
    setup_logging()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
