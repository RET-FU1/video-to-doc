"""
流水线编排器 — 串联下载 → 转写 → 总结
支持断点续跑：每步完成后记录状态，失败可从中断处继续。
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from downloader import Downloader
from transcriber import Transcriber
from summarizer import create_summarizer
from format_converter import save_formats
from utils import get_state, set_state, split_text_with_overlap, MEDIA_EXTS, sanitize_filename

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config: Dict[str, Any] = config
        self.output_root: Path = Path(config["output_dir"])
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.downloader: Downloader = Downloader(config, self.output_root)
        self.transcriber: Transcriber = Transcriber(config, self.output_root)
        self.summarizer = create_summarizer(config)

    def process(self, url: str, is_playlist: bool = False) -> Path:
        if is_playlist:
            return self._process_playlist(url)
        return self._process_single(url)

    def download_only(self, url: str, is_playlist: bool = False) -> None:
        """仅下载，不做转写和总结"""
        if is_playlist:
            logger.info("[仅下载 - 播放列表]")
            results = self.downloader.download_playlist(url)
            logger.info("下载完成! 共 %d 个视频", len(results))
            for video_path, meta in results:
                logger.info("  %s", video_path)
            return

        logger.info("[仅下载]")
        video_path, meta = self.downloader.download(url)
        logger.info("下载完成! %s", video_path)

    def _process_single(self, url: str, output_subdir: Optional[str] = None) -> Path:
        logger.info("[1/3] 下载视频...")
        video_path, meta = self.downloader.download(url, output_subdir)
        self._process_one(video_path, meta)
        return video_path.parent

    def _process_one(self, video_path: Path, meta: Dict[str, Any]) -> None:
        """单个视频/音频的转写→抛光→总结"""
        formats: List[str] = self.config.get("summarizer", {}).get("output_formats", ["md"])
        style: str = self.config.get("summarizer", {}).get("summary_style", "auto")
        folder: Path = video_path.parent

        first_fmt: str = formats[0] if formats else "md"
        if (folder / f"{video_path.stem}-总结.{first_fmt}").exists() and get_state(folder) == "done":
            logger.info("已完成，跳过")
            return

        logger.info("转写中...")
        transcript_path = self.transcriber.transcribe(video_path, folder)
        transcript_raw: str = transcript_path.read_text(encoding="utf-8")

        logger.info("后处理（标点 + 分段）...")
        transcript_md: str
        try:
            transcript_md = self._polish_transcript(transcript_raw)
        except Exception as e:
            logger.warning("抛光失败，使用原始转写文本: %s", e)
            transcript_md = transcript_raw
        save_formats(transcript_md, folder / video_path.stem, formats, meta=meta)

        logger.info("总结中...")
        summary_text: str = self.summarizer.summarize(transcript_md, meta, style=style)
        set_state(folder, "done")
        save_formats(summary_text, folder / f"{video_path.stem}-总结", formats, meta=meta)

    def _polish_transcript(self, raw_text: str) -> str:
        """用 LLM 为转写文本添加标点并按语义分段（并行处理 + 重叠上下文）"""
        max_input_chars: int = 5000
        overlap: int = 300

        if len(raw_text) <= max_input_chars:
            return self._polish_chunk(raw_text)

        chunks = split_text_with_overlap(raw_text, max_input_chars, overlap, sep="\n")
        polished: List[Optional[str]] = [None] * len(chunks)

        max_workers = min(4, len(chunks))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._polish_chunk, chunk): i
                for i, chunk in enumerate(chunks)
            }
            for future in as_completed(futures):
                i = futures[future]
                try:
                    polished[i] = future.result()
                except Exception as e:
                    logger.warning("分段 %d 抛光失败，使用原文: %s", i + 1, e)
                    polished[i] = chunks[i]

        # 合并：丢弃每块（除第一块外）的第一个段落来去重重叠区域
        result: str = polished[0] or ""
        for i in range(1, len(polished)):
            chunk = polished[i] or ""
            parts = chunk.split("\n\n", 1)
            result += "\n\n" + (parts[1] if len(parts) > 1 else chunk)

        return result

    def _polish_chunk(self, text: str) -> str:
        try:
            return self.summarizer.polish(text)
        except Exception as e:
            logger.warning("抛光失败，使用原文: %s", e)
            return text

    def _process_playlist(self, url: str) -> Path:
        logger.info("[播放列表模式]")
        results: List[Tuple[Path, Dict[str, Any]]] = self.downloader.download_playlist(url)

        for video_path, meta in results:
            logger.info("--- %s ---", meta.get('title', video_path.stem))
            try:
                self._process_one(video_path, meta)
            except Exception as e:
                logger.error("[FAIL] %s", e)
                continue

        if results:
            group_dir: Path = results[0][0].parent.parent
            self._collect_outputs(group_dir)

        logger.info("全部完成! 输出目录: %s", self.output_root)
        return self.output_root

    def process_folder(self, folder_path: str) -> None:
        """批量处理文件夹内所有视频/音频"""
        folder: Path = Path(folder_path).resolve()
        if not folder.is_dir():
            raise NotADirectoryError(f"路径不存在或不是文件夹: {folder_path}")

        files: List[Path] = sorted(
            f for f in folder.iterdir()
            if f.is_file() and f.suffix.lower() in MEDIA_EXTS
        )
        if not files:
            logger.warning("文件夹内未找到视频/音频文件: %s", folder)
            return

        group_name: str = sanitize_filename(folder.name)
        logger.info("[文件夹模式] 共发现 %d 个文件 → 输出: %s", len(files), self.output_root / group_name)

        for i, file_path in enumerate(files):
            logger.info("--- [%d/%d] %s ---", i + 1, len(files), file_path.stem)
            try:
                video_path, meta = self.downloader._import_local(str(file_path), output_subdir=group_name)
            except Exception as e:
                logger.error("[FAIL] 导入失败: %s", e)
                continue

            try:
                self._process_one(video_path, meta)
            except Exception as e:
                logger.error("[FAIL] %s", e)
                continue

        self._collect_outputs(self.output_root / group_name)
        logger.info("全部完成! 输出目录: %s", self.output_root)

    def _collect_outputs(self, group_dir: Path) -> None:
        """将批量处理的所有转写和总结分别收集到汇总文件夹"""
        import shutil

        formats: List[str] = self.config.get("summarizer", {}).get("output_formats", ["md"])
        exts = set(f".{f}" for f in formats)

        transcript_dir = group_dir / "转写汇总"
        summary_dir = group_dir / "总结汇总"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        summary_dir.mkdir(parents=True, exist_ok=True)

        count_t, count_s = 0, 0
        for subdir in sorted(group_dir.iterdir()):
            if not subdir.is_dir():
                continue
            if subdir.name in ("转写汇总", "总结汇总"):
                continue

            for file in sorted(subdir.iterdir()):
                if not file.is_file() or file.suffix not in exts:
                    continue
                if file.stem.endswith("-总结"):
                    shutil.copy2(file, summary_dir / file.name)
                    count_s += 1
                else:
                    shutil.copy2(file, transcript_dir / file.name)
                    count_t += 1

        if count_t > 0:
            logger.info("转写汇总: %d 个文件 → %s", count_t, transcript_dir)
        if count_s > 0:
            logger.info("总结汇总: %d 个文件 → %s", count_s, summary_dir)
