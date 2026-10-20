"""
音频转文字模块 — faster-whisper 转写
从视频提取音频后转写为文本
"""
import gc
import json
import logging
import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from utils import (sanitize_filename, find_ffmpeg, set_state, PROJECT_ROOT,
                   AUDIO_EXTS, init_cuda, CancelledError, CancelToken)

logger = logging.getLogger(__name__)

_GPU_ERROR_KEYWORDS: List[str] = [
    "cublas", "cuda", "cudnn", "out of memory", "no kernel image",
    "invalid device", "device not found", "driver is too old",
    "cuda error", "gpu", "failed to load",
]

# ---- 低置信判定阈值 ----
# faster-whisper 内部用同样三个指标判断"这段是不是幻觉/静音"
# (log_prob_threshold=-1.0 / no_speech_threshold=0.6 / compression_ratio_threshold=2.4)。
# 这里只是**告警**，不删段：降级段的文本仍可能有用，删掉就是真丢内容。
LOWCONF_LOGPROB: float = -1.0
LOWCONF_NO_SPEECH: float = 0.6
LOWCONF_COMPRESSION: float = 2.4

# 合法音频提取格式
_AUDIO_FORMATS = ("wav", "mp3")


class Transcriber:

    def __init__(self, config: Dict[str, Any], output_root: Path,
                 cancel_token: Optional[CancelToken] = None) -> None:
        self.config: Dict[str, Any] = config
        self.output_root: Path = Path(output_root)
        self.whisper_config: Dict[str, Any] = config.get("whisper", {})
        self._model: Optional[Any] = None
        self._model_cache: Path = PROJECT_ROOT / "models"
        self._cancel: CancelToken = cancel_token or CancelToken()

    # ---- 音频提取 context manager ----

    def _audio_format(self) -> str:
        """配置的音频提取格式（wav / mp3）

        wav 16kHz 单声道是 whisper 的原生采样格式，省掉一次重采样，
        且不像 mp3 那样受 VBR 解码差异影响 —— 实测两者识别结果一致，
        但 wav 更稳。
        """
        fmt: str = str(self.whisper_config.get("audio_format", "wav") or "wav").lower()
        if fmt not in _AUDIO_FORMATS:
            logger.warning("whisper.audio_format=%r 无效，回退 wav", fmt)
            return "wav"
        return fmt

    @contextmanager
    def _ensure_audio(self, input_path: Path, output_folder: Path,
                      fmt: Optional[str] = None) -> Iterator[Path]:
        """上下文管理器：确保音频可用，退出时自动清理临时文件"""
        is_audio: bool = input_path.suffix.lower() in AUDIO_EXTS
        if is_audio:
            yield input_path
            return

        fmt = fmt or self._audio_format()
        audio_path: Path = output_folder / f"_tmp_audio_{os.getpid()}.{fmt}"
        try:
            self._extract_audio(input_path, audio_path, fmt=fmt)
            yield audio_path
        finally:
            if audio_path.exists():
                try:
                    audio_path.unlink()
                except OSError as e:
                    logger.warning("清理临时音频失败（可手动删除）: %s", e)

    # ---- 模型加载 ----

    def _resolve_model_path(self) -> str:
        # 优先查找 ModelScope 目录结构
        ms_dirs = list(self._model_cache.glob("pengzhendong/faster-whisper-large-v3-turbo"))
        if ms_dirs:
            return str(ms_dirs[0])

        # 查找 HuggingFace 缓存结构: models--*/snapshots/*/
        for hf_parent in self._model_cache.glob("models--*"):
            snapshots = hf_parent / "snapshots"
            if snapshots.is_dir():
                for snap in sorted(snapshots.iterdir(), reverse=True):
                    if snap.is_dir() and any(f.suffix == ".bin" for f in snap.iterdir()):
                        return str(snap)

        return "large-v3-turbo"

    def _get_model(self, force_cpu: bool = False) -> Any:
        """加载 Whisper 模型，自动 GPU→CPU 回退"""
        if self._model and not force_cpu:
            return self._model

        self._cancel.check()
        init_cuda()

        from faster_whisper import WhisperModel

        model_path: str = self._resolve_model_path()

        if force_cpu:
            device, compute = "cpu", "int8"
        else:
            device = self.whisper_config.get("device", "cuda")
            compute = self.whisper_config.get("compute_type", "float16")

        try:
            self._model = WhisperModel(model_path, device=device, compute_type=compute,
                                       download_root=str(self._model_cache))
            logger.info("模型已加载 (device=%s, compute=%s)", device, compute)
        except Exception as e:
            if force_cpu:
                raise
            if self._is_gpu_error(e):
                logger.warning("GPU 不可用，回退到 CPU: %s", e)
            else:
                logger.error("模型加载失败（非 GPU 错误），回退到 CPU: %s", e)
            device, compute = "cpu", "int8"
            self._model = WhisperModel(model_path, device="cpu", compute_type="int8",
                                       download_root=str(self._model_cache))
            logger.info("模型已加载 (device=cpu, compute=int8)")

        return self._model

    @staticmethod
    def _is_gpu_error(error: Exception) -> bool:
        msg = str(error).lower()
        return any(keyword in msg for keyword in _GPU_ERROR_KEYWORDS)

    # ---- 主入口 ----

    def transcribe(self, input_path: str, output_folder: str) -> Path:
        """转写视频/音频，返回 transcript 文件路径"""
        output_folder = Path(output_folder)
        output_folder.mkdir(parents=True, exist_ok=True)

        input_path = Path(input_path)
        safe_name: str = sanitize_filename(input_path.stem)
        transcript_path: Path = output_folder / f"{safe_name}.txt"
        segments_path: Path = output_folder / f"{safe_name}_segments.json"

        # 只要 _segments.json 还在就直接复用：它比 .txt 更完整（含时间戳和置信度），
        # .txt 只是派生产物。历史 bug：换总结风格/换输出格式重跑时，pipeline 会删掉
        # .txt，而旧逻辑要求"txt + segments 都在"才短路，于是 40 分钟视频被整段重转。
        if segments_path.exists():
            logger.info("已转写: %s", safe_name)
            if not transcript_path.exists():
                self._write_transcript_from_segments(segments_path, transcript_path, safe_name)
            set_state(output_folder, "transcribed")
            return transcript_path

        if transcript_path.exists():
            logger.warning("发现转写文本但缺少段落时间戳(%s)，重新转写",
                           segments_path.name)

        self._cancel.check()
        file_type: str = "音频" if input_path.suffix.lower() in AUDIO_EXTS else "视频"
        logger.info("转写中 (%s): %s", file_type, safe_name)

        with self._ensure_audio(input_path, output_folder) as audio_path:
            self._cancel.check()
            return self._run_transcription(audio_path, safe_name, transcript_path,
                                           output_folder)

    def _transcribe_kwargs(self) -> Dict[str, Any]:
        """把 whisper 配置映射为 faster-whisper 的 transcribe() 参数

        可用 `inspect.signature(WhisperModel.transcribe)` 核对：以下参数在
        faster-whisper 1.2.1 中均存在。
        """
        cfg = self.whisper_config

        # language 不再强制转 None：只有配置成 auto 才让模型自己检测
        language: Optional[str] = cfg.get("language", "zh")
        if isinstance(language, str) and language.strip().lower() in ("", "auto", "none"):
            language = None

        kwargs: Dict[str, Any] = {
            "language": language,
            "word_timestamps": True,
            "beam_size": int(cfg.get("beam_size", 5)),
            # 关闭"把上一段输出当提示词"：中文长音频会因此陷入重复循环
            "condition_on_previous_text": _as_bool(
                cfg.get("condition_on_previous_text", False)),
            "language_detection_segments": int(cfg.get("language_detection_segments", 1)),
        }

        # VAD 语音检测：自动跳过静音，提升速度减少幻觉
        if _as_bool(cfg.get("vad_enabled", True)):
            kwargs["vad_filter"] = True
            kwargs["vad_parameters"] = {
                "min_silence_duration_ms": 500,
                "speech_pad_ms": 400,
            }

        # 幻觉静音阈值：只在配置了值时才传（None 是"不启用"，不用显式传）
        halluc = cfg.get("hallucination_silence_threshold", None)
        if halluc is not None:
            try:
                kwargs["hallucination_silence_threshold"] = float(halluc)
            except (TypeError, ValueError):
                logger.warning("hallucination_silence_threshold=%r 不是数字，忽略", halluc)

        # 初始提示词：帮助模型理解主题，提高术语识别率
        prompt = str(cfg.get("initial_prompt", "") or "").strip()
        if prompt:
            kwargs["initial_prompt"] = prompt

        # 热词增强：提高特定词汇的识别概率
        hotwords = str(cfg.get("hotwords", "") or "").strip()
        if hotwords:
            kwargs["hotwords"] = hotwords

        return kwargs

    def _collect_segments(self, segs: Any) -> List[Dict[str, Any]]:
        """消费 faster-whisper 的段落生成器，保留置信度指标

        Segment 带 avg_logprob / no_speech_prob / compression_ratio，
        是事后质检验证的唯一依据（以前全部丢弃，等于无法定位错漏段落）。
        """
        items: List[Dict[str, Any]] = []
        for seg in segs:
            self._cancel.check()
            text = (seg.text or "").strip()
            if not text:
                continue
            items.append({
                "start": seg.start,
                "end": seg.end,
                "text": text,
                "avg_logprob": _round_metric(getattr(seg, "avg_logprob", None)),
                "no_speech_prob": _round_metric(getattr(seg, "no_speech_prob", None)),
                "compression_ratio": _round_metric(getattr(seg, "compression_ratio", None)),
            })
        return items

    def _run_transcription(self, audio_path: Path, safe_name: str,
                           transcript_path: Path, output_folder: Path) -> Path:
        """执行转写并保存"""
        model = self._get_model()
        kwargs = self._transcribe_kwargs()

        def _do_transcribe(m):
            return m.transcribe(str(audio_path), **kwargs)

        try:
            segs, info = _do_transcribe(model)
            seg_items = self._collect_segments(segs)
        except CancelledError:
            raise
        except RuntimeError as e:
            # 仅 GPU 相关错误才回退 CPU，其他错误直接抛出
            if self._is_gpu_error(e):
                logger.warning("GPU 转写失败，回退到 CPU (%s)", e)
                self._model = None
                gc.collect()
                model = self._get_model(force_cpu=True)
                segs, info = _do_transcribe(model)
                seg_items = self._collect_segments(segs)
            else:
                raise

        self._cancel.check()

        log_info = (
            "识别语言=%s (概率 %.2f), 音频时长 %.1fs"
            % (getattr(info, "language", "?"),
               getattr(info, "language_probability", 0.0) or 0.0,
               getattr(info, "duration", 0.0) or 0.0)
        )
        logger.info("%s，共 %d 段", log_info, len(seg_items))

        self._save_segments_json(output_folder, safe_name, seg_items)
        self._cancel.check()

        transcript_text: str = self._format_transcript(seg_items)

        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(f"# {safe_name}\n\n")
            f.write(transcript_text)

        self._report_low_confidence(output_folder, safe_name, seg_items)

        logger.info("已保存: %s", transcript_path.name)
        set_state(output_folder, "transcribed")
        return transcript_path

    # ---- 文本格式化 ----

    @staticmethod
    def _format_transcript(segments) -> str:
        lines: List[str] = []
        for seg in segments:
            if isinstance(seg, dict):
                text = seg.get("text", "").strip()
            else:
                text = seg.text.strip()
            if text:
                lines.append(text)
        return "\n".join(lines)

    @staticmethod
    def _write_transcript_from_segments(segments_path: Path, transcript_path: Path,
                                        safe_name: str) -> None:
        """由已存在的 _segments.json 重建 .txt

        `_segments.json` 是完整产物（含时间戳与置信度），`.txt` 只是派生的纯文本。
        `_cleanup` 在输出格式不含 txt 时会删掉 .txt，重跑时用它重建即可，
        不必再花几十分钟重跑 ASR。
        """
        items = json.loads(segments_path.read_text(encoding="utf-8"))
        text = Transcriber._format_transcript(items)
        transcript_path.write_text(f"# {safe_name}\n\n{text}", encoding="utf-8")

    @staticmethod
    def _save_segments_json(folder: Path, safe_name: str, items: List[Dict[str, Any]]) -> None:
        seg_path = folder / f"{safe_name}_segments.json"
        with open(seg_path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)

    # ---- 低置信告警 ----

    @staticmethod
    def find_low_confidence(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """挑出低置信段落（只用于告警/质检，绝不删除）"""
        out: List[Dict[str, Any]] = []
        for seg in segments:
            logprob = seg.get("avg_logprob")
            no_speech = seg.get("no_speech_prob")
            compression = seg.get("compression_ratio")
            if ((logprob is not None and logprob < LOWCONF_LOGPROB) or
                    (no_speech is not None and no_speech > LOWCONF_NO_SPEECH) or
                    (compression is not None and compression > LOWCONF_COMPRESSION)):
                out.append(seg)
        return out

    def _report_low_confidence(self, folder: Path, safe_name: str,
                               segments: List[Dict[str, Any]]) -> None:
        """统计并告警低置信段落，同时落盘 {stem}_lowconf.json 供人工复核"""
        lowconf = self.find_low_confidence(segments)
        report_path = folder / f"{safe_name}_lowconf.json"

        if not lowconf:
            # 重转后可能已无低置信段，清掉上次的残留报告
            report_path.unlink(missing_ok=True)
            logger.info("置信度检查通过（0 段低置信）")
            return

        start = min(seg.get("start", 0.0) for seg in lowconf)
        end = max(seg.get("end", 0.0) for seg in lowconf)
        logger.warning("检测到 %d 段低置信转写（可能在 %.1f-%.1f 秒），建议核对",
                       len(lowconf), start, end)

        payload = [{
            "start": seg.get("start"),
            "end": seg.get("end"),
            "text": seg.get("text", ""),
            "avg_logprob": seg.get("avg_logprob"),
            "no_speech_prob": seg.get("no_speech_prob"),
            "compression_ratio": seg.get("compression_ratio"),
        } for seg in lowconf]
        try:
            report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
            logger.info("低置信段落已记录: %s", report_path.name)
        except OSError as e:
            logger.warning("写入低置信报告失败: %s", e)

    # ---- 音频提取 ----

    def _extract_audio(self, video_path: Path, audio_path: Path, fmt: str = "wav") -> None:
        ffmpeg: str = find_ffmpeg()

        # 先探一下有没有音轨：没有音轨时 ffmpeg 会报一段晦涩的
        # "Output file does not contain any stream / Invalid argument"，
        # 用户完全看不出是「下载到的文件里没有声音」。这里提前给出可读原因。
        if not self._has_audio_stream(video_path):
            raise RuntimeError(
                f"文件里没有音轨，无法转写：{video_path.name}\n"
                f"常见原因：下载时只保存了视频流（视频站点的音视频是分开的），"
                f"或该视频本身就是无声的。\n"
                f"解决办法：删除该目录后重新下载（确保 config.yaml 的 "
                f"downloader.quality 不是 'audio'，且下载未被中断）。"
            )

        if fmt == "wav":
            # 16kHz 单声道 pcm_s16le：whisper 的原生输入格式
            cmd: List[str] = [ffmpeg, "-i", str(video_path), "-vn", "-acodec", "pcm_s16le",
                              "-ar", "16000", "-ac", "1", "-y", str(audio_path)]
        else:
            cmd: List[str] = [ffmpeg, "-i", str(video_path), "-vn", "-acodec", "mp3",
                              "-q:a", "2", "-y", str(audio_path)]
        try:
            subprocess.run(cmd, capture_output=True, check=True, timeout=1800)
        except subprocess.CalledProcessError as e:
            stderr_tail = (e.stderr or b"").decode("utf-8", "replace").strip().split("\n")[-3:]
            raise RuntimeError(
                f"音频提取失败（ffmpeg 退出码 {e.returncode}）: {' / '.join(stderr_tail)}"
            ) from e

    @staticmethod
    def _has_audio_stream(video_path: Path) -> bool:
        """用 ffprobe 检查文件是否含音轨。

        ffprobe 不可用或探测失败时返回 True（保守策略：让 ffmpeg 自己去试，
        不要把「探测工具缺失」误判成「文件没音轨」）。
        """
        ffprobe = None
        try:
            ffprobe = str(Path(find_ffmpeg()).with_name(
                "ffprobe.exe" if os.name == "nt" else "ffprobe"))
        except FileNotFoundError:
            return True
        if not Path(ffprobe).exists():
            found = shutil.which("ffprobe")
            if not found:
                return True
            ffprobe = found
        try:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(video_path)],
                capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        if result.returncode != 0:
            return True
        return "audio" in (result.stdout or "")


def _round_metric(value: Any, digits: int = 4) -> Optional[float]:
    """置信度指标四舍五入到 4 位（json 里避免一长串浮点噪音）"""
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any, default: bool = False) -> bool:
    """把配置值转成 bool。

    直接 bool("false") 会得到 True —— YAML 里写引号的 "false" 很常见，
    这里显式识别字符串形式，避免配置静默失效。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("false", "0", "no", "off", ""):
            return False
        if lowered in ("true", "1", "yes", "on"):
            return True
        return default
    return bool(value)
