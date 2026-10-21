"""总结风格单一数据源的守卫测试。

背景：风格表以前散落在 pipeline / summarizer / gui / README 四处，
加一种风格要改 4 个文件，漏一个就会出现「界面能选但产物命名对不上」
或「README 写了但程序不认」。现在统一到 styles.py，这里断言各处确实一致。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from styles import (STYLES, STYLE_CN, STYLE_KEYS, BUILTIN_STYLE_KEYS,
                    STYLE_PREFIXES, DEFAULT_STYLE, label_of)

PROJECT_ROOT = Path(__file__).parent.parent


class TestStyleRegistry:
    def test_keys_unique(self):
        assert len(STYLE_KEYS) == len(set(STYLE_KEYS))

    def test_labels_non_empty_and_unique(self):
        labels = [s.label for s in STYLES]
        assert all(labels), "风格中文名不能为空"
        assert len(labels) == len(set(labels)), "风格中文名不能重复（会撞产物文件名）"

    def test_style_cn_matches(self):
        assert STYLE_CN == {s.key: s.label for s in STYLES}

    def test_default_style_exists(self):
        assert DEFAULT_STYLE in STYLE_CN

    def test_custom_is_not_builtin(self):
        """自定义风格没有内置提示词，走用户配置"""
        custom = [s for s in STYLES if s.key == "custom"][0]
        assert custom.builtin is False
        assert "custom" not in BUILTIN_STYLE_KEYS

    def test_prefixes(self):
        assert STYLE_PREFIXES == tuple(f"{s.label}-" for s in STYLES)

    def test_label_of_unknown_passthrough(self):
        assert label_of("auto") == STYLE_CN["auto"]
        assert label_of("不存在的风格") == "不存在的风格"


class TestSummarizerPromptsCoverStyles:
    def test_prompts_cover_all_builtin_styles(self):
        summarizer = pytest.importorskip("summarizer")
        prompts = summarizer.OpenAICompatSummarizer.STYLE_PROMPTS
        missing = set(BUILTIN_STYLE_KEYS) - set(prompts)
        assert not missing, f"这些内置风格缺少提示词: {sorted(missing)}"

    def test_no_orphan_prompts(self):
        """提示词里不应存在 styles.py 未定义的 key"""
        summarizer = pytest.importorskip("summarizer")
        prompts = summarizer.OpenAICompatSummarizer.STYLE_PROMPTS
        orphans = set(prompts) - set(BUILTIN_STYLE_KEYS)
        assert not orphans, f"这些提示词对应的风格未在 styles.py 定义: {sorted(orphans)}"


class TestPipelineReexports:
    def test_pipeline_imports_same_table(self):
        pipeline = pytest.importorskip("pipeline")
        assert pipeline.STYLE_CN is STYLE_CN
        assert pipeline.STYLE_PREFIXES == STYLE_PREFIXES


class TestReadmeInSync:
    """README 的风格表格必须与代码一致（防止文档漂移）"""

    README = PROJECT_ROOT / "README.md"

    @staticmethod
    def _readme_table_rows() -> list:
        """解析 README 里风格表格的数据行，返回 [(中文名, 参数名), ...]

        用子串断言会出现假绿：代码是「知识点」而文档写「知识点提取」时，
        `"知识点" in text` 仍然成立。必须逐行精确比对。
        """
        text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        rows = []
        for line in text.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 2 and cells[1].startswith("`") and cells[1].endswith("`"):
                key = cells[1].strip("`")
                if key in STYLE_KEYS:
                    rows.append((cells[0], key))
        return rows

    def test_readme_table_matches_code_exactly(self):
        rows = self._readme_table_rows()
        assert rows, "README 里没有解析到风格表格"
        expected = [(s.label, s.key) for s in STYLES]
        assert rows == expected, (
            f"README 风格表格与 styles.py 不一致\n"
            f"  文档: {rows}\n  代码: {expected}")

    def test_readme_style_count(self):
        text = self.README.read_text(encoding="utf-8")
        builtin = len(BUILTIN_STYLE_KEYS)
        assert f"{builtin} 种内置风格" in text, (
            f"README 里应写「{builtin} 种内置风格」，实际文案可能已过期")
