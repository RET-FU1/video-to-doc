"""下载格式选择：把「画质预设」翻译成 yt-dlp 的 format 候选链。

## 为什么要候选链

yt-dlp 的 `a/b/c` 只在"格式**不可用**"时降级，**不会**在"下载**出错**"时降级。
而 B站 会把同一个视频的不同编码分发到不同 CDN，其中部分节点是坏的：
实测 2026-10，`mountaintoys.cn` 上的 1080p AVC 流一开始下载就
`ConnectionResetError 10054`，而同一视频的 HEVC/AV1 流在正常 CDN 上。

早期代码为"稳定"强制选 AVC（`bestvideo[vcodec^=avc]`），结果恰好选中坏节点，
且 yt-dlp 认为该格式"可用"，于是一直在坏节点上重试到失败。

现在改为：从元信息里枚举真实可用的流，按「画质优先、同画质按编码兼容性」
排出一串候选，下载失败就换下一个 —— 换编码等于换 CDN，通常 10 秒就能下完。

本模块只做纯计算（不发起网络/进程），便于单测。
"""
from typing import Any, Dict, List

# 最多生成多少个候选（避免在同一个视频上无限换格式重试）
MAX_FORMAT_CANDIDATES = 5

# 各画质预设的静态降级链（拿不到元信息时兜底）。
# 说明：优先 AVC（兼容性最好），失败退回任意编码。
_QUALITY_MAP: Dict[str, str] = {
    "best":   "bestvideo[vcodec^=avc]+bestaudio/bestvideo+bestaudio/best",
    "2160p":  "bestvideo[height<=2160][vcodec^=avc]+bestaudio/bestvideo[height<=2160]+bestaudio/best",
    "1080p":  "bestvideo[height<=1080][vcodec^=avc]+bestaudio/bestvideo[height<=1080]+bestaudio/best",
    "720p":   "bestvideo[height<=720][vcodec^=avc]+bestaudio/bestvideo[height<=720]+bestaudio/best",
    "480p":   "bestvideo[height<=480][vcodec^=avc]+bestaudio/bestvideo[height<=480]+bestaudio/best",
    "360p":   "bestvideo[height<=360][vcodec^=avc]+bestaudio/bestvideo[height<=360]+bestaudio/best",
    "audio":  "bestaudio/best",
}
_DEFAULT_QUALITY = "1080p"

# 编码兼容性排序：AVC 最通用，其次 HEVC，最后 AV1
_CODEC_RANK = (("avc", 0), (("hev", "hvc"), 1), ("av01", 2))


def quality_to_format(quality: str) -> str:
    """把清晰度预设翻译成 yt-dlp 的 format 表达式（静态兜底用）"""
    return _QUALITY_MAP.get(quality, _QUALITY_MAP[_DEFAULT_QUALITY])


def _size(fmt: Dict[str, Any]) -> float:
    return float(fmt.get("filesize") or fmt.get("filesize_approx") or 0)


def _codec_rank(fmt: Dict[str, Any]) -> int:
    vcodec = (fmt.get("vcodec") or "").lower()
    for prefix, rank in _CODEC_RANK:
        if isinstance(prefix, tuple):
            if vcodec.startswith(prefix):
                return rank
        elif vcodec.startswith(prefix):
            return rank
    return 3


def format_candidates(quality: str, *, format_cfg: str = "",
                      meta: Dict[str, Any] | None = None) -> List[str]:
    """返回 format 候选列表，每个元素是可直接交给 yt-dlp 的表达式。

    :param quality:    画质预设（best / 1080p / 720p / audio ...）
    :param format_cfg: 用户自定义的 format 字符串（非空时优先，按其 `/` 分段）
    :param meta:       视频元信息；缺失时退回静态降级链
    """
    fmt_cfg = (format_cfg or "").strip()
    if fmt_cfg:
        parts = [p.strip() for p in fmt_cfg.split("/") if p.strip()]
        return parts or [fmt_cfg]
    return candidates_from_meta(quality, meta or {})


def candidates_from_meta(quality: str, meta: Dict[str, Any]) -> List[str]:
    """从真实格式列表里挑候选：画质从高到低，同画质按编码兼容性排序。"""
    formats = meta.get("formats") or []
    if not formats:
        # 没有元信息（如已在下载中途调用）：退回静态映射
        return [p.strip() for p in quality_to_format(quality).split("/") if p.strip()]

    height_cap = None
    if quality not in ("best", "audio"):
        digits = "".join(ch for ch in quality if ch.isdigit())
        height_cap = int(digits) if digits else None

    videos = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("url")]
    audios = [f for f in formats if f.get("acodec") not in (None, "none")
              and f.get("vcodec") in (None, "none") and f.get("url")]

    if quality == "audio":
        audios.sort(key=_size, reverse=True)
        return [a["format_id"] for a in audios] or ["bestaudio/best"]

    if height_cap:
        videos = [f for f in videos if (f.get("height") or 0) <= height_cap]

    # 先按画质从高到低，同画质内按编码兼容性：
    # 这样 1080p AVC 失败时会先试 1080p HEVC/AV1（换个 CDN 但保持画质），
    # 而不是直接掉到 720p。
    videos.sort(key=lambda f: (-(f.get("height") or 0), _codec_rank(f), -_size(f)))

    best_audio = None
    if audios:
        audios.sort(key=_size, reverse=True)
        best_audio = audios[0]["format_id"]

    candidates: List[str] = []
    seen = set()
    for v in videos:
        fid = v.get("format_id")
        if vid := fid:
            if vid in seen:
                continue
            seen.add(vid)
        candidates.append(f"{fid}+{best_audio or 'bestaudio'}")
        if len(candidates) >= MAX_FORMAT_CANDIDATES:
            break

    candidates.append("bestvideo+bestaudio/best")
    return candidates
