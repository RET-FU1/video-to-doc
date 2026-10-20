"""downloader 下载超时 / 进度 / 取消 测试（不联网）

这组测试针对一次真实事故：下载卡在无响应的 CDN 上，用户等了 10 分钟
没有任何输出，也不知道是在下载还是卡死了。根因有两个：
1. 进度回调只按 debug 级别打日志，GUI 里看不到 → 用户无法判断状态
2. 没有"整次下载总超时"，socket_timeout 又曾被误调成 7200s
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from downloader import Downloader, _DownloadTimeout
from utils import fmt_size
from utils import CancelToken, CancelledError


def _dl(tmp: Path, **cfg) -> Downloader:
    return Downloader({"downloader": cfg}, tmp)


class _SlowYDL:
    def __init__(self, seconds=30):
        self.seconds = seconds

    def download(self, urls):
        time.sleep(self.seconds)


class _FailingYDL:
    def download(self, urls):
        raise RuntimeError("模拟下载失败")


class TestFmtSize:
    def test_zero(self):
        assert fmt_size(0) == "?"

    def test_bytes(self):
        assert fmt_size(512) == "512B"

    def test_kb_mb(self):
        assert fmt_size(1536) == "1.5KB"
        assert fmt_size(231041989) == "220.3MB"


class TestDownloadTimeout:
    def test_times_out(self, tmp_path):
        d = _dl(tmp_path, timeout=1)
        start = time.time()
        try:
            d._download_with_timeout(_SlowYDL(30), "http://x", 1.0, start)
        except _DownloadTimeout as e:
            assert "超过" in str(e)
            assert time.time() - start < 10, "看门狗应在 ~1 秒内触发"
            return
        raise AssertionError("应当抛 _DownloadTimeout")

    def test_no_timeout_when_disabled(self, tmp_path):
        """timeout=0 表示不限制总时长；此时慢下载不会被看门狗打断"""

        class FastYDL:
            def download(self, urls):
                time.sleep(0.1)

        d = _dl(tmp_path, timeout=0)
        d._download_with_timeout(FastYDL(), "http://x", 0, time.time())

    def test_propagates_download_error(self, tmp_path):
        d = _dl(tmp_path, timeout=60)
        try:
            d._download_with_timeout(_FailingYDL(), "http://x", 60, time.time())
        except RuntimeError as e:
            assert "模拟下载失败" in str(e)
            return
        raise AssertionError("应把 yt-dlp 的异常原样抛回调用方")

    def test_cancel_interrupts_download(self, tmp_path):
        tok = CancelToken()
        tok.cancel()
        d = Downloader({"downloader": {"timeout": 60}}, tmp_path, cancel_token=tok)
        start = time.time()
        try:
            d._download_with_timeout(_SlowYDL(30), "http://x", 60, start)
        except CancelledError:
            assert time.time() - start < 10, "取消应立即生效"
            return
        raise AssertionError("取消时应抛 CancelledError")

    def test_success_returns(self, tmp_path):
        class FastYDL:
            def download(self, urls):
                return None

        d = _dl(tmp_path, timeout=60)
        d._download_with_timeout(FastYDL(), "http://x", 60, time.time())


class TestProgressHook:
    def test_downloading_logged_and_throttled(self, tmp_path, caplog):
        import logging
        d = _dl(tmp_path)
        payload = {
            "status": "downloading", "_percent_str": " 42.0%",
            "_speed_str": "1.5MiB/s", "_eta_str": "00:30",
            "downloaded_bytes": 42 * 1024 * 1024, "total_bytes": 100 * 1024 * 1024,
        }
        with caplog.at_level(logging.INFO, logger="downloader"):
            d._progress_hook(payload)
            first = d._last_progress_log
            d._progress_hook(payload)      # 10 秒内第二次应被限流
            assert d._last_progress_log == first, "进度日志应当限流"
        assert any("下载中" in r.message for r in caplog.records), "进度必须打到 INFO 级别"

    def test_finished_logged(self, tmp_path, caplog):
        import logging
        d = _dl(tmp_path)
        with caplog.at_level(logging.INFO, logger="downloader"):
            d._progress_hook({"status": "finished"})
        assert any("下载完成" in r.message for r in caplog.records)


class TestSocketTimeoutNotOverridden:
    def test_socket_timeout_stays_config_value(self, tmp_path):
        """回归：曾把 socket_timeout 改成 max(socket_timeout, timeout)=7200s，
        导致卡住的连接要等 2 小时。它必须保持配置里的单请求超时值。"""
        d = _dl(tmp_path, socket_timeout=60, timeout=7200)
        opts = d._ytdlp_opts(tmp_path, "%(title)s.%(ext)s")
        assert opts["socket_timeout"] == 60, (
            "socket_timeout 是单请求超时，不能被 downloader.timeout 覆盖"
        )

    def test_progress_hook_registered(self, tmp_path):
        d = _dl(tmp_path)
        opts = d._ytdlp_opts(tmp_path, "%(title)s.%(ext)s")
        assert len(opts["progress_hooks"]) == 1
