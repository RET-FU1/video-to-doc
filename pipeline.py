"""
流水线编排器 — 串联下载 → 转写 → 总结
支持断点续跑：每步完成后记录状态，失败可从中断处继续。
"""
from pathlib import Path
from downloader import Downloader
from transcriber import Transcriber
from summarizer import create_summarizer
from format_converter import save_formats
from utils import get_state, set_state, split_text


class Pipeline:
    def __init__(self, config):
        self.config = config
        self.output_root = Path(config["output_dir"])
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.downloader = Downloader(config, self.output_root)
        self.transcriber = Transcriber(config, self.output_root)
        self.summarizer = create_summarizer(config)

    def process(self, url, is_playlist=False):
        if is_playlist:
            return self._process_playlist(url)
        return self._process_single(url)

    def download_only(self, url, is_playlist=False):
        """仅下载，不做转写和总结"""
        if is_playlist:
            print("\n[仅下载 - 播放列表]")
            results = self.downloader.download_playlist(url)
            print(f"\n下载完成! 共 {len(results)} 个视频")
            for video_path, meta in results:
                print(f"  {video_path}")
            return

        print("\n[仅下载]")
        video_path, meta = self.downloader.download(url)
        print(f"\n下载完成! {video_path}")

    def _process_single(self, url, output_subdir=None):
        print("\n[1/3] 下载视频...")
        video_path, meta = self.downloader.download(url, output_subdir)
        self._process_one(video_path, meta)
        return video_path.parent

    def _process_one(self, video_path, meta):
        """单个视频/音频的转写→抛光→总结"""
        formats = self.config.get("summarizer", {}).get("output_formats", ["md"])
        style = self.config.get("summarizer", {}).get("summary_style", "auto")
        folder = video_path.parent

        if (folder / "summary.md").exists() and get_state(folder) == "done":
            print(f"  已完成，跳过")
            return

        print("  转写中...")
        transcript_path = self.transcriber.transcribe(video_path, folder)
        transcript_raw = transcript_path.read_text(encoding="utf-8")

        print("  后处理（标点 + 分段）...")
        transcript_md = self._polish_transcript(transcript_raw)
        transcript_path.write_text(transcript_md, encoding="utf-8")
        save_formats(transcript_md, folder / "video", formats)

        print("  总结中...")
        summary_text = self.summarizer.summarize(transcript_md, meta, style=style)
        set_state(folder, "done")
        save_formats(summary_text, folder / "summary", formats)

    def _polish_transcript(self, raw_text):
        """用 LLM 为转写文本添加标点并按语义分段"""
        max_input_chars = 5000

        if len(raw_text) <= max_input_chars:
            return self._polish_chunk(raw_text)

        # 长文本分段抛光
        chunks = split_text(raw_text, max_input_chars, sep="\n")
        polished = []
        for i, chunk in enumerate(chunks):
            print(f"    抛光分段 [{i+1}/{len(chunks)}]...")
            polished.append(self._polish_chunk(chunk))
        return "\n\n".join(polished)

    def _polish_chunk(self, text):
        prompt = (
            "你是一个中文文本格式化助手。请对以下视频转写文本做两件事：\n"
            "1. 添加合适的标点符号（逗号、句号、问号等）\n"
            "2. 按语义将文本拆分为合适的段落（用空行分隔）\n\n"
            "规则：\n"
            "- 只添加标点和段落分隔，不要修改任何文字内容\n"
            "- 不要增删改任何词语，保持原文字不变\n"
            "- 段落拆分按话题/语义边界，每段5-12句话为宜，避免过碎\n\n"
            "直接输出格式化后的文本，不要任何解释。\n\n"
            f"以下是转写文本：\n\n{text}"
        )

        response = self.summarizer.client.chat.completions.create(
            model=self.summarizer.model,
            max_tokens=16384,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content

    def _process_playlist(self, url):
        print("\n[播放列表模式]")
        results = self.downloader.download_playlist(url)

        for video_path, meta in results:
            print(f"\n--- {meta.get('title', video_path.stem)} ---")
            try:
                self._process_one(video_path, meta)
            except Exception as e:
                print(f"  [FAIL] {e}")
                continue

        print(f"\n全部完成! 输出目录: {self.output_root}")

    def process_folder(self, folder_path):
        """批量处理文件夹内所有视频/音频"""
        folder = Path(folder_path).resolve()
        if not folder.is_dir():
            raise NotADirectoryError(f"路径不存在或不是文件夹: {folder_path}")

        video_exts = {".mp4", ".mkv", ".webm", ".flv", ".avi", ".mov",
                       ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".opus", ".wma"}
        files = sorted(
            f for f in folder.iterdir()
            if f.is_file() and f.suffix.lower() in video_exts
        )
        if not files:
            print(f"  文件夹内未找到视频/音频文件: {folder}")
            return

        print(f"\n[文件夹模式] 共发现 {len(files)} 个文件")

        for i, file_path in enumerate(files):
            print(f"\n--- [{i+1}/{len(files)}] {file_path.stem} ---")
            try:
                video_path, meta = self.downloader._import_local(str(file_path))
            except Exception as e:
                print(f"  [FAIL] 导入失败: {e}")
                continue

            try:
                self._process_one(video_path, meta)
            except Exception as e:
                print(f"  [FAIL] {e}")
                continue

        print(f"\n全部完成! 输出目录: {self.output_root}")
