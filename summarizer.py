"""
总结模块 — 可插拔设计
支持 OpenAI 兼容 API（DeepSeek、智谱、通义千问、月之暗面等国内服务商）
支持长文本自动分段总结 + 汇总
"""
import copy
import logging
import time
from typing import Any, Dict, Optional

from openai import OpenAI
from utils import load_env, split_text

logger = logging.getLogger(__name__)

# 可重试的 OpenAI 异常类型
try:
    import openai as _openai
    _RETRYABLE_ERRORS = (
        _openai.APITimeoutError,
        _openai.APIConnectionError,
        _openai.RateLimitError,
        _openai.InternalServerError,
    )
except (ImportError, AttributeError):
    _RETRYABLE_ERRORS = ()


class OpenAICompatSummarizer:
    """OpenAI 兼容 API 总结器 — 支持所有兼容 OpenAI 接口的服务商"""

    STYLE_PROMPTS: Dict[str, str] = {
        "auto": (
            "请写一篇完整的视频内容总结，像一篇精炼的文章，而不是要点罗列。\n"
            "要求：\n"
            "- 先一句话概括核心观点，再展开主要论证和论据，最后总结关键收获\n"
            "- 保留原文中精彩的比喻、案例、数据，它们是让总结生动的关键\n"
            "- 体现出内容之间的逻辑链条，让读者理解「为什么」而不仅是「是什么」\n"
            
        ),
        "knowledge_points": (
            "请提取视频中的全部知识点，以结构化方式列出。每条知识点包括：\n"
            "- 概念名称和简明解释\n"
            "- 这个知识点为什么重要（它解决了什么问题 / 改变了什么认知）\n"
            "- 视频中用来解释这个概念的例子或比喻（如果有的话）\n"
            "按知识点的逻辑依赖关系排序，先基础后进阶"
        ),
        "steps": (
            "请提取视频中的操作步骤或方法论，按逻辑顺序列出。每一步包括：\n"
            "- 做什么：具体行动\n"
            "- 为什么：这一步的必要性和背后的原理\n"
            "- 怎么做：操作细节\n"
            "- 注意事项：常见坑点和避坑方法"
        ),
        "core_ideas": (
            "请提炼视频的核心观点，每条用一句话概括。注意：\n"
            "- 不要只列出话题或主题词，要提炼出观点 / 判断 / 结论\n"
            "- 每条都应该让读者产生「原来如此」的感觉，而不是「嗯，提到了这个」\n"
            "- 保留原文中生动的表达方式，不要改成干巴巴的术语\n"
            "- 按重要性排序，最有洞察力的放在最前面"
        ),
    }

    def __init__(self, config: Dict[str, Any]) -> None:
        import os

        self.config: Dict[str, Any] = config.get("summarizer", {})
        self.model: str = self.config.get("model", "deepseek-chat")
        self.max_chunk: int = int(self.config.get("max_chunk_chars", 80000))
        self.max_tokens: int = int(self.config.get("max_tokens", 4096))
        self._timeout: float = float(self.config.get("timeout", 300))
        self._max_retries: int = int(self.config.get("max_retries", 3))

        base_url: str = self.config.get("base_url", "https://api.deepseek.com")
        api_key: str = os.environ.get("API_KEY", "")
        if not api_key:
            raise ValueError("API_KEY 未设置，请在 .env 文件中配置 API_KEY")

        self.client: OpenAI = OpenAI(api_key=api_key, base_url=base_url, timeout=self._timeout)

    def summarize(self, text: str, meta: Dict[str, Any], style: str = "auto") -> str:
        prompt_instruction = self.STYLE_PROMPTS.get(style, self.STYLE_PROMPTS["auto"])

        if len(text) <= self.max_chunk:
            return self._summarize_chunk(text, meta, prompt_instruction)

        return self._summarize_long(text, meta, prompt_instruction)

    def _call_api(self, messages: list, max_tokens: Optional[int] = None) -> str:
        """带重试的 API 调用"""
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    max_tokens=max_tokens or self.max_tokens,
                    messages=messages,
                )
                content: Optional[str] = response.choices[0].message.content
                return content or ""
            except _RETRYABLE_ERRORS as e:
                last_exc = e
                wait = 2 ** attempt
                logger.warning("API 调用失败 [%d/%d]: %s，%ds 后重试", attempt + 1, self._max_retries, e, wait)
                time.sleep(wait)
            except Exception as e:
                raise

        raise RuntimeError(f"API 调用失败（已重试 {self._max_retries} 次）: {last_exc}")

    def _summarize_chunk(self, text: str, meta: Dict[str, Any], prompt_instruction: str) -> str:
        system_prompt = (
            "你是一位世界级全能专家。你的智力水平、知识广度与思辨深度，"
            "与各领域最顶尖的人才不相上下。"
            "你的任务是基于视频转写文本，撰写一份完整、详尽、有深度的总结。\n\n"
            "核心准则：\n"
            "- 逐步消化内容后重新组织，用自己的理解输出，绝不逐段复述\n"
            "- 保留原文中精彩的比喻、案例、数据——它们比概括性描述更有说服力\n"
            "- 体现内容之间的逻辑链条，让读者理解「为什么」而不仅是「是什么」\n"
            "- 对反常识或令人意外的观点，要突出强调并审视其论证是否站得住脚\n"
            "- 自我核查所有事实、数据、名称、日期——绝不虚构或编造任何内容\n"
            "- 对不确定的信息，明确标注置信度（高/中/低/未知）\n\n"
            "输出要求：\n"
            "- 精准、锐利，不回避质疑视频中的观点——如果某个论证有漏洞，直接指出\n"
            "- 绝不使用「好问题」「你说得对」「有趣」等空洞赞美\n"
            "- 准确性是唯一成功标准，不需要取悦任何人\n\n"
            "使用 Markdown 格式输出。"
        )

        title: str = meta.get("title", "未知标题")
        uploader: str = meta.get("uploader", "")
        duration_sec: int = meta.get("duration", 0)
        duration_min: int = duration_sec // 60 if duration_sec else 0
        duration_str: str = f"{duration_min}" if duration_min else "?"

        user_message = f"""视频标题：{title}
作者：{uploader}
时长：约 {duration_str} 分钟

{prompt_instruction}

以下是视频转写文本：

{text}"""

        return self._call_api(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ]
        )

    def _summarize_long(self, text: str, meta: Dict[str, Any], prompt_instruction: str) -> str:
        """长文本分段总结"""
        chunks = split_text(text, self.max_chunk)
        chunk_summaries: list = []

        for i, chunk in enumerate(chunks):
            logger.info("分段总结 [%d/%d]...", i + 1, len(chunks))
            instruction = (
                f"这是一段视频转写文本的第 {i + 1}/{len(chunks)} 部分。"
                f"请提取这部分的核心内容和关键信息。\n{prompt_instruction}"
            )
            chunk_summaries.append(self._summarize_chunk(chunk, meta, instruction))

        logger.info("汇总所有分段...")
        combined = "\n\n---\n\n".join(
            f"## 第 {i + 1} 部分\n{s}" for i, s in enumerate(chunk_summaries)
        )

        merge_instruction = (
            "请基于以下多个部分的总结，写一份完整的总体总结。"
            "去重合并重复的内容，按逻辑重新组织，"
            "保持生动的表达方式，保留精彩的例子和数据。"
        )
        return self._summarize_chunk(combined, meta, merge_instruction)


class OllamaSummarizer(OpenAICompatSummarizer):
    """Ollama 本地模型总结器"""

    def __init__(self, config: Dict[str, Any]) -> None:
        config = copy.deepcopy(config)
        config.setdefault("summarizer", {})["base_url"] = \
            config.get("summarizer", {}).get("base_url", "http://localhost:11434/v1")
        super().__init__(config)


def create_summarizer(config: Dict[str, Any]) -> OpenAICompatSummarizer:
    """工厂函数：根据配置创建总结器"""
    load_env()

    provider: str = config.get("summarizer", {}).get("provider", "openai")

    if provider == "openai":
        return OpenAICompatSummarizer(config)
    if provider == "ollama":
        return OllamaSummarizer(config)

    raise ValueError(f"未知的总结器: {provider}")