"""utils.py 纯函数单元测试"""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import (
    sanitize_filename, split_text, split_text_with_overlap, validate_config,
    VIDEO_EXTS, AUDIO_EXTS, MEDIA_EXTS,
)


class TestSanitizeFilename:
    def test_normal(self):
        assert sanitize_filename("Hello World") == "Hello World"

    def test_illegal_chars(self):
        result = sanitize_filename('file<name>:with"illegal/chars?*|')
        assert all(c not in result for c in '<>:"/\\|?*')

    def test_empty(self):
        assert sanitize_filename("") == "untitled"

    def test_whitespace_only(self):
        assert sanitize_filename("   ") == "untitled"

    def test_dot_only(self):
        assert sanitize_filename("...") == "untitled"

    def test_reserved_name(self):
        assert sanitize_filename("CON") == "_CON"
        assert sanitize_filename("PRN") == "_PRN"
        assert sanitize_filename("COM1") == "_COM1"
        assert sanitize_filename("LPT1") == "_LPT1"

    def test_reserved_with_dot(self):
        assert sanitize_filename("CON.txt") == "_CON.txt"

    def test_leading_trailing_spaces_dots(self):
        result = sanitize_filename("  hello...  ")
        assert result == "hello"


class TestSplitText:
    def test_empty(self):
        assert split_text("", 1000) == []
        assert split_text("   ", 1000) == []

    def test_single_paragraph(self):
        result = split_text("hello world", 1000)
        assert result == ["hello world"]

    def test_fits_in_one(self):
        result = split_text("para1\n\npara2\n\npara3", 1000)
        assert result == ["para1\n\npara2\n\npara3"]

    def test_splits_at_boundary(self):
        result = split_text("a" * 50 + "\n\n" + "b" * 50, 60)
        assert len(result) == 2
        assert "a" * 50 in result[0]
        assert "b" * 50 in result[1]

    def test_no_separator(self):
        result = split_text("a" * 100, 50)
        assert len(result) == 1
        assert result[0] == "a" * 100


class TestSplitTextWithOverlap:
    def test_short_text(self):
        result = split_text_with_overlap("line1\nline2\nline3", 5000, 300, "\n")
        assert len(result) == 1

    def test_produces_chunks(self):
        lines = [f"line {i:03d}" for i in range(200)]
        text = "\n".join(lines)
        result = split_text_with_overlap(text, 500, 50, "\n")
        assert len(result) > 1

    def test_overlap_present(self):
        lines = [f"line {i:03d}" for i in range(100)]
        text = "\n".join(lines)
        result = split_text_with_overlap(text, 400, 100, "\n")
        # Should produce multiple chunks with overlap context prepended
        assert len(result) >= 2
        # Chunk 1 starts with overlap from end of chunk 0
        chunk0_last_lines = result[0].split("\n")[-3:]
        chunk1_content = "\n".join(chunk0_last_lines)
        assert chunk1_content in result[1]


class TestValidateConfig:
    def test_empty_dict(self):
        errors = validate_config({})
        assert len(errors) > 0

    def test_not_dict(self):
        errors = validate_config(None)
        assert any("字典" in e for e in errors)

    def test_valid_minimal(self):
        config = {
            "output_dir": "./output",
            "whisper": {"device": "cpu", "compute_type": "int8"},
            "summarizer": {"provider": "openai", "model": "test-model"},
            "downloader": {"timeout": 7200},
        }
        errors = validate_config(config)
        assert errors == []

    def test_invalid_device(self):
        config = {
            "output_dir": "./out",
            "whisper": {"device": "invalid"},
            "summarizer": {"provider": "openai", "model": "m"},
        }
        errors = validate_config(config)
        assert any("device" in e for e in errors)

    def test_invalid_compute_type(self):
        config = {
            "output_dir": "./out",
            "whisper": {"device": "cpu", "compute_type": "fp8"},
            "summarizer": {"provider": "openai", "model": "m"},
        }
        errors = validate_config(config)
        assert any("compute_type" in e for e in errors)

    def test_invalid_provider(self):
        config = {
            "output_dir": "./out",
            "summarizer": {"provider": "anthropic", "model": "m"},
        }
        errors = validate_config(config)
        assert any("provider" in e for e in errors)

    def test_missing_model(self):
        config = {
            "output_dir": "./out",
            "summarizer": {"provider": "openai"},
        }
        errors = validate_config(config)
        assert any("model" in e for e in errors)

    def test_invalid_max_chunk(self):
        config = {
            "output_dir": "./out",
            "summarizer": {"provider": "openai", "model": "m", "max_chunk_chars": 100},
        }
        errors = validate_config(config)
        assert any("max_chunk_chars" in e for e in errors)

    def test_invalid_max_tokens(self):
        config = {
            "output_dir": "./out",
            "summarizer": {"provider": "openai", "model": "m", "max_tokens": 100},
        }
        errors = validate_config(config)
        assert any("max_tokens" in e for e in errors)

    def test_diarization_speaker_range(self):
        config = {
            "output_dir": "./out",
            "summarizer": {"provider": "openai", "model": "m"},
            "diarization": {"enabled": True, "min_speakers": 5, "max_speakers": 2},
        }
        errors = validate_config(config)
        assert any("min_speakers" in e for e in errors)

    def test_invalid_downloader_timeout(self):
        config = {
            "output_dir": "./out",
            "summarizer": {"provider": "openai", "model": "m"},
            "downloader": {"timeout": 5},
        }
        errors = validate_config(config)
        assert any("downloader.timeout" in e for e in errors)


class TestMediaExts:
    def test_video_exts(self):
        assert ".mp4" in VIDEO_EXTS
        assert ".mkv" in VIDEO_EXTS

    def test_audio_exts(self):
        assert ".mp3" in AUDIO_EXTS
        assert ".wav" in AUDIO_EXTS

    def test_media_exts_union(self):
        assert MEDIA_EXTS == VIDEO_EXTS | AUDIO_EXTS
