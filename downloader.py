"""
视频下载模块 — yt-dlp 封装 + 本地视频支持
支持单视频、播放列表、本地视频文件。自动提取元信息。
"""
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils import sanitize_filename, find_venv_executable, get_state, set_state, is_done, VIDEO_EXTS

logger = logging.getLogger(__name__)


class Downloader:
    def __init__(self, config: Dict[str, Any], output_root: Path) -> None:
        self.config: Dict[str, Any] = config
        self.output_root: Path = Path(output_root)
        self.dl_config: Dict[str, Any] = config.get("downloader", {})
        self._ytdlp: Optional[str] = None

    def _get_ytdlp(self) -> str:
        if not self._ytdlp:
            self._ytdlp = find_venv_executable("yt-dlp")
        return self._ytdlp

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
        """将清晰度预设映射为 yt-dlp format 字符串"""
        mapping = {
            "best":   "bestvideo+bestaudio/best",
            "2160p":  "bestvideo[height<=2160]+bestaudio/best",
            "1080p":  "bestvideo[height<=1080]+bestaudio/best",
            "720p":   "bestvideo[height<=720]+bestaudio/best",
            "480p":   "bestvideo[height<=480]+bestaudio/best",
            "360p":   "bestvideo[height<=360]+bestaudio/best",
            "audio":  "bestaudio/best",
        }
        return mapping.get(quality, mapping["1080p"])

    @staticmethod
    def _progress_hook(d: Dict[str, Any]) -> None:
        """yt-dlp 下载进度回调"""
        status = d.get("status", "")
        if status == "downloading":
            pct = d.get("_percent_str", "?").strip()
            speed = d.get("_speed_str", "?").strip()
            eta = d.get("_eta_str", "?").strip()
            logger.debug("下载: %s | %s | 剩余 %s", pct, speed, eta)
        elif status == "finished":
            logger.debug("下载完成，正在合并...")

    def _ytdlp_opts(self, folder: Path, template: str, *,
                     no_playlist: bool = True) -> Dict[str, Any]:
        """构建 yt-dlp Python API 选项"""
        retries: int = int(self.dl_config.get("retries", 10))
        fragment_retries: int = int(self.dl_config.get("fragment_retries", 30))
        socket_timeout: int = int(self.dl_config.get("socket_timeout", 30))
        # 清晰度：优先使用自定义 format，否则从 quality 预设映射
        fmt = self.dl_config.get("format", "")
        if not fmt:
            fmt = self._quality_to_format(self.dl_config.get("quality", "1080p"))
        opts: Dict[str, Any] = {
            "outtmpl": str(folder / template),
            "format": fmt,
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [self._progress_hook],
            "retries": retries,
            "fragment_retries": fragment_retries,
            "concurrent_fragment_downloads": 8,  # 并行下载分片，加速
            "socket_timeout": socket_timeout,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
            },
        }
        if no_playlist:
            opts["noplaylist"] = True
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
        title: str = sanitize_filename(meta.get("title", "untitled"))
        folder: Path = self.output_root / output_subdir / title if output_subdir else self.output_root / title
        folder.mkdir(parents=True, exist_ok=True)

        video_path: Optional[Path] = self._find_video(folder)
        if video_path and is_done(folder):
            logger.info("已下载: %s", title)
            self._try_download_subtitles(url, folder, title)
            return video_path, meta

        logger.info("下载中: %s", title)

        max_attempts: int = int(self.dl_config.get("max_download_attempts", 3))
        last_error = None
        import yt_dlp
        for attempt in range(1, max_attempts + 1):
            try:
                opts = self._ytdlp_opts(folder, f"{title}.%(ext)s")
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                break
            except Exception as e:
                # 不吞掉用户中断
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                last_error = e
                if attempt < max_attempts:
                    wait = attempt * 5
                    logger.warning("下载失败 (%d/%d): %s，%d 秒后重试...", attempt, max_attempts, e, wait)
                    time.sleep(wait)
                else:
                    raise

        video_path = self._find_video(folder)
        if not video_path:
            raise FileNotFoundError(f"下载后未找到视频文件于 {folder}")

        set_state(folder, "downloaded")
        self._try_download_subtitles(url, folder, title)
        return video_path, meta

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

        if dest.exists() and is_done(folder):
            logger.info("已导入: %s", title)
            return dest, {"title": title, "uploader": "本地文件"}

        logger.info("导入本地文件: %s", local.name)
        shutil.copy2(str(local), str(dest))
        set_state(folder, "downloaded")
        return dest, {"title": title, "uploader": "本地文件"}

    def download_playlist(self, url: str) -> List[Tuple[Path, Dict[str, Any]]]:
        """下载播放列表所有视频，返回 [(video_path, meta), ...]"""
        logger.info("获取播放列表信息...")
        meta: Dict[str, Any] = self._fetch_meta(url)
        playlist_title: str = sanitize_filename(meta.get("title", meta.get("playlist", "playlist")))

        result: subprocess.CompletedProcess = subprocess.run(
            [self._get_ytdlp(), "--flat-playlist", "--dump-json", url],
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
        opts = self._ytdlp_opts(folder, "%(playlist_index)s-%(title)s.%(ext)s",
                                no_playlist=False)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        results: List[Tuple[Path, Dict[str, Any]]] = []
        for f in sorted(folder.glob("*.mp4")):
            results.append((f, {"title": f.stem}))
        return results

    def _fetch_meta(self, url: str) -> Dict[str, Any]:
        """获取视频元信息"""
        ytdlp = self._get_ytdlp()
        base_args = [
            ytdlp, "--dump-json", "--no-playlist",
            "--add-header", "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "--add-header", "Accept:text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "--add-header", "Accept-Language:zh-CN,zh;q=0.9,en;q=0.5",
        ]
        cookies: str = self.dl_config.get("cookies_file", "")
        if cookies and Path(cookies).exists():
            base_args.extend(["--cookies", cookies])
        browser: str = self.dl_config.get("cookies_from_browser", "").strip()
        if browser:
            base_args.extend(["--cookies-from-browser", browser])
        for attempt in range(1, 4):
            try:
                result: subprocess.CompletedProcess = subprocess.run(
                    base_args + [url],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0 and result.stdout.strip():
                    info = json.loads(result.stdout.strip())
                    # 提取章节信息（YouTube/B站等平台的视频章节标记）
                    chapters = info.get("chapters") or []
                    if chapters:
                        info["_chapters"] = [
                            {"start": c["start_time"], "end": c["end_time"], "title": c.get("title", "")}
                            for c in chapters
                        ]
                    return info
                # 输出 stderr 便于排查
                stderr_tail = result.stderr.strip().split("\n")[-1] if result.stderr.strip() else ""
                if stderr_tail:
                    logger.warning("获取元信息失败 (尝试 %d/3): %s", attempt, stderr_tail[:200])
            except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
                logger.warning("获取元信息失败 (尝试 %d/3): %s", attempt, e)
            except Exception as e:
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                logger.warning("获取元信息失败 (%s, 尝试 %d/3): %s", type(e).__name__, attempt, e)
            if attempt < 3:
                time.sleep(3)
        # Fallback: extract video ID from URL, stripping query params
        from urllib.parse import urlparse
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        fallback_id = path_parts[-1] if path_parts else url
        return {"title": fallback_id}

    def _find_video(self, folder: Path) -> Optional[Path]:
        """在目录中递归寻找视频文件"""
        for ext in VIDEO_EXTS:
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
            opts = {
                "quiet": True, "no_warnings": False, "skip_download": True,
                "http_headers": {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
                },
            }
            cookies: str = self.dl_config.get("cookies_file", "")
            if cookies and Path(cookies).exists():
                opts["cookiefile"] = cookies
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
                        url, lang, folder, stem, source, target, cookies
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

    @staticmethod
    def _download_single_subtitle(url: str, lang: str, folder: Path,
                                   stem: str, source: str,
                                   target: Path, cookies: str = "") -> Optional[Path]:
        """下载单个语言的字幕轨道，保存为标准文件名"""
        import json as _json
        import yt_dlp

        is_manual = (source == "manual")

        dl_opts = {
            "quiet": True,
            "no_warnings": False,
            "skip_download": True,
            "subtitleslangs": [lang],
            "subtitlesformat": "srt",
            "outtmpl": str(folder / f"{stem}.%(ext)s"),
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
            },
        }
        if cookies and Path(cookies).exists():
            dl_opts["cookiefile"] = cookies
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
