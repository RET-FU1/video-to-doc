"""
音频转文字模块 — faster-whisper
从视频提取音频后转写为 Markdown 文档
"""
import os
import re
import subprocess
import shutil
import sys
from pathlib import Path

# GPU 加速：加载 CUDA DLL
# os.add_dll_directory 方式
for _d in sys.path:
    _nvidia = os.path.join(_d, "nvidia")
    if os.path.isdir(_nvidia):
        for _sub in os.listdir(_nvidia):
            _bin = os.path.join(_nvidia, _sub, "bin")
            if os.path.isdir(_bin):
                try:
                    os.add_dll_directory(_bin)
                except Exception:
                    pass
    _ctranslate = os.path.join(_d, "ctranslate2")
    if os.path.isdir(_ctranslate):
        try:
            os.add_dll_directory(_ctranslate)
        except Exception:
            pass

# 显式预加载 cublas，确保 ctranslate2 能找到
try:
    import ctypes as _ctypes
    _ctypes.cdll.LoadLibrary("cublas64_12.dll")
    _ctypes.cdll.LoadLibrary("cudart64_12.dll")
except OSError:
    pass


class Transcriber:
    def __init__(self, config, output_root):
        self.config = config
        self.output_root = Path(output_root)
        self.whisper_config = config.get("whisper", {})
        self._model = None
        self._model_cache = Path(__file__).parent / "models"

    def _find_ffmpeg(self):
        ff = shutil.which("ffmpeg")
        if ff:
            return ff
        raise FileNotFoundError("未找到 ffmpeg，请先安装 ffmpeg")

    def _resolve_model_path(self):
        model_dirs = list(self._model_cache.glob("pengzhendong/faster-whisper-large-v3-turbo"))
        if not model_dirs:
            model_dirs = list(self._model_cache.glob("models--*"))
        if model_dirs:
            return str(model_dirs[0])
        return "large-v3-turbo"

    def _get_model(self):
        if self._model:
            return self._model

        from faster_whisper import WhisperModel

        model_path = self._resolve_model_path()
        device = self.whisper_config.get("device", "cuda")
        compute = self.whisper_config.get("compute_type", "float16")

        try:
            model = WhisperModel(model_path, device=device, compute_type=compute,
                                 download_root=str(self._model_cache))
            print(f"  模型已加载 (device={device}, compute={compute})")
        except Exception:
            print(f"  GPU 不可用，回退到 CPU")
            model = WhisperModel(model_path, device="cpu", compute_type="int8",
                                 download_root=str(self._model_cache))
            print(f"  模型已加载 (device=cpu, compute=int8)")

        self._model = model
        return model

    def transcribe(self, video_path, output_folder):
        """转写视频，返回 transcript 文件路径"""
        output_folder = Path(output_folder)
        output_folder.mkdir(parents=True, exist_ok=True)

        video_stem = Path(video_path).stem
        safe_name = re.sub(r'[<>:"/\\|?*]', "-", video_stem)
        transcript_path = output_folder / f"{safe_name}.md"
        audio_path = output_folder / f"_tmp_audio_{os.getpid()}.mp3"

        state_file = output_folder / ".pipeline_state"
        if transcript_path.exists():
            print(f"  已转写: {safe_name}")
            self._set_state(state_file, "transcribed")
            return transcript_path

        print(f"  转写中: {safe_name}")

        # 提取音频
        self._extract_audio(video_path, audio_path)

        # 转写
        try:
            model = self._get_model()
            language = self.whisper_config.get("language", "zh")
            if language == "auto":
                language = None

            def _do_transcribe(m):
                segs, info = m.transcribe(str(audio_path), language=language)
                return [seg.text.strip() for seg in segs]

            try:
                lines = _do_transcribe(model)
            except Exception as gpu_err:
                if "cublas" in str(gpu_err).lower() or "cuda" in str(gpu_err).lower():
                    print(f"  GPU 转写失败，回退到 CPU ({gpu_err})")
                    from faster_whisper import WhisperModel
                    self._model = None
                    model = WhisperModel(
                        self._resolve_model_path(), device="cpu", compute_type="int8",
                        download_root=str(self._model_cache))
                    self._model = model
                    lines = _do_transcribe(model)
                else:
                    raise

            # 写入 Markdown
            with open(transcript_path, "w", encoding="utf-8") as f:
                f.write(f"# {video_stem}\n\n")
                f.write("\n\n".join(lines))

            print(f"  已保存: {transcript_path.name}")
            self._set_state(state_file, "transcribed")
            return transcript_path

        finally:
            if audio_path.exists():
                audio_path.unlink()

    def _extract_audio(self, video_path, audio_path):
        ffmpeg = self._find_ffmpeg()
        cmd = [ffmpeg, "-i", str(video_path), "-vn", "-acodec", "mp3",
               "-q:a", "2", "-y", str(audio_path)]
        subprocess.run(cmd, capture_output=True, check=True)

    @staticmethod
    def _set_state(state_file, state):
        with open(state_file, "w") as f:
            f.write(state)
