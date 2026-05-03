"""
Video-to-Doc 图形界面
"""
import os
import sys
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

from utils import find_venv_executable

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


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Video-to-Doc")
        self.root.geometry("680x540")
        self.root.minsize(500, 400)
        self.root.configure(bg=C["bg"])
        self._process = None
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
            tk.Label(card, text="视频链接", font=F["section"],
                     fg=C["text"], bg=C["card"]).pack(anchor="w")

            self.url_text = tk.Text(card, height=3, wrap="word",
                                    font=F["body"], bg="#f9fafb", fg=C["text"],
                                    relief="solid", borderwidth=1,
                                    padx=10, pady=8)
            self.url_text.pack(fill="x", pady=(8, 4))

            tk.Label(card, text="支持 URL 和本地文件路径，每行一个",
                     font=F["small"], fg=C["muted"], bg=C["card"]).pack(anchor="w")

        # 选项卡片
        with self._card(main, pady=(0, 10)) as card:
            row1 = tk.Frame(card, bg=C["card"])
            row1.pack(fill="x", pady=(0, 8))

            self.playlist_var = tk.BooleanVar()
            tk.Checkbutton(row1, text="播放列表/合集", variable=self.playlist_var,
                           font=F["body"], bg=C["card"],
                           activebackground=C["card"],
                           selectcolor=C["card"]).pack(side="left")

            tk.Label(row1, text="  总结风格：", font=F["body"],
                     fg=C["text"], bg=C["card"]).pack(side="left")
            self.style_var = tk.StringVar(value="auto")
            ttk.Combobox(row1, textvariable=self.style_var,
                         values=["auto", "knowledge_points", "steps", "core_ideas"],
                         state="readonly", width=18,
                         font=F["body"]).pack(side="left", padx=(6, 0))

            row2 = tk.Frame(card, bg=C["card"])
            row2.pack(fill="x")
            tk.Label(row2, text="输出格式：", font=F["body"],
                     fg=C["text"], bg=C["card"]).pack(side="left")

            self.fmt_md = tk.BooleanVar(value=True)
            self.fmt_txt = tk.BooleanVar(value=False)
            self.fmt_html = tk.BooleanVar(value=False)
            for v, lb in [(self.fmt_md, ".md"), (self.fmt_txt, ".txt"), (self.fmt_html, ".html")]:
                tk.Checkbutton(row2, text=lb, variable=v, font=F["body"],
                               bg=C["card"], activebackground=C["card"],
                               selectcolor=C["card"]).pack(side="left", padx=(12, 0))

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
        if self._process and self._process.poll() is None:
            self._process.terminate()
        self._log("\n[WARN] 用户停止")
        self._set_status("已停止")
        self._done()

    def _run(self, urls):
        try:
            python = find_venv_executable("python")
            formats = []
            if self.fmt_md.get():   formats.append("md")
            if self.fmt_txt.get():  formats.append("txt")
            if self.fmt_html.get(): formats.append("html")

            for i, url in enumerate(urls):
                if self._stopped:
                    break
                self._set_status(f"处理中 ({i+1}/{len(urls)})")
                self._log(f"\n[{i+1}/{len(urls)}] {url}")

                cmd = [python, "-u", str(PROJECT_ROOT / "main.py"), url]
                if self.playlist_var.get():
                    cmd.append("--playlist")
                cmd.extend(["--summary-style", self.style_var.get()])
                cmd.extend(["--output-formats", ",".join(formats)])

                self._process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, cwd=str(PROJECT_ROOT),
                )

                for line in self._process.stdout:
                    line = line.strip()
                    if line:
                        self.root.after(0, lambda t=line: self._log(t))

                self._process.wait()
                if self._process.returncode != 0:
                    self._log(f"  [错误] 处理失败 (退出码 {self._process.returncode})")

            self._log("\n全部完成!")
            self._set_status("完成")

        except Exception as e:
            self._log(f"异常: {e}")
            self._set_status("出错")
        finally:
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
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
