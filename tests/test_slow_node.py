"""慢节点检测 + 换节点重试 测试（不联网）

背景：B站 每次取元信息都会随机分配 CDN 节点，实测同一视频不同节点速度
差 6 倍（429KB/s ~ 2722KB/s）。抽到慢节点时单次下载可能耗时十几分钟。
这里测的是"持续低速 → 主动放弃 → 重新取元信息换节点"这套机制。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import downloader as _dl_mod
from downloader import Downloader, _SlowDownload


@pytest.fixture(autouse=True)
def _fast_sampling(monkeypatch):
    """把速度采样间隔从 1 秒调成 20 毫秒。

    生产环境 1 秒一次足够；测试里若真等 3 秒 × 好几个用例，整个套件会慢到
    一分钟。判定逻辑本身不受间隔影响（仍是"连续 N 个采样点都慢"）。
    """
    monkeypatch.setattr(_dl_mod, "_SPEED_SAMPLE_INTERVAL", 0.02)
    monkeypatch.setattr(_dl_mod, "_WATCHDOG_POLL", 0.02)
    yield


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
    """注意：采样间隔被 fixture 调成 0.02s，所以这里用"每 0.02s 写 N 字节"
    来构造目标速度：4KB/0.02s ≈ 200KB/s（慢），256KB/0.02s ≈ 12MB/s（快）。"""

    def test_slow_node_raises(self, tmp_path):
        """持续低速（远低于阈值）应当抛 _SlowDownload"""
        d = _dl(tmp_path, slow_node_min_speed_kb=300,
                slow_node_min_bytes_mb=0.05,        # 50KB 即可判定，别真等下几 MB
                slow_node_min_samples=3)
        ydl = _FeedYDL(d, step_bytes=4 * 1024, steps=200, sleep=0.02)
        try:
            d._download_with_timeout(ydl, "http://x", time.time() + 60, time.time())
        except _SlowDownload as e:
            assert "KB/s" in str(e)
            return
        raise AssertionError("持续低速应当抛 _SlowDownload")

    def test_fast_node_not_interrupted(self, tmp_path):
        """速度正常时不能误报"""
        d = _dl(tmp_path, slow_node_min_speed_kb=300, slow_node_min_bytes_mb=1,
                slow_node_min_samples=3)
        ydl = _FeedYDL(d, step_bytes=256 * 1024, steps=40, sleep=0.02)
        d._download_with_timeout(ydl, "http://x", time.time() + 60, time.time())

    def test_small_file_not_interrupted(self, tmp_path):
        """小文件（未达 min_bytes）不参与慢节点判定，避免误杀"""
        d = _dl(tmp_path, slow_node_min_speed_kb=300, slow_node_min_bytes_mb=8,
                slow_node_min_samples=2)
        ydl = _FeedYDL(d, step_bytes=4 * 1024, steps=80, sleep=0.02)   # 总共 320KB
        d._download_with_timeout(ydl, "http://x", time.time() + 60, time.time())

    def test_disabled_when_zero(self, tmp_path):
        """slow_node_min_speed_kb=0 表示关闭检测"""
        d = _dl(tmp_path, slow_node_min_speed_kb=0, slow_node_min_bytes_mb=0.05,
                slow_node_min_samples=2)
        ydl = _FeedYDL(d, step_bytes=4 * 1024, steps=200, sleep=0.02)
        d._download_with_timeout(ydl, "http://x", time.time() + 60, time.time())


class TestSpeedSampling:
    def test_samples_reset_per_attempt(self, tmp_path):
        """每次尝试开始都要清空采样，否则 delta 会算出负数/错误速度"""
        d = _dl(tmp_path)
        d._downloaded_bytes = 99 * 1024 * 1024      # 模拟上一轮残留
        d._speed_samples = [1.0, 2.0, 3.0, 4.0, 5.0]
        d._last_speed_bytes = 99 * 1024 * 1024

        ydl = _FeedYDL(d, step_bytes=1024, steps=2, sleep=0.05)
        d._download_with_timeout(ydl, "http://x", time.time() + 60, time.time())
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


class TestAbortStopsWorker:
    """复核者指出：看门狗退出时 daemon 线程仍在下载、继续写 .part，
    而调用方正在删那些文件，还会污染下一轮的慢节点采样。
    修复：置 _abort 让 progress_hook 抛 _Aborted，并等线程结束。"""

    def test_slow_node_aborts_worker(self, tmp_path):
        import threading
        d = _dl(tmp_path, slow_node_min_speed_kb=300, slow_node_min_bytes_mb=1,
                slow_node_min_samples=2)
        stopped = threading.Event()

        class SlowYDL:
            def download(self, urls):
                while not d._abort.is_set():
                    d._downloaded_bytes += 4096
                    d._progress_hook({"status": "downloading",
                                      "downloaded_bytes": d._downloaded_bytes,
                                      "total_bytes": 100 * 1024 * 1024})
                    time.sleep(0.02)
                stopped.set()

        try:
            d._download_with_timeout(SlowYDL(), "http://x", 0.0, time.time())
        except _SlowDownload:
            pass
        else:
            raise AssertionError("应当抛 _SlowDownload")

        assert d._abort.is_set(), "退出前必须置 abort 标志"
        assert stopped.wait(5), "工作线程没有停下来（会继续写 .part 污染下次尝试）"

    def test_timeout_aborts_worker(self, tmp_path):
        import threading
        d = _dl(tmp_path, slow_node_min_speed_kb=0)   # 关掉慢节点，只测超时
        stopped = threading.Event()

        class HangingYDL:
            def download(self, urls):
                while not d._abort.is_set():
                    d._progress_hook({"status": "downloading",
                                      "downloaded_bytes": 1024, "total_bytes": 1 << 30})
                    time.sleep(0.02)
                stopped.set()

        try:
            d._download_with_timeout(HangingYDL(), "http://x", time.time() + 0.5, time.time())
        except Exception as e:
            assert type(e).__name__ == "_DownloadTimeout", f"意外异常: {type(e).__name__}"
        else:
            raise AssertionError("应当抛 _DownloadTimeout")

        assert stopped.wait(5), "超时后工作线程没有停下来"

    def test_cancel_aborts_worker(self, tmp_path):
        import threading
        from utils import CancelToken
        token = CancelToken()
        d = _dl(tmp_path, slow_node_min_speed_kb=0)
        d._cancel = token
        stopped = threading.Event()

        class HangingYDL:
            def download(self, urls):
                while not d._abort.is_set():
                    d._progress_hook({"status": "downloading",
                                      "downloaded_bytes": 1024, "total_bytes": 1 << 30})
                    time.sleep(0.02)
                stopped.set()

        threading.Timer(0.3, token.cancel).start()
        try:
            d._download_with_timeout(HangingYDL(), "http://x", 0.0, time.time())
        except Exception as e:
            assert "Cancel" in type(e).__name__, f"意外异常: {type(e).__name__}"
        else:
            raise AssertionError("应当抛出取消异常")

        assert stopped.wait(5), "取消后工作线程没有停下来"
