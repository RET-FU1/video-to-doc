"""pipeline.py 关键逻辑单元测试（不联网、不调 API）

覆盖的是这次修复的核心缺陷：
- 分块抛光合并不得丢内容 / 不得重复
- 章节注入必须能在抛光后保留
- 行与时间戳的对齐不得静默错位
- 保真校验（长度 / 相似度）能拦住被改写的输出
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline import Pipeline, STYLE_CN, _normalize_for_match


class TestStripEchoedContext:
    def test_no_echo_keeps_everything(self):
        acc = "第一段内容。第二段内容。"
        new = "全新的一段，和前面毫无重叠。"
        assert Pipeline._strip_echoed_context(acc, new) == new

    def test_full_echo_removed(self):
        acc = "前面的内容。这是重叠的上下文部分，长度足够触发去重逻辑。"
        overlap = "这是重叠的上下文部分，长度足够触发去重逻辑。"
        new = overlap + "后面是真正的新内容。"
        out = Pipeline._strip_echoed_context(acc, new)
        assert "这是重叠的上下文" not in out
        assert out == "后面是真正的新内容。"

    def test_short_overlap_not_stripped(self):
        # 少于 20 字的巧合重复不该被切掉，否则会误删正文
        acc = "他说构图很重要"
        new = "他说构图很重要，但这只是开始。"
        assert Pipeline._strip_echoed_context(acc, new) == new

    def test_punctuation_difference_still_matches(self):
        acc = "规则这个词本身就奠定了基调，为什么？因为规则就是用来约束的。"
        new = "规则这个词本身就奠定了基调为什么因为规则就是用来约束的，接下来是新的内容。"
        out = Pipeline._strip_echoed_context(acc, new)
        assert out.startswith("接下来是新的内容")


class TestVerifyPolish:
    def test_identical_passes(self):
        src = "这是一段用于测试的文本，内容需要保持一致。" * 10
        assert Pipeline._verify_polish(src, src) == src

    def test_added_punctuation_passes(self):
        src = "\n".join(["这是第一句话", "这是第二句话", "这是第三句话"] * 20)
        polished = src.replace("\n", "，") + "。"
        assert Pipeline._verify_polish(src, polished) == polished

    def test_truncated_output_rejected(self):
        src = "正常内容。" * 100
        out = "正常内容。" * 10
        assert Pipeline._verify_polish(src, out) == src

    def test_paraphrased_output_rejected(self):
        src = "摄影是光的艺术这个观点在今天的讨论中被反复提到并且展开了详细论证。" * 5
        out = "摄影属于光线的艺术，这一说法本次谈话里多次出现，并进行了详尽论述。" * 5
        assert Pipeline._verify_polish(src, out) == src

    def test_empty_source(self):
        assert Pipeline._verify_polish("", "任何输出") == "任何输出"


class TestPolishMerge:
    """用假 summarizer 替身验证合并逻辑：纯函数层面断言不丢字、不重复"""

    class _EchoSummarizer:
        """模拟真实 LLM 行为：会回显上下文（这是旧代码丢内容的主因）"""

        def polish(self, text, context=""):
            body = text
            if context:
                # 模拟"把上下文并进第一段"这种真实行为
                return context + "。" + body.replace("\n", "") + "。"
            return body.replace("\n", "") + "。"

        def polish_multispeaker(self, text, context=""):
            return self.polish(text, context)

    class _FaithfulSummarizer:
        def polish(self, text, context=""):
            return text.replace("\n", "，") + "。"

        def polish_multispeaker(self, text, context=""):
            return self.polish(text, context)

    def _make_pipeline(self, summarizer):
        p = Pipeline.__new__(Pipeline)
        p.summarizer = summarizer
        p.config = {"summarizer": {}}

        class _Tok:
            def check(self):
                return None

        p._cancel = _Tok()
        return p

    def test_echoing_llm_does_not_lose_content(self):
        lines = [f"第{i}句话的内容在这里" for i in range(300)]
        src = "\n".join(lines)
        p = self._make_pipeline(self._EchoSummarizer())
        out = p._polish_transcript(src)

        norm_src = _normalize_for_match(src)
        norm_out = _normalize_for_match(out)
        # 不丢字：输出应覆盖原文 95% 以上
        assert len(norm_out) >= len(norm_src) * 0.95, (
            f"原文 {len(norm_src)} 字，输出仅 {len(norm_out)} 字，发生丢字"
        )

    def test_faithful_llm_output_equals_source(self):
        lines = [f"第{i}句话的内容在这里" for i in range(300)]
        src = "\n".join(lines)
        p = self._make_pipeline(self._FaithfulSummarizer())
        out = p._polish_transcript(src)
        assert _normalize_for_match(out) == _normalize_for_match(src)

    def test_no_duplication_at_boundaries(self):
        lines = [f"第{i}号句子的正文内容" for i in range(300)]
        src = "\n".join(lines)
        p = self._make_pipeline(self._EchoSummarizer())
        out = p._polish_transcript(src)
        norm_out = _normalize_for_match(out)
        # 不重复：输出不应显著长于原文
        assert len(norm_out) <= len(_normalize_for_match(src)) * 1.15, (
            "分块边界出现内容重复"
        )


class TestBuildPolishInput:
    @staticmethod
    def _segments(n, step=3.0):
        return [{"start": i * step, "end": (i + 1) * step, "text": f"s{i}"} for i in range(n)]

    def test_lines_get_timestamps(self):
        lines = ["第一行", "第二行", "第三行"]
        out = Pipeline._build_polish_input(lines, self._segments(3), [])
        assert out.split("\n")[0] == "[00:00] 第一行"
        assert out.split("\n")[1] == "[00:03] 第二行"
        assert "[00:06] 第三行" in out

    def test_chapter_marker_is_injected(self):
        lines = ["第一行", "第二行", "第三行", "第四行"]
        chapters = [{"start": 3.0, "end": 9.0, "title": "第二章 光线的原理"}]
        out = Pipeline._build_polish_input(lines, self._segments(4), chapters)
        assert "## 第二章 光线的原理" in out

    def test_chapter_marker_placed_at_right_line(self):
        lines = ["A", "B", "C", "D"]
        chapters = [{"start": 3.0, "end": 9.0, "title": "CH2"}]
        rows = Pipeline._build_polish_input(lines, self._segments(4), chapters).split("\n")
        idx = rows.index("[00:03] ## CH2")
        assert rows[idx + 1] == "[00:03] B"

    def test_no_segments_still_works(self):
        out = Pipeline._build_polish_input(["只有一行"], [], [])
        assert out == "[00:00] 只有一行"


class TestPlainLines:
    def test_drops_headers_and_time_prefix(self):
        text = "# 标题\n\n[00:01] 第一段\n[00:05] 第二段\n"
        assert Pipeline._plain_lines(text) == ["第一段", "第二段"]

    def test_keeps_content_starting_with_hash_inside_line(self):
        assert Pipeline._plain_lines("这行有 # 号但不是标题") == ["这行有 # 号但不是标题"]


class TestAlignLinesToSegments:
    def test_exact_match_untouched(self):
        lines = ["a", "b", "c"]
        segs = TestBuildPolishInput._segments(3)
        out_lines, out_segs = Pipeline._align_lines_to_segments(lines, segs, "x")
        assert out_lines == lines and len(out_segs) == 3

    def test_mismatch_truncates_to_shorter(self):
        lines = ["a", "b"]
        segs = TestBuildPolishInput._segments(3)
        out_lines, out_segs = Pipeline._align_lines_to_segments(lines, segs, "x")
        assert len(out_lines) == len(out_segs) == 2

    def test_no_segments_returns_empty_segments(self):
        out_lines, out_segs = Pipeline._align_lines_to_segments(["a"], [], "x")
        assert out_lines == ["a"] and out_segs == []

    def test_chapter_index_lookup(self):
        segs = TestBuildPolishInput._segments(5, step=10.0)
        assert Pipeline._chapter_line_index(0, segs) == 0
        assert Pipeline._chapter_line_index(10, segs) == 1
        assert Pipeline._chapter_line_index(15, segs) == 2
        assert Pipeline._chapter_line_index(999, segs) is None


class TestStyleMap:
    def test_all_styles_have_chinese_names(self):
        for key in ("auto", "knowledge_points", "steps", "core_ideas", "expert", "custom"):
            assert key in STYLE_CN
