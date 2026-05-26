"""
视频下载模块 — yt-dlp 封装 + 本地视频支持
支持单视频、播放列表、本地视频文件。自动提取元信息。
"""
import json
import logging
import os
import shutil
import subprocess
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
        opts: Dict[str, Any] = {
            "outtmpl": str(folder / template),
            "format": self.dl_config.get("format", "bestvideo[height<=1080]+bestaudio/best"),
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [self._progress_hook],
        }
        if no_playlist:
            opts["noplaylist"] = True
        cookies: str = self.dl_config.get("cookies_file", "")
        if cookies and os.path.exists(cookies):
            opts["cookiefile"] = cookies
        return opts

    def download(self, url: str, output_subdir: Optional[str] = None) -> Tuple[Path, Dict[str, Any]]:
        """下载视频或导入本地文件，返回 (video_path, meta)"""
        if not self._is_url(url):
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

        import yt_dlp
        opts = self._ytdlp_opts(folder, f"{title}.%(ext)s")
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

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
            logger.info("使用 yt-dlp 原生播放列表支持...")
            return self._download_playlist_direct(url, playlist_title)

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
        try:
            result: subprocess.CompletedProcess = subprocess.run(
                [self._get_ytdlp(), "--dump-json", "--no-playlist", url],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                return json.loads(result.stdout.strip())
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
            logger.warning("获取元信息失败: %s", e)
        except Exception as e:
            logger.warning("获取元信息失败 (%s): %s", type(e).__name__, e)
        return {"title": url.split("/")[-1]}

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
            opts = {"quiet": True, "no_warnings": True, "skip_download": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:
            logger.debug("无法查询字幕信息")
            return None

        manual_subs = info.get("subtitles", {}) or {}
        auto_subs = info.get("automatic_captions", {}) or {}

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

        return None

    @staticmethod
    def _download_single_subtitle(url: str, lang: str, folder: Path,
                                   stem: str, source: str,
                                   target: Path) -> Optional[Path]:
        """下载单个语言的字幕轨道，保存为标准文件名"""
        import json as _json
        import yt_dlp

        is_manual = (source == "manual")

        dl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "subtitleslangs": [lang],
            "subtitlesformat": "srt",
            "outtmpl": str(folder / f"{stem}.%(ext)s"),
        }
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
            logger.debug("字幕下载失败 (%s/%s): %s", source, lang, e)
            return None
