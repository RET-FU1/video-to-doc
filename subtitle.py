"""
字幕模块 — 从 whisper 段落时间戳 + 翻译文本生成 SRT 字幕
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def generate_srt(segments_path: Path, text_lines: List[str],
                 output_path: Path, min_duration: float = 1.5,
                 max_duration: float = 7.0) -> Path:
    """从段落时间戳和文本行生成 SRT 字幕文件

    Args:
        segments_path: _segments.json 路径
        text_lines: 翻译或原文文本行（与 segments 一一对应）
        output_path: 输出 .srt 路径
        min_duration: 单条字幕最短持续时间（秒），短于此值会合并
        max_duration: 单条字幕最长持续时间（秒），长于此值会拆分
    """
    segments = _load_segments(segments_path)
    if not segments:
        logger.warning("无段落数据，无法生成字幕")
        return output_path

    # 对齐行数与段落数
    lines = text_lines[:len(segments)]
    while len(lines) < len(segments):
        lines.append("")

    entries = _build_entries(segments, lines, min_duration, max_duration)
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
    with open(output_path, "w", encoding="utf-8") as f:
        for i, entry in enumerate(entries, 1):
            f.write(f"{i}\n")
            f.write(f"{_fmt_srt_time(entry['start'])} --> {_fmt_srt_time(entry['end'])}\n")

            # 合并行文本，有人数上限（2 行）
            text_lines = entry["lines"][:2]
            text = "\n".join(text_lines)

            # 说话人前缀
            speaker = entry.get("speaker", "")
            if speaker and speaker != "UNKNOWN":
                text = f"[{speaker}] {text}"

            f.write(f"{text}\n\n")


def _fmt_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = max(0, min(999, round((seconds % 1) * 1000)))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
