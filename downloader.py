"""
视频下载模块 — yt-dlp 封装 + 本地视频支持
支持单视频、播放列表、本地视频文件。自动提取元信息。
"""
import json
import os
import shutil
import subprocess
from pathlib import Path
from utils import sanitize_filename, find_venv_executable, get_state, set_state, is_done


class Downloader:
    def __init__(self, config, output_root):
        self.config = config
        self.output_root = Path(output_root)
        self.dl_config = config.get("downloader", {})
        self._ytdlp = None

    def _get_ytdlp(self):
        if not self._ytdlp:
            self._ytdlp = find_venv_executable("yt-dlp")
        return self._ytdlp

    @staticmethod
    def _is_url(path):
        return path.startswith(("http://", "https://"))

    def download(self, url, output_subdir=None):
        """下载视频或导入本地文件，返回 (video_path, meta)"""
        # 本地文件路径
        if not self._is_url(url):
            return self._import_local(url, output_subdir)

        meta = self._fetch_meta(url)
        title = sanitize_filename(meta.get("title", "untitled"))
        folder = self.output_root / (output_subdir or title)
        folder.mkdir(parents=True, exist_ok=True)

        video_path = self._find_video(folder)
        if video_path and is_done(folder):
            print(f"  已下载: {title}")
            return video_path, meta

        print(f"  下载中: {title}")

        cmd = [
            self._get_ytdlp(),
            url,
            "-P", str(folder),
            "-o", "video.%(ext)s",
            "--format", self.dl_config.get("format", "bestvideo[height<=1080]+bestaudio/best"),
            "--merge-output-format", "mp4",
            "--no-playlist",
            "--print", "after_move:filepath",
        ]

        cookies = self.dl_config.get("cookies_file", "")
        if cookies and os.path.exists(cookies):
            cmd += ["--cookies", cookies]

        timeout = self.dl_config.get("timeout", 7200)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"下载失败: {result.stderr.strip() or result.stdout.strip()}")

        video_path = self._find_video(folder)
        if not video_path:
            raise FileNotFoundError(f"下载后未找到视频文件于 {folder}")

        set_state(folder, "downloaded")
        return video_path, meta

    def _import_local(self, path, output_subdir=None):
        """导入本地视频文件到输出目录，返回 (video_path, meta)"""
        local = Path(path).resolve()
        if not local.exists():
            raise FileNotFoundError(f"本地文件不存在: {path}")
        if not local.is_file():
            raise ValueError(f"路径不是文件: {path}")

        title = sanitize_filename(local.stem)
        folder = self.output_root / (output_subdir or title)
        folder.mkdir(parents=True, exist_ok=True)

        ext = local.suffix or ".mp4"
        dest = folder / f"video{ext}"

        # 已导入过
        if dest.exists() and is_done(folder):
            print(f"  已导入: {title}")
            return dest, {"title": title, "uploader": "本地文件"}

        print(f"  导入本地文件: {local.name}")
        shutil.copy2(str(local), str(dest))
        set_state(folder, "downloaded")
        return dest, {"title": title, "uploader": "本地文件"}

    def download_playlist(self, url):
        """下载播放列表所有视频，返回 [(video_path, meta), ...]"""
        print("  获取播放列表信息...")
        meta = self._fetch_meta(url)
        playlist_title = sanitize_filename(meta.get("title", meta.get("playlist", "playlist")))

        result = subprocess.run(
            [self._get_ytdlp(), "--flat-playlist", "--dump-json", url],
            capture_output=True, text=True
        )

        entries = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                entry = json.loads(line)
                entries.append(entry)
            except json.JSONDecodeError:
                continue

        if not entries:
            print("  使用 yt-dlp 原生播放列表支持...")
            return self._download_playlist_direct(url, playlist_title)

        print(f"  共 {len(entries)} 个视频")
        results = []
        for i, entry in enumerate(entries):
            video_url = entry.get("url") or entry.get("webpage_url")
            if not video_url:
                print(f"  [SKIP] 无法获取视频 URL: {entry.get('title', entry.get('id', 'unknown'))}")
                continue
            print(f"\n  [{i+1}/{len(entries)}]")
            try:
                video_path, video_meta = self.download(video_url, output_subdir=playlist_title)
                results.append((video_path, video_meta))
            except Exception as e:
                print(f"  [SKIP] 下载失败: {e}")
                continue

        return results

    def _download_playlist_direct(self, url, playlist_title):
        """yt-dlp 原生播放列表下载"""
        folder = self.output_root / playlist_title
        folder.mkdir(parents=True, exist_ok=True)

        cmd = [
            self._get_ytdlp(), url,
            "-o", str(folder / "%(playlist_index)s-%(title)s.%(ext)s"),
            "--format", self.dl_config.get("format", "bestvideo[height<=1080]+bestaudio/best"),
            "--merge-output-format", "mp4",
        ]

        cookies = self.dl_config.get("cookies_file", "")
        if cookies and os.path.exists(cookies):
            cmd += ["--cookies", cookies]

        subprocess.run(cmd, check=True, cwd=str(folder))

        results = []
        for f in sorted(folder.glob("*.mp4")):
            results.append((f, {"title": f.stem}))
        return results

    def _fetch_meta(self, url):
        """获取视频元信息"""
        try:
            result = subprocess.run(
                [self._get_ytdlp(), "--dump-json", "--no-playlist", url],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                return json.loads(result.stdout.strip())
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
            print(f"  [WARN] 获取元信息失败: {e}")
        except Exception as e:
            print(f"  [WARN] 获取元信息失败 ({type(e).__name__}): {e}")
        return {"title": url.split("/")[-1]}

    def _find_video(self, folder):
        """在目录中递归寻找视频文件"""
        for ext in (".mp4", ".mkv", ".webm", ".flv", ".avi", ".mov"):
            videos = sorted(folder.glob(f"**/*{ext}"))
            if videos:
                return videos[0]
        return None
