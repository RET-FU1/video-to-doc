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
        folder: Path = video_path.parent

        # 向后兼容：支持旧的 summary_style (字符串) 和新的 summary_styles (列表)
        style_cfg = self.config.get("summarizer", {})
        style_val = style_cfg.get("summary_styles") or style_cfg.get("summary_style", "auto")
        styles: List[str] = style_val if isinstance(style_val, list) else [style_val]

        first_fmt: str = formats[0] if formats else "md"
        _style_cn = {"auto": "全面总结", "knowledge_points": "知识点", "steps": "操作步骤",
                     "core_ideas": "核心观点", "expert": "专家深度", "custom": "自定义"}
        done_marker = folder / (f"{video_path.stem}.{first_fmt}" if self._skip_summary
                                else f"{_style_cn.get(styles[0], styles[0])}-{video_path.stem}.{first_fmt}")
        if done_marker.exists() and get_state(folder) == "done":
            logger.info("已完成，跳过")
            return

        transcript_raw: str = self._get_transcript(video_path, meta)

        # 注入章节标题（YouTube/B站视频的章节标记）
        chapters = meta.get("_chapters") or []
        if chapters:
            transcript_raw = self._inject_chapters(transcript_raw, folder / f"{video_path.stem}_segments.json", chapters)

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
        segments_path = folder / f"{video_path.stem}_segments.json"
        text_to_polish = self._add_timestamps(text_to_polish, segments_path)
        transcript_md: str
        try:
            multi_speaker: bool = self.config.get("summarizer", {}).get("multi_speaker", False)
            if multi_speaker:
                transcript_md = self._polish_multispeaker_transcript(text_to_polish)
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
            logger.info("总结中（%d 种风格）...", len(styles))
            _style_names = {
                "auto": "全面总结", "knowledge_points": "知识点", "steps": "操作步骤",
                "core_ideas": "核心观点", "expert": "专家深度", "custom": "自定义",
            }
            for style in styles:
                logger.info("  风格: %s", style)
                summary_text: str = self.summarizer.summarize(transcript_md, meta, style=style)
                style_cn = _style_names.get(style, style)
                save_formats(summary_text, folder / f"{style_cn}-{video_path.stem}", formats, meta=meta)
            set_state(folder, "done")

        # SRT 字幕（可选）
        if self._srt:
            logger.info("生成字幕...")
            self._generate_srt(folder, video_path.stem, translated_raw or transcript_raw)

        # 清理中间文件
        stem = video_path.stem
        if "txt" not in formats:
            (folder / f"{stem}.txt").unlink(missing_ok=True)
        (folder / f"{stem}_segments.json").unlink(missing_ok=True)
        (folder / f"{stem}_subtitle.srt").unlink(missing_ok=True)
        (folder / f"{stem}_subtitle.vtt").unlink(missing_ok=True)
        (folder / f"{stem}_subtitle_info.json").unlink(missing_ok=True)
        if translated_raw:
            zh_path = folder / f"{stem}_zh.txt"
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

    @staticmethod
    def _inject_chapters(text: str, segments_path: Path, chapters: list) -> str:
        """将视频章节标题注入到转写文本中，按时间戳定位"""
        if not chapters or not segments_path.exists():
            return text
        import json
        segments = json.loads(segments_path.read_text(encoding="utf-8"))
        lines = text.split("\n")

        # 每个章节找最近的段落位置
        injections = []  # [(line_index, title)]
        seg_idx = 0
        for ch in chapters:
            ch_start = ch["start"]
            # 找到第一个 start >= ch_start 的段落
            while seg_idx < len(segments) and segments[seg_idx]["start"] < ch_start:
                seg_idx += 1
            if seg_idx < len(segments) and seg_idx < len(lines):
                injections.append((seg_idx, ch["title"]))

        # 从后往前插入（避免索引偏移）
        for idx, title in reversed(injections):
            lines.insert(idx, f"## {title}")

        return "\n".join(lines)

    def _polish_chunk(self, text: str) -> str:
        try:
            return self.summarizer.polish(text)
        except Exception as e:
            logger.warning("抛光失败，使用原文: %s", e)
            return text

    def _polish_multispeaker_transcript(self, raw_text: str) -> str:
        """用 LLM 为转写文本做说话人识别 + 标点分段（并行处理 + 重叠上下文）"""
        max_input_chars: int = 5000
        overlap: int = 300

        if len(raw_text) <= max_input_chars:
            return self._polish_multispeaker_chunk(raw_text)

        chunks = split_text_with_overlap(raw_text, max_input_chars, overlap, sep="\n")
        polished: List[Optional[str]] = [None] * len(chunks)

        max_workers = min(4, len(chunks))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._polish_multispeaker_chunk, chunk): i
                for i, chunk in enumerate(chunks)
            }
            for future in as_completed(futures):
                i = futures[future]
                try:
                    polished[i] = future.result()
                except Exception as e:
                    logger.warning("多说话人抛光分段 %d 失败，使用原文: %s", i + 1, e)
                    polished[i] = chunks[i]

        # 合并：每块开头包含上一块的末尾作为上下文（重叠区域），需去除
        result: str = polished[0] or ""
        for i in range(1, len(polished)):
            chunk = polished[i] or ""
            parts = chunk.split("\n\n", 1)
            if len(parts) > 1:
                result += "\n\n" + parts[1]
            elif "\n" in chunk:
                lines = chunk.split("\n", 1)
                result += "\n\n" + lines[1].lstrip() if len(lines) > 1 else ""
            else:
                result += "\n\n" + chunk

        return result

    def _polish_multispeaker_chunk(self, text: str) -> str:
        try:
            return self.summarizer.polish_multispeaker(text)
        except Exception as e:
            logger.warning("多说话人抛光失败，使用原文: %s", e)
            return text

    def _translate_transcript(self, raw_text: str) -> str:
        """逐行翻译转写文本，保持行结构以对齐 SRT 时间戳"""
        translator = self._get_translator()
        return translator.translate(raw_text)

    def _generate_srt(self, folder: Path, stem: str, text: str) -> None:
        """从段落时间戳和文本生成 SRT 字幕文件"""
        from subtitle import generate_srt

        segments_path = folder / f"{stem}_segments.json"
        if not segments_path.exists():
            logger.warning("无段落时间戳数据，跳过字幕生成")
            return

        plain_lines = [
            line.strip() for line in text.split("\n")
            if line.strip() and not line.startswith("#")
        ]

        generate_srt(segments_path, plain_lines, folder / f"{stem}.srt")

    def _get_transcript(self, video_path: Path, meta: Dict[str, Any]) -> str:
        """获取转写文本：优先使用视频平台字幕，不可用则回退 Whisper"""
        folder = video_path.parent
        stem = video_path.stem
        # 支持 .srt 和 .vtt 两种字幕格式
        sub_path = folder / f"{stem}_subtitle.srt"
        if not sub_path.exists():
            sub_path = folder / f"{stem}_subtitle.vtt"

        if sub_path.exists():
            result = self._try_use_subtitles(sub_path, folder, stem, meta)
            if result is not None:
                return result
            # 字幕不达标，清理并回退（两种格式都清理）
            (folder / f"{stem}_subtitle.srt").unlink(missing_ok=True)
            (folder / f"{stem}_subtitle.vtt").unlink(missing_ok=True)
            (folder / f"{stem}_subtitle_info.json").unlink(missing_ok=True)

        # 回退 Whisper 转写
        logger.info("转写中...")
        transcript_path = self.transcriber.transcribe(video_path, folder)
        return transcript_path.read_text(encoding="utf-8")

    def _try_use_subtitles(self, sub_path: Path, folder: Path, stem: str,
                            meta: Dict[str, Any]) -> Optional[str]:
        """尝试使用下载的字幕。达标则写转写文件并返回文本，不达标返回 None。"""
        import json as _json
        from subtitle_extractor import parse_subtitle_file, assess_quality, write_transcript_output

        info_path = folder / f"{stem}_subtitle_info.json"
        if not info_path.exists():
            return None

        sub_info = _json.loads(info_path.read_text(encoding="utf-8"))

        segments = parse_subtitle_file(sub_path)
        if not segments:
            logger.warning("字幕文件解析为空，回退 Whisper")
            return None

        sub_config = self.config.get("subtitles", {})
        if not sub_config.get("enabled", False):
            return None

        duration = meta.get("duration", 0)
        expected_lang = self.config.get("whisper", {}).get("language", "auto")
        auto_cfg = sub_config.get("auto_subtitle", {})
        quality = assess_quality(
            segments, duration, sub_info["source"], expected_lang,
            min_coverage=auto_cfg.get("min_coverage", 0.50),
            max_noise_ratio=auto_cfg.get("max_noise_ratio", 0.10),
        )

        if not quality.is_acceptable:
            logger.info("字幕质量不达标（%s），回退 Whisper", ", ".join(quality.details[1:]))
            return None

        logger.info("使用视频自带字幕（%s/%s），%s，跳过 Whisper 转写",
                   sub_info["source"], sub_info["language"], quality.details[0])
        return write_transcript_output(segments, stem, folder)

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
                # 总结文件：文件名以风格名开头（而非视频标题）
                # 判断：stem 不是纯标题格式（标题来自 sanitize_filename，不含中划线前缀）
                if file.stem.startswith(("全面总结-", "知识点-", "操作步骤-", "核心观点-", "专家深度-", "自定义-")):
                    shutil.copy2(file, summary_dir / file.name)
                    count_s += 1
                else:
                    shutil.copy2(file, transcript_dir / file.name)
                    count_t += 1

        if count_t > 0:
            logger.info("转写汇总: %d 个文件 → %s", count_t, transcript_dir)
        if count_s > 0:
            logger.info("总结汇总: %d 个文件 → %s", count_s, summary_dir)
