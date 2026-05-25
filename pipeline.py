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
    def __init__(self, config: Dict[str, Any], translate: bool = False,
                 srt: bool = False, skip_summary: bool = False) -> None:
        self.config: Dict[str, Any] = config
        self.output_root: Path = Path(config["output_dir"])
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.downloader: Downloader = Downloader(config, self.output_root)
        self.transcriber: Transcriber = Transcriber(config, self.output_root)
        self.summarizer = create_summarizer(config)
        self._translate: bool = translate
        self._srt: bool = srt
        self._skip_summary: bool = skip_summary
        self._translator: Optional[Any] = None

    def _get_translator(self):
        if self._translator is None:
            from translator import Translator
            t_config = self.config.get("translation", {})
            self._translator = Translator(
                client=self.summarizer.client,
                model=t_config.get("model", "") or self.summarizer.model,
                target_lang=t_config.get("target_lang", "zh"),
            )
        return self._translator

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
        """单个视频/音频的转写→[翻译]→抛光→总结→[字幕]"""
        formats: List[str] = self.config.get("summarizer", {}).get("output_formats", ["md"])
        style: str = self.config.get("summarizer", {}).get("summary_style", "auto")
        folder: Path = video_path.parent

        first_fmt: str = formats[0] if formats else "md"
        done_marker = folder / (f"{video_path.stem}.{first_fmt}" if self._skip_summary
                                else f"{video_path.stem}-总结.{first_fmt}")
        if done_marker.exists() and get_state(folder) == "done":
            logger.info("已完成，跳过")
            return

        logger.info("转写中...")
        transcript_path = self.transcriber.transcribe(video_path, folder)
        transcript_raw: str = transcript_path.read_text(encoding="utf-8")

        # 翻译（可选，在抛光之前）
        translated_raw: Optional[str] = None
        if self._translate:
            logger.info("翻译中...")
            translated_raw = self._translate_transcript(transcript_raw)
            zh_path = folder / f"{video_path.stem}_zh.txt"
            zh_path.write_text(translated_raw, encoding="utf-8")
            logger.info("已保存汉化文档: %s", zh_path.name)

        # 抛光
        logger.info("后处理（标点 + 分段）...")
        text_to_polish = translated_raw or transcript_raw
        if "## SPEAKER_" not in text_to_polish:
            segments_path = folder / f"{video_path.stem}_segments.json"
            text_to_polish = self._add_timestamps(text_to_polish, segments_path)
        transcript_md: str
        try:
            if "## SPEAKER_" in text_to_polish:
                transcript_md = self._polish_diarized_transcript(text_to_polish)
            else:
                transcript_md = self._polish_transcript(text_to_polish)
        except Exception as e:
            logger.warning("抛光失败，使用原始文本: %s", e)
            transcript_md = text_to_polish
        save_formats(transcript_md, folder / video_path.stem, formats, meta=meta)

        if self._skip_summary:
            logger.info("跳过总结")
            set_state(folder, "done")
        else:
            logger.info("总结中...")
            summary_text: str = self.summarizer.summarize(transcript_md, meta, style=style)
            set_state(folder, "done")
            save_formats(summary_text, folder / f"{video_path.stem}-总结", formats, meta=meta)

        # SRT 字幕（可选）
        if self._srt:
            logger.info("生成字幕...")
            self._generate_srt(folder, video_path.stem, translated_raw or transcript_raw)

        # 清理中间文件
        transcript_path.unlink(missing_ok=True)
        (folder / ".pipeline_state").unlink(missing_ok=True)
        (folder / f"{video_path.stem}_segments.json").unlink(missing_ok=True)
        if translated_raw:
            zh_path = folder / f"{video_path.stem}_zh.txt"
            zh_path.unlink(missing_ok=True)

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

        # 合并：每块开头包含上一块的末尾作为上下文（重叠区域），需去除
        result: str = polished[0] or ""
        for i in range(1, len(polished)):
            chunk = polished[i] or ""
            # 优先按段落边界去除重叠（LLM 通常以空行分隔段落）
            parts = chunk.split("\n\n", 1)
            if len(parts) > 1:
                result += "\n\n" + parts[1]
            elif "\n" in chunk:
                # 无段落分隔时回退按首行去除
                lines = chunk.split("\n", 1)
                result += "\n\n" + lines[1].lstrip() if len(lines) > 1 else ""
            else:
                result += "\n\n" + chunk

        return result

    @staticmethod
    def _add_timestamps(text: str, segments_path: Path) -> str:
        """给文本行加上时间戳前缀，帮助 LLM 判断段落边界

        时间间隔大 → 话题转折 → 该分段；连续密集 → 同一段落。
        """
        if not segments_path.exists():
            return text
        import json
        segments = json.loads(segments_path.read_text(encoding="utf-8"))
        raw_lines = [l for l in text.split("\n") if l.strip() and not l.startswith("#")]

        result = []
        for i, line in enumerate(raw_lines):
            t = segments[i]["start"] if i < len(segments) else 0
            m, s = divmod(int(t), 60)
            result.append(f"[{m:02d}:{s:02d}] {line}")
        return "\n".join(result)

    def _polish_chunk(self, text: str) -> str:
        try:
            return self.summarizer.polish(text)
        except Exception as e:
            logger.warning("抛光失败，使用原文: %s", e)
            return text

    def _polish_diarized_transcript(self, raw_text: str) -> str:
        """对带说话人标签的转写逐段抛光，避免跨说话人合并段落"""
        parts = raw_text.split('\n\n## SPEAKER_')
        title = parts[0].strip()

        if len(parts) < 2:
            return self._polish_transcript(raw_text)

        max_chars = 5000
        result_parts = []

        for part in parts[1:]:
            line_break = part.find('\n')
            if line_break == -1:
                continue
            header = part[:line_break].strip()
            body = part[line_break:].strip()

            if len(body) < 20:
                if body and body[-1] not in '。！？.!?、':
                    body += '。'
            elif len(body) <= max_chars:
                try:
                    body = self._polish_chunk(body)
                except Exception as e:
                    logger.warning("说话人 SPEAKER_%s 抛光失败: %s", header, e)
            else:
                try:
                    body = self._polish_transcript(body)
                except Exception as e:
                    logger.warning("说话人 SPEAKER_%s 长文本抛光失败: %s", header, e)

            result_parts.append(f"## SPEAKER_{header}\n\n{body}")

        return f"{title}\n\n" + "\n\n".join(result_parts)

    def _translate_transcript(self, raw_text: str) -> str:
        """逐行翻译转写文本，保持行结构以对齐 SRT 时间戳"""
        if "## SPEAKER_" in raw_text:
            return self._translate_diarized(raw_text)
        translator = self._get_translator()
        return translator.translate(raw_text)

    def _translate_diarized(self, raw_text: str) -> str:
        """翻译 diarized 文本：保留 speaker 头不变，只翻译正文段落"""
        parts = raw_text.split('\n\n## SPEAKER_')
        title = parts[0].strip()

        if len(parts) < 2:
            return self._get_translator().translate(raw_text)

        translator = self._get_translator()
        result_parts = []

        for part in parts[1:]:
            line_break = part.find('\n')
            if line_break == -1:
                continue
            header = part[:line_break].strip()
            body = part[line_break:].strip()
            if body:
                body = translator.translate(body)
            result_parts.append(f"## SPEAKER_{header}\n\n{body}")

        return f"{title}\n\n" + "\n\n".join(result_parts)

    def _generate_srt(self, folder: Path, stem: str, text: str) -> None:
        """从段落时间戳和文本生成 SRT 字幕文件"""
        from subtitle import generate_srt

        segments_path = folder / f"{stem}_segments.json"
        if not segments_path.exists():
            logger.warning("无段落时间戳数据，跳过字幕生成")
            return

        # 说话人分离输出会合并同说话人段落，文本行数 < segments JSON 条目数，
        # 直接从 JSON 提取文本确保与时间戳一一对齐
        if "## SPEAKER_" in text:
            import json
            segments = json.loads(segments_path.read_text(encoding="utf-8"))
            plain_lines = [seg.get("text", "").strip() for seg in segments]
        else:
            plain_lines = [
                line.strip() for line in text.split("\n")
                if line.strip() and not line.startswith("#")
            ]

        generate_srt(segments_path, plain_lines, folder / f"{stem}.srt")

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
