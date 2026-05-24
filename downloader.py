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
            return video_path, meta

        logger.info("下载中: %s", title)

        cmd: List[str] = [
            self._get_ytdlp(),
            url,
            "-P", str(folder),
            "-o", f"{title}.%(ext)s",
            "--format", self.dl_config.get("format", "bestvideo[height<=1080]+bestaudio/best"),
            "--merge-output-format", "mp4",
            "--no-playlist",
            "--print", "after_move:filepath",
        ]

        cookies: str = self.dl_config.get("cookies_file", "")
        if cookies and os.path.exists(cookies):
            cmd += ["--cookies", cookies]

        timeout: int = int(self.dl_config.get("timeout", 7200))
        result: subprocess.CompletedProcess = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"下载失败: {result.stderr.strip() or result.stdout.strip()}")

        video_path = self._find_video(folder)
        if not video_path:
            raise FileNotFoundError(f"下载后未找到视频文件于 {folder}")

        set_state(folder, "downloaded")
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
            capture_output=True, text=True
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

        cmd: List[str] = [
            self._get_ytdlp(), url,
            "-o", str(folder / "%(playlist_index)s-%(title)s.%(ext)s"),
            "--format", self.dl_config.get("format", "bestvideo[height<=1080]+bestaudio/best"),
            "--merge-output-format", "mp4",
        ]

        cookies: str = self.dl_config.get("cookies_file", "")
        if cookies and os.path.exists(cookies):
            cmd += ["--cookies", cookies]

        subprocess.run(cmd, check=True, cwd=str(folder))

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
