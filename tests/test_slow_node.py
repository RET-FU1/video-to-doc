"""慢节点检测 + 换节点重试 测试（不联网）

背景：B站 每次取元信息都会随机分配 CDN 节点，实测同一视频不同节点速度
差 6 倍（429KB/s ~ 2722KB/s）。抽到慢节点时单次下载可能耗时十几分钟。
这里测的是"持续低速 → 主动放弃 → 重新取元信息换节点"这套机制。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from downloader import Downloader, _SlowDownload


def _dl(tmp: Path, **cfg) -> Downloader:
    return Downloader({"downloader": cfg}, tmp)


class _FeedYDL:
    """模拟 yt-dlp：按给定字节序列推进 progress_hook"""

    def __init__(self, owner: Downloader, step_bytes: int, steps: int, sleep: float = 0.0):
        self.owner = owner
        self.step_bytes = step_bytes
        self.steps = steps
        self.sleep = sleep
        self.done = 0

    def download(self, urls):
        for _ in range(self.steps):
            self.done += self.step_bytes
            self.owner._downloaded_bytes = self.done
            # 复刻 yt-dlp 会调用 progress_hook
            self.owner._progress_hook({"status": "downloading",
                                       "downloaded_bytes": self.done,
                                       "total_bytes": self.step_bytes * self.steps})
            if self.sleep:
                time.sleep(self.sleep)


class TestSlowNodeDetection:
    def test_slow_node_raises(self, tmp_path):
        """持续低速（远低于阈值）应当抛 _SlowDownload"""
        d = _dl(tmp_path, slow_node_min_speed_kb=300, slow_node_min_bytes_mb=1,
                slow_node_min_samples=3)
        # 每步 64KB、间隔 0.05s → 约 1.2MB/s ... 用更慢的步长
        ydl = _FeedYDL(d, step_bytes=4 * 1024, steps=300, sleep=0.05)
        try:
            d._download_with_timeout(ydl, "http://x", 60, time.time())
        except _SlowDownload as e:
            assert "KB/s" in str(e)
            return
        raise AssertionError("持续低速应当抛 _SlowDownload")

    def test_fast_node_not_interrupted(self, tmp_path):
        """速度正常时不能误报"""
        d = _dl(tmp_path, slow_node_min_speed_kb=300, slow_node_min_bytes_mb=1,
                slow_node_min_samples=3)
        ydl = _FeedYDL(d, step_bytes=5 * 1024 * 1024, steps=6, sleep=0.15)
        d._download_with_timeout(ydl, "http://x", 60, time.time())

    def test_small_file_not_interrupted(self, tmp_path):
        """小文件（未达 min_bytes）不参与慢节点判定，避免误杀"""
        d = _dl(tmp_path, slow_node_min_speed_kb=300, slow_node_min_bytes_mb=8,
                slow_node_min_samples=2)
        ydl = _FeedYDL(d, step_bytes=4 * 1024, steps=80, sleep=0.05)   # 总共 320KB
        d._download_with_timeout(ydl, "http://x", 60, time.time())

    def test_disabled_when_zero(self, tmp_path):
        """slow_node_min_speed_kb=0 表示关闭检测"""
        d = _dl(tmp_path, slow_node_min_speed_kb=0, slow_node_min_bytes_mb=1,
                slow_node_min_samples=2)
        ydl = _FeedYDL(d, step_bytes=4 * 1024, steps=300, sleep=0.05)
        d._download_with_timeout(ydl, "http://x", 60, time.time())


class TestSpeedSampling:
    def test_samples_reset_per_attempt(self, tmp_path):
        """每次尝试开始都要清空采样，否则 delta 会算出负数/错误速度"""
        d = _dl(tmp_path)
        d._downloaded_bytes = 99 * 1024 * 1024      # 模拟上一轮残留
        d._speed_samples = [1.0, 2.0, 3.0, 4.0, 5.0]
        d._last_speed_bytes = 99 * 1024 * 1024

        ydl = _FeedYDL(d, step_bytes=1024, steps=2, sleep=0.05)
        d._download_with_timeout(ydl, "http://x", 60, time.time())
        assert all(s >= 0 for s in d._speed_samples), "不应出现负速度采样"

    def test_progress_hook_updates_bytes(self, tmp_path):
        d = _dl(tmp_path)
        d._progress_hook({"status": "downloading", "downloaded_bytes": 12345,
                          "total_bytes": 100000})
        assert d._downloaded_bytes == 12345

    def test_speed_sample_window_capped(self, tmp_path):
        d = _dl(tmp_path, slow_node_min_speed_kb=1, slow_node_min_bytes_mb=1000)
        # 只测采样窗口不超过 5，不触发判定（min_bytes 很高）
        for i in range(10):
            d._downloaded_bytes += 1024 * 1024
            d._last_speed_at = time.time() - 1.1
            d._check_slow_node()
        assert len(d._speed_samples) <= 5
