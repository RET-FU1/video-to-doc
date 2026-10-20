"""
流水线编排器 — 串联下载 → 转写 → 总结
支持断点续跑：每步完成后记录状态，失败可从中断处继续。
"""
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from downloader import Downloader
from transcriber import Transcriber
from summarizer import create_summarizer
from format_converter import save_formats
from utils import (get_state, set_state, split_text, MEDIA_EXTS,
                   sanitize_filename, CancelledError, CancelToken)

logger = logging.getLogger(__name__)

# 总结风格 → 中文名（总结文件名前缀、断点续跑标记都依赖它，只定义一次）
STYLE_CN: Dict[str, str] = {
    "auto": "全面总结",
    "knowledge_points": "知识点",
    "steps": "操作步骤",
    "core_ideas": "核心观点",
    "expert": "专家深度",
    "custom": "自定义",
}


def _normalize_for_match(text: str) -> str:
    """去掉标点与空白，用于跨分块文本比对"""
    return re.sub(r"[\s，。、；：？！“”‘’（）《》…—\-·,.;:?!()\[\]\"'`~]", "", text)


class Pipeline:
    def __init__(self, config: Dict[str, Any], translate: bool = False,
                 srt: bool = False, skip_summary: bool = False,
                 cancel_token: Optional[CancelToken] = None) -> None:
        self.config: Dict[str, Any] = config
        self.output_root: Path = Path(config["output_dir"])
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._cancel: CancelToken = cancel_token or CancelToken()
        self.downloader: Downloader = Downloader(config, self.output_root,
                                                 cancel_token=self._cancel)
        self.transcriber: Transcriber = Transcriber(config, self.output_root,
                                                    cancel_token=self._cancel)
        self.summarizer = create_summarizer(config)
        self._translate: bool = translate
        self._srt: bool = srt
        self._skip_summary: bool = skip_summary
        self._translator: Optional[Any] = None
        # 最近一次批量处理（文件夹/播放列表）里失败的条目数，供 GUI 汇总显示
        self.last_failures: int = 0

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _check_cancel(self) -> None:
        self._cancel.check()

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

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def process(self, url: str, is_playlist: bool = False) -> Path:
        if is_playlist:
            return self._process_playlist(url)
        return self._process_single(url)

    def download_only(self, url: str, is_playlist: bool = False) -> None:
        """仅下载，不做转写和总结"""
        self._check_cancel()
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

    # ------------------------------------------------------------------
    # 单视频
    # ------------------------------------------------------------------

    def _process_single(self, url: str, output_subdir: Optional[str] = None) -> Path:
        self._check_cancel()
        logger.info("[1/3] 下载视频...")
        video_path, meta = self.downloader.download(url, output_subdir)
        self._check_cancel()
        self._process_one(video_path, meta)
        return video_path.parent

    def _process_one(self, video_path: Path, meta: Dict[str, Any]) -> None:
        """单个视频/音频的转写→[翻译]→抛光→总结→[字幕]"""
        folder: Path = video_path.parent
        stem: str = video_path.stem
        formats: List[str] = self.config.get("summarizer", {}).get("output_formats", ["md"])
        if not formats:
            raise ValueError("未选择任何输出格式（md/txt/html 至少选一个）")

        style_cfg = self.config.get("summarizer", {})
        style_val = style_cfg.get("summary_styles") or style_cfg.get("summary_style", "auto")
        styles: List[str] = style_val if isinstance(style_val, list) else [style_val]
        styles = [s for s in styles if s] or ["auto"]   # 空列表/空串一律回退 auto

        # 判断"是否已完成"必须覆盖**本次请求的全部产物**，不能只看第一个
        # 风格 + 第一个格式：
        #   上次只跑了 auto/md，这次勾了 auto+expert、md+txt —— 若只看
        #   auto/md 存在就跳过，expert 和 txt 永远不会生成（实际踩到）。
        expected: List[Path] = []
        # 勾了「生成字幕」就必须有对应的 .srt，否则删掉/上次没生成 srt 后
        # 重跑会因为"文档齐全 + state=done"直接跳过，SRT 永远不会补上。
        if self._srt:
            expected.append(folder / f"{stem}.srt")
        for style in ([""] if self._skip_summary else styles):
            for fmt in formats:
                base = f"{stem}" if self._skip_summary else f"{STYLE_CN.get(style, style)}-{stem}"
                expected.append(folder / f"{base}.{fmt}")
        missing = [p for p in expected if not p.exists()]

        if not missing and get_state(folder) == "done":
            # 不要删 .pipeline_state：删了 is_done() 会变 False，下一轮又会重来。
            # 保留状态即可让重复运行稳定跳过（删除产物文件才会真正重跑）。
            logger.info("已完成，跳过（如需重跑请删除该任务目录或 .pipeline_state）")
            return
        if missing and get_state(folder) == "done":
            logger.info("已有产物缺 %d 个（%s），继续处理以补齐",
                        len(missing), "、".join(p.name for p in missing[:3]))

        # 1) 转写（或取平台字幕）
        self._check_cancel()
        transcript_raw: str = self._get_transcript(video_path, meta)
        lines: List[str] = self._plain_lines(transcript_raw)
        segments: List[Dict[str, Any]] = self._load_segments(folder, stem)
        lines, segments = self._align_lines_to_segments(lines, segments, stem)

        # 2) 翻译（可选，在抛光之前）
        translated_lines: Optional[List[str]] = None
        if self._translate:
            self._check_cancel()
            logger.info("翻译中...")
            try:
                translated_lines = self._translate_lines(lines)
            except CancelledError:
                raise
            except (ValueError, RuntimeError) as e:
                # 行数对不齐 / API 不可用时宁可不翻译，也不能让整条任务失败，
                # 更不能把"没翻成"当成"翻译完成"（否则文档静默保持原语言）
                logger.warning("翻译失败，保留原文: %s", e)
            else:
                if translated_lines is not None:
                    logger.info("翻译完成（%d 行）", len(translated_lines))

        # 3) 抛光（标点 + 分段）
        self._check_cancel()
        logger.info("后处理（标点 + 分段）...")
        source_lines: List[str] = translated_lines or lines
        chapters: List[Dict[str, Any]] = meta.get("_chapters") or []
        polish_input = self._build_polish_input(source_lines, segments, chapters)
        try:
            multi_speaker: bool = self.config.get("summarizer", {}).get("multi_speaker", False)
            polished: str = self._polish_transcript(polish_input, multispeaker=multi_speaker)
            polished = self._verify_polish(polish_input, polished)
        except CancelledError:
            raise
        except Exception as e:
            logger.warning("抛光失败，使用原始文本: %s", e)
            polished = self._strip_time_prefix(polish_input)

        # 章节兜底：LLM 常把「## 标题」当噪声删掉而不影响长度/相似度，
        # 守卫发现不了。这里对比章节标题是否还在，缺了就在正文里补回。
        polished = self._restore_missing_chapters(polished, chapters)

        title: str = meta.get("title") or stem
        transcript_md: str = f"# {title}\n\n{polished.strip()}\n"
        save_formats(transcript_md, folder / stem, formats, meta=meta)

        # 4) 字幕（可选）— 用原始段落时间戳，不依赖抛光后的行结构
        if self._srt:
            self._check_cancel()
            logger.info("生成字幕...")
            self._generate_srt(folder, stem, segments, source_lines)

        # 5) 总结
        if self._skip_summary:
            logger.info("跳过总结")
        else:
            logger.info("总结中（%d 种风格）...", len(styles))
            for style in styles:
                self._check_cancel()
                logger.info("  风格: %s", style)
                summary_text: str = self.summarizer.summarize(transcript_md, meta, style=style)
                style_cn = STYLE_CN.get(style, style)
                save_formats(summary_text, folder / f"{style_cn}-{stem}", formats, meta=meta)

        # 6) 清理中间文件（先清理再标 done：清理若失败，下次重跑还能补做）
        self._cleanup(folder, stem, formats, translated=translated_lines is not None)
        set_state(folder, "done")

    @staticmethod
    def _cleanup(folder: Path, stem: str, formats: List[str], translated: bool) -> None:
        """删除可再生的中间文件；保留 _segments.json（重跑字幕/质检要用）

        单文件删除失败不应该让整个任务失败（Windows 上文件被预览/杀软占用很常见）。
        """
        names: List[str] = []
        if "txt" not in formats:
            names.append(f"{stem}.txt")
        names += [f"{stem}_subtitle.srt", f"{stem}_subtitle.vtt", f"{stem}_subtitle_info.json"]
        if translated:
            names.append(f"{stem}_zh.txt")
        for name in names:
            try:
                (folder / name).unlink(missing_ok=True)
            except OSError as e:
                logger.debug("清理临时文件失败（可忽略）: %s (%s)", name, e)

    # ------------------------------------------------------------------
    # 转写 / 对齐
    # ------------------------------------------------------------------

    def _get_transcript(self, video_path: Path, meta: Dict[str, Any]) -> str:
        """获取转写文本：优先使用视频平台字幕，不可用则回退 Whisper"""
        folder = video_path.parent
        stem = video_path.stem
        sub_path = folder / f"{stem}_subtitle.srt"
        if not sub_path.exists():
            sub_path = folder / f"{stem}_subtitle.vtt"

        if sub_path.exists():
            result = self._try_use_subtitles(sub_path, folder, stem, meta)
            if result is not None:
                return result
            for name in (f"{stem}_subtitle.srt", f"{stem}_subtitle.vtt",
                         f"{stem}_subtitle_info.json"):
                (folder / name).unlink(missing_ok=True)

        logger.info("转写中...")
        transcript_path = self.transcriber.transcribe(video_path, folder)
        return transcript_path.read_text(encoding="utf-8")

    @staticmethod
    def _plain_lines(text: str) -> List[str]:
        """取正文行：去掉标题行、空行、以及抛光输入里的时间戳前缀

        时间戳分钟数可能是 1-3 位数（100 分钟以上的视频），所以正则用 \\d{1,3}。
        """
        out: List[str] = []
        for line in text.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            line = re.sub(r"^\[\d{1,3}:\d{2}\]\s*", "", line)
            if line:
                out.append(line)
        return out

    @staticmethod
    def _load_segments(folder: Path, stem: str) -> List[Dict[str, Any]]:
        path = folder / f"{stem}_segments.json"
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("读取段落时间戳失败: %s", e)
            return []

    @staticmethod
    def _align_lines_to_segments(lines: List[str], segments: List[Dict[str, Any]],
                                 stem: str) -> Tuple[List[str], List[Dict[str, Any]]]:
        """把文本行与段落时间戳对齐。

        正常情况二者严格 1:1（两者都来自同一份逐段数据）。历史 bug 的根源是
        "假定 1:1 却从不校验"，一旦翻译丢行，后续所有时间戳就整体错位。
        这里显式对齐：多出的行用最后一段的时间，缺时间戳的行给 None（不猜）。
        """
        if not segments:
            return lines, []
        if len(lines) == len(segments):
            return lines, segments
        logger.warning(
            "文本行数(%d)与段落时间戳数(%d)不一致，按较短者对齐；字幕可能不完整",
            len(lines), len(segments),
        )
        n = min(len(lines), len(segments))
        return lines[:n], segments[:n]

    @staticmethod
    def _build_polish_input(lines: List[str], segments: List[Dict[str, Any]],
                            chapters: List[Dict[str, Any]]) -> str:
        """给每行加 [MM:SS] 时间戳；章节以 [MM:SS] ## 标题 形式插入（供 LLM 分段参考）

        超过 100 分钟的视频分钟数会是三位数（[100:23]），后续剥离必须用 \\d{1,3}。
        """
        stamps: List[str] = []
        for i in range(len(lines)):
            t = segments[i]["start"] if i < len(segments) else (segments[-1]["start"] if segments else 0)
            m, s = divmod(int(t), 60)
            stamps.append(f"{m:02d}:{s:02d}")

        heading_at: Dict[int, str] = {}
        if chapters and segments:
            for ch in chapters:
                idx = Pipeline._chapter_line_index(ch.get("start", 0), segments)
                if idx is not None and idx < len(lines):
                    heading_at.setdefault(idx, str(ch.get("title") or "").strip())

        out: List[str] = []
        for i, line in enumerate(lines):
            head = heading_at.get(i)
            if head:
                out.append(f"[{stamps[i]}] ## {head}")
            out.append(f"[{stamps[i]}] {line}")
        return "\n".join(out)

    @staticmethod
    def _chapter_line_index(start_sec: float, segments: List[Dict[str, Any]]) -> Optional[int]:
        """章节起点 → 第一个不早于它的段落下标"""
        for i, seg in enumerate(segments):
            if seg.get("start", 0) >= start_sec:
                return i
        return None

    @staticmethod
    def _strip_time_prefix(text: str) -> str:
        lines = []
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            line = re.sub(r"^\[\d{1,3}:\d{2}\]\s*", "", line)
            if line:
                lines.append(line)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 抛光
    # ------------------------------------------------------------------

    def _polish_transcript(self, raw_text: str, multispeaker: bool = False) -> str:
        """并行抛光：分块（不重叠）→ LLM 处理 → 去边界重复后拼接。

        历史实现给每块复制上一块末尾 300 字作为"上下文"，合并时按段落砍掉第一段。
        实测该策略会静默丢掉 1.5% 正文（LLM 常把上下文与新内容并进同一段）。
        现在改为：分块不重叠，上下文单独传给 LLM 并要求"不要输出"，
        合并时再用确定性前缀去重兜底 —— 无论 LLM 是否回显上下文都不会丢内容。
        """
        max_input_chars: int = 4000
        label = "多说话人抛光" if multispeaker else "抛光"

        if len(raw_text) <= max_input_chars:
            return self._polish_chunk(raw_text, multispeaker=multispeaker)

        chunks = split_text(raw_text, max_input_chars, sep="\n")
        polished: List[Optional[str]] = [None] * len(chunks)
        context_chars = 200

        max_workers = min(4, len(chunks))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for i, chunk in enumerate(chunks):
                ctx = ""
                if i > 0:
                    ctx = self._tail_context(chunks[i - 1], context_chars)
                futures[executor.submit(self._polish_chunk, chunk, multispeaker, ctx)] = i
            for future in as_completed(futures):
                self._check_cancel()
                i = futures[future]
                try:
                    polished[i] = future.result()
                except CancelledError:
                    raise
                except Exception as e:
                    logger.warning("%s分段 %d 失败，使用原文: %s", label, i + 1, e)
                    polished[i] = self._strip_time_prefix(chunks[i])

        result = ""
        for i, chunk_out in enumerate(polished):
            piece = chunk_out or ""
            if i == 0:
                result = piece
                continue
            piece = self._strip_echoed_context(result, piece)
            if not piece.strip():
                logger.warning("%s分段 %d 输出为空，使用原文", label, i + 1)
                piece = self._strip_time_prefix(chunks[i])
            result = result.rstrip() + "\n\n" + piece.strip()

        return result

    @staticmethod
    def _tail_context(text: str, max_chars: int) -> str:
        """取上一块末尾若干字符作为上下文（按行边界对齐）"""
        if len(text) <= max_chars:
            return text
        start = len(text) - max_chars
        nl = text.find("\n", start)
        return text[nl + 1:] if nl != -1 else text[start:]

    @staticmethod
    def _strip_echoed_context(acc: str, new: str, max_check: int = 400) -> str:
        """若新分块开头回显了已累积文本的结尾，按规范化的最长公共部分切掉。

        这是确定性去重：LLM 回显了就切，没回显就原样保留，绝不靠"砍掉第一段"猜测。
        """
        norm_acc = _normalize_for_match(acc)
        norm_new = _normalize_for_match(new)
        if not norm_acc or not norm_new:
            return new

        limit = min(len(norm_acc), len(norm_new), max_check)
        overlap = 0
        for size in range(limit, 19, -1):
            if norm_acc[-size:] == norm_new[:size]:
                overlap = size
                break
        if overlap < 20:
            return new

        # 把规范化长度映射回原文位置
        seen = 0
        cut = 0
        for idx, ch in enumerate(new):
            if seen >= overlap:
                cut = idx
                break
            if _normalize_for_match(ch):
                seen += 1
        else:
            cut = len(new)
        stripped = new[cut:].lstrip(" \t\n，。、；：？！,.;:?!")
        logger.debug("去掉分块边界重复 %d 字", overlap)
        return stripped

    def _polish_chunk(self, text: str, multispeaker: bool = False,
                      context: str = "") -> str:
        func = self.summarizer.polish_multispeaker if multispeaker else self.summarizer.polish
        try:
            try:
                return func(text, context=context)
            except TypeError:
                # 兼容未支持 context 参数的旧实现（上下文只是锦上添花，不该让整块失败）
                return func(text)
        except CancelledError:
            raise
        except Exception as e:
            label = "多说话人抛光" if multispeaker else "抛光"
            logger.warning("%s失败，使用原文: %s", label, e)
            return self._strip_time_prefix(text)

    @staticmethod
    def _restore_missing_chapters(polished: str, chapters: List[Dict[str, Any]]) -> str:
        """LLM 常把插入的「## 章节」当噪声删掉，而长度/相似度守卫看不出来。

        章节标题很短，丢了对正文影响不到 1%，所以 `_verify_polish` 不会回退。
        这里显式检查：标题不在正文里就在末尾补一份章节清单并告警，
        至少不让「章节保留」功能静默失效（README 承诺过会保留）。
        """
        if not chapters:
            return polished
        titles = [str(c.get("title") or "").strip() for c in chapters]
        missing = [t for t in titles if t and t not in polished]
        if not missing:
            return polished
        logger.warning("章节标题被 LLM 丢弃 %d/%d 个，已在文末补回章节清单: %s",
                       len(missing), len(titles), "、".join(missing[:5]))
        lines = [polished.rstrip(), "", "---", "", "## 章节"]
        for ch in chapters:
            title = str(ch.get("title") or "").strip()
            if not title:
                continue
            start = float(ch.get("start") or 0)
            m, s = divmod(int(start), 60)
            lines.append(f"- [{m:02d}:{s:02d}] {title}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _verify_polish(source: str, polished: str) -> str:
        """整体保真校验：长度或相似度明显异常时回退原文并告警。

        单个分块已有 guard，这里是全片兜底，防止"看起来成功但内容被改写/截断"。
        多说话人模式下模型会合法新增「说话人A：」标签，比对前必须剔除，
        否则短文本会被误判成"长度翻倍"而错误回退（本次实测踩到过）。
        """
        import difflib

        from summarizer import _strip_speaker_labels

        body = Pipeline._strip_time_prefix(source)
        compared = _strip_speaker_labels(polished)
        norm_src = _normalize_for_match(body)
        norm_out = _normalize_for_match(compared)
        if not norm_src:
            return polished
        ratio = len(norm_out) / len(norm_src)
        if not 0.85 <= ratio <= 1.25:
            logger.warning("抛光结果长度异常（原文 %d 字 → %d 字，%.0f%%），回退原文",
                           len(norm_src), len(norm_out), ratio * 100)
            return body
        sim = difflib.SequenceMatcher(None, norm_src, norm_out, autojunk=False).ratio()
        if sim < 0.90:
            logger.warning("抛光结果与原文相似度过低（%.2f），回退原文", sim)
            return body
        return polished

    # ------------------------------------------------------------------
    # 翻译
    # ------------------------------------------------------------------

    def _translate_lines(self, lines: List[str]) -> Optional[List[str]]:
        """逐行翻译，保持与段落 1:1；行数不一致时返回 None（宁可不翻译也不错位）"""
        translator = self._get_translator()
        text = "\n".join(lines)
        translated = translator.translate(text)
        out = [ln.strip() for ln in translated.split("\n") if ln.strip()]
        if len(out) != len(lines):
            logger.warning("翻译行数不匹配（%d → %d），跳过翻译以免字幕错位",
                           len(lines), len(out))
            return None
        return out

    # ------------------------------------------------------------------
    # 字幕
    # ------------------------------------------------------------------

    def _generate_srt(self, folder: Path, stem: str, segments: List[Dict[str, Any]],
                      lines: List[str]) -> None:
        """从段落时间戳 + 对应行文本生成 SRT（不依赖抛光后的文本结构）"""
        from subtitle import generate_srt

        if not segments:
            logger.warning("无段落时间戳数据，跳过字幕生成")
            return
        generate_srt(segments, lines, folder / f"{stem}.srt")

    # ------------------------------------------------------------------
    # 字幕优先
    # ------------------------------------------------------------------

    def _try_use_subtitles(self, sub_path: Path, folder: Path, stem: str,
                           meta: Dict[str, Any]) -> Optional[str]:
        """尝试使用下载的字幕。达标则写转写文件并返回文本，不达标返回 None。"""
        from subtitle_extractor import parse_subtitle_file, assess_quality, write_transcript_output

        info_path = folder / f"{stem}_subtitle_info.json"
        if not info_path.exists():
            return None

        sub_info = json.loads(info_path.read_text(encoding="utf-8"))

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

    # ------------------------------------------------------------------
    # 批量
    # ------------------------------------------------------------------

    def _process_playlist(self, url: str) -> Path:
        """处理播放列表，返回输出目录。

        失败数通过 `self.last_failures` 上报（GUI 用来显示"完成（N 个失败）"），
        不能让单个条目失败后仍然只打一句"全部完成" —— 用户会以为产物齐全。
        """
        logger.info("[播放列表模式]")
        results: List[Tuple[Path, Dict[str, Any]]] = self.downloader.download_playlist(url)

        failed = 0
        for video_path, meta in results:
            self._check_cancel()
            logger.info("--- %s ---", meta.get('title', video_path.stem))
            try:
                self._process_one(video_path, meta)
            except CancelledError:
                raise
            except Exception as e:
                failed += 1
                logger.error("[FAIL] %s", e)
                continue

        if results:
            group_dir: Path = results[0][0].parent.parent
            self._collect_outputs(group_dir)

        self.last_failures = failed
        self._log_batch_result(failed, len(results))
        return self.output_root

    def process_folder(self, folder_path: str) -> None:
        """批量处理文件夹内所有视频/音频

        失败数通过 `self.last_failures` 上报，理由同 `_process_playlist`。
        """
        folder: Path = Path(folder_path).resolve()
        if not folder.is_dir():
            raise NotADirectoryError(f"路径不存在或不是文件夹: {folder_path}")

        files: List[Path] = sorted(
            f for f in folder.iterdir()
            if f.is_file() and f.suffix.lower() in MEDIA_EXTS
        )
        if not files:
            logger.warning("文件夹内未找到视频/音频文件: %s", folder)
            self.last_failures = 0
            return

        group_name: str = sanitize_filename(folder.name)
        logger.info("[文件夹模式] 共发现 %d 个文件 → 输出: %s", len(files), self.output_root / group_name)

        failed = 0
        for i, file_path in enumerate(files):
            self._check_cancel()
            logger.info("--- [%d/%d] %s ---", i + 1, len(files), file_path.stem)
            try:
                video_path, meta = self.downloader._import_local(str(file_path), output_subdir=group_name)
            except Exception as e:
                failed += 1
                logger.error("[FAIL] 导入失败: %s", e)
                continue

            try:
                self._process_one(video_path, meta)
            except CancelledError:
                raise
            except Exception as e:
                failed += 1
                logger.error("[FAIL] %s", e)
                continue

        self._collect_outputs(self.output_root / group_name)
        self.last_failures = failed
        self._log_batch_result(failed, len(files))

    def _log_batch_result(self, failed: int, total: int) -> None:
        """批量结束时如实报告成功/失败数"""
        if failed:
            logger.warning("完成，但有 %d/%d 个任务失败（见上方 [FAIL]）", failed, total)
        else:
            logger.info("全部完成! 输出目录: %s", self.output_root)

    def _collect_outputs(self, group_dir: Path) -> None:
        """将批量处理的所有转写和总结分别收集到汇总文件夹"""
        import shutil

        formats: List[str] = self.config.get("summarizer", {}).get("output_formats", ["md"])
        exts = set(f".{f}" for f in formats)
        prefixes = tuple(f"{cn}-" for cn in STYLE_CN.values())

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
                if file.stem.startswith(prefixes):
                    shutil.copy2(file, summary_dir / file.name)
                    count_s += 1
                else:
                    shutil.copy2(file, transcript_dir / file.name)
                    count_t += 1

        if count_t > 0:
            logger.info("转写汇总: %d 个文件 → %s", count_t, transcript_dir)
        if count_s > 0:
            logger.info("总结汇总: %d 个文件 → %s", count_s, summary_dir)
