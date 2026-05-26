"""
字幕提取模块 — 从视频平台字幕（SRT/VTT）解析并转换为 Whisper 兼容格式
包含多维度质量评估：覆盖率、噪音占比、段落密度、语言匹配
"""
import html
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from utils import set_state

logger = logging.getLogger(__name__)

# ── 编译正则（模块级复用） ──

_NOISE_PATTERNS = re.compile(
    r'^\s*[\[\(「]?\s*(?:music|applause|cheering|laughter|silence|noise|sound'
    r'|背景音乐|掌声|音乐|欢呼|笑声|静音|噪音|杂音|♪|♫|～|~)'
    r'\s*[\]\)」]?\s*$',
    re.IGNORECASE,
)
_HTML_TAG = re.compile(r'<[^>]+>')
_TIMESTAMP_LINE = re.compile(r'-->')

# 常见非语音行（不参与质量计算的内容标记）
_META_LABELS = re.compile(
    r'^\s*[\[\(]?\s*(?:music|applause|cheering|laughter|silence|noise|sound|背景音乐|掌声|音乐|欢呼|笑声)\s*[\]\)]?\s*$',
    re.IGNORECASE,
)


@dataclass
class SubtitleQuality:
    """字幕质量评估结果"""
    source: str                # "manual" | "auto_generated"
    coverage: float            # 0-1，字幕覆盖的视频时长占比
    noise_ratio: float         # 0-1，噪音标签占比
    segment_density: float     # 每 10 秒段数
    language_match: float      # 0-1，语言匹配度
    is_acceptable: bool        # 是否可用
    details: List[str] = field(default_factory=list)


# ── 公共 API ──

def parse_subtitle_file(path: Path) -> List[Dict]:
    """解析 SRT 或 VTT 字幕文件，返回 segments [{start, end, text}]"""
    if not path.exists():
        return []
    content = path.read_text(encoding="utf-8-sig")
    if content.lstrip().startswith("WEBVTT"):
        return _parse_vtt(content)
    return _parse_srt(content)


def assess_quality(segments: List[Dict], video_duration: float,
                   source: str, expected_lang: str) -> SubtitleQuality:
    """多维度评估字幕质量"""
    details: List[str] = []

    if not segments:
        return SubtitleQuality(
            source=source, coverage=0.0, noise_ratio=1.0, segment_density=0.0,
            language_match=0.0, is_acceptable=False,
            details=["无有效字幕段落"],
        )

    first_start = segments[0]["start"]
    last_end = segments[-1]["end"]
    covered_duration = last_end - first_start

    # 1. 覆盖率
    coverage = covered_duration / video_duration if video_duration > 0 else 1.0

    # 2. 噪音占比
    noise_count = sum(1 for seg in segments if _is_noise(seg["text"]))
    noise_ratio = noise_count / len(segments) if segments else 0.0

    # 3. 段落密度（每 10 秒段数）
    density = len(segments) / (covered_duration / 10) if covered_duration > 0 else 0.0

    # 4. 语言匹配
    lang_match = _check_language(segments, expected_lang)

    details.append(f"覆盖率={coverage:.0%}")
    details.append(f"噪音={noise_ratio:.0%}")
    details.append(f"密度={density:.1f}条/10秒")
    details.append(f"语言匹配={lang_match:.0%}")

    # 判断
    if source == "manual":
        # 人工字幕：仅检查覆盖率
        is_ok = coverage >= 0.50
        if not is_ok:
            details.append("人工字幕覆盖率不足 (需>50%)")
    else:
        # 自动字幕：全维度检查
        checks = [
            (coverage >= 0.50, f"覆盖率不足 ({coverage:.0%}, 需>50%)"),
            (noise_ratio <= 0.10, f"噪音过高 ({noise_ratio:.0%}, 需<10%)"),
            (1.0 <= density <= 15.0, f"段落密度异常 ({density:.1f}, 需1-15)"),
            (lang_match >= 0.30, f"语言匹配度低 ({lang_match:.0%}, 需>30%)"),
        ]
        is_ok = True
        for check, msg in checks:
            if not check:
                details.append(msg)
                is_ok = False

    details.insert(0, "通过" if is_ok else "不通过")

    return SubtitleQuality(
        source=source, coverage=coverage, noise_ratio=noise_ratio,
        segment_density=density, language_match=lang_match,
        is_acceptable=is_ok, details=details,
    )


def write_transcript_output(segments: List[Dict], stem: str, folder: Path) -> str:
    """写入转写输出（与 Whisper 输出格式一致），返回纯文本"""
    # _segments.json
    seg_path = folder / f"{stem}_segments.json"
    seg_path.write_text(json.dumps(segments, ensure_ascii=False), encoding="utf-8")

    # .txt（与 Transcriber._format_transcript / _run_transcription 输出一致）
    lines = [seg.get("text", "").strip() for seg in segments if seg.get("text", "").strip()]
    transcript_text = "\n".join(lines)
    txt_path = folder / f"{stem}.txt"
    txt_path.write_text(f"# {stem}\n\n{transcript_text}", encoding="utf-8")

    set_state(folder, "transcribed")
    return transcript_text


