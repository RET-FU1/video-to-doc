"""format_converter.py 纯函数单元测试"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from format_converter import md_to_txt, md_to_html, _kebab_id, _count_reading_minutes, _extract_subtitle


class TestKebabId:
    def test_english(self):
        assert _kebab_id("Hello World") == "hello-world"

    def test_mixed(self):
        assert _kebab_id("API请求 Rate Limit") == "api-rate-limit"

    def test_chinese_only(self):
        assert _kebab_id("纯中文标题") is None

    def test_empty(self):
        assert _kebab_id("") is None


class TestCountReadingMinutes:
    def test_short(self):
        assert _count_reading_minutes("hello") == 1

    def test_medium(self):
        text = "x" * 500
        assert _count_reading_minutes(text) == 2

    def test_minimum_one(self):
        assert _count_reading_minutes("") == 1


class TestExtractSubtitle:
    def test_basic(self):
        md = "# Title\n\nFirst paragraph here."
        assert "First paragraph" in _extract_subtitle(md)

    def test_empty(self):
        assert _extract_subtitle("# Only title") == ""

    def test_truncated(self):
        md = "# T\n\n" + "z" * 300
        result = _extract_subtitle(md)
        assert len(result) <= 200


class TestMdToTxt:
    def test_removes_headings(self):
        result = md_to_txt("# heading\n\ntext")
        assert "#" not in result
        assert "heading" in result
        assert "text" in result

    def test_removes_bold_italic(self):
        result = md_to_txt("**bold** and *italic*")
        assert "bold" in result
        assert "italic" in result
        assert "**" not in result
        assert result.count("*") == 0

    def test_removes_links(self):
        result = md_to_txt("[click here](https://example.com)")
        assert "click here" in result
        assert "https" not in result

    def test_removes_images(self):
        result = md_to_txt("text ![alt](img.png) more")
        assert "alt" not in result
        assert "text" in result
        assert "more" in result

    def test_removes_inline_code(self):
        result = md_to_txt("run `command` now")
        assert "command" in result
        assert "`" not in result

    def test_removes_blockquotes(self):
        result = md_to_txt("> quoted text\n\nnormal")
        assert "quoted text" in result
        assert ">" not in result

    def test_removes_list_markers(self):
        result = md_to_txt("- item 1\n- item 2")
        assert "item 1" in result
        assert "item 2" in result
        assert "-" not in result

    def test_collapses_multiple_newlines(self):
        result = md_to_txt("a\n\n\n\nb")
        assert result.count("\n\n") == 1
        assert "a\n\nb" in result


class TestMdToHtml:
    def test_returns_html_string(self):
        html = md_to_html("# Test\n\ncontent", title="Test Doc")
        assert "<!DOCTYPE html>" in html or "<html" in html.lower()

    def test_injects_title(self):
        html = md_to_html("# Page Title\n\ntext", title="Page Title")
        assert "Page Title" in html

    def test_has_body_content(self):
        html = md_to_html("# T\n\nhello world", title="T")
        assert "hello world" in html
