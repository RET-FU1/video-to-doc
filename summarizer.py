"""
总结模块 — 可插拔设计
支持 OpenAI 兼容 API（DeepSeek、智谱、通义千问、月之暗面等国内服务商）
支持长文本自动分段总结 + 汇总
"""
import difflib
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from openai import OpenAI
from utils import load_env, split_text

logger = logging.getLogger(__name__)

# ── 抛光保真守卫用的正则 ──
# 抛光输入每行带 [MM:SS] 时间戳前缀，提示词要求输出时去掉，比对前也必须先去掉。
# 分块文本单独送 LLM 后可能跨小时，分钟位允许 2 位以上（pipeline 用 %02d 格式化）。
_TIME_PREFIX_RE = re.compile(r"^\[\d{2,}:\d{2}\]\s*", re.MULTILINE)
# 归一化时去掉的标点 / 空白 / 格式标记（长度与相似度比对只看正文）
_FIDELITY_STRIP_RE = re.compile(
    r"[\s，。、；：？！“”‘’（）《》〈〉【】…—–\-·,.;:?!()\[\]{}<>\"'`~#*|/\\]"
)
_EN_WORD_RE = re.compile(r"[A-Za-z]{3,}")
_NUMBER_RE = re.compile(r"\d+")
# 多说话人模式下模型会合法地新增「说话人A：」这类标签，比对前要剔除，
# 否则短分块会被误判成"长度异常"（标签占比过大）而错误回退原文
_SPEAKER_LABEL_RE = re.compile(
    r"(?:^|(?<=\n))\s*(?:说话人\s*[A-Za-z0-9]+|SPEAKER[_\s]*\d+)\s*[:：]\s*",
    re.MULTILINE,
)


def _strip_time_prefix(text: str) -> str:
    """去掉行首的 [MM:SS] 时间戳标记（提示词要求输出不含时间标记）"""
    return _TIME_PREFIX_RE.sub("", text or "")


def _fallback_text(src: str) -> str:
    """保真校验失败时的回退文本。

    没有 [MM:SS] 前缀时原样返回 src；有前缀时去掉前缀（否则时间戳会被当成
    正文写进转写稿），这是"回退原文"语义的最小改动。
    """
    if _TIME_PREFIX_RE.search(src or ""):
        return _TIME_PREFIX_RE.sub("", src).strip()
    return src


def _normalize_for_fidelity(text: str) -> str:
    """去时间戳、标点、空白，得到只含正文的归一化串"""
    return _FIDELITY_STRIP_RE.sub("", _strip_time_prefix(text))


def _strip_speaker_labels(text: str) -> str:
    """去掉多说话人模式新增的「说话人A：」标签，只留正文"""
    return _SPEAKER_LABEL_RE.sub("", text or "")


def _token_loss(src: str, out: str) -> Tuple[float, List[str]]:
    """统计英文词 / 数字的丢失情况，返回 (丢失比例, 丢失的长英文词列表)。

    英文词不区分大小写；长英文词（长度 >= 6）丢失往往意味着整段被截断或漏译。
    """
    src_words: Set[str] = {w.lower() for w in _EN_WORD_RE.findall(src)}
    out_words: Set[str] = {w.lower() for w in _EN_WORD_RE.findall(out)}
    src_nums: Set[str] = set(_NUMBER_RE.findall(src))
    out_nums: Set[str] = set(_NUMBER_RE.findall(out))

    total = len(src_words) + len(src_nums)
    if total == 0:
        return 0.0, []

    lost_words = sorted(w for w in src_words if w not in out_words)
    lost_nums = sorted(n for n in src_nums if n not in out_nums)
    lost_ratio = (len(lost_words) + len(lost_nums)) / total
    long_lost = [w for w in lost_words if len(w) >= 6]
    return lost_ratio, long_lost


