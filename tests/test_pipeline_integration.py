"""pipeline._process_one 主路径集成测试（不联网、不调用真实模型/API）

复核者指出：127 个测试里没有任何一条真正走 `_process_one`，导致
「transcriber 调用了不存在的方法」这种 P0 回归测不出来。这个文件补上
主路径覆盖：用假 downloader / transcriber / summarizer，跑完整条流水线，
断言产物文件、断点续跑语义、以及各阶段是否被正确调用。

设计原则：假实现只模拟"契约"，不模拟内部实现——所以只要真实代码调用了
不存在的方法或传错参数，这里就会 AttributeError/TypeError。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline import Pipeline, STYLE_CN


class FakeTranscriber:
    """按契约写 segments.json + .txt，返回 .txt 路径"""

    def __init__(self, texts):
        self.texts = texts
        self.calls = 0

    def transcribe(self, video_path: Path, folder: Path) -> Path:
        self.calls += 1
        stem = Path(video_path).stem
        segments = [
            {"start": i * 2.0, "end": i * 2.0 + 2.0, "text": t,
             "avg_logprob": -0.1, "no_speech_prob": 0.01, "compression_ratio": 1.0}
            for i, t in enumerate(self.texts)
        ]
        (folder / f"{stem}_segments.json").write_text(
            json.dumps(segments, ensure_ascii=False), encoding="utf-8")
        txt = folder / f"{stem}.txt"
        txt.write_text("# 标题\n\n" + "\n".join(self.texts), encoding="utf-8")
        return txt


class FakeSummarizer:
    """polish 只加标点；summarize 返回固定 markdown"""

    client = object()
    model = "fake-model"

    def __init__(self):
        self.polish_calls = 0
        self.summarize_calls = []

    def polish(self, text, context=""):
        self.polish_calls += 1
        return "\n\n".join(
            line.split("] ", 1)[-1] + "。" for line in text.split("\n") if line.strip()
        )

    def polish_multispeaker(self, text, context=""):
        self.polish_calls += 1
        return "\n\n".join(
            "说话人A：" + line.split("] ", 1)[-1] + "。"
            for line in text.split("\n") if line.strip()
        )

    def summarize(self, text, meta, style="auto"):
        self.summarize_calls.append(style)
        return f"# 总结（{style}）\n\n这是总结正文。"


def make_pipeline(tmp_path, *, formats=("md",), styles=None, texts=None,
                  srt=False, skip_summary=False, translate=False,
                  multi_speaker=False, summarizer=None):
    cfg = {
        "output_dir": str(tmp_path),
        "summarizer": {
            "output_formats": list(formats),
            "summary_styles": styles or ["auto"],
            "multi_speaker": multi_speaker,
        },
        "translation": {"target_lang": "zh"},
        "whisper": {"language": "zh"},
    }
    p = Pipeline.__new__(Pipeline)
    p.config = cfg
    p.output_root = tmp_path
    p._translate = translate
    p._srt = srt
    p._skip_summary = skip_summary
    p._translator = None
    p.transcriber = FakeTranscriber(texts or ["第一句话", "第二句话", "第三句话"])
    p.summarizer = summarizer or FakeSummarizer()
    from utils import CancelToken
    p._cancel = CancelToken()
    p.downloader = None
    return p


def _prepare_video(tmp_path, stem="视频"):
    folder = tmp_path / stem
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / f"{stem}.mp4"
    video.write_bytes(b"fake-video")
    return video, folder


class TestProcessOneMainPath:
    def test_md_only(self, tmp_path):
        p = make_pipeline(tmp_path)
        video, folder = _prepare_video(tmp_path)
        p._process_one(video, {"title": "测试标题"})

        md = folder / "视频.md"
        assert md.exists(), "应产出转写 md"
        content = md.read_text(encoding="utf-8")
        assert content.startswith("# 测试标题")
        assert "第一句话" in content
        assert (folder / "全面总结-视频.md").exists()
        # 清理：txt 被删，segments 保留
        assert not (folder / "视频.txt").exists()
        assert (folder / "视频_segments.json").exists()
        assert (folder / ".pipeline_state").read_text(encoding="utf-8") == "done"

    def test_keep_txt_format(self, tmp_path):
        p = make_pipeline(tmp_path, formats=("txt",))
        video, folder = _prepare_video(tmp_path)
        p._process_one(video, {"title": "T"})
        assert (folder / "视频.txt").exists()

    def test_srt_generated_from_segments(self, tmp_path):
        p = make_pipeline(tmp_path, srt=True)
        video, folder = _prepare_video(tmp_path)
        p._process_one(video, {"title": "T"})
        srt = folder / "视频.srt"
        assert srt.exists()
        body = srt.read_text(encoding="utf-8")
        assert "00:00:00,000 -->" in body
        for t in ("第一句话", "第二句话", "第三句话"):
            assert t in body, f"字幕丢失了: {t}"

    def test_multiple_styles(self, tmp_path):
        p = make_pipeline(tmp_path, styles=["auto", "expert"])
        video, folder = _prepare_video(tmp_path)
        p._process_one(video, {"title": "T"})
        assert (folder / f"{STYLE_CN['auto']}-视频.md").exists()
        assert (folder / f"{STYLE_CN['expert']}-视频.md").exists()
        assert sorted(p.summarizer.summarize_calls) == ["auto", "expert"]

    def test_skip_summary(self, tmp_path):
        p = make_pipeline(tmp_path, skip_summary=True)
        video, folder = _prepare_video(tmp_path)
        p._process_one(video, {"title": "T"})
        assert (folder / "视频.md").exists()
        assert not list(folder.glob("全面总结-*.md"))
        assert p.summarizer.summarize_calls == []

    def test_html_format(self, tmp_path):
        p = make_pipeline(tmp_path, formats=("html",))
        video, folder = _prepare_video(tmp_path)
        p._process_one(video, {"title": "T"})
        html = folder / "视频.html"
        assert html.exists()
        assert "<html" in html.read_text(encoding="utf-8").lower()

    def test_empty_formats_raises(self, tmp_path):
        p = make_pipeline(tmp_path, formats=())
        video, _folder = _prepare_video(tmp_path)
        try:
            p._process_one(video, {"title": "T"})
        except ValueError as e:
            assert "输出格式" in str(e)
            return
        raise AssertionError("空输出格式应当抛 ValueError")

    def test_empty_styles_fall_back_to_auto(self, tmp_path):
        p = make_pipeline(tmp_path, styles=[])
        video, folder = _prepare_video(tmp_path)
        p._process_one(video, {"title": "T"})
        assert p.summarizer.summarize_calls == ["auto"]

    def test_chapters_injected_into_transcript(self, tmp_path):
        p = make_pipeline(tmp_path, texts=[f"第{i}句话" for i in range(10)])
        # polish 会保留 "## " 行吗？FakeSummarizer 只保留正文，所以这里验证兜底清单
        video, folder = _prepare_video(tmp_path)
        meta = {"title": "带章节", "_chapters": [
            {"start": 0.0, "end": 10.0, "title": "开场"},
            {"start": 10.0, "end": 20.0, "title": "第二部分"},
        ]}
        p._process_one(video, meta)
        md = (folder / "视频.md").read_text(encoding="utf-8")
        # 不管 LLM 是否保留，最终文档里都应有这两个标题
        assert "开场" in md
        assert "第二部分" in md

    def test_multispeaker_labels_survive(self, tmp_path):
        p = make_pipeline(tmp_path, multi_speaker=True)
        video, folder = _prepare_video(tmp_path)
        p._process_one(video, {"title": "T"})
        md = (folder / "视频.md").read_text(encoding="utf-8")
        assert "说话人A" in md, "多说话人标签被吃掉了"


class TestResumeSemantics:
    def test_second_run_skips(self, tmp_path):
        p = make_pipeline(tmp_path)
        video, folder = _prepare_video(tmp_path)
        p._process_one(video, {"title": "T"})
        transcribe_calls = p.transcriber.calls

        p._process_one(video, {"title": "T"})
        assert p.transcriber.calls == transcribe_calls, "第二次运行不应重复转写"
        assert (folder / ".pipeline_state").exists(), "跳过时不应删除状态"

    def test_adding_style_or_format_backfills(self, tmp_path):
        """回归：先跑 auto/md，再改成 auto+expert、md+txt 重跑。

        旧实现只看 styles[0]+formats[0] 的产物是否存在，于是直接"已完成，跳过"，
        expert 总结和 txt 文件永远不会生成。现在必须校验**全部**请求的产物。
        """
        p = make_pipeline(tmp_path, styles=["auto"], formats=("md",))
        video, folder = _prepare_video(tmp_path)
        p._process_one(video, {"title": "T"})
        assert not (folder / f"{STYLE_CN['expert']}-视频.md").exists()

        # 同目录、同视频，但这次要多生成一个风格和一个格式
        p2 = make_pipeline(tmp_path, styles=["auto", "expert"], formats=("md", "txt"))
        p2._process_one(video, {"title": "T"})

        assert (folder / f"{STYLE_CN['expert']}-视频.md").exists(), "新增风格应被补齐"
        assert (folder / f"{STYLE_CN['auto']}-视频.txt").exists(), "新增格式应被补齐"

    def test_missing_srt_triggers_rerun(self, tmp_path):
        """回归：勾了生成字幕但 .srt 丢了，重跑必须补出来而不是"已完成，跳过"。"""
        p = make_pipeline(tmp_path, srt=True)
        video, folder = _prepare_video(tmp_path)
        p._process_one(video, {"title": "T"})
        srt = folder / "视频.srt"
        assert srt.exists(), "首次运行应生成 SRT"
        srt.unlink()

        calls_before = p.transcriber.calls
        p._process_one(video, {"title": "T"})
        assert srt.exists(), "SRT 缺失时重跑应补生成"

    def test_delete_output_reruns_without_asr(self, tmp_path):
        """删掉 md 重跑：应复用 segments 重建，不重跑 ASR"""
        p = make_pipeline(tmp_path)
        video, folder = _prepare_video(tmp_path)
        p._process_one(video, {"title": "T"})
        (folder / "视频.md").unlink()
        (folder / f"{STYLE_CN['auto']}-视频.md").unlink()

        real = FakeTranscriber(["第一句话", "第二句话", "第三句话"])
        # 模拟真实 transcriber 的复用语义：segments 存在就直接返回
        def transcribe(video_path, out_folder):
            seg = Path(out_folder) / f"{Path(video_path).stem}_segments.json"
            txt = Path(out_folder) / f"{Path(video_path).stem}.txt"
            if seg.exists():
                real.calls += 1
                txt.write_text("# 标题\n\n" + "\n".join(real.texts), encoding="utf-8")
                return txt
            return real.transcribe(video_path, out_folder)

        p.transcriber.transcribe = transcribe
        p._process_one(video, {"title": "T"})
        assert (folder / "视频.md").exists()


class TestPolishInputIntegration:
    def test_timestamps_reach_polish_and_are_stripped(self, tmp_path):
        """抛光输入带 [MM:SS]，最终文档里不能出现时间戳"""
        captured = {}

        class CapturingSummarizer(FakeSummarizer):
            def polish(self, text, context=""):
                captured["input"] = text
                return super().polish(text, context)

        p = make_pipeline(tmp_path, summarizer=CapturingSummarizer())
        video, folder = _prepare_video(tmp_path)
        p._process_one(video, {"title": "T"})
        assert "[00:00]" in captured["input"], "抛光输入应当带时间戳"
        md = (folder / "视频.md").read_text(encoding="utf-8")
        import re
        assert not re.search(r"\[\d{1,3}:\d{2}\]", md), "最终文档不应残留时间戳"


class TestBatchFailureCount:
    """复核者指出：process_folder / _process_playlist 以前对单个文件失败只打
    [FAIL] 然后继续，最后仍打"全部完成"，GUI 无从得知有失败。"""

    def test_process_folder_counts_failures(self, tmp_path, monkeypatch):
        p = make_pipeline(tmp_path)
        src = tmp_path / "batch"
        src.mkdir()
        for name in ("a.mp4", "b.mp4", "c.mp4"):
            (src / name).write_bytes(b"x")

        calls = {"n": 0}

        def fake_process_one(video_path, meta):
            calls["n"] += 1
            if video_path.stem in ("b", "c"):
                raise RuntimeError("模拟失败")

        class _StubDownloader:
            def _import_local(self, path, output_subdir=None):
                return Path(path), {"title": Path(path).stem}

        p.downloader = _StubDownloader()
        monkeypatch.setattr(p, "_process_one", fake_process_one)
        monkeypatch.setattr(p, "_collect_outputs", lambda *a, **k: None)
        p.process_folder(str(src))

        assert calls["n"] == 3
        assert p.last_failures == 2, f"应记录 2 个失败，实际 {p.last_failures}"

    def test_process_folder_all_ok_reports_zero(self, tmp_path, monkeypatch):
        p = make_pipeline(tmp_path)
        src = tmp_path / "batch"
        src.mkdir()
        (src / "a.mp4").write_bytes(b"x")
        class _StubDownloader:
            def _import_local(self, path, output_subdir=None):
                return Path(path), {"title": Path(path).stem}

        p.downloader = _StubDownloader()
        monkeypatch.setattr(p, "_process_one", lambda v, m: None)
        monkeypatch.setattr(p, "_collect_outputs", lambda *a, **k: None)
        p.process_folder(str(src))
        assert p.last_failures == 0


class TestPlaylistFailureReporting:
    """回归：播放列表里某一集**下载失败**时，以前不计入失败数，
    界面仍显示"全部完成"；汇总目录也用 `parent.parent` 猜，原生多集
    下载分支会猜成 output 根目录，把历史任务也汇进来。"""

    @staticmethod
    def _fake_playlist(tmp_path, ok=("ep1",), failed=2):
        from downloader import PlaylistResult
        playlist_dir = tmp_path / "播放列表"
        items = []
        for stem in ok:
            folder = playlist_dir / stem
            folder.mkdir(parents=True, exist_ok=True)
            video = folder / f"{stem}.mp4"
            video.write_bytes(b"fake")
            items.append((video, {"title": stem}))
        return PlaylistResult(items, failed, playlist_dir), playlist_dir

    def test_download_failures_are_counted(self, tmp_path, monkeypatch):
        p = make_pipeline(tmp_path)
        res, playlist_dir = self._fake_playlist(tmp_path, ok=("ep1",), failed=2)
        p.downloader = type("D", (), {"download_playlist": lambda self, url: res})()
        monkeypatch.setattr(p, "_process_one", lambda v, m: None)
        monkeypatch.setattr(p, "_collect_outputs", lambda d: None)

        p._process_playlist("http://x")

        assert p.last_failures == 2, (
            f"下载阶段失败的 2 集应计入失败数，实际 {p.last_failures}")

    def test_download_and_process_failures_add_up(self, tmp_path, monkeypatch):
        p = make_pipeline(tmp_path)
        res, _ = self._fake_playlist(tmp_path, ok=("ep1", "ep2"), failed=1)
        p.downloader = type("D", (), {"download_playlist": lambda self, url: res})()

        def fake_process_one(video_path, meta):
            if video_path.stem == "ep2":
                raise RuntimeError("模拟转写失败")

        monkeypatch.setattr(p, "_process_one", fake_process_one)
        monkeypatch.setattr(p, "_collect_outputs", lambda d: None)

        p._process_playlist("http://x")
        assert p.last_failures == 2, "1 个下载失败 + 1 个转写失败 = 2"

    def test_collect_uses_folder_from_downloader(self, tmp_path, monkeypatch):
        """汇总目录必须来自 downloader，不能靠 parent.parent 猜"""
        p = make_pipeline(tmp_path)
        res, playlist_dir = self._fake_playlist(tmp_path, ok=("ep1",), failed=0)
        p.downloader = type("D", (), {"download_playlist": lambda self, url: res})()
        monkeypatch.setattr(p, "_process_one", lambda v, m: None)
        collected = {}
        monkeypatch.setattr(p, "_collect_outputs",
                            lambda d: collected.setdefault("dir", d))

        p._process_playlist("http://x")
        assert collected["dir"] == playlist_dir


class TestCollectOutputsLayouts:
    """_collect_outputs 要同时支持两种布局：
    逐条下载/文件夹模式（每个视频一个子目录）与原生多集下载（文件平铺）。"""

    def _prepare(self, tmp_path, flat: bool):
        p = make_pipeline(tmp_path, formats=("md",))
        group = tmp_path / "组"
        group.mkdir(parents=True, exist_ok=True)
        target = group if flat else group / "ep1"
        target.mkdir(parents=True, exist_ok=True)
        (target / "ep1.md").write_text("转写", encoding="utf-8")
        (target / "全面总结-ep1.md").write_text("总结", encoding="utf-8")
        (target / "ep1.mp4").write_bytes(b"video")     # 视频不该被汇总
        return p, group

    def test_nested_layout(self, tmp_path):
        p, group = self._prepare(tmp_path, flat=False)
        p._collect_outputs(group)
        assert (group / "转写汇总" / "ep1.md").exists()
        assert (group / "总结汇总" / "全面总结-ep1.md").exists()

    def test_flat_layout(self, tmp_path):
        """原生多集下载时文件平铺，以前一个都收不到"""
        p, group = self._prepare(tmp_path, flat=True)
        p._collect_outputs(group)
        assert (group / "转写汇总" / "ep1.md").exists(), "平铺布局的转写也应被汇总"
        assert (group / "总结汇总" / "全面总结-ep1.md").exists()

    def test_video_files_not_collected(self, tmp_path):
        p, group = self._prepare(tmp_path, flat=True)
        p._collect_outputs(group)
        assert not (group / "转写汇总" / "ep1.mp4").exists()
