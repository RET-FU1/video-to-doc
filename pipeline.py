"""
流水线编排器 — 串联下载 → 转写 → 总结
支持断点续跑：每步完成后记录状态，失败可从中断处继续。
"""
import json
import sys
from pathlib import Path
from downloader import Downloader
from transcriber import Transcriber
from summarizer import create_summarizer
from format_converter import save_formats


class Pipeline:
    def __init__(self, config):
        self.config = config
        self.output_root = Path(config["output_dir"])
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.downloader = Downloader(config, self.output_root)
        self.transcriber = Transcriber(config, self.output_root)
        self.summarizer = create_summarizer(config)

    def process(self, url, is_playlist=False):
        """处理单个URL（可能包含多个视频）"""
        if is_playlist:
            return self._process_playlist(url)

        return self._process_single(url)

    def _process_single(self, url, output_subdir=None):
        """处理单个视频"""
        formats = self.config.get("summarizer", {}).get("output_formats", ["md"])

        # Step 1: 下载
        print("\n[1/3] 下载视频...")
        video_path, meta = self.downloader.download(url, output_subdir)
        folder = video_path.parent

        # Step 2: 转写
        print("\n[2/3] 转写音频...")
        transcript_path = self.transcriber.transcribe(video_path, folder)
        # 转写文档多格式输出
        transcript_md = transcript_path.read_text(encoding="utf-8")
        saved = save_formats(transcript_md, folder / "video", formats)
        for p in saved:
            print(f"  已保存: {p.name}")

        # Step 3: 总结
        print("\n[3/3] AI 总结...")
        summary_base = folder / "summary"

        if (folder / "summary.md").exists() and self._read_state(folder) == "done":
            print(f"  已总结")
        else:
            style = self.config.get("summarizer", {}).get("summary_style", "auto")
            summary = self.summarizer.summarize(transcript_md, meta, style=style)
            (folder / "summary.md").write_text(summary, encoding="utf-8")
            self._set_state(folder, "done")

        # 总结多格式输出
        summary_md = (folder / "summary.md").read_text(encoding="utf-8")
        saved = save_formats(summary_md, summary_base, formats)
        for p in saved:
            print(f"  已保存: {p.name}")

        print(f"\n完成! 输出目录: {folder}")
        return folder

    def _process_playlist(self, url):
        """处理播放列表"""
        print("\n[播放列表模式]")
        results = self.downloader.download_playlist(url)

        for video_path, meta in results:
            print(f"\n--- {meta.get('title', video_path.stem)} ---")
            try:
                folder = video_path.parent
                transcript_path = self.transcriber.transcribe(video_path, folder)
                text = transcript_path.read_text(encoding="utf-8")
                style = self.config.get("summarizer", {}).get("summary_style", "auto")
                summary = self.summarizer.summarize(text, meta, style=style)
                (folder / "summary.md").write_text(summary, encoding="utf-8")
                self._set_state(folder, "done")
                print(f"  完成: {meta.get('title', '')}")
            except Exception as e:
                print(f"  [FAIL] {e}")
                continue

        print(f"\n全部完成! 输出目录: {self.output_root}")

    @staticmethod
    def _read_state(folder):
        state_file = folder / ".pipeline_state"
        if not state_file.exists():
            return ""
        return state_file.read_text().strip()

    @staticmethod
    def _set_state(folder, state):
        (folder / ".pipeline_state").write_text(state)