def _verify_fidelity(src: str, out: str, label: str = "抛光") -> str:
    """确定性保真守卫：输出明显异常时回退原文并告警。

    提示词写着"不要增删改任何文字"，但模型仍会静默改写 / 截断 / 漏段，
    之前只做 `return content or text`，完全发现不了。这里用三个与模型无关的
    指标兜底（只校验 src，context 是上下文、不参与校验）：

    1. 归一化后长度比不在 [0.85, 1.25] → 回退
    2. 字符相似度（difflib，autojunk=False）< 0.75 → 回退
    3. 英文词 / 数字丢失超过 20%，或丢失任意长度 >= 6 的英文词 → 回退

    比对时会剔除模型合法新增的「说话人X：」标签，避免多说话人模式误报；
    但**通过校验时原样返回模型输出**，绝不修改它的合法产物（否则多说话人
    模式的标注会被守卫吃掉）。
    """
    raw_out = out or ""
    compared = _strip_speaker_labels(raw_out)
    body = _strip_time_prefix(src or "")
    if not body.strip():
        return raw_out
    if not compared.strip():
        logger.warning("%s：模型输出为空，疑似截断，已回退原文", label)
        return _fallback_text(src)

    norm_src = _normalize_for_fidelity(src)
    norm_out = _normalize_for_fidelity(compared)
    if not norm_src:
        return raw_out

    ratio = len(norm_out) / len(norm_src)
    if not 0.85 <= ratio <= 1.25:
        logger.warning(
            "%s：输出长度异常（原文 %d 字 → 输出 %d 字，%.0f%%），疑似改写/截断，已回退原文",
            label, len(norm_src), len(norm_out), ratio * 100,
        )
        return _fallback_text(src)

    similarity = difflib.SequenceMatcher(None, norm_src, norm_out, autojunk=False).ratio()
    if similarity < 0.75:
        logger.warning(
            "%s：输出与原文相似度过低（%.2f），疑似改写/截断，已回退原文",
            label, similarity,
        )
        return _fallback_text(src)

    lost_ratio, long_lost = _token_loss(body, compared)
    if long_lost:
        logger.warning(
            "%s：输出丢失英文词 %s，疑似漏段/截断，已回退原文",
            label, "、".join(long_lost[:5]),
        )
        return _fallback_text(src)
    if lost_ratio > 0.20:
        logger.warning(
            "%s：输出丢失 %.0f%% 的英文词/数字（超过 20%%），疑似改写/截断，已回退原文",
            label, lost_ratio * 100,
        )
        return _fallback_text(src)

    # 校验通过：原样返回模型输出（保留它合法新增的「说话人A：」标签），
    # 只兜掉模型偶尔忘记去掉的行首 [MM:SS] 时间戳
    if _TIME_PREFIX_RE.search(raw_out):
        logger.debug("%s：模型输出残留时间戳，已剥离", label)
        return _strip_time_prefix(raw_out)
    return raw_out


# ── API 服务商预设 ──
# 选一个 provider 即可，base_url 和 model 会自动填充
API_PROVIDERS = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-pro",
        "polish_model": "deepseek-flash",
        "description": "推荐，国内直连，10 元用很久",
    },
    "zhipu": {
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "polish_model": "glm-4-flash",
        "description": "有免费额度",
    },
    "tongyi": {
        "name": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "polish_model": "qwen-turbo",
        "description": "有免费额度",
    },
    "moonshot": {
        "name": "月之暗面",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "polish_model": "moonshot-v1-8k",
        "description": "",
    },
    "ollama": {
        "name": "Ollama（本地）",
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:7b",
        "polish_model": "",
        "description": "需先本地安装 Ollama",
    },
    "mimo": {
        "name": "小米 MiMo",
        "base_url": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2-pro",
        "polish_model": "mimo-v2-flash",
        "description": "1M 上下文，性价比高",
    },
}

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
    import logging
    _logger = logging.getLogger(__name__)
    _logger.warning("无法导入 OpenAI 可重试异常类型，API 调用将不会自动重试")


