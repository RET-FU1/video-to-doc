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

    def test_stale_dest_is_reimported(self, tmp_path):
        """回归：dest 已存在但内容不同（上次复制中断/源文件已更新）时必须重新复制。

        旧实现只要 dest.exists() 就直接复用，会静默转写一份半截或过期的文件。
        """
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        src = src_dir / "video.mp4"
        src.write_bytes(b"new content that is longer")
        out = tmp_path / "out"
        folder = out / "video"
        folder.mkdir(parents=True)
        stale = folder / "video.mp4"
        stale.write_bytes(b"old")          # 半截/过期文件

        d = _make_downloader(out)
        dest, _meta = d._import_local(str(src))
        assert dest.read_bytes() == b"new content that is longer", "应重新复制而不是复用残件"

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


class TestUrlDetection:
    """回归：用户从浏览器地址栏复制时经常拿到**没有协议头**的裸域名
    （`bilibili.com/video/BV...`）。旧实现把它当本地文件路径，直接报
    "本地文件不存在" —— 用户实际踩到。"""

    def test_bare_domain_is_url(self):
        d = Downloader({"downloader": {}}, Path("."))
        assert d._is_url("bilibili.com/video/BV1NvJ66gEMn/?trackid=abc&spm_id_from=1.2.3.click")
        assert d._is_url("www.bilibili.com/video/BV1xx")
        assert d._is_url("//bilibili.com/video/BV1xx")
        assert d._is_url("youtube.com/watch?v=abc")

    def test_local_paths_are_not_urls(self):
        d = Downloader({"downloader": {}}, Path("."))
        for p in ("C:\\videos\\a.mp4", "C:/videos/a.mp4", "videos/a.mp4",
                  "a.mp4", "my video file.mkv", "D:\\B站下载\\视频.mp4"):
            assert not d._is_url(p), f"本地路径被误判为 URL: {p}"

    def test_normalize_adds_scheme(self):
        d = Downloader({"downloader": {}}, Path("."))
        assert d._normalize_url("bilibili.com/x") == "https://bilibili.com/x"
        assert d._normalize_url("//bilibili.com/x") == "https://bilibili.com/x"
        assert d._normalize_url("https://a.com/x") == "https://a.com/x"

    def test_extract_from_share_text_without_scheme(self):
        d = Downloader({"downloader": {}}, Path("."))
        got = d._extract_url("【标题】看看这个 bilibili.com/video/BV1NvJ66gEMn/?trackid=abc 很有意思")
        assert got == "https://bilibili.com/video/BV1NvJ66gEMn/?trackid=abc"

    def test_extract_with_scheme(self):
        d = Downloader({"downloader": {}}, Path("."))
        assert d._extract_url("https://www.bilibili.com/video/BV1x 复制打开") == \
            "https://www.bilibili.com/video/BV1x"

    def test_bare_url_does_not_go_to_import_local(self, tmp_path, monkeypatch):
        """核心回归：裸域名必须走网络下载路径，而不是被当成本地文件"""
        d = _make_downloader(tmp_path / "out")
        called = {"import": 0, "meta": None}

        monkeypatch.setattr(d, "_import_local",
                            lambda *a, **k: called.__setitem__("import", called["import"] + 1))

        def fake_fetch(url):
            called["meta"] = url
            raise RuntimeError("到这里说明 URL 已被正确识别")

        monkeypatch.setattr(d, "_fetch_meta", fake_fetch)
        try:
            d.download("bilibili.com/video/BV1NvJ66gEMn/?trackid=abc")
        except RuntimeError:
            pass
        assert called["import"] == 0, "裸域名不应走本地文件导入"
        assert called["meta"] == "https://bilibili.com/video/BV1NvJ66gEMn/?trackid=abc"

class TestFormatSelection:
    """format_selection 是纯函数，单测覆盖候选链的排序与兜底逻辑"""

    META = {"formats": [
        # 1080p：AVC / HEVC / AV1 三条流（模拟 B站）
        {"format_id": "30080", "vcodec": "avc1.640033", "acodec": "none",
         "height": 1080, "url": "u8", "filesize": 160 * 1024 * 1024},
        {"format_id": "30077", "vcodec": "hvc1.1.6.L15", "acodec": "none",
         "height": 1080, "url": "u7", "filesize": 74 * 1024 * 1024},
        {"format_id": "100026", "vcodec": "av01.0.08M.0", "acodec": "none",
         "height": 1080, "url": "u26", "filesize": 64 * 1024 * 1024},
        # 720p
        {"format_id": "30064", "vcodec": "avc1.640033", "acodec": "none",
         "height": 720, "url": "u64", "filesize": 72 * 1024 * 1024},
        # 音频
        {"format_id": "30280", "vcodec": "none", "acodec": "mp4a.40.2",
         "url": "ua", "filesize": 7 * 1024 * 1024},
    ]}

    def test_best_order_keeps_quality_then_codec(self):
        from format_selection import candidates_from_meta
        cand = candidates_from_meta("best", self.META)
        # 1080p 三种编码应排在 720p 之前
        assert cand[0] == "30080+30280", "首选应为 1080p AVC"
        assert cand[1] == "30077+30280", "其次 1080p HEVC（换 CDN 但保持画质）"
        assert cand[2] == "100026+30280", "再次 1080p AV1"
        assert cand[3].startswith("30064"), "画质降级应排在这些之后"

    def test_fallback_chain_tail(self):
        from format_selection import candidates_from_meta
        cand = candidates_from_meta("best", self.META)
        assert cand[-1] == "bestvideo+bestaudio/best", "末尾应有兜底表达式"

    def test_height_cap(self):
        from format_selection import candidates_from_meta
        cand = candidates_from_meta("720p", self.META)
        assert all(not c.startswith(("30080", "30077", "100026")) for c in cand), \
            "720p 预设不应包含 1080p 流"

    def test_audio_only(self):
        from format_selection import candidates_from_meta
        cand = candidates_from_meta("audio", self.META)
        assert cand == ["30280"]

    def test_user_format_override_splits(self):
        from format_selection import format_candidates
        cand = format_candidates("best", format_cfg="30080+30280/30077+30280")
        assert cand == ["30080+30280", "30077+30280"]

    def test_no_meta_falls_back_to_static(self):
        from format_selection import format_candidates, quality_to_format
        cand = format_candidates("1080p", meta={})
        assert cand == [p.strip() for p in quality_to_format("1080p").split("/") if p.strip()]

    def test_candidate_cap(self):
        from format_selection import MAX_FORMAT_CANDIDATES, candidates_from_meta
        many = {"formats": [
            {"format_id": f"v{i}", "vcodec": "avc1", "acodec": "none",
             "height": 1080 - i, "url": "u", "filesize": 1}
            for i in range(20)
        ] + [{"format_id": "a", "vcodec": "none", "acodec": "mp4a", "url": "u", "filesize": 1}]}
        cand = candidates_from_meta("best", many)
        # 上限 + 末尾兜底
        assert len(cand) <= MAX_FORMAT_CANDIDATES + 1