# ── 时间戳解析 ──

def _parse_timestamp(ts: str) -> float:
    """解析 SRT/VTT 时间戳为秒数。支持 HH:MM:SS.mmm 和 MM:SS.mmm。
    同时处理 SRT 的逗号分隔 (00:00:00,000) 和 VTT 的句点分隔 (00:00:00.000)。
    """
    ts = ts.strip().split()[0]  # 去除 VTT 位置信息（如 "00:00:04.000 line:80%"）
    parts = [p for p in re.split(r'[:,.]', ts) if p]
    if len(parts) >= 4:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]) + int(parts[3]) / 1000
    if len(parts) == 3:
        return int(parts[0]) * 60 + int(parts[1]) + int(parts[2]) / 1000
    return 0.0


# ── SRT 解析 ──

def _parse_srt(content: str) -> List[Dict]:
    segments: List[Dict] = []
    # 按空行分割字幕块
    blocks = re.split(r'\n\s*\n', content.strip())

    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue

        # 找时间戳行（包含 -->）
        ts_idx = _find_timestamp_line(lines)
        if ts_idx is None:
            continue

        parts = lines[ts_idx].split("-->")
        if len(parts) != 2:
            continue

        start_sec = _parse_timestamp(parts[0])
        end_sec = _parse_timestamp(parts[1])

        # 文本 = 时间戳行之后的所有行
        text_lines = [ln for ln in lines[ts_idx + 1:] if ln.strip()]
        text = " ".join(text_lines)
        text = _clean_text(text)
        if text:
            segments.append({"start": start_sec, "end": end_sec, "text": text})

    return segments


# ── VTT 解析 ──

def _parse_vtt(content: str) -> List[Dict]:
    segments: List[Dict] = []
    blocks = re.split(r'\n\s*\n', content.strip())

    for block in blocks:
        lines = block.strip().split("\n")
        if not lines:
            continue

        first = lines[0].strip()
        # 跳过 WEBVTT 头、STYLE / NOTE 块
        if first.startswith("WEBVTT") or first.startswith("STYLE") or first.startswith("NOTE"):
            continue
        # 跳过元数据行（如 Kind: captions, Language: en）
        if ":" in first and not _TIMESTAMP_LINE.search(first) and len(lines) == 1:
            continue

        ts_idx = _find_timestamp_line(lines)
        if ts_idx is None:
            continue

        parts = lines[ts_idx].split("-->")
        if len(parts) != 2:
            continue

        start_sec = _parse_timestamp(parts[0])
        end_sec = _parse_timestamp(parts[1])

        text_lines = []
        for ln in lines[ts_idx + 1:]:
            ln = ln.strip()
            if not ln:
                continue
            # 跳过 VTT cue 设置（如 "line:80%"、"position:50%"、"align:start"）
            if re.match(r'^(?:line|position|align|size|region|vertical):', ln, re.IGNORECASE):
                continue
            text_lines.append(ln)

        text = " ".join(text_lines)
        text = _clean_text(text)
        if text:
            segments.append({"start": start_sec, "end": end_sec, "text": text})

    return segments


# ── 文本清理 ──

def _clean_text(text: str) -> str:
    """去除 HTML 标签、解码实体、压缩空白"""
    text = _HTML_TAG.sub("", text)
    text = html.unescape(text)
    # 处理 YouTube 自动字幕的特殊标记
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


# ── 辅助函数 ──

def _find_timestamp_line(lines: List[str]) -> int:
    """返回包含 '-->' 的行索引，找不到返回 None"""
    for i, line in enumerate(lines):
        if _TIMESTAMP_LINE.search(line):
            return i
    return None


def _is_noise(text: str) -> bool:
    """判断是否为噪音标签（[Music]、[Applause] 等）"""
    return bool(_META_LABELS.match(text))


def _check_language(segments: List[Dict], expected_lang: str) -> float:
    """检测字幕文本是否匹配期望语言，返回 0-1 的匹配度"""
    if expected_lang in ("auto", "", None):
        return 1.0

    total = 0
    match = 0
    for seg in segments:
        for ch in seg.get("text", ""):
            total += 1
            if expected_lang.startswith("zh"):
                # CJK 统一汉字
                if '一' <= ch <= '鿿' or '㐀' <= ch <= '䶿':
                    match += 1
            elif expected_lang in ("en", "ja", "ko"):
                # 非 CJK 目标语言：统计 ASCII 字母
                if ch.isascii() and ch.isalpha():
                    match += 1

    return match / total if total > 0 else 0.0
