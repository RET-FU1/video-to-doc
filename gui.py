"""
Video-to-Doc 图形界面
"""
import logging
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

import yaml
from pipeline import Pipeline
from utils import load_env, setup_logging

PROJECT_ROOT = Path(__file__).parent

# 配色
C = {
    "bg":         "#f0f2f5",
    "card":       "#ffffff",
    "primary":    "#2563eb",
    "primary2":   "#1d4ed8",
    "text":       "#1a1a2e",
    "muted":      "#6b7280",
    "border":     "#e5e7eb",
    "log_bg":     "#1e1e2e",
    "log_fg":     "#c9d1d9",
    "green":      "#10b981",
    "red":        "#ef4444",
    "yellow":     "#eab308",
    "blue":       "#60a5fa",
}

F = {
    "header":  ("Segoe UI", 18, "bold"),
    "section": ("Segoe UI", 11, "bold"),
    "body":    ("Segoe UI", 10),
    "small":   ("Segoe UI", 9),
    "mono":    ("Consolas", 10),
}


class _GuiLogHandler(logging.Handler):
    """日志处理器：将 log 消息桥接到 GUI 日志窗口"""

    def __init__(self, app: "App") -> None:
        super().__init__()
        self._app = app

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        self._app._log(msg)


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Video-to-Doc")
        self.root.geometry("900x780")
        self.root.minsize(600, 450)
        self.root.configure(bg=C["bg"])
        self._stopped = False
        self._build_ui()

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------

    def _build_ui(self):
        # 顶部标题栏
        header = tk.Frame(self.root, bg=C["primary"], height=52)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="Video-to-Doc", font=F["header"],
                 fg="white", bg=C["primary"]).pack(side="left", padx=20, pady=10)
        tk.Label(header, text="下载 · 转写 · AI 总结", font=F["small"],
                 fg="#93c5fd", bg=C["primary"]).pack(side="left", pady=14)

        # 主内容区
        main = tk.Frame(self.root, bg=C["bg"])
        main.pack(fill="both", expand=True, padx=16, pady=(12, 0))

        # URL 输入卡片
        with self._card(main, pady=(0, 10)) as card:
            tk.Label(card, text="视频/音频", font=F["section"],
                     fg=C["text"], bg=C["card"]).pack(anchor="w")

            self.url_text = tk.Text(card, height=3, wrap="word",
                                    font=F["body"], bg="#f9fafb", fg=C["text"],
                                    relief="solid", borderwidth=1,
                                    padx=10, pady=8)
            self.url_text.pack(fill="x", pady=(8, 4))

            tk.Label(card, text="支持 URL / 本地视频 / 本地音频 (mp3/wav/m4a等) / 文件夹路径。文件夹模式只需填写一行",
                     font=F["small"], fg=C["muted"], bg=C["card"]).pack(anchor="w")

        # 选项卡片
        with self._card(main, pady=(0, 10)) as card:
            row1 = tk.Frame(card, bg=C["card"])
            row1.pack(fill="x", pady=(0, 6))

            self.playlist_var = tk.BooleanVar()
            self.playlist_cb = tk.Checkbutton(row1, text="播放列表/合集", variable=self.playlist_var,
                           font=F["body"], bg=C["card"],
                           activebackground=C["card"],
                           selectcolor=C["card"])
            self.playlist_cb.pack(side="left")

            self.folder_var = tk.BooleanVar()
            self.folder_cb = tk.Checkbutton(row1, text="文件夹模式", variable=self.folder_var,
                           font=F["body"], bg=C["card"],
                           activebackground=C["card"],
                           selectcolor=C["card"],
                           command=self._on_folder_toggle)
            self.folder_cb.pack(side="left", padx=(12, 0))

            self.dlonly_var = tk.BooleanVar()
            self.dlonly_cb = tk.Checkbutton(row1, text="仅下载", variable=self.dlonly_var,
                           font=F["body"], bg=C["card"],
                           activebackground=C["card"],
                           selectcolor=C["card"],
                           command=self._on_dlonly_toggle)
            self.dlonly_cb.pack(side="left", padx=(12, 0))

            self.diarize_var = tk.BooleanVar()
            self.diarize_cb = tk.Checkbutton(row1, text="说话人分离", variable=self.diarize_var,
                           font=F["body"], bg=C["card"],
                           activebackground=C["card"],
                           selectcolor=C["card"])
            self.diarize_cb.pack(side="left", padx=(12, 0))

            self.translate_var = tk.BooleanVar()
            self.translate_cb = tk.Checkbutton(row1, text="翻译", variable=self.translate_var,
                           font=F["body"], bg=C["card"],
                           activebackground=C["card"],
                           selectcolor=C["card"])
            self.translate_cb.pack(side="left", padx=(12, 0))

            self.srt_var = tk.BooleanVar()
            self.srt_cb = tk.Checkbutton(row1, text="字幕", variable=self.srt_var,
                           font=F["body"], bg=C["card"],
                           activebackground=C["card"],
                           selectcolor=C["card"])
            self.srt_cb.pack(side="left", padx=(12, 0))

            self.nosummary_var = tk.BooleanVar()
            self.nosummary_cb = tk.Checkbutton(row1, text="跳过总结", variable=self.nosummary_var,
                           font=F["body"], bg=C["card"],
                           activebackground=C["card"],
                           selectcolor=C["card"])
            self.nosummary_cb.pack(side="left", padx=(12, 0))

            row2 = tk.Frame(card, bg=C["card"])
            row2.pack(fill="x", pady=(0, 6))

            tk.Label(row2, text="总结风格：", font=F["body"],
                     fg=C["text"], bg=C["card"]).pack(side="left")

            self.style_labels = ["全面总结", "知识点提取", "操作步骤", "核心观点", "专家深度"]
            self.style_map = {
                "全面总结": "auto",
                "知识点提取": "knowledge_points",
                "操作步骤": "steps",
                "核心观点": "core_ideas",
                "专家深度": "expert",
            }

            self.style_var = tk.StringVar(value="全面总结")
            self.style_combo = ttk.Combobox(row2, textvariable=self.style_var,
                         values=self.style_labels,
                         state="readonly", width=14,
                         font=F["body"])
            self.style_combo.pack(side="left", padx=(6, 0))
            self.style_combo.bind("<<ComboboxSelected>>", self._on_style_change)

            self.style_desc = tk.Label(row2, text="— 精炼文章式：核心观点 → 论证展开 → 关键收获",
                                       font=F["small"], fg=C["muted"], bg=C["card"])
            self.style_desc.pack(side="left", padx=(6, 0))

            row3 = tk.Frame(card, bg=C["card"])
            row3.pack(fill="x")
            tk.Label(row3, text="输出格式：", font=F["body"],
                     fg=C["text"], bg=C["card"]).pack(side="left")

            self.fmt_md = tk.BooleanVar(value=True)
            self.fmt_txt = tk.BooleanVar(value=False)
            self.fmt_html = tk.BooleanVar(value=False)
            self.fmt_cbs = []
            for v, lb in [(self.fmt_md, ".md"), (self.fmt_txt, ".txt"), (self.fmt_html, ".html")]:
                cb = tk.Checkbutton(row3, text=lb, variable=v, font=F["body"],
                               bg=C["card"], activebackground=C["card"],
                               selectcolor=C["card"])
                cb.pack(side="left", padx=(12, 0))
                self.fmt_cbs.append(cb)

        # 按钮行
        btn_row = tk.Frame(main, bg=C["bg"])
        btn_row.pack(fill="x", pady=(0, 10))

        self.start_btn = tk.Button(btn_row, text="▶  开始处理", command=self.start,
                                   font=F["section"], fg="white", bg=C["primary"],
                                   activebackground=C["primary2"],
                                   activeforeground="white",
                                   relief="flat", cursor="hand2",
                                   padx=24, pady=8, bd=0)
        self.start_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = tk.Button(btn_row, text="■  停止", command=self.stop,
                                  font=F["section"], fg="white", bg=C["red"],
                                  activebackground="#dc2626",
                                  activeforeground="white",
                                  relief="flat", cursor="hand2",
                                  padx=20, pady=8, bd=0)

        tk.Button(btn_row, text="打开输出目录", command=self.open_output,
                  font=F["body"], fg=C["text"], bg=C["card"],
                  activebackground="#f3f4f6", relief="solid",
                  borderwidth=1, padx=16, pady=8).pack(side="left")

        # 进度条（初始隐藏）
        self.progress = ttk.Progressbar(main, mode="indeterminate")

        # 日志区域（暗色终端风格）
        log_frame = tk.Frame(main, bg=C["log_bg"], highlightthickness=0)
        log_frame.pack(fill="both", expand=True)

        self.log = tk.Text(log_frame, wrap="word", state="disabled",
                           font=F["mono"], bg=C["log_bg"], fg=C["log_fg"],
                           relief="flat", borderwidth=0, padx=12, pady=8,
                           insertbackground="white")
        self.log.pack(side="left", fill="both", expand=True)

        scrollbar = tk.Scrollbar(log_frame, bg=C["log_bg"],
                                 troughcolor=C["log_bg"],
                                 activebackground=C["muted"])
        scrollbar.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scrollbar.set)
        scrollbar.configure(command=self.log.yview)

        # 日志颜色标签
        self.log.tag_configure("warn",   foreground=C["yellow"])
        self.log.tag_configure("error",  foreground=C["red"])
        self.log.tag_configure("ok",     foreground=C["green"])
        self.log.tag_configure("stage",  foreground=C["blue"])

        # 底部状态栏
        bar = tk.Frame(self.root, bg=C["border"], height=26)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.status_label = tk.Label(bar, text="就绪", font=F["small"],
                                     fg=C["muted"], bg=C["bg"], anchor="w", padx=14)
        self.status_label.pack(fill="x")

    @contextmanager
    def _card(self, parent, **pack_kw):
        f = tk.Frame(parent, bg=C["card"], highlightbackground=C["border"],
                     highlightthickness=1, padx=16, pady=12)
        f.pack(fill="x", **pack_kw)
        yield f

    def _on_folder_toggle(self):
        if self.folder_var.get():
            self.dlonly_var.set(False)
            self._on_dlonly_toggle()

    def _on_dlonly_toggle(self):
        dlonly = self.dlonly_var.get()
        if dlonly:
            self.folder_var.set(False)
        state = "disabled" if dlonly else "normal"
        readonly = "disabled" if dlonly else "readonly"
        for w in (self.folder_cb, self.diarize_cb,
                  self.translate_cb, self.srt_cb, self.nosummary_cb):
            w.configure(state=state)
        self.style_combo.configure(state=readonly)
        for cb in self.fmt_cbs:
            cb.configure(state=state)

    def _on_style_change(self, event=None):
        descs = {
            "全面总结": "— 精炼文章式：核心观点 → 论证展开 → 关键收获",
            "知识点提取": "— 结构化知识点：概念解释 + 为何重要 + 原文例子",
            "操作步骤": "— 步骤拆解：做什么 + 为什么必要 + 怎么做 + 坑点",
            "核心观点": "— 洞察提炼：拒绝话题罗列，每条都是「原来如此」",
            "专家深度": "— 世界级专家视角，自我核查事实，锐利批判思维",
        }
        label = self.style_var.get()
        self.style_desc.configure(text=descs.get(label, ""))

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------

    @staticmethod
    def _log_tag(text):
        upper = text.upper()
        if "[WARN]" in upper or "失败" in text or "跳过" in text:
            return "warn"
        if "[ERROR]" in upper or "错误" in text or "异常" in text:
            return "error"
        if "完成" in text or "已保存" in text or "成功" in text:
            return "ok"
        if text.startswith("[") and "/" in text[:6]:
            return "stage"
        return None

    def _log(self, text):
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, lambda t=text: self._log(t))
            return
        tag = self._log_tag(text)

        self.log.configure(state="normal")
        if tag:
            self.log.insert("end", text + "\n", tag)
        else:
            self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_status(self, text):
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, lambda t=text: self._set_status(t))
            return
        self.status_label.configure(text=text)

    # ------------------------------------------------------------------
    # 业务逻辑
    # ------------------------------------------------------------------

    def start(self):
        raw = self.url_text.get("1.0", "end").strip()
        urls = [u.strip() for u in raw.split("\n") if u.strip()]
        if not urls:
            messagebox.showwarning("提示", "请输入视频链接或本地文件路径")
            return

        self.start_btn.pack_forget()
        self.stop_btn.pack(side="left", padx=(0, 8))
        self.progress.pack(fill="x", pady=(0, 8))
        self.progress.start()
        self._log(f"共 {len(urls)} 个任务，开始处理...")
        self._set_status(f"处理中 (0/{len(urls)})")

        self._stopped = False
        threading.Thread(target=self._run, args=(urls,), daemon=True).start()

    def stop(self):
        self._stopped = True
        self._log("\n[WARN] 用户停止")
        self._set_status("已停止")
        self._done()

    def _run(self, urls):
        formats = []
        if self.fmt_md.get():   formats.append("md")
        if self.fmt_txt.get():  formats.append("txt")
        if self.fmt_html.get(): formats.append("html")

        style_eng = self.style_map.get(self.style_var.get(), "auto")

        with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        load_env()

        summarizer_cfg = config.setdefault("summarizer", {})
        summarizer_cfg["summary_style"] = style_eng
        summarizer_cfg["output_formats"] = formats
        if self.diarize_var.get():
            config.setdefault("diarization", {})["enabled"] = True

        # 注入 GUI 日志 handler
        handler = _GuiLogHandler(self)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        try:
            pipeline = Pipeline(config,
                                translate=self.translate_var.get(),
                                srt=self.srt_var.get(),
                                skip_summary=self.nosummary_var.get())

            for i, url in enumerate(urls):
                if self._stopped:
                    break
                self._set_status(f"处理中 ({i+1}/{len(urls)})")
                self._log(f"\n[{i+1}/{len(urls)}] {url}")

                try:
                    if self.dlonly_var.get():
                        pipeline.download_only(url, is_playlist=self.playlist_var.get())
                    elif self.folder_var.get():
                        pipeline.process_folder(url)
                    elif self.playlist_var.get():
                        pipeline.process(url, is_playlist=True)
                    else:
                        pipeline.process(url)
                except Exception as e:
                    self._log(f"  [错误] {e}")

            if not self._stopped:
                self._log("\n全部完成!")
                self._set_status("完成")

        except Exception as e:
            self._log(f"异常: {e}")
            self._set_status("出错")
        finally:
            root_logger.removeHandler(handler)
            self.root.after(0, self._done)

    def _done(self):
        self.progress.stop()
        self.progress.pack_forget()
        self.stop_btn.pack_forget()
        self.start_btn.pack(side="left", padx=(0, 8))
        self.start_btn.configure(state="normal")

    def open_output(self):
        output_dir = PROJECT_ROOT / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(output_dir))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(output_dir)])
        else:
            subprocess.run(["xdg-open", str(output_dir)])


def main():
    setup_logging()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
