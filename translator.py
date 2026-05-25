"""
翻译模块 — 基于 LLM 逐行翻译转写文本
保持行结构不变，确保翻译后每行与原始 whisper 段落时间戳对应
"""
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

_LANG_NAMES = {
    "zh": "简体中文", "en": "英文", "ja": "日文", "ko": "韩文",
    "fr": "法文", "de": "德文", "es": "西班牙文", "ru": "俄文",
    "auto": "自动检测",
}


class Translator:
    """逐行翻译器 — 复用现有 OpenAI 兼容客户端"""

    def __init__(self, client, model: str, target_lang: str = "zh",
                 max_lines_per_chunk: int = 80, timeout: float = 120) -> None:
        self.client = client
        self.model = model
        self.target_lang = target_lang
        self.max_lines = max_lines_per_chunk
        self.timeout = timeout

    def translate(self, text: str) -> str:
        """翻译整段文本，逐行翻译保持行结构"""
        lines = text.strip().split("\n")
        non_empty = [(i, line) for i, line in enumerate(lines) if line.strip()]
        if not non_empty:
            return text

        # 长文本分块翻译
        if len(non_empty) <= self.max_lines:
            result = self._translate_chunk([line for _, line in non_empty])
            return self._reassemble(lines, non_empty, result)

        # 分块处理
        all_translated = []
        for chunk_start in range(0, len(non_empty), self.max_lines):
            chunk = non_empty[chunk_start:chunk_start + self.max_lines]
            chunk_lines = [line for _, line in chunk]
            translated = self._translate_chunk(chunk_lines)
            all_translated.extend(translated)

        return self._reassemble(lines, non_empty, all_translated)

    def _translate_chunk(self, lines: List[str]) -> List[str]:
        """翻译一个行块，确保行数一致"""
        line_count = len(lines)
        source = "\n".join(lines)
        target_name = _LANG_NAMES.get(self.target_lang, self.target_lang)

        prompt = (
            f"将以下文本逐行翻译为{target_name}。\n\n"
            "关键规则：\n"
            f"- 原文有 {line_count} 行，你的输出必须恰好也是 {line_count} 行\n"
            "- 每行独立翻译，不合并、不拆分、不跳行\n"
            "- 翻译准确流畅自然，符合目标语言表达习惯\n"
            "- 保留原文的语气、修辞、情感色彩\n"
            "- 只输出翻译文本，不要任何编号、前缀、解释\n\n"
            f"原文：\n\n{source}"
        )

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                max_tokens=8192,
                messages=[{"role": "user", "content": prompt}],
                timeout=self.timeout,
            )
            content = resp.choices[0].message.content or ""
        except Exception as e:
            logger.warning("翻译失败，保留原文: %s", e)
            return lines

        translated = [t.strip() for t in content.strip().split("\n") if t.strip()]

        if len(translated) != line_count:
            logger.warning(
                "翻译行数不匹配: 期望 %d 行，实际 %d 行。已尽量对齐。",
                line_count, len(translated),
            )
            # 截断或补齐
            if len(translated) < line_count:
                translated += [""] * (line_count - len(translated))
            else:
                translated = translated[:line_count]

        return translated

    @staticmethod
    def _reassemble(original_lines: List[str], non_empty: List[tuple],
                    translated: List[str]) -> str:
        """将翻译结果填回原始行结构（保留空行位置）"""
        mapping = {idx: translated[i] for i, (idx, _) in enumerate(non_empty)}
        result = []
        for i, line in enumerate(original_lines):
            if i in mapping:
                result.append(mapping[i])
            elif not line.strip():
                result.append("")
            else:
                result.append(line)
        return "\n".join(result)
