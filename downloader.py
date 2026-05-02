"""
视频下载模块 — yt-dlp 封装
支持单视频、播放列表。自动提取元信息。
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


class Downloader:
    def __init__(self, config, output_root):
        self.config = config
        self.output_root = Path(output_root)
        self.dl_config = config.get("downloader", {})
        self._ffmpeg = None
        self._ytdlp = None

    def _find_ffmpeg(self):
        if self._ffmpeg:
            return self._ffmpeg
        ff = shutil.which("ffmpeg")
        if ff:
            self._ffmpeg = ff
            return ff
        raise FileNotFoundError("未找到 ffmpeg，请先安装 ffmpeg")

    def _find_ytdlp(self):
        """跨平台查找 yt-dlp"""
        if self._ytdlp:
            return self._ytdlp
        # 先在项目 venv 中查找
        if sys.platform == "win32":
            venv_ytdlp = Path(__file__).parent / "venv" / "Scripts" / "yt-dlp.exe"
        else:
            venv_ytdlp = Path(__file__).parent / "venv" / "bin" / "yt-dlp"
        if venv_ytdlp.exists():
            self._ytdlp = str(venv_ytdlp)
            return self._ytdlp
        # 再在 PATH 中查找
        ff = shutil.which("yt-dlp")
        if ff:
            self._ytdlp = ff
            return ff
        raise FileNotFoundError("未找到 yt-dlp，请先运行 python setup.py")

    def _sanitize(self, name):
        return re.sub(r'[<>:"/\\|?*]', "-", name)

    def download(self, url, output_subdir=None):
        """下载视频，返回 (video_path, meta)"""
        meta = self._fetch_meta(url)
        title = self._sanitize(meta.get("title", "untitled"))
        folder = self.output_root / (output_subdir or title)
        folder.mkdir(parents=True, exist_ok=True)

        state_file = folder / ".pipeline_state"
        video_path = self._find_video(folder)
        if video_path and self._is_done(state_file):
            print(f"  已下载: {title}")
            return video_path, meta

        print(f"  下载中: {title}")

        cmd = [
            self._find_ytdlp(),
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

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            # yt-dlp sometimes prints info to stderr even on success
            if "ERROR" in result.stderr or "Error" in result.stderr:
                raise RuntimeError(f"下载失败: {result.stderr}")

        # 查找下载的视频文件
        video_path = self._find_video(folder)
        if not video_path:
            raise FileNotFoundError(f"下载后未找到视频文件于 {folder}")

        self._set_state(state_file, "downloaded")
        return video_path, meta

    def download_playlist(self, url):
        """下载播放列表所有视频，返回 [(video_path, meta), ...]"""
        print("  获取播放列表信息...")
        meta = self._fetch_meta(url)
        playlist_title = self._sanitize(meta.get("title", meta.get("playlist", "playlist")))

        # 获取播放列表条目
        result = subprocess.run(
            [self._find_ytdlp(), "--flat-playlist", "--dump-json", url],
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
            # 回退: 尝试用 yt-dlp 直接下载播放列表
            print("  使用 yt-dlp 原生播放列表支持...")
            return self._download_playlist_direct(url, playlist_title)

        print(f"  共 {len(entries)} 个视频")
        results = []
        for i, entry in enumerate(entries):
            video_url = entry.get("url") or entry.get("webpage_url") or f"https://youtube.com/watch?v={entry['id']}"
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
            self._find_ytdlp(), url,
            "-o", str(folder / "%(playlist_index)s-%(title)s.%(ext)s"),
            "--format", self.dl_config.get("format", "bestvideo[height<=1080]+bestaudio/best"),
            "--merge-output-format", "mp4",
        ]

        cookies = self.dl_config.get("cookies_file", "")
        if cookies and os.path.exists(cookies):
            cmd += ["--cookies", cookies]

        subprocess.run(cmd, check=True, cwd=str(folder))

        # 收集结果
        results = []
        for f in sorted(folder.glob("*.mp4")):
            results.append((f, {"title": f.stem}))
        return results

    def _fetch_meta(self, url):
        """获取视频元信息"""
        try:
            result = subprocess.run(
                [self._find_ytdlp(), "--dump-json", "--no-playlist", url],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                return json.loads(result.stdout.strip())
        except Exception:
            pass
        return {"title": url.split("/")[-1]}

    def _find_video(self, folder):
        """在目录中递归寻找视频文件"""
        for ext in (".mp4", ".mkv", ".webm", ".flv", ".avi", ".mov"):
            videos = sorted(folder.glob(f"**/*{ext}"))
            if videos:
                return videos[0]
        return None

    @staticmethod
    def _set_state(state_file, state):
        with open(state_file, "w") as f:
            f.write(state)

    @staticmethod
    def _is_done(state_file):
        if not state_file.exists():
            return False
        return state_file.read_text().strip() in ("downloaded", "transcribed", "summarized", "done")
