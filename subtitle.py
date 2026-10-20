"""
字幕模块 — 从 whisper 段落时间戳 + 翻译文本生成 SRT 字幕
"""
import json
import logging
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

logger = logging.getLogger(__name__)

# 单行显示宽度上限（以半角字符为单位，CJK 记 2）：约 17 个汉字或 34 个半角字符
_MAX_LINE_WIDTH: int = 34
# 中文标点：若上一段以这些字符结尾，直接拼接，不再插入分隔
_SEP_AFTER = set("，。、；：？！…—,.;:?!)]}」』》】）\"'")
# 句末标点：拆行时优先在这些字符之后断开
_SENTENCE_END = set("。！？；…，、,.!?;:")


def _display_width(text: str) -> int:
    """估算显示宽度：CJK 全角字符记 2，其余记 1

    只认 W(全角) / F(全角形式)：A(模糊) 里既有中文引号也有 — … · 这类
    排版符号，一律算 1 更接近实际渲染宽度，避免把一行拆得过碎。
    """
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _join_lines(lines: Sequence[str]) -> str:
    """把同一 cue 的多行合并成单行文本（绝不丢字）

    合并规则：上一行以标点结尾直接相连；两侧都是半角字符时用空格连接；
    其余情况用中文逗号衔接，保持可读性。
    """
    joined = ""
    for raw in lines:
        part = (raw or "").strip()
        if not part:
            continue
        if not joined:
            joined = part
            continue
        if joined[-1] in _SEP_AFTER:
            joined += part
        elif joined[-1].isascii() and part[0].isascii():
            joined += " " + part
        else:
            joined += "，" + part
    return joined


