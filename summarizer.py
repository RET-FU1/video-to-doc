"""
总结模块 — 可插拔设计
支持 OpenAI 兼容 API（DeepSeek、智谱、通义千问、月之暗面等国内服务商）
支持长文本自动分段总结 + 汇总
"""
import copy
from utils import load_env


class BaseSummarizer:
    """总结器基类"""

    def summarize(self, text, meta, style="auto"):
        raise NotImplementedError


class OpenAICompatSummarizer(BaseSummarizer):
    """OpenAI 兼容 API 总结器 — 支持所有兼容 OpenAI 接口的服务商"""

    STYLE_PROMPTS = {
        "auto": "请对这个视频内容做一个全面的总结。包括：核心主题、主要观点、关键结论。",
        "knowledge_points": "请提取这个视频中的全部知识点，以结构化方式列出。每条知识点包括：概念名称、解释、在视频中的位置。",
        "steps": "请将这个视频中的操作步骤或方法论逐个提取出来，按顺序列出。每一步包括：做什么、怎么做、注意事项。",
        "core_ideas": "请提炼这个视频的核心思想/观点，用不超过10条列出，每条一句话。",
    }

    def __init__(self, config):
        import os
        from openai import OpenAI

        self.config = config.get("summarizer", {})
        self.model = self.config.get("model", "deepseek-chat")
        self.max_chunk = self.config.get("max_chunk_tokens", 80000)
        self.max_tokens = self.config.get("max_tokens", 4096)

        base_url = self.config.get("base_url", "https://api.deepseek.com")
        api_key = os.environ.get("API_KEY", "")
        if not api_key:
            raise ValueError("API_KEY 未设置，请在 .env 文件中配置 API_KEY")

        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def summarize(self, text, meta, style="auto"):
        prompt_instruction = self.STYLE_PROMPTS.get(style, self.STYLE_PROMPTS["auto"])

        if len(text) <= self.max_chunk:
            return self._summarize_chunk(text, meta, prompt_instruction)

        return self._summarize_long(text, meta, prompt_instruction)

    def _summarize_chunk(self, text, meta, prompt_instruction):
        system_prompt = (
            "你是一个专业的视频内容总结助手。你的任务是阅读视频转写文本，"
            "用简洁清晰的中文做出总结。使用 Markdown 格式输出。"
        )

        title = meta.get("title", "未知标题")
        uploader = meta.get("uploader", "")
        duration_sec = meta.get("duration", 0)
        duration_min = duration_sec // 60 if duration_sec else "?"

        user_message = f"""视频标题：{title}
作者：{uploader}
时长：约 {duration_min} 分钟

{prompt_instruction}

以下是视频转写文本：

{text}"""

        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        )

        return response.choices[0].message.content

    def _summarize_long(self, text, meta, prompt_instruction):
        """长文本分段总结"""
        chunks = self._split_text(text, self.max_chunk)
        chunk_summaries = []

        for i, chunk in enumerate(chunks):
            print(f"  分段总结 [{i+1}/{len(chunks)}]...")
            instruction = (
                f"这是一段视频转写文本的第 {i+1}/{len(chunks)} 部分。"
                f"请提取这部分的核心内容和关键信息。\n{prompt_instruction}"
            )
            chunk_summaries.append(self._summarize_chunk(chunk, meta, instruction))

        print("  汇总所有分段...")
        combined = "\n\n---\n\n".join(
            f"## 第 {i+1} 部分\n{s}" for i, s in enumerate(chunk_summaries)
        )

        merge_instruction = (
            "请基于以下多个部分的总结，写一份完整的、结构清晰的总体总结。"
            "去重合并重复的内容，按逻辑重新组织。"
        )
        return self._summarize_chunk(combined, meta, merge_instruction)

    @staticmethod
    def _split_text(text, max_chars):
        """按段落粗略分段"""
        paragraphs = text.split("\n\n")
        chunks = []
        current = ""

        for p in paragraphs:
            if len(current) + len(p) < max_chars:
                current += p + "\n\n"
            else:
                if current:
                    chunks.append(current.strip())
                current = p + "\n\n"

        if current.strip():
            chunks.append(current.strip())

        return chunks or [text]


class OllamaSummarizer(OpenAICompatSummarizer):
    """Ollama 本地模型总结器"""

    def __init__(self, config):
        config = copy.deepcopy(config)
        config.setdefault("summarizer", {})["base_url"] = \
            config.get("summarizer", {}).get("base_url", "http://localhost:11434/v1")
        super().__init__(config)


def create_summarizer(config):
    """工厂函数：根据配置创建总结器"""
    load_env()

    provider = config.get("summarizer", {}).get("provider", "openai")

    if provider == "openai":
        return OpenAICompatSummarizer(config)
    if provider == "ollama":
        return OllamaSummarizer(config)

    raise ValueError(f"未知的总结器: {provider}")
