"""
音频转文字模块 — faster-whisper + 可选 whisperX 说话人分离
从视频提取音频后转写为 Markdown 文档
"""
import os
import subprocess
from pathlib import Path
from utils import sanitize_filename, find_ffmpeg, set_state, PROJECT_ROOT

_cuda_inited = False


def _init_cuda():
    """懒加载 CUDA DLL（首次调用 _get_model 时执行）"""
    global _cuda_inited
    if _cuda_inited:
        return
    _cuda_inited = True

    import sys

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

    import ctypes
    for dll in ("cublas64_12.dll", "cudart64_12.dll"):
        try:
            ctypes.cdll.LoadLibrary(dll)
        except OSError:
            pass


class Transcriber:
    def __init__(self, config, output_root):
        self.config = config
        self.output_root = Path(output_root)
        self.whisper_config = config.get("whisper", {})
        self.diar_config = config.get("diarization", {})
        self._model = None
        self._model_cache = PROJECT_ROOT / "models"

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

        _init_cuda()

        from faster_whisper import WhisperModel

        model_path = self._resolve_model_path()
        device = self.whisper_config.get("device", "cuda")
        compute = self.whisper_config.get("compute_type", "float16")

        try:
            model = WhisperModel(model_path, device=device, compute_type=compute,
                                 download_root=str(self._model_cache))
            print(f"  模型已加载 (device={device}, compute={compute})")
            self._model = model
        except Exception:
            print(f"  GPU 不可用，回退到 CPU")
            model = WhisperModel(model_path, device="cpu", compute_type="int8",
                                 download_root=str(self._model_cache))
            print(f"  模型已加载 (device=cpu, compute=int8)")

        return model

    AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".opus", ".wma"}

    def transcribe(self, input_path, output_folder):
        """转写视频/音频，返回 transcript 文件路径"""
        output_folder = Path(output_folder)
        output_folder.mkdir(parents=True, exist_ok=True)

        input_path = Path(input_path)
        ext = input_path.suffix.lower()
        is_audio = ext in self.AUDIO_EXTS

        safe_name = sanitize_filename(input_path.stem)
        transcript_path = output_folder / f"{safe_name}.txt"

        if transcript_path.exists():
            print(f"  已转写: {safe_name}")
            set_state(output_folder, "transcribed")
            return transcript_path

        file_type = "音频" if is_audio else "视频"
        print(f"  转写中 ({file_type}): {safe_name}")

        # 尝试说话人分离（如已启用）
        if self.diar_config.get("enabled", False):
            try:
                return self._transcribe_with_diarization(
                    input_path, output_folder, safe_name
                )
            except Exception as e:
                print(f"  [WARN] 说话人分离失败，回退到基础转写: {e}")

        # 基础转写路径（faster-whisper）
        if is_audio:
            audio_path = input_path
        else:
            audio_path = output_folder / f"_tmp_audio_{os.getpid()}.mp3"

        try:
            if not is_audio:
                self._extract_audio(input_path, audio_path)

            model = self._get_model()
            language = self.whisper_config.get("language", "zh")
            if language == "auto":
                language = None

            def _do_transcribe(m):
                return m.transcribe(str(audio_path), language=language,
                                    word_timestamps=True)

            try:
                segs, info = _do_transcribe(model)
            except Exception as gpu_err:
                if "cublas" in str(gpu_err).lower() or "cuda" in str(gpu_err).lower():
                    print(f"  GPU 转写失败，回退到 CPU ({gpu_err})")
                    import gc
                    from faster_whisper import WhisperModel
                    self._model = None
                    gc.collect()
                    model = WhisperModel(
                        self._resolve_model_path(), device="cpu", compute_type="int8",
                        download_root=str(self._model_cache))
                    self._model = model
                    segs, info = _do_transcribe(model)
                else:
                    raise

            transcript_text = self._format_transcript(segs)

            with open(transcript_path, "w", encoding="utf-8") as f:
                f.write(f"# {safe_name}\n\n")
                f.write(transcript_text)

            print(f"  已保存: {transcript_path.name}")
            set_state(output_folder, "transcribed")
            return transcript_path

        finally:
            if not is_audio and audio_path.exists():
                audio_path.unlink()

    # ---- 转写文本格式化 ----

    @staticmethod
    def _format_transcript(segments):
        """拼接 ASR 片段为原始文本（标点和分段由 LLM 后处理负责）"""
        lines = []
        for seg in segments:
            text = seg.text.strip()
            if text:
                lines.append(text)
        return "\n".join(lines)

    # ---- whisperX 说话人分离路径 ----

    def _transcribe_with_diarization(self, input_path, output_folder, safe_name):
        """whisperX: 转写 + 时间戳对齐 + 说话人分离"""
        import gc

        # 检查 whisperX 是否安装
        try:
            import whisperx
        except ImportError:
            print("  [WARN] whisperX 未安装。安装命令: pip install whisperx")
            print("  [WARN] Python 3.12+ 可能有依赖冲突，建议 Python 3.10-3.11")
            raise

        # 检查 HF Token
        hf_token = (self.diar_config.get("hf_token") or
                     os.environ.get("HF_TOKEN") or "")
        if not hf_token:
            print("  [WARN] 未设置 HF_TOKEN，说话人分离需要 HuggingFace Token")
            print("  [WARN] 在 .env 中添加 HF_TOKEN=hf_xxx")
            print("  [WARN] 从 https://huggingface.co/settings/tokens 获取 Read token")
            raise ValueError("HF_TOKEN not configured")

        input_path = Path(input_path)
        is_audio = input_path.suffix.lower() in self.AUDIO_EXTS

        if is_audio:
            audio_path = input_path
        else:
            audio_path = output_folder / f"_tmp_audio_{os.getpid()}.mp3"

        try:
            if not is_audio:
                self._extract_audio(input_path, audio_path)

            device = self.whisper_config.get("device", "cuda")
            compute = self.whisper_config.get("compute_type", "float16")
            language = self.whisper_config.get("language", "zh")
            if language == "auto":
                language = None

            # 1. 加载音频
            audio = whisperx.load_audio(str(audio_path))

            # 2. 转写
            print("  转写中 (whisperX)...")
            model_path = self._resolve_model_path()
            model = whisperx.load_model(
                model_path, device=device, compute_type=compute,
                download_root=str(self._model_cache),
                language=language,
            )
            result = model.transcribe(audio, batch_size=16)
            del model
            gc.collect()

            # 3. 词级时间戳对齐
            print("  对齐时间戳...")
            model_a, metadata = whisperx.load_align_model(
                language_code=result["language"], device=device
            )
            result = whisperx.align(result["segments"], model_a, metadata, audio, device)
            del model_a
            gc.collect()

            # 4. 说话人分离
            print("  说话人分离...")
            diarize_model = whisperx.DiarizationPipeline(
                use_auth_token=hf_token, device=device
            )
            diarize_kwargs = {}
            if self.diar_config.get("min_speakers"):
                diarize_kwargs["min_speakers"] = self.diar_config["min_speakers"]
            if self.diar_config.get("max_speakers"):
                diarize_kwargs["max_speakers"] = self.diar_config["max_speakers"]
            diarize_segments = diarize_model(audio, **diarize_kwargs)

            # 5. 分配说话人
            result = whisperx.assign_word_speakers(diarize_segments, result)

            return self._format_diarized_output(result, output_folder, safe_name)

        finally:
            if not is_audio and audio_path.exists():
                audio_path.unlink()

    def _format_diarized_output(self, result, output_folder, safe_name):
        """将 whisperX 结果格式化为带说话人标签的 Markdown"""
        transcript_path = output_folder / f"{safe_name}.txt"

        # 合并同一说话人的连续段落
        merged = []
        for seg in result.get("segments", []):
            speaker = seg.get("speaker", "UNKNOWN")
            text = seg.get("text", "").strip()
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

        lines = [f"# {safe_name}", ""]
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

        content = "\n".join(lines)
        transcript_path.write_text(content, encoding="utf-8")
        print(f"  已保存（含说话人标签）: {transcript_path.name}")
        set_state(output_folder, "transcribed")
        return transcript_path

    @staticmethod
    def _format_time(seconds):
        """浮点秒 → HH:MM:SS.mmm"""
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}"

    # ---- 音频提取 ----

    def _extract_audio(self, video_path, audio_path):
        ffmpeg = find_ffmpeg()
        cmd = [ffmpeg, "-i", str(video_path), "-vn", "-acodec", "mp3",
               "-q:a", "2", "-y", str(audio_path)]
        subprocess.run(cmd, capture_output=True, check=True)
