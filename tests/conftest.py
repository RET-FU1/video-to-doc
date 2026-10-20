"""pytest 公共配置。

唯一职责：把 `tmp_path` 的 basetemp 固定到项目内的 `.pytest_tmp/`。

为什么：本机 `C:\\Users\\<用户>\\AppData\\Local\\Temp` 无法创建子目录
（`WinError 5 拒绝访问`），pytest 默认 basetemp 落在那里时，所有用到
`tmp_path` fixture 的用例都会 PermissionError；而 `tempfile.gettempdir()`
在候选目录都不可写时会回退到当前工作目录，导致 pytest 把临时目录直接
建在仓库根目录里（`pytest-of-<user>/`），污染工作区。

固定到 `.pytest_tmp/`（已在 .gitignore 中）后行为确定：不依赖系统临时目录、
不污染仓库根、跑完自动清理。
"""
import shutil
import uuid
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_BASETEMP_ROOT = _PROJECT_ROOT / ".pytest_tmp"


def pytest_configure(config):
    # 用户/CI 显式指定 --basetemp 时尊重它
    if config.option.basetemp:
        return
    try:
        _BASETEMP_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError:
        return  # 建不出来就退回 pytest 默认行为
    config.option.basetemp = str(_BASETEMP_ROOT / uuid.uuid4().hex[:8])


def pytest_sessionfinish(session, exitstatus):
    """清理本次会话的临时目录（只用不删别的）"""
    basetemp = session.config.option.basetemp
    if not basetemp:
        return
    path = Path(basetemp)
    try:
        path.relative_to(_BASETEMP_ROOT)
    except ValueError:
        return  # 不是我们建的，别动
    shutil.rmtree(path, ignore_errors=True)
    try:
        _BASETEMP_ROOT.rmdir()  # 空了就顺手删掉空目录
    except OSError:
        pass
