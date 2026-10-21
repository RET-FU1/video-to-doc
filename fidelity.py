"""确定性保真校验（不依赖模型，可单测）。

## 为什么需要它

提示词写着"不要增删改任何文字"，但 LLM 仍会静默改写 / 截断 / 漏段。
这里用三个与模型无关的指标兜底，任一不达标就回退原文并告警：

1. 归一化后长度比不在 [min_ratio, max_ratio]
2. 字符相似度（difflib.SequenceMatcher，autojunk=False）< min_similarity
3. 英文词 / 数字丢失比例 > max_token_loss，或丢失任意长度 >= long_token_min_len
   的英文词（后者往往意味着整段被截掉）

## 为什么单独一个模块

改造前有**两套实现**：

  - `pipeline._verify_polish`（全片校验）：相似度阈值 0.90
  - `summarizer._verify_fidelity`（分块校验）：相似度阈值 0.75

阈值不一致会造成"分块全部通过、全片却被回退"的诡异现象——用户看到的是
"这次没抛光"，而日志里两个阶段各说各话。现在只有一个实现，
阈值以 `Thresholds` 显式传入，不会再各写一份。
"""
import difflib
import logging
import re
from dataclasses import dataclass
from typing import List, Set, Tuple

logger = logging.getLogger(__name__)

# ── 归一化用的正则 ──
# 抛光输入每行带 [MM:SS] 前缀，提示词要求输出时去掉，比对前也必须先去掉。
# 分钟位不限位数：通常 `%02d` 产出 2 位，但超长视频（如 >16h40m 的直播/
# 有声书）会出现 4 位（[1000:00]），写死位数会漏匹配。
TIME_PREFIX_RE = re.compile(r"^\[\d+:\d{2}\]\s*", re.MULTILINE)
# 归一化时去掉的标点 / 空白 / 格式标记（长度与相似度只看正文）
_STRIP_RE = re.compile(
    r"[\s，。、；：？！“”‘’（）《》〈〉【】…—–\-·,.;:?!()\[\]{}<>\"'`~#*|/\\]"
)
_EN_WORD_RE = re.compile(r"[A-Za-z]{3,}")
_NUMBER_RE = re.compile(r"\d+")
# 多说话人模式下模型会合法新增「说话人A：」标签，比对前必须剔除，
# 否则短分块会被误判成"长度异常"而错误回退
_SPEAKER_LABEL_RE = re.compile(
    r"(?:^|(?<=\n))\s*(?:说话人\s*[A-Za-z0-9]+|SPEAKER[_\s]*\d+)\s*[:：]\s*",
    re.MULTILINE,
)


@dataclass(frozen=True)
class Thresholds:
    """一组保真阈值"""

    min_ratio: float = 0.85
    max_ratio: float = 1.25
    min_similarity: float = 0.90
    max_token_loss: float = 0.20
    long_token_min_len: int = 6
    # 归一化后短于这个长度时，相似度判据改用更宽松的 short_text_min_similarity：
    # difflib 对短文本极其敏感（29 字改 3 个字 = 0.897，会被 0.90 误判），
    # 而 split_text 的最后一块可能很短，误杀会让该块退回未加标点的原文。
    # 注意是"放宽"而不是"跳过"——完全跳过会放过中等长度（100~200 字）的改写。
    similarity_min_chars: int = 200
    short_text_min_similarity: float = 0.75


# 抛光：同文只加标点/分段，归一化后应与原文几乎一致 → 要求高相似度
POLISH = Thresholds()


def strip_time_prefix(text: str, *, drop_empty_lines: bool = False) -> str:
    """去掉行首的 [MM:SS] 时间戳标记。

    `drop_empty_lines=True` 时顺带丢弃空行（抛光拼接前整理文本用）；
    保真比对不需要，因为 `normalize()` 本来就会去掉所有空白。
    """
    cleaned = TIME_PREFIX_RE.sub("", text or "")
    if not drop_empty_lines:
        return cleaned
    lines = []
    for line in cleaned.split("\n"):
        line = line.strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def strip_speaker_labels(text: str) -> str:
    """去掉多说话人模式新增的「说话人A：」标签，只留正文"""
    return _SPEAKER_LABEL_RE.sub("", text or "")


