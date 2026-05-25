"""
音频转文字模块 — faster-whisper + 可选 pyannote 说话人分离
从视频提取音频后转写为 Markdown 文档
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
        self.diar_config: Dict[str, Any] = config.get("diarization", {})
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

    # ---- 说话人分离前置检查 ----

    def _preflight_diarization(self) -> bool:
        """前置检查：HF_TOKEN + 模型可访问性。返回 True=通过，False=回退基础转写。"""
        hf_token: str = (self.diar_config.get("hf_token") or
                          os.environ.get("HF_TOKEN", ""))

        if not hf_token:
            logger.warning(
                "未配置 HF_TOKEN，说话人分离将被跳过。\n"
                "  获取步骤：\n"
                "  1. 访问 https://huggingface.co/settings/tokens\n"
                "  2. 登录后点击 \"Create new token\"，类型选 Read\n"
                "  3. 将生成的 token 写入 .env 文件：HF_TOKEN=hf_xxx\n"
                "  详情见 README.md → 说话人分离"
            )
            return False

        required_models = [
            ("pyannote/speaker-diarization-3.1",
             "https://huggingface.co/pyannote/speaker-diarization-3.1"),
            ("pyannote/segmentation-3.0",
             "https://huggingface.co/pyannote/segmentation-3.0"),
            ("pyannote/speaker-diarization-community-1",
             "https://huggingface.co/pyannote/speaker-diarization-community-1"),
        ]

        try:
            from huggingface_hub import HfApi
        except ImportError:
            logger.warning("huggingface_hub 未安装，跳过模型访问检查")
            return True

        from huggingface_hub.errors import RevisionNotFoundError

        api = HfApi()
        for model_id, url in required_models:
            try:
                api.model_info(model_id, token=hf_token)
            except RevisionNotFoundError:
                pass
            except Exception as e:
                msg = str(e).lower()
                if any(kw in msg for kw in ("403", "401", "access", "unauthorized", "gated")):
                    logger.warning(
                        "模型 %s 尚未授权，说话人分离将被跳过。\n"
                        "  解决：访问 %s\n"
                        "  点击 \"Agree and access repository\" 接受用户协议\n"
                        "  （姓名、机构可随意填写）\n"
                        "  注意：每个模型需单独授权，共 3 个",
                        model_id, url
                    )
                    return False
                logger.debug("检查模型 %s 时出现网络问题: %s", model_id, e)

        logger.info("说话人分离前置检查通过")
        return True

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

        # 说话人分离路径
        if self.diar_config.get("enabled", False):
            if self._preflight_diarization():
                try:
                    return self._transcribe_with_diarization(
                        input_path, output_folder, safe_name
                    )
                except Exception as e:
                    logger.warning("pyannote 说话人分离失败，回退到基础转写: %s", e)
            else:
                logger.warning("说话人分离前置检查未通过，回退到基础转写")

        # 基础转写路径（faster-whisper）
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
            return m.transcribe(str(audio_path), language=language, word_timestamps=True)

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
        except Exception as gpu_err:
            if self._is_gpu_error(gpu_err):
                logger.warning("GPU 转写失败，回退到 CPU (%s)", gpu_err)
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

    # ---- pyannote 说话人分离 ----

    def _transcribe_with_diarization(self, input_path: Path, output_folder: Path,
                                     safe_name: str) -> Path:
        """faster-whisper 转写 + pyannote 说话人分离"""
        try:
            from pyannote.audio import Pipeline
        except ImportError:
            raise ImportError(
                "pyannote.audio 未安装。安装命令: pip install pyannote.audio"
            )

        hf_token: str = self.diar_config.get("hf_token") or os.environ.get("HF_TOKEN", "")
        if not hf_token:
            raise ValueError("HF_TOKEN 未配置，pyannote 说话人分离需要 HuggingFace Token")

        with self._ensure_audio(input_path, output_folder, fmt="wav") as audio_path:
            # 1. faster-whisper 转写（含词级时间戳）
            model = self._get_model()
            language: Optional[str] = self.whisper_config.get("language", "zh")
            if language == "auto":
                language = None

            segments, info = model.transcribe(
                str(audio_path), language=language
            )

            # 2. pyannote 说话人分离
            import numpy as np
            import scipy.io.wavfile as wavfile
            import torch

            sample_rate, audio_np = wavfile.read(str(audio_path))
            if audio_np.dtype == np.int16:
                audio_np = audio_np.astype(np.float32) / 32768.0
            elif audio_np.dtype == np.int32:
                audio_np = audio_np.astype(np.float32) / 2147483648.0
            else:
                audio_np = audio_np.astype(np.float32)

            waveform = torch.from_numpy(audio_np).float()
            if waveform.ndim == 1:
                waveform = waveform.unsqueeze(0)
            elif waveform.ndim == 2:
                waveform = waveform.T

            pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                token=hf_token,
            )
            try:
                if torch.cuda.is_available():
                    pipeline = pipeline.to(torch.device("cuda"))
            except Exception:
                logger.warning("GPU 迁移失败，将使用 CPU 进行说话人分离（速度较慢）")

            min_spk: int = self.diar_config.get("min_speakers", 2)
            max_spk: int = self.diar_config.get("max_speakers", 5)
            diarization = pipeline(
                {"waveform": waveform, "sample_rate": sample_rate},
                min_speakers=min_spk,
                max_speakers=max_spk,
            )

            # 3. 词 → 说话人匹配
            speaker_segments = self._assign_speakers(segments, diarization)
            self._save_segments_json(output_folder, safe_name, speaker_segments)

            # 4. 格式化输出
            return self._format_diarized_output(
                {"segments": speaker_segments}, output_folder, safe_name
            )

    @staticmethod
    def _assign_speakers(segments, diarization) -> list:
        speaker_turns = []
        for turn, _, speaker in diarization.speaker_diarization.itertracks(yield_label=True):
            speaker_turns.append((turn.start, turn.end, speaker))

        results = []
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue

            speaker_overlap: dict = {}
            for t_start, t_end, spk in speaker_turns:
                overlap = max(0.0, min(seg.end, t_end) - max(seg.start, t_start))
                if overlap > 0:
                    speaker_overlap[spk] = speaker_overlap.get(spk, 0.0) + overlap

            dominant = max(speaker_overlap, key=speaker_overlap.get) if speaker_overlap else "UNKNOWN"
            results.append({
                "speaker": dominant,
                "start": seg.start,
                "end": seg.end,
                "text": text,
            })

        return results

    def _format_diarized_output(self, result: Dict[str, Any], output_folder: Path,
                                safe_name: str) -> Path:
        transcript_path: Path = output_folder / f"{safe_name}.txt"

        merged: List[Dict[str, Any]] = []
        for seg in result.get("segments", []):
            speaker: str = seg.get("speaker", "UNKNOWN")
            text: str = seg.get("text", "").strip()
            if not text:
                continue
            if merged and merged[-1]["speaker"] == speaker:
                merged[-1]["end"] = seg.get("end", 0)
                merged[-1]["text"] += " " + text
            else:
                merged.append({
                    "speaker": speaker,
                    "start": seg.get("start", 0),
                    "end": seg.get("end", 0),
                    "text": text,
                })

        lines: List[str] = [f"# {safe_name}", ""]
        for block in merged:
            header = (
                f"## {block['speaker']} "
                f"({self._format_time(block['start'])} - "
                f"{self._format_time(block['end'])})"
            )
            lines.append(header)
            lines.append("")
            lines.append(block["text"])
            lines.append("")

        content: str = "\n".join(lines)
        transcript_path.write_text(content, encoding="utf-8")
        logger.info("已保存（含说话人标签）: %s", transcript_path.name)
        set_state(output_folder, "transcribed")
        return transcript_path

    @staticmethod
    def _format_time(seconds: float) -> str:
        h: int = int(seconds // 3600)
        m: int = int((seconds % 3600) // 60)
        s: float = seconds % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}"

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
