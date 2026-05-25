"""subtitle.py 纯函数单元测试"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from subtitle import _fmt_srt_time, _build_entries, _write_srt


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

    def test_max_two_lines(self, tmp_path):
        entries = [{"start": 0, "end": 5, "lines": ["a", "b", "c"], "speaker": ""}]
        out = tmp_path / "test.srt"
        _write_srt(entries, out)
        content = out.read_text("utf-8")
        assert "a\nb\n" in content
        assert "c" not in content
