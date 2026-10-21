"""fidelity.py 保真校验测试。

背景：改造前有两套独立实现（pipeline 全片 0.90 相似度、summarizer 分块 0.75），
阈值不一致会出现"每个分块都通过、最后整篇被回退"的诡异现象。
现在只有一个实现，阈值通过 Thresholds 显式传入，这里覆盖各条判据。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import fidelity
from fidelity import (POLISH, SUMMARY, Thresholds, fallback_text, normalize,
                      strip_speaker_labels, strip_time_prefix, token_loss, verify)


class TestStripHelpers:
    def test_strip_time_prefix(self):
        assert strip_time_prefix("[00:05] 第一句\n[01:23] 第二句") == "第一句\n第二句"

    def test_strip_time_prefix_drop_empty_lines(self):
        text = "[00:05] 第一句\n\n[00:10] 第二句\n"
        assert strip_time_prefix(text, drop_empty_lines=True) == "第一句\n第二句"

    def test_long_minute_value(self):
        """超长视频可能出现 3 位分钟（[100:23]）"""
        assert strip_time_prefix("[100:23] 内容") == "内容"

    def test_strip_speaker_labels(self):
        text = "说话人A：你好\n说话人B：你好啊\nSPEAKER_1: hello"
        assert strip_speaker_labels(text) == "你好\n你好啊\nhello"

    def test_normalize_removes_punctuation(self):
        assert normalize("你好，世界！") == "你好世界"

    def test_fallback_text_drops_timestamp(self):
        assert fallback_text("[00:05] 正文") == "正文"
        assert fallback_text("无时间戳正文") == "无时间戳正文"


class TestTokenLoss:
    def test_no_loss(self):
        ratio, long_lost = token_loss("hello world 123", "hello world 123")
        assert ratio == 0.0 and long_lost == []

    def test_word_lost(self):
        ratio, long_lost = token_loss("CTranslate2 faster-whisper 2024",
                                      "CTranslate2 2024")
        assert ratio > 0
        assert "faster" in long_lost

    def test_no_tokens(self):
        assert token_loss("纯中文内容", "纯中文内容") == (0.0, [])


class TestVerifyPasses:
    def test_identical(self):
        src = "这是一段用于测试的文本，内容需要保持一致。" * 10
        assert verify(src, src) == src

    def test_punctuation_added(self):
        src = "\n".join(["这是第一句话", "这是第二句话"] * 20)
        out = src.replace("\n", "，") + "。"
        assert verify(src, out) == out

    def test_multispeaker_labels_added_is_ok(self):
        """多说话人模式合法新增「说话人A：」标签，不应被判为长度异常"""
        src = "\n".join(["今天天气不错", "适合出门走走"] * 10)
        out = "\n".join(f"说话人{'A' if i % 2 == 0 else 'B'}：{line}"
                        for i, line in enumerate(src.split("\n")))
        assert verify(src, out) == out, "加了说话人标签不应触发回退"

    def test_empty_source_passthrough(self):
        assert verify("", "任何输出") == "任何输出"


class TestVerifyRejects:
    def test_truncated(self):
        src = "正常内容。" * 100
        assert verify(src, "正常内容。" * 10) == src

    def test_paraphrased(self):
        src = "摄影是光的艺术这个观点在今天的讨论中被反复提到并且展开了详细论证。" * 5
        out = "摄影属于光线的艺术，这一说法本次谈话里多次出现，并进行了详尽论述。" * 5
        assert verify(src, out) == src

    def test_empty_output_falls_back(self):
        src = "内容" * 50
        assert verify(src, "") == src

    def test_lost_english_word(self):
        """长英文词丢失往往意味着整段被截断"""
        src = ("今天讨论了 Photoshop 和 CTranslate2 的性能，" * 20)
        out = src.replace("CTranslate2", "")           # 去掉关键术语
        assert verify(src, out) == src


class TestThresholds:
    def test_summary_more_lenient_than_polish(self):
        assert SUMMARY.min_similarity < POLISH.min_similarity
        assert SUMMARY.max_token_loss > POLISH.max_token_loss

    def test_custom_thresholds_respected(self):
        src = "正常内容。" * 100
        out = "正常内容。" * 50          # 长度比 0.5
        loose = Thresholds(min_ratio=0.4, max_ratio=2.0, min_similarity=0.0,
                           max_token_loss=1.0)
        assert verify(src, out, thresholds=loose) == out, "放宽阈值后应通过"

    def test_module_exports_for_pipeline(self):
        pipeline = __import__("pipeline")
        assert pipeline.POLISH is POLISH
        assert pipeline.verify is fidelity.verify
