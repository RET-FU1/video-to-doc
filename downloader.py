"""
视频下载模块 — yt-dlp 封装 + 本地视频支持
支持单视频、播放列表、本地视频文件。自动提取元信息。
"""
import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils import (sanitize_filename, set_state, is_done, python_module_cmd,
                   VIDEO_EXTS, AUDIO_EXTS, MEDIA_EXTS, CancelToken, CancelledError,
                   fmt_size)

logger = logging.getLogger(__name__)

# 慢节点速度采样间隔（秒）。1 秒一次对真实下载足够灵敏；
# 测试里会临时调小，这样验证判定逻辑不必真的等好几秒。
_SPEED_SAMPLE_INTERVAL = 1.0
# 看门狗轮询间隔（秒）：越小则"停止"响应越快，但空转开销也越大。
_WATCHDOG_POLL = 0.25
# 叫停工作线程后最多等多久（秒）。若线程卡死在 socket 读上、根本没有进度
# 回调可触发，就不再无谓死等，直接弃用本次尝试、换下一个候选。
_ABORT_JOIN_TIMEOUT = 3.0


class _YdlLogger:
    """yt-dlp 日志适配器：把 yt-dlp 的输出收进本项目 logger

    不接的话 yt-dlp 会自己往 stderr 打字；接上以后统一走 logging，
    且都落在 debug 级别——真正的失败由 _fetch_meta 逐次 warning 汇报，
    避免同一条错误被打印两遍。
    """

    def debug(self, msg: str) -> None:
        logger.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        logger.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        logger.debug("yt-dlp 警告: %s", msg)

    def error(self, msg: str) -> None:
        logger.debug("yt-dlp 错误: %s", msg)


class _DownloadTimeout(RuntimeError):
    """整次下载超过 downloader.timeout（不是单次请求超时，那个走 socket_timeout）"""


class _SlowDownload(RuntimeError):
    """下载速度持续低于阈值，判定为抽到了坏 CDN 节点，主动放弃换节点"""


class _Aborted(Exception):
    """主线程要求中止本次 yt-dlp 下载（由 progress_hook 抛出，让 yt-dlp 自己退出）"""


