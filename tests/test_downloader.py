"""下载 / 转写边界情况单元测试（不联网）

覆盖这次修复的两个真事故：
- 本地文件"重新导入"到它自己所在目录时会和自己冲突（WinError 32）
- 只下载音频（quality: audio）时 _find_video 找不到文件
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from downloader import Downloader
from utils import AUDIO_EXTS


def _make_downloader(tmp: Path) -> Downloader:
    return Downloader({"output_dir": str(tmp)}, tmp)


class TestFindVideo:
    def test_finds_video(self, tmp_path):
        (tmp_path / "a.mp4").write_bytes(b"x")
        d = _make_downloader(tmp_path)
        assert d._find_video(tmp_path) == tmp_path / "a.mp4"

    def test_finds_audio_only(self, tmp_path):
        """quality: audio 时只下载 m4a/mp3，旧实现会返回 None 并报错"""
        (tmp_path / "a.m4a").write_bytes(b"x")
        d = _make_downloader(tmp_path)
        found = d._find_video(tmp_path)
        assert found is not None and found.suffix in AUDIO_EXTS

    def test_prefers_video_over_audio(self, tmp_path):
        (tmp_path / "a.mp3").write_bytes(b"x")
        (tmp_path / "a.mp4").write_bytes(b"x")
        d = _make_downloader(tmp_path)
        assert d._find_video(tmp_path).suffix == ".mp4"

    def test_recursive_search(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "v.mp4").write_bytes(b"x")
        assert _make_downloader(tmp_path)._find_video(tmp_path) is not None

    def test_empty_dir(self, tmp_path):
        assert _make_downloader(tmp_path)._find_video(tmp_path) is None


class TestImportLocal:
    def test_copies_into_output(self, tmp_path):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        src = src_dir / "video.mp4"
        src.write_bytes(b"content")
        out = tmp_path / "out"
        d = _make_downloader(out)
        dest, meta = d._import_local(str(src))
        assert dest.exists() and dest.read_bytes() == b"content"
        assert meta["uploader"] == "本地文件"

    def test_reimport_into_same_folder_is_noop(self, tmp_path):
        """把 output 里的文件再喂回去：不能试图把自己复制到自己身上"""
        out = tmp_path / "out"
        folder = out / "video"
        folder.mkdir(parents=True)
        existing = folder / "video.mp4"
        existing.write_bytes(b"content")
        d = _make_downloader(out)
        dest, _meta = d._import_local(str(existing))
        assert dest == existing
        assert dest.read_bytes() == b"content"

    def test_missing_file_raises(self, tmp_path):
        d = _make_downloader(tmp_path / "out")
        try:
            d._import_local(str(tmp_path / "nope.mp4"))
        except FileNotFoundError:
            return
        raise AssertionError("应当抛 FileNotFoundError")


class TestQualityToFormat:
    def test_audio_preset(self):
        fmt = Downloader._quality_to_format("audio")
        assert "bestaudio" in fmt

    def test_unknown_quality_falls_back(self):
        assert Downloader._quality_to_format("nonsense") != ""


class TestExtractUrl:
    def test_extracts_from_share_text(self):
        text = "看看这个 https://www.bilibili.com/video/BV1xx411x7xx 超好看"
        assert Downloader._extract_url(text) == "https://www.bilibili.com/video/BV1xx411x7xx"

    def test_no_url(self):
        assert Downloader._extract_url("没有链接") is None