def _split_balanced(text: str) -> List[str]:
    """按显示宽度把文本均分成两行，优先在句末标点处断开（不丢字）

    候选断点按优先级打分：句末标点 > 逗号类标点/空白 > 中点。同分取更靠近
    中线者；实在找不到自然断点才退化为字符数中点（只为可读性，绝不丢字）。
    """
    if not text:
        return [""]
    half = _display_width(text) / 2
    best: Optional[tuple] = None
    width = 0
    for idx, ch in enumerate(text[:-1]):
        width += _display_width(ch)
        if not (ch in _SEP_AFTER or ch.isspace()):
            continue
        # 越"自然"的断点优先级越高（句末 0、其他标点/空白 1），同优先级取更靠近中线者
        priority = 0 if ch in _SENTENCE_END else 1
        score = (priority, abs(width - half))
        if best is None or score < best[0]:
            best = (score, idx + 1)
    cut = best[1] if best else max(1, len(text) // 2)
    return [text[:cut].strip(), text[cut:].strip()]


def _format_cue_lines(text: str) -> List[str]:
    """把 cue 文本排版成 1~2 行（优先单行），超长时按宽度拆行，绝不截断"""
    if _display_width(text) <= _MAX_LINE_WIDTH:
        return [text]
    return [line for line in _split_balanced(text) if line]


def _cue_parts(entry: Dict[str, Any]) -> List[str]:
    """把一个 entry 拆成 1~2 条 cue 的文本（全部文字都保留）

    - 合并后宽度 <= 2 行上限：单条 cue（内部最多两行显示）
    - 否则：按宽度比例拆成两条 cue，时间也按比例分配
    """
    # 兼容两种 entry：内部 _build_entries 产出的 "lines"，以及外部直接给的 "text"
    raw_lines = entry.get("lines")
    if raw_lines is None:
        raw_lines = [entry.get("text", "")]
    text = _join_lines(raw_lines)
    if not text:
        return []
    if _display_width(text) <= _MAX_LINE_WIDTH * 2:
        return [text]
    half = _split_balanced(text)
    return [part for part in half if part]


def generate_srt(segments: Union[Sequence[Dict[str, Any]], Path, str],
                 text_lines: List[str], output_path: Path,
                 min_duration: float = 1.5,
                 max_duration: float = 7.0) -> Path:
    """从段落时间戳和文本行生成 SRT 字幕文件

    Args:
        segments: 段落列表（含 start/end/text，可选 speaker）；
                  也兼容旧签名 —— 传 _segments.json 的路径时先读文件
        text_lines: 翻译或原文文本行（与 segments 一一对应）
        output_path: 输出 .srt 路径
        min_duration: 单条字幕最短持续时间（秒），短于此值会合并
        max_duration: 单条字幕最长持续时间（秒），长于此值会拆分
    """
    if isinstance(segments, (str, Path)):
        segment_list = _load_segments(Path(segments))
    else:
        segment_list = list(segments)

    if not segment_list:
        logger.warning("无段落数据，无法生成字幕")
        return output_path

    # 对齐行数与段落数（不一致要告警，不能静默丢弃文本）
    lines = list(text_lines)
    if len(lines) > len(segment_list):
        logger.warning("文本行数(%d)多于段落数(%d)，多出的 %d 行缺少时间戳，无法写入字幕",
                       len(lines), len(segment_list), len(lines) - len(segment_list))
        lines = lines[:len(segment_list)]
    elif len(lines) < len(segment_list):
        logger.warning("文本行数(%d)少于段落数(%d)，缺少文本的 %d 个段落将不产出字幕",
                       len(lines), len(segment_list), len(segment_list) - len(lines))
        lines = lines + [""] * (len(segment_list) - len(lines))

    entries = _build_entries(segment_list, lines, min_duration, max_duration)
    _write_srt(entries, output_path)
    logger.info("字幕已生成: %s", output_path.name)
    return output_path


def _load_segments(segments_path: Path) -> List[Dict[str, Any]]:
    if not segments_path.exists():
        return []
    with open(segments_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _build_entries(segments: List[Dict[str, Any]], lines: List[str],
                   min_dur: float, max_dur: float) -> List[Dict[str, Any]]:
    """将短段落合并为可读的字幕条目"""
    entries: List[Dict[str, Any]] = []
    current = None

    for seg, text in zip(segments, lines):
        text = text.strip()
        if not text:
            if current:
                entries.append(current)
                current = None
            continue

        start = seg.get("start", 0)
        end = seg.get("end", start + 1)
        speaker = seg.get("speaker", "")

        if current is None:
            current = {"start": start, "end": end, "lines": [text], "speaker": speaker}
            continue

        # 合并条件：同说话人 & 合并后时长 < max_duration
        combined_end = max(current["end"], end)
        if (speaker == current["speaker"] and
                combined_end - current["start"] <= max_dur and
                end - current["end"] < min_dur * 2):
            current["end"] = combined_end
            current["lines"].append(text)
        else:
            entries.append(current)
            current = {"start": start, "end": end, "lines": [text], "speaker": speaker}

    if current:
        entries.append(current)

    return entries


def _write_srt(entries: List[Dict[str, Any]], output_path: Path) -> None:
    """写 SRT。合并多条文本时绝不截断，必要时拆成两条 cue。"""
    merged_count = 0   # 多条原始段落被合并进同一 cue 的条目数
    split_count = 0    # 因过长被拆成两条 cue 的条目数

    with open(output_path, "w", encoding="utf-8") as f:
        index = 0
        for entry in entries:
            parts = _cue_parts(entry)
            if not parts:
                continue
            if len(entry.get("lines", [])) > 1:
                merged_count += 1
            if len(parts) > 1:
                split_count += 1

            start = float(entry.get("start", 0))
            end = float(entry.get("end", start + 1))
            if end < start:
                end = start
            speaker = entry.get("speaker", "")

            # 多条 cue 时按字符占比分配时间，避免第二条时间倒挂
            total_width = sum(max(1, _display_width(p)) for p in parts)
            cursor = start
            for i, part in enumerate(parts):
                if i == len(parts) - 1:
                    cue_end = end
                else:
                    span = (end - start) * max(1, _display_width(part)) / total_width
                    cue_end = min(end, cursor + span)
                cue_start = cursor
                cursor = cue_end

                cue_lines = _format_cue_lines(part)
                if speaker and speaker != "UNKNOWN":
                    cue_lines = [f"[{speaker}] {cue_lines[0]}"] + cue_lines[1:]

                index += 1
                f.write(f"{index}\n")
                f.write(f"{_fmt_srt_time(cue_start)} --> {_fmt_srt_time(cue_end)}\n")
                f.write("\n".join(cue_lines) + "\n\n")

    if merged_count:
        logger.warning("字幕合并：%d 条 cue 由多个短段落合并而成（文字已全部保留）", merged_count)
    if split_count:
        logger.warning("字幕拆分：%d 条过长 cue 已拆为两条（文字已全部保留）", split_count)


def _fmt_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = max(0, min(999, round((seconds % 1) * 1000)))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