def normalize(text: str) -> str:
    """去时间戳、标点、空白，得到只含正文的归一化串"""
    return _STRIP_RE.sub("", strip_time_prefix(text))


def fallback_text(src: str) -> str:
    """校验失败时的回退文本。

    没有 [MM:SS] 前缀时原样返回；有前缀时去掉前缀（否则时间戳会被当成
    正文写进转写稿）。这是"回退原文"语义的最小改动。
    """
    if TIME_PREFIX_RE.search(src or ""):
        return TIME_PREFIX_RE.sub("", src).strip()
    return src


def token_loss(src: str, out: str,
               long_min_len: int = 6) -> Tuple[float, List[str]]:
    """统计英文词 / 数字的丢失情况，返回 (丢失比例, 丢失的长英文词列表)。

    英文词不区分大小写；长英文词（长度 >= `long_min_len`）丢失往往意味着
    整段被截断或漏译。
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
    long_lost = [w for w in lost_words if len(w) >= long_min_len]
    return lost_ratio, long_lost


def verify(src: str, out: str, label: str = "抛光",
           thresholds: Thresholds = POLISH) -> str:
    """校验 `out` 是否忠实于 `src`；不达标则回退原文并告警。

    通过校验时**返回模型输出本身**（不改写、不重排，多说话人模式合法新增的
    「说话人A：」标签会保留），仅额外剥掉模型偶尔忘记去掉的行首 [MM:SS]——
    否则时间戳会混进转写正文。

    比对时剔除说话人标签，避免多说话人模式误报。
    """
    raw_out = out or ""
    compared = strip_speaker_labels(raw_out)
    body = strip_time_prefix(src or "")
    if not body.strip():
        return raw_out
    if not compared.strip():
        logger.warning("%s：模型输出为空，疑似截断，已回退原文", label)
        return fallback_text(src)

    norm_src = normalize(src)
    norm_out = normalize(compared)
    if not norm_src:
        return raw_out

    ratio = len(norm_out) / len(norm_src)
    if not thresholds.min_ratio <= ratio <= thresholds.max_ratio:
        logger.warning(
            "%s：输出长度异常（原文 %d 字 → 输出 %d 字，%.0f%%），疑似改写/截断，已回退原文",
            label, len(norm_src), len(norm_out), ratio * 100,
        )
        return fallback_text(src)

    # 相似度判据按文本长度取不同阈值：短文本用宽容值（见 Thresholds 注释）
    min_similarity = (thresholds.min_similarity
                      if len(norm_src) >= thresholds.similarity_min_chars
                      else thresholds.short_text_min_similarity)
    similarity = difflib.SequenceMatcher(
        None, norm_src, norm_out, autojunk=False).ratio()
    if similarity < min_similarity:
        logger.warning(
            "%s：输出与原文相似度过低（%.2f < %.2f），疑似改写/截断，已回退原文",
            label, similarity, min_similarity,
        )
        return fallback_text(src)

    lost_ratio, long_lost = token_loss(body, compared,
                                       thresholds.long_token_min_len)
    if long_lost:
        logger.warning(
            "%s：输出丢失英文词 %s，疑似漏段/截断，已回退原文",
            label, "、".join(long_lost[:5]),
        )
        return fallback_text(src)
    if lost_ratio > thresholds.max_token_loss:
        logger.warning(
            "%s：输出丢失 %.0f%% 的英文词/数字（超过 %.0f%%），疑似改写/截断，已回退原文",
            label, lost_ratio * 100, thresholds.max_token_loss * 100,
        )
        return fallback_text(src)

    # 通过：原样返回模型输出（保留它合法新增的「说话人A：」标签），
    # 但兜掉模型偶尔忘记去掉的行首 [MM:SS] 时间戳 —— 否则时间戳会混进正文。
    if TIME_PREFIX_RE.search(raw_out):
        logger.debug("%s：模型输出残留时间戳，已剥离", label)
        return strip_time_prefix(raw_out)
    return raw_out
