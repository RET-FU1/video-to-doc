"""总结风格的**唯一**数据源。

改造前，风格表散落在 4 个地方：

  1. `pipeline.STYLE_CN`                —— key → 中文名（用于产物文件命名）
  2. `summarizer.STYLE_PROMPTS`         —— key → 内置提示词
  3. `gui` 里硬编码的 (中文名, key) 列表  —— 生成界面上的 chip
  4. `README.md` 的风格表格              —— 给用户看

加一种风格要改 4 个文件，漏一个就会出现「界面能选但产物命名对不上」或者
「README 写了但程序不认」这类问题。现在统一到这里，其余地方只做引用：

  - pipeline：`from styles import STYLE_CN, STYLE_PREFIXES, label_of`
  - summarizer：`from styles import BUILTIN_STYLE_KEYS, DEFAULT_STYLE`
  - gui：`from styles import STYLES, DEFAULT_STYLE`，遍历 STYLES 生成 chip
  - main（CLI）：`STYLE_KEYS` 生成 `--summary-styles` 的候选列表
  - README：由 `tests/test_styles.py` 逐行精确比对表格（防止再次漂移）
"""
from typing import Dict, List, NamedTuple


class Style(NamedTuple):
    """一种总结风格"""

    key: str          # 配置/CLI 里用的标识
    label: str        # 中文名，同时作为产物文件名前缀
    builtin: bool     # 是否有内置提示词（custom 走用户自定义提示词）


STYLES: List[Style] = [
    Style("auto", "全面总结", True),
    Style("knowledge_points", "知识点", True),
    Style("steps", "操作步骤", True),
    Style("core_ideas", "核心观点", True),
    Style("expert", "专家深度", True),
    Style("custom", "自定义", False),      # 提示词来自 summarizer.custom_prompt
]

# key → 中文名（产物命名用）
STYLE_CN: Dict[str, str] = {s.key: s.label for s in STYLES}

# 全部合法 key，顺序即界面/文档顺序
STYLE_KEYS: List[str] = [s.key for s in STYLES]

# 有内置提示词的风格（summarizer.STYLE_PROMPTS 必须覆盖这些）
BUILTIN_STYLE_KEYS: List[str] = [s.key for s in STYLES if s.builtin]

DEFAULT_STYLE: str = "auto"

# 产物文件名的风格前缀，如 ("全面总结-", "知识点-", ...)
STYLE_PREFIXES: tuple = tuple(f"{cn}-" for cn in STYLE_CN.values())


def label_of(key: str) -> str:
    """取风格的中文名；未知 key 原样返回（保持旧行为）"""
    return STYLE_CN.get(key, key)
