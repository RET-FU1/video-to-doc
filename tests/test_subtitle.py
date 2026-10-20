"""subtitle.py 纯函数单元测试"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from subtitle import (_fmt_srt_time, _build_entries, _write_srt, generate_srt,
                      _join_lines)


class TestFmtSrtTime:
    def test_zero(self):
        assert _fmt_srt_time(0) == "00:00:00,000"

    def test_seconds(self):
        assert _fmt_srt_time(5.5) == "00:00:05,500"

    def test_minutes(self):
        assert _fmt_srt_time(125.75) == "00:02:05,750"

    def test_hours(self):
        assert _fmt_srt_time(3723.123) == "01:02:03,123"

    def test_millisecond_rounding(self):
        # 0.9995 应该 round 为 1000ms → 进位到下一秒
        result = _fmt_srt_time(0.9995)
        assert result in ("00:00:00,999", "00:00:01,000")

    def test_millisecond_boundary(self):
        result = _fmt_srt_time(1.001)
        assert result == "00:00:01,001"


class TestBuildEntries:
    def test_empty(self):
        assert _build_entries([], [], 1.5, 7.0) == []

    def test_single_segment(self):
        segments = [{"start": 0, "end": 3, "speaker": "SPEAKER_00"}]
        lines = ["hello world"]
        entries = _build_entries(segments, lines, 1.5, 7.0)
        assert len(entries) == 1
        assert entries[0]["start"] == 0
        assert entries[0]["end"] == 3
        assert entries[0]["lines"] == ["hello world"]

    def test_merges_short_adjacent(self):
        segments = [
            {"start": 0, "end": 1, "speaker": "SPEAKER_00"},
            {"start": 1.2, "end": 2, "speaker": "SPEAKER_00"},
        ]
        lines = ["first", "second"]
        entries = _build_entries(segments, lines, 1.5, 7.0)
        assert len(entries) == 1
        assert entries[0]["lines"] == ["first", "second"]

    def test_splits_different_speakers(self):
        segments = [
            {"start": 0, "end": 1, "speaker": "SPEAKER_00"},
            {"start": 1.2, "end": 2, "speaker": "SPEAKER_01"},
        ]
        lines = ["a", "b"]
        entries = _build_entries(segments, lines, 1.5, 7.0)
        assert len(entries) == 2

    def test_splits_long_duration(self):
        segments = [
            {"start": 0, "end": 5, "speaker": "SPEAKER_00"},
            {"start": 5.5, "end": 10, "speaker": "SPEAKER_00"},
        ]
        lines = ["first long segment", "second long segment"]
        entries = _build_entries(segments, lines, 1.5, 3.0)
        # Each segment individually exceeds max_dur=3s, so they won't merge
        assert len(entries) == 2
        assert entries[0]["lines"] == ["first long segment"]
        assert entries[1]["lines"] == ["second long segment"]

    def test_skips_empty_lines(self):
        segments = [
            {"start": 0, "end": 1, "speaker": "SPEAKER_00"},
            {"start": 1.2, "end": 2, "speaker": "SPEAKER_00"},
            {"start": 2.5, "end": 3, "speaker": "SPEAKER_00"},
        ]
        lines = ["first", "", "third"]
        entries = _build_entries(segments, lines, 1.5, 7.0)
        # Empty line splits: first alone, third alone
        assert len(entries) == 2


class TestJoinLines:
    def test_no_characters_lost(self):
        joined = _join_lines(["a", "b", "c"])
        for ch in "abc":
            assert ch in joined

    def test_chinese_joined_with_punctuation(self):
        joined = _join_lines(["第一段", "第二段"])
        assert "第一段" in joined and "第二段" in joined

    def test_keeps_existing_punctuation(self):
        joined = _join_lines(["第一段，", "第二段。"])
        assert joined == "第一段，第二段。"


class TestWriteSrt:
    def test_single_entry(self, tmp_path):
        entries = [{"start": 0, "end": 3, "lines": ["hello world"], "speaker": ""}]
        out = tmp_path / "test.srt"
        _write_srt(entries, out)
        content = out.read_text("utf-8")
        assert "1\n" in content
        assert "00:00:00,000 --> 00:00:03,000" in content
        assert "hello world" in content

    def test_with_speaker_label(self, tmp_path):
        entries = [{"start": 0, "end": 2, "lines": ["hi"], "speaker": "SPEAKER_00"}]
        out = tmp_path / "test.srt"
        _write_srt(entries, out)
        content = out.read_text("utf-8")
        assert "[SPEAKER_00] hi" in content

    def test_unknown_speaker_omitted(self, tmp_path):
        entries = [{"start": 0, "end": 2, "lines": ["hi"], "speaker": "UNKNOWN"}]
        out = tmp_path / "test.srt"
        _write_srt(entries, out)
        content = out.read_text("utf-8")
        assert content.count("hi") == 1
        assert "[UNKNOWN]" not in content

    def test_three_merged_lines_keep_all_text(self, tmp_path):
        """回归：以前 entry["lines"][:2] 会静默丢掉第 3 行及以后的所有文字"""
        entries = [{"start": 0, "end": 5,
                    "lines": ["第一句内容。", "第二句内容。", "第三句内容。"],
                    "speaker": ""}]
        out = tmp_path / "test.srt"
        _write_srt(entries, out)
        content = out.read_text("utf-8")
        # 一个字都不能少
        for text in ("第一句内容", "第二句内容", "第三句内容"):
            assert text in content
        # 也不再断言 "c" not in content（那是在断言 bug）

    def test_merged_cue_written_as_single_line(self, tmp_path):
        """多条短句优先合并成一行，而不是堆成多行"""
        entries = [{"start": 0, "end": 5, "lines": ["甲", "乙", "丙"], "speaker": ""}]
        out = tmp_path / "test.srt"
        _write_srt(entries, out)
        content = out.read_text("utf-8")
        body = [ln for ln in content.split("\n") if ln and not ln[0].isdigit()
                and "-->" not in ln]
        assert len(body) == 1
        assert "甲" in body[0] and "乙" in body[0] and "丙" in body[0]

    def test_long_cue_split_but_no_text_lost(self, tmp_path):
        """超长 cue 会拆成两条，但文字全部保留（不丢字、不截断）"""
        long_text = "这是一段很长的字幕内容需要拆分" * 4
        entries = [{"start": 0, "end": 8, "lines": [long_text], "speaker": ""}]
        out = tmp_path / "test.srt"
        _write_srt(entries, out)
        content = out.read_text("utf-8")
        body = "".join(ln for ln in content.split("\n")
                       if ln and not ln[0].isdigit() and "-->" not in ln)
        assert body == long_text

    def test_split_cues_time_order(self, tmp_path):
        """拆成两条时时间必须递增且不超出原区间"""
        long_text = "内容内容内容内容内容内容内容内容内容内容内容内容内容内容内容" * 3
        entries = [{"start": 10, "end": 20, "lines": [long_text], "speaker": ""}]
        out = tmp_path / "test.srt"
        _write_srt(entries, out)
        content = out.read_text("utf-8")
        times = [ln for ln in content.split("\n") if "-->" in ln]
        assert len(times) == 2
        assert times[0].startswith("00:00:10,000")
        assert times[1].endswith("00:00:20,000")
        first_end = times[0].split(" --> ")[1]
        second_start = times[1].split(" --> ")[0]
        assert first_end <= second_start


class TestGenerateSrt:
    def test_accepts_segment_list(self, tmp_path):
        """新签名：直接传 segments 列表"""
        segments = [
            {"start": 0, "end": 2, "text": "第一段"},
            {"start": 2.1, "end": 4, "text": "第二段"},
        ]
        out = tmp_path / "a.srt"
        generate_srt(segments, ["第一段", "第二段"], out)
        content = out.read_text("utf-8")
        assert "第一段" in content and "第二段" in content

    def test_accepts_segments_json_path(self, tmp_path):
        """旧签名兼容：第一个参数是 _segments.json 路径时读文件"""
        segments = [{"start": 0, "end": 2, "text": "来自文件"}]
        seg_path = tmp_path / "x_segments.json"
        seg_path.write_text(json.dumps(segments, ensure_ascii=False), encoding="utf-8")
        out = tmp_path / "b.srt"
        generate_srt(seg_path, ["来自文件"], out)
        assert "来自文件" in out.read_text("utf-8")

    def test_accepts_str_path(self, tmp_path):
        """字符串路径也走旧逻辑"""
        segments = [{"start": 0, "end": 2}]
        seg_path = tmp_path / "y_segments.json"
        seg_path.write_text(json.dumps(segments), encoding="utf-8")
        out = tmp_path / "c.srt"
        generate_srt(str(seg_path), ["字符串路径"], out)
        assert "字符串路径" in out.read_text("utf-8")

    def test_missing_segments_path_returns_without_create(self, tmp_path):
        out = tmp_path / "missing.srt"
        generate_srt(tmp_path / "nope_segments.json", ["x"], out)
        assert not out.exists()

    def test_extra_lines_warn_not_crash(self, tmp_path):
        segments = [{"start": 0, "end": 2}]
        out = tmp_path / "d.srt"
        generate_srt(segments, ["一", "二", "三"], out)
        content = out.read_text("utf-8")
        assert "一" in content

    def test_fewer_lines_than_segments(self, tmp_path):
        segments = [{"start": 0, "end": 2}, {"start": 2, "end": 4}]
        out = tmp_path / "e.srt"
        generate_srt(segments, ["只有一行"], out)
        assert "只有一行" in out.read_text("utf-8")

    def test_end_to_end_three_lines_no_loss(self, tmp_path):
        """合并 3 段后写出 SRT，所有文字都必须还在"""
        segments = [
            {"start": 0, "end": 1},
            {"start": 1.2, "end": 2},
            {"start": 2.3, "end": 3},
        ]
        lines = ["春眠不觉晓", "处处闻啼鸟", "夜来风雨声"]
        out = tmp_path / "f.srt"
        generate_srt(segments, lines, out)
        content = out.read_text("utf-8")
        for line in lines:
            assert line in content
