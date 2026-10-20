"""format_converter.py 纯函数单元测试"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from format_converter import (md_to_txt, md_to_html, save_formats, _kebab_id,
                             _count_reading_minutes, _extract_subtitle)


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


class TestSaveFormats:
    """save_formats 文件名规则：base_path 前缀原样保留，只追加扩展名。

    历史 bug：Path.with_suffix 把最后一个点之后的内容当扩展名，
    `EP1.5 讲解` 和 `EP1.6 讲解` 都写成 `EP1.md` → 后一个视频覆盖前一个。
    """

    def test_dotted_titles_do_not_overwrite(self, tmp_path):
        base1 = tmp_path / "EP1.5 讲解"
        base2 = tmp_path / "EP1.6 讲解"
        saved1 = save_formats("# 视频1\n\n内容一", base1, ["md"])
        saved2 = save_formats("# 视频2\n\n内容二", base2, ["md"])

        assert [p.name for p in saved1] == ["EP1.5 讲解.md"]
        assert [p.name for p in saved2] == ["EP1.6 讲解.md"]
        assert saved1[0] != saved2[0]
        # 两个文件都存在且各自内容独立，没有互相覆盖
        assert "内容一" in saved1[0].read_text(encoding="utf-8")
        assert "内容二" in saved2[0].read_text(encoding="utf-8")

    def test_date_like_title(self, tmp_path):
        saved = save_formats("# t\n\nx", tmp_path / "2026.10 更新说明", ["md", "txt"])
        assert [p.name for p in saved] == ["2026.10 更新说明.md", "2026.10 更新说明.txt"]
        assert all(p.exists() for p in saved)

    def test_all_three_formats(self, tmp_path):
        base = tmp_path / "EP2.3 摄影构图"
        saved = save_formats("# t\n\n**加粗**内容", base, ["md", "txt", "html"])
        names = sorted(p.name for p in saved)
        assert names == ["EP2.3 摄影构图.html", "EP2.3 摄影构图.md", "EP2.3 摄影构图.txt"]
        for path in saved:
            assert path.exists()
            assert path.stat().st_size > 0
        # html 真的是 html，txt 去掉了 markdown 标记
        html_path = next(p for p in saved if p.suffix == ".html")
        txt_path = next(p for p in saved if p.suffix == ".txt")
        assert "<html" in html_path.read_text(encoding="utf-8").lower()
        assert "**" not in txt_path.read_text(encoding="utf-8")

    def test_no_extension_base(self, tmp_path):
        """base_path 完全没有扩展名时行为不变"""
        saved = save_formats("# t\n\nx", tmp_path / "plain_title", ["md"])
        assert saved[0].name == "plain_title.md"

    def test_returns_paths_and_meta_source(self, tmp_path):
        saved = save_formats("# t\n\nx", tmp_path / "a.b.c", ["md"],
                             meta={"source_file": "video.mp4"})
        assert saved[0].name == "a.b.c.md"
        assert isinstance(saved[0], Path)