class OpenAICompatSummarizer:
    """OpenAI 兼容 API 总结器 — 支持所有兼容 OpenAI 接口的服务商"""

    STYLE_PROMPTS: Dict[str, str] = {
        "auto": (
            "请写一篇完整的视频内容总结，像一篇精炼的文章，而不是要点罗列。\n"
            "要求：\n"
            "- 先一句话概括核心观点，再展开主要论证和论据，最后总结关键收获\n"
            "- 保留原文中精彩的比喻、案例、数据，它们是让总结生动的关键\n"
            "- 体现出内容之间的逻辑链条，让读者理解「为什么」而不仅是「是什么」"
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
        "expert": (
            "你是一位世界级的全能专家。你的智力水平、知识广度、深邃的思考过程与博学程度，与最顶尖的人不相上下。\n"
            "请以完整、详尽、具体的标准来分析视频内容。\n"
            "\n"
            "分析准则：\n"
            "- 逐步消化视频内容后用自己的理解重新组织，绝不逐段复述\n"
            "- 自我核查所有事实、数据、名称、日期——对关键信息进行双重检查\n"
            "- 绝不虚构或捏造任何内容；如果某处无法确定或视频中未明确，直接说明，不要含糊带过\n"
            "- 不要盲信视频中给出的数字；先用自己的知识独立验证其合理性\n"
            "- 对每个关键论断明确标注置信度（高/中/低/未知）\n"
            "- 尽可能使分析长而详细\n"
            "\n"
            "批判准则：\n"
            "- 在认可视频观点之前，先构建最强有力的反驳论点——主动寻找论证中的薄弱环节\n"
            "- 如果视频的论证有漏洞或事实错误，立即指出，不为视频作者留余地\n"
            "- 如果视频的结论经不起推敲，坚持你的判断，不因其立场或情绪而让步\n"
            "- 得出负面结论或指出视频质量低下完全没问题——传递坏消息是你的职责\n"
            "- 不被视频的立场裹挟，拒绝为政治正确而粉饰问题\n"
            "\n"
            "语气准则：\n"
            "- 精准、锐利，但不尖锐或迂腐\n"
            "- 你的分析可以而且应该具有挑战性、争论性和针对性——你是在做独立批判，不是在写赞美信\n"
            "- 不要附加免责声明，不要提及道德伦理，不要使用「值得学习」「值得关注」等说教措辞\n"
            "- 不必在意视频作者的感受或礼节——对内容不对人\n"
            "- 不使用「好问题」「有趣的视角」「讲得很好」等空洞称赞\n"
            "- 永远不为指出问题而道歉\n"
            "\n"
            "准确性是唯一成功标准，不是视频作者的认可。"
        ),
    }

    def __init__(self, config: Dict[str, Any]) -> None:
        import os

        self.config: Dict[str, Any] = config.get("summarizer", {})
        self.max_chunk: int = int(self.config.get("max_chunk_chars", 80000))
        self.max_tokens: int = int(self.config.get("max_tokens", 4096))
        self._timeout: float = float(self.config.get("timeout", 300))
        self._max_retries: int = int(self.config.get("max_retries", 3))
        # 抛光用的独立预算：思考型模型在 16k 内仍可能截断正文，允许用户调大
        self.polish_max_tokens: int = int(self.config.get("polish_max_tokens", 16384))
        self.polish_timeout: float = float(self.config.get("polish_timeout", 120))

        # 解析 API 提供商：支持预设名称（deepseek / zhipu / tongyi / moonshot / ollama）
        # 也兼容旧格式（openai / ollama 写在 provider 字段）
        provider_key: str = self.config.get("api_provider", "") or self.config.get("provider", "")
        preset = API_PROVIDERS.get(provider_key, None)

        if preset:
            # 使用预设：自动填充 base_url 和 model
            base_url: str = self.config.get("base_url") or preset["base_url"]
            self.model: str = self.config.get("model") or preset["model"]
            self.polish_model: str = self.config.get("polish_model") or preset["polish_model"]
        else:
            # 自定义模式：用户手动填写 base_url 和 model
            base_url: str = self.config.get("base_url", "https://api.deepseek.com")
            self.model: str = self.config.get("model", "deepseek-v4-pro")
            self.polish_model: str = self.config.get("polish_model", "")

        api_key: str = os.environ.get("API_KEY", "")
        if not api_key:
            raise ValueError("API_KEY 未设置，请在 .env 文件中配置 API_KEY")

        self.client: OpenAI = OpenAI(api_key=api_key, base_url=base_url, timeout=self._timeout)

    def summarize(self, text: str, meta: Dict[str, Any], style: str = "auto") -> str:
        # 自定义提示词优先
        if style == "custom":
            prompt_instruction = self.config.get("custom_prompt", "")
            if not prompt_instruction:
                logger.warning("未配置 custom_prompt，回退到 auto 风格")
                prompt_instruction = self.STYLE_PROMPTS["auto"]
        else:
            prompt_instruction = self.STYLE_PROMPTS.get(style, self.STYLE_PROMPTS["auto"])

        if len(text) <= self.max_chunk:
            return self._summarize_chunk(text, meta, prompt_instruction)

        return self._summarize_long(text, meta, prompt_instruction)

    def _call_api(self, messages: list, max_tokens: Optional[int] = None,
                  timeout: Optional[float] = None, model: Optional[str] = None) -> str:
        """带重试的 API 调用。

        推理模型（DeepSeek V4 系列等）会先输出思考内容再输出正文，
        max_tokens 限制的是「思考 + 正文」的总预算。思考过长时正文
        可能为空或被截断（finish_reason == "length"）。此时把 token
        预算加倍重试（最多加倍 2 次），避免静默产出空白/半截内容。
        """
        tokens: int = max_tokens or self.max_tokens
        last_exc: Optional[Exception] = None
        escalated: int = 0  # 已因正文缺失而加倍的次数

        for attempt in range(self._max_retries + 2):
            try:
                response = self.client.chat.completions.create(
                    model=model or self.model,
                    max_tokens=tokens,
                    messages=messages,
                    timeout=timeout or self._timeout,
                )
                choice = response.choices[0]
                content: Optional[str] = choice.message.content
                finish: str = choice.finish_reason or ""

                if (not content or finish == "length") and escalated < 2:
                    escalated += 1
                    reason = "正文为空（思考占满预算）" if not content else "正文疑似截断"
                    logger.warning("%s: finish=%s, max_tokens=%d，预算加倍到 %d 后重试",
                                   reason, finish, tokens, tokens * 2)
                    tokens *= 2
                    continue

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
            "你是一位专业的文档整理助手。你的任务是基于视频转写文本撰写总结。\n\n"
            "核心准则：\n"
            "- 忠于原文内容，不虚构、不编造任何信息\n"
            "- 保留原文中精彩的比喻、案例、数据\n"
            "- 体现内容之间的逻辑链条，让读者理解「为什么」而不仅是「是什么」\n"
            "- 对不确定的信息明确标注置信度\n\n"
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

    def _polish_prompt(self, text: str, context: str, multispeaker: bool) -> str:
        """构造抛光提示词。context 是上一分块末尾原文，仅供衔接参考、不得输出。"""
        context_section = ""
        if context.strip():
            context_section = (
                "【上文参考（仅供理解衔接，绝对不要输出、不要翻译、不要重复）】\n"
                "<<<上文开始\n"
                f"{context.strip()}\n"
                "上文结束>>>\n\n"
            )

        if multispeaker:
            rules = (
                "你是一个中文语音转写文本格式化助手。请对以下语音转写文本做三件事：\n"
                "1. 根据对话内容和时间间隔识别不同的说话人，在说话人切换时标注前缀\n"
                "2. 添加合适的标点符号（逗号、句号、问号等）\n"
                "3. 按语义将文本拆分为合适的段落（用空行分隔）\n\n"
                "提示：每行开头的 [MM:SS] 是该片段的起始时间。"
                "时间间隔较大（如 >5 秒）通常意味着说话人转换或话题切换。"
                "连续密集的片段通常属于同一说话人、同一段落。\n\n"
                "识别说话人的线索（按优先级）：\n"
                "- 内容中的问答关系（问句后紧跟的回答通常来自不同人）\n"
                "- 观点的交替和转折（\"但是\"、\"不过\"、\"我觉得\"、\"不对\"等转折词暗示切换）\n"
                "- 时间间隔变化（长时间间隔通常暗示说话人转换）\n"
                "- 语义的边界（话题切换时通常涉及不同说话人）\n\n"
                "规则：\n"
                "- 说话人标签格式为「说话人A：」「说话人B：」，以此类推\n"
                "- 每个新段落开头标注说话人，段落内同一人连续话语不重复标注\n"
                "- 如果全文明显只有一个人说话（无问答、无观点交替、时间间隔均匀），则不添加说话人标签\n"
                "- 只添加标点、段落分隔和说话人标注，不要修改任何文字内容\n"
                "- 不要增删改任何词语，保持原文字不变\n"
                "- 输出时去掉行首的 [MM:SS] 时间标记\n"
                "- 每段内容连续书写，不要在段落内部额外换行\n"
                "- 每段5-12句话为宜，段落间用空行分隔\n\n"
                "直接输出格式化后的文本，不要任何解释。\n\n"
            )
        else:
            rules = (
                "你是一个中文文本格式化助手。请对以下语音转写文本做两件事：\n"
                "1. 添加合适的标点符号（逗号、句号、问号等）\n"
                "2. 按语义将文本拆分为合适的段落（用空行分隔）\n\n"
                "提示：每行开头的 [MM:SS] 是该片段的起始时间。"
                "时间间隔较大（如 >5 秒）通常意味着话题切换或说话人转换，"
                "应在该处分段。连续密集的片段通常属于同一段落。\n\n"
                "规则：\n"
                "- 只添加标点和段落分隔，不要修改任何文字内容\n"
                "- 不要增删改任何词语，保持原文字不变\n"
                "- 输出时去掉行首的 [MM:SS] 时间标记\n"
                "- 连续多个短片段通常属于同一段落，应合并书写\n"
                "- 每段5-12句话为宜，段落间用空行分隔\n"
                "- 每段内容连续书写，不要在段落内部额外换行\n\n"
                "直接输出格式化后的文本，不要任何解释。\n\n"
            )

        return f"{rules}{context_section}以下是转写文本：\n\n{text}"

    def polish(self, text: str, context: str = "") -> str:
        """为转写文本添加标点并分段，使用 polish_model（若配置则独立，否则复用主模型）

        Args:
            text: 待抛光的转写文本（可带 [MM:SS] 行首时间戳）
            context: 上一分块的末尾原文，仅作为衔接上下文传给模型；
                     绝不参与保真校验，也不会出现在返回值里
        """
        model = self.polish_model or self.model
        content = self._call_api(
            messages=[{"role": "user", "content": self._polish_prompt(text, context, False)}],
            max_tokens=self.polish_max_tokens,
            timeout=self.polish_timeout,
            model=model,
        )
        return _verify_fidelity(text, content, label="抛光")

    def polish_multispeaker(self, text: str, context: str = "") -> str:
        """为转写文本做说话人识别 + 添加标点 + 分段，使用 polish_model

        参数语义同 polish()：context 只作衔接参考，不校验、不输出。
        """
        model = self.polish_model or self.model
        content = self._call_api(
            messages=[{"role": "user", "content": self._polish_prompt(text, context, True)}],
            max_tokens=self.polish_max_tokens,
            timeout=self.polish_timeout,
            model=model,
        )
        return _verify_fidelity(text, content, label="多说话人抛光")


def create_summarizer(config: Dict[str, Any]) -> OpenAICompatSummarizer:
    """工厂函数：根据配置创建总结器。支持预设名称 (deepseek/zhipu/tongyi/moonshot/ollama) 和自定义。"""
    load_env()

    cfg = config.setdefault("summarizer", {})

    # 兼容旧配置：将 provider 字段迁移为 api_provider
    old_provider = cfg.get("provider", "")
    if old_provider and not cfg.get("api_provider"):
        if old_provider == "ollama":
            cfg["api_provider"] = "ollama"
        else:
            cfg["api_provider"] = "deepseek"  # 旧版 openai 默认即 DeepSeek

    return OpenAICompatSummarizer(config)