class Downloader:
    def __init__(self, config: Dict[str, Any], output_root: Path,
                 cancel_token: Optional[CancelToken] = None) -> None:
        self.config: Dict[str, Any] = config
        self.output_root: Path = Path(output_root)
        self.dl_config: Dict[str, Any] = config.get("downloader", {})
        self._last_progress_log: float = 0.0
        self._cancel: CancelToken = cancel_token or CancelToken()
        # 最近一次取到的视频元信息（含 formats），用于挑选可用的下载候选
        self._current_meta: Dict[str, Any] = {}
        # 下载进度（供慢节点检测采样）
        self._downloaded_bytes: int = 0
        self._speed_samples: List[float] = []
        self._last_speed_bytes: int = 0
        self._last_speed_at: float = time.time()
        # 中止开关：主线程置位后，progress_hook 抛 _Aborted 让 yt-dlp 收手
        self._abort: threading.Event = threading.Event()

    @staticmethod
    def _ytdlp_cmd() -> list[str]:
        """yt-dlp 命令行：`python -m yt_dlp`。

        venv\\Scripts\\yt-dlp.exe 内嵌的是创建 venv 时的 python 绝对路径，
        项目目录移动后启动器会静默失效（exit=1、stdout/stderr 全空），
        所以这里一律走当前解释器。
        """
        return python_module_cmd("yt_dlp")

    @staticmethod
    def _is_url(path: str) -> bool:
        return path.startswith(("http://", "https://"))

    @staticmethod
    def _extract_url(text: str) -> Optional[str]:
        """从文本中提取第一个 URL（抖音/B站 分享链接经常附带大量描述文字）"""
        import re
        urls = re.findall(r'https?://[^\s]+', text)
        return urls[0].rstrip(".,;:!?）)】]") if urls else None

    @staticmethod
    def _quality_to_format(quality: str) -> str:
        """将清晰度预设映射为 yt-dlp format 字符串

        历史背景：早期 B站 把 AV1 流分发到不稳定的第三方 CDN（mountaintoys.cn），
        所以这里曾强制选 AVC。但实测（2026-10）B站 把 **1080p AVC 流**分到了
        mountaintoys，而 AV1/HEVC 反而在正常 CDN 上 —— 强制 AVC 恰好选中了
        坏节点，下载 37 秒后 `ConnectionResetError 10054` 且重试无效。

        现在改为按「可用性优先」的降级链：
          1. 优先 AVC（兼容性最好，剪辑/播放最稳）
          2. 失败则退回任意编码（AV1/HEVC 通常在其他 CDN 上，能下）
        yt-dlp 会在主格式不可用时按 `/` 逐级降级，正好符合这个需求。
        """
        mapping = {
            # best：优先 AVC，失败退回任意可用流
            "best":   "bestvideo[vcodec^=avc]+bestaudio/bestvideo+bestaudio/best",
            "2160p":  "bestvideo[height<=2160][vcodec^=avc]+bestaudio/bestvideo[height<=2160]+bestaudio/best",
            "1080p":  "bestvideo[height<=1080][vcodec^=avc]+bestaudio/bestvideo[height<=1080]+bestaudio/best",
            "720p":   "bestvideo[height<=720][vcodec^=avc]+bestaudio/bestvideo[height<=720]+bestaudio/best",
            "480p":   "bestvideo[height<=480][vcodec^=avc]+bestaudio/bestvideo[height<=480]+bestaudio/best",
            "360p":   "bestvideo[height<=360][vcodec^=avc]+bestaudio/bestvideo[height<=360]+bestaudio/best",
            "audio":  "bestaudio/best",
        }
        return mapping.get(quality, mapping["1080p"])

    def _format_candidates(self, quality: str) -> List[str]:
        """返回 format 候选列表，每个元素是完整的 format 表达式。

        yt-dlp 的 `a/b/c` 只在"格式不可用"时降级，**不会**在"下载出错"时降级。
        所以下载失败时必须由我们自己换下一个候选重试 —— 这是本次
        `mountaintoys.cn` 事故的直接原因：AVC 1080p 在坏 CDN 上，
        但 yt-dlp 认为它"可用"，于是一直在这个节点上重试到失败。
        """
        fmt_cfg: str = (self.dl_config.get("format") or "").strip()
        if fmt_cfg:
            # 用户自定义 format：按其 `/` 分段也生成候选
            parts = [p.strip() for p in fmt_cfg.split("/") if p.strip()]
            if parts:
                return parts
            return [fmt_cfg]

        # 依赖元信息里的实际可用流，按"编码兼容性 + 画质"排序挑选
        return self._candidates_from_meta(quality)

    def _candidates_from_meta(self, quality: str) -> List[str]:
        """从当前视频的真实格式列表里，按可用性挑出若干候选 format_id 组合。

        这样能避开"某个 CDN 节点连不上"的坑：同一个视频的 1080p 往往同时有
        AVC / HEVC / AV1 三个流，分别在不同 CDN 上，换一个就能下。
        """
        info = self._current_meta or {}
        formats = info.get("formats") or []
        if not formats:
            # 没有元信息（如已在下载中途调用）：退回静态映射
            return [p.strip() for p in self._quality_to_format(quality).split("/") if p.strip()]

        height_cap = None
        if quality not in ("best", "audio"):
            digits = "".join(ch for ch in quality if ch.isdigit())
            height_cap = int(digits) if digits else None

        def _size(f: Dict[str, Any]) -> float:
            return float(f.get("filesize") or f.get("filesize_approx") or 0)

        videos = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("url")]
        audios = [f for f in formats if f.get("acodec") not in (None, "none")
                  and f.get("vcodec") in (None, "none") and f.get("url")]

        if quality == "audio":
            audios.sort(key=_size, reverse=True)
            return [a["format_id"] for a in audios] or ["bestaudio/best"]

        if height_cap:
            videos = [f for f in videos if (f.get("height") or 0) <= height_cap]

        # 编码优先级：AVC 兼容性最好，其次 HEVC，最后 AV1
        def _codec_rank(f: Dict[str, Any]) -> int:
            v = (f.get("vcodec") or "").lower()
            if v.startswith("avc"):
                return 0
            if v.startswith(("hev", "hvc")):
                return 1
            if v.startswith("av01"):
                return 2
            return 3

        # 排序：先按画质从高到低，同画质内按编码兼容性（AVC > HEVC > AV1）。
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
            if fid in seen:
                continue
            seen.add(fid)
            audio = best_audio or "bestaudio"
            candidates.append(f"{fid}+{audio}")
            if len(candidates) >= 5:      # 最多试 5 个，避免无限重试
                break

        candidates.append("bestvideo+bestaudio/best")
        return candidates

    def _progress_hook(self, d: Dict[str, Any]) -> None:
        """yt-dlp 下载进度回调（纯记录，**不**判断中止）

        每秒可能触发多次，这里限流成"每 10 秒一条 INFO"，让用户在 GUI 里
        能看到"到底下到哪了"，而不是盯着一个空目录以为卡死。

        中止判断**不在这里**：`_ytdlp_opts()` 会给每次下载包一层闭包，
        闭包里持有本次下载专属的 abort Event 并在置位时抛 _Aborted。
        如果把检查放在这个方法里（读共享的 self._abort），那么上一次下载
        因慢节点/取消置位后，下一次下载（尤其播放列表那条路径）的第一次
        进度回调就会立刻抛 _Aborted —— 表现为"播放列表直接失败"。
        """
        status = d.get("status", "")
        if status == "downloading":
            self._downloaded_bytes = d.get("downloaded_bytes") or 0
            now = time.time()
            if now - self._last_progress_log < 10:
                return
            self._last_progress_log = now
            pct = (d.get("_percent_str") or "?").strip()
            speed = (d.get("_speed_str") or "?").strip()
            eta = (d.get("_eta_str") or "?").strip()
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            # 注意：yt-dlp 的 _speed_str 已自带 "/s"（如 " 1.8MiB/s"），别再拼一次
            logger.info("下载中: %s |%s | 已下载 %s / %s | 剩余 %s",
                        pct, speed, fmt_size(done), fmt_size(total), eta)
        elif status == "finished":
            self._downloaded_bytes = d.get("downloaded_bytes") or self._downloaded_bytes
            logger.info("下载完成，正在合并/后处理...")

    def _ytdlp_opts(self, folder: Path, template: str, *,
                     no_playlist: bool = True,
                     format_override: Optional[str] = None,
                     retries_override: Optional[int] = None,
                     abort_event: Optional[threading.Event] = None) -> Dict[str, Any]:
        """构建 yt-dlp Python API 选项

        `abort_event` 用于打断本次下载：每次尝试传一个**独立的** Event，
        progress_hook 检查它并抛 _Aborted。绝不能共用一个实例 ——
        否则下一轮 clear() 会让上一轮残留的线程"复活"。
        """
        retries: int = int(self.dl_config.get("retries", 10))
        fragment_retries: int = int(self.dl_config.get("fragment_retries", 30))
        if retries_override is not None:
            retries = retries_override
            fragment_retries = min(fragment_retries, retries_override)
        socket_timeout: int = int(self.dl_config.get("socket_timeout", 30))
        # 清晰度：优先使用自定义 format，否则从 quality 预设映射
        fmt = format_override or self.dl_config.get("format", "")
        if not fmt:
            fmt = self._quality_to_format(self.dl_config.get("quality", "1080p"))
        abort = abort_event if abort_event is not None else threading.Event()

        def _hook(d: Dict[str, Any]) -> None:
            if abort.is_set():
                raise _Aborted()
            self._progress_hook(d)

        opts: Dict[str, Any] = {
            "outtmpl": str(folder / template),
            "format": fmt,
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [_hook],
            "retries": retries,
            "fragment_retries": fragment_retries,
            "socket_timeout": socket_timeout,
        }
        # 注意：socket_timeout 是"单次 HTTP 请求(连接+读)的超时"，不是整次下载的总时长。
        # `downloader.timeout` 语义是"整次下载超时"，yt-dlp 没有对应选项，所以**不能**
        # 用它覆盖 socket_timeout —— 那会让一个卡住的连接要等 2 小时才超时（实测踩过）。
        # socket_timeout 保持配置值（默认 60s），配合 retries 快速失败重试。
        total_timeout: int = int(self.dl_config.get("timeout", 0) or 0)
        if no_playlist:
            opts["noplaylist"] = True
        opts.update(self._auth_opts())
        return opts

    def _auth_opts(self) -> Dict[str, Any]:
        """cookies / proxy 公共选项（进程内 API 与 subprocess 兜底共用）"""
        opts: Dict[str, Any] = {}
        cookies: str = self.dl_config.get("cookies_file", "")
        if cookies and Path(cookies).exists():
            opts["cookiefile"] = cookies
        # 直接从浏览器读取 Cookie（无需手动导出文件）
        browser: str = self.dl_config.get("cookies_from_browser", "").strip()
        if browser:
            opts["cookiesfrombrowser"] = (browser,)
        proxy: str = self.dl_config.get("proxy", "")
        if proxy:
            opts["proxy"] = proxy
        return opts

    def _download_with_timeout(self, ydl: Any, url: str, deadline: float,
                               started: float = 0.0,
                               abort: Optional[threading.Event] = None) -> None:
        """在后台线程里跑 yt-dlp，主线程轮询总超时 / 取消 / **下载速度**。

        yt-dlp 是阻塞调用、没有"总时长"上限，卡在一个不响应的 CDN 上就会
        一直挂着（实测：39 分钟视频卡了 10 分钟没有任何进度）。这里用看门狗
        兜住，超时或用户点停止都能及时退出，而不是让用户干等 2 小时。

        另外还兼"慢节点检测"：B站 每次取元信息都会随机分配 CDN 节点，
        实测同一视频不同节点速度能差 6 倍（429KB/s ~ 2722KB/s），抽到慢节点
        时单次下载可能耗时十几分钟。持续低于阈值就抛 _SlowDownload，
        由调用方重新取元信息、换一个节点重来（见 download()）。

        `deadline` 是**整次下载的绝对截止时刻**（0 表示不限），不是本次尝试的
        时长——否则每换一个候选取都重新计时，总耗时会远超配置。

        退出时必须让工作线程真正停下来：daemon 线程不会因为主线程 raise 而结束，
        它会继续下载、写 .part（而调用方正在删这些），还会污染下一轮的采样。
        所以置 `abort` 让工作线程的 progress_hook 抛 _Aborted 把它顶出去。
        注意 abort 必须是**本次尝试专属**的 Event（由 download() 创建并传进来），
        不能共用 self._abort —— 否则下一次尝试 clear() 会把上一个残留线程
        一起"复活"。
        """
        abort = abort if abort is not None else threading.Event()
        self._abort = abort          # 供观测/测试
        result: Dict[str, Any] = {}
        # 每次尝试都重置采样状态：上一轮的字节数/速度不能带进本轮，
        # 否则 delta 会算出错误的（甚至负的）瞬时速度
        self._downloaded_bytes = 0
        self._speed_samples = []
        self._last_speed_bytes = 0
        self._last_speed_at = time.time()

        def _worker() -> None:
            try:
                ydl.download([url])
            except _Aborted:
                result["aborted"] = True
            except BaseException as e:      # 交给主线程判定
                result["error"] = e

        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()

        try:
            while worker.is_alive():
                worker.join(timeout=_WATCHDOG_POLL)
                self._cancel.check()
                if deadline and time.time() > deadline:
                    raise _DownloadTimeout(
                        f"下载超过配置的 downloader.timeout 仍未完成，已中止"
                        f"（当前值 {self.dl_config.get('timeout', 7200)} 秒，可调大）"
                    )
                self._check_slow_node()
        except BaseException:
            # 超时 / 慢节点 / 用户取消：先叫停工作线程，再抛出
            abort.set()
            worker.join(timeout=_ABORT_JOIN_TIMEOUT)
            if worker.is_alive():
                logger.warning(
                    "yt-dlp 线程仍卡在 socket 读上（无进度回调可打断），"
                    "已弃用本次尝试，后台自行结束；不影响换节点重试")
            raise
        if "error" in result:
            raise result["error"]

    def _check_slow_node(self) -> None:
        """采样下载速度，持续过慢则抛 _SlowDownload（换 CDN 节点重试）

        判据设计得很保守，避免把"正常但慢"的下载打断：
        - 已下载超过 slow_node_min_bytes_mb（默认 8MB）才判定 —— 小文件不值得换节点
        - 保留最近 slow_node_min_samples 个采样点（默认 5，约 5 秒），要求**全部**
          低于阈值才放弃
        """
        cfg = self.dl_config
        min_speed = float(cfg.get("slow_node_min_speed_kb", 300)) * 1024
        min_bytes = float(cfg.get("slow_node_min_bytes_mb", 8)) * 1024 * 1024
        min_samples = max(1, int(cfg.get("slow_node_min_samples", 5)))
        if min_speed <= 0:
            return                                  # 0 = 关闭该检测

        now = time.time()
        elapsed = now - self._last_speed_at
        if elapsed < _SPEED_SAMPLE_INTERVAL:
            return
        downloaded = getattr(self, "_downloaded_bytes", 0)
        delta = downloaded - self._last_speed_bytes
        self._last_speed_bytes = downloaded
        self._last_speed_at = now
        if downloaded <= 0:
            return                                  # 还没开始下，不判

        inst_speed = delta / elapsed
        self._speed_samples.append(inst_speed)
        if len(self._speed_samples) > min_samples:
            self._speed_samples.pop(0)

        if downloaded < min_bytes or len(self._speed_samples) < min_samples:
            return
        if all(s < min_speed for s in self._speed_samples):
            avg = sum(self._speed_samples) / len(self._speed_samples)
            raise _SlowDownload(
                f"当前 CDN 节点速度仅 {avg / 1024:.0f} KB/s（低于 "
                f"{min_speed / 1024:.0f} KB/s 阈值），已放弃该节点重新分配"
            )

    def download(self, url: str, output_subdir: Optional[str] = None) -> Tuple[Path, Dict[str, Any]]:
        """下载视频或导入本地文件，返回 (video_path, meta)"""
        # 自动从粘贴文本中提取 URL（抖音/B站分享常附带大量描述文字）
        if not self._is_url(url):
            extracted = self._extract_url(url)
            if extracted:
                logger.info("从粘贴文本中提取到链接: %s", extracted)
                url = extracted
            else:
                return self._import_local(url, output_subdir)

        meta: Dict[str, Any] = self._fetch_meta(url)
        self._current_meta = meta        # 供 _candidates_from_meta 挑可用流
        title: str = sanitize_filename(meta.get("title", "untitled"))
        folder: Path = self.output_root / output_subdir / title if output_subdir else self.output_root / title
        folder.mkdir(parents=True, exist_ok=True)

        video_path: Optional[Path] = self._find_video(folder)
        if video_path and is_done(folder):
            logger.info("已下载: %s", title)
            self._try_download_subtitles(url, folder, title)
            return video_path, meta

        logger.info("下载中: %s", title)

        quality: str = self.dl_config.get("quality", "best")
        # 整次下载的总超时（config: downloader.timeout，默认 2 小时）
        # 注意：是**整次**的预算，不是每个候选各给一份 —— 否则 6 个候选 × 3 轮
        # 最坏能拖到几十小时，和"总超时"的语义完全不符。
        total_timeout: float = float(self.dl_config.get("timeout", 0) or 0)
        deadline = time.time() + total_timeout if total_timeout > 0 else 0.0
        # 抽到慢节点时最多重取几次元信息（每次重取会重新分配 CDN 节点）
        max_rounds = max(1, int(self.dl_config.get("slow_node_retries", 3)))

        import yt_dlp
        last_error: Optional[Exception] = None
        for round_no in range(1, max_rounds + 1):
            self._check_cancel()
            # 重要：每轮都重新取元信息，B站 才会重新分配 CDN 节点。
            # 只在同一个 meta 里换 format_id 是没用的 —— URL 已经绑定旧节点。
            if round_no > 1 or not self._current_meta:
                meta = self._fetch_meta(url)
                self._current_meta = meta
            candidates: List[str] = self._format_candidates(quality)
            # 每个候选只给少量重试：卡在坏 CDN 上重试 30 次要 9 分钟（实测），
            # 而换下一个编码/CDN 通常 10 秒就能下完。这里优先"快速换路"。
            per_candidate_retries = 2 if len(candidates) > 1 else int(
                self.dl_config.get("retries", 10))

            for idx, fmt in enumerate(candidates, 1):
                self._check_cancel()
                started = time.time()
                try:
                    abort = threading.Event()
                    self._abort = abort       # 供观测/测试
                    opts = self._ytdlp_opts(folder, f"{title}.%(ext)s", format_override=fmt,
                                            retries_override=per_candidate_retries,
                                            abort_event=abort)
                    if len(candidates) > 1:
                        logger.info("尝试格式 %d/%d: %s", idx, len(candidates), fmt)
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        self._download_with_timeout(ydl, url, deadline, started, abort)
                    last_error = None
                    break
                except _DownloadTimeout:
                    raise
                except CancelledError:
                    raise
                except _SlowDownload as e:
                    # 慢节点：不是编码问题，换 format 也没用，直接进下一轮重取元信息
                    last_error = e
                    self._cleanup_part_files(folder)
                    logger.warning("%s", e)
                    break
                except Exception as e:
                    # 总预算用完就不再换候选了
                    if deadline and time.time() > deadline:
                        raise _DownloadTimeout(
                            f"下载超过 {total_timeout:.0f} 秒仍未完成，已中止"
                            f"（可调大 config.yaml → downloader.timeout）") from e
                    if isinstance(e, (KeyboardInterrupt, SystemExit)):
                        raise
                    last_error = e
                    self._cleanup_part_files(folder)
                    if idx < len(candidates):
                        # 换下一个编码/CDN 节点重试：这才是 B站 下载失败最常见的解法
                        logger.warning("格式 %s 下载失败（%s）；换下一个候选重试...",
                                       fmt, str(e).strip().splitlines()[-1][:120])
                        time.sleep(min(idx, 2))
                    else:
                        raise

            found = self._find_video(folder)
            # 校验音轨：B站 音视频是分开的两条流，只下到视频流（中断、格式串写错）
            # 时文件看起来"下载成功"，但转写阶段会因没有音轨而失败（用户只看到
            # 一串 ffmpeg 报错）。这里提前发现，删掉残次品重新下一轮。
            if found and not self._ensure_has_audio(found):
                logger.warning("下载到的文件没有音轨，丢弃后重试...")
                try:
                    found.unlink()
                except OSError:
                    pass
                last_error = RuntimeError("下载产物不含音轨")
                found = None
            if found:
                break
            if round_no < max_rounds and isinstance(last_error, _SlowDownload):
                logger.warning("第 %d/%d 轮下载太慢，重新获取下载地址换节点重试...",
                               round_no, max_rounds)
            elif round_no < max_rounds and last_error is not None:
                logger.warning("第 %d/%d 轮下载失败，重新获取下载地址重试...",
                               round_no, max_rounds)
            elif last_error is not None:
                break

        if not self._find_video(folder):
            if last_error:
                raise last_error
            raise FileNotFoundError(f"下载后未找到视频/音频文件于 {folder}")

        video_path = self._find_video(folder)
        set_state(folder, "downloaded")
        self._try_download_subtitles(url, folder, title)
        return video_path, meta

    @staticmethod
    def _ensure_has_audio(video_path: Path) -> bool:
        """检查下载产物有没有音轨（纯音频文件直接算通过）"""
        if video_path.suffix.lower() in AUDIO_EXTS:
            return True
        try:
            from transcriber import Transcriber
            return Transcriber._has_audio_stream(video_path)
        except Exception as e:      # noqa: BLE001 - 探测失败不应阻断下载
            logger.debug("音轨探测失败（跳过校验）: %s", e)
            return True

    def _check_cancel(self) -> None:
        self._cancel.check()

    @staticmethod
    def _cleanup_part_files(folder: Path) -> None:
        """清掉失败轮次的 .part / .ytdl 残片，避免下一轮续传到坏片段上"""
        for pattern in ("*.part", "*.ytdl", "*.temp"):
            for f in folder.glob(pattern):
                try:
                    f.unlink()
                except OSError as e:
                    logger.debug("清理残片失败（可忽略）: %s (%s)", f.name, e)

    def _import_local(self, path: str, output_subdir: Optional[str] = None) -> Tuple[Path, Dict[str, Any]]:
        """导入本地视频文件到输出目录，返回 (video_path, meta)"""
        local: Path = Path(path).resolve()
        if not local.exists():
            raise FileNotFoundError(f"本地文件不存在: {path}")
        if not local.is_file():
            raise ValueError(f"路径不是文件: {path}")

        title: str = sanitize_filename(local.stem)
        folder: Path = self.output_root / output_subdir / title if output_subdir else self.output_root / title
        folder.mkdir(parents=True, exist_ok=True)

        ext: str = local.suffix or ".mp4"
        dest: Path = folder / f"{title}{ext}"

        # 已经导入过就直接复用：既避免重复拷贝，也避免复制时源文件
        # 恰好被占用（如本文件就是上一次的输出）导致的 WinError 32。
        # 但**不能只看 dest 存在**：上次复制中断会留下半截文件，或用户替换了
        # 同名但内容不同的源文件 —— 那样后面会静默转写一份旧/残文件。
        # 这里用大小 + 修改时间判断是否确实同一份内容。
        if dest.exists():
            if dest.resolve() == local:
                logger.info("使用已有文件: %s", dest)
                return dest, {"title": title, "uploader": "本地文件"}
            try:
                same = (dest.stat().st_size == local.stat().st_size
                        and abs(dest.stat().st_mtime - local.stat().st_mtime) < 2)
            except OSError:
                same = False
            if same:
                logger.info("已导入: %s", title)
                return dest, {"title": title, "uploader": "本地文件"}
            logger.warning("目标文件与源文件不一致（可能上次中断或源已更新），重新导入: %s",
                           dest.name)

        logger.info("导入本地文件: %s", local.name)
        try:
            shutil.copy2(str(local), str(dest))
        except OSError as e:
            # 拷贝失败时退化为"就地使用"：视频就在用户指定的目录里，
            # 没必要因为一次复制失败让整个任务失败
            logger.warning("复制本地文件失败(%s)，改为直接使用原文件", e)
            return local, {"title": title, "uploader": "本地文件"}
        set_state(folder, "downloaded")
        return dest, {"title": title, "uploader": "本地文件"}

    def download_playlist(self, url: str) -> List[Tuple[Path, Dict[str, Any]]]:
        """下载播放列表所有视频，返回 [(video_path, meta), ...]"""
        logger.info("获取播放列表信息...")
        meta: Dict[str, Any] = self._fetch_meta(url)
        playlist_title: str = sanitize_filename(meta.get("title", meta.get("playlist", "playlist")))

        result: subprocess.CompletedProcess = subprocess.run(
            [*self._ytdlp_cmd(), "--flat-playlist", "--dump-json", url],
            capture_output=True, text=True, timeout=60
        )

        entries: List[Dict[str, Any]] = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                entry = json.loads(line)
                entries.append(entry)
            except json.JSONDecodeError:
                continue

        if not entries:
            # flat-playlist 无结果 → 可能是 B站分P / YouTube 系列等，尝试原生下载
            logger.info("未检测到独立播放列表条目，尝试 yt-dlp 原生多集下载...")
            try:
                return self._download_playlist_direct(url, playlist_title)
            except Exception as e:
                logger.warning("原生多集下载失败: %s，回退为单视频下载", e)
                video_path, video_meta = self.download(url, output_subdir=playlist_title)
                return [(video_path, video_meta)]

        logger.info("共 %d 个视频", len(entries))
        results: List[Tuple[Path, Dict[str, Any]]] = []
        for i, entry in enumerate(entries):
            video_url: Optional[str] = entry.get("url") or entry.get("webpage_url")
            if not video_url:
                logger.warning("[SKIP] 无法获取视频 URL: %s", entry.get('title', entry.get('id', 'unknown')))
                continue
            logger.info("[%d/%d]", i + 1, len(entries))
            try:
                video_path, video_meta = self.download(video_url, output_subdir=playlist_title)
                results.append((video_path, video_meta))
            except Exception as e:
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                logger.warning("[SKIP] 下载失败: %s", e)
                continue

        return results

    def _download_playlist_direct(self, url: str, playlist_title: str) -> List[Tuple[Path, Dict[str, Any]]]:
        """yt-dlp 原生播放列表下载"""
        folder: Path = self.output_root / playlist_title
        folder.mkdir(parents=True, exist_ok=True)

        import yt_dlp
        # 每次下载都用一个全新的 abort Event，并同步到 self._abort：
        # 否则上一次单视频下载被中止后（self._abort 已置位），这里没有独立的
        # Event 可重置，会一启动就被判定为"已中止"。
        abort = threading.Event()
        self._abort = abort
        opts = self._ytdlp_opts(folder, "%(playlist_index)s-%(title)s.%(ext)s",
                                no_playlist=False, abort_event=abort)
        with yt_dlp.YoutubeDL(opts) as ydl:
            self._download_with_timeout(ydl, url, 0.0, time.time(), abort)

        results: List[Tuple[Path, Dict[str, Any]]] = []
        # 不能只 glob *.mp4：quality=audio 时产物是 m4a/mp3/webm
        for ext in sorted(MEDIA_EXTS):
            for f in sorted(folder.glob(f"*{ext}")):
                results.append((f, {"title": f.stem}))
        results.sort(key=lambda item: item[0].name)
        return results

    # ------------------------------------------------------------------
    # 元信息
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_chapters(info: Dict[str, Any]) -> List[Dict[str, Any]]:
        """把 yt-dlp 的 chapters 规范化为 [{start, end, title}, ...]"""
        chapters = info.get("chapters") or []
        out: List[Dict[str, Any]] = []
        for c in chapters:
            start = c.get("start_time", c.get("start"))
            end = c.get("end_time", c.get("end", start))
            if start is None:
                continue
            out.append({"start": start, "end": end, "title": c.get("title", "")})
        return out

    def _meta_from_api(self, url: str) -> Dict[str, Any]:
        """进程内 yt-dlp API 取元信息（首选路径）

        venv 里的 .exe 启动器内嵌旧 python 路径，项目目录移动后会静默失效
        （exit=1、无任何输出），导致此处只能拿到 BV 号当标题。进程内 API
        不依赖启动器，也不受 PATH/解释器路径影响。
        """
        import yt_dlp

        opts: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "logger": _YdlLogger(),
        }
        opts.update(self._auth_opts())
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise ValueError("yt_dlp 返回空元信息")
        chapters = self._extract_chapters(info)
        if chapters:
            info["_chapters"] = chapters
        return info

    def _meta_from_subprocess(self, url: str) -> Dict[str, Any]:
        """subprocess 兜底路径（`python -m yt_dlp --dump-json`）"""
        base_args = [*self._ytdlp_cmd(), "--dump-json", "--no-playlist"]
        cookies: str = self.dl_config.get("cookies_file", "")
        if cookies and Path(cookies).exists():
            base_args.extend(["--cookies", cookies])
        browser: str = self.dl_config.get("cookies_from_browser", "").strip()
        if browser:
            base_args.extend(["--cookies-from-browser", browser])
        proxy: str = self.dl_config.get("proxy", "")
        if proxy:
            base_args.extend(["--proxy", proxy])

        result: subprocess.CompletedProcess = subprocess.run(
            base_args + [url],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=60,
        )
        if result.returncode != 0 or not (result.stdout or "").strip():
            stderr_tail = (result.stderr or "").strip().split("\n")[-1]
            raise RuntimeError(
                f"returncode={result.returncode} "
                f"stderr={stderr_tail[:200] or '(空)'}"
            )
        info = json.loads(result.stdout.strip())
        chapters = self._extract_chapters(info)
        if chapters:
            info["_chapters"] = chapters
        return info

    def _fetch_meta(self, url: str) -> Dict[str, Any]:
        """获取视频元信息：进程内 yt_dlp API 优先，subprocess 仅兜底"""
        attempts: List[Tuple[str, Any]] = [
            ("yt_dlp API", self._meta_from_api),
            ("python -m yt_dlp", self._meta_from_subprocess),
        ]
        total: int = len(attempts) * 3
        done: int = 0
        for label, fetch in attempts:
            for _ in range(3):
                done += 1
                try:
                    return fetch(url)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as e:
                    logger.warning("获取元信息失败（%s, 尝试 %d/%d）: %s: %s",
                                   label, done, total, type(e).__name__, e)
                    if done < total:
                        time.sleep(3)
        # Fallback: extract video ID from URL, stripping query params
        from urllib.parse import urlparse
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        fallback_id = path_parts[-1] if path_parts else url
        logger.error("全部元信息获取路径失败，标题退化为 ID: %s", fallback_id)
        return {"title": fallback_id}

    def _find_video(self, folder: Path) -> Optional[Path]:
        """在目录中递归寻找媒体文件（视频优先，其次音频）

        `quality: audio` 时 yt-dlp 只产出 m4a/mp3/webm，旧实现只 glob
        视频扩展名，于是永远判定"下载后未找到视频文件"。
        """
        for extensions in (VIDEO_EXTS, AUDIO_EXTS):
            for ext in sorted(extensions):
                videos = sorted(folder.glob(f"**/*{ext}"))
                if videos:
                    return videos[0]
        return None

    def _try_download_subtitles(self, url: str, folder: Path, stem: str) -> Optional[Path]:
        """尝试下载最佳匹配的字幕，保存为 {stem}_subtitle.srt。
        返回字幕文件路径，无可用于幕则返回 None。
        """
        sub_config = self.config.get("subtitles", {})
        if not sub_config.get("enabled", False):
            return None

        target = folder / f"{stem}_subtitle.srt"
        if target.exists():
            return target

        preferred_langs = sub_config.get("languages", ["zh", "en"])
        prefer_manual = sub_config.get("prefer_manual", True)

        import yt_dlp

        # 查询可用字幕
        try:
            opts: Dict[str, Any] = {
                "quiet": True, "no_warnings": False, "skip_download": True,
                "logger": _YdlLogger(),
            }
            # 与下载共用同一套 cookies/proxy：B站字幕往往需要登录才返回
            auth_opts = self._auth_opts()
            opts.update(auth_opts)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            logger.info("无法查询字幕信息: %s", e)
            return None

        manual_subs = info.get("subtitles", {}) or {}
        auto_subs = info.get("automatic_captions", {}) or {}

        # 过滤弹幕（B站 danmaku 不是真正的字幕）
        manual_subs = {k: v for k, v in manual_subs.items() if k != "danmaku"}
        auto_subs = {k: v for k, v in auto_subs.items() if k != "danmaku"}

        # 按优先级尝试：人工 → 自动
        sources: List[tuple] = []
        if prefer_manual:
            sources.append(("manual", manual_subs))
        sources.append(("auto_generated", auto_subs))

        for source, subs in sources:
            if not subs:
                continue
            for lang in preferred_langs:
                if lang in subs:
                    result = self._download_single_subtitle(
                        url, lang, folder, stem, source, target
                    )
                    if result:
                        return result

        # 汇总可用语言用于日志提示
        all_langs = set(manual_subs.keys()) | set(auto_subs.keys())
        if all_langs:
            logger.info("未找到匹配的字幕语言（可用: %s，期望: %s）",
                       ", ".join(sorted(all_langs)), ", ".join(preferred_langs))
        else:
            logger.info("该视频无可用于幕（可能需要登录平台或视频本身无字幕）")
        return None

    def _download_single_subtitle(self, url: str, lang: str, folder: Path,
                                   stem: str, source: str,
                                   target: Path) -> Optional[Path]:
        """下载单个语言的字幕轨道，保存为标准文件名"""
        import json as _json
        import yt_dlp

        is_manual = (source == "manual")

        dl_opts: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": False,
            "skip_download": True,
            "subtitleslangs": [lang],
            "subtitlesformat": "srt",
            "outtmpl": str(folder / f"{stem}.%(ext)s"),
            "logger": _YdlLogger(),
        }
        # 同 _auth_opts()：浏览器 cookies / cookie 文件 / proxy 都要带上，
        # 否则需要登录的字幕轨一律拿不到
        dl_opts.update(self._auth_opts())
        if is_manual:
            dl_opts["writesubtitles"] = True
        else:
            dl_opts["writeautomaticsub"] = True

        try:
            before = set(folder.glob("*"))
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                ydl.download([url])
            after = set(folder.glob("*"))

            new_files = [f for f in (after - before) if f.suffix in (".srt", ".vtt")]
            if not new_files:
                return None

            sub_path = new_files[0]
            if sub_path != target:
                # 避免 Windows 下跨磁盘 rename 报错
                if sub_path.suffix != target.suffix:
                    target = target.with_suffix(sub_path.suffix)
                sub_path.rename(target)

            # 保存字幕元信息
            info_path = folder / f"{stem}_subtitle_info.json"
            info_path.write_text(_json.dumps({
                "source": source,
                "language": lang,
            }, ensure_ascii=False), encoding="utf-8")

            logger.info("已下载字幕 (%s/%s)", source, lang)
            return target
        except Exception as e:
            logger.info("字幕下载失败 (%s/%s): %s", source, lang, e)
            return None
