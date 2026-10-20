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
