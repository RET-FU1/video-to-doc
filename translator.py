"""
翻译模块 — 基于 LLM 逐行翻译转写文本
保持行结构不变，确保翻译后每行与原始 whisper 段落时间戳对应
"""
import logging
import re
from typing import List, Optional

from utils import CancelledError

logger = logging.getLogger(__name__)

_LANG_NAMES = {
    "zh": "简体中文", "en": "英文", "ja": "日文", "ko": "韩文",
    "fr": "法文", "de": "德文", "es": "西班牙文", "ru": "俄文",
    "auto": "自动检测",
}

# 模型偶尔会给输出加编号（"1. xxx" / "1、xxx"），剥掉以还原纯译文
_NUMBERING_RE = re.compile(r"^\s*\d+\s*[.、)．]\s*")


class TranslationLineCountError(ValueError):
    """翻译结果行数与原文不一致。

    继承 ValueError 以兼容 `except ValueError`；单独定义类型是为了让调用方
    （pipeline）能精确地"只放弃翻译、继续跑完其余流程"，而不是整段任务失败。
    """


class TranslationAPIError(RuntimeError):
    """翻译 API 调用失败（网络异常、鉴权失败等）。

    单独定义是为了让 pipeline 区分"模型没翻好"和"根本没连上"：
    两种情况都放弃翻译，但日志措辞不同，用户能判断是网络问题还是模型问题。
    """


def _split_lines(content: str) -> List[str]:
    """把模型输出拆成非空行（去掉首尾空白与空行）"""
    return [t.strip() for t in (content or "").strip().split("\n") if t.strip()]


def _strip_numbering(line: str) -> str:
    """去掉行首编号，返回纯译文"""
    return _NUMBERING_RE.sub("", line).strip()


class Translator:
    """逐行翻译器 — 复用现有 OpenAI 兼容客户端"""

    def __init__(self, client, model: str, target_lang: str = "zh",
                 max_lines_per_chunk: int = 80, timeout: float = 120,
                 max_tokens: int = 8192) -> None:
        self.client = client
        self.model = model
        self.target_lang = target_lang
        self.max_lines = max_lines_per_chunk
        self.timeout = timeout
        self.max_tokens = max_tokens

    def translate(self, text: str) -> str:
        """翻译整段文本，逐行翻译保持行结构

        Raises:
            TranslationLineCountError(ValueError): 翻译结果行数与输入非空行数不一致。
                这里刻意抛异常而不是用空串补齐 —— 补齐会让后续
                时间戳与文本行整体错位（pipeline 捕获后会放弃翻译）。
            TranslationAPIError(RuntimeError): API 调用失败，没有拿到译文。
        """
        lines = text.strip().split("\n")
        non_empty = [(i, line) for i, line in enumerate(lines) if line.strip()]
        if not non_empty:
            return text

        # 长文本分块翻译
        if len(non_empty) <= self.max_lines:
            result = self._translate_chunk([line for _, line in non_empty])
            return self._reassemble(lines, non_empty, result)

        # 分块处理
        all_translated: List[str] = []
        for chunk_start in range(0, len(non_empty), self.max_lines):
            chunk = non_empty[chunk_start:chunk_start + self.max_lines]
            chunk_lines = [line for _, line in chunk]
            translated = self._translate_chunk(chunk_lines)
            all_translated.extend(translated)

        return self._reassemble(lines, non_empty, all_translated)

    def _translate_chunk(self, lines: List[str]) -> List[str]:
        """翻译一个行块，行数必须与输入一致，否则抛 ValueError

        行数不一致时先带编号重试一次（LLM 常见失败是把多行合并成一段，
        编号 + 明确"上次行数不对"能救回大部分情况）。仍不一致就抛异常，
        绝不填空串补齐 —— 补齐会让后续时间戳与文本行整体错位。
        """
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

        content = self._call_api(prompt)
        if content is None:
            # API 异常：返回原文行并让上层知道"没翻译成功"，否则 pipeline 会
            # 打印"翻译完成"，用户以为拿到了译文、实际文档还是原语言。
            raise TranslationAPIError("翻译 API 调用失败（详见上方日志）")
        translated = _split_lines(content)

        if len(translated) != line_count:
            logger.warning(
                "翻译行数不匹配: 期望 %d 行，实际 %d 行，按编号格式重试一次",
                line_count, len(translated),
            )
            numbered = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(lines))
            retry_prompt = (
                f"将以下文本逐行翻译为{target_name}。\n\n"
                f"原文共 {line_count} 行，每行都有编号。"
                f"你必须输出恰好 {line_count} 行，每行保留同样的编号前缀，"
                "一行对一行，不合并、不拆分、不跳行。\n\n"
                f"原文：\n\n{numbered}"
            )
            content = self._call_api(retry_prompt)
            if content is None:
                raise TranslationAPIError("翻译 API 调用失败（重试阶段）")
            translated = [_strip_numbering(t) for t in _split_lines(content)]

        if len(translated) != line_count:
            logger.warning(
                "翻译行数不匹配: 期望 %d 行，实际 %d 行，放弃本次翻译以免字幕错位",
                line_count, len(translated),
            )
            raise TranslationLineCountError(
                f"翻译行数不匹配: 期望 {line_count}，实际 {len(translated)}"
            )

        return translated

    def _call_api(self, prompt: str) -> Optional[str]:
        """单次 API 调用；网络/API 异常时返回 None 由调用方保留原文"""
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=[{"role": "user", "content": prompt}],
                timeout=self.timeout,
            )
            return resp.choices[0].message.content or ""
        except CancelledError:
            raise
        except Exception as e:
            # 网络/API 异常时保留原文，但必须留痕（含堆栈）便于排查
            logger.warning("翻译失败，保留原文: %s", e, exc_info=True)
            return None

    @staticmethod
    def _reassemble(original_lines: List[str], non_empty: List[tuple],
                    translated: List[str]) -> str:
        """将翻译结果填回原始行结构（保留空行位置）"""
        if len(translated) != len(non_empty):
            raise TranslationLineCountError(
                f"翻译行数不匹配: 期望 {len(non_empty)}，实际 {len(translated)}"
            )
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
