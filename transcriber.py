"""
音频转文字模块 — faster-whisper 转写
从视频提取音频后转写为文本
"""
import gc
import json
import logging
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from utils import sanitize_filename, find_ffmpeg, set_state, PROJECT_ROOT, AUDIO_EXTS, init_cuda

logger = logging.getLogger(__name__)

_GPU_ERROR_KEYWORDS: List[str] = [
    "cublas", "cuda", "cudnn", "out of memory", "no kernel image",
    "invalid device", "device not found", "driver is too old",
    "cuda error", "gpu", "failed to load",
]


class Transcriber:

    def __init__(self, config: Dict[str, Any], output_root: Path) -> None:
        self.config: Dict[str, Any] = config
        self.output_root: Path = Path(output_root)
        self.whisper_config: Dict[str, Any] = config.get("whisper", {})
        self._model: Optional[Any] = None
        self._model_cache: Path = PROJECT_ROOT / "models"

    # ---- 音频提取 context manager ----

    @contextmanager
    def _ensure_audio(self, input_path: Path, output_folder: Path, fmt: str = "mp3") -> Iterator[Path]:
        """上下文管理器：确保音频可用，退出时自动清理临时文件"""
        is_audio: bool = input_path.suffix.lower() in AUDIO_EXTS
        if is_audio:
            yield input_path
            return

        audio_path: Path = output_folder / f"_tmp_audio_{os.getpid()}.{fmt}"
        try:
            self._extract_audio(input_path, audio_path, fmt=fmt)
            yield audio_path
        finally:
            if audio_path.exists():
                audio_path.unlink()

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

        if transcript_path.exists():
            logger.info("已转写: %s", safe_name)
            set_state(output_folder, "transcribed")
            return transcript_path

        file_type: str = "音频" if input_path.suffix.lower() in AUDIO_EXTS else "视频"
        logger.info("转写中 (%s): %s", file_type, safe_name)

        with self._ensure_audio(input_path, output_folder) as audio_path:
            return self._run_transcription(audio_path, safe_name, transcript_path, output_folder)

    def _run_transcription(self, audio_path: Path, safe_name: str,
                           transcript_path: Path, output_folder: Path) -> Path:
        """执行转写并保存"""
        model = self._get_model()
        language: Optional[str] = self.whisper_config.get("language", "zh")
        if language == "auto":
            language = None

        def _do_transcribe(m):
            kwargs = {"language": language, "word_timestamps": True}

            # VAD 语音检测：自动跳过静音，提升速度减少幻觉
            if self.whisper_config.get("vad_enabled", True):
                kwargs["vad_filter"] = True
                kwargs["vad_parameters"] = {
                    "min_silence_duration_ms": 500,
                    "speech_pad_ms": 400,
                }

            # 初始提示词：帮助模型理解主题，提高术语识别率
            prompt = self.whisper_config.get("initial_prompt", "").strip()
            if prompt:
                kwargs["initial_prompt"] = prompt

            # 热词增强：提高特定词汇的识别概率
            hotwords = self.whisper_config.get("hotwords", "").strip()
            if hotwords:
                kwargs["hotwords"] = hotwords

            return m.transcribe(str(audio_path), **kwargs)

        def _collect_segments(segs) -> List[Dict[str, Any]]:
            items: List[Dict[str, Any]] = []
            for seg in segs:
                text = seg.text.strip()
                if text:
                    items.append({
                        "start": seg.start,
                        "end": seg.end,
                        "text": text,
                    })
            return items

        try:
            segs, info = _do_transcribe(model)
            seg_items = _collect_segments(segs)
        except RuntimeError as e:
            # 仅 GPU 相关错误才回退 CPU，其他错误直接抛出
            if self._is_gpu_error(e):
                logger.warning("GPU 转写失败，回退到 CPU (%s)", e)
                self._model = None
                gc.collect()
                model = self._get_model(force_cpu=True)
                segs, info = _do_transcribe(model)
                seg_items = _collect_segments(segs)
            else:
                raise

        self._save_segments_json(output_folder, safe_name, seg_items)
        transcript_text: str = self._format_transcript(seg_items)

        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(f"# {safe_name}\n\n")
            f.write(transcript_text)

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
    def _save_segments_json(folder: Path, safe_name: str, items: List[Dict[str, Any]]) -> None:
        seg_path = folder / f"{safe_name}_segments.json"
        with open(seg_path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)

    # ---- 音频提取 ----

    def _extract_audio(self, video_path: Path, audio_path: Path, fmt: str = "mp3") -> None:
        ffmpeg: str = find_ffmpeg()
        if fmt == "wav":
            cmd: List[str] = [ffmpeg, "-i", str(video_path), "-vn", "-acodec", "pcm_s16le",
                              "-ar", "16000", "-ac", "1", "-y", str(audio_path)]
        else:
            cmd: List[str] = [ffmpeg, "-i", str(video_path), "-vn", "-acodec", "mp3",
                              "-q:a", "2", "-y", str(audio_path)]
        subprocess.run(cmd, capture_output=True, check=True, timeout=600)
