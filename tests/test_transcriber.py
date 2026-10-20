"""transcriber.py 复用分支测试（不联网、不加载模型）

覆盖一条真实的 P0 回归：output 目录里只有 `_segments.json`、`.txt` 已被
`_cleanup` 删掉时（这是"跑完一次"的常见状态），transcribe() 必须能复用
已有段落数据重建文本，而不是重跑 ASR 或者直接崩溃。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from transcriber import Transcriber


def _transcriber(tmp: Path) -> Transcriber:
    # 只构造，不建模型：复用分支不会碰 _get_model()
    return Transcriber({"whisper": {"language": "zh"}}, tmp)


class TestRebuildFromSegments:
    def test_has_rebuild_method(self):
        assert hasattr(Transcriber, "_write_transcript_from_segments")

    def test_rebuild_writes_txt(self, tmp_path):
        seg = tmp_path / "v_segments.json"
        seg.write_text(json.dumps([
            {"start": 0, "end": 1, "text": "第一段"},
            {"start": 1, "end": 2, "text": "第二段"},
        ]), encoding="utf-8")
        out = tmp_path / "v.txt"
        Transcriber._write_transcript_from_segments(seg, out, "v")
        content = out.read_text(encoding="utf-8")
        assert content.startswith("# v")
        assert "第一段" in content and "第二段" in content

    def test_transcribe_reuses_segments_without_model(self, tmp_path):
        """只有 segments、没有 .txt：不应加载模型，而应重建 .txt 并返回"""
        (tmp_path / "v.m4a").write_bytes(b"fake-audio")
        (tmp_path / "v_segments.json").write_text(json.dumps([
            {"start": 0, "end": 1, "text": "已有转写"},
        ]), encoding="utf-8")
        t = _transcriber(tmp_path)
        result = t.transcribe(str(tmp_path / "v.m4a"), str(tmp_path))
        assert result == tmp_path / "v.txt"
        assert "已有转写" in result.read_text(encoding="utf-8")
        # 关键：没有加载模型（模型加载会抛错/耗时）
        assert t._model is None

    def test_transcribe_reuses_existing_txt_and_segments(self, tmp_path):
        (tmp_path / "v.m4a").write_bytes(b"fake-audio")
        (tmp_path / "v_segments.json").write_text(json.dumps([
            {"start": 0, "end": 1, "text": "x"},
        ]), encoding="utf-8")
        (tmp_path / "v.txt").write_text("# v\n\nx", encoding="utf-8")
        t = _transcriber(tmp_path)
        result = t.transcribe(str(tmp_path / "v.m4a"), str(tmp_path))
        assert result.read_text(encoding="utf-8") == "# v\n\nx"
        assert t._model is None


class TestConfigMapping:
    def test_kwargs_contain_new_config_keys(self):
        t = Transcriber({
            "whisper": {
                "language": "zh",
                "beam_size": 3,
                "condition_on_previous_text": False,
                "audio_format": "wav",
            }
        }, Path("."))
        kwargs = t._transcribe_kwargs()
        assert kwargs["language"] == "zh"
        assert kwargs["beam_size"] == 3
        assert kwargs["condition_on_previous_text"] is False

    def test_auto_language_becomes_none(self):
        t = Transcriber({"whisper": {"language": "auto"}}, Path("."))
        assert t._transcribe_kwargs()["language"] is None

    def test_audio_format_fallback(self):
        t = Transcriber({"whisper": {"audio_format": "flac"}}, Path("."))
        assert t._audio_format() == "wav"

    def test_kwargs_are_valid_faster_whisper_args(self):
        """参数名必须真的存在于 faster-whisper 的 transcribe() 签名里"""
        import inspect
        from faster_whisper import WhisperModel
        valid = set(inspect.signature(WhisperModel.transcribe).parameters)
        t = Transcriber({"whisper": {}}, Path("."))
        invalid = [k for k in t._transcribe_kwargs() if k not in valid]
        assert invalid == [], f"传给 faster-whisper 的非法参数: {invalid}"
