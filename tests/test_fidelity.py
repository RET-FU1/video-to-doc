"""fidelity.py 保真校验测试。

背景：改造前有两套独立实现（pipeline 全片 0.90 相似度、summarizer 分块 0.75），
阈值不一致会出现"每个分块都通过、最后整篇被回退"的诡异现象。
现在只有一个实现，阈值通过 Thresholds 显式传入，这里覆盖各条判据。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import fidelity
from fidelity import (POLISH, Thresholds, fallback_text, normalize,
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
    def test_polish_preset_values(self):
        assert POLISH.min_ratio == 0.85
        assert POLISH.max_ratio == 1.25
        assert POLISH.min_similarity == 0.90
        assert POLISH.max_token_loss == 0.20

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

class TestSuccessPathCleanup:
    """回归：模型忘记去掉 [MM:SS] 时，通过校验的输出仍要剥离时间戳。

    重构（9522be2）一度丢掉了这个行为 —— 时间戳会原样混进转写正文，
    而这正是本项目最初要修的"错漏"之一。
    """

    def test_residual_timestamp_stripped(self):
        src = "这是第一句话" * 20
        out = "[00:05] " + src
        result = verify(src, out)
        assert not result.startswith("[00:05]"), "通过校验后仍应剥离残留时间戳"
        assert result == src

    def test_speaker_label_preserved(self):
        """剥离时间戳不能顺手把合法的说话人标签也吃掉"""
        src = "今天天气不错" * 20
        out = "说话人A：" + src
        assert verify(src, out).startswith("说话人A：")


class TestShortChunkTolerance:
    """相似度对短文本极敏感（29 字改 3 个字≈0.897），
    而 split_text 的最后一块可能很短，误杀会让该块退回未加标点的原文。"""

    def test_short_chunk_passes_despite_low_similarity(self):
        src = "摄影是光的艺术这个观点在今天的讨论中反复提到并展开论证"
        out = "摄影属于光线的艺术，这个观点今天被反复提到并展开论证。"
        assert len(src) < POLISH.similarity_min_chars
        assert verify(src, out) == out, "短块不应触发相似度回退"

    def test_short_chunk_still_checks_length(self):
        """短块跳过相似度，但长度比与 token 丢失仍然生效"""
        src = "这是一段短文本内容"
        assert verify(src, "完全不同的另一段内容在这里") == src

    def test_long_chunk_similarity_still_enforced(self):
        src = "摄影是光的艺术。" * 50
        out = "摄影属于光线艺术。" * 50
        assert len(normalize(src)) >= POLISH.similarity_min_chars
        assert verify(src, out) == src, "长文本改写仍应回退"


class TestLongMinuteValues:
    def test_four_digit_minutes(self):
        """>16h40m 的直播/有声书会出现 [1000:00]"""
        assert strip_time_prefix("[1000:00] 内容") == "内容"

    def test_one_digit_minutes(self):
        assert strip_time_prefix("[5:03] 内容") == "内容"
