"""
Video-to-Doc 图形界面
"""
import os
import sys
import subprocess
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

PROJECT_ROOT = Path(__file__).parent


def get_python():
    if sys.platform == "win32":
        return str(PROJECT_ROOT / "venv" / "Scripts" / "python.exe")
    return str(PROJECT_ROOT / "venv" / "bin" / "python")


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Video-to-Doc")
        self.root.geometry("600x400")
        self.root.resizable(True, True)

        # 主框架
        main = ttk.Frame(root, padding=16)
        main.pack(fill="both", expand=True)

        # 标题
        title = ttk.Label(main, text="Video-to-Doc", font=("", 16, "bold"))
        title.pack(pady=(0, 16))

        # URL 输入
        url_frame = ttk.Frame(main)
        url_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(url_frame, text="视频链接：").pack(anchor="w")
        self.url_text = tk.Text(url_frame, height=4, wrap="word",
                                font=("", 9))
        self.url_text.pack(fill="x", pady=(4, 0))
        ttk.Label(url_frame, text="每行一个链接，支持同时粘贴多个",
                  foreground="gray").pack(anchor="w")

        # 选项行1
        opt_frame = ttk.Frame(main)
        opt_frame.pack(fill="x", pady=(0, 4))
        self.playlist_var = tk.BooleanVar()
        ttk.Checkbutton(opt_frame, text="播放列表/合集", variable=self.playlist_var).pack(side="left")
        self.style_var = tk.StringVar(value="auto")
        ttk.Label(opt_frame, text="  总结风格：").pack(side="left")
        style_combo = ttk.Combobox(opt_frame, textvariable=self.style_var,
                                   values=["auto", "knowledge_points", "steps", "core_ideas"],
                                   state="readonly", width=16)
        style_combo.pack(side="left", padx=(4, 0))

        # 输出格式
        fmt_frame = ttk.Frame(main)
        fmt_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(fmt_frame, text="输出格式：").pack(side="left")
        self.fmt_md = tk.BooleanVar(value=True)
        self.fmt_txt = tk.BooleanVar(value=False)
        self.fmt_html = tk.BooleanVar(value=False)
        ttk.Checkbutton(fmt_frame, text=".md", variable=self.fmt_md).pack(side="left", padx=(4, 0))
        ttk.Checkbutton(fmt_frame, text=".txt", variable=self.fmt_txt).pack(side="left", padx=(4, 0))
        ttk.Checkbutton(fmt_frame, text=".html", variable=self.fmt_html).pack(side="left", padx=(4, 0))

        # 按钮行
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill="x", pady=(0, 8))
        self.start_btn = ttk.Button(btn_frame, text="开始处理", command=self.start)
        self.start_btn.pack(side="left", padx=(0, 8))
        self.open_btn = ttk.Button(btn_frame, text="打开输出目录", command=self.open_output)
        self.open_btn.pack(side="left")

        # 进度条
        self.progress = ttk.Progressbar(main, mode="indeterminate")

        # 日志区域
        log_frame = ttk.Frame(main)
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.log = tk.Text(log_frame, height=10, wrap="word", state="disabled",
                           font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._process = None

    def log_append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self):
        text = self.url_text.get("1.0", "end").strip()
        urls = [u.strip() for u in text.split("\n") if u.strip()]
        if not urls:
            messagebox.showwarning("提示", "请输入视频链接")
            return

        self.start_btn.configure(state="disabled")
        self.progress.pack(fill="x", pady=(0, 8))
        self.progress.start()
        self.log_append(f"共 {len(urls)} 个链接，开始处理...")

        thread = threading.Thread(target=self._run_pipeline, args=(urls,), daemon=True)
        thread.start()

    def _run_pipeline(self, urls):
        try:
            python = get_python()
            formats = []
            if self.fmt_md.get(): formats.append("md")
            if self.fmt_txt.get(): formats.append("txt")
            if self.fmt_html.get(): formats.append("html")

            for i, url in enumerate(urls):
                self.log_append(f"\n[{i+1}/{len(urls)}] {url}")

                cmd = [python, str(PROJECT_ROOT / "main.py"), url]
                if self.playlist_var.get():
                    cmd.append("--playlist")
                cmd.extend(["--summary-style", self.style_var.get()])
                cmd.extend(["--output-formats", ",".join(formats)])

                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=str(PROJECT_ROOT),
                )

                for line in self._process.stdout:
                    line = line.strip()
                    if line:
                        self.log_append(line)

                self._process.wait()
                if self._process.returncode != 0:
                    self.log_append(f"  [跳过] 处理出错")

            self.log_append("\n全部完成!")

        except Exception as e:
            self.log_append(f"异常: {e}")
        finally:
            self.root.after(0, self._done)

    def _done(self):
        self.progress.stop()
        self.progress.pack_forget()
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
