"""音轨校验测试（不联网）

背景：一次真实事故 —— 下载产物只有视频轨、没有音轨，转写阶段 ffmpeg 报
"Output file does not contain any stream / Invalid argument"，用户完全看不懂。
根因是 B站 音视频分两条流，只下到视频流时文件"看起来下载成功"。

修复分两层：
1. transcriber 提取音频前先探测音轨，给出可读原因
2. downloader 下载完校验音轨，没有就丢弃重试
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from transcriber import Transcriber
from downloader import Downloader


def _ffmpeg_available() -> bool:
    try:
        from utils import find_ffmpeg
        find_ffmpeg()
        return True
    except FileNotFoundError:
        return False


def _make_video(path: Path, with_audio: bool) -> Path:
    """用 ffmpeg 生成测试视频（0.5 秒纯色 + 可选正弦音）"""
    from utils import find_ffmpeg
    ffmpeg = find_ffmpeg()
    cmd = [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.5"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
                "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(path)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    return path


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_available(),
                                  reason="需要 ffmpeg")


@needs_ffmpeg
class TestHasAudioStream:
    def test_video_with_audio(self, tmp_path):
        video = _make_video(tmp_path / "with_audio.mp4", with_audio=True)
        assert Transcriber._has_audio_stream(video) is True

    def test_video_without_audio(self, tmp_path):
        video = _make_video(tmp_path / "no_audio.mp4", with_audio=False)
        assert Transcriber._has_audio_stream(video) is False

    def test_nonexistent_file_is_permissive(self, tmp_path):
        """探测不到时保守返回 True，不要把「tool 缺失」误判成「没音轨」"""
        assert Transcriber._has_audio_stream(tmp_path / "nope.mp4") is True


@needs_ffmpeg
class TestExtractAudioError:
    def test_no_audio_gives_readable_error(self, tmp_path):
        video = _make_video(tmp_path / "no_audio.mp4", with_audio=False)
        t = Transcriber({"whisper": {"audio_format": "wav"}}, tmp_path)
        try:
            t._extract_audio(video, tmp_path / "out.wav")
        except RuntimeError as e:
            msg = str(e)
            assert "没有音轨" in msg, f"错误信息应说清原因，实际: {msg}"
            assert "重新下载" in msg, "错误信息应给出解决办法"
            return
        raise AssertionError("无音轨视频应当抛 RuntimeError")

    def test_with_audio_extracts_ok(self, tmp_path):
        video = _make_video(tmp_path / "with_audio.mp4", with_audio=True)
        t = Transcriber({"whisper": {"audio_format": "wav"}}, tmp_path)
        out = tmp_path / "out.wav"
        t._extract_audio(video, out)
        assert out.exists() and out.stat().st_size > 0


class TestDownloaderAudioCheck:
    def test_audio_file_always_passes(self, tmp_path):
        d = Downloader({"downloader": {}}, tmp_path)
        assert d._ensure_has_audio(tmp_path / "x.m4a", tmp_path) is True
        assert d._ensure_has_audio(tmp_path / "x.mp3", tmp_path) is True

    @needs_ffmpeg
    def test_video_without_audio_rejected(self, tmp_path):
        d = Downloader({"downloader": {}}, tmp_path)
        video = _make_video(tmp_path / "no_audio.mp4", with_audio=False)
        assert d._ensure_has_audio(video, tmp_path) is False

    @needs_ffmpeg
    def test_video_with_audio_accepted(self, tmp_path):
        d = Downloader({"downloader": {}}, tmp_path)
        video = _make_video(tmp_path / "with_audio.mp4", with_audio=True)
        assert d._ensure_has_audio(video, tmp_path) is True